// SPDX-License-Identifier: GPL-3.0-or-later
// Test hook: LOGINRELAY_FAKE_PHONE=host:port makes the app behave as if exactly one USB phone ("Fake Phone") were
// plugged in, without touching libusb. "Opening" it connects a plain TCP socket to host:port, and the engine runs
// the same USB-mode logic over that socket (hello, ping/pong liveness, callback delivery). Same line-delimited JSON
// as the real pipe, but no 64-byte padding on TCP.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;

namespace LoginRelay.Core
{
    /// <summary>One always-present accessory-mode phone whose pipe is a TCP connection.</summary>
    public sealed class TcpPhoneBackend : IUsbBackend
    {
        public const string PhoneLocation = "fake-1";
        public const string PhoneName = "Fake Phone";

        private readonly string _host;
        private readonly int _port;

        public TcpPhoneBackend(string host, int port)
        {
            _host = host;
            _port = port;
        }

        /// <summary>Parses "host:port". False (and the real USB stack is used) if the text is empty or malformed.</summary>
        public static bool TryParse(string? text, out string host, out int port)
        {
            host = "";
            port = 0;
            string t = (text ?? "").Trim();
            int colon = t.LastIndexOf(':');
            if (colon <= 0 || colon == t.Length - 1)
            {
                return false;
            }
            int parsed;
            if (!int.TryParse(t.Substring(colon + 1), out parsed) || parsed <= 0 || parsed > 65535)
            {
                return false;
            }
            host = t.Substring(0, colon).Trim('[', ']');
            port = parsed;
            return host.Length > 0;
        }

        public IReadOnlyList<UsbDevice> ListDevices()
        {
            // Already in accessory mode, so the engine opens it without a switch.
            return new List<UsbDevice>
            {
                new UsbDevice(Aoa.VendorGoogle, Aoa.PidFirst, PhoneLocation, true, true, PhoneName, "FAKE0001"),
            };
        }

        public UsbSwitchResult SwitchToAccessory(string location, string hostName)
        {
            return new UsbSwitchResult(UsbCode.Ok, 2);
        }

        public IUsbConnection OpenAccessory(string location)
        {
            return TcpPhoneConnection.Connect(_host, _port, 3000);
        }
    }

    /// <summary>
    /// An <see cref="IUsbConnection"/> over a TCP socket. Windows lesson: closing
    /// (disposing) the socket is what wakes a blocked call, and the engine's link runs the reads on its own thread.
    /// </summary>
    public sealed class TcpPhoneConnection : IUsbConnection
    {
        private readonly Socket _socket;
        private int _closed;

        private TcpPhoneConnection(Socket socket)
        {
            _socket = socket;
        }

        /// <summary>Connects (waiting at most <paramref name="timeoutMs"/>). Throws <see cref="UsbException"/> if it can't.</summary>
        public static TcpPhoneConnection Connect(string host, int port, int timeoutMs)
        {
            Socket? socket = null;
            try
            {
                IPAddress? resolved;
                if (!IPAddress.TryParse(host, out resolved))
                {
                    resolved = Dns.GetHostAddresses(host).FirstOrDefault(a => a.AddressFamily == AddressFamily.InterNetwork);
                }
                if (resolved == null)
                {
                    throw new UsbException(UsbCode.Io, "couldn't find " + host);
                }
                IPAddress address = resolved;
                Socket created = new Socket(address.AddressFamily, SocketType.Stream, ProtocolType.Tcp);
                socket = created;
                created.NoDelay = true;
                Task connecting = Task.Run(() => created.Connect(new IPEndPoint(address, port)));
                if (!connecting.Wait(timeoutMs))
                {
                    throw new UsbException(UsbCode.Io, "timed out connecting to " + host + ":" + port);
                }
                return new TcpPhoneConnection(created);
            }
            catch (UsbException)
            {
                DisposeQuietly(socket);
                throw;
            }
            catch (Exception ex)
            {
                DisposeQuietly(socket);
                Exception inner = ex is AggregateException && ex.InnerException != null ? ex.InnerException : ex;
                throw new UsbException(UsbCode.Io, "couldn't connect to " + host + ":" + port + ": " + inner.Message);
            }
        }

        private static void DisposeQuietly(Socket? socket)
        {
            if (socket == null)
            {
                return;
            }
            try
            {
                socket.Dispose();
            }
            catch (Exception)
            {
            }
        }

        public int Read(byte[] buffer, int timeoutMs)
        {
            if (Volatile.Read(ref _closed) != 0)
            {
                throw new UsbException(UsbCode.NoDevice, "closed");
            }
            try
            {
                if (!_socket.Poll(Math.Max(1, timeoutMs) * 1000, SelectMode.SelectRead))
                {
                    return 0;
                }
                int n = _socket.Receive(buffer);
                if (n <= 0)
                {
                    throw new UsbException(UsbCode.NoDevice, "the phone closed the connection");
                }
                return n;
            }
            catch (UsbException)
            {
                throw;
            }
            catch (Exception ex)
            {
                throw new UsbException(UsbCode.NoDevice, ex.Message);
            }
        }

        public int Write(byte[] data, int offset, int count, int timeoutMs)
        {
            if (Volatile.Read(ref _closed) != 0)
            {
                throw new UsbException(UsbCode.NoDevice, "closed");
            }
            // No packet boundaries on TCP: drop the extra newline UsbFraming.Frame adds after a 64-byte-multiple
            // message (a JSON message never contains a raw newline, so a trailing blank line is that padding).
            int toSend = count;
            if (count >= 2 && data[offset + count - 1] == 0x0A && data[offset + count - 2] == 0x0A)
            {
                toSend = count - 1;
            }
            try
            {
                _socket.SendTimeout = Math.Max(1, timeoutMs);
                int sent = 0;
                while (sent < toSend)
                {
                    int n = _socket.Send(data, offset + sent, toSend - sent, SocketFlags.None);
                    if (n <= 0)
                    {
                        return 0;
                    }
                    sent += n;
                }
                return count;
            }
            catch (SocketException ex)
            {
                if (ex.SocketErrorCode == SocketError.TimedOut || ex.SocketErrorCode == SocketError.WouldBlock)
                {
                    return 0;
                }
                throw new UsbException(UsbCode.NoDevice, ex.Message);
            }
            catch (Exception ex)
            {
                throw new UsbException(UsbCode.NoDevice, ex.Message);
            }
        }

        public void Close()
        {
            if (Interlocked.Exchange(ref _closed, 1) != 0)
            {
                return;
            }
            try
            {
                _socket.Shutdown(SocketShutdown.Both);
            }
            catch (Exception)
            {
                // Already closed by the other side.
            }
            DisposeQuietly(_socket);
        }
    }
}
