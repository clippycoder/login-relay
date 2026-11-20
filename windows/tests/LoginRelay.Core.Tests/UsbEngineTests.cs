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
    /// The engine against a fake USB bus (and a test "UI thread"). None of these tests needs libusb or a phone.
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

        private sealed class UsbRig : IDisposable
        {
            public readonly TestDispatcher Ui = new TestDispatcher();
            public readonly FakePlatform Platform = new FakePlatform();
            public readonly FakeUsbBackend Backend = new FakeUsbBackend();
            public readonly RelayEngine Engine;
            public readonly Prefs Prefs;
            public readonly string PrefsPath;
            public int BackendsCreated;
            private readonly string _dir;

            public UsbRig(Action<RelayEngine>? tweak = null, bool start = true)
            {
                _dir = Path.Combine(Path.GetTempPath(), "loginrelay-usbtest-" + Guid.NewGuid().ToString("N"));
                PrefsPath = Path.Combine(_dir, "prefs.json");
                Prefs = new Prefs(PrefsPath);
                Engine = new RelayEngine(Ui, Platform, Prefs);
                Engine.UsbBackendFactory = () =>
                {
                    Interlocked.Increment(ref BackendsCreated);
                    return Backend;
                };
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
                Assert.Equal(NotAnsweringYet, detail);
                Assert.True(rig.Logged("USB connection open."));

                // The phone app starts answering and says hello.
                conn.RespondToPings = true;
                conn.PhoneHello("0.5.0");
                Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.Ready));
                Assert.True(TestUtil.WaitFor(() => rig.Logged("Login Relay 0.5.0 on the phone says hello (usb).")));
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
                Assert.Equal("Plug the phone in and choose “File transfer” in its USB notification. USB debugging isn't needed.", detail);
                Assert.False(rig.Ui.Invoke(() => rig.Engine.UsbCantOpen));
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
                rig.Backend.SwitchCode = UsbCode.Request;
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbDetail) == "Couldn't switch Fake Phone to accessory mode (error -4)."));
                Assert.Equal(PhaseKind.Connecting, rig.Kind());
                Assert.Equal(0, rig.Backend.OpenCount);
            }
        }

        [Fact]
        public void ABusyPhoneStopsTheAdbServerOnceAndTriesAgain()
        {
            int stops = 0;
            using (var rig = new UsbRig(tweak: e => e.StopAdbServer = () =>
            {
                Interlocked.Increment(ref stops);
                return true;
            }))
            {
                rig.Backend.SwitchCode = UsbCode.Busy;
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                Assert.True(TestUtil.WaitFor(() => rig.Backend.Switches.Length == 2), "the switch was retried after stopping adb");
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbDetail) == "Another program has Fake Phone open (usually adb)."));
                Assert.Equal(1, Volatile.Read(ref stops));
                Assert.True(TestUtil.WaitFor(() => rig.LoggedContaining("Stopped it")));
            }
        }

        [Fact]
        public void ABusyPhoneWithNoAdbToStopIsSilentAboutIt()
        {
            using (var rig = new UsbRig(tweak: e => e.StopAdbServer = () => false))
            {
                rig.Backend.SwitchCode = UsbCode.Busy;
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbDetail) == "Another program has Fake Phone open (usually adb)."));
                Assert.Single(rig.Backend.Switches);
                Assert.False(rig.LoggedContaining("adb server"));
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

        // ---- a phone libusb can't open ---------------------------------------------------------------

        private static void AssertPlainCantOpen(UsbRig rig)
        {
            (string status, string detail) = rig.Ui.Invoke(() => rig.Engine.Describe());
            Assert.Equal("Can't open the phone", status);
            Assert.Equal("Login Relay needs a WinUSB driver for the phone. See “Windows drivers” in the README (github.com/clippycoder/login-relay).", detail);
            Assert.DoesNotContain("Zadig", status + detail);
            Assert.DoesNotContain("Step", status + detail);
            Assert.Equal(PhaseKind.CantOpen, rig.Kind());
            Assert.True(rig.Ui.Invoke(() => rig.Engine.UsbCantOpen));
        }

        [Fact]
        public void APhoneLibusbCannotOpenIsReportedPlainly()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(new UsbDevice(0x04E8, 0x6860, "1-1", false, true, "Android phone (04e8:6860)", "", false));
                Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.CantOpen));
                AssertPlainCantOpen(rig);
                Assert.Empty(rig.Backend.Switches);
                Assert.Equal(0, rig.Backend.OpenCount);
            }
        }

        [Fact]
        public void ADownloadModeDeviceIsNotReportedAsAPhoneThatCannotBeOpened()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(new UsbDevice(0x05C6, 0x9008, "1-3", false, true, "QDLoader", "", false));
                Thread.Sleep(500);
                Assert.Equal(PhaseKind.NoPhone, rig.Kind());
                Assert.Equal("No phone found over USB", rig.Ui.Invoke(() => rig.Engine.Describe()).Status);
            }
        }

        [Fact]
        public void AnAccessoryLibusbCannotOpenIsReportedTheSameWay()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.AccessoryOpenable = false;
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                Assert.True(TestUtil.WaitFor(() => rig.Backend.Switches.Length == 1));
                Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.CantOpen));
                AssertPlainCantOpen(rig);
                Assert.Equal(0, rig.Backend.OpenCount);
                Thread.Sleep(300);
                Assert.Single(rig.Backend.Switches); // no endless re-switching
            }
        }

        [Fact]
        public void AnAccessoryAlreadyOnTheBusThatCannotBeOpenedIsReportedToo()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(new UsbDevice(Aoa.VendorGoogle, Aoa.PidFirst + 1, "2-3", true, true, "Fake Phone", "S", false));
                Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.CantOpen));
                AssertPlainCantOpen(rig);
                Assert.Empty(rig.Backend.Switches);
            }
        }

        [Theory]
        [InlineData(UsbCode.Unsupported)]
        [InlineData(UsbCode.Permission)]
        [InlineData(UsbCode.NoDriver)]
        public void AFailedSwitchThatMeansNoAccessIsReportedPlainly(int code)
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.SwitchCode = code;
                rig.Backend.Add(FakeUsbBackend.Phone("1-1"));
                Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.CantOpen));
                AssertPlainCantOpen(rig);
                Assert.Equal(0, rig.Backend.OpenCount);
            }
        }

        [Theory]
        [InlineData(UsbCode.Permission)]
        [InlineData(UsbCode.NoDriver)]
        public void AnAccessoryThatFailsToOpenForWantOfAccessIsReportedPlainly(int code)
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.OpenFailCode = code;
                rig.Backend.Add(new UsbDevice(Aoa.VendorGoogle, Aoa.PidFirst, "1-1", true, true, "Fake Phone", "S"));
                Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.CantOpen));
                AssertPlainCantOpen(rig);
                Assert.Equal(0, rig.Backend.OpenCount);
            }
        }

        [Fact]
        public void AnOtherwiseFailedOpenKeepsItsErrorCode()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.OpenFailCode = UsbCode.Io;
                rig.Backend.Add(new UsbDevice(Aoa.VendorGoogle, Aoa.PidFirst, "1-1", true, true, "Fake Phone", "S"));
                Assert.True(TestUtil.WaitFor(() => rig.Ui.Invoke(() => rig.Engine.UsbDetail) == "Couldn't open the accessory connection (error " + UsbCode.Io + ": fake open failure)."));
                Assert.Equal(PhaseKind.Connecting, rig.Kind());
                Assert.False(rig.Ui.Invoke(() => rig.Engine.UsbCantOpen));
            }
        }

        [Fact]
        public void TheStatusGoesAwayOnceThePhoneCanBeOpened()
        {
            using (var rig = new UsbRig())
            {
                rig.Backend.Add(new UsbDevice(0x04E8, 0x6860, "1-1", false, true, "Android phone (04e8:6860)", "", false));
                Assert.True(TestUtil.WaitFor(() => rig.Kind() == PhaseKind.CantOpen));

                // A driver was installed: the phone now shows up as an accessory that can be opened.
                rig.Backend.Remove("1-1");
                rig.Backend.Add(new UsbDevice(Aoa.VendorGoogle, Aoa.PidFirst, "1-1", true, true, "Fake Phone", "S"));
                rig.Ready();
                Assert.False(rig.Ui.Invoke(() => rig.Engine.UsbCantOpen));
                Assert.Equal("Fake Phone is ready", rig.Ui.Invoke(() => rig.Engine.Describe()).Status);
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
        public void TheUsbChoiceIsRememberedAndOldPrefsAreStillReadable()
        {
            string dir = Path.Combine(Path.GetTempPath(), "loginrelay-usbprefs-" + Guid.NewGuid().ToString("N"));
            string path = Path.Combine(dir, "prefs.json");
            try
            {
                Directory.CreateDirectory(dir);
                // A prefs file from before 0.5.0 has adb settings in it; they are ignored.
                File.WriteAllText(path, "{\"AdbPath\":\"C:\\\\adb.exe\",\"SelectedSerial\":\"ABC\",\"Transport\":\"adb\",\"Notifications\":true}");
                var prefs = new Prefs(path);
                Assert.True(prefs.Notifications);
                Assert.Null(prefs.UsbSelected);
                prefs.UsbSelected = "1-4.2";
                var again = new Prefs(path);
                Assert.Equal("1-4.2", again.UsbSelected);
                Assert.True(again.Notifications);
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
