// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Drawing;
using System.Linq;
using System.Windows.Forms;
using LoginRelay.Core;

namespace LoginRelay.App
{
    /// <summary>
    /// A small window with the current status, the one or two actions that make sense right now, and
    /// the log. Closing it only hides it.
    /// </summary>
    internal sealed class ActivityForm : Form
    {
        private readonly RelayEngine _engine;
        private readonly Label _status = new Label();
        private readonly Label _detail = new Label();
        private readonly Button _primary = new Button();
        private readonly Button _secondary = new Button();
        private readonly TextBox _log = new TextBox();
        private Action? _primaryAction;
        private Action? _secondaryAction;
        private string _shownLog = "";

        public ActivityForm(RelayEngine engine, Icon appIcon)
        {
            _engine = engine;

            Text = "Login Relay";
            Icon = appIcon;
            ClientSize = new Size(540, 460);
            MinimumSize = new Size(420, 340);
            StartPosition = FormStartPosition.CenterScreen;
            Font = SystemFonts.MessageBoxFont ?? Font;

            var root = new TableLayoutPanel();
            root.Dock = DockStyle.Fill;
            root.Padding = new Padding(16);
            root.ColumnCount = 1;
            root.RowCount = 4;
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

            // Header: icon, headline, one line of explanation.
            var header = new TableLayoutPanel();
            header.AutoSize = true;
            header.ColumnCount = 2;
            header.RowCount = 1;
            header.Dock = DockStyle.Fill;
            var picture = new PictureBox();
            picture.Size = new Size(48, 48);
            picture.SizeMode = PictureBoxSizeMode.Zoom;
            picture.Image = new Icon(appIcon, new Size(48, 48)).ToBitmap();
            picture.Margin = new Padding(0, 0, 12, 0);
            header.Controls.Add(picture, 0, 0);

            _status.AutoSize = true;
            _status.Font = new Font(Font.FontFamily, 12f, FontStyle.Bold);
            _status.MaximumSize = new Size(460, 0);
            _detail.AutoSize = true;
            _detail.MaximumSize = new Size(460, 0);
            _detail.ForeColor = SystemColors.GrayText;
            var titles = new FlowLayoutPanel();
            titles.AutoSize = true;
            titles.FlowDirection = FlowDirection.TopDown;
            titles.WrapContents = false;
            titles.Controls.Add(_status);
            titles.Controls.Add(_detail);
            header.Controls.Add(titles, 1, 0);
            root.Controls.Add(header, 0, 0);

            // Buttons.
            var buttons = new FlowLayoutPanel();
            buttons.AutoSize = true;
            buttons.FlowDirection = FlowDirection.LeftToRight;
            buttons.Margin = new Padding(0, 12, 0, 12);
            _primary.AutoSize = true;
            _primary.MinimumSize = new Size(110, 28);
            _primary.Click += (sender, args) => RunAction(_primaryAction);
            _secondary.AutoSize = true;
            _secondary.MinimumSize = new Size(110, 28);
            _secondary.Click += (sender, args) => RunAction(_secondaryAction);
            buttons.Controls.Add(_primary);
            buttons.Controls.Add(_secondary);
            root.Controls.Add(buttons, 0, 1);

            var logTitle = new Label();
            logTitle.Text = "Activity";
            logTitle.AutoSize = true;
            logTitle.ForeColor = SystemColors.GrayText;
            root.Controls.Add(logTitle, 0, 2);

            _log.Multiline = true;
            _log.ReadOnly = true;
            _log.ScrollBars = ScrollBars.Vertical;
            _log.WordWrap = true;
            _log.Font = new Font("Consolas", 9f);
            _log.BackColor = SystemColors.Window;
            _log.Dock = DockStyle.Fill;
            root.Controls.Add(_log, 0, 3);

            Controls.Add(root);
            UpdateView();
        }

        private static void RunAction(Action? action)
        {
            if (action != null)
            {
                action();
            }
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
            // Windows won't always let a background app take focus; toggling TopMost reliably raises it.
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

        /// <summary>Refreshes headline, buttons and log from the engine. Cheap when the window is hidden.</summary>
        public void UpdateView()
        {
            if (!Visible && IsHandleCreated)
            {
                return;
            }
            (string status, string detail) = _engine.Describe();
            _status.Text = status;
            _detail.Text = detail;

            _primaryAction = null;
            _secondaryAction = null;
            _primary.Visible = false;
            _secondary.Visible = false;
            PhaseInfo phase = _engine.Phase;
            switch (phase.Kind)
            {
                case PhaseKind.Ready:
                    break;
                case PhaseKind.Working:
                    Offer(_primary, "Cancel", () => _engine.CancelActive("Cancelled on the computer.", true), true);
                    break;
                case PhaseKind.CantOpen:
                    Offer(_primary, "Check Again", _engine.CheckAgain, true);
                    Offer(_secondary, "Driver Help…", _engine.OpenDriverHelp, false);
                    break;
                default:
                    Offer(_primary, "Check Again", _engine.CheckAgain, true);
                    break;
            }
            _primary.Enabled = true;

            string text = string.Join(
                Environment.NewLine,
                _engine.LogLines.Select(line => line.Time.ToString("HH:mm:ss") + "  " + line.Text));
            if (text != _shownLog)
            {
                _shownLog = text;
                _log.Text = text;
                _log.SelectionStart = _log.TextLength;
                _log.ScrollToCaret();
            }
        }

        private void Offer(Button button, string title, Action action, bool isPrimary)
        {
            button.Text = title;
            button.Visible = true;
            if (isPrimary)
            {
                _primaryAction = action;
            }
            else
            {
                _secondaryAction = action;
            }
        }
    }
}
