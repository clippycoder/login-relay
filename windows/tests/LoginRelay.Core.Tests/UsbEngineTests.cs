// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.IO;
using System.Linq;
using System.Threading;
using LoginRelay.Core;
using Xunit;

namespace LoginRelay.Core.Tests
{
    /// <summary>
    /// The engine in USB accessory mode, against a fake USB bus (and a test "UI thread"). None of these tests
    /// needs libusb, WMI or a phone.
    /// </summary>
    public class UsbEngineTests
    {
        private const string Callback = "com.example.app://login.example.com/android/callback";
        private const string NotAnsweringYet = "Login Relay on the phone isn't answering yet. If Android asks to allow USB access, tap OK; otherwise just start a sign-in on the phone and Login Relay starts by itself.";

        private static string SignInUrl(string state)
        {
            return "https://login.example.com/authorize?client_id=x&response_type=code&state=" + state
                + "&code_challenge=abc&redirect_uri=com.example.app%3A%2F%2Flogin.example.com%2Fandroid%2Fcallback";
        }

        private static string OpenMessage(string state)
        {
            return "{\"v\":1,\"type\":\"open\",\"url\":" + System.Text.Json.JsonSerializer.Serialize(SignInUrl(state))
                + ",\"referrer\":\"android-app://com.example.app\",\"label\":\"Example App\"}";
        }

        private static PnpDevice Pnp(string name, string deviceId, string[] compatible, string? service = null, int error = 0, bool? present = null)
        {
            return new PnpDevice(name, deviceId, new string[0], compatible, service, error, present);
        }

        private sealed class UsbRig : IDisposable
        {
            public readonly TestDispatcher Ui = new TestDispatcher();
            public readonly FakePlatform Platform = new FakePlatform();
            public readonly FakeUsbBackend Backend = new FakeUsbBackend();
            public readonly FakeDiagnostics Diagnostics = new FakeDiagnostics();
            public readonly RelayEngine Engine;
            public readonly Prefs Prefs;
            public readonly string PrefsPath;
            public int BackendsCreated;
            private readonly string _dir;

            public UsbRig(TransportKind transport = TransportKind.Usb, IAdb? adb = null, Action<RelayEngine>? tweak = null, bool start = true)
            {
                _dir = Path.Combine(Path.GetTempPath(), "loginrelay-usbtest-" + Guid.NewGuid().ToString("N"));
                PrefsPath = Path.Combine(_dir, "prefs.json");
                Prefs = new Prefs(PrefsPath);
                Prefs.Transport = transport;
                Engine = new RelayEngine(Ui, Platform, Prefs);
                Engine.UsbBackendFactory = () =>
                {
                    Interlocked.Increment(ref BackendsCreated);
                    return Backend;
                };
                Engine.UsbDiagnostics = Diagnostics;
                Engine.AdbFactory = ignored => adb;
                Engine.BrowserDisabled = true;
                Engine.HostName = "TEST-PC";
                Engine.DoneDelay = TimeSpan.FromMilliseconds(50);
                Engine.PlainLinkDoneDelay = TimeSpan.FromMilliseconds(50);
                Engine.CheckInterval = TimeSpan.FromMilliseconds(300);
                Engine.RetryDelay = TimeSpan.FromMilliseconds(100);
                Engine.UsbPingInterval = TimeSpan.FromMilliseconds(100);
                Engine.UsbSilence = TimeSpan.FromMilliseconds(700);
                Engine.UsbAppearTimeout = TimeSpan.FromMilliseconds(1500);
                Engine.UsbOpenRetryDelay = TimeSpan.FromMilliseconds(50);
                Engine.UsbDiagnosticsInterval = TimeSpan.FromMilliseconds(100);
                if (tweak != null)
                {
                    tweak(Engine);
                }
                if (start)
                {
                    Ui.Invoke(() => Engine.Start());
                }
            }

            public PhaseKind Kind()
            {
                return Ui.Invoke(() => Engine.Phase.Kind);
            }

            public bool Active()
            {
                return Ui.Invoke(() => Engine.Active != null);
            }

            public bool Logged(string text)
            {
                return Ui.Invoke(() => Engine.LogLines.Any(l => l.Text == text));
            }

            public bool LoggedContaining(string part)
            {
                return Ui.Invoke(() => Engine.LogLines.Any(l => l.Text.Contains(part, StringComparison.Ordinal)));
            }

            /// <summary>Waits until the engine has opened the accessory pipe, and returns it.</summary>
            public FakeUsbConnection Connected()
            {
                Assert.True(TestUtil.WaitFor(() => Backend.Connection != null), "the accessory pipe was opened");
                return Backend.Connection!;
            }

            /// <summary>Connected and answering (the fake phone replies to pings).</summary>
            public FakeUsbConnection Ready()
            {
                FakeUsbConnection conn = Connected();
                Assert.True(TestUtil.WaitFor(() => Kind() == PhaseKind.Ready), "ready");
                return conn;
            }

            public void Dispose()
            {
                try
                {
                    Ui.Invoke(() => Engine.Shutdown());
                }
                finally
                {
                    Ui.Dispose();
                    try
                    {
                        Directory.Delete(_dir, true);
                    }
                    catch (Exception)
                    {
                    }
                }
                Assert.Null(Ui.LastError);
            }
        }

        // ---- finding and choosing a phone ------------------------------------------------------------

        [Fact]
        public void OneCandidateIsSwitchedOpenedAndBecomesReady()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.RespondToPings = false;
                rig.Backend.Add(FakeUsbBackend.Phone("1-4"));
                FakeUsbConnection conn = rig.Connected();

                // The switch: one request, with our name on it, and the phone came back as an accessory at the same place.
                Assert.Equal(new[] { "1-4" }, rig.Backend.Switches);
                Assert.Equal("TEST-PC", rig.Backend.HostNames[0]);
                Assert.Equal(1, rig.Backend.OpenCount);
                Assert.Contains(rig.Backend.ListDevices(), d => d.IsAccessory && d.Location == "1-4");

                // Pipe open, nobody answering yet.
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.Linked && rig.Engine.Phase.Kind == PhaseKind.Connecting)));
                (string status, string detail) = rig.Ui.Invoke(() => rig.Engine.Describe());
                Assert.Equal("Fake Phone is connected over USB", status);
                Assert.StartsWith(NotAnsweringYet, detail);
                Assert.Contains("USB driver setup", detail);
                Assert.True(rig.Logged("USB connection open."));

                // The phone app starts answering and says hello.
                conn.RespondToPings = true;
                conn.PhoneHello("0.5.0");
                Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.Ready));
                Assert.True(TestUtil.WaitFor(() => rig.Logged("Login Relay 0.5.0 on the phone says hello (usb).")));
                Assert.Equal("0.5.0", rig.Ui.Invoke(() => rig.Engine.PhoneVersion));
                Assert.True(rig.Logged("Login Relay on the phone is answering."));
                (string status2, string detail2) = rig.Ui.Invoke(() => rig.Engine.Describe());
                Assert.Equal("Fake Phone is ready", status2);
                Assert.Equal("Sign in to an app on the phone and the page opens here.", detail2);
                Assert.True(conn.Count("ping") >= 1);
                Assert.Single(rig.Backend.Switches);
            }
        }

        [Fact]
        public void SeveralCandidatesWaitForTheUserToChoose()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1", "Phone A"));
                rig.Backend.Add(FakeUsbBackend.Phone("1-2", "Phone B"));
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbCandidates.Count == 2)));
                Thread.Sleep(500);
                Assert.Empty(rig.Backend.Switches);
                Assert.Equal(PhaseKind.NoPhone, rig.Kind());
                Assert.Equal("Choose which phone to use from the menu.", rig.Ui.Invoke(() => rig.Engine.UsbDetail));

                rig.Ui.Invoke(() => rig.Engine.SelectUsb("1-2"));
                Assert.True(TestUtil.WaitFor(() => rig.Backend.Switches.Length == 1));
                Assert.Equal(new[] { "1-2" }, rig.Backend.Switches);
                rig.Connected();
                Assert.Equal("Phone B", rig.Ui.Invoke(() => rig.Engine.Phone?.Title));
                Assert.Equal("1-2", rig.Prefs.UsbSelected);
                Assert.Equal(new[] { "1-2" }, rig.Backend.Switches);
            }
        }

        [Fact]
        public void ARememberedChoiceIsUsedAtStartup()
        {
            using (var rig = new UsbRig(start: false))
            {
                rig.Prefs.UsbSelected = "1-1";
                rig.Backend.Add(FakeUsbBackend.Phone("1-1", "Phone A"));
                rig.Backend.Add(FakeUsbBackend.Phone("1-2", "Phone B"));
                rig.Ui.Invoke(() => rig.Engine.Start());
                rig.Connected();
                Assert.Equal(new[] { "1-1" }, rig.Backend.Switches);
            }
        }

        [Fact]
        public void AnAccessoryAlreadyOnTheBusIsOpenedWithoutASwitch()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(new UsbDevice(Aoa.VendorGoogle, Aoa.PidFirst + 1, "2-3", true, true, "Fake Phone", "S"));
                rig.Ready();
                Assert.Empty(rig.Backend.Switches);
                Assert.Equal(1, rig.Backend.OpenCount);
            }
        }

        [Fact]
        public void DevicesThatAreNotPhonesAreNeverSwitched()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(new UsbDevice(0x05C6, 0x9008, "1-3", false, false, "QDLoader", ""));
                rig.Backend.Add(new UsbDevice(0x8087, 0x0024, "1-1", false, false, "Hub", ""));
                rig.Backend.Add(new UsbDevice(0x1234, 0x5678, "1-5", false, false, "Vendor thing", ""));
                Thread.Sleep(700);
                Assert.Empty(rig.Backend.Switches);
                Assert.Equal(0, rig.Backend.OpenCount);
                Assert.Equal(PhaseKind.NoPhone, rig.Kind());
                (string status, string detail) = rig.Ui.Invoke(() => rig.Engine.Describe());
                Assert.Equal("No phone found over USB", status);
                Assert.StartsWith("Plug the phone in and choose “File transfer” in its USB notification. USB debugging isn't needed.", detail);
                Assert.Contains("USB driver setup", detail);
                Assert.Equal(UsbSetupState.NoPhone, rig.Ui.Invoke(() => rig.Engine.UsbSetup)!.State);
            }
        }

        [Fact]
        public void SwitchesAreRateLimitedPerLocation()
        {
            using (var rig = new UsbRig(tweak: e =>
            {
                e.UsbSwitchInterval = TimeSpan.FromMilliseconds(1500);
                e.UsbAppearTimeout = TimeSpan.FromMilliseconds(300);
            }))
            {
                rig.Backend.AccessoryAppears = false;
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                Assert.True(TestUtil.WaitFor(() => rig.Backend.Switches.Length == 1));
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbDetail) == "The phone didn't switch to accessory mode.", 1200));
                Thread.Sleep(500);
                Assert.Single(rig.Backend.Switches); // still inside the limit
                Assert.True(TestUtil.WaitFor(() => rig.Backend.Switches.Length == 2, 6000), "tried again after the interval");
            }
        }

        [Fact]
        public void AFailedSwitchSaysWhy()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.SwitchCode = UsbCode.Unsupported;
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbDetail) == "Fake Phone doesn't answer the USB accessory protocol."));
                Assert.Equal(PhaseKind.Connecting, rig.Kind());
                Assert.Equal(0, rig.Backend.OpenCount);
            }
        }

        // ---- liveness --------------------------------------------------------------------------------

        [Fact]
        public void AnIdlePhoneStaysReady()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                FakeUsbConnection conn = rig.Ready();
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbAlive)));

                // Android reclaims the idle phone app: it stops answering. That is normal, not a problem.
                conn.RespondToPings = false;
                Assert.True(TestUtil.WaitFor(() => !rig.Ui.Invoke(() => rig.Engine.UsbAlive)), "silence was noticed");
                Assert.True(TestUtil.WaitFor(() => rig.Logged("Login Relay on the phone went idle (it starts again by itself when an app asks to sign in).")));
                Assert.Equal(PhaseKind.Ready, rig.Kind());
                Assert.True(rig.Ui.Invoke(() => rig.Engine.Linked));
                Assert.Null(rig.Ui.Invoke(() => rig.Engine.PhoneVersion));
                Assert.False(conn.Closed);

                // The next sign-in starts it again.
                conn.RespondToPings = true;
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbAlive)));
                Assert.Equal(PhaseKind.Ready, rig.Kind());
            }
        }

        [Fact]
        public void LosingTheLinkMidLoginCancelsTheLogin()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                FakeUsbConnection conn = rig.Ready();
                conn.PhoneSend(OpenMessage("S9"));
                Assert.True(TestUtil.WaitFor(() => rig.Platform.Claims.Length == 1 && rig.Active()));
                FakeClaim claim = rig.Platform.Claims[0];
                Assert.Equal(PhaseKind.Working, rig.Kind());

                conn.RespondToPings = false;
                Assert.True(TestUtil.WaitFor(() => !rig.Active()), "the login was cancelled");
                Assert.True(rig.Logged("Login Relay on the phone stopped answering."));
                Assert.True(claim.Released);
                Assert.Equal(0, conn.Count("callback"));
            }
        }

        [Fact]
        public void UnpluggingEndsTheLinkAndTheLogin()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                FakeUsbConnection conn = rig.Ready();
                conn.PhoneSend(OpenMessage("S8"));
                Assert.True(TestUtil.WaitFor(() => rig.Platform.Claims.Length == 1 && rig.Active()));
                FakeClaim claim = rig.Platform.Claims[0];

                conn.Unplug();
                Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.NoPhone), "no phone any more");
                Assert.False(rig.Active());
                Assert.True(claim.Released);
                Assert.True(rig.Logged("Link to the phone closed."));
                Assert.False(rig.Ui.Invoke(() => rig.Engine.Linked));
                Assert.True(conn.Closed, "the pipe was released");
                Assert.True(TestUtil.WaitFor(() => rig.Backend.ClosedCount == 1));
            }
        }

        [Fact]
        public void APhoneThatComesBackIsSwitchedAgain()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                FakeUsbConnection first = rig.Ready();
                first.Unplug();
                Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.NoPhone));
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                Assert.True(TestUtil.WaitFor(() => rig.Backend.Connections.Length == 2), "reconnected");
                Assert.Equal(2, rig.Backend.Switches.Length); // forgotten when it left the bus, so no 20 s wait
            }
        }

        // ---- a login ---------------------------------------------------------------------------------

        [Fact]
        public void SignInIsDeliveredThroughACallbackMessage()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                FakeUsbConnection conn = rig.Ready();
                conn.PhoneSend(OpenMessage("S1"));
                Assert.True(TestUtil.WaitFor(() => rig.Platform.Claims.Length == 1 && rig.Active()), "claimed");
                Assert.Equal("com.example.app", rig.Platform.Claims[0].Scheme);
                Assert.True(TestUtil.WaitFor(() => conn.GotText("Finish signing in on your computer.")));
                Assert.True(rig.Logged("Example App wants to open a sign-in page at login.example.com."));

                // Wrong state: ignored.
                rig.Ui.Invoke(() => rig.Engine.ReceiveCallback(Callback + "?code=EVIL&state=other"));
                Thread.Sleep(300);
                Assert.Equal(0, conn.Count("callback"));

                string good = Callback + "?code=GOOD&state=S1";
                rig.Ui.Invoke(() => rig.Engine.ReceiveCallback(good));
                Assert.True(TestUtil.WaitFor(() => conn.Count("callback") == 1), "the callback was sent to the phone");
                Assert.Equal(good, conn.Message("callback")!["url"]);
                Assert.True(rig.Logged("Returning to Example App on the phone…"));
                Assert.False(rig.Active());
                Assert.True(rig.Platform.Claims[0].Released);

                // The phone checks it, hands it to the app and says so. The computer sends no "done": the phone closes itself.
                Assert.False(rig.LoggedContaining("Done: "));
                conn.PhoneSend("{\"type\":\"delivered\"}");
                Assert.True(TestUtil.WaitFor(() => rig.Logged("Done: Example App is finishing the sign-in.")));
                Thread.Sleep(300);
                Assert.Equal(0, conn.Count("done"));
            }
        }

        [Fact]
        public void APhoneErrorIsLoggedAndEndsTheWaitForDelivery()
        {
            using (var rig = new UsbRig(tweak: e => e.UsbDeliveryTimeout = TimeSpan.FromMilliseconds(500)))
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                FakeUsbConnection conn = rig.Ready();
                conn.PhoneSend(OpenMessage("S2"));
                Assert.True(TestUtil.WaitFor(() => rig.Active()));
                rig.Ui.Invoke(() => rig.Engine.ReceiveCallback(Callback + "?code=1&state=S2"));
                Assert.True(TestUtil.WaitFor(() => conn.Count("callback") == 1));
                conn.PhoneSend("{\"type\":\"error\",\"text\":\"The app didn't accept the result.\"}");
                Assert.True(TestUtil.WaitFor(() => rig.Logged("The phone says: The app didn't accept the result.")));
                Thread.Sleep(900);
                Assert.False(rig.Logged("The phone didn't confirm it handed the result to the app."), "the error cancelled the timer");
                Assert.False(rig.LoggedContaining("Done: "));
            }
        }

        [Fact]
        public void ASilentPhoneTriggersTheDeliveryTimeout()
        {
            using (var rig = new UsbRig(tweak: e => e.UsbDeliveryTimeout = TimeSpan.FromMilliseconds(400)))
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                FakeUsbConnection conn = rig.Ready();
                conn.PhoneSend(OpenMessage("S3"));
                Assert.True(TestUtil.WaitFor(() => rig.Active()));
                rig.Ui.Invoke(() => rig.Engine.ReceiveCallback(Callback + "?code=1&state=S3"));
                Assert.True(TestUtil.WaitFor(() => rig.Logged("The phone didn't confirm it handed the result to the app.")));
                // A late "delivered" after the timeout is ignored.
                conn.PhoneSend("{\"type\":\"delivered\"}");
                Thread.Sleep(300);
                Assert.False(rig.LoggedContaining("Done: "));
            }
        }

        [Fact]
        public void ACancelFromThePhoneEndsTheLoginAndSendsNothingBack()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                FakeUsbConnection conn = rig.Ready();
                conn.PhoneSend(OpenMessage("S4"));
                Assert.True(TestUtil.WaitFor(() => rig.Active()));
                conn.PhoneSend("{\"type\":\"cancel\"}");
                Assert.True(TestUtil.WaitFor(() => !rig.Active()));
                Assert.True(rig.Logged("Cancelled on the phone."));
                Assert.True(rig.Platform.Claims[0].Released);
                Thread.Sleep(200);
                Assert.Equal(0, conn.Count("done"));
            }
        }

        [Fact]
        public void CancellingOnTheComputerTellsThePhone()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                FakeUsbConnection conn = rig.Ready();
                conn.PhoneSend(OpenMessage("S5"));
                Assert.True(TestUtil.WaitFor(() => rig.Active()));
                rig.Ui.Invoke(() => rig.Engine.CancelActive("Cancelled on the computer.", true));
                Assert.True(TestUtil.WaitFor(() => conn.Count("done") == 1));
            }
        }

        [Fact]
        public void PlainLinksNeedNoCallback()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                FakeUsbConnection conn = rig.Ready();
                conn.PhoneSend("{\"v\":1,\"type\":\"open\",\"url\":\"https://example.com/terms\",\"referrer\":\"android-app://com.a.b\",\"label\":\"T\"}");
                Assert.True(TestUtil.WaitFor(() => rig.Platform.Opened.Contains("https://example.com/terms")));
                Assert.True(TestUtil.WaitFor(() => conn.GotText("Opened on your computer.")));
                Assert.True(TestUtil.WaitFor(() => conn.Count("done") == 1));
                Assert.Equal(0, conn.Count("callback"));
            }
        }

        // ---- switching connection type ---------------------------------------------------------------

        [Fact]
        public void TheConnectionTypeCanBeSwitchedWhileRunning()
        {
            using (var phone = new FakePhone(null))
            {
                var adb = new FakeAdb(phone.Port);
                using (var rig = new UsbRig(TransportKind.Adb, adb))
                {
                    rig.Backend.Add(FakeUsbBackend.Phone("1-1", "Usb Phone"));
                    Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.Ready), "ready over adb");
                    Assert.Equal(0, rig.BackendsCreated);
                    Assert.Equal("Fake Phone is ready", rig.Ui.Invoke(() => rig.Engine.Describe()).Status);

                    rig.Ui.Invoke(() => rig.Engine.SetTransport(TransportKind.Usb));
                    FakeUsbConnection conn = rig.Ready();
                    Assert.Equal(TransportKind.Usb, rig.Ui.Invoke(() => rig.Engine.Transport));
                    Assert.True(rig.Ui.Invoke(() => rig.Engine.Phone!.IsUsb));
                    Assert.Equal("Usb Phone is ready", rig.Ui.Invoke(() => rig.Engine.Describe()).Status);
                    Assert.Equal(TransportKind.Usb, new Prefs(rig.PrefsPath).Transport);
                    Assert.True(rig.Logged("Connecting over USB accessory mode (no USB debugging needed)."));

                    rig.Ui.Invoke(() => rig.Engine.SetTransport(TransportKind.Adb));
                    Assert.True(TestUtil.WaitFor(() => conn.Closed), "the USB pipe was released");
                    Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.Phase.Kind == PhaseKind.Ready && rig.Engine.Phone!.Serial == "FAKE0001")), "back on adb");
                    Assert.Equal(TransportKind.Adb, new Prefs(rig.PrefsPath).Transport);
                    Assert.True(rig.Logged("Connecting over adb."));
                    Assert.Equal(1, rig.Backend.Connections.Length);
                }
            }
        }

        [Fact]
        public void SwitchingToTheCurrentTypeDoesNothing()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                rig.Ready();
                rig.Ui.Invoke(() => rig.Engine.SetTransport(TransportKind.Usb));
                Assert.Equal(1, rig.Backend.Connections.Length);
                Assert.False(rig.LoggedContaining("Connecting over USB accessory mode"));
            }
        }

        [Fact]
        public void AdbModeNeverTouchesTheUsbBackendAndKeepsItsWording()
        {
            using (var phone = new FakePhone("https://example.com/terms", "android-app://com.a.b", "Terms App"))
            {
                var adb = new FakeAdb(phone.Port);
                using (var rig = new UsbRig(TransportKind.Adb, adb))
                {
                    Assert.True(TestUtil.WaitFor(() => rig.Platform.Opened.Contains("https://example.com/terms")));
                    Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.Ready));
                    Thread.Sleep(300);
                    Assert.Equal(0, rig.BackendsCreated);
                    Assert.Equal(0, rig.Backend.ListCalls);
                    (string status, string detail) = rig.Ui.Invoke(() => rig.Engine.Describe());
                    Assert.Equal("Fake Phone is ready", status);
                    Assert.Equal("Sign in to an app on the phone and the page opens here.", detail);
                    Assert.True(rig.Logged("Linked to Login Relay on the phone."));
                }
            }
        }

        [Fact]
        public void AdbModeWithNoAdbStillSaysSo()
        {
            using (var rig = new UsbRig(TransportKind.Adb, null))
            {
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.Phase.Kind == PhaseKind.NoAdb)));
                Assert.Equal("adb not found", rig.Ui.Invoke(() => rig.Engine.Describe()).Status);
            }
        }

        // ---- driver guidance through the engine ------------------------------------------------------

        [Fact]
        public void APhoneLibusbCannotOpenNeedsStepOne()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(new UsbDevice(0x04E8, 0x6860, "1-1", false, true, "Android phone (04e8:6860)", "", false));
                rig.Diagnostics.Set(Pnp("SAMSUNG Android", @"USB\VID_04E8&PID_6860\RF8M123", new[] { @"USB\Class_06&SubClass_01&Prot_01", @"USB\Class_06&SubClass_01" }, "WUDFWpdMtp"));
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbSetup?.State == UsbSetupState.NeedsPhoneDriver)));
                Assert.Empty(rig.Backend.Switches);
                (string status, string detail) = rig.Ui.Invoke(() => rig.Engine.Describe());
                Assert.Equal("SAMSUNG Android needs a USB driver", status);
                Assert.Contains("Options > List All Devices", detail);
                Assert.Contains("SAMSUNG Android (USB ID 04E8 6860)", detail);
                string brief = rig.Ui.Invoke(() => rig.Engine.Describe(true)).Detail;
                Assert.Contains("Step 1 of 2", brief);
                Assert.Contains("USB driver setup", brief);
                Assert.True(brief.Length < detail.Length);
            }
        }

        [Fact]
        public void APhoneOnlyWindowsCanSeeNeedsStepOneToo()
        {
            using (var rig = new UsbRig())
            {
                rig.Diagnostics.Set(
                    Pnp("USB Composite Device", @"USB\VID_18D1&PID_4EE2\ABC", new[] { @"USB\COMPOSITE" }, "usbccgp"),
                    Pnp("Pixel 7", @"USB\VID_18D1&PID_4EE2&MI_00\6&1&0&0000", new[] { @"USB\Class_06&SubClass_01&Prot_01" }, "WUDFWpdMtp"));
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbSetup?.State == UsbSetupState.NeedsPhoneDriver)));
                Assert.Equal("Pixel 7 needs a USB driver", rig.Ui.Invoke(() => rig.Engine.Describe()).Status);
                Assert.Equal(PhaseKind.NoPhone, rig.Kind());
            }
        }

        [Fact]
        public void AnAccessoryLibusbCannotOpenNeedsStepTwo()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.AccessoryOpenable = false;
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                Assert.True(TestUtil.WaitFor(() => rig.Backend.Switches.Length == 1));
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbSetup?.State == UsbSetupState.NeedsAccessoryDriver)));
                Assert.Equal(0, rig.Backend.OpenCount);
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.Describe()).Status == "The phone's accessory mode needs a USB driver"));
                (string status, string detail) = rig.Ui.Invoke(() => rig.Engine.Describe());
                Assert.Equal("The phone's accessory mode needs a USB driver", status);
                Assert.Contains("Android Accessory Interface", detail);
                Assert.Contains("18D1 2D00", detail);
                Assert.Single(rig.Backend.Switches); // no endless re-switching while the user installs the driver
            }
        }

        [Fact]
        public void TheDriverGuideFollowsTheUserThroughBothSteps()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.AccessoryOpenable = false;
                rig.Backend.Add(new UsbDevice(0x04E8, 0x6860, "1-1", false, true, "Android phone (04e8:6860)", "", false));
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbSetup?.State == UsbSetupState.NeedsPhoneDriver)));

                // Step 1 done: the phone is now visible to libusb.
                rig.Backend.Remove("1-1");
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbSetup?.State == UsbSetupState.NeedsAccessoryDriver)));

                // Step 2 done: the accessory can be opened (the switch limiter has run its course; swap in an accessory).
                rig.Backend.Remove("1-1");
                rig.Backend.Add(new UsbDevice(Aoa.VendorGoogle, Aoa.PidFirst, "1-1", true, true, "Fake Phone", "S", true));
                rig.Connected();
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbSetup?.State == UsbSetupState.ReadyToConnect)));
            }
        }

        [Fact]
        public void AFailingWindowsQueryIsReportedOnceAndDoesNotStopTheLoop()
        {
            using (var rig = new UsbRig())
            {
                rig.Diagnostics.Throw = true;
                Assert.True(TestUtil.WaitFor(() => rig.Diagnostics.Calls >= 3));
                Assert.Equal(1, rig.Ui.Invoke(() => rig.Engine.LogLines.Count(l => l.Text == "Couldn't ask Windows about USB devices: WMI is broken")));
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                rig.Connected(); // it still works
            }
        }

        [Fact]
        public void LibusbBeingMissingIsExplained()
        {
            using (var rig = new UsbRig(start: false))
            {
                rig.Engine.UsbBackendFactory = () => new MissingLibusbBackend();
                rig.Ui.Invoke(() => rig.Engine.Start());
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.Describe()).Detail == UsbText.NeedsLibusb));
                Assert.Equal(PhaseKind.NoPhone, rig.Kind());
            }
        }

        private sealed class MissingLibusbBackend : IUsbBackend
        {
            public System.Collections.Generic.IReadOnlyList<UsbDevice> ListDevices()
            {
                throw new UsbException(UsbCode.Unavailable, "");
            }

            public UsbSwitchResult SwitchToAccessory(string location, string hostName)
            {
                throw new UsbException(UsbCode.Unavailable, "");
            }

            public IUsbConnection OpenAccessory(string location)
            {
                throw new UsbException(UsbCode.Unavailable, "");
            }
        }

        // ---- prefs -----------------------------------------------------------------------------------

        [Fact]
        public void TransportPrefDefaultsToAdbAndPersists()
        {
            string dir = Path.Combine(Path.GetTempPath(), "loginrelay-transport-" + Guid.NewGuid().ToString("N"));
            string path = Path.Combine(dir, "prefs.json");
            try
            {
                var prefs = new Prefs(path);
                Assert.Equal(TransportKind.Adb, prefs.Transport);
                Assert.Null(prefs.UsbSelected);
                prefs.Transport = TransportKind.Usb;
                prefs.UsbSelected = "1-4.2";
                var again = new Prefs(path);
                Assert.Equal(TransportKind.Usb, again.Transport);
                Assert.Equal("1-4.2", again.UsbSelected);
                Assert.Contains("\"Transport\": \"usb\"", File.ReadAllText(path));
            }
            finally
            {
                try
                {
                    Directory.Delete(dir, true);
                }
                catch (Exception)
                {
                }
            }
        }
    }
}
