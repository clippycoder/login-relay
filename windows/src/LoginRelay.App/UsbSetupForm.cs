// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Drawing;
using System.Windows.Forms;
using LoginRelay.Core;

namespace LoginRelay.App
{
    /// <summary>
    /// The guided USB driver setup: shows what Windows and libusb currently see and the exact next step
    /// (Zadig step 1 for the phone, step 2 for its accessory mode). Closing it only hides it.
    /// </summary>
    internal sealed class UsbSetupForm : Form
    {
        public const string ZadigUrl = "https://zadig.akeo.ie/";

        private readonly RelayEngine _engine;
        private readonly Func<string, bool> _openUrl;
        private readonly Label _title = new Label();
        private readonly TextBox _body = new TextBox();
        private readonly Button _zadig = new Button();
        private readonly Button _check = new Button();
        private readonly Button _close = new Button();
        private string _shown = "";

        public UsbSetupForm(RelayEngine engine, Icon appIcon, Func<string, bool> openUrl)
        {
            _engine = engine;
            _openUrl = openUrl;

            Text = "USB driver setup";
            Icon = appIcon;
            ClientSize = new Size(560, 400);
            MinimumSize = new Size(440, 320);
            StartPosition = FormStartPosition.CenterScreen;
            Font = SystemFonts.MessageBoxFont ?? Font;

            var root = new TableLayoutPanel();
            root.Dock = DockStyle.Fill;
            root.Padding = new Padding(16);
            root.ColumnCount = 1;
            root.RowCount = 3;
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));

            _title.AutoSize = true;
            _title.Font = new Font(Font.FontFamily, 12f, FontStyle.Bold);
            _title.MaximumSize = new Size(520, 0);
            _title.Margin = new Padding(0, 0, 0, 10);
            root.Controls.Add(_title, 0, 0);

            // A read-only text box, so the user can select and copy the USB ID.
            _body.Multiline = true;
            _body.ReadOnly = true;
            _body.ScrollBars = ScrollBars.Vertical;
            _body.WordWrap = true;
            _body.BorderStyle = BorderStyle.None;
            _body.BackColor = SystemColors.Control;
            _body.TabStop = false;
            _body.Dock = DockStyle.Fill;
            root.Controls.Add(_body, 0, 1);

            var buttons = new FlowLayoutPanel();
            buttons.AutoSize = true;
            buttons.FlowDirection = FlowDirection.LeftToRight;
            buttons.Margin = new Padding(0, 10, 0, 0);
            _zadig.Text = "Open Zadig website";
            _zadig.AutoSize = true;
            _zadig.MinimumSize = new Size(120, 28);
            _zadig.Click += (sender, args) =>
            {
                if (!_openUrl(ZadigUrl))
                {
                    MessageBox.Show("Couldn't open " + ZadigUrl + " in your browser. Type it in by hand.", "USB driver setup");
                }
            };
            _check.Text = "Check again";
            _check.AutoSize = true;
            _check.MinimumSize = new Size(100, 28);
            _check.Click += (sender, args) => _engine.CheckAgain();
            _close.Text = "Close";
            _close.AutoSize = true;
            _close.MinimumSize = new Size(80, 28);
            _close.Click += (sender, args) => Hide();
            buttons.Controls.Add(_zadig);
            buttons.Controls.Add(_check);
            buttons.Controls.Add(_close);
            root.Controls.Add(buttons, 0, 2);

            Controls.Add(root);
            CancelButton = _close;
            UpdateView();
        }

        /// <summary>Brings the window to the front (it is normally hidden).</summary>
        public void ShowWindow()
        {
            if (WindowState == FormWindowState.Minimized)
            {
                WindowState = FormWindowState.Normal;
            }
            Show();
            UpdateView();
            TopMost = true;
            TopMost = false;
            Activate();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;
                Hide();
                return;
            }
            base.OnFormClosing(e);
        }

        /// <summary>Refreshes the title and steps from the engine's latest diagnosis. Cheap when hidden.</summary>
        public void UpdateView()
        {
            if (!Visible && IsHandleCreated)
            {
                return;
            }
            string title;
            string body;
            if (_engine.Transport != TransportKind.Usb)
            {
                title = "USB accessory mode is off";
                body = "Choose “USB accessory (no USB debugging) — beta” under Connect Over in the tray menu first. Then this window shows what Windows needs for it.";
            }
            else
            {
                UsbSetupInfo? setup = _engine.UsbSetup;
                title = UsbSetupText.Title(setup);
                body = setup == null ? "Login Relay is looking for the phone…" : UsbSetupText.Steps(setup);
            }
            string key = title + "\n" + body;
            if (key == _shown)
            {
                return;
            }
            _shown = key;
            _title.Text = title;
            _body.Text = body;
            _body.SelectionStart = 0;
            _body.SelectionLength = 0;
        }
    }
}
