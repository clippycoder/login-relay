// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Windows.Forms;
using LoginRelay.Core;
using Microsoft.Win32;

namespace LoginRelay.App
{
    /// <summary>Windows' side of the engine's <see cref="IPlatform"/>. Everything runs on the UI thread.</summary>
    internal sealed class WindowsPlatform : IPlatform
    {
        private readonly NotifyIcon _tray;

        public static string ExePath
        {
            get { return Environment.ProcessPath ?? Application.ExecutablePath; }
        }

        private static string ExeDirectory
        {
            get { return Path.GetDirectoryName(ExePath) ?? AppContext.BaseDirectory; }
        }

        public WindowsPlatform(NotifyIcon tray)
        {
            _tray = tray;
        }

        /// <summary>
        /// Opens a web address with the default browser. Only http and https are opened: the phone app
        /// supplies these addresses, and ShellExecute would happily run a file: or ms-* address.
        /// </summary>
        public bool OpenUrl(string url)
        {
            try
            {
                if (!Uri.TryCreate(url, UriKind.Absolute, out Uri? uri)
                    || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
                {
                    return false;
                }
                var psi = new ProcessStartInfo(uri.AbsoluteUri);
                psi.UseShellExecute = true;
                using (Process? started = Process.Start(psi))
                {
                    // Nothing to do with it; disposing just releases the handle.
                }
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        public IClaim? Claim(string scheme)
        {
            return SchemeClaim.Create(scheme, ExePath);
        }

        public void Notify(string title, string body)
        {
            try
            {
                _tray.ShowBalloonTip(5000, title, body, ToolTipIcon.Info);
            }
            catch (Exception)
            {
            }
        }

        public void ShowError(string title, string detail)
        {
            MessageBox.Show(detail, title, MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }

        public bool ConfirmWithoutPkce(string appName)
        {
            int choice = Dialogs.Choose(
                appName + " is signing in without PKCE",
                "The code that passes through this computer could be used on its own to take over the sign-in. Only continue if you trust this app and this computer.",
                "Cancel",
                "Continue Anyway");
            return choice == 1;
        }

        public IPromptHandle AskForPastedAddress(string startsWith, Func<string, string?> check, Action onCancel)
        {
            var form = new PasteForm(startsWith, check, onCancel);
            form.Show();
            form.Activate();
            return form;
        }

        public string? BundledApkPath
        {
            get
            {
                string path = Path.Combine(ExeDirectory, "login-relay.apk");
                return File.Exists(path) ? path : null;
            }
        }

        public string? BundledPhoneVersion
        {
            get
            {
                try
                {
                    string path = Path.Combine(ExeDirectory, "phone-app-version.txt");
                    if (File.Exists(path))
                    {
                        string text = File.ReadAllText(path).Trim();
                        return text.Length == 0 ? null : text;
                    }
                }
                catch (Exception)
                {
                }
                return null;
            }
        }
    }

    /// <summary>Launch at login: HKCU\Software\Microsoft\Windows\CurrentVersion\Run, value "LoginRelay".</summary>
    internal static class LaunchAtLogin
    {
        private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        private const string ValueName = "LoginRelay";

        public static bool IsEnabled()
        {
            try
            {
                using (RegistryKey? key = Registry.CurrentUser.OpenSubKey(RunKey, false))
                {
                    return key != null && key.GetValue(ValueName) != null;
                }
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>Throws if the registry can't be written.</summary>
        public static void Set(bool enabled)
        {
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RunKey))
            {
                if (enabled)
                {
                    key.SetValue(ValueName, "\"" + WindowsPlatform.ExePath + "\" --background", RegistryValueKind.String);
                }
                else if (key.GetValue(ValueName) != null)
                {
                    key.DeleteValue(ValueName, false);
                }
            }
        }
    }

    /// <summary>Small modal dialogs with custom button labels (MessageBox can't relabel its buttons).</summary>
    internal static class Dialogs
    {
        /// <summary>
        /// Shows a message with the given buttons and returns the index of the one pressed (-1 if the
        /// window was closed). The first button is the default; a button labeled "Cancel" answers Escape.
        /// </summary>
        public static int Choose(string title, string message, params string[] buttons)
        {
            using (var form = new ChoiceForm(title, message, buttons))
            {
                form.ShowDialog();
                return form.Result;
            }
        }
    }

    internal sealed class ChoiceForm : Form
    {
        public int Result { get; private set; } = -1;

        public ChoiceForm(string title, string message, string[] buttons)
        {
            Text = title;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            ShowInTaskbar = true;
            StartPosition = FormStartPosition.CenterScreen;
            TopMost = true;
            AutoSize = true;
            AutoSizeMode = AutoSizeMode.GrowAndShrink;
            Padding = new Padding(16);

            var layout = new TableLayoutPanel();
            layout.AutoSize = true;
            layout.ColumnCount = 1;
            layout.RowCount = 2;
            layout.Dock = DockStyle.Fill;

            var label = new Label();
            label.Text = message;
            label.AutoSize = true;
            label.MaximumSize = new Size(420, 0);
            label.Margin = new Padding(0, 0, 0, 16);
            layout.Controls.Add(label, 0, 0);

            var row = new FlowLayoutPanel();
            row.AutoSize = true;
            row.WrapContents = false;
            row.FlowDirection = FlowDirection.LeftToRight;
            row.Margin = new Padding(0);
            layout.Controls.Add(row, 0, 1);

            for (int i = 0; i < buttons.Length; i++)
            {
                int index = i;
                var button = new Button();
                button.Text = buttons[i];
                button.AutoSize = true;
                button.MinimumSize = new Size(90, 28);
                button.Click += (sender, args) =>
                {
                    Result = index;
                    DialogResult = DialogResult.OK;
                    Close();
                };
                row.Controls.Add(button);
                if (i == 0)
                {
                    AcceptButton = button;
                }
                if (buttons[i] == "Cancel")
                {
                    CancelButton = button;
                }
            }
            Controls.Add(layout);
        }
    }

    /// <summary>
    /// The "paste the address you ended up on" window. Modeless, so the engine can close it if the login
    /// ends some other way (cancelled on the phone, timed out).
    /// </summary>
    internal sealed class PasteForm : Form, IPromptHandle
    {
        private readonly Func<string, string?> _check;
        private readonly Action _onCancel;
        private readonly Label _message = new Label();
        private readonly TextBox _box = new TextBox();
        private bool _done;

        public PasteForm(string startsWith, Func<string, string?> check, Action onCancel)
        {
            _check = check;
            _onCancel = onCancel;

            Text = "Paste the address you ended up on";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            ShowInTaskbar = true;
            StartPosition = FormStartPosition.CenterScreen;
            TopMost = true;
            ClientSize = new Size(480, 170);

            _message.Text = "After you sign in, copy the full address from your browser's address bar. It starts with " + startsWith + ".";
            _message.Location = new Point(16, 14);
            _message.Size = new Size(448, 60);

            _box.Location = new Point(16, 80);
            _box.Width = 448;

            var send = new Button();
            send.Text = "Send to Phone";
            send.AutoSize = true;
            send.Location = new Point(264, 124);
            send.Click += (sender, args) => OnSend();

            var cancel = new Button();
            cancel.Text = "Cancel";
            cancel.AutoSize = true;
            cancel.Location = new Point(384, 124);
            cancel.Click += (sender, args) => Close();

            Controls.Add(_message);
            Controls.Add(_box);
            Controls.Add(send);
            Controls.Add(cancel);
            AcceptButton = send;
            CancelButton = cancel;
            ActiveControl = _box;
        }

        private void OnSend()
        {
            string? problem = _check(_box.Text.Trim());
            if (problem != null)
            {
                _message.Text = problem;
                _box.SelectAll();
                _box.Focus();
                return;
            }
            CloseSilently();
        }

        /// <summary>IPromptHandle: dismiss without reporting a cancel.</summary>
        void IPromptHandle.Close()
        {
            CloseSilently();
        }

        private void CloseSilently()
        {
            if (_done || IsDisposed)
            {
                return;
            }
            _done = true;
            Close();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            base.OnFormClosing(e);
            if (_done)
            {
                return;
            }
            _done = true;
            if (e.CloseReason == CloseReason.UserClosing)
            {
                _onCancel();
            }
        }
    }
}
