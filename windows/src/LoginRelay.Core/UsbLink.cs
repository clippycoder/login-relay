// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Text.Json;
using System.Threading;

namespace LoginRelay.Core
{
    /// <summary>What the engine needs from a connection to the phone, whichever way it's wired.</summary>
    public interface IPhoneConnection
    {
        /// <summary>Sends {"type":type} or {"type":type,"text":text}. Never throws.</summary>
        void Send(string type, string? text = null);

        /// <summary>Sends a JSON object whose members are all strings. Never throws.</summary>
        void SendFields(Dictionary<string, string> fields);

        /// <summary>Hangs up; the read thread then ends and reports OnClose.</summary>
        void Close();
    }

    /// <summary>
    /// The connection to Login Relay on the phone over a USB accessory pipe (line-delimited JSON). Bulk USB has no "connection closed", so the link pings the phone and tracks whether anything
    /// has come back recently ("alive"). Port of UsbPhoneLink in macos/Sources/Usb.swift.
    /// </summary>
    public sealed class UsbPhoneLink : IPhoneConnection
    {
        private const int ReadTimeoutMs = 200;
        private const int WriteTimeoutMs = 500;

        private readonly IUsbConnection _conn;
        private readonly TimeSpan _pingInterval;
        private readonly TimeSpan _silence;
        private readonly object _writeLock = new object();
        private bool _closed;                   // guarded by _writeLock
        private volatile bool _stopping;

        /// <summary>Called on the read thread for each JSON message from the phone (a detached copy).</summary>
        public Action<JsonElement>? OnMessage { get; set; }

        /// <summary>Called once, on the read thread, when the link has ended and the pipe is closed.</summary>
        public Action? OnClose { get; set; }

        /// <summary>Called on the read thread when the phone app starts or stops answering.</summary>
        public Action<bool>? OnAlive { get; set; }

        public UsbPhoneLink(IUsbConnection conn, TimeSpan pingInterval, TimeSpan silence)
        {
            _conn = conn;
            _pingInterval = pingInterval;
            _silence = silence;
        }

        public void Start()
        {
            var thread = new Thread(ReadLoop);
            thread.IsBackground = true;
            thread.Name = "usb-link";
            thread.Start();
        }

        public void Send(string type, string? text = null)
        {
            var obj = new Dictionary<string, string>();
            obj["type"] = type;
            if (text != null)
            {
                obj["text"] = text;
            }
            SendFields(obj);
        }

        public void SendFields(Dictionary<string, string> fields)
        {
            byte[] frame;
            try
            {
                frame = UsbFraming.Frame(JsonSerializer.SerializeToUtf8Bytes(fields));
            }
            catch (Exception)
            {
                return;
            }
            lock (_writeLock)
            {
                if (_closed)
                {
                    return;
                }
                int offset = 0;
                while (offset < frame.Length)
                {
                    int chunk = Math.Min(frame.Length - offset, UsbFraming.WriteChunkSize);
                    int written;
                    try
                    {
                        written = _conn.Write(frame, offset, chunk, WriteTimeoutMs);
                    }
                    catch (Exception)
                    {
                        return;
                    }
                    // A timeout means the phone app isn't reading; drop the message. Liveness covers the rest.
                    if (written <= 0)
                    {
                        return;
                    }
                    offset += written;
                }
            }
        }

        /// <summary>Ends the link; the read thread notices within a fraction of a second.</summary>
        public void Close()
        {
            _stopping = true;
        }

        private void ReadLoop()
        {
            byte[] buffer = new byte[UsbFraming.ReadBufferSize];
            var splitter = new LineSplitter();
            Stopwatch clock = Stopwatch.StartNew();
            double lastHeard = double.NegativeInfinity;
            double lastPing = double.NegativeInfinity;
            bool alive = false;
            try
            {
                while (!_stopping)
                {
                    int n;
                    try
                    {
                        n = _conn.Read(buffer, ReadTimeoutMs);
                    }
                    catch (Exception)
                    {
                        return;
                    }
                    if (n > 0)
                    {
                        lastHeard = clock.Elapsed.TotalSeconds;
                        List<byte[]>? lines = splitter.Feed(buffer, n);
                        if (lines == null)
                        {
                            return; // a runaway line; hang up rather than grow forever
                        }
                        foreach (byte[] line in lines)
                        {
                            Deliver(line);
                        }
                    }
                    double now = clock.Elapsed.TotalSeconds;
                    if (now - lastPing > _pingInterval.TotalSeconds)
                    {
                        Send("ping");
                        lastPing = clock.Elapsed.TotalSeconds;
                    }
                    bool nowAlive = now - lastHeard < _silence.TotalSeconds;
                    if (nowAlive != alive)
                    {
                        alive = nowAlive;
                        try
                        {
                            OnAlive?.Invoke(alive);
                        }
                        catch (Exception)
                        {
                        }
                    }
                }
            }
            finally
            {
                lock (_writeLock)
                {
                    _closed = true;
                    try
                    {
                        _conn.Close();
                    }
                    catch (Exception)
                    {
                    }
                }
                try
                {
                    OnClose?.Invoke();
                }
                catch (Exception)
                {
                }
            }
        }

        private void Deliver(byte[] line)
        {
            JsonElement? message = UsbFraming.ParseMessage(line);
            if (message == null)
            {
                return;
            }
            try
            {
                OnMessage?.Invoke(message.Value);
            }
            catch (Exception)
            {
                // A handler's failure must not take the read thread (and the process) down.
            }
        }
    }
}
