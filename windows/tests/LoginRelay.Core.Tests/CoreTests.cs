// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
using LoginRelay.Core;
using Xunit;

namespace LoginRelay.Core.Tests
{
    public class CoreTests
    {
        [Fact]
        public void ParsesAdbDevicesOutput()
        {
            string output = string.Join(
                "\n",
                "List of devices attached",
                "* daemon not running; starting now at tcp:5037",
                "* daemon started successfully",
                "ABC123\tdevice usb:1-1 product:x model:Pixel_7 device:y transport_id:1",
                "DEF456\tunauthorized usb:1-2 transport_id:2",
                "");
            var devices = Adb.ParseDevices(output);
            Assert.Equal(2, devices.Count);
            Assert.Equal("ABC123", devices[0].Serial);
            Assert.True(devices[0].IsUsable);
            Assert.Equal("Pixel 7", devices[0].Title);
            Assert.Equal("DEF456", devices[1].Serial);
            Assert.False(devices[1].IsUsable);
            Assert.Equal("DEF456", devices[1].Title);
        }

        [Fact]
        public void FindsTheWindowsPlatformToolsArchive()
        {
            string xml = "<sdk:sdk-repository><remotePackage path=\"platform-tools\"><archives>"
                + "<archive><complete><size>1</size><checksum type=\"sha1\">" + new string('a', 40) + "</checksum><url>platform-tools_r35.0.2-darwin.zip</url></complete><host-os>macosx</host-os></archive>"
                + "<archive><complete><size>1</size><checksum type=\"sha1\">" + new string('b', 40) + "</checksum><url>platform-tools_r35.0.2-linux.zip</url></complete><host-os>linux</host-os></archive>"
                + "<archive><complete><size>1</size><checksum type=\"sha1\">" + new string('c', 40) + "</checksum><url>platform-tools_r35.0.2-win.zip</url></complete><host-os>windows</host-os></archive>"
                + "</archives></remotePackage></sdk:sdk-repository>";
            var (sha1, file) = AdbInstaller.FindArchive(xml);
            Assert.Equal(new string('c', 40), sha1);
            Assert.Equal("platform-tools_r35.0.2-win.zip", file);
            Assert.False(AdbInstaller.IsWindowsArchive("platform-tools_r35.0.2-darwin.zip"));
            Assert.Throws<AdbInstaller.InstallException>(() =>
            {
                AdbInstaller.FindArchive("<nothing/>");
            });
        }

        [Fact]
        public void PrefsRoundTrip()
        {
            string dir = Path.Combine(Path.GetTempPath(), "loginrelay-prefs-" + Guid.NewGuid().ToString("N"));
            string path = Path.Combine(dir, "prefs.json");
            try
            {
                var prefs = new Prefs(path);
                Assert.Null(prefs.AdbPath);
                Assert.False(prefs.Notifications);
                Assert.False(prefs.HasLaunchedBefore);
                prefs.AdbPath = @"C:\tools\adb.exe";
                prefs.SelectedSerial = "ABC";
                prefs.Notifications = true;
                prefs.HasLaunchedBefore = true;

                var again = new Prefs(path);
                Assert.Equal(@"C:\tools\adb.exe", again.AdbPath);
                Assert.Equal("ABC", again.SelectedSerial);
                Assert.True(again.Notifications);
                Assert.True(again.HasLaunchedBefore);
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

        private static ProcessResult RunShell(string command, double timeoutSeconds)
        {
            if (OperatingSystem.IsWindows())
            {
                return Adb.Exec("cmd.exe", new[] { "/c", command }, timeoutSeconds);
            }
            return Adb.Exec("/bin/sh", new[] { "-c", command }, timeoutSeconds);
        }

        [Fact]
        public void ExecCollectsOutputAndStatus()
        {
            ProcessResult ok = RunShell("echo hello", 20);
            Assert.True(ok.Ok);
            Assert.Equal("hello", ok.Out);

            ProcessResult bad = RunShell("exit 3", 20);
            Assert.Equal(3, bad.Status);
        }

        [Fact]
        public void ExecReportsAProgramThatCannotStart()
        {
            ProcessResult r = Adb.Exec(Path.Combine(Path.GetTempPath(), "no-such-adb-" + Guid.NewGuid().ToString("N")), new string[0], 5);
            Assert.False(r.Ok);
        }

        [Fact]
        public void ExecKillsAProgramThatOverstaysItsTimeout()
        {
            string slow = OperatingSystem.IsWindows() ? "ping -n 8 127.0.0.1 > nul" : "sleep 8";
            var clock = Stopwatch.StartNew();
            ProcessResult r = RunShell(slow, 1);
            Assert.False(r.Ok);
            Assert.True(clock.Elapsed < TimeSpan.FromSeconds(6), "took " + clock.Elapsed);
        }

        [Fact]
        public void ExecDoesNotWaitForADaemonHoldingTheOutput()
        {
            // The shell exits at once but leaves a background process holding stdout, like adb starting its
            // server. Exec must return soon after the shell exits, not when the daemon does.
            string command = OperatingSystem.IsWindows() ? "start /b ping -n 6 127.0.0.1" : "sleep 6 &";
            var clock = Stopwatch.StartNew();
            ProcessResult r = RunShell(command, 20);
            Assert.True(clock.Elapsed < TimeSpan.FromSeconds(4), "took " + clock.Elapsed);
        }

        [Fact]
        public void LinkRejectsAConnectionThatIsClosedAtOnce()
        {
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            try
            {
                int port = ((IPEndPoint)listener.LocalEndpoint).Port;
                Task accepting = Task.Run(() =>
                {
                    using (TcpClient client = listener.AcceptTcpClient())
                    {
                    }
                });
                var link = new PhoneLink();
                Assert.False(link.Open(port));
                accepting.Wait(5000);
            }
            finally
            {
                listener.Stop();
            }
        }

        [Fact]
        public void LinkAcceptsAConnectionThatStaysOpenAndReportsItsEnd()
        {
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            var release = new ManualResetEventSlim(false);
            var closed = new ManualResetEventSlim(false);
            try
            {
                int port = ((IPEndPoint)listener.LocalEndpoint).Port;
                Task accepting = Task.Run(() =>
                {
                    using (TcpClient client = listener.AcceptTcpClient())
                    {
                        release.Wait(10000);
                    }
                });
                var link = new PhoneLink();
                link.OnClose = () => closed.Set();
                Assert.True(link.Open(port));
                Assert.False(closed.IsSet);
                link.Close();
                Assert.True(closed.Wait(5000), "OnClose fired after Close");
                release.Set();
                accepting.Wait(5000);
            }
            finally
            {
                release.Set();
                listener.Stop();
            }
        }
    }
}
