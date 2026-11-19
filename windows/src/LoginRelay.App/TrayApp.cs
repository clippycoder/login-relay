// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Windows.Forms;
using LoginRelay.Core;

namespace LoginRelay.App
{
    /// <summary>Posts actions to the UI thread through a hidden control that lives there.</summary>
    internal sealed class UiDispatcher : IUiDispatcher
    {
        private readonly Control _control;

        public UiDispatcher(Control control)
        {
            _control = control;
        }

        public void Post(Action action)
        {
            try
            {
                if (_control.IsDisposed)
                {
                    return;
                }
                _control.BeginInvoke(action);
            }
            catch (InvalidOperationException)
            {
                // The UI is shutting down (this includes ObjectDisposedException).
            }
        }
    }

    /// <summary>
    /// The tray icon, its menu, and the engine behind them. The menu is rebuilt every time it opens, with
    /// the same items and wording as the macOS menu (minus "Show in Dock", which Windows has no use for).
    /// </summary>
    internal sealed class TrayApp : ApplicationContext
    {
        private readonly Options _options;
        private readonly Control _sync = new Control();
        private readonly UiDispatcher _ui;
        private readonly Prefs _prefs;
        private readonly RelayEngine _engine;
        private readonly WindowsPlatform _platform;
        private readonly NotifyIcon _tray;
        private readonly ContextMenuStrip _menu = new ContextMenuStrip();
        private readonly ActivityForm _window;
        private readonly Icon _iconReady;
        private readonly Icon _iconOff;
        private readonly Icon _iconActive;
        private readonly Font _boldFont;
        private readonly PipeServer _pipe;
        private bool _cleanedUp;

        public TrayApp(Options options)
        {
            _options = options;
            _ = _sync.Handle; // creates the handle now, on the UI thread, so BeginInvoke works from the start
            _ui = new UiDispatcher(_sync);
            _prefs = new Prefs();

            Size small = SystemInformation.SmallIconSize;
            _iconReady = LoadIcon("tray.ico", small);
            _iconOff = LoadIcon("tray-off.ico", small);
            _iconActive = LoadIcon("tray-active.ico", small);
            Icon appIcon = LoadIcon("app.ico", SystemInformation.IconSize);
            _boldFont = new Font(SystemFonts.MenuFont ?? SystemFonts.DefaultFont, FontStyle.Bold);

            _tray = new NotifyIcon();
            _tray.Icon = _iconOff;
            _tray.Text = "Login Relay";
            _tray.ContextMenuStrip = _menu;
            _menu.Opening += (sender, args) => RebuildMenu();
            _tray.MouseClick += (sender, args) =>
            {
                if (args.Button == MouseButtons.Left)
                {
                    _window.ShowWindow();
                }
            };

            _platform = new WindowsPlatform(_tray);
            _engine = new RelayEngine(_ui, _platform, _prefs);
            _engine.LogSink = Program.WriteLog;
            _window = new ActivityForm(_engine, appIcon, ConfirmDownloadPlatformTools, ChooseAdb);
            _engine.Changed += OnEngineChanged;

            // A crash in the middle of a login can leave a return scheme claimed; undo that first.
            SchemeClaim.ReleaseStaleClaims();

            _pipe = new PipeServer(OnPipeMessage);
            _pipe.Start();

            Application.ApplicationExit += (sender, args) => Cleanup();

            UpdateIcon();
            _tray.Visible = true;
            _engine.Start();

            bool firstLaunch = !_prefs.HasLaunchedBefore;
            if (firstLaunch)
            {
                _prefs.HasLaunchedBefore = true;
            }
            // Background by default: no window at startup, apart from the very first launch (or when the
            // user starts the program by hand rather than through autostart or a return address).
            if (firstLaunch || options.Show || (!options.Background && options.Url == null))
            {
                _window.ShowWindow();
            }
            if (options.Url != null)
            {
                string url = options.Url;
                _ui.Post(() => _engine.ReceiveCallback(url));
            }
        }

        private static Icon LoadIcon(string resourceName, Size size)
        {
            using (Stream? stream = typeof(TrayApp).Assembly.GetManifestResourceStream(resourceName))
            {
                if (stream == null)
                {
                    return SystemIcons.Application;
                }
                return new Icon(stream, size);
            }
        }

        // ---- messages from a second launch ----------------------------------------------------------

        /// <summary>Runs on a pool thread: "URL\t&lt;address&gt;" or "SHOW".</summary>
        private void OnPipeMessage(string message)
        {
            _ui.Post(() =>
            {
                const string urlPrefix = "URL\t";
                if (message.StartsWith(urlPrefix, StringComparison.Ordinal))
                {
                    _engine.ReceiveCallback(message.Substring(urlPrefix.Length).Trim());
                }
                else if (message.StartsWith("SHOW", StringComparison.Ordinal))
                {
                    _window.ShowWindow();
                }
            });
        }

        // ---- icon -----------------------------------------------------------------------------------

        private void OnEngineChanged()
        {
            UpdateIcon();
            _window.UpdateView();
        }

        private void UpdateIcon()
        {
            PhaseKind kind = _engine.Phase.Kind;
            Icon icon = kind == PhaseKind.Working ? _iconActive : (kind == PhaseKind.Ready ? _iconReady : _iconOff);
            if (!ReferenceEquals(_tray.Icon, icon))
            {
                _tray.Icon = icon;
            }
            string tip = _engine.Describe().Status;
            // NotifyIcon.Text throws above 63 characters.
            _tray.Text = tip.Length > 63 ? tip.Substring(0, 60) + "..." : tip;
        }

        // ---- menu -----------------------------------------------------------------------------------

        private void RebuildMenu()
        {
            List<ToolStripItem> old = _menu.Items.Cast<ToolStripItem>().ToList();
            _menu.Items.Clear();
            foreach (ToolStripItem item in old)
            {
                item.Dispose();
            }

            (string status, string detail) = _engine.Describe();
            PhaseInfo phase = _engine.Phase;
            bool good = phase.Kind == PhaseKind.Ready || phase.Kind == PhaseKind.Working;

            var head = new ToolStripLabel("●  " + status);
            head.Font = _boldFont;
            _menu.Items.Add(head);
            if (!good && _engine.Busy == null)
            {
                var hintLabel = new Label();
                hintLabel.Text = detail;
                hintLabel.AutoSize = true;
                hintLabel.MaximumSize = new Size(320, 0);
                hintLabel.ForeColor = SystemColors.GrayText;
                hintLabel.Padding = new Padding(22, 2, 8, 6);
                _menu.Items.Add(new ToolStripControlHost(hintLabel));
            }
            _menu.Items.Add(new ToolStripSeparator());

            if (_engine.Busy == null)
            {
                switch (phase.Kind)
                {
                    case PhaseKind.NoAdb:
                        AddItem("Download Android Platform-Tools…", ConfirmDownloadPlatformTools);
                        AddItem("Choose adb…", ChooseAdb);
                        break;
                    case PhaseKind.NeedsApp:
                        if (_engine.BundledApk != null)
                        {
                            AddItem("Install Login Relay on Phone", _engine.InstallPhoneApp);
                        }
                        break;
                    case PhaseKind.Ready:
                        AddItem("Open Login Relay on Phone", _engine.OpenOnPhone);
                        if (_engine.PhoneAppIsOutdated && _engine.PhoneVersion != null && _engine.BundledPhoneVersion != null)
                        {
                            AddItem(
                                "Update Login Relay on Phone (" + _engine.PhoneVersion + " → " + _engine.BundledPhoneVersion + ")",
                                _engine.InstallPhoneApp);
                        }
                        break;
                    case PhaseKind.Working:
                        string verb = phase.Flow != null ? phase.Flow.Request.Verb : "login";
                        AddItem("Cancel " + verb, () => _engine.CancelActive("Cancelled on the computer.", true));
                        break;
                    default:
                        AddItem("Check Again", _engine.CheckAgain);
                        break;
                }
            }

            List<Device> usable = _engine.Devices.Where(d => d.IsUsable).ToList();
            if (usable.Count > 1)
            {
                var parent = new ToolStripMenuItem("Phone");
                foreach (Device device in usable)
                {
                    string serial = device.Serial;
                    var entry = new ToolStripMenuItem(device.Title + " (" + serial + ")");
                    entry.Checked = _engine.Phone != null && _engine.Phone.Serial == serial;
                    entry.Click += (sender, args) => SafeRun(() => _engine.SelectDevice(serial));
                    parent.DropDownItems.Add(entry);
                }
                _menu.Items.Add(parent);
            }
            if (_menu.Items.Count > 0 && !(_menu.Items[_menu.Items.Count - 1] is ToolStripSeparator))
            {
                _menu.Items.Add(new ToolStripSeparator());
            }

            AddItem("Show Activity…", () => _window.ShowWindow());
            _menu.Items.Add(new ToolStripSeparator());

            ToolStripMenuItem login = AddItem("Launch at Login", ToggleLaunchAtLogin);
            login.Checked = LaunchAtLogin.IsEnabled();
            ToolStripMenuItem notes = AddItem("Notify When a Sign-in Arrives", ToggleNotifications);
            notes.Checked = _prefs.Notifications;
            _menu.Items.Add(new ToolStripSeparator());

            AddItem("About Login Relay", About);
            AddItem("Quit Login Relay", Quit);
        }

        private ToolStripMenuItem AddItem(string text, Action action)
        {
            var item = new ToolStripMenuItem(text);
            item.Click += (sender, args) => SafeRun(action);
            _menu.Items.Add(item);
            return item;
        }

        private void SafeRun(Action action)
        {
            try
            {
                action();
            }
            catch (Exception ex)
            {
                _engine.Log("Something went wrong: " + ex.Message);
            }
        }

        // ---- actions --------------------------------------------------------------------------------

        private void ConfirmDownloadPlatformTools()
        {
            int choice = Dialogs.Choose(
                "Download Android platform-tools?",
                "This downloads Google's platform-tools (which include adb) directly from dl.google.com, checks them against Google's published checksum, and keeps them in Login Relay's own folder. They're covered by the Android SDK License.",
                "Download",
                "Cancel",
                "View License");
            if (choice == 0)
            {
                _engine.DownloadPlatformTools();
            }
            else if (choice == 2)
            {
                _platform.OpenUrl(AdbInstaller.LicenseUrl);
            }
        }

        private void ChooseAdb()
        {
            using (var dialog = new OpenFileDialog())
            {
                dialog.Title = "Choose the adb program";
                dialog.Filter = "adb (adb.exe)|adb.exe|Programs (*.exe)|*.exe|All files (*.*)|*.*";
                dialog.CheckFileExists = true;
                dialog.InitialDirectory = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                if (dialog.ShowDialog() == DialogResult.OK)
                {
                    _engine.ChooseAdb(dialog.FileName);
                }
            }
        }

        private void ToggleLaunchAtLogin()
        {
            try
            {
                LaunchAtLogin.Set(!LaunchAtLogin.IsEnabled());
            }
            catch (Exception ex)
            {
                _platform.ShowError("Couldn't change Launch at Login", ex.Message);
            }
        }

        private void ToggleNotifications()
        {
            _prefs.Notifications = !_prefs.Notifications;
            if (_prefs.Notifications)
            {
                _engine.Notify("Login Relay", "You'll see a note here when an app asks to sign in.");
            }
        }

        private void About()
        {
            MessageBox.Show(
                "Login Relay " + Application.ProductVersion
                    + "\n\nFinishes browser sign-ins for Android phones that have no browser, using this computer's browser over adb."
                    + "\n\nGPL-3.0-or-later",
                "About Login Relay",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
        }

        private void Quit()
        {
            Cleanup();
            ExitThread();
        }

        /// <summary>Hands back any claimed scheme, tells the phone, and removes the tray icon. Idempotent.</summary>
        private void Cleanup()
        {
            if (_cleanedUp)
            {
                return;
            }
            _cleanedUp = true;
            try
            {
                _engine.Shutdown();
            }
            catch (Exception)
            {
            }
            _pipe.Dispose();
            _tray.Visible = false;
            _tray.Dispose();
        }
    }
}
