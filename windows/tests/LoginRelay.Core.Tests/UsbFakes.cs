// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;
using LoginRelay.Core;

namespace LoginRelay.Core.Tests
{
    internal static class TestUtil
    {
        public static bool WaitFor(Func<bool> condition, int milliseconds = 15000)
        {
            DateTime end = DateTime.UtcNow.AddMilliseconds(milliseconds);
            while (DateTime.UtcNow < end)
            {
                if (condition())
                {
                    return true;
                }
                Thread.Sleep(20);
            }
            return condition();
        }
    }

    /// <summary>A phone's accessory pipe, in memory. The test plays the phone: PhoneSend, Silent, Unplug.</summary>
    internal sealed class FakeUsbConnection : IUsbConnection
    {
        private readonly FakeUsbBackend _backend;
        private readonly string _location;
        private readonly BlockingCollection<byte[]> _inbox = new BlockingCollection<byte[]>();
        private readonly object _lock = new object();
        private readonly LineSplitter _splitter = new LineSplitter();
        private readonly List<Dictionary<string, string>> _messages = new List<Dictionary<string, string>>();
        private readonly List<byte[]> _rawWrites = new List<byte[]>();
        private volatile bool _broken;
        private int _closed;

        public FakeUsbConnection(FakeUsbBackend backend, string location, bool respondToPings)
        {
            _backend = backend;
            _location = location;
            RespondToPings = respondToPings;
        }

        /// <summary>Answer each ping with a pong, like the phone app does while its process is alive.</summary>
        public volatile bool RespondToPings;

        public bool Closed
        {
            get { return Volatile.Read(ref _closed) != 0; }
        }

        public byte[][] RawWrites
        {
            get { lock (_lock) { return _rawWrites.ToArray(); } }
        }

        public Dictionary<string, string>[] Messages
        {
            get { lock (_lock) { return _messages.ToArray(); } }
        }

        public Dictionary<string, string>? Message(string type)
        {
            return Messages.FirstOrDefault(m => m.TryGetValue("type", out string? t) && t == type);
        }

        public int Count(string type)
        {
            return Messages.Count(m => m.TryGetValue("type", out string? t) && t == type);
        }

        public bool GotText(string text)
        {
            return Messages.Any(m => m.TryGetValue("text", out string? t) && t == text);
        }

        /// <summary>The phone sends one line.</summary>
        public void PhoneSend(string json)
        {
            if (!_inbox.IsAddingCompleted)
            {
                _inbox.Add(Encoding.UTF8.GetBytes(json + "\n"));
            }
        }

        public void PhoneHello(string version = "0.6.0")
        {
            PhoneSend("{\"v\":1,\"type\":\"hello\",\"transport\":\"usb\",\"version\":\"" + version + "\"}");
        }

        /// <summary>The cable comes out: the device leaves the bus and every transfer fails.</summary>
        public void Unplug()
        {
            _broken = true;
            _backend.Remove(_location);
        }

        public int Read(byte[] buffer, int timeoutMs)
        {
            if (Closed || _broken)
            {
                throw new UsbException(UsbCode.NoDevice, "gone");
            }
            byte[]? chunk;
            if (!_inbox.TryTake(out chunk, timeoutMs))
            {
                if (_broken)
                {
                    throw new UsbException(UsbCode.NoDevice, "unplugged");
                }
                return 0;
            }
            int n = Math.Min(chunk.Length, buffer.Length);
            Buffer.BlockCopy(chunk, 0, buffer, 0, n);
            return n;
        }

        public int Write(byte[] data, int offset, int count, int timeoutMs)
        {
            if (Closed || _broken)
            {
                throw new UsbException(UsbCode.NoDevice, "gone");
            }
            byte[] copy = new byte[count];
            Buffer.BlockCopy(data, offset, copy, 0, count);
            bool pong = false;
            lock (_lock)
            {
                _rawWrites.Add(copy);
                List<byte[]>? lines = _splitter.Feed(copy, copy.Length);
                if (lines != null)
                {
                    foreach (byte[] line in lines)
                    {
                        var message = new Dictionary<string, string>();
                        try
                        {
                            using (JsonDocument doc = JsonDocument.Parse(line))
                            {
                                foreach (JsonProperty p in doc.RootElement.EnumerateObject())
                                {
                                    if (p.Value.ValueKind == JsonValueKind.String)
                                    {
                                        message[p.Name] = p.Value.GetString() ?? "";
                                    }
                                }
                            }
                        }
                        catch (JsonException)
                        {
                            message["type"] = "garbage";
                        }
                        _messages.Add(message);
                        if (message.TryGetValue("type", out string? type) && type == "ping")
                        {
                            pong = true;
                        }
                    }
                }
            }
            if (pong && RespondToPings)
            {
                PhoneSend("{\"type\":\"pong\"}");
            }
            return count;
        }

        public void Close()
        {
            Interlocked.Exchange(ref _closed, 1);
            _backend.NoteClosed(this);
        }
    }

    /// <summary>A fake USB bus. A switch turns the device at that location into an accessory (18D1:2D00).</summary>
    internal sealed class FakeUsbBackend : IUsbBackend
    {
        private readonly object _lock = new object();
        private readonly List<UsbDevice> _devices = new List<UsbDevice>();
        private readonly List<string> _switches = new List<string>();
        private readonly List<string> _hostNames = new List<string>();
        private readonly List<FakeUsbConnection> _connections = new List<FakeUsbConnection>();
        private int _openCount;
        private int _closedCount;
        private int _listCalls;

        /// <summary>The result of a switch request (UsbCode.Ok means the phone accepted).</summary>
        public int SwitchCode = UsbCode.Ok;

        /// <summary>After an accepted switch, does the accessory device show up on the bus?</summary>
        public bool AccessoryAppears = true;

        /// <summary>Is the accessory device usable by libusb (WinUSB bound)?</summary>
        public bool AccessoryOpenable = true;

        /// <summary>Do new connections answer pings?</summary>
        public bool RespondToPings = true;

        /// <summary>If not zero, opening the accessory fails with this UsbCode.</summary>
        public int OpenFailCode = 0;

        public static UsbDevice Phone(string location, string name = "Fake Phone", ushort vid = 0x04E8, ushort pid = 0x6860)
        {
            return new UsbDevice(vid, pid, location, false, true, name, "SER" + location);
        }

        public void Add(UsbDevice device)
        {
            lock (_lock)
            {
                _devices.Add(device);
            }
        }

        public void Remove(string location)
        {
            lock (_lock)
            {
                _devices.RemoveAll(d => d.Location == location);
            }
        }

        public string[] Switches
        {
            get { lock (_lock) { return _switches.ToArray(); } }
        }

        public string[] HostNames
        {
            get { lock (_lock) { return _hostNames.ToArray(); } }
        }

        public int OpenCount
        {
            get { lock (_lock) { return _openCount; } }
        }

        public int ClosedCount
        {
            get { lock (_lock) { return _closedCount; } }
        }

        public int ListCalls
        {
            get { lock (_lock) { return _listCalls; } }
        }

        public FakeUsbConnection? Connection
        {
            get { lock (_lock) { return _connections.Count == 0 ? null : _connections[_connections.Count - 1]; } }
        }

        public FakeUsbConnection[] Connections
        {
            get { lock (_lock) { return _connections.ToArray(); } }
        }

        public void NoteClosed(FakeUsbConnection connection)
        {
            lock (_lock)
            {
                _closedCount++;
            }
        }

        public IReadOnlyList<UsbDevice> ListDevices()
        {
            lock (_lock)
            {
                _listCalls++;
                return _devices.ToList();
            }
        }

        public UsbSwitchResult SwitchToAccessory(string location, string hostName)
        {
            lock (_lock)
            {
                _switches.Add(location);
                _hostNames.Add(hostName);
                UsbDevice? device = _devices.FirstOrDefault(d => d.Location == location);
                if (device == null)
                {
                    return new UsbSwitchResult(UsbCode.NotFound);
                }
                if (SwitchCode != UsbCode.Ok)
                {
                    return new UsbSwitchResult(SwitchCode, 0, "fake failure");
                }
                if (AccessoryAppears)
                {
                    _devices.RemoveAll(d => d.Location == location);
                    _devices.Add(new UsbDevice(Aoa.VendorGoogle, Aoa.PidFirst, location, true, true, device.Name, device.Serial, AccessoryOpenable));
                }
                return new UsbSwitchResult(UsbCode.Ok, 2);
            }
        }

        public IUsbConnection OpenAccessory(string location)
        {
            lock (_lock)
            {
                UsbDevice? device = _devices.FirstOrDefault(d => d.Location == location && d.IsAccessory);
                if (device == null)
                {
                    throw new UsbException(UsbCode.NotFound, "no accessory at " + location);
                }
                if (!device.Openable)
                {
                    throw new UsbException(UsbCode.NoDriver, "not bound to WinUSB");
                }
                if (OpenFailCode != 0)
                {
                    throw new UsbException(OpenFailCode, "fake open failure");
                }
                _openCount++;
                var connection = new FakeUsbConnection(this, location, RespondToPings);
                _connections.Add(connection);
                return connection;
            }
        }
    }
}
