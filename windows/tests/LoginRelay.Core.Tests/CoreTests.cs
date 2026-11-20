// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using LoginRelay.Core;
using Xunit;

namespace LoginRelay.Core.Tests
{
    public class CoreTests
    {
        [Fact]
        public void PrefsRoundTrip()
        {
            string dir = Path.Combine(Path.GetTempPath(), "loginrelay-prefs-" + Guid.NewGuid().ToString("N"));
            string path = Path.Combine(dir, "prefs.json");
            try
            {
                var prefs = new Prefs(path);
                Assert.False(prefs.Notifications);
                Assert.False(prefs.HasLaunchedBefore);
                Assert.Null(prefs.UsbSelected);
                prefs.UsbSelected = "1-4.2";
                prefs.Notifications = true;
                prefs.HasLaunchedBefore = true;

                var again = new Prefs(path);
                Assert.Equal("1-4.2", again.UsbSelected);
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

        // ---- LOGINRELAY_FAKE_PHONE ---------------------------------------------------------------------

        [Theory]
        [InlineData("127.0.0.1:5555", "127.0.0.1", 5555)]
        [InlineData(" localhost:80 ", "localhost", 80)]
        [InlineData("[::1]:9", "::1", 9)]
        public void FakePhoneAddressesAreParsed(string text, string host, int port)
        {
            string parsedHost;
            int parsedPort;
            Assert.True(TcpPhoneBackend.TryParse(text, out parsedHost, out parsedPort));
            Assert.Equal(host, parsedHost);
            Assert.Equal(port, parsedPort);
        }

        [Theory]
        [InlineData(null)]
        [InlineData("")]
        [InlineData("nonsense")]
        [InlineData("host:")]
        [InlineData(":80")]
        [InlineData("host:0")]
        [InlineData("host:70000")]
        [InlineData("host:port")]
        public void MalformedFakePhoneAddressesAreRefused(string? text)
        {
            string host;
            int port;
            Assert.False(TcpPhoneBackend.TryParse(text, out host, out port));
        }

        [Fact]
        public void TheFakeBusHasExactlyOneAccessoryPhoneCalledFakePhone()
        {
            var backend = new TcpPhoneBackend("127.0.0.1", 1);
            UsbDevice device = Assert.Single(backend.ListDevices());
            Assert.True(device.IsAccessory);
            Assert.True(device.Openable);
            Assert.Equal("Fake Phone", device.Name);
            Assert.Equal(UsbCode.Ok, backend.SwitchToAccessory(device.Location, "host").Code);
        }

        [Fact]
        public void ConnectingToNothingFailsWithAUsbException()
        {
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            int port = ((IPEndPoint)listener.LocalEndpoint).Port;
            listener.Stop(); // nothing listens there now
            var backend = new TcpPhoneBackend("127.0.0.1", port);
            UsbException ex = Assert.Throws<UsbException>(() => backend.OpenAccessory(TcpPhoneBackend.PhoneLocation));
            Assert.Equal(UsbCode.Io, ex.Code);
        }

        [Fact]
        public void TheTcpPipeReadsWritesAndTimesOutLikeAUsbPipe()
        {
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            try
            {
                int port = ((IPEndPoint)listener.LocalEndpoint).Port;
                Task<TcpClient> accepting = Task.Run(() => listener.AcceptTcpClient());
                IUsbConnection conn = new TcpPhoneBackend("127.0.0.1", port).OpenAccessory(TcpPhoneBackend.PhoneLocation);
                using (TcpClient peer = accepting.Result)
                {
                    NetworkStream stream = peer.GetStream();
                    byte[] buffer = new byte[UsbFraming.ReadBufferSize];

                    // Nothing sent: a read times out and returns 0 (not an error).
                    Assert.Equal(0, conn.Read(buffer, 50));

                    // Bytes from the phone arrive.
                    byte[] hello = Encoding.UTF8.GetBytes("{\"type\":\"hello\"}\n");
                    stream.Write(hello, 0, hello.Length);
                    stream.Flush();
                    int n = 0;
                    DateTime end = DateTime.UtcNow.AddSeconds(5);
                    while (n == 0 && DateTime.UtcNow < end)
                    {
                        n = conn.Read(buffer, 200);
                    }
                    Assert.Equal(hello.Length, n);

                    // A write reaches the phone.
                    byte[] ping = UsbFraming.Frame(Encoding.UTF8.GetBytes("{\"type\":\"ping\"}"));
                    Assert.Equal(ping.Length, conn.Write(ping, 0, ping.Length, 500));
                    byte[] got = ReadExactly(stream, ping.Length);
                    Assert.Equal(ping, got);

                    // The phone hangs up: the next read says so.
                    peer.Close();
                    Assert.Throws<UsbException>(() =>
                    {
                        DateTime stop = DateTime.UtcNow.AddSeconds(5);
                        while (DateTime.UtcNow < stop)
                        {
                            conn.Read(buffer, 100);
                        }
                    });

                    conn.Close();
                    conn.Close(); // idempotent
                    Assert.Throws<UsbException>(() => conn.Read(buffer, 10));
                    Assert.Throws<UsbException>(() => conn.Write(ping, 0, ping.Length, 10));
                }
            }
            finally
            {
                listener.Stop();
            }
        }

        [Fact]
        public void TheTcpPipeDropsTheExtraNewlineThatOnlyUsbNeeds()
        {
            // A message whose length plus its newline is a multiple of 64 gets a second newline on a USB pipe.
            string json = "{\"type\":\"x\",\"text\":\"" + new string('a', 64 - "{\"type\":\"x\",\"text\":\"".Length - 3) + "\"}";
            byte[] jsonBytes = Encoding.UTF8.GetBytes(json);
            Assert.Equal(0, (jsonBytes.Length + 1) % 64);
            byte[] frame = UsbFraming.Frame(jsonBytes);
            Assert.Equal(jsonBytes.Length + 2, frame.Length);

            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            try
            {
                int port = ((IPEndPoint)listener.LocalEndpoint).Port;
                Task<TcpClient> accepting = Task.Run(() => listener.AcceptTcpClient());
                IUsbConnection conn = new TcpPhoneBackend("127.0.0.1", port).OpenAccessory(TcpPhoneBackend.PhoneLocation);
                using (TcpClient peer = accepting.Result)
                {
                    Assert.Equal(frame.Length, conn.Write(frame, 0, frame.Length, 500));
                    byte[] got = ReadExactly(peer.GetStream(), jsonBytes.Length + 1);
                    Assert.Equal(Encoding.UTF8.GetBytes(json + "\n"), got);
                    // Nothing more follows.
                    peer.GetStream().ReadTimeout = 200;
                    var one = new byte[1];
                    Assert.ThrowsAny<IOException>(() => peer.GetStream().Read(one, 0, 1));
                    conn.Close();
                }
            }
            finally
            {
                listener.Stop();
            }
        }

        private static byte[] ReadExactly(NetworkStream stream, int count)
        {
            stream.ReadTimeout = 5000;
            byte[] data = new byte[count];
            int read = 0;
            while (read < count)
            {
                int n = stream.Read(data, read, count - read);
                if (n <= 0)
                {
                    throw new EndOfStreamException();
                }
                read += n;
            }
            return data;
        }
    }
}
