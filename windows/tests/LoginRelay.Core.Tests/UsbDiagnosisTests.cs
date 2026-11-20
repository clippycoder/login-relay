// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Generic;
using System.Linq;
using LoginRelay.Core;
using Xunit;

namespace LoginRelay.Core.Tests
{
    /// <summary>The driver-diagnosis decision table: what libusb sees plus what Windows sees gives one setup state.</summary>
    public class UsbDiagnosisTests
    {
        private static readonly string[] Mtp = { @"USB\Class_06&SubClass_01&Prot_01", @"USB\Class_06&SubClass_01", @"USB\Class_06" };
        private static readonly string[] Adb = { @"USB\Class_FF&SubClass_42&Prot_01", @"USB\Class_FF&SubClass_42", @"USB\Class_FF" };
        private static readonly string[] VendorMtp = { @"USB\Class_FF&SubClass_FF&Prot_00", @"USB\Class_FF&SubClass_FF", @"USB\Class_FF" };
        private static readonly string[] VendorOther = { @"USB\Class_FF&SubClass_FF&Prot_FF", @"USB\Class_FF&SubClass_FF", @"USB\Class_FF" };
        private static readonly string[] Composite = { @"USB\COMPOSITE" };
        private static readonly string[] Hub = { @"USB\Class_09&SubClass_00&Prot_01", @"USB\Class_09&SubClass_00", @"USB\Class_09" };

        private static PnpDevice Pnp(string name, string deviceId, string[] compatible, string? service = null, int error = 0, bool? present = null)
        {
            return new PnpDevice(name, deviceId, new string[0], compatible, service, error, present);
        }

        private static UsbDevice Lib(ushort vid, ushort pid, string location, string name, bool openable = true, bool looksAndroid = true)
        {
            return new UsbDevice(vid, pid, location, false, looksAndroid, name, "", openable);
        }

        private static UsbDevice LibAccessory(ushort pid, string location, bool openable = true)
        {
            return new UsbDevice(Aoa.VendorGoogle, pid, location, true, true, "Phone", "", openable);
        }

        private static UsbSetupInfo Diagnose(UsbDevice[] libusb, PnpDevice[] windows)
        {
            return UsbDiagnosisLogic.Diagnose(libusb, windows);
        }

        // ---- the five states -------------------------------------------------------------------------

        [Fact]
        public void AnAccessoryVisibleToLibusbIsReadyToConnect()
        {
            UsbSetupInfo info = Diagnose(new[] { LibAccessory(0x2D00, "1-1") }, new PnpDevice[0]);
            Assert.Equal(UsbSetupState.ReadyToConnect, info.State);
            Assert.Equal((ushort)0x18D1, info.VendorId);
            Assert.Equal((ushort)0x2D00, info.ProductId);
        }

        [Theory]
        [InlineData(0x2D00)]
        [InlineData(0x2D01)]
        [InlineData(0x2D02)]
        [InlineData(0x2D03)]
        [InlineData(0x2D04)]
        [InlineData(0x2D05)]
        public void EveryAccessoryProductIdCounts(int pid)
        {
            Assert.Equal(UsbSetupState.ReadyToConnect, Diagnose(new[] { LibAccessory((ushort)pid, "1-1") }, new PnpDevice[0]).State);
        }

        [Fact]
        public void ACandidatePhoneVisibleToLibusbCanBeSwitched()
        {
            UsbSetupInfo info = Diagnose(new[] { Lib(0x04E8, 0x6860, "1-1", "Galaxy") }, new PnpDevice[0]);
            Assert.Equal(UsbSetupState.CanSwitch, info.State);
            Assert.Equal("Galaxy", info.DeviceName);
            Assert.Equal("04E8 6860", info.UsbId);
        }

        [Fact]
        public void AnAccessoryWindowsHasButLibusbCannotSeeNeedsStepTwo()
        {
            var windows = new[] { Pnp("Android Accessory Interface", @"USB\VID_18D1&PID_2D00\0123", new[] { @"USB\Class_FF&SubClass_FF" }, null, 28) };
            UsbSetupInfo info = Diagnose(new UsbDevice[0], windows);
            Assert.Equal(UsbSetupState.NeedsAccessoryDriver, info.State);
            Assert.Equal("18D1 2D00", info.UsbId);
            Assert.Contains("no driver", info.Reason);
        }

        [Fact]
        public void AnAccessoryListedButNotOpenableNeedsStepTwoEvenWithoutWindowsInfo()
        {
            UsbSetupInfo info = Diagnose(new[] { LibAccessory(0x2D01, "1-1", false) }, new PnpDevice[0]);
            Assert.Equal(UsbSetupState.NeedsAccessoryDriver, info.State);
            Assert.Equal("18D1 2D01", info.UsbId);
        }

        [Fact]
        public void AnAccessoryWithADifferentDriverExplainsWhat()
        {
            var windows = new[] { Pnp("Android Accessory Interface", @"USB\VID_18D1&PID_2D01\0123", new string[0], "WinUSBish") };
            UsbSetupInfo info = Diagnose(new UsbDevice[0], windows);
            Assert.Equal(UsbSetupState.NeedsAccessoryDriver, info.State);
            Assert.Contains("WinUSBish", info.Reason);
        }

        [Fact]
        public void AnAccessoryWindowsSaysIsWinUsbButLibusbCannotOpenTellsTheUserToReplug()
        {
            var windows = new[] { Pnp("Android Accessory Interface", @"USB\VID_18D1&PID_2D00\0123", new string[0], "WinUSB") };
            UsbSetupInfo info = Diagnose(new UsbDevice[0], windows);
            Assert.Equal(UsbSetupState.NeedsAccessoryDriver, info.State);
            Assert.Contains("replug", info.Reason);
        }

        [Fact]
        public void APhoneWindowsHasButLibusbCannotSeeNeedsStepOne()
        {
            var windows = new[] { Pnp("Pixel 7", @"USB\VID_18D1&PID_4EE1\ABC", Mtp, "WUDFWpdMtp") };
            UsbSetupInfo info = Diagnose(new UsbDevice[0], windows);
            Assert.Equal(UsbSetupState.NeedsPhoneDriver, info.State);
            Assert.Equal("Pixel 7", info.DeviceName);
            Assert.Equal("18D1 4EE1", info.UsbId);
        }

        [Fact]
        public void APhoneLibusbListsButCannotOpenNeedsStepOne()
        {
            UsbSetupInfo info = Diagnose(new[] { Lib(0x04E8, 0x6860, "1-1", "Android phone (04e8:6860)", false) }, new PnpDevice[0]);
            Assert.Equal(UsbSetupState.NeedsPhoneDriver, info.State);
            Assert.Equal("Android phone (04e8:6860)", info.DeviceName);
        }

        [Fact]
        public void TheWindowsNameIsPreferredForAPhoneLibusbCannotOpen()
        {
            var windows = new[] { Pnp("SAMSUNG Mobile MTP Device", @"USB\VID_04E8&PID_6860\RF8", Mtp, "WUDFWpdMtp") };
            UsbSetupInfo info = Diagnose(new[] { Lib(0x04E8, 0x6860, "1-1", "Android phone (04e8:6860)", false) }, windows);
            Assert.Equal("SAMSUNG Mobile MTP Device", info.DeviceName);
        }

        [Fact]
        public void NothingAtAllIsNoPhone()
        {
            UsbSetupInfo info = Diagnose(new UsbDevice[0], new PnpDevice[0]);
            Assert.Equal(UsbSetupState.NoPhone, info.State);
            Assert.False(info.HasId);
        }

        // ---- precedence ------------------------------------------------------------------------------

        [Fact]
        public void AnOpenableAccessoryBeatsEverythingElse()
        {
            var libusb = new[] { Lib(0x04E8, 0x6860, "1-2", "Other phone"), LibAccessory(0x2D00, "1-1") };
            var windows = new[] { Pnp("Pixel 7", @"USB\VID_18D1&PID_4EE1\ABC", Mtp) };
            Assert.Equal(UsbSetupState.ReadyToConnect, Diagnose(libusb, windows).State);
        }

        [Fact]
        public void AnOpenableCandidateBeatsAPhoneThatStillNeedsADriver()
        {
            var libusb = new[] { Lib(0x04E8, 0x6860, "1-1", "Phone A"), Lib(0x18D1, 0x4EE1, "1-2", "Phone B", false) };
            var windows = new[] { Pnp("Pixel 7", @"USB\VID_18D1&PID_4EE1\ABC", Mtp) };
            UsbSetupInfo info = Diagnose(libusb, windows);
            Assert.Equal(UsbSetupState.CanSwitch, info.State);
            Assert.Equal("Phone A", info.DeviceName);
        }

        [Fact]
        public void TheAccessoryStepComesBeforeThePhoneStep()
        {
            var windows = new[]
            {
                Pnp("Pixel 7", @"USB\VID_18D1&PID_4EE1\ABC", Mtp),
                Pnp("Android Accessory Interface", @"USB\VID_18D1&PID_2D00\ABC", new string[0], null, 28),
            };
            Assert.Equal(UsbSetupState.NeedsAccessoryDriver, Diagnose(new UsbDevice[0], windows).State);
        }

        [Fact]
        public void SeveralPhonesNeedingDriversReportTheFirstOne()
        {
            var windows = new[]
            {
                Pnp("Phone One", @"USB\VID_04E8&PID_6860\A", Mtp),
                Pnp("Phone Two", @"USB\VID_2717&PID_FF40\B", Mtp),
            };
            UsbSetupInfo info = Diagnose(new UsbDevice[0], windows);
            Assert.Equal(UsbSetupState.NeedsPhoneDriver, info.State);
            Assert.Equal("Phone One", info.DeviceName);
        }

        [Fact]
        public void OnePhoneWithWinUsbIsEnoughEvenIfAnotherNeedsADriver()
        {
            var libusb = new[] { Lib(0x04E8, 0x6860, "1-1", "Phone One") };
            var windows = new[]
            {
                Pnp("Phone One", @"USB\VID_04E8&PID_6860\A", Mtp, "WinUSB"),
                Pnp("Phone Two", @"USB\VID_2717&PID_FF40\B", Mtp, "WUDFWpdMtp"),
            };
            Assert.Equal(UsbSetupState.CanSwitch, Diagnose(libusb, windows).State);
        }

        // ---- what is, and is not, a phone in Windows' eyes -------------------------------------------

        [Fact]
        public void AdbAndVendorMtpInterfacesCountAsPhones()
        {
            Assert.Equal(UsbSetupState.NeedsPhoneDriver, Diagnose(new UsbDevice[0], new[] { Pnp("Android ADB Interface", @"USB\VID_1234&PID_0001\A", Adb) }).State);
            Assert.Equal(UsbSetupState.NeedsPhoneDriver, Diagnose(new UsbDevice[0], new[] { Pnp("Vendor MTP", @"USB\VID_1234&PID_0002\A", VendorMtp) }).State);
        }

        [Fact]
        public void ACompositeWhoseChildMatchesCountsAsAPhone()
        {
            var windows = new[]
            {
                Pnp("USB Composite Device", @"USB\VID_2717&PID_FF48\XYZ", Composite, "usbccgp"),
                Pnp("Mi MTP", @"USB\VID_2717&PID_FF48&MI_00\7&2&0&0000", Mtp, "WUDFWpdMtp"),
                Pnp("ADB Interface", @"USB\VID_2717&PID_FF48&MI_01\7&2&0&0001", Adb, "WinUSB"),
            };
            UsbSetupInfo info = Diagnose(new UsbDevice[0], windows);
            Assert.Equal(UsbSetupState.NeedsPhoneDriver, info.State);
            Assert.Equal("Mi MTP", info.DeviceName);
            Assert.Equal("2717 FF48", info.UsbId);
        }

        [Fact]
        public void ACompositeWithNoMatchingChildIsNotAPhone()
        {
            var windows = new[]
            {
                Pnp("USB Composite Device", @"USB\VID_046D&PID_0825\XYZ", Composite, "usbccgp"),
                Pnp("USB Video Device", @"USB\VID_046D&PID_0825&MI_00\7&2&0&0000", new[] { @"USB\Class_0E&SubClass_01&Prot_00" }, "usbvideo"),
                Pnp("USB Audio Device", @"USB\VID_046D&PID_0825&MI_02\7&2&0&0002", new[] { @"USB\Class_01&SubClass_01&Prot_00" }, "usbaudio"),
            };
            Assert.Equal(UsbSetupState.NoPhone, Diagnose(new UsbDevice[0], windows).State);
        }

        [Fact]
        public void HubsAreNotPhones()
        {
            var windows = new[]
            {
                Pnp("Generic USB Hub", @"USB\VID_8087&PID_0024\5&1&0&1", Hub, "USBHUB3"),
                Pnp("USB Root Hub (USB 3.0)", @"USB\ROOT_HUB30\4&1&0&0", Hub, "USBHUB3"),
            };
            var libusb = new[] { new UsbDevice(0x8087, 0x0024, "1-1", false, false, "Hub", "") };
            Assert.Equal(UsbSetupState.NoPhone, Diagnose(libusb, windows).State);
        }

        [Fact]
        public void QualcommDownloadModeIsIgnoredEverywhere()
        {
            var windows = new[] { Pnp("Qualcomm HS-USB QDLoader 9008", @"USB\VID_05C6&PID_9008\6&2&0&1", VendorMtp, "QcUsbNdis") };
            var libusb = new[] { new UsbDevice(0x05C6, 0x9008, "1-3", false, true, "QDLoader", "", false) };
            Assert.Equal(UsbSetupState.NoPhone, Diagnose(libusb, windows).State);
            Assert.Equal(UsbSetupState.NoPhone, Diagnose(new UsbDevice[0], windows).State);
        }

        [Fact]
        public void AnUnrelatedVendorSpecificDeviceIsNotAPhone()
        {
            var windows = new[] { Pnp("Some Dongle", @"USB\VID_1234&PID_5678\A", VendorOther, "WinUSB") };
            Assert.Equal(UsbSetupState.NoPhone, Diagnose(new UsbDevice[0], windows).State);
            // And one libusb knows isn't a phone is not either, openable or not.
            var libusb = new[] { Lib(0x1234, 0x5678, "1-1", "Some Dongle", false, false), Lib(0x1234, 0x5679, "1-2", "Other", true, false) };
            Assert.Equal(UsbSetupState.NoPhone, Diagnose(libusb, windows).State);
        }

        [Fact]
        public void TheShortFormsOfVendorClassIdsAreNotEnough()
        {
            var windows = new[] { Pnp("Thing", @"USB\VID_1234&PID_5678\A", new[] { @"USB\Class_FF&SubClass_FF", @"USB\Class_FF" }) };
            Assert.Equal(UsbSetupState.NoPhone, Diagnose(new UsbDevice[0], windows).State);
        }

        [Fact]
        public void DevicesWindowsSaysAreNotPresentAreIgnored()
        {
            var windows = new[] { Pnp("Old Phone", @"USB\VID_04E8&PID_6860\A", Mtp, null, 0, false) };
            Assert.Equal(UsbSetupState.NoPhone, Diagnose(new UsbDevice[0], windows).State);
            var acc = new[] { Pnp("Android Accessory Interface", @"USB\VID_18D1&PID_2D00\A", new string[0], null, 0, false) };
            Assert.Equal(UsbSetupState.NoPhone, Diagnose(new UsbDevice[0], acc).State);
        }

        [Fact]
        public void EntriesThatAreNotUsbDeviceIdsAreIgnored()
        {
            var windows = new[]
            {
                Pnp("Mass storage", @"USBSTOR\DISK&VEN_SAMSUNG&PROD_ANDROID", Mtp),
                Pnp("Garbage", "", Mtp),
            };
            Assert.Equal(UsbSetupState.NoPhone, Diagnose(new UsbDevice[0], windows).State);
        }

        [Fact]
        public void DeviceIdParsingHandlesCaseAndInterfaces()
        {
            ushort v, p;
            int mi;
            Assert.True(UsbDiagnosisLogic.TryParseDeviceId(@"USB\VID_18d1&PID_2d01&MI_01\6&abc&0&0001", out v, out p, out mi));
            Assert.Equal((ushort)0x18D1, v);
            Assert.Equal((ushort)0x2D01, p);
            Assert.Equal(1, mi);
            Assert.True(UsbDiagnosisLogic.TryParseDeviceId(@"USB\VID_04E8&PID_6860\RF8M123", out v, out p, out mi));
            Assert.Equal(-1, mi);
            Assert.False(UsbDiagnosisLogic.TryParseDeviceId(@"PCI\VEN_8086&DEV_1234", out v, out p, out mi));
        }

        // ---- the words -------------------------------------------------------------------------------

        [Fact]
        public void StepOneTextHasTheDeviceNameAndUsbId()
        {
            var info = new UsbSetupInfo(UsbSetupState.NeedsPhoneDriver, 0x18D1, 0x4EE1, "Pixel 7");
            string steps = UsbSetupText.Steps(info);
            Assert.Contains("Download and start Zadig", steps);
            Assert.Contains("Options > List All Devices", steps);
            Assert.Contains("Pixel 7 (USB ID 18D1 4EE1)", steps);
            Assert.Contains("WinUSB", steps);
            Assert.Contains("Replace Driver (or Install Driver)", steps);
            Assert.Contains("Attempt to remove the driver for this device", steps);
            Assert.Contains("18D1 4EE1", UsbSetupText.Summary(info));
            Assert.StartsWith("Step 1 of 2", UsbSetupText.Title(info));
        }

        [Fact]
        public void StepTwoTextHasTheUsbId()
        {
            var info = new UsbSetupInfo(UsbSetupState.NeedsAccessoryDriver, 0x18D1, 0x2D01, UsbDiagnosisLogic.AccessoryName);
            string steps = UsbSetupText.Steps(info);
            Assert.Contains("Android Accessory Interface", steps);
            Assert.Contains("USB ID 18D1 2D01", steps);
            Assert.Contains("Replace/Install Driver", steps);
            Assert.Contains("You only need to do each step once.", steps);
            Assert.StartsWith("Step 2 of 2", UsbSetupText.Title(info));

            var unknown = new UsbSetupInfo(UsbSetupState.NeedsAccessoryDriver, 0, 0, UsbDiagnosisLogic.AccessoryName);
            Assert.Contains("18D1 2D00 or 2D01", UsbSetupText.Steps(unknown));
        }

        [Fact]
        public void NoPhoneTextIsTheUsersHint()
        {
            var info = new UsbSetupInfo(UsbSetupState.NoPhone, 0, 0, "");
            Assert.Equal("Plug the phone in and choose “File transfer” in its USB notification.", UsbSetupText.Summary(info));
            Assert.Null(UsbSetupText.Headline(info));
            Assert.False(info.IsDriverProblem);
        }

        [Fact]
        public void EveryStateHasTitleSummaryAndSteps()
        {
            foreach (UsbSetupState state in Enum.GetValues(typeof(UsbSetupState)))
            {
                var info = new UsbSetupInfo(state, 0x18D1, 0x2D00, "Phone");
                Assert.False(string.IsNullOrWhiteSpace(UsbSetupText.Title(info)), state.ToString());
                Assert.False(string.IsNullOrWhiteSpace(UsbSetupText.Summary(info)), state.ToString());
                Assert.False(string.IsNullOrWhiteSpace(UsbSetupText.Steps(info)), state.ToString());
            }
        }
    }
}
