// SPDX-License-Identifier: GPL-3.0-or-later
// Driver diagnosis: turns "what libusb can see" plus "what Windows sees" into one setup state and the exact
// next step for the user. Pure logic (no WMI, no libusb), so it is unit-tested with a table of cases.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.RegularExpressions;

namespace LoginRelay.Core
{
    /// <summary>One USB entry in Windows' Plug and Play tree (Win32_PnPEntity).</summary>
    public sealed record PnpDevice(
        string Name,
        string DeviceId,
        string[] HardwareIds,
        string[] CompatibleIds,
        string? Service,
        int ConfigManagerErrorCode,
        bool? Present);

    /// <summary>Windows' view of the USB devices. The real one asks WMI; tests use a fake.</summary>
    public interface IUsbDiagnostics
    {
        /// <summary>Entries whose DeviceID starts with USB\VID_. Blocks (WMI is slow); call off the UI thread. May throw.</summary>
        IReadOnlyList<PnpDevice> QueryUsbDevices();
    }

    public enum UsbSetupState
    {
        /// <summary>An accessory-mode device is visible to libusb: open it.</summary>
        ReadyToConnect,

        /// <summary>A candidate phone is visible to libusb (WinUSB bound): switch it.</summary>
        CanSwitch,

        /// <summary>The phone is in accessory mode but libusb can't use it: Zadig step 2.</summary>
        NeedsAccessoryDriver,

        /// <summary>Windows shows a phone that libusb can't use: Zadig step 1.</summary>
        NeedsPhoneDriver,

        /// <summary>Nothing found.</summary>
        NoPhone,
    }

    /// <summary>The diagnosis. Value equality, so the engine can tell when it changed.</summary>
    public sealed record UsbSetupInfo(
        UsbSetupState State,
        ushort VendorId,
        ushort ProductId,
        string DeviceName,
        string? Reason = null)
    {
        public bool IsDriverProblem
        {
            get { return State == UsbSetupState.NeedsAccessoryDriver || State == UsbSetupState.NeedsPhoneDriver; }
        }

        public bool HasId
        {
            get { return VendorId != 0 || ProductId != 0; }
        }

        /// <summary>"18D1 2D00", the way Zadig shows a USB ID.</summary>
        public string UsbId
        {
            get { return VendorId.ToString("X4") + " " + ProductId.ToString("X4"); }
        }
    }

    public static class UsbDiagnosisLogic
    {
        public const string AccessoryName = "Android Accessory Interface";

        private static readonly Regex IdPattern = new Regex(
            @"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(?:&REV_[0-9A-F]{4})?(?:&MI_([0-9A-F]{2}))?",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

        /// <summary>Reads VID, PID and (for a composite function) the interface number from a Windows DeviceID.</summary>
        public static bool TryParseDeviceId(string deviceId, out ushort vendor, out ushort product, out int interfaceNumber)
        {
            vendor = 0;
            product = 0;
            interfaceNumber = -1;
            Match m = IdPattern.Match(deviceId ?? "");
            if (!m.Success)
            {
                return false;
            }
            vendor = Convert.ToUInt16(m.Groups[1].Value, 16);
            product = Convert.ToUInt16(m.Groups[2].Value, 16);
            if (m.Groups[3].Success)
            {
                interfaceNumber = Convert.ToInt32(m.Groups[3].Value, 16);
            }
            return true;
        }

        /// <summary>
        /// Windows compatible IDs that mean "phone": PTP/MTP (06/01/*), adb (FF/42/01), vendor MTP (FF/FF/00).
        /// The shorter forms Windows also lists (USB\Class_FF&amp;SubClass_FF, USB\Class_FF) are deliberately not
        /// matched: they would also match unrelated vendor-specific devices.
        /// </summary>
        public static bool IsPhoneCompatibleId(string id)
        {
            string s = (id ?? "").Trim();
            if (s.Equals(@"USB\Class_06&SubClass_01", StringComparison.OrdinalIgnoreCase)
                || s.StartsWith(@"USB\Class_06&SubClass_01&", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
            return s.Equals(@"USB\Class_FF&SubClass_42&Prot_01", StringComparison.OrdinalIgnoreCase)
                || s.Equals(@"USB\Class_FF&SubClass_FF&Prot_00", StringComparison.OrdinalIgnoreCase);
        }

        private sealed class Group
        {
            public ushort Vendor;
            public ushort Product;
            public List<PnpDevice> Entries = new List<PnpDevice>();
            public List<int> Interfaces = new List<int>();
        }

        private static List<Group> GroupWindows(IReadOnlyList<PnpDevice> windows)
        {
            var groups = new List<Group>();
            foreach (PnpDevice p in windows)
            {
                if (p.Present == false)
                {
                    continue;
                }
                ushort vid, pid;
                int mi;
                if (!TryParseDeviceId(p.DeviceId, out vid, out pid, out mi))
                {
                    continue;
                }
                Group? g = groups.FirstOrDefault(x => x.Vendor == vid && x.Product == pid);
                if (g == null)
                {
                    g = new Group();
                    g.Vendor = vid;
                    g.Product = pid;
                    groups.Add(g);
                }
                g.Entries.Add(p);
                g.Interfaces.Add(mi);
            }
            return groups;
        }

        private static bool GroupLooksLikePhone(Group g)
        {
            return g.Entries.Any(e => (e.CompatibleIds ?? new string[0]).Any(IsPhoneCompatibleId));
        }

        /// <summary>Best human name for a Windows device group: the whole device if it has a real name, else a function's.</summary>
        private static string GroupName(Group g)
        {
            for (int i = 0; i < g.Entries.Count; i++)
            {
                string name = (g.Entries[i].Name ?? "").Trim();
                bool generic = name.Length == 0
                    || name.Equals("USB Composite Device", StringComparison.OrdinalIgnoreCase)
                    || name.StartsWith("Unknown", StringComparison.OrdinalIgnoreCase);
                if (g.Interfaces[i] < 0 && !generic)
                {
                    return name;
                }
            }
            foreach (PnpDevice e in g.Entries)
            {
                string name = (e.Name ?? "").Trim();
                if (name.Length > 0 && !name.StartsWith("Unknown", StringComparison.OrdinalIgnoreCase)
                    && !name.Equals("USB Composite Device", StringComparison.OrdinalIgnoreCase))
                {
                    return name;
                }
            }
            return "USB device " + g.Vendor.ToString("x4") + ":" + g.Product.ToString("x4");
        }

        /// <summary>Why a Windows entry that libusb can't use isn't usable (for the accessory step).</summary>
        private static string? ReasonFor(Group? g)
        {
            if (g == null)
            {
                return null;
            }
            if (g.Entries.Any(e => e.ConfigManagerErrorCode == 28))
            {
                return "Windows reports that no driver is installed for it.";
            }
            if (g.Entries.Any(e => string.Equals(e.Service, "winusb", StringComparison.OrdinalIgnoreCase)))
            {
                return "WinUSB is already installed for it, but Login Relay can't open it yet. Unplug and replug the phone, then choose Check again.";
            }
            string? service = g.Entries.Select(e => e.Service).FirstOrDefault(s => !string.IsNullOrEmpty(s));
            if (service != null)
            {
                return "Windows is using the \"" + service + "\" driver for it, not WinUSB.";
            }
            return null;
        }

        /// <summary>
        /// Decides the setup state. Precedence: an openable accessory, an openable candidate phone, an
        /// accessory-mode device that libusb can't use, a phone that libusb can't use, nothing.
        /// </summary>
        public static UsbSetupInfo Diagnose(IReadOnlyList<UsbDevice> libusb, IReadOnlyList<PnpDevice> windows)
        {
            List<Group> groups = GroupWindows(windows ?? new List<PnpDevice>());

            // a) accessory mode, visible to libusb
            UsbDevice? accessory = libusb.FirstOrDefault(d => d.IsAccessory && d.Openable);
            if (accessory != null)
            {
                return new UsbSetupInfo(UsbSetupState.ReadyToConnect, accessory.VendorId, accessory.ProductId, accessory.Name);
            }

            // b) a candidate phone, visible to libusb
            UsbDevice? candidate = libusb.FirstOrDefault(
                d => d.LooksAndroid && !d.IsAccessory && d.Openable && !UsbHeuristics.IsDownloadMode(d.VendorId, d.ProductId));
            if (candidate != null)
            {
                return new UsbSetupInfo(UsbSetupState.CanSwitch, candidate.VendorId, candidate.ProductId, candidate.Name);
            }

            // c) accessory mode present, but libusb can't use it
            UsbDevice? blockedAccessory = libusb.FirstOrDefault(d => d.IsAccessory && !d.Openable);
            Group? windowsAccessory = groups.FirstOrDefault(g => Aoa.IsAccessoryIds(g.Vendor, g.Product));
            if (blockedAccessory != null || windowsAccessory != null)
            {
                ushort vid = blockedAccessory != null ? blockedAccessory.VendorId : windowsAccessory!.Vendor;
                ushort pid = blockedAccessory != null ? blockedAccessory.ProductId : windowsAccessory!.Product;
                Group? same = groups.FirstOrDefault(g => g.Vendor == vid && g.Product == pid);
                return new UsbSetupInfo(UsbSetupState.NeedsAccessoryDriver, vid, pid, AccessoryName, ReasonFor(same));
            }

            // d) a phone that libusb can't use
            UsbDevice? blockedPhone = libusb.FirstOrDefault(
                d => d.LooksAndroid && !d.IsAccessory && !d.Openable && !UsbHeuristics.IsDownloadMode(d.VendorId, d.ProductId));
            if (blockedPhone != null)
            {
                Group? same = groups.FirstOrDefault(g => g.Vendor == blockedPhone.VendorId && g.Product == blockedPhone.ProductId);
                string name = same != null ? GroupName(same) : blockedPhone.Name;
                return new UsbSetupInfo(UsbSetupState.NeedsPhoneDriver, blockedPhone.VendorId, blockedPhone.ProductId, name);
            }
            Group? windowsPhone = groups.FirstOrDefault(
                g => !UsbHeuristics.IsDownloadMode(g.Vendor, g.Product)
                    && !Aoa.IsAccessoryIds(g.Vendor, g.Product)
                    && GroupLooksLikePhone(g));
            if (windowsPhone != null)
            {
                return new UsbSetupInfo(UsbSetupState.NeedsPhoneDriver, windowsPhone.Vendor, windowsPhone.Product, GroupName(windowsPhone));
            }

            // e) nothing
            return new UsbSetupInfo(UsbSetupState.NoPhone, 0, 0, "");
        }
    }

    /// <summary>The words for each setup state. Shown in the setup window and, shortened, in the menu and the activity window.</summary>
    public static class UsbSetupText
    {
        public const string NoPhoneHint = "Plug the phone in and choose “File transfer” in its USB notification.";
        public const string SetupMenuName = "USB driver setup…";

        public static string Title(UsbSetupInfo? info)
        {
            if (info == null)
            {
                return "Looking for the phone…";
            }
            switch (info.State)
            {
                case UsbSetupState.NeedsPhoneDriver:
                    return "Step 1 of 2: install a USB driver for the phone";
                case UsbSetupState.NeedsAccessoryDriver:
                    return "Step 2 of 2: install a USB driver for accessory mode";
                case UsbSetupState.CanSwitch:
                    return "The phone is ready to switch";
                case UsbSetupState.ReadyToConnect:
                    return "Accessory mode is ready";
                default:
                    return "No phone found over USB";
            }
        }

        /// <summary>A short headline for the status line, or null when the state isn't a driver problem.</summary>
        public static string? Headline(UsbSetupInfo? info)
        {
            if (info == null)
            {
                return null;
            }
            if (info.State == UsbSetupState.NeedsPhoneDriver)
            {
                return info.DeviceName + " needs a USB driver";
            }
            if (info.State == UsbSetupState.NeedsAccessoryDriver)
            {
                return "The phone's accessory mode needs a USB driver";
            }
            return null;
        }

        /// <summary>One sentence: the next step.</summary>
        public static string Summary(UsbSetupInfo info)
        {
            switch (info.State)
            {
                case UsbSetupState.NeedsPhoneDriver:
                    return "Step 1 of 2: with Zadig, install the WinUSB driver for " + info.DeviceName
                        + " (USB ID " + info.UsbId + "). Choose “" + UsbSetupText.SetupMenuName + "” in the tray menu for the exact steps.";
                case UsbSetupState.NeedsAccessoryDriver:
                    return "Step 2 of 2: with Zadig, install the WinUSB driver for “" + UsbDiagnosisLogic.AccessoryName
                        + "” (USB ID " + info.UsbId + "). Choose “" + UsbSetupText.SetupMenuName + "” in the tray menu for the exact steps.";
                case UsbSetupState.CanSwitch:
                    return info.DeviceName + " is visible over USB. Login Relay will ask it to switch to accessory mode.";
                case UsbSetupState.ReadyToConnect:
                    return "The phone is in accessory mode and Login Relay can open it.";
                default:
                    return NoPhoneHint;
            }
        }

        /// <summary>The full instructions for the current state.</summary>
        public static string Steps(UsbSetupInfo info)
        {
            string nl = Environment.NewLine;
            switch (info.State)
            {
                case UsbSetupState.NeedsPhoneDriver:
                    return "1. Download and start Zadig (zadig.akeo.ie)." + nl
                        + "2. In Zadig choose Options > List All Devices." + nl
                        + "3. Pick " + info.DeviceName + " (USB ID " + info.UsbId + ") in the drop-down. If the list has several entries for it, for example “... (Interface 0)”, pick the first one." + nl
                        + "4. Make sure the target driver box shows WinUSB, then click Replace Driver (or Install Driver) and wait." + nl
                        + nl
                        + "Note: while this driver is installed Windows can't use this phone for file transfer; to undo it, open Device Manager, find the phone, choose Uninstall device and tick ‘Attempt to remove the driver for this device’, then unplug and replug the phone.";
                case UsbSetupState.NeedsAccessoryDriver:
                {
                    string id = info.HasId ? "USB ID " + info.UsbId : "USB ID 18D1 2D00 or 2D01";
                    string text = "The phone is now in accessory mode and Windows shows ‘" + UsbDiagnosisLogic.AccessoryName
                        + "’ (" + id + "). In Zadig (Options > List All Devices) pick it, with WinUSB as the target, and Replace/Install Driver. You only need to do each step once.";
                    if (!string.IsNullOrEmpty(info.Reason))
                    {
                        text += nl + nl + info.Reason;
                    }
                    return text;
                }
                case UsbSetupState.CanSwitch:
                    return Summary(info) + nl + "Nothing to install.";
                case UsbSetupState.ReadyToConnect:
                    return Summary(info) + nl + "Nothing to install.";
                default:
                    return NoPhoneHint + nl + "USB debugging isn't needed. If the phone is plugged in and still isn't found, Windows may have no driver for it yet; Login Relay will tell you here.";
            }
        }
    }
}
