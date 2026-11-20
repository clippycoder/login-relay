// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.IO;
using System.Text.Json;

namespace LoginRelay.Core
{
    /// <summary>The stored shape of the preferences file.</summary>
    public sealed class PrefsData
    {
        public bool Notifications { get; set; }
        public bool HasLaunchedBefore { get; set; }

        /// <summary>Which USB phone to use when several are plugged in (a port location such as "1-4.2").</summary>
        public string? UsbSelected { get; set; }
    }

    /// <summary>
    /// Preferences in a small JSON file (%APPDATA%\Login Relay\prefs.json by default). Safe to use from
    /// any thread. A missing or unreadable file means defaults; a failed save is ignored.
    /// </summary>
    public sealed class Prefs
    {
        private readonly object _lock = new object();
        private readonly string _path;
        private PrefsData _data;

        public static string DefaultPath
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                    "Login Relay",
                    "prefs.json");
            }
        }

        public Prefs(string? path = null)
        {
            _path = path ?? DefaultPath;
            _data = Load();
        }

        /// <summary>Show a balloon when a sign-in arrives. Off by default (like the macOS app).</summary>
        public bool Notifications
        {
            get { lock (_lock) { return _data.Notifications; } }
            set { lock (_lock) { _data.Notifications = value; Save(); } }
        }

        public bool HasLaunchedBefore
        {
            get { lock (_lock) { return _data.HasLaunchedBefore; } }
            set { lock (_lock) { _data.HasLaunchedBefore = value; Save(); } }
        }

        /// <summary>Which USB phone to use when several are plugged in (its port location).</summary>
        public string? UsbSelected
        {
            get { lock (_lock) { return _data.UsbSelected; } }
            set { lock (_lock) { _data.UsbSelected = value; Save(); } }
        }

        private PrefsData Load()
        {
            try
            {
                if (File.Exists(_path))
                {
                    PrefsData? loaded = JsonSerializer.Deserialize<PrefsData>(File.ReadAllText(_path));
                    if (loaded != null)
                    {
                        return loaded;
                    }
                }
            }
            catch (Exception)
            {
                // Corrupt or unreadable: start over with defaults.
            }
            return new PrefsData();
        }

        /// <summary>Called with the lock held.</summary>
        private void Save()
        {
            try
            {
                string? dir = Path.GetDirectoryName(_path);
                if (!string.IsNullOrEmpty(dir))
                {
                    Directory.CreateDirectory(dir);
                }
                string temp = _path + ".tmp";
                File.WriteAllText(temp, JsonSerializer.Serialize(_data, new JsonSerializerOptions { WriteIndented = true }));
                File.Move(temp, _path, true);
            }
            catch (Exception)
            {
                // Best effort.
            }
        }
    }
}
