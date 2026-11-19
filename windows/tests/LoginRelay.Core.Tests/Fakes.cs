// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using LoginRelay.Core;

namespace LoginRelay.Core.Tests
{
    /// <summary>A stand-in UI thread: a queue drained by one thread.</summary>
    internal sealed class TestDispatcher : IUiDispatcher, IDisposable
    {
        private readonly BlockingCollection<Action> _queue = new BlockingCollection<Action>();
        private readonly Thread _thread;
        private Exception? _lastError;

        public TestDispatcher()
        {
            _thread = new Thread(Run);
            _thread.IsBackground = true;
            _thread.Name = "test-ui";
            _thread.Start();
        }

        /// <summary>The first exception an action threw, if any (the tests assert it stayed null).</summary>
        public Exception? LastError
        {
            get { return Volatile.Read(ref _lastError); }
        }

        private void Run()
        {
            foreach (Action action in _queue.GetConsumingEnumerable())
            {
                try
                {
                    action();
                }
                catch (Exception ex)
                {
                    Interlocked.CompareExchange(ref _lastError, ex, null);
                }
            }
        }

        public void Post(Action action)
        {
            try
            {
                _queue.Add(action);
            }
            catch (InvalidOperationException)
            {
                // Completed: the "UI" is gone.
            }
        }

        /// <summary>Runs a function on the UI thread and waits for its result.</summary>
        public T Invoke<T>(Func<T> function)
        {
            T result = default(T)!;
            Exception? error = null;
            var done = new ManualResetEventSlim(false);
            Post(() =>
            {
                try
                {
                    result = function();
                }
                catch (Exception ex)
                {
                    error = ex;
                }
                done.Set();
            });
            if (!done.Wait(15000))
            {
                throw new TimeoutException("the UI thread did not run the action");
            }
            if (error != null)
            {
                throw new InvalidOperationException("UI action failed: " + error.Message, error);
            }
            return result;
        }

        public void Invoke(Action action)
        {
            Invoke<int>(() =>
            {
                action();
                return 0;
            });
        }

        public void Dispose()
        {
            _queue.CompleteAdding();
            _thread.Join(3000);
        }
    }

    internal sealed class FakeClaim : IClaim
    {
        public string Scheme { get; }
        private int _released;

        public FakeClaim(string scheme)
        {
            Scheme = scheme;
        }

        public bool Released
        {
            get { return Volatile.Read(ref _released) != 0; }
        }

        public void Release()
        {
            Interlocked.Exchange(ref _released, 1);
        }
    }

    internal sealed class FakePrompt : IPromptHandle
    {
        public Func<string, string?> Check { get; }
        public Action OnCancel { get; }
        public bool Closed { get; private set; }

        public FakePrompt(Func<string, string?> check, Action onCancel)
        {
            Check = check;
            OnCancel = onCancel;
        }

        public void Close()
        {
            Closed = true;
        }
    }

    internal sealed class FakePlatform : IPlatform
    {
        private readonly object _lock = new object();
        private readonly List<string> _opened = new List<string>();
        private readonly List<FakeClaim> _claims = new List<FakeClaim>();

        public bool AllowClaims { get; set; } = true;
        public bool PkceAnswer { get; set; } = true;
        public FakePrompt? Prompt { get; private set; }

        public string[] Opened
        {
            get { lock (_lock) { return _opened.ToArray(); } }
        }

        public FakeClaim[] Claims
        {
            get { lock (_lock) { return _claims.ToArray(); } }
        }

        public bool OpenUrl(string url)
        {
            lock (_lock)
            {
                _opened.Add(url);
            }
            return true;
        }

        public IClaim? Claim(string scheme)
        {
            if (!AllowClaims)
            {
                return null;
            }
            var claim = new FakeClaim(scheme);
            lock (_lock)
            {
                _claims.Add(claim);
            }
            return claim;
        }

        public void Notify(string title, string body)
        {
        }

        public void ShowError(string title, string detail)
        {
        }

        public bool ConfirmWithoutPkce(string appName)
        {
            return PkceAnswer;
        }

        public IPromptHandle AskForPastedAddress(string startsWith, Func<string, string?> check, Action onCancel)
        {
            Prompt = new FakePrompt(check, onCancel);
            return Prompt;
        }

        public string? BundledApkPath
        {
            get { return null; }
        }

        public string? BundledPhoneVersion
        {
            get { return null; }
        }
    }

    internal sealed class Delivery
    {
        public string Serial { get; }
        public string Url { get; }
        public string? Package { get; }

        public Delivery(string serial, string url, string? package)
        {
            Serial = serial;
            Url = url;
            Package = package;
        }
    }

    /// <summary>One phone, always connected and ready, listening on a fixed local port.</summary>
    internal sealed class FakeAdb : IAdb
    {
        private readonly int _port;
        private readonly object _lock = new object();
        private readonly List<Delivery> _delivered = new List<Delivery>();

        public FakeAdb(int port)
        {
            _port = port;
        }

        public Delivery[] Delivered
        {
            get { lock (_lock) { return _delivered.ToArray(); } }
        }

        public string ExePath
        {
            get { return "fake-adb"; }
        }

        public IReadOnlyList<Device> Devices()
        {
            return new List<Device> { new Device("FAKE0001", "device", "Fake_Phone") };
        }

        public string? InstalledVersion(string serial, string package)
        {
            return "0.3.0";
        }

        public int? ExistingForward(string serial)
        {
            return _port;
        }

        public int? Forward(string serial)
        {
            return _port;
        }

        public void RemoveForward(int port, string serial)
        {
        }

        public ProcessResult Deliver(string serial, string url, string? package)
        {
            lock (_lock)
            {
                _delivered.Add(new Delivery(serial, url, package));
            }
            return new ProcessResult(0, "Starting: Intent { ... }", "");
        }

        public ProcessResult Install(string serial, string apk)
        {
            return new ProcessResult(0, "Success", "");
        }

        public ProcessResult Shell(string serial, string command, double timeoutSeconds = 20)
        {
            return new ProcessResult(0, "", "");
        }
    }

    /// <summary>
    /// The Login Relay app on the phone: accepts connections on a local port, sends its pending page to
    /// each one until it hears "done", and records what it receives.
    /// </summary>
    internal sealed class FakePhone : IDisposable
    {
        private readonly TcpListener _listener;
        private readonly string? _openJson;
        private readonly object _writeLock = new object();
        private volatile bool _done;
        private volatile bool _stop;

        public int Port { get; }
        public int CancelAfterMilliseconds { get; set; }
        public ConcurrentQueue<string> Received { get; } = new ConcurrentQueue<string>();

        public FakePhone(string? pageUrl, string? referrer = "android-app://com.example.app", string label = "Example App")
        {
            if (pageUrl != null)
            {
                string urlJson = System.Text.Json.JsonSerializer.Serialize(pageUrl);
                string referrerJson = System.Text.Json.JsonSerializer.Serialize(referrer);
                string labelJson = System.Text.Json.JsonSerializer.Serialize(label);
                _openJson = "{\"v\":1,\"type\":\"open\",\"url\":" + urlJson + ",\"referrer\":" + referrerJson + ",\"label\":" + labelJson + "}";
            }
            _listener = new TcpListener(IPAddress.Loopback, 0);
            _listener.Start();
            Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
            var thread = new Thread(AcceptLoop);
            thread.IsBackground = true;
            thread.Start();
        }

        public bool Got(string needle)
        {
            return Received.Any(line => line.Contains(needle, StringComparison.Ordinal));
        }

        private void AcceptLoop()
        {
            while (!_stop)
            {
                TcpClient client;
                try
                {
                    client = _listener.AcceptTcpClient();
                }
                catch (Exception)
                {
                    return;
                }
                var thread = new Thread(() => Serve(client));
                thread.IsBackground = true;
                thread.Start();
            }
        }

        private void Write(NetworkStream stream, string line)
        {
            byte[] data = Encoding.UTF8.GetBytes(line + "\n");
            lock (_writeLock)
            {
                try
                {
                    stream.Write(data, 0, data.Length);
                    stream.Flush();
                }
                catch (Exception)
                {
                }
            }
        }

        private void Serve(TcpClient client)
        {
            using (client)
            {
                try
                {
                    NetworkStream stream = client.GetStream();
                    if (_openJson != null && !_done)
                    {
                        Write(stream, _openJson);
                        if (CancelAfterMilliseconds > 0)
                        {
                            int delay = CancelAfterMilliseconds;
                            var canceller = new Thread(() =>
                            {
                                Thread.Sleep(delay);
                                Write(stream, "{\"type\":\"cancel\"}");
                            });
                            canceller.IsBackground = true;
                            canceller.Start();
                        }
                    }
                    using (var reader = new StreamReader(stream, Encoding.UTF8))
                    {
                        string? line;
                        while ((line = reader.ReadLine()) != null)
                        {
                            Received.Enqueue(line);
                            if (line.Contains("\"type\":\"done\"", StringComparison.Ordinal))
                            {
                                _done = true;
                            }
                        }
                    }
                }
                catch (Exception)
                {
                    // The engine hung up.
                }
            }
        }

        public void Dispose()
        {
            _stop = true;
            try
            {
                _listener.Stop();
            }
            catch (Exception)
            {
            }
        }
    }
}
