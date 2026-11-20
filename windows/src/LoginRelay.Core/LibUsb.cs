// SPDX-License-Identifier: GPL-3.0-or-later
// The real USB backend: a thin P/Invoke layer over libusb-1.0 (libusb-1.0.dll, bundled next to LoginRelay.exe;
// LGPL-2.1-or-later, linked dynamically). libusb on Windows can only open devices bound to WinUSB, and it has no
// hotplug there, so the engine polls ListDevices.
//
// Everything here is checked by hand against libusb.h (1.0.27); it can't be unit-tested without hardware. The
// structs are read with Marshal (no unsafe code). Every handle, device list and config descriptor is released
// in a finally block: a handle leak killed the first macOS build.
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace LoginRelay.Core
{
    /// <summary>The libusb entry points and struct layouts we use. 64-bit layouts are noted next to each struct.</summary>
    internal static class LibUsbNative
    {
        private const string Dll = "libusb-1.0";

        // libusb_error values
        public const int Success = 0;
        public const int ErrorIo = -1;
        public const int ErrorInvalidParam = -2;
        public const int ErrorAccess = -3;
        public const int ErrorNoDevice = -4;
        public const int ErrorNotFound = -5;
        public const int ErrorBusy = -6;
        public const int ErrorTimeout = -7;
        public const int ErrorOverflow = -8;
        public const int ErrorPipe = -9;
        public const int ErrorInterrupted = -10;
        public const int ErrorNoMem = -11;
        public const int ErrorNotSupported = -12;

        static LibUsbNative()
        {
            // Look for libusb-1.0.dll next to the program first (the single-file exe keeps it beside itself).
            try
            {
                NativeLibrary.SetDllImportResolver(typeof(LibUsbNative).Assembly, Resolve);
            }
            catch (Exception)
            {
                // A resolver was already set for this assembly; default probing still finds the app directory.
            }
        }

        private static IntPtr Resolve(string name, Assembly assembly, DllImportSearchPath? searchPath)
        {
            if (name != Dll)
            {
                return IntPtr.Zero;
            }
            var dirs = new List<string>();
            string? process = Environment.ProcessPath;
            if (!string.IsNullOrEmpty(process))
            {
                string? dir = Path.GetDirectoryName(process);
                if (!string.IsNullOrEmpty(dir))
                {
                    dirs.Add(dir);
                }
            }
            dirs.Add(AppContext.BaseDirectory);
            string? here = Path.GetDirectoryName(assembly.Location);
            if (!string.IsNullOrEmpty(here))
            {
                dirs.Add(here);
            }
            foreach (string dir in dirs)
            {
                string file = Path.Combine(dir, "libusb-1.0.dll");
                IntPtr handle;
                if (File.Exists(file) && NativeLibrary.TryLoad(file, out handle))
                {
                    return handle;
                }
            }
            return IntPtr.Zero; // fall back to the default search
        }

        public static string ErrorName(int code)
        {
            switch (code)
            {
                case 0: return "LIBUSB_SUCCESS";
                case -1: return "LIBUSB_ERROR_IO";
                case -2: return "LIBUSB_ERROR_INVALID_PARAM";
                case -3: return "LIBUSB_ERROR_ACCESS";
                case -4: return "LIBUSB_ERROR_NO_DEVICE";
                case -5: return "LIBUSB_ERROR_NOT_FOUND";
                case -6: return "LIBUSB_ERROR_BUSY";
                case -7: return "LIBUSB_ERROR_TIMEOUT";
                case -8: return "LIBUSB_ERROR_OVERFLOW";
                case -9: return "LIBUSB_ERROR_PIPE";
                case -10: return "LIBUSB_ERROR_INTERRUPTED";
                case -11: return "LIBUSB_ERROR_NO_MEM";
                case -12: return "LIBUSB_ERROR_NOT_SUPPORTED";
                case -99: return "LIBUSB_ERROR_OTHER";
                default: return "libusb error " + code;
            }
        }

        // struct libusb_device_descriptor: 18 bytes, no padding.
        [StructLayout(LayoutKind.Sequential)]
        public struct DeviceDescriptor
        {
            public byte bLength;
            public byte bDescriptorType;
            public ushort bcdUSB;
            public byte bDeviceClass;
            public byte bDeviceSubClass;
            public byte bDeviceProtocol;
            public byte bMaxPacketSize0;
            public ushort idVendor;
            public ushort idProduct;
            public ushort bcdDevice;
            public byte iManufacturer;
            public byte iProduct;
            public byte iSerialNumber;
            public byte bNumConfigurations;
        }

        // struct libusb_config_descriptor: 9 bytes, 7 padding, then three 8-byte fields (40 bytes on x64).
        [StructLayout(LayoutKind.Sequential)]
        public struct ConfigDescriptor
        {
            public byte bLength;
            public byte bDescriptorType;
            public ushort wTotalLength;
            public byte bNumInterfaces;
            public byte bConfigurationValue;
            public byte iConfiguration;
            public byte bmAttributes;
            public byte MaxPower;
            public IntPtr pInterface;       // const struct libusb_interface *interface (a C++ keyword in C#)
            public IntPtr extra;
            public int extraLength;
        }

        // struct libusb_interface: { const struct libusb_interface_descriptor *altsetting; int num_altsetting; } (16 bytes on x64).
        [StructLayout(LayoutKind.Sequential)]
        public struct InterfaceStruct
        {
            public IntPtr altsetting;
            public int numAltsetting;
        }

        // struct libusb_interface_descriptor: 9 bytes, 7 padding, then endpoint*, extra*, int (40 bytes on x64).
        [StructLayout(LayoutKind.Sequential)]
        public struct InterfaceDescriptor
        {
            public byte bLength;
            public byte bDescriptorType;
            public byte bInterfaceNumber;
            public byte bAlternateSetting;
            public byte bNumEndpoints;
            public byte bInterfaceClass;
            public byte bInterfaceSubClass;
            public byte bInterfaceProtocol;
            public byte iInterface;
            public IntPtr endpoint;
            public IntPtr extra;
            public int extraLength;
        }

        // struct libusb_endpoint_descriptor: 4 bytes, uint16, 3 bytes, 1 padding, extra*, int (32 bytes on x64).
        [StructLayout(LayoutKind.Sequential)]
        public struct EndpointDescriptor
        {
            public byte bLength;
            public byte bDescriptorType;
            public byte bEndpointAddress;
            public byte bmAttributes;
            public ushort wMaxPacketSize;
            public byte bInterval;
            public byte bRefresh;
            public byte bSynchAddress;
            public IntPtr extra;
            public int extraLength;
        }

        // On x64 Windows every calling convention is the same, so the default (Winapi) matches LIBUSB_CALL.
        [DllImport(Dll)]
        public static extern int libusb_init(out IntPtr context);

        [DllImport(Dll)]
        public static extern void libusb_exit(IntPtr context);

        // ssize_t libusb_get_device_list(libusb_context *, libusb_device ***list)
        [DllImport(Dll)]
        public static extern IntPtr libusb_get_device_list(IntPtr context, out IntPtr list);

        [DllImport(Dll)]
        public static extern void libusb_free_device_list(IntPtr list, int unrefDevices);

        [DllImport(Dll)]
        public static extern int libusb_get_device_descriptor(IntPtr device, out DeviceDescriptor descriptor);

        [DllImport(Dll)]
        public static extern byte libusb_get_bus_number(IntPtr device);

        [DllImport(Dll)]
        public static extern byte libusb_get_device_address(IntPtr device);

        [DllImport(Dll)]
        public static extern int libusb_get_port_numbers(IntPtr device, [Out] byte[] ports, int portNumbersLength);

        [DllImport(Dll)]
        public static extern int libusb_get_active_config_descriptor(IntPtr device, out IntPtr config);

        [DllImport(Dll)]
        public static extern void libusb_free_config_descriptor(IntPtr config);

        [DllImport(Dll)]
        public static extern int libusb_open(IntPtr device, out IntPtr handle);

        [DllImport(Dll)]
        public static extern void libusb_close(IntPtr handle);

        [DllImport(Dll)]
        public static extern int libusb_claim_interface(IntPtr handle, int interfaceNumber);

        [DllImport(Dll)]
        public static extern int libusb_release_interface(IntPtr handle, int interfaceNumber);

        [DllImport(Dll)]
        public static extern int libusb_clear_halt(IntPtr handle, byte endpoint);

        [DllImport(Dll)]
        public static extern int libusb_control_transfer(
            IntPtr handle, byte requestType, byte request, ushort value, ushort index,
            [In, Out] byte[]? data, ushort length, uint timeout);

        [DllImport(Dll)]
        public static extern int libusb_bulk_transfer(
            IntPtr handle, byte endpoint, [In, Out] byte[] data, int length, out int transferred, uint timeout);

        [DllImport(Dll)]
        public static extern int libusb_get_string_descriptor_ascii(IntPtr handle, byte index, [Out] byte[] data, int length);
    }

    /// <summary>One interface (alternate setting) of the active configuration.</summary>
    internal sealed class InterfaceInfo
    {
        public int Number;
        public int Alternate;
        public int Class;
        public int SubClass;
        public int Protocol;
        public List<byte[]> Endpoints = new List<byte[]>();   // { address, attributes }
    }

    public sealed class LibUsbBackend : IUsbBackend, IDisposable
    {
        private const int ControlTimeoutMs = 1000;

        private readonly object _lock = new object();
        private IntPtr _context = IntPtr.Zero;
        private bool _disposed;
        private readonly Dictionary<string, KeyValuePair<string, string>> _names = new Dictionary<string, KeyValuePair<string, string>>();

        // ---- context --------------------------------------------------------------------------------

        private IntPtr Context()
        {
            lock (_lock)
            {
                if (_disposed)
                {
                    throw new UsbException(UsbCode.Unavailable, "The USB library has been shut down.");
                }
                if (_context != IntPtr.Zero)
                {
                    return _context;
                }
                IntPtr ctx;
                int rc;
                try
                {
                    rc = LibUsbNative.libusb_init(out ctx);
                }
                catch (DllNotFoundException)
                {
                    throw new UsbException(UsbCode.Unavailable, UsbText.NeedsLibusb);
                }
                catch (BadImageFormatException)
                {
                    throw new UsbException(UsbCode.Unavailable, "libusb-1.0.dll isn't a 64-bit library; Login Relay needs the x64 build (VS2022/MS64).");
                }
                catch (EntryPointNotFoundException)
                {
                    throw new UsbException(UsbCode.Unavailable, "libusb-1.0.dll doesn't look like libusb 1.0.");
                }
                if (rc != 0 || ctx == IntPtr.Zero)
                {
                    throw new UsbException(UsbCode.Unavailable, "libusb_init failed: " + LibUsbNative.ErrorName(rc), rc);
                }
                _context = ctx;
                return ctx;
            }
        }

        /// <summary>Shuts libusb down. Close every connection first.</summary>
        public void Dispose()
        {
            lock (_lock)
            {
                if (_disposed)
                {
                    return;
                }
                _disposed = true;
                if (_context != IntPtr.Zero)
                {
                    try
                    {
                        LibUsbNative.libusb_exit(_context);
                    }
                    catch (Exception)
                    {
                    }
                    _context = IntPtr.Zero;
                }
            }
        }

        // ---- descriptors ----------------------------------------------------------------------------

        private static string LocationOf(IntPtr device)
        {
            int bus = LibUsbNative.libusb_get_bus_number(device);
            int address = LibUsbNative.libusb_get_device_address(device);
            byte[] ports = new byte[8];
            int count = LibUsbNative.libusb_get_port_numbers(device, ports, ports.Length);
            return UsbLocation.Format(bus, ports, count, address);
        }

        /// <summary>Reads the active configuration's interfaces. Descriptors only: nothing is sent to the device.</summary>
        private static List<InterfaceInfo> ReadInterfaces(IntPtr device)
        {
            var result = new List<InterfaceInfo>();
            IntPtr configPtr;
            if (LibUsbNative.libusb_get_active_config_descriptor(device, out configPtr) != 0 || configPtr == IntPtr.Zero)
            {
                return result;
            }
            try
            {
                var config = Marshal.PtrToStructure<LibUsbNative.ConfigDescriptor>(configPtr);
                if (config.pInterface == IntPtr.Zero)
                {
                    return result;
                }
                int interfaceSize = Marshal.SizeOf<LibUsbNative.InterfaceStruct>();
                int descriptorSize = Marshal.SizeOf<LibUsbNative.InterfaceDescriptor>();
                int endpointSize = Marshal.SizeOf<LibUsbNative.EndpointDescriptor>();
                for (int i = 0; i < config.bNumInterfaces && i < 32; i++)
                {
                    var iface = Marshal.PtrToStructure<LibUsbNative.InterfaceStruct>(IntPtr.Add(config.pInterface, i * interfaceSize));
                    if (iface.altsetting == IntPtr.Zero)
                    {
                        continue;
                    }
                    for (int a = 0; a < iface.numAltsetting && a < 32; a++)
                    {
                        var d = Marshal.PtrToStructure<LibUsbNative.InterfaceDescriptor>(IntPtr.Add(iface.altsetting, a * descriptorSize));
                        var info = new InterfaceInfo();
                        info.Number = d.bInterfaceNumber;
                        info.Alternate = d.bAlternateSetting;
                        info.Class = d.bInterfaceClass;
                        info.SubClass = d.bInterfaceSubClass;
                        info.Protocol = d.bInterfaceProtocol;
                        if (d.endpoint != IntPtr.Zero)
                        {
                            for (int e = 0; e < d.bNumEndpoints && e < 32; e++)
                            {
                                var ep = Marshal.PtrToStructure<LibUsbNative.EndpointDescriptor>(IntPtr.Add(d.endpoint, e * endpointSize));
                                info.Endpoints.Add(new[] { ep.bEndpointAddress, ep.bmAttributes });
                            }
                        }
                        result.Add(info);
                    }
                }
            }
            finally
            {
                LibUsbNative.libusb_free_config_descriptor(configPtr);
            }
            return result;
        }

        private static string ReadString(IntPtr handle, byte index)
        {
            if (index == 0)
            {
                return "";
            }
            byte[] buffer = new byte[256];
            int n = LibUsbNative.libusb_get_string_descriptor_ascii(handle, index, buffer, buffer.Length);
            if (n <= 0)
            {
                return "";
            }
            return Encoding.UTF8.GetString(buffer, 0, n).Trim();
        }

        /// <summary>Opens the device just long enough to read its product and serial strings. Returns the libusb error, or 0.</summary>
        private static int TryReadNames(IntPtr device, LibUsbNative.DeviceDescriptor descriptor, out string name, out string serial)
        {
            name = "";
            serial = "";
            IntPtr handle;
            int rc = LibUsbNative.libusb_open(device, out handle);
            if (rc != 0)
            {
                return rc;
            }
            try
            {
                name = ReadString(handle, descriptor.iProduct);
                serial = ReadString(handle, descriptor.iSerialNumber);
            }
            finally
            {
                LibUsbNative.libusb_close(handle);
            }
            return 0;
        }

        private UsbDevice? Describe(IntPtr device, HashSet<string> seen)
        {
            LibUsbNative.DeviceDescriptor d;
            if (LibUsbNative.libusb_get_device_descriptor(device, out d) != 0)
            {
                return null;
            }
            string location = LocationOf(device);
            List<InterfaceInfo> interfaces = ReadInterfaces(device);
            var triples = new List<(int Class, int Sub, int Protocol)>();
            foreach (InterfaceInfo i in interfaces)
            {
                triples.Add((i.Class, i.SubClass, i.Protocol));
            }
            bool accessory = Aoa.IsAccessoryIds(d.idVendor, d.idProduct);
            bool phone = UsbHeuristics.IsCandidate(d.idVendor, d.idProduct, triples);

            string name = "";
            string serial = "";
            bool openable = true;
            if (phone)
            {
                // Only a phone is ever opened (to read its name). Cached per attachment so polling doesn't reopen it.
                int address = LibUsbNative.libusb_get_device_address(device);
                string key = location + "|" + d.idVendor + "|" + d.idProduct + "|" + address;
                seen.Add(key);
                KeyValuePair<string, string> cached;
                if (_names.TryGetValue(key, out cached))
                {
                    name = cached.Key;
                    serial = cached.Value;
                }
                else
                {
                    int rc = TryReadNames(device, d, out name, out serial);
                    if (rc == 0)
                    {
                        _names[key] = new KeyValuePair<string, string>(name, serial);
                    }
                    else
                    {
                        openable = false;   // no WinUSB driver (or somebody else has it)
                    }
                }
                if (name.Length == 0)
                {
                    name = "Android phone (" + d.idVendor.ToString("x4") + ":" + d.idProduct.ToString("x4") + ")";
                }
            }
            return new UsbDevice(d.idVendor, d.idProduct, location, accessory, phone, name, serial, openable);
        }

        public IReadOnlyList<UsbDevice> ListDevices()
        {
            IntPtr context = Context();
            IntPtr list;
            long count = LibUsbNative.libusb_get_device_list(context, out list).ToInt64();
            if (count < 0)
            {
                throw new UsbException(UsbCode.Io, "libusb_get_device_list failed: " + LibUsbNative.ErrorName((int)count), (int)count);
            }
            var result = new List<UsbDevice>();
            var seen = new HashSet<string>();
            try
            {
                for (int i = 0; i < count; i++)
                {
                    IntPtr device = Marshal.ReadIntPtr(list, i * IntPtr.Size);
                    try
                    {
                        UsbDevice? described = Describe(device, seen);
                        if (described != null)
                        {
                            result.Add(described);
                        }
                    }
                    catch (Exception)
                    {
                        // One odd device must not hide the others.
                    }
                }
            }
            finally
            {
                LibUsbNative.libusb_free_device_list(list, 1);
            }
            var stale = new List<string>();
            foreach (string key in _names.Keys)
            {
                if (!seen.Contains(key))
                {
                    stale.Add(key);
                }
            }
            foreach (string key in stale)
            {
                _names.Remove(key);
            }
            return result;
        }

        // ---- finding a device by location -----------------------------------------------------------

        /// <summary>
        /// Runs <paramref name="action"/> with the libusb_device at this location (the device list is held while it
        /// runs and always freed). Throws NotFound if no device is there.
        /// </summary>
        private T WithDevice<T>(string location, Func<IntPtr, T> action)
        {
            IntPtr context = Context();
            IntPtr list;
            long count = LibUsbNative.libusb_get_device_list(context, out list).ToInt64();
            if (count < 0)
            {
                throw new UsbException(UsbCode.Io, "libusb_get_device_list failed: " + LibUsbNative.ErrorName((int)count), (int)count);
            }
            try
            {
                for (int i = 0; i < count; i++)
                {
                    IntPtr device = Marshal.ReadIntPtr(list, i * IntPtr.Size);
                    if (LocationOf(device) == location)
                    {
                        return action(device);
                    }
                }
            }
            finally
            {
                LibUsbNative.libusb_free_device_list(list, 1);
            }
            throw new UsbException(UsbCode.NotFound, "no device at " + location);
        }

        private static int MapOpenError(int rc)
        {
            switch (rc)
            {
                case LibUsbNative.ErrorAccess:
                    return UsbCode.Permission;
                case LibUsbNative.ErrorBusy:
                    return UsbCode.Busy;
                case LibUsbNative.ErrorNoDevice:
                    return UsbCode.NotFound;
                case LibUsbNative.ErrorNotSupported:
                case LibUsbNative.ErrorNotFound:
                    return UsbCode.NoDriver;
                default:
                    return UsbCode.Open;
            }
        }

        private static int Control(IntPtr handle, byte requestType, byte request, ushort index, byte[]? data, ushort length)
        {
            return LibUsbNative.libusb_control_transfer(handle, requestType, request, 0, index, data, length, ControlTimeoutMs);
        }

        // ---- the switch -----------------------------------------------------------------------------

        public UsbSwitchResult SwitchToAccessory(string location, string hostName)
        {
            IntPtr handle;
            try
            {
                handle = WithDevice(location, device =>
                {
                    IntPtr h;
                    int rc = LibUsbNative.libusb_open(device, out h);
                    if (rc != 0)
                    {
                        throw new UsbException(MapOpenError(rc), "libusb_open: " + LibUsbNative.ErrorName(rc), rc);
                    }
                    return h;
                });
            }
            catch (UsbException ex)
            {
                return new UsbSwitchResult(ex.Code, 0, ex.Detail);
            }

            try
            {
                // 1. Does the phone speak AOA, and which version?
                byte[] protocol = new byte[2];
                int rc = Control(handle, 0xC0, Aoa.ReqGetProtocol, 0, protocol, 2);
                int version = protocol[0] | (protocol[1] << 8);
                if (rc < 0)
                {
                    if (rc == LibUsbNative.ErrorNoDevice)
                    {
                        return new UsbSwitchResult(UsbCode.NotFound, 0, LibUsbNative.ErrorName(rc));
                    }
                    return new UsbSwitchResult(UsbCode.Unsupported, 0, "get protocol: " + LibUsbNative.ErrorName(rc));
                }
                if (version < 1)
                {
                    return new UsbSwitchResult(UsbCode.Unsupported, version, "protocol version " + version);
                }

                // 2. The six strings (the first two must match the phone app's accessory filter).
                string[] strings = Aoa.Strings(location, hostName);
                for (int i = 0; i < strings.Length; i++)
                {
                    byte[] text = Encoding.UTF8.GetBytes(strings[i] + "\0");
                    rc = Control(handle, 0x40, Aoa.ReqSendString, (ushort)i, text, (ushort)text.Length);
                    if (rc < 0)
                    {
                        return new UsbSwitchResult(UsbCode.Request, version, "send string " + i + ": " + LibUsbNative.ErrorName(rc));
                    }
                }

                // 3. Start. The device drops off the bus while handling this, so an error here is expected.
                Control(handle, 0x40, Aoa.ReqStart, 0, null, 0);
                return new UsbSwitchResult(UsbCode.Ok, version);
            }
            finally
            {
                LibUsbNative.libusb_close(handle);
            }
        }

        // ---- the pipe -------------------------------------------------------------------------------

        private sealed class OpenedAccessory
        {
            public IntPtr Handle;
            public byte EndpointIn;
            public byte EndpointOut;
        }

        public IUsbConnection OpenAccessory(string location)
        {
            OpenedAccessory opened = WithDevice(location, device =>
            {
                LibUsbNative.DeviceDescriptor d;
                if (LibUsbNative.libusb_get_device_descriptor(device, out d) != 0 || !Aoa.IsAccessoryIds(d.idVendor, d.idProduct))
                {
                    throw new UsbException(UsbCode.NotFound, "the device at " + location + " isn't in accessory mode");
                }
                // The accessory is interface 0 (with 18D1:2D01 there is a second one for adb: leave it alone).
                byte endpointIn = 0;
                byte endpointOut = 0;
                bool haveIn = false;
                bool haveOut = false;
                foreach (InterfaceInfo info in ReadInterfaces(device))
                {
                    if (info.Number != 0 || info.Alternate != 0)
                    {
                        continue;
                    }
                    foreach (byte[] ep in info.Endpoints)
                    {
                        bool bulk = (ep[1] & 0x03) == 0x02;
                        bool input = (ep[0] & 0x80) != 0;
                        if (!bulk)
                        {
                            continue;
                        }
                        if (input && !haveIn)
                        {
                            endpointIn = ep[0];
                            haveIn = true;
                        }
                        else if (!input && !haveOut)
                        {
                            endpointOut = ep[0];
                            haveOut = true;
                        }
                    }
                }
                if (!haveIn || !haveOut)
                {
                    throw new UsbException(UsbCode.Endpoints, "the accessory interface has no bulk endpoint pair");
                }
                IntPtr h;
                int rc = LibUsbNative.libusb_open(device, out h);
                if (rc != 0)
                {
                    throw new UsbException(MapOpenError(rc), "libusb_open: " + LibUsbNative.ErrorName(rc), rc);
                }
                var result = new OpenedAccessory();
                result.Handle = h;
                result.EndpointIn = endpointIn;
                result.EndpointOut = endpointOut;
                return result;
            });

            // Claim only interface 0. libusb has no kernel driver to detach on Windows.
            int claim = LibUsbNative.libusb_claim_interface(opened.Handle, 0);
            if (claim != 0)
            {
                LibUsbNative.libusb_close(opened.Handle);
                throw new UsbException(MapOpenError(claim), "libusb_claim_interface: " + LibUsbNative.ErrorName(claim), claim);
            }
            return new LibUsbConnection(opened.Handle, opened.EndpointIn, opened.EndpointOut);
        }
    }

    /// <summary>The accessory interface of one phone. Read and write may be used from different threads.</summary>
    internal sealed class LibUsbConnection : IUsbConnection
    {
        private readonly object _closeLock = new object();
        private readonly IntPtr _handle;
        private readonly byte _in;
        private readonly byte _out;
        private bool _closed;

        public LibUsbConnection(IntPtr handle, byte endpointIn, byte endpointOut)
        {
            _handle = handle;
            _in = endpointIn;
            _out = endpointOut;
        }

        private static UsbException Failure(int rc, string what)
        {
            int code = rc == LibUsbNative.ErrorNoDevice ? UsbCode.NoDevice : UsbCode.Io;
            return new UsbException(code, what + ": " + LibUsbNative.ErrorName(rc), rc);
        }

        public int Read(byte[] buffer, int timeoutMs)
        {
            if (Volatile.Read(ref _closed))
            {
                throw new UsbException(UsbCode.NoDevice, "closed");
            }
            int transferred;
            int rc = LibUsbNative.libusb_bulk_transfer(_handle, _in, buffer, buffer.Length, out transferred, (uint)timeoutMs);
            // A timeout is not an error; data that arrived before it still counts.
            if (rc == LibUsbNative.Success || rc == LibUsbNative.ErrorTimeout)
            {
                return transferred;
            }
            if (rc == LibUsbNative.ErrorPipe)
            {
                LibUsbNative.libusb_clear_halt(_handle, _in);
                return 0;
            }
            throw Failure(rc, "read");
        }

        public int Write(byte[] data, int offset, int count, int timeoutMs)
        {
            if (Volatile.Read(ref _closed))
            {
                throw new UsbException(UsbCode.NoDevice, "closed");
            }
            byte[] chunk = data;
            if (offset != 0 || count != data.Length)
            {
                chunk = new byte[count];
                Buffer.BlockCopy(data, offset, chunk, 0, count);
            }
            int transferred;
            int rc = LibUsbNative.libusb_bulk_transfer(_handle, _out, chunk, count, out transferred, (uint)timeoutMs);
            if (rc == LibUsbNative.Success || rc == LibUsbNative.ErrorTimeout)
            {
                return transferred;
            }
            if (rc == LibUsbNative.ErrorPipe)
            {
                LibUsbNative.libusb_clear_halt(_handle, _out);
                return 0;
            }
            throw Failure(rc, "write");
        }

        public void Close()
        {
            lock (_closeLock)
            {
                if (_closed)
                {
                    return;
                }
                Volatile.Write(ref _closed, true);
                try
                {
                    LibUsbNative.libusb_release_interface(_handle, 0);
                }
                catch (Exception)
                {
                }
                try
                {
                    LibUsbNative.libusb_close(_handle);
                }
                catch (Exception)
                {
                }
            }
        }
    }
}
