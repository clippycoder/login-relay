// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.IO;
using System.IO.Compression;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text.RegularExpressions;

namespace LoginRelay.Core
{
    /// <summary>
    /// Downloads Google's Android platform-tools (which contains adb) for people who don't have it. It
    /// comes straight from dl.google.com and is checked against the SHA-1 in Google's own repository
    /// manifest. Nothing is redistributed by this project.
    /// </summary>
    public static class AdbInstaller
    {
        public const string ManifestUrl = "https://dl.google.com/android/repository/repository2-3.xml";
        public const string ArchiveBase = "https://dl.google.com/android/repository/";
        public const string LicenseUrl = "https://developer.android.com/studio/terms";

        public sealed class InstallException : Exception
        {
            public InstallException(string message) : base(message)
            {
            }
        }

        public static string InstallDir
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "Login Relay");
            }
        }

        public static string InstalledPath
        {
            get { return Path.Combine(InstallDir, "platform-tools", Adb.ExeName); }
        }

        /// <summary>Blocking; call off the UI thread.</summary>
        public static void Install(Action<string> progress)
        {
            progress("Reading Google's download list…");
            string manifest = System.Text.Encoding.UTF8.GetString(Fetch(ManifestUrl, 60));
            (string sha1, string file) = FindArchive(manifest);

            progress("Downloading platform-tools…");
            byte[] zip = Fetch(ArchiveBase + file, 300);
            string digest = Convert.ToHexString(SHA1.HashData(zip)).ToLowerInvariant();
            if (digest != sha1)
            {
                throw new InstallException("The download didn't match Google's checksum, so it was discarded.");
            }

            progress("Unpacking…");
            // Stage next to the destination so the final move stays on one volume.
            Directory.CreateDirectory(InstallDir);
            string staging = Path.Combine(InstallDir, ".staging-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(staging);
            try
            {
                string zipFile = Path.Combine(staging, "pt.zip");
                File.WriteAllBytes(zipFile, zip);
                string extracted = Path.Combine(staging, "x");
                ZipFile.ExtractToDirectory(zipFile, extracted);

                string fresh = Path.Combine(extracted, "platform-tools");
                if (!File.Exists(Path.Combine(fresh, Adb.ExeName)))
                {
                    throw new InstallException("The download didn't contain adb.");
                }
                string target = Path.Combine(InstallDir, "platform-tools");
                if (Directory.Exists(target))
                {
                    try
                    {
                        Directory.Delete(target, true);
                    }
                    catch (IOException)
                    {
                        throw new InstallException("Couldn't replace the old copy of platform-tools; it may still be running.");
                    }
                }
                Directory.Move(fresh, target);
            }
            finally
            {
                try
                {
                    Directory.Delete(staging, true);
                }
                catch (Exception)
                {
                }
            }
            progress("Installed.");
        }

        /// <summary>
        /// Pulls the Windows archive's checksum and file name out of the platform-tools entry. The file name
        /// has to contain "win" but not "darwin" (which contains "win" too).
        /// </summary>
        public static (string Sha1, string File) FindArchive(string xml)
        {
            return FindArchive(xml, IsWindowsArchive);
        }

        public static bool IsWindowsArchive(string fileName)
        {
            string lower = fileName.ToLowerInvariant();
            return lower.Contains("win") && !lower.Contains("darwin");
        }

        public static (string Sha1, string File) FindArchive(string xml, Func<string, bool> wanted)
        {
            const string open = "<remotePackage path=\"platform-tools\"";
            const string close = "</remotePackage>";
            bool sawPackage = false;
            int from = 0;
            while (true)
            {
                int start = xml.IndexOf(open, from, StringComparison.Ordinal);
                if (start < 0)
                {
                    break;
                }
                sawPackage = true;
                int end = xml.IndexOf(close, start, StringComparison.Ordinal);
                if (end < 0)
                {
                    end = xml.Length;
                }
                string block = xml.Substring(start, end - start);
                var re = new Regex("<checksum type=\"sha1\">([0-9a-f]{40})</checksum>\\s*<url>([^<]*)</url>");
                foreach (Match m in re.Matches(block))
                {
                    string file = m.Groups[2].Value.Trim();
                    if (wanted(file) && Regex.IsMatch(file, "^[A-Za-z0-9._-]+$"))
                    {
                        return (m.Groups[1].Value, file);
                    }
                }
                from = end;
            }
            if (!sawPackage)
            {
                throw new InstallException("Google's download list has no platform-tools entry.");
            }
            throw new InstallException("Google's download list has no Windows platform-tools.");
        }

        private static byte[] Fetch(string url, int timeoutSeconds)
        {
            using (var client = new HttpClient())
            {
                client.Timeout = TimeSpan.FromSeconds(timeoutSeconds);
                using (HttpResponseMessage response = client.GetAsync(url).GetAwaiter().GetResult())
                {
                    if (!response.IsSuccessStatusCode)
                    {
                        string host = new Uri(url).Host;
                        throw new InstallException(host + " answered " + ((int)response.StatusCode).ToString() + ".");
                    }
                    return response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult();
                }
            }
        }
    }
}
