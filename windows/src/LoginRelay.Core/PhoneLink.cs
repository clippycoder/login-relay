// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;

namespace LoginRelay.Core
{
    /// <summary>
    /// The connection to the Login Relay app on the phone, through `adb forward`.
    ///
    /// adb accepts the TCP connection even when nothing is listening on the phone, then closes it. So a
    /// connect only counts once it stays open for a moment (400 ms).
    /// </summary>
    public sealed class PhoneLink : IPhoneConnection
    {
        public const int StayOpenMilliseconds = 400;

        private Socket? _socket;
        private readonly object _lock = new object();
        private bool _closed;

        /// <summary>Called on the read thread for each JSON message from the phone (a detached copy).</summary>
        public Action<JsonElement>? OnMessage { get; set; }

        /// <summary>Called once, on the read thread, when an established link ends.</summary>
        public Action? OnClose { get; set; }

        /// <summary>Connects to 127.0.0.1:port. Returns false if the phone app isn't listening right now.</summary>
        public bool Open(int port)
        {
            Socket? s = null;
            try
            {
                s = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
                s.NoDelay = true;
                s.Connect(new IPEndPoint(IPAddress.Loopback, port));

                // Give adb a moment to drop us if the phone side isn't there. Poll says "readable" both
                // when data arrived and when the connection was closed; nothing available means closed.
                if (s.Poll(StayOpenMilliseconds * 1000, SelectMode.SelectRead))
                {
                    if (s.Available == 0)
                    {
                        s.Dispose();
                        return false;
                    }
                }
            }
            catch (Exception)
            {
                if (s != null)
                {
                    s.Dispose();
                }
                return false;
            }

            Socket connected = s!;
            lock (_lock)
            {
                _socket = connected;
                _closed = false;
            }
            var thread = new Thread(() => ReadLoop(connected));
            thread.IsBackground = true;
            thread.Name = "phone-link";
            thread.Start();
            return true;
        }

        /// <summary>Sends {"type":type} or {"type":type,"text":text}, newline-terminated. Never throws.</summary>
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

        /// <summary>Sends a JSON object whose members are all strings, newline-terminated. Never throws.</summary>
        public void SendFields(Dictionary<string, string> obj)
        {
            byte[] data = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(obj) + "\n");
            lock (_lock)
            {
                if (_socket == null || _closed)
                {
                    return;
                }
                try
                {
                    int sent = 0;
                    while (sent < data.Length)
                    {
                        int n = _socket.Send(data, sent, data.Length - sent, SocketFlags.None);
                        if (n <= 0)
                        {
                            return;
                        }
                        sent += n;
                    }
                }
                catch (SocketException)
                {
                }
                catch (ObjectDisposedException)
                {
                }
            }
        }

        /// <summary>Hangs up; the read thread then ends and reports OnClose.</summary>
        public void Close()
        {
            Socket? s;
            lock (_lock)
            {
                _closed = true;
                s = _socket;
            }
            if (s == null)
            {
                return;
            }
            try
            {
                s.Shutdown(SocketShutdown.Both);
            }
            catch (Exception)
            {
                // Already closed, or the read thread disposed it first.
            }
            try
            {
                // Shutdown wakes a thread blocked in Receive on Linux and macOS but not on Windows,
                // where only closing the socket aborts the pending call. The read loop then sees the
                // exception, ends, and reports OnClose.
                s.Dispose();
            }
            catch (Exception)
            {
            }
        }

        private void ReadLoop(Socket s)
        {
            var line = new List<byte>();
            byte[] chunk = new byte[4096];
            while (true)
            {
                int n;
                try
                {
                    n = s.Receive(chunk);
                }
                catch (Exception)
                {
                    break;
                }
                if (n <= 0)
                {
                    break;
                }
                for (int i = 0; i < n; i++)
                {
                    byte b = chunk[i];
                    if (b == 0x0A)
                    {
                        Deliver(line);
                        line.Clear();
                    }
                    else
                    {
                        line.Add(b);
                    }
                }
                if (line.Count > 1024 * 1024)
                {
                    break; // a runaway line; hang up rather than grow forever
                }
            }

            lock (_lock)
            {
                _closed = true;
                if (ReferenceEquals(_socket, s))
                {
                    _socket = null;
                }
            }
            try
            {
                s.Dispose();
            }
            catch (Exception)
            {
            }
            try
            {
                OnClose?.Invoke();
            }
            catch (Exception)
            {
            }
        }

        private void Deliver(List<byte> bytes)
        {
            if (bytes.Count == 0)
            {
                return;
            }
            JsonElement message;
            try
            {
                string text = Encoding.UTF8.GetString(bytes.ToArray());
                using (JsonDocument doc = JsonDocument.Parse(text))
                {
                    if (doc.RootElement.ValueKind != JsonValueKind.Object)
                    {
                        return;
                    }
                    message = doc.RootElement.Clone();
                }
            }
            catch (JsonException)
            {
                return;
            }
            try
            {
                OnMessage?.Invoke(message);
            }
            catch (Exception)
            {
                // A handler's failure must not take the read thread (and the process) down.
            }
        }
    }
}
