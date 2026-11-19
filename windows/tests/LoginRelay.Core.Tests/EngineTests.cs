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
    /// The engine against a fake adb, a fake platform and a fake phone listening on a real local socket.
    /// A test "UI thread" stands in for the WinForms one.
    /// </summary>
    public class EngineTests
    {
        private const string Callback = "com.example.app://login.example.com/android/callback";

        private static string SignInUrl(string state)
        {
            return "https://login.example.com/authorize?client_id=x&response_type=code&state=" + state
                + "&code_challenge=abc&redirect_uri=com.example.app%3A%2F%2Flogin.example.com%2Fandroid%2Fcallback";
        }

        private static bool WaitFor(Func<bool> condition, int milliseconds = 15000)
        {
            DateTime end = DateTime.UtcNow.AddMilliseconds(milliseconds);
            while (DateTime.UtcNow < end)
            {
                if (condition())
                {
                    return true;
                }
                Thread.Sleep(25);
            }
            return condition();
        }

        private sealed class Rig : IDisposable
        {
            public readonly TestDispatcher Ui = new TestDispatcher();
            public readonly FakePlatform Platform = new FakePlatform();
            public readonly FakePhone Phone;
            public readonly FakeAdb Adb;
            public readonly RelayEngine Engine;
            private readonly string _dir;

            public Rig(FakePhone phone, bool allowClaims = true)
            {
                Phone = phone;
                Platform.AllowClaims = allowClaims;
                Adb = new FakeAdb(phone.Port);
                _dir = Path.Combine(Path.GetTempPath(), "loginrelay-test-" + Guid.NewGuid().ToString("N"));
                var prefs = new Prefs(Path.Combine(_dir, "prefs.json"));
                Engine = new RelayEngine(Ui, Platform, prefs);
                Engine.AdbFactory = ignored => Adb;
                Engine.BrowserDisabled = true;
                Engine.DoneDelay = TimeSpan.FromMilliseconds(50);
                Engine.PlainLinkDoneDelay = TimeSpan.FromMilliseconds(50);
                Engine.CheckInterval = TimeSpan.FromSeconds(1);
                Engine.RetryDelay = TimeSpan.FromMilliseconds(100);
                Ui.Invoke(() => Engine.Start());
            }

            public bool Active()
            {
                return Ui.Invoke(() => Engine.Active != null);
            }

            public void Dispose()
            {
                try
                {
                    Ui.Invoke(() => Engine.Shutdown());
                }
                finally
                {
                    Phone.Dispose();
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

        [Fact]
        public void PlainLinkIsOpenedAndAcknowledged()
        {
            using (var rig = new Rig(new FakePhone("https://example.com/terms", "android-app://com.a.b", "Terms App")))
            {
                Assert.True(WaitFor(() => rig.Platform.Opened.Contains("https://example.com/terms")), "the page was opened");
                Assert.True(WaitFor(() => rig.Phone.Got("Opened on your computer.")), "the phone got a status");
                Assert.True(WaitFor(() => rig.Phone.Got("\"type\":\"done\"")), "the phone was told it's done");
                Assert.Empty(rig.Platform.Claims);
            }
        }

        [Fact]
        public void SignInIsClaimedFilteredDeliveredAndReleased()
        {
            using (var rig = new Rig(new FakePhone(SignInUrl("S1"))))
            {
                Assert.True(WaitFor(() => rig.Platform.Claims.Length == 1 && rig.Active()), "the scheme was claimed");
                FakeClaim claim = rig.Platform.Claims[0];
                Assert.Equal("com.example.app", claim.Scheme);
                Assert.True(WaitFor(() => rig.Phone.Got("Finish signing in on your computer.")), "the phone got the status line");
                Assert.Empty(rig.Platform.Opened); // LOGINRELAY_BROWSER=none

                // Wrong state: ignored, still waiting, still claimed.
                rig.Ui.Invoke(() => rig.Engine.ReceiveCallback(Callback + "?code=EVIL&state=other"));
                Thread.Sleep(300);
                Assert.Empty(rig.Adb.Delivered);
                Assert.True(rig.Active());
                Assert.False(claim.Released);

                // Another app's address: ignored silently.
                rig.Ui.Invoke(() => rig.Engine.ReceiveCallback("com.other.app://x/y?state=S1"));
                Assert.Empty(rig.Adb.Delivered);

                // The right one: handed to the phone, claim released, phone told "done".
                string good = Callback + "?code=GOOD&state=S1";
                rig.Ui.Invoke(() => rig.Engine.ReceiveCallback(good));
                Assert.True(WaitFor(() => rig.Adb.Delivered.Length == 1), "the address reached adb");
                Delivery delivery = rig.Adb.Delivered[0];
                Assert.Equal(good, delivery.Url);
                Assert.Equal("com.example.app", delivery.Package);
                Assert.Equal("FAKE0001", delivery.Serial);
                Assert.True(claim.Released);
                Assert.False(rig.Active());
                Assert.True(WaitFor(() => rig.Phone.Got("\"type\":\"done\"")), "the phone was told it's done");
            }
        }

        [Fact]
        public void CancelFromThePhoneReleasesTheClaim()
        {
            var phone = new FakePhone(SignInUrl("S2"));
            phone.CancelAfterMilliseconds = 800;
            using (var rig = new Rig(phone))
            {
                Assert.True(WaitFor(() => rig.Platform.Claims.Length == 1), "the scheme was claimed");
                FakeClaim claim = rig.Platform.Claims[0];
                Assert.True(WaitFor(() => claim.Released), "the claim was released after the phone cancelled");
                Assert.False(rig.Active());

                rig.Ui.Invoke(() => rig.Engine.ReceiveCallback(Callback + "?code=LATE&state=S2"));
                Thread.Sleep(300);
                Assert.Empty(rig.Adb.Delivered);
            }
        }

        [Fact]
        public void WebAddressesFallBackToPasting()
        {
            string page = "https://x.example/authorize?response_type=code&code_challenge=z&redirect_uri=https%3A%2F%2Fapp.example%2Fcb";
            using (var rig = new Rig(new FakePhone(page, "android-app://a.b", "Web App")))
            {
                Assert.True(WaitFor(() => rig.Platform.Prompt != null), "the paste prompt was shown");
                Assert.Empty(rig.Platform.Claims);
                FakePrompt prompt = rig.Platform.Prompt!;

                string? problem = rig.Ui.Invoke(() => prompt.Check("https://elsewhere.example/cb?code=1"));
                Assert.Equal("That isn't this app's return address. Try again.", problem);
                Assert.Empty(rig.Adb.Delivered);

                string? accepted = rig.Ui.Invoke(() => prompt.Check("  https://app.example/cb?code=1  "));
                Assert.Null(accepted);
                Assert.True(WaitFor(() => rig.Adb.Delivered.Length == 1), "the pasted address reached adb");
                Assert.Equal("https://app.example/cb?code=1", rig.Adb.Delivered[0].Url);
                Assert.True(prompt.Closed, "the prompt was dismissed");
            }
        }

        [Fact]
        public void RefusedClaimFallsBackToPastingToo()
        {
            using (var rig = new Rig(new FakePhone(SignInUrl("S3")), false))
            {
                Assert.True(WaitFor(() => rig.Platform.Prompt != null), "the paste prompt was shown");
                Assert.Empty(rig.Platform.Claims);
                FakePrompt prompt = rig.Platform.Prompt!;

                string? mismatch = rig.Ui.Invoke(() => prompt.Check(Callback + "?code=1&state=nope"));
                Assert.Equal("That address is from a different sign-in. Try again.", mismatch);

                string? accepted = rig.Ui.Invoke(() => prompt.Check(Callback + "?code=1&state=S3"));
                Assert.Null(accepted);
                Assert.True(WaitFor(() => rig.Adb.Delivered.Length == 1), "the pasted address reached adb");
            }
        }

        [Fact]
        public void StatusWordingMatchesTheMacApp()
        {
            using (var rig = new Rig(new FakePhone(null)))
            {
                Assert.True(WaitFor(() => rig.Ui.Invoke(() => rig.Engine.Phase.Kind == PhaseKind.Ready)), "ready");
                (string status, string detail) = rig.Ui.Invoke(() => rig.Engine.Describe());
                Assert.Equal("Fake Phone is ready", status);
                Assert.Equal("Sign in to an app on the phone and the page opens here.", detail);
            }
        }
    }
}
