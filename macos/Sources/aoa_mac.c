// SPDX-License-Identifier: GPL-3.0-or-later
#include "aoa_mac.h"

#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/usb/IOUSBLib.h>
#include <IOKit/usb/USB.h>
#include <stdlib.h>
#include <string.h>

#define AOA_VENDOR_GOOGLE 0x18D1
#define AOA_PID_FIRST 0x2D00
#define AOA_PID_LAST 0x2D05

#define AOA_REQ_GET_PROTOCOL 51
#define AOA_REQ_SEND_STRING 52
#define AOA_REQ_START 53

static __thread uint32_t g_last_kr;

uint32_t aoa_last_iokit_error(void) { return g_last_kr; }

struct aoa_conn {
    IOUSBDeviceInterface320 **dev;
    IOUSBInterfaceInterface300 **intf;
    UInt8 pipe_in;
    UInt8 pipe_out;
};

// ---- I/O registry helpers ------------------------------------------------------------------

static int prop_int(io_registry_entry_t e, CFStringRef key, int64_t *out) {
    CFTypeRef v = IORegistryEntryCreateCFProperty(e, key, kCFAllocatorDefault, 0);
    if (!v) return 0;
    int ok = 0;
    if (CFGetTypeID(v) == CFNumberGetTypeID()) ok = CFNumberGetValue((CFNumberRef)v, kCFNumberSInt64Type, out);
    CFRelease(v);
    return ok;
}

static int prop_str(io_registry_entry_t e, CFStringRef key, char *out, size_t cap) {
    CFTypeRef v = IORegistryEntryCreateCFProperty(e, key, kCFAllocatorDefault, 0);
    if (!v) return 0;
    int ok = 0;
    if (CFGetTypeID(v) == CFStringGetTypeID()) ok = CFStringGetCString((CFStringRef)v, out, (CFIndex)cap, kCFStringEncodingUTF8);
    CFRelease(v);
    return ok;
}

// Visits every USB device node. macOS 11+ publishes IOUSBHostDevice; older systems IOUSBDevice.
static void for_each_device(void (*visit)(io_service_t, void *), void *ctx) {
    static const char *const classes[] = {"IOUSBHostDevice", "IOUSBDevice"};
    for (int c = 0; c < 2; c++) {
        io_iterator_t it = 0;
        if (IOServiceGetMatchingServices(MACH_PORT_NULL, IOServiceMatching(classes[c]), &it) != KERN_SUCCESS || !it) {
            continue;
        }
        int any = 0;
        io_service_t svc;
        while ((svc = IOIteratorNext(it)) != 0) {
            any = 1;
            visit(svc, ctx);
            IOObjectRelease(svc);
        }
        IOObjectRelease(it);
        if (any) return;
    }
}

// Does this device expose an interface that Android phones use for MTP/PTP or a debugging interface?
static int has_android_interface(io_service_t dev) {
    io_iterator_t kids = 0;
    if (IORegistryEntryGetChildIterator(dev, kIOServicePlane, &kids) != KERN_SUCCESS || !kids) return 0;
    int found = 0;
    io_registry_entry_t k;
    while ((k = IOIteratorNext(kids)) != 0) {
        int64_t cls = -1, sub = -1, proto = -1;
        if (prop_int(k, CFSTR("bInterfaceClass"), &cls)) {
            prop_int(k, CFSTR("bInterfaceSubClass"), &sub);
            prop_int(k, CFSTR("bInterfaceProtocol"), &proto);
            if ((cls == 0x06 && sub == 0x01)                  // still image / PTP / MTP
                || (cls == 0xFF && sub == 0x42 && proto == 0x01)  // a phone's debugging interface
                || (cls == 0xFF && sub == 0xFF && proto == 0x00)) // vendor-specific MTP
                found = 1;
        }
        IOObjectRelease(k);
    }
    IOObjectRelease(kids);
    return found;
}

typedef struct {
    aoa_info *out;
    int capacity;
    int count;
} list_ctx;

static void list_visit(io_service_t svc, void *p) {
    list_ctx *ctx = (list_ctx *)p;
    if (ctx->count >= ctx->capacity) return;
    int64_t vid = 0, pid = 0, loc = 0;
    if (!prop_int(svc, CFSTR("idVendor"), &vid) || !prop_int(svc, CFSTR("idProduct"), &pid)) return;
    prop_int(svc, CFSTR("locationID"), &loc);
    aoa_info *i = &ctx->out[ctx->count];
    memset(i, 0, sizeof *i);
    i->vendor_id = (uint16_t)vid;
    i->product_id = (uint16_t)pid;
    i->location_id = (uint32_t)loc;
    i->is_accessory = (vid == AOA_VENDOR_GOOGLE && pid >= AOA_PID_FIRST && pid <= AOA_PID_LAST);
    i->looks_android = i->is_accessory || has_android_interface(svc);
    if (!prop_str(svc, CFSTR("USB Product Name"), i->name, sizeof i->name)) {
        prop_str(svc, CFSTR("kUSBProductString"), i->name, sizeof i->name);
    }
    if (!prop_str(svc, CFSTR("USB Serial Number"), i->serial, sizeof i->serial)) {
        prop_str(svc, CFSTR("kUSBSerialNumberString"), i->serial, sizeof i->serial);
    }
    ctx->count++;
}

int aoa_list(aoa_info *out, int capacity) {
    list_ctx ctx = {out, capacity, 0};
    for_each_device(list_visit, &ctx);
    return ctx.count;
}

typedef struct {
    uint32_t location;
    io_service_t found;
} find_ctx;

static void find_visit(io_service_t svc, void *p) {
    find_ctx *ctx = (find_ctx *)p;
    if (ctx->found) return;
    int64_t loc = 0;
    if (prop_int(svc, CFSTR("locationID"), &loc) && (uint32_t)loc == ctx->location) {
        IOObjectRetain(svc);
        ctx->found = svc;
    }
}

// Returns a retained service for the device at this location, or 0.
static io_service_t find_device(uint32_t location) {
    find_ctx ctx = {location, 0};
    for_each_device(find_visit, &ctx);
    return ctx.found;
}

// ---- device access ---------------------------------------------------------------------------

static IOUSBDeviceInterface320 **open_device(io_service_t svc) {
    IOCFPlugInInterface **plugin = NULL;
    SInt32 score = 0;
    kern_return_t kr = IOCreatePlugInInterfaceForService(svc, kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID,
                                                         &plugin, &score);
    if (kr != KERN_SUCCESS || !plugin) {
        g_last_kr = (uint32_t)kr;
        return NULL;
    }
    IOUSBDeviceInterface320 **dev = NULL;
    HRESULT hr = (*plugin)->QueryInterface(plugin, CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID320), (LPVOID *)&dev);
    IODestroyPlugInInterface(plugin);
    if (hr != 0 || !dev) {
        g_last_kr = (uint32_t)hr;
        return NULL;
    }
    // Other programs (the system's camera/MTP helpers, other USB tools) may hold a phone; ask them to let go.
    kr = (*dev)->USBDeviceOpenSeize(dev);
    if (kr != kIOReturnSuccess) {
        g_last_kr = (uint32_t)kr;
        (*dev)->Release(dev);
        return NULL;
    }
    return dev;
}

static IOReturn control(IOUSBDeviceInterface320 **dev, UInt8 direction, UInt8 request, UInt16 index, void *data,
                        UInt16 length) {
    IOUSBDevRequest req;
    memset(&req, 0, sizeof req);
    req.bmRequestType = USBmakebmRequestType(direction, kUSBVendor, kUSBDevice);
    req.bRequest = request;
    req.wValue = 0;
    req.wIndex = index;
    req.wLength = length;
    req.pData = data;
    return (*dev)->DeviceRequest(dev, &req);
}

int aoa_switch(uint32_t location_id, const char *const strings[6], int *protocol_out) {
    io_service_t svc = find_device(location_id);
    if (!svc) return AOA_ERR_NOT_FOUND;
    IOUSBDeviceInterface320 **dev = open_device(svc);
    IOObjectRelease(svc);
    if (!dev) return AOA_ERR_OPEN;

    int result = AOA_OK;
    UInt8 proto[2] = {0, 0};
    IOReturn kr = control(dev, kUSBIn, AOA_REQ_GET_PROTOCOL, 0, proto, 2);
    int version = proto[0] | (proto[1] << 8);
    if (protocol_out) *protocol_out = version;
    if (kr != kIOReturnSuccess || version < 1) {
        g_last_kr = (uint32_t)kr;
        result = AOA_ERR_UNSUPPORTED;
    } else {
        for (int i = 0; i < 6 && result == AOA_OK; i++) {
            const char *s = strings[i] ? strings[i] : "";
            kr = control(dev, kUSBOut, AOA_REQ_SEND_STRING, (UInt16)i, (void *)s, (UInt16)(strlen(s) + 1));
            if (kr != kIOReturnSuccess) {
                g_last_kr = (uint32_t)kr;
                result = AOA_ERR_REQUEST;
            }
        }
        if (result == AOA_OK) {
            // The device drops off the bus while handling this, so an error here is expected.
            control(dev, kUSBOut, AOA_REQ_START, 0, NULL, 0);
        }
    }
    (*dev)->USBDeviceClose(dev);
    (*dev)->Release(dev);
    return result;
}

// Finds the interface node with this bInterfaceNumber among the device's children (retained), or 0.
static io_service_t find_interface(io_service_t dev, int number) {
    io_iterator_t kids = 0;
    if (IORegistryEntryGetChildIterator(dev, kIOServicePlane, &kids) != KERN_SUCCESS || !kids) return 0;
    io_service_t found = 0, k;
    while ((k = IOIteratorNext(kids)) != 0) {
        int64_t n = -1;
        if (!found && prop_int(k, CFSTR("bInterfaceNumber"), &n) && n == number) {
            found = k;  // keep our reference
        } else {
            IOObjectRelease(k);
        }
    }
    IOObjectRelease(kids);
    return found;
}

aoa_conn *aoa_open(uint32_t location_id, int *error_out) {
    int dummy;
    if (!error_out) error_out = &dummy;
    *error_out = AOA_OK;

    io_service_t svc = find_device(location_id);
    if (!svc) {
        *error_out = AOA_ERR_NOT_FOUND;
        return NULL;
    }
    int64_t vid = 0, pid = 0;
    prop_int(svc, CFSTR("idVendor"), &vid);
    prop_int(svc, CFSTR("idProduct"), &pid);
    if (vid != AOA_VENDOR_GOOGLE || pid < AOA_PID_FIRST || pid > AOA_PID_LAST) {
        IOObjectRelease(svc);
        *error_out = AOA_ERR_NOT_FOUND;
        return NULL;
    }

    // The accessory is interface 0 (with 18D1:2D01 there is a second, debugging one). Open that interface
    // directly rather than the whole device: another program may hold the device exclusively, and
    // this way we don't have to compete with it for something we don't need.
    io_service_t isvc = find_interface(svc, 0);
    IOObjectRelease(svc);
    if (!isvc) {
        *error_out = AOA_ERR_ENDPOINTS;
        return NULL;
    }
    IOCFPlugInInterface **plugin = NULL;
    SInt32 score = 0;
    kern_return_t pk = IOCreatePlugInInterfaceForService(isvc, kIOUSBInterfaceUserClientTypeID, kIOCFPlugInInterfaceID,
                                                         &plugin, &score);
    IOObjectRelease(isvc);
    if (pk != KERN_SUCCESS || !plugin) {
        g_last_kr = (uint32_t)pk;
        *error_out = AOA_ERR_OPEN;
        return NULL;
    }
    IOUSBInterfaceInterface300 **chosen = NULL;
    HRESULT hr = (*plugin)->QueryInterface(plugin, CFUUIDGetUUIDBytes(kIOUSBInterfaceInterfaceID300), (LPVOID *)&chosen);
    IODestroyPlugInInterface(plugin);
    if (hr != 0 || !chosen) {
        g_last_kr = (uint32_t)hr;
        *error_out = AOA_ERR_OPEN;
        return NULL;
    }

    IOReturn kr = (*chosen)->USBInterfaceOpen(chosen);
    if (kr != kIOReturnSuccess) {
        g_last_kr = (uint32_t)kr;
        (*chosen)->Release(chosen);
        *error_out = AOA_ERR_OPEN;
        return NULL;
    }

    UInt8 count = 0, in_pipe = 0, out_pipe = 0;
    (*chosen)->GetNumEndpoints(chosen, &count);
    for (UInt8 p = 1; p <= count; p++) {
        UInt8 dir = 0, num = 0, xfer = 0, interval = 0;
        UInt16 max_packet = 0;
        if ((*chosen)->GetPipeProperties(chosen, p, &dir, &num, &xfer, &max_packet, &interval) != kIOReturnSuccess) continue;
        if (xfer != kUSBBulk) continue;
        if (dir == kUSBIn && !in_pipe) in_pipe = p;
        if (dir == kUSBOut && !out_pipe) out_pipe = p;
    }
    if (!in_pipe || !out_pipe) {
        (*chosen)->USBInterfaceClose(chosen);
        (*chosen)->Release(chosen);
        *error_out = AOA_ERR_ENDPOINTS;
        return NULL;
    }

    aoa_conn *c = (aoa_conn *)calloc(1, sizeof *c);
    c->dev = NULL;
    c->intf = chosen;
    c->pipe_in = in_pipe;
    c->pipe_out = out_pipe;
    return c;
}

static int map_error(aoa_conn *c, IOReturn kr) {
    g_last_kr = (uint32_t)kr;
    if (kr == kIOUSBTransactionTimeout) return 0;
    if (kr == kIOReturnNoDevice || kr == kIOReturnNotAttached || kr == kIOReturnAborted ||
        kr == kIOUSBPipeStalled) {
        if (kr == kIOUSBPipeStalled) {
            (*c->intf)->ClearPipeStallBothEnds(c->intf, c->pipe_in);
            return 0;
        }
        return AOA_ERR_NO_DEVICE;
    }
    return AOA_ERR_IO;
}

int aoa_read(aoa_conn *c, uint8_t *buffer, uint32_t capacity, uint32_t timeout_ms) {
    UInt32 size = capacity;
    IOReturn kr = (*c->intf)->ReadPipeTO(c->intf, c->pipe_in, buffer, &size, timeout_ms, timeout_ms);
    if (kr == kIOReturnSuccess) return (int)size;
    return map_error(c, kr);
}

int aoa_write(aoa_conn *c, const uint8_t *buffer, uint32_t length, uint32_t timeout_ms) {
    IOReturn kr = (*c->intf)->WritePipeTO(c->intf, c->pipe_out, (void *)buffer, length, timeout_ms, timeout_ms);
    if (kr == kIOReturnSuccess) return (int)length;
    int r = map_error(c, kr);
    if (r == 0) (*c->intf)->ClearPipeStallBothEnds(c->intf, c->pipe_out);
    return r;
}

void aoa_close(aoa_conn *c) {
    if (!c) return;
    (*c->intf)->USBInterfaceClose(c->intf);
    (*c->intf)->Release(c->intf);
    if (c->dev) {
        (*c->dev)->USBDeviceClose(c->dev);
        (*c->dev)->Release(c->dev);
    }
    free(c);
}
