// SPDX-License-Identifier: GPL-3.0-or-later
//
// Login Relay for Windows: a tray app that finishes browser sign-ins for an Android phone with no browser.
//
// Command line:
//   --background     start hidden (the Run-at-login entry uses this)
//   --url <address>  a return address from the browser. If Login Relay is already running, the address is
//                    handed to it and this process exits; Windows starts one such process per URL.
//   --show           bring the activity window up (same as starting the program a second time by hand)
//   --verbose        also write every activity-log line to standard error
//
// Environment hooks (used by tests/e2e/run_e2e.py; keep them):
//   LOGINRELAY_ADB=<path>      use exactly this adb (may be a .cmd wrapper)
//   LOGINRELAY_BROWSER=none    don't open the sign-in page in a browser
//   LOGINRELAY_LOG=<file>      append every activity-log line to this file
//   LOGINRELAY_TRANSPORT=adb|usb   pin the connection type, overriding the saved choice
using System;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace LoginRelay.App
{
    internal sealed class Options
    {
        public bool Background { get; private set; }
        public bool Verbose { get; private set; }
        public bool Show { get; private set; }
        public string? Url { get; private set; }

        public static Options Parse(string[] args)
        {
            var options = new Options();
            for (int i = 0; i < args.Length; i++)
            {
                string arg = args[i];
                if (arg == "--background")
                {
                    options.Background = true;
                }
                else if (arg == "--verbose")
                {
                    options.Verbose = true;
                }
                else if (arg == "--show")
                {
                    options.Show = true;
                }
                else if (arg == "--url" && i + 1 < args.Length)
                {
                    i++;
                    options.Url = args[i];
                }
                else if (arg.StartsWith("--url=", StringComparison.Ordinal))
                {
                    options.Url = arg.Substring("--url=".Length);
                }
            }
            return options;
        }
    }

    /// <summary>One running copy per user: a named mutex decides who is first, a named pipe carries messages to it.</summary>
    internal static class SingleInstance
    {
        public const string MutexName = @"Local\LoginRelay.Instance";

        public static string PipeName
        {
            get { return "LoginRelay." + Regex.Replace(Environment.UserName, "[^A-Za-z0-9._-]", "_"); }
        }

        /// <summary>Sends a message to the running copy, waiting up to 10 s for it to be listening.</summary>
        public static bool Send(string message)
        {
            try
            {
                using (var client = new NamedPipeClientStream(".", PipeName, PipeDirection.Out, PipeOptions.CurrentUserOnly))
                {
                    client.Connect(10000);
                    byte[] data = Encoding.UTF8.GetBytes(message);
                    client.Write(data, 0, data.Length);
                    client.Flush();
                }
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }
    }

    /// <summary>The listening end: runs on pool threads and reports each message it receives.</summary>
    internal sealed class PipeServer : IDisposable
    {
        private readonly Action<string> _onMessage;
        private readonly CancellationTokenSource _cts = new CancellationTokenSource();

        public PipeServer(Action<string> onMessage)
        {
            _onMessage = onMessage;
        }

        public void Start()
        {
            CancellationToken token = _cts.Token;
            Task.Run(() => RunAsync(token));
        }

        public void Dispose()
        {
            try
            {
                _cts.Cancel();
            }
            catch (Exception)
            {
            }
        }

        private async Task RunAsync(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    using (var server = new NamedPipeServerStream(
                        SingleInstance.PipeName,
                        PipeDirection.In,
                        1,
                        PipeTransmissionMode.Byte,
                        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly))
                    {
                        await server.WaitForConnectionAsync(token).ConfigureAwait(false);
                        using (CancellationTokenSource limit = CancellationTokenSource.CreateLinkedTokenSource(token))
                        {
                            // A client that connects and then says nothing mustn't wedge the listener.
                            limit.CancelAfter(5000);
                            string message = await ReadAllAsync(server, limit.Token).ConfigureAwait(false);
                            if (message.Length > 0)
                            {
                                _onMessage(message);
                            }
                        }
                    }
                }
                catch (OperationCanceledException)
                {
                    if (token.IsCancellationRequested)
                    {
                        return;
                    }
                }
                catch (Exception)
                {
                    try
                    {
                        await Task.Delay(500, token).ConfigureAwait(false);
                    }
                    catch (OperationCanceledException)
                    {
                        return;
                    }
                }
            }
        }

        private static async Task<string> ReadAllAsync(Stream stream, CancellationToken token)
        {
            var buffer = new byte[4096];
            using (var all = new MemoryStream())
            {
                while (all.Length < 32768)
                {
                    int n = await stream.ReadAsync(buffer, 0, buffer.Length, token).ConfigureAwait(false);
                    if (n <= 0)
                    {
                        break;
                    }
                    all.Write(buffer, 0, n);
                }
                return Encoding.UTF8.GetString(all.ToArray());
            }
        }
    }

    internal static class Program
    {
        private static readonly object LogLock = new object();
        private static string? _logFile;
        private static bool _verbose;

        /// <summary>Used as the engine's log sink: LOGINRELAY_LOG file and, with --verbose, standard error.</summary>
        internal static void WriteLog(string line)
        {
            if (_verbose)
            {
                try
                {
                    Console.Error.WriteLine(line);
                }
                catch (Exception)
                {
                }
            }
            string? file = _logFile;
            if (string.IsNullOrEmpty(file))
            {
                return;
            }
            try
            {
                lock (LogLock)
                {
                    File.AppendAllText(file, line + Environment.NewLine, Encoding.UTF8);
                }
            }
            catch (Exception)
            {
            }
        }

        private static void WriteCrash(string text)
        {
            WriteLog("[crash] " + text);
            try
            {
                string dir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "Login Relay");
                Directory.CreateDirectory(dir);
                File.AppendAllText(
                    Path.Combine(dir, "crash.log"),
                    DateTime.Now.ToString("s") + " " + text + Environment.NewLine,
                    Encoding.UTF8);
            }
            catch (Exception)
            {
            }
        }

        [STAThread]
        private static int Main(string[] args)
        {
            Options options = Options.Parse(args);
            _verbose = options.Verbose;
            _logFile = Environment.GetEnvironmentVariable("LOGINRELAY_LOG");
            AppDomain.CurrentDomain.UnhandledException += (sender, e) => WriteCrash(e.ExceptionObject?.ToString() ?? "unknown error");

            bool createdNew;
            using (var mutex = new Mutex(true, SingleInstance.MutexName, out createdNew))
            {
                if (!createdNew)
                {
                    // Another copy is running. Hand it what we were asked to do, then get out of the way.
                    if (options.Url != null)
                    {
                        SingleInstance.Send("URL\t" + options.Url);
                    }
                    else if (!options.Background || options.Show)
                    {
                        SingleInstance.Send("SHOW");
                    }
                    return 0;
                }

                Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
                Application.ThreadException += (sender, e) => WriteCrash(e.Exception.ToString());
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.SetHighDpiMode(HighDpiMode.SystemAware);
                Application.Run(new TrayApp(options));
                GC.KeepAlive(mutex);
                return 0;
            }
        }
    }
}
