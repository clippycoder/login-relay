// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;

namespace LoginRelay.Core
{
    /// <summary>
    /// The adb server holds a phone's USB interface exclusively, so while it runs libusb gets "busy". Login Relay
    /// doesn't use adb, but if one happens to be installed this stops its server (it restarts by itself when
    /// something asks for it). Best effort; if there is no adb, nothing happens and nothing is said.
    /// </summary>
    public static class AdbServer
    {
        private static readonly string ExeName = OperatingSystem.IsWindows() ? "adb.exe" : "adb";

        /// <summary>The first adb found in PATH, then in the Android SDK's usual per-user folder. Null if none.</summary>
        public static string? Find()
        {
            var candidates = new List<string>();
            string? pathVar = Environment.GetEnvironmentVariable("PATH");
            if (!string.IsNullOrEmpty(pathVar))
            {
                foreach (string rawDir in pathVar.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
                {
                    string dir = rawDir.Trim().Trim('"');
                    if (dir.Length > 0)
                    {
                        candidates.Add(SafeCombine(dir, ExeName));
                    }
                }
            }
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (local.Length > 0)
            {
                candidates.Add(SafeCombine(local, "Android", "Sdk", "platform-tools", ExeName));
            }
            foreach (string candidate in candidates)
            {
                if (candidate.Length > 0 && SafeFileExists(candidate))
                {
                    return candidate;
                }
            }
            return null;
        }

        /// <summary>
        /// Runs "adb kill-server". Returns false if there is no adb or it couldn't be started. The output is never
        /// read: adb's server is a daemon that inherits any pipe we gave it, so waiting for the end of its output
        /// would never finish.
        /// </summary>
        public static bool Stop()
        {
            string? path = Find();
            if (path == null)
            {
                return false;
            }
            try
            {
                var psi = new ProcessStartInfo(path);
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.ArgumentList.Add("kill-server");
                using (Process? proc = Process.Start(psi))
                {
                    if (proc == null)
                    {
                        return false;
                    }
                    if (!proc.WaitForExit(10000))
                    {
                        try
                        {
                            proc.Kill(true);
                        }
                        catch (Exception)
                        {
                        }
                    }
                }
                return true;
            }
            catch (Exception)
            {
                return false;
            }
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
    }
}
