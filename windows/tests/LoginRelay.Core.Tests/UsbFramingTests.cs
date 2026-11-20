// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading;
using LoginRelay.Core;
using Xunit;

namespace LoginRelay.Core.Tests
{
    public class UsbFramingTests
    {
        private static byte[] Bytes(string text)
        {
            return Encoding.UTF8.GetBytes(text);
        }

        [Fact]
        public void FrameAddsOneNewline()
        {
            byte[] json = Bytes("{\"type\":\"ping\"}");
            byte[] frame = UsbFraming.Frame(json);
            Assert.Equal(json.Length + 1, frame.Length);
            Assert.Equal((byte)0x0A, frame[frame.Length - 1]);
            Assert.Equal(json, frame.Take(json.Length).ToArray());
        }

        [Theory]
        [InlineData(63)]
        [InlineData(127)]
        [InlineData(511)]
        [InlineData(1023)]
        public void FrameAddsASecondNewlineWhenTheLengthIsAMultipleOf64(int jsonLength)
        {
            byte[] json = Enumerable.Repeat((byte)'a', jsonLength).ToArray();
            byte[] frame = UsbFraming.Frame(json);
            Assert.Equal(jsonLength + 2, frame.Length);
            Assert.Equal((byte)0x0A, frame[jsonLength]);
            Assert.Equal((byte)0x0A, frame[jsonLength + 1]);
            Assert.NotEqual(0, frame.Length % 64); // the padded frame is not a multiple of 64
        }

        [Theory]
        [InlineData(0)]
        [InlineData(1)]
        [InlineData(62)]
        [InlineData(64)]
        [InlineData(65)]
        [InlineData(126)]
        public void FrameNeverEndsOnAPacketBoundary(int jsonLength)
        {
            byte[] frame = UsbFraming.Frame(Enumerable.Repeat((byte)'a', jsonLength).ToArray());
            Assert.NotEqual(0, frame.Length % 64);
        }

        [Fact]
        public void FrameLengthIsNeverAMultipleOf64ForAnyJsonLength()
        {
            for (int n = 0; n < 2100; n++)
            {
                byte[] frame = UsbFraming.Frame(new byte[n]);
                Assert.True(frame.Length % 64 != 0, "length " + n);
            }
        }

        [Fact]
        public void SplitterReturnsCompleteLinesAndHoldsOverTheRest()
        {
            var splitter = new LineSplitter();
            byte[] a = Bytes("{\"a\":1}\n{\"b\":");
            List<byte[]>? first = splitter.Feed(a, a.Length);
            Assert.NotNull(first);
            Assert.Single(first!);
            Assert.Equal("{\"a\":1}", Encoding.UTF8.GetString(first![0]));

            byte[] b = Bytes("2}\n");
            List<byte[]>? second = splitter.Feed(b, b.Length);
            Assert.NotNull(second);
            Assert.Single(second!);
            Assert.Equal("{\"b\":2}", Encoding.UTF8.GetString(second![0]));
        }

        [Fact]
        public void SplitterSkipsBlankLines()
        {
            var splitter = new LineSplitter();
            byte[] data = Bytes("\n\n{\"a\":1}\n\n\n{\"b\":2}\n\n");
            List<byte[]>? lines = splitter.Feed(data, data.Length);
            Assert.NotNull(lines);
            Assert.Equal(2, lines!.Count);
        }

        [Fact]
        public void SplitterHonoursTheCountNotTheBufferLength()
        {
            var splitter = new LineSplitter();
            byte[] data = Bytes("{\"a\":1}\nXXXXXXXX\n");
            List<byte[]>? lines = splitter.Feed(data, 8);
            Assert.NotNull(lines);
            Assert.Single(lines!);
        }

        [Fact]
        public void SplitterReadsAFramedMessageWithItsPadding()
        {
            byte[] json = Enumerable.Repeat((byte)'a', 63).ToArray();
            byte[] frame = UsbFraming.Frame(json);
            var splitter = new LineSplitter();
            List<byte[]>? lines = splitter.Feed(frame, frame.Length);
            Assert.NotNull(lines);
            Assert.Single(lines!);
            Assert.Equal(json, lines![0]);
        }

        [Fact]
        public void SplitterAcceptsALineOfExactlyOneMebibyteButNotMore()
        {
            var ok = new LineSplitter();
            byte[] max = new byte[UsbFraming.MaxLine];
            for (int i = 0; i < max.Length; i++)
            {
                max[i] = (byte)'x';
            }
            Assert.NotNull(ok.Feed(max, max.Length));

            var broken = new LineSplitter();
            byte[] tooLong = new byte[UsbFraming.MaxLine + 1];
            for (int i = 0; i < tooLong.Length; i++)
            {
                tooLong[i] = (byte)'x';
            }
            Assert.Null(broken.Feed(tooLong, tooLong.Length));
        }

        [Fact]
        public void ParseMessageAcceptsObjectsOnly()
        {
            Assert.NotNull(UsbFraming.ParseMessage(Bytes("{\"type\":\"pong\"}")));
            Assert.Null(UsbFraming.ParseMessage(Bytes("[1,2]")));
            Assert.Null(UsbFraming.ParseMessage(Bytes("not json")));
            Assert.Null(UsbFraming.ParseMessage(new byte[0]));
        }

        // ---- the link, over a fake pipe --------------------------------------------------------------

        private static UsbPhoneLink StartLink(FakeUsbConnection conn, out List<string> types)
        {
            var seen = new List<string>();
            var link = new UsbPhoneLink(conn, TimeSpan.FromSeconds(30), TimeSpan.FromSeconds(30));
            link.OnMessage = m =>
            {
                lock (seen)
                {
                    seen.Add(m.GetProperty("type").GetString() ?? "");
                }
            };
            types = seen;
            link.Start();
            return link;
        }

        [Fact]
        public void EveryWriteTheLinkMakesEndsOffAPacketBoundary()
        {
            var backend = new FakeUsbBackend();
            backend.Add(FakeUsbBackend.Phone("1-1"));
            backend.SwitchToAccessory("1-1", "host");
            var conn = (FakeUsbConnection)backend.OpenAccessory("1-1");
            conn.RespondToPings = false;
            List<string> types;
            UsbPhoneLink link = StartLink(conn, out types);
            try
            {
                for (int n = 1; n <= 140; n++)
                {
                    link.Send("status", new string('a', n));
                }
                // The first ping may also be in there; look at everything that was written.
                Assert.True(TestUtil.WaitFor(() => conn.Count("status") == 140));
                foreach (byte[] raw in conn.RawWrites)
                {
                    Assert.True(raw.Length % 64 != 0, "a write of " + raw.Length + " bytes ends on a packet boundary");
                }
            }
            finally
            {
                link.Close();
                Assert.True(TestUtil.WaitFor(() => conn.Closed));
            }
        }

        [Fact]
        public void LongMessagesAreWrittenInPiecesOfAtMost16384Bytes()
        {
            var backend = new FakeUsbBackend();
            backend.Add(FakeUsbBackend.Phone("1-1"));
            backend.SwitchToAccessory("1-1", "host");
            var conn = (FakeUsbConnection)backend.OpenAccessory("1-1");
            conn.RespondToPings = false;
            List<string> types;
            UsbPhoneLink link = StartLink(conn, out types);
            try
            {
                string text = new string('b', 40000);
                link.Send("status", text);
                Assert.True(TestUtil.WaitFor(() => conn.GotText(text)), "the whole message arrived in one piece");
                byte[][] writes = conn.RawWrites;
                Assert.True(writes.Length >= 3);
                Assert.All(writes, w => Assert.True(w.Length <= UsbFraming.WriteChunkSize));
            }
            finally
            {
                link.Close();
                Assert.True(TestUtil.WaitFor(() => conn.Closed));
            }
        }

        [Fact]
        public void TheLinkDeliversTheMessagesThePhoneSendsAndSkipsGarbage()
        {
            var backend = new FakeUsbBackend();
            backend.Add(FakeUsbBackend.Phone("1-1"));
            backend.SwitchToAccessory("1-1", "host");
            var conn = (FakeUsbConnection)backend.OpenAccessory("1-1");
            conn.RespondToPings = false;
            List<string> types;
            UsbPhoneLink link = StartLink(conn, out types);
            try
            {
                conn.PhoneSend("");
                conn.PhoneSend("this is not json");
                conn.PhoneSend("[1]");
                conn.PhoneSend("{\"type\":\"hello\"}");
                conn.PhoneSend("{\"type\":\"pong\"}");
                Assert.True(TestUtil.WaitFor(() => { lock (types) { return types.Count == 2; } }));
                lock (types)
                {
                    Assert.Equal(new[] { "hello", "pong" }, types.ToArray());
                }
            }
            finally
            {
                link.Close();
                Assert.True(TestUtil.WaitFor(() => conn.Closed));
            }
        }

        [Fact]
        public void ClosingTheLinkClosesThePipeAndReportsOnce()
        {
            var backend = new FakeUsbBackend();
            backend.Add(FakeUsbBackend.Phone("1-1"));
            backend.SwitchToAccessory("1-1", "host");
            var conn = (FakeUsbConnection)backend.OpenAccessory("1-1");
            int closes = 0;
            var link = new UsbPhoneLink(conn, TimeSpan.FromSeconds(30), TimeSpan.FromSeconds(30));
            link.OnClose = () => Interlocked.Increment(ref closes);
            link.Start();
            link.Close();
            Assert.True(TestUtil.WaitFor(() => conn.Closed));
            Assert.True(TestUtil.WaitFor(() => Volatile.Read(ref closes) == 1));
            Thread.Sleep(300);
            Assert.Equal(1, Volatile.Read(ref closes));
            Assert.Equal(1, backend.ClosedCount);
            link.Send("done"); // after close: ignored, must not throw
        }

        [Fact]
        public void AnUnplugEndsTheLink()
        {
            var backend = new FakeUsbBackend();
            backend.Add(FakeUsbBackend.Phone("1-1"));
            backend.SwitchToAccessory("1-1", "host");
            var conn = (FakeUsbConnection)backend.OpenAccessory("1-1");
            bool closed = false;
            var link = new UsbPhoneLink(conn, TimeSpan.FromSeconds(30), TimeSpan.FromSeconds(30));
            link.OnClose = () => closed = true;
            link.Start();
            conn.Unplug();
            Assert.True(TestUtil.WaitFor(() => closed));
            Assert.True(conn.Closed);
        }

        [Fact]
        public void AliveFollowsWhatTheLinkHears()
        {
            var backend = new FakeUsbBackend();
            backend.Add(FakeUsbBackend.Phone("1-1"));
            backend.SwitchToAccessory("1-1", "host");
            var conn = (FakeUsbConnection)backend.OpenAccessory("1-1");
            conn.RespondToPings = false;
            var changes = new List<bool>();
            var link = new UsbPhoneLink(conn, TimeSpan.FromMilliseconds(50), TimeSpan.FromMilliseconds(500));
            link.OnAlive = alive => { lock (changes) { changes.Add(alive); } };
            link.Start();
            try
            {
                Thread.Sleep(300);
                lock (changes)
                {
                    Assert.Empty(changes); // never heard anything: not alive, and no change to report
                }
                conn.PhoneSend("{\"type\":\"pong\"}");
                Assert.True(TestUtil.WaitFor(() => { lock (changes) { return changes.Count == 1 && changes[0]; } }));
                // Silence for longer than the limit: not alive again.
                Assert.True(TestUtil.WaitFor(() => { lock (changes) { return changes.Count == 2 && !changes[1]; } }, 3000));
                Assert.True(conn.Count("ping") >= 2, "the link keeps pinging");
            }
            finally
            {
                link.Close();
                Assert.True(TestUtil.WaitFor(() => conn.Closed));
            }
        }

        // ---- small pure helpers ----------------------------------------------------------------------

        [Fact]
        public void LocationFormatUsesTheBusAndPortPath()
        {
            Assert.Equal("1-4.2", UsbLocation.Format(1, new byte[] { 4, 2, 0, 0 }, 2, 9));
            Assert.Equal("3-1", UsbLocation.Format(3, new byte[] { 1 }, 1, 9));
            Assert.Equal("2:7", UsbLocation.Format(2, new byte[0], 0, 7));
            Assert.Equal("2:7", UsbLocation.Format(2, new byte[0], -3, 7));
        }

        [Fact]
        public void AccessoryStringsMatchThePhoneAppsFilter()
        {
            string[] s = Aoa.Strings("1-4.2", "MY-PC");
            Assert.Equal(6, s.Length);
            Assert.Equal("Login Relay", s[0]);
            Assert.Equal("Desktop", s[1]);
            Assert.Equal("MY-PC", s[2]);
            Assert.Equal("1", s[3]);
            Assert.Equal("https://github.com/clippycoder/login-relay", s[4]);
            Assert.Equal("1-4.2", s[5]);
            Assert.Equal("Computer", Aoa.Strings("x", "")[2]);
        }

        [Fact]
        public void SwitchLimiterAllowsOneAttemptPerLocationPerTwentySeconds()
        {
            var limiter = new SwitchLimiter(TimeSpan.FromSeconds(20));
            DateTime t = new DateTime(2026, 1, 1, 12, 0, 0, DateTimeKind.Utc);
            Assert.True(limiter.TryAcquire("1-1", t));
            Assert.False(limiter.TryAcquire("1-1", t.AddSeconds(1)));
            Assert.False(limiter.TryAcquire("1-1", t.AddSeconds(19.9)));
            Assert.True(limiter.TryAcquire("1-2", t.AddSeconds(1)), "another location is independent");
            Assert.True(limiter.TryAcquire("1-1", t.AddSeconds(20)));
        }

        [Fact]
        public void SwitchLimiterForgetsALocationThatLeftTheBus()
        {
            var limiter = new SwitchLimiter(TimeSpan.FromSeconds(20));
            DateTime t = new DateTime(2026, 1, 1, 12, 0, 0, DateTimeKind.Utc);
            Assert.True(limiter.TryAcquire("1-1", t));
            limiter.Forget(new[] { "1-1", "1-2" });
            Assert.False(limiter.TryAcquire("1-1", t.AddSeconds(2)), "still plugged in: still limited");
            limiter.Forget(new[] { "1-2" });
            Assert.True(limiter.TryAcquire("1-1", t.AddSeconds(3)), "it left and came back: a fresh try");
        }

        [Fact]
        public void CandidateHeuristicAcceptsPhoneInterfacesOnly()
        {
            Assert.True(UsbHeuristics.LooksLikePhone(new[] { (6, 1, 1) }));
            Assert.True(UsbHeuristics.LooksLikePhone(new[] { (6, 1, 0) }));
            Assert.True(UsbHeuristics.LooksLikePhone(new[] { (0xFF, 0x42, 0x01) }));
            Assert.True(UsbHeuristics.LooksLikePhone(new[] { (0xFF, 0xFF, 0x00) }));
            Assert.True(UsbHeuristics.LooksLikePhone(new[] { (8, 6, 0x50), (6, 1, 1) }));
            Assert.False(UsbHeuristics.LooksLikePhone(new[] { (0xFF, 0xFF, 0xFF) }), "vendor specific, not MTP");
            Assert.False(UsbHeuristics.LooksLikePhone(new[] { (0xFF, 0x42, 0x02) }));
            Assert.False(UsbHeuristics.LooksLikePhone(new[] { (3, 1, 1) }), "keyboard");
            Assert.False(UsbHeuristics.LooksLikePhone(new[] { (9, 0, 0) }), "hub");
            Assert.False(UsbHeuristics.LooksLikePhone(new[] { (7, 1, 2) }), "printer");
            Assert.False(UsbHeuristics.LooksLikePhone(new (int, int, int)[0]));
        }

        [Fact]
        public void IsCandidateIgnoresDownloadModeAndAcceptsAccessories()
        {
            Assert.False(UsbHeuristics.IsCandidate(0x05C6, 0x9008, new[] { (0xFF, 0xFF, 0x00) }), "Qualcomm 9008 is left alone even if its interface looks like MTP");
            Assert.True(UsbHeuristics.IsCandidate(0x18D1, 0x2D00, new (int, int, int)[0]));
            Assert.True(UsbHeuristics.IsCandidate(0x18D1, 0x2D05, new (int, int, int)[0]));
            Assert.False(UsbHeuristics.IsCandidate(0x18D1, 0x2D06, new (int, int, int)[0]));
            Assert.False(UsbHeuristics.IsCandidate(0x18D1, 0x4EE1, new (int, int, int)[0]), "a Google device with no phone interface");
            Assert.True(UsbHeuristics.IsCandidate(0x18D1, 0x4EE1, new[] { (6, 1, 1) }));
        }
    }
}
