// SPDX-License-Identifier: GPL-3.0-or-later
// USB accessory mode (Android Open Accessory, "AOA"): the types the engine uses, and the pure helpers.
// The only code that touches libusb is LibUsb.cs; tests use a fake IUsbBackend.
using System;
using System.Collections.Generic;

namespace LoginRelay.Core
{
    /// <summary>The phone the engine is talking to (or about to). <see cref="Serial"/> is "usb:" + the port location.</summary>
    public sealed record Device(string Serial, string Name)
    {
        public string Title
        {
            get { return Name.Length == 0 ? Serial : Name; }
        }
    }

    public static class Aoa
    {
        public const ushort VendorGoogle = 0x18D1;
        public const ushort PidFirst = 0x2D00;
        public const ushort PidLast = 0x2D05;

        public const byte ReqGetProtocol = 51;
        public const byte ReqSendString = 52;
        public const byte ReqStart = 53;

        // The first two must match res/xml/accessory_filter.xml in the phone app.
        public const string Manufacturer = "Login Relay";
        public const string Model = "Desktop";
        public const string Version = "1";
        public const string Uri = "https://github.com/clippycoder/login-relay";

        public static bool IsAccessoryIds(int vendorId, int productId)
        {
            return vendorId == VendorGoogle && productId >= PidFirst && productId <= PidLast;
        }

        /// <summary>The six AOA strings in index order. The description (shown on the phone) is the computer's name.</summary>
        public static string[] Strings(string location, string hostName)
        {
            return new[]
            {
                Manufacturer,
                Model,
                string.IsNullOrEmpty(hostName) ? "Computer" : hostName,
                Version,
                Uri,
                location,
            };
        }
    }

    /// <summary>Result codes (same meaning as AOA_* in macos/Sources/aoa_mac.h, plus a few Windows ones).</summary>
    public static class UsbCode
    {
        public const int Ok = 0;
        public const int NotFound = -1;        // no such device on the bus
        public const int Open = -2;            // couldn't open the device
        public const int Unsupported = -3;     // the device doesn't answer the AOA protocol request
        public const int Request = -4;         // a control request failed
        public const int Endpoints = -5;       // the accessory interface has no bulk IN/OUT pair
        public const int Io = -6;              // a transfer failed
        public const int NoDevice = -7;        // unplugged
        public const int Permission = -8;      // the operating system won't let us use the device
        public const int Busy = -9;            // another program has it open
        public const int Unavailable = -10;    // libusb isn't there
        public const int NoDriver = -11;       // the device isn't bound to WinUSB
    }

    /// <summary>A USB operation failed. <see cref="Code"/> is one of <see cref="UsbCode"/>.</summary>
    public sealed class UsbException : Exception
    {
        public int Code { get; }
        public string Detail { get; }

        /// <summary>The raw libusb error (negative), or 0 if there wasn't one.</summary>
        public int LibusbCode { get; }

        public UsbException(int code, string detail, int libusbCode = 0)
            : base(detail.Length > 0 ? detail : "USB error " + code)
        {
            Code = code;
            Detail = detail;
            LibusbCode = libusbCode;
        }
    }

    /// <summary>One device on the USB bus, as libusb sees it.</summary>
    public sealed record UsbDevice(
        ushort VendorId,
        ushort ProductId,
        string Location,
        bool IsAccessory,
        bool LooksAndroid,
        string Name,
        string Serial,
        bool Openable = true)
    {
        /// <summary>What the user picks and the preferences store: the port location ("1-4.2").</summary>
        public string Id
        {
            get { return Location; }
        }

        /// <summary>"18D1 2D00".</summary>
        public string UsbId
        {
            get { return VendorId.ToString("X4") + " " + ProductId.ToString("X4"); }
        }
    }

    public sealed record UsbSwitchResult(int Code, int ProtocolVersion = 0, string Detail = "");

    /// <summary>An open accessory pipe: interface 0 of the phone, one bulk IN and one bulk OUT endpoint.</summary>
    public interface IUsbConnection
    {
        /// <summary>
        /// Reads up to buffer.Length bytes (use at least <see cref="UsbFraming.ReadBufferSize"/>). Returns 0 on a
        /// timeout, which is not an error. Throws <see cref="UsbException"/> if the transfer failed or the phone is gone.
        /// </summary>
        int Read(byte[] buffer, int timeoutMs);

        /// <summary>Writes data; returns the count written, 0 on a timeout. Throws <see cref="UsbException"/> on failure.</summary>
        int Write(byte[] data, int offset, int count, int timeoutMs);

        /// <summary>Releases the interface and the handle. Idempotent.</summary>
        void Close();
    }

    /// <summary>Everything the engine needs from the operating system's USB stack. Calls block; use them off the UI thread.</summary>
    public interface IUsbBackend
    {
        /// <summary>
        /// The devices on the bus. Reads descriptors only; talks to a device (to read its name) only if it looks
        /// like a phone. Throws <see cref="UsbException"/> (Unavailable) if there is no USB library.
        /// </summary>
        IReadOnlyList<UsbDevice> ListDevices();

        /// <summary>
        /// Asks the phone at <paramref name="location"/> to switch to accessory mode. It then drops off the bus and
        /// reappears at the same location as an accessory. Never throws for an ordinary failure; look at the code.
        /// </summary>
        UsbSwitchResult SwitchToAccessory(string location, string hostName);

        /// <summary>Opens the accessory-mode device at <paramref name="location"/>. Throws <see cref="UsbException"/>.</summary>
        IUsbConnection OpenAccessory(string location);
    }

    /// <summary>Pure rules for deciding what is a phone. No USB library.</summary>
    public static class UsbHeuristics
    {
        /// <summary>Qualcomm "QDLoader 9008" download mode. Never touch it.</summary>
        public static bool IsDownloadMode(int vendorId, int productId)
        {
            return vendorId == 0x05C6 && productId == 0x9008;
        }

        /// <summary>
        /// Does this device expose an interface Android phones use for MTP/PTP or ADB? Interfaces are
        /// (class, subclass, protocol) triples: 06/01/* (PTP, MTP), FF/42/01 (adb), FF/FF/00 (vendor MTP).
        /// </summary>
        public static bool LooksLikePhone(IEnumerable<(int Class, int Sub, int Protocol)> interfaces)
        {
            foreach ((int cls, int sub, int proto) in interfaces)
            {
                if ((cls == 0x06 && sub == 0x01)
                    || (cls == 0xFF && sub == 0x42 && proto == 0x01)
                    || (cls == 0xFF && sub == 0xFF && proto == 0x00))
                {
                    return true;
                }
            }
            return false;
        }

        /// <summary>
        /// Is it worth asking this device about accessory mode? Already an accessory, or looks like a phone, and
        /// not a download-mode device.
        /// </summary>
        public static bool IsCandidate(int vendorId, int productId, IEnumerable<(int Class, int Sub, int Protocol)> interfaces)
        {
            if (IsDownloadMode(vendorId, productId))
            {
                return false;
            }
            return Aoa.IsAccessoryIds(vendorId, productId) || LooksLikePhone(interfaces);
        }
    }

    public static class UsbLocation
    {
        /// <summary>
        /// A stable name for a physical port: bus number and port path, like Linux's "1-4.2". It survives the
        /// re-enumeration after the accessory switch (the device address does not). A device with no port
        /// path falls back to "bus:address".
        /// </summary>
        public static string Format(int bus, IReadOnlyList<byte> ports, int portCount, int address)
        {
            if (portCount > 0 && portCount <= ports.Count)
            {
                var parts = new string[portCount];
                for (int i = 0; i < portCount; i++)
                {
                    parts[i] = ports[i].ToString();
                }
                return bus + "-" + string.Join(".", parts);
            }
            return bus + ":" + address;
        }
    }

    /// <summary>Allows one accessory-switch attempt per location per interval; forgets locations that left the bus.</summary>
    public sealed class SwitchLimiter
    {
        private readonly TimeSpan _interval;
        private readonly Dictionary<string, DateTime> _last = new Dictionary<string, DateTime>();

        public SwitchLimiter(TimeSpan interval)
        {
            _interval = interval;
        }

        /// <summary>True (and records the attempt) if a switch at this location is allowed now.</summary>
        public bool TryAcquire(string location, DateTime now)
        {
            DateTime last;
            if (_last.TryGetValue(location, out last) && now - last < _interval)
            {
                return false;
            }
            _last[location] = now;
            return true;
        }

        /// <summary>A phone that was unplugged gets a fresh try when it comes back.</summary>
        public void Forget(IEnumerable<string> presentLocations)
        {
            var present = new HashSet<string>(presentLocations);
            var gone = new List<string>();
            foreach (string location in _last.Keys)
            {
                if (!present.Contains(location))
                {
                    gone.Add(location);
                }
            }
            foreach (string location in gone)
            {
                _last.Remove(location);
            }
        }
    }

    /// <summary>User-visible wording for USB errors.</summary>
    public static class UsbText
    {
        public const string NeedsLibusb = "USB mode needs libusb-1.0.dll next to LoginRelay.exe.";

        /// <summary>The one status shown when the phone is plugged in but libusb can't open it.</summary>
        public const string CantOpenHeadline = "Can't open the phone";

        public const string CantOpenDetail = "Login Relay needs a WinUSB driver for the phone. See “Windows drivers” in the README (github.com/clippycoder/login-relay).";

        /// <summary>No phone found: the same wording on every screen.</summary>
        public const string NoPhoneHeadline = "No phone found over USB";

        public const string NoPhoneDetail = "Plug the phone in and choose “File transfer” in its USB notification. USB debugging isn't needed.";

        /// <summary>Does this failure mean "libusb can't use that device" (no WinUSB driver, no access, not supported)?</summary>
        public static bool IsCantOpen(int code)
        {
            return code == UsbCode.NoDriver
                || code == UsbCode.Permission
                || code == UsbCode.Unsupported
                || code == UsbCode.Open;
        }

        /// <summary>The libusb name of a (negative) libusb error code, for diagnostics.</summary>
        public static string LibusbErrorName(int code)
        {
            return LibUsbNative.ErrorName(code);
        }

        public static string ErrorText(UsbSwitchResult r, string name)
        {
            switch (r.Code)
            {
                case UsbCode.Unavailable:
                    return r.Detail.Length > 0 ? r.Detail : NeedsLibusb;
                case UsbCode.Unsupported:
                    return name + " doesn't answer the USB accessory protocol.";
                case UsbCode.Busy:
                    return "Another program has " + name + " open (usually adb).";
                case UsbCode.Open:
                    return "Couldn't open " + name + " over USB.";
                case UsbCode.NotFound:
                    return name + " disappeared.";
                case UsbCode.Permission:
                    return "Windows won't let Login Relay use " + name + " over USB.";
                case UsbCode.NoDriver:
                    return CantOpenDetail;
                default:
                    return "Couldn't switch " + name + " to accessory mode (error " + r.Code + ").";
            }
        }
    }
}
