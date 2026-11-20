// SPDX-License-Identifier: GPL-3.0-or-later
// Windows' view of the USB devices, from WMI (Win32_PnPEntity). This file is also compiled into the
// LoginRelay.UsbProbe tool (it must not use WinForms).
using System;
using System.Collections.Generic;
using System.Management;
using LoginRelay.Core;

namespace LoginRelay.App
{
    internal sealed class WmiUsbDiagnostics : IUsbDiagnostics
    {
        public IReadOnlyList<PnpDevice> QueryUsbDevices()
        {
            var result = new List<PnpDevice>();
            // "SELECT *" because a property missing on some Windows versions would make a column list fail;
            // LIKE 'USB%' keeps it fast (everything else is filtered below).
            using (var searcher = new ManagementObjectSearcher("SELECT * FROM Win32_PnPEntity WHERE DeviceID LIKE 'USB%'"))
            {
                searcher.Options.Timeout = TimeSpan.FromSeconds(10);
                using (ManagementObjectCollection items = searcher.Get())
                {
                    foreach (ManagementBaseObject item in items)
                    {
                        using (item)
                        {
                            string deviceId = AsString(Read(item, "DeviceID")) ?? "";
                            if (!deviceId.StartsWith(@"USB\VID_", StringComparison.OrdinalIgnoreCase))
                            {
                                continue;
                            }
                            result.Add(new PnpDevice(
                                AsString(Read(item, "Name")) ?? "",
                                deviceId,
                                AsStrings(Read(item, "HardwareID")),
                                AsStrings(Read(item, "CompatibleID")),
                                AsString(Read(item, "Service")),
                                AsInt(Read(item, "ConfigManagerErrorCode")),
                                AsBool(Read(item, "Present"))));
                        }
                    }
                }
            }
            return result;
        }

        /// <summary>A property's value, or null if the property doesn't exist or can't be read.</summary>
        private static object? Read(ManagementBaseObject item, string name)
        {
            try
            {
                return item[name];
            }
            catch (Exception)
            {
                return null;
            }
        }

        private static string? AsString(object? value)
        {
            return value as string;
        }

        private static string[] AsStrings(object? value)
        {
            string[]? list = value as string[];
            return list ?? new string[0];
        }

        private static int AsInt(object? value)
        {
            try
            {
                return value == null ? 0 : Convert.ToInt32(value);
            }
            catch (Exception)
            {
                return 0;
            }
        }

        private static bool? AsBool(object? value)
        {
            return value is bool ? (bool?)(bool)value : null;
        }
    }
}
