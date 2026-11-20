// SPDX-License-Identifier: GPL-3.0-or-later
// Android Open Accessory (AOA) host side for macOS, on IOKit. No libusb, no kernel extension.
//
// An Android phone can be switched into "accessory mode" over USB without USB debugging: the computer
// sends a few vendor control requests, the phone reappears as Google 18D1:2D00..2D05 with one bulk IN
// and one bulk OUT endpoint, and Android connects that pipe to the app that matches the strings we
// sent. See https://source.android.com/docs/core/interaction/accessories/aoa
#ifndef AOA_MAC_H
#define AOA_MAC_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define AOA_STRING_MAX 128

typedef struct {
    uint16_t vendor_id;
    uint16_t product_id;
    uint32_t location_id;   // identifies the port; stays the same across the mode switch
    int is_accessory;       // already in accessory mode (18D1:2D00..2D05)
    int looks_android;      // exposes an MTP/PTP/ADB-style interface, so worth asking about AOA
    char name[AOA_STRING_MAX];
    char serial[AOA_STRING_MAX];
} aoa_info;

enum {
    AOA_OK = 0,
    AOA_ERR_NOT_FOUND = -1,    // no such device on the bus
    AOA_ERR_OPEN = -2,         // couldn't open the device (another program has it, or no permission)
    AOA_ERR_UNSUPPORTED = -3,  // the device doesn't answer the AOA protocol request
    AOA_ERR_REQUEST = -4,      // a control request failed
    AOA_ERR_ENDPOINTS = -5,    // the accessory interface has no bulk IN/OUT pair
    AOA_ERR_IO = -6,           // a transfer failed
    AOA_ERR_NO_DEVICE = -7     // unplugged
};

typedef struct aoa_conn aoa_conn;

// Lists USB devices (reads the I/O registry; no USB traffic). Returns how many were stored.
int aoa_list(aoa_info *out, int capacity);

// Asks the device at `location_id` for its AOA protocol version, sends the six strings (manufacturer,
// model, description, version, URI, serial) and tells it to start accessory mode. The device then
// disconnects and reappears as an accessory at the same location. Returns AOA_OK, and the protocol
// version in *protocol_out.
int aoa_switch(uint32_t location_id, const char *const strings[6], int *protocol_out);

// Opens the accessory-mode device at `location_id` and finds its bulk endpoints.
aoa_conn *aoa_open(uint32_t location_id, int *error_out);

// Reads up to `capacity` bytes (use at least 16384). Returns the byte count, 0 on timeout, or a
// negative AOA_ERR_*.
int aoa_read(aoa_conn *conn, uint8_t *buffer, uint32_t capacity, uint32_t timeout_ms);

// Writes `length` bytes. Returns the count written, 0 on timeout, or a negative AOA_ERR_*.
int aoa_write(aoa_conn *conn, const uint8_t *buffer, uint32_t length, uint32_t timeout_ms);

void aoa_close(aoa_conn *conn);

// The IOKit return code behind the last failure on this thread, for logs.
uint32_t aoa_last_iokit_error(void);

#ifdef __cplusplus
}
#endif

#endif
