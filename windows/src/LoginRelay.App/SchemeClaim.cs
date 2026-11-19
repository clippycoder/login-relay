// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using LoginRelay.Core;
using Microsoft.Win32;

namespace LoginRelay.App
{
    /// <summary>One registry value, in a form that survives a round trip through JSON.</summary>
    public sealed class RegValueData
    {
        public string Name { get; set; } = "";
        public string Kind { get; set; } = "String";
        public string? Text { get; set; }
        public long Number { get; set; }
        public string? Base64 { get; set; }
        public string[]? Multi { get; set; }
    }

    /// <summary>A registry key and everything under it.</summary>
    public sealed class RegTree
    {
        public List<RegValueData> Values { get; set; } = new List<RegValueData>();
        public Dictionary<string, RegTree> Subkeys { get; set; } = new Dictionary<string, RegTree>();
    }

    /// <summary>What a claim replaced: the old key tree, or nothing.</summary>
    public sealed class ClaimEntry
    {
        public bool Existed { get; set; }
        public RegTree? Tree { get; set; }
    }

    public sealed class ClaimsFile
    {
        public Dictionary<string, ClaimEntry> Claims { get; set; } = new Dictionary<string, ClaimEntry>();
    }

    /// <summary>
    /// Makes this app the Windows handler for one URL scheme (an app's sign-in return address) for the
    /// length of one login, then puts the previous handler back.
    ///
    /// It is a key under HKCU\Software\Classes\&lt;scheme&gt; (no admin needed) whose command is
    /// <c>"exe" --url "%1"</c>. Windows starts a second process for every URL; that process hands its
    /// --url to the running one and exits. The previous key tree is backed up into
    /// %LOCALAPPDATA%\Login Relay\claims.json BEFORE the registry is touched, so a crash mid-login is
    /// cleaned up by <see cref="ReleaseStaleClaims"/> on the next launch.
    /// </summary>
    public sealed class SchemeClaim : IClaim
    {
        private const string Marker = "LoginRelayClaim";
        private const int MaxDepth = 16;

        private static readonly object Gate = new object();
        private static readonly Regex SchemePattern = new Regex("^[a-z][a-z0-9+.-]*$", RegexOptions.CultureInvariant);

        // Schemes Windows or other programs rely on. A phone app has no business asking for these, and
        // taking one over, even briefly, would be a way to redirect the computer's own behavior.
        private static readonly HashSet<string> Refused = new HashSet<string>(StringComparer.Ordinal)
        {
            "http", "https", "file", "ftp", "mailto", "javascript", "vbscript", "data", "about", "blob",
            "ws", "wss", "tel", "sms", "shell", "search", "search-ms", "ldap", "telnet", "res", "mk",
        };

        private readonly string _scheme;
        private readonly ClaimEntry _entry;
        private bool _released;

        private SchemeClaim(string scheme, ClaimEntry entry)
        {
            _scheme = scheme;
            _entry = entry;
        }

        public static string ClaimsPath
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "Login Relay",
                    "claims.json");
            }
        }

        public static string CommandFor(string exePath)
        {
            return "\"" + exePath + "\" --url \"%1\"";
        }

        private static string KeyPath(string scheme)
        {
            return @"Software\Classes\" + scheme;
        }

        public static bool IsClaimable(string scheme)
        {
            return SchemePattern.IsMatch(scheme)
                && !Refused.Contains(scheme)
                && !scheme.StartsWith("ms-", StringComparison.Ordinal);
        }

        /// <summary>Returns null if the claim can't be made or verified, so the caller falls back to pasting.</summary>
        public static SchemeClaim? Create(string scheme, string exePath)
        {
            string name = scheme.ToLowerInvariant();
            if (!IsClaimable(name))
            {
                return null;
            }
            lock (Gate)
            {
                ClaimEntry? entry = null;
                ClaimsFile? claims = null;
                try
                {
                    claims = LoadClaims();
                    if (IsOurs(name))
                    {
                        // A leftover from a crashed run isn't a "previous handler" worth restoring; keep
                        // whatever that run recorded as the thing to put back.
                        ClaimEntry? kept;
                        entry = claims.Claims.TryGetValue(name, out kept) && kept != null ? kept : new ClaimEntry();
                    }
                    else
                    {
                        RegTree? tree = ReadTree(KeyPath(name));
                        entry = new ClaimEntry { Existed = tree != null, Tree = tree };
                    }

                    // Recorded first, so a crash after this point is recoverable.
                    claims.Claims[name] = entry;
                    SaveClaims(claims);

                    WriteClaimKey(name, exePath);
                    if (!VerifyClaim(name, exePath))
                    {
                        Undo(name, entry, claims);
                        return null;
                    }
                    return new SchemeClaim(name, entry);
                }
                catch (Exception)
                {
                    if (entry != null && claims != null)
                    {
                        try
                        {
                            Undo(name, entry, claims);
                        }
                        catch (Exception)
                        {
                        }
                    }
                    return null;
                }
            }
        }

        /// <summary>Restores the previous key and forgets the claim; keeps the record if restoring failed.</summary>
        private static void Undo(string scheme, ClaimEntry entry, ClaimsFile claims)
        {
            if (RestoreKey(scheme, entry))
            {
                claims.Claims.Remove(scheme);
                SaveClaims(claims);
            }
        }

        public void Release()
        {
            lock (Gate)
            {
                if (_released)
                {
                    return;
                }
                _released = true;
                try
                {
                    if (RestoreKey(_scheme, _entry))
                    {
                        ClaimsFile claims = LoadClaims();
                        claims.Claims.Remove(_scheme);
                        SaveClaims(claims);
                    }
                }
                catch (Exception)
                {
                    // The record stays in claims.json, so the next launch tries again.
                }
            }
        }

        /// <summary>Undoes claims left behind by a previous run that didn't get to clean up.</summary>
        public static void ReleaseStaleClaims()
        {
            lock (Gate)
            {
                try
                {
                    ClaimsFile claims = LoadClaims();
                    foreach (KeyValuePair<string, ClaimEntry> pair in claims.Claims.ToList())
                    {
                        string scheme = pair.Key;
                        if (!SchemePattern.IsMatch(scheme))
                        {
                            claims.Claims.Remove(scheme);
                            continue;
                        }
                        bool exists = KeyExists(scheme);
                        if (!exists && !pair.Value.Existed)
                        {
                            claims.Claims.Remove(scheme);
                        }
                        else if (!exists || IsOurs(scheme))
                        {
                            // Either it's still our claim, or the crash happened after the old key was
                            // removed and before ours was written. Put the old one back.
                            if (RestoreKey(scheme, pair.Value))
                            {
                                claims.Claims.Remove(scheme);
                            }
                        }
                        else
                        {
                            // Someone else owns it now; nothing of ours to undo.
                            claims.Claims.Remove(scheme);
                        }
                    }
                    SaveClaims(claims);
                }
                catch (Exception)
                {
                }
            }
        }

        // ---- the registry ---------------------------------------------------------------------------

        private static bool KeyExists(string scheme)
        {
            using (RegistryKey? key = Registry.CurrentUser.OpenSubKey(KeyPath(scheme), false))
            {
                return key != null;
            }
        }

        private static bool IsOurs(string scheme)
        {
            using (RegistryKey? key = Registry.CurrentUser.OpenSubKey(KeyPath(scheme), false))
            {
                return key != null && key.GetValue(Marker) != null;
            }
        }

        private static void WriteClaimKey(string scheme, string exePath)
        {
            string path = KeyPath(scheme);
            Registry.CurrentUser.DeleteSubKeyTree(path, false);
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(path))
            {
                key.SetValue("", "URL:" + scheme);
                key.SetValue("URL Protocol", "");
                key.SetValue(Marker, "1");
                using (RegistryKey command = key.CreateSubKey(@"shell\open\command"))
                {
                    command.SetValue("", CommandFor(exePath));
                }
            }
        }

        private static bool VerifyClaim(string scheme, string exePath)
        {
            using (RegistryKey? command = Registry.CurrentUser.OpenSubKey(KeyPath(scheme) + @"\shell\open\command", false))
            {
                if (command == null)
                {
                    return false;
                }
                string? value = command.GetValue("", null, RegistryValueOptions.DoNotExpandEnvironmentNames) as string;
                return value == CommandFor(exePath);
            }
        }

        /// <summary>Puts the previous key tree back, or removes the key if there was none. True on success.</summary>
        private static bool RestoreKey(string scheme, ClaimEntry entry)
        {
            try
            {
                string path = KeyPath(scheme);
                Registry.CurrentUser.DeleteSubKeyTree(path, false);
                if (entry.Existed && entry.Tree != null)
                {
                    using (RegistryKey key = Registry.CurrentUser.CreateSubKey(path))
                    {
                        WriteTree(key, entry.Tree);
                    }
                }
                // Exactly restored means: nothing of ours is left, and the old tree is back if there was one.
                return entry.Existed ? KeyExists(scheme) : !KeyExists(scheme);
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static RegTree? ReadTree(string path)
        {
            using (RegistryKey? key = Registry.CurrentUser.OpenSubKey(path, false))
            {
                return key == null ? null : ReadTree(key, 0);
            }
        }

        private static RegTree ReadTree(RegistryKey key, int depth)
        {
            if (depth > MaxDepth)
            {
                throw new IOException("Registry key tree too deep to back up.");
            }
            var tree = new RegTree();
            foreach (string valueName in key.GetValueNames())
            {
                RegistryValueKind kind = key.GetValueKind(valueName);
                object? raw = key.GetValue(valueName, null, RegistryValueOptions.DoNotExpandEnvironmentNames);
                var data = new RegValueData { Name = valueName, Kind = kind.ToString() };
                switch (kind)
                {
                    case RegistryValueKind.String:
                    case RegistryValueKind.ExpandString:
                        data.Text = raw as string ?? "";
                        break;
                    case RegistryValueKind.DWord:
                        data.Number = raw is int i ? i : 0;
                        break;
                    case RegistryValueKind.QWord:
                        data.Number = raw is long l ? l : 0;
                        break;
                    case RegistryValueKind.MultiString:
                        data.Multi = raw as string[] ?? new string[0];
                        break;
                    default:
                        data.Base64 = Convert.ToBase64String(raw as byte[] ?? new byte[0]);
                        break;
                }
                tree.Values.Add(data);
            }
            foreach (string subName in key.GetSubKeyNames())
            {
                using (RegistryKey? sub = key.OpenSubKey(subName, false))
                {
                    if (sub != null)
                    {
                        tree.Subkeys[subName] = ReadTree(sub, depth + 1);
                    }
                }
            }
            return tree;
        }

        private static void WriteTree(RegistryKey key, RegTree tree)
        {
            foreach (RegValueData data in tree.Values)
            {
                RegistryValueKind kind;
                if (!Enum.TryParse(data.Kind, out kind))
                {
                    kind = RegistryValueKind.String;
                }
                switch (kind)
                {
                    case RegistryValueKind.String:
                    case RegistryValueKind.ExpandString:
                        key.SetValue(data.Name, data.Text ?? "", kind);
                        break;
                    case RegistryValueKind.DWord:
                        key.SetValue(data.Name, unchecked((int)data.Number), RegistryValueKind.DWord);
                        break;
                    case RegistryValueKind.QWord:
                        key.SetValue(data.Name, data.Number, RegistryValueKind.QWord);
                        break;
                    case RegistryValueKind.MultiString:
                        key.SetValue(data.Name, data.Multi ?? new string[0], RegistryValueKind.MultiString);
                        break;
                    default:
                        key.SetValue(data.Name, Convert.FromBase64String(data.Base64 ?? ""), kind == RegistryValueKind.None ? RegistryValueKind.None : RegistryValueKind.Binary);
                        break;
                }
            }
            foreach (KeyValuePair<string, RegTree> sub in tree.Subkeys)
            {
                using (RegistryKey child = key.CreateSubKey(sub.Key))
                {
                    WriteTree(child, sub.Value);
                }
            }
        }

        // ---- claims.json ----------------------------------------------------------------------------

        private static ClaimsFile LoadClaims()
        {
            try
            {
                string path = ClaimsPath;
                if (File.Exists(path))
                {
                    ClaimsFile? loaded = JsonSerializer.Deserialize<ClaimsFile>(File.ReadAllText(path));
                    if (loaded != null && loaded.Claims != null)
                    {
                        return loaded;
                    }
                }
            }
            catch (Exception)
            {
            }
            return new ClaimsFile();
        }

        /// <summary>Written atomically (temp file, then replace). Throws if it can't be written.</summary>
        private static void SaveClaims(ClaimsFile claims)
        {
            string path = ClaimsPath;
            if (claims.Claims.Count == 0)
            {
                if (File.Exists(path))
                {
                    File.Delete(path);
                }
                return;
            }
            string? dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir))
            {
                Directory.CreateDirectory(dir);
            }
            string temp = path + ".tmp";
            File.WriteAllText(temp, JsonSerializer.Serialize(claims, new JsonSerializerOptions { WriteIndented = true }));
            File.Move(temp, path, true);
        }
    }
}
