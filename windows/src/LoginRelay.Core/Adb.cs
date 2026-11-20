// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

namespace LoginRelay.Core
{
    public sealed record Device(string Serial, string State, string Model)
    {
        /// <summary>"device", "unauthorized", "offline", ...</summary>
        public bool IsUsable
        {
            get { return State == "device"; }
        }

        public string Title
        {
            get { return Model.Length == 0 ? Serial : Model.Replace('_', ' '); }
        }

        /// <summary>A phone found over USB accessory mode rather than adb (its serial is "usb:" + port location).</summary>
        public bool IsUsb
        {
            get { return Serial.StartsWith("usb:", StringComparison.Ordinal); }
        }
    }

    public sealed record ProcessResult(int Status, string Out, string Err)
    {
        public bool Ok
        {
            get { return Status == 0; }
        }
    }

    /// <summary>What the engine needs from adb. The real one runs adb.exe; tests use a fake.</summary>
    public interface IAdb
    {
        string ExePath { get; }
        IReadOnlyList<Device> Devices();
        string? InstalledVersion(string serial, string package);
        int? ExistingForward(string serial);
        int? Forward(string serial);
        void RemoveForward(int port, string serial);
        ProcessResult Deliver(string serial, string url, string? package);
        ProcessResult Install(string serial, string apk);
        ProcessResult Shell(string serial, string command, double timeoutSeconds = 20);
    }

    /// <summary>Thin wrapper over the adb command-line tool. Everything here blocks; call it off the UI thread.</summary>
    public sealed class Adb : IAdb
    {
        public static readonly string ExeName = OperatingSystem.IsWindows() ? "adb.exe" : "adb";

        public string ExePath { get; }

        public Adb(string exePath)
        {
            ExePath = exePath;
        }

        // ---- finding adb -------------------------------------------------------------------------

        /// <summary>
        /// First match wins: LOGINRELAY_ADB (test hook, returned as is); the path the user picked; the copy
        /// this app downloaded; ANDROID_HOME / ANDROID_SDK_ROOT; the usual locations; then PATH.
        /// </summary>
        public static string? Locate(string? userChoice, string? installedPath)
        {
            string? forced = Environment.GetEnvironmentVariable("LOGINRELAY_ADB");
            if (!string.IsNullOrEmpty(forced))
            {
                return forced;
            }

            var candidates = new List<string>();
            if (!string.IsNullOrEmpty(userChoice))
            {
                candidates.Add(userChoice);
            }
            if (!string.IsNullOrEmpty(installedPath))
            {
                candidates.Add(installedPath);
            }
            foreach (string key in new[] { "ANDROID_HOME", "ANDROID_SDK_ROOT" })
            {
                string? sdk = Environment.GetEnvironmentVariable(key);
                if (!string.IsNullOrEmpty(sdk))
                {
                    candidates.Add(Path.Combine(sdk, "platform-tools", ExeName));
                }
            }
            candidates.AddRange(CommonLocations());

            foreach (string candidate in candidates)
            {
                if (SafeFileExists(candidate))
                {
                    return candidate;
                }
            }

            string? pathVar = Environment.GetEnvironmentVariable("PATH");
            if (!string.IsNullOrEmpty(pathVar))
            {
                foreach (string rawDir in pathVar.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
                {
                    string dir = rawDir.Trim().Trim('"');
                    if (dir.Length == 0)
                    {
                        continue;
                    }
                    string candidate = SafeCombine(dir, ExeName);
                    if (candidate.Length > 0 && SafeFileExists(candidate))
                    {
                        return candidate;
                    }
                }
            }
            return null;
        }

        private static List<string> CommonLocations()
        {
            var list = new List<string>();
            string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (OperatingSystem.IsWindows())
            {
                string programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
                string programFilesX86 = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);
                string? choco = Environment.GetEnvironmentVariable("ChocolateyInstall");
                list.Add(SafeCombine(local, "Android", "Sdk", "platform-tools", ExeName));
                list.Add(SafeCombine(home, "scoop", "shims", ExeName));
                list.Add(SafeCombine(local, "Microsoft", "WinGet", "Links", ExeName));
                list.Add(SafeCombine(programFiles, "Android", "platform-tools", ExeName));
                list.Add(SafeCombine(programFilesX86, "Android", "android-sdk", "platform-tools", ExeName));
                if (!string.IsNullOrEmpty(choco))
                {
                    list.Add(SafeCombine(choco, "bin", ExeName));
                }
                list.Add(@"C:\ProgramData\chocolatey\bin\adb.exe");
            }
            else
            {
                list.Add(SafeCombine(home, "Library", "Android", "sdk", "platform-tools", ExeName));
                list.Add(SafeCombine(home, "Android", "Sdk", "platform-tools", ExeName));
                list.Add("/opt/homebrew/bin/adb");
                list.Add("/usr/local/bin/adb");
                list.Add("/usr/bin/adb");
            }
            list.RemoveAll(s => s.Length == 0);
            return list;
        }

        private static string SafeCombine(params string[] parts)
        {
            try
            {
                return Path.Combine(parts);
            }
            catch (ArgumentException)
            {
                return "";
            }
        }

        private static bool SafeFileExists(string path)
        {
            try
            {
                return File.Exists(path);
            }
            catch (Exception)
            {
                return false;
            }
        }

        // ---- running -----------------------------------------------------------------------------

        public ProcessResult Run(IEnumerable<string> args, string? serial = null, double timeoutSeconds = 20)
        {
            var all = new List<string>();
            if (serial != null)
            {
                all.Add("-s");
                all.Add(serial);
            }
            all.AddRange(args);
            return Exec(ExePath, all, timeoutSeconds);
        }

        /// <summary>
        /// Runs a program and collects its output, never waiting for end-of-file on the pipes.
        ///
        /// When adb has to start its server it forks a daemon that inherits our output handles, so "read
        /// until EOF" blocks until that daemon exits, i.e. forever. So: wait for the process (with a
        /// timeout, killing its whole tree if it overstays), then give the readers at most about half a
        /// second to finish. Every process is disposed.
        ///
        /// The output is read by two dedicated threads rather than with Process.BeginOutputReadLine: the
        /// async events run on the thread pool, and when the pool is busy (a loaded machine, many tests
        /// at once) they can fire late enough for the bounded drain to expire and return empty output.
        /// </summary>
        public static ProcessResult Exec(string path, IEnumerable<string> args, double timeoutSeconds = 20)
        {
            var psi = new ProcessStartInfo(path)
            {
                UseShellExecute = false,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            foreach (string arg in args)
            {
                psi.ArgumentList.Add(arg);
            }

            var outText = new StringBuilder();
            var errText = new StringBuilder();
            var gate = new object();
            bool accepting = true;

            // Copies a stream into a StringBuilder until it ends. Daemon-style: if we give up on it, it
            // simply stops appending (accepting == false) and dies with the process or the disposed stream.
            Thread Pump(StreamReader reader, StringBuilder sink, string name)
            {
                var t = new Thread(() =>
                {
                    var buffer = new char[4096];
                    try
                    {
                        int n;
                        while ((n = reader.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            lock (gate)
                            {
                                if (!accepting)
                                {
                                    return;
                                }
                                sink.Append(buffer, 0, n);
                            }
                        }
                    }
                    catch (Exception)
                    {
                        // The stream was closed under us (disposed after we stopped waiting).
                    }
                })
                {
                    IsBackground = true,
                    Name = name,
                };
                t.Start();
                return t;
            }

            Process? proc = null;
            try
            {
                proc = new Process();
                proc.StartInfo = psi;
                proc.Start();
                try
                {
                    proc.StandardInput.Close();
                }
                catch (IOException)
                {
                }
                Thread outThread = Pump(proc.StandardOutput, outText, "adb-stdout");
                Thread errThread = Pump(proc.StandardError, errText, "adb-stderr");

                int waitMs = (int)Math.Min(int.MaxValue - 1, Math.Max(1, timeoutSeconds * 1000));
                bool exited = proc.WaitForExit(waitMs);
                bool timedOut = !exited;
                if (timedOut)
                {
                    try
                    {
                        proc.Kill(true);
                    }
                    catch (Exception)
                    {
                    }
                    proc.WaitForExit(2000);
                }

                // The process is gone (or killed). Give the readers at most ~500 ms to reach the end.
                var clock = Stopwatch.StartNew();
                outThread.Join(500);
                errThread.Join((int)Math.Max(0, 500 - clock.ElapsedMilliseconds));

                string o;
                string e;
                lock (gate)
                {
                    accepting = false;
                    o = outText.ToString();
                    e = errText.ToString();
                }
                if (timedOut)
                {
                    string note = "timed out after " + ((int)timeoutSeconds).ToString() + " s";
                    return new ProcessResult(-1, o.Trim(), (e.Trim() + " " + note).Trim());
                }
                int code = proc.HasExited ? proc.ExitCode : -1;
                return new ProcessResult(code, o.Trim(), e.Trim());
            }
            catch (Exception ex)
            {
                return new ProcessResult(-1, "", ex.Message);
            }
            finally
            {
                if (proc != null)
                {
                    try
                    {
                        proc.Dispose();
                    }
                    catch (Exception)
                    {
                    }
                }
            }
        }

        // ---- phone queries -----------------------------------------------------------------------

        public IReadOnlyList<Device> Devices()
        {
            ProcessResult r = Run(new[] { "devices", "-l" }, null, 15);
            if (!r.Ok)
            {
                return new List<Device>();
            }
            return ParseDevices(r.Out);
        }

        /// <summary>Parses `adb devices -l`: serial, state, then "model:NAME" among the key:value fields.</summary>
        public static List<Device> ParseDevices(string output)
        {
            var list = new List<Device>();
            foreach (string raw in output.Split('\n'))
            {
                string line = raw.TrimEnd('\r');
                if (line.StartsWith("*", StringComparison.Ordinal) || line.StartsWith("List of devices", StringComparison.Ordinal))
                {
                    continue;
                }
                string[] parts = line.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
                if (parts.Length < 2)
                {
                    continue;
                }
                string model = "";
                foreach (string part in parts)
                {
                    if (part.StartsWith("model:", StringComparison.Ordinal))
                    {
                        model = part.Substring("model:".Length);
                        break;
                    }
                }
                list.Add(new Device(parts[0], parts[1], model));
            }
            return list;
        }

        public ProcessResult Shell(string serial, string command, double timeoutSeconds = 20)
        {
            return Run(new[] { "shell", command }, serial, timeoutSeconds);
        }

        /// <summary>Version name of the phone-side app, or null if it isn't installed.</summary>
        public string? InstalledVersion(string serial, string package)
        {
            ProcessResult r = Shell(serial, "dumpsys package " + package);
            if (!r.Ok || !r.Out.Contains("versionName=", StringComparison.Ordinal))
            {
                return null;
            }
            foreach (string line in r.Out.Split('\n'))
            {
                string t = line.Trim();
                if (t.StartsWith("versionName=", StringComparison.Ordinal))
                {
                    return t.Substring("versionName=".Length).Trim();
                }
            }
            return null;
        }

        /// <summary>Reuses an existing forward for this phone rather than adding a new one every few seconds.</summary>
        public int? ExistingForward(string serial)
        {
            ProcessResult r = Run(new[] { "forward", "--list" });
            if (!r.Ok)
            {
                return null;
            }
            foreach (string line in r.Out.Split('\n'))
            {
                string[] p = line.Trim().Split(' ', StringSplitOptions.RemoveEmptyEntries);
                if (p.Length == 3 && p[0] == serial && p[2] == "localabstract:oauthrelay" && p[1].StartsWith("tcp:", StringComparison.Ordinal))
                {
                    if (int.TryParse(p[1].Substring(4), out int port) && port > 0 && port <= 65535)
                    {
                        return port;
                    }
                }
            }
            return null;
        }

        /// <summary>Sets up the tunnel to the phone app's abstract socket and returns the local TCP port.</summary>
        public int? Forward(string serial)
        {
            ProcessResult r = Run(new[] { "forward", "tcp:0", "localabstract:oauthrelay" }, serial);
            if (r.Ok && int.TryParse(r.Out.Trim(), out int port) && port > 0 && port <= 65535)
            {
                return port;
            }
            return null;
        }

        public void RemoveForward(int port, string serial)
        {
            Run(new[] { "forward", "--remove", "tcp:" + port.ToString() }, serial, 5);
        }

        /// <summary>Hands a return address to the app that asked for the page.</summary>
        public ProcessResult Deliver(string serial, string url, string? package)
        {
            string cmd = "am start -a android.intent.action.VIEW -d " + Redirect.ShellQuote(url);
            if (package != null)
            {
                cmd += " -p " + Redirect.ShellQuote(package);
            }
            return Shell(serial, cmd);
        }

        public ProcessResult Install(string serial, string apk)
        {
            return Run(new[] { "install", "-r", apk }, serial, 120);
        }
    }
}
