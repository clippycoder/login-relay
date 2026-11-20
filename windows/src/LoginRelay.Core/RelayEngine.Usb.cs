// SPDX-License-Identifier: GPL-3.0-or-later
// The USB accessory half of the engine (port of the USB parts of macos/Sources/RelayEngine.swift).
using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;

namespace LoginRelay.Core
{
    public sealed partial class RelayEngine
    {
        // ---- tunables (tests shorten them; set before Start) --------------------------------------

        /// <summary>The computer sends a ping this often.</summary>
        public TimeSpan UsbPingInterval { get; set; } = TimeSpan.FromSeconds(2);

        /// <summary>The link is alive if any message arrived in this long.</summary>
        public TimeSpan UsbSilence { get; set; } = TimeSpan.FromSeconds(8);

        /// <summary>How long to wait for the phone to say it handed the result to the app.</summary>
        public TimeSpan UsbDeliveryTimeout { get; set; } = TimeSpan.FromSeconds(10);

        /// <summary>One switch attempt per location per this long.</summary>
        public TimeSpan UsbSwitchInterval { get; set; } = TimeSpan.FromSeconds(20);

        /// <summary>Android gives up if the host hasn't reconnected within 10 s, so wait a little longer than that.</summary>
        public TimeSpan UsbAppearTimeout { get; set; } = TimeSpan.FromSeconds(12);

        public TimeSpan UsbOpenRetryDelay { get; set; } = TimeSpan.FromMilliseconds(500);
        public int UsbOpenAttempts { get; set; } = 6;

        /// <summary>Shown on the phone as the accessory's description.</summary>
        public string HostName { get; set; } = Environment.MachineName;

        /// <summary>How to reach libusb (or, with LOGINRELAY_FAKE_PHONE, a TCP socket). Replaced by tests.</summary>
        public Func<IUsbBackend> UsbBackendFactory { get; set; } = DefaultBackend;

        // ---- state (UI thread) --------------------------------------------------------------------

        public IReadOnlyList<UsbDevice> UsbCandidates { get; private set; } = new List<UsbDevice>();
        public string UsbDetail { get; private set; } = "";
        public bool UsbAlive { get; private set; }

        /// <summary>The phone is plugged in but libusb can't open it (see <see cref="UsbText.CantOpenDetail"/>).</summary>
        public bool UsbCantOpen { get; private set; }

        /// <summary>
        /// Login Relay on the phone has answered at least once on this connection. Android reclaims an idle app
        /// process on a small phone, which silences it; that's normal, and the next sign-in starts it again by itself.
        /// </summary>
        private bool _usbEverAlive;
        private Flow? _awaitingDelivery;
        private Timer? _deliveryTimer;

        private PhaseInfo UsbPhaseInfo()
        {
            Device? phone = Phone;
            if (phone == null)
            {
                return new PhaseInfo(UsbCantOpen ? PhaseKind.CantOpen : PhaseKind.NoPhone);
            }
            if (Active != null)
            {
                return new PhaseInfo(PhaseKind.Working, phone, Active);
            }
            if (UsbCantOpen)
            {
                return new PhaseInfo(PhaseKind.CantOpen, phone);
            }
            if (UsbAlive || _usbEverAlive)
            {
                return new PhaseInfo(PhaseKind.Ready, phone);
            }
            return new PhaseInfo(PhaseKind.Connecting, phone, null, UsbDetail);
        }

        private static IUsbBackend DefaultBackend()
        {
            // Test hook: LOGINRELAY_FAKE_PHONE=host:port means "one phone is plugged in" without touching libusb.
            string? fake = Environment.GetEnvironmentVariable("LOGINRELAY_FAKE_PHONE");
            string host;
            int port;
            if (TcpPhoneBackend.TryParse(fake, out host, out port))
            {
                return new TcpPhoneBackend(host, port);
            }
            return new LibUsbBackend();
        }

        // ---- choosing -----------------------------------------------------------------------------

        public void SelectUsb(string id)
        {
            _prefs.UsbSelected = id;
            RefreshNow();
        }

        /// <summary>
        /// "Driver Help…": opens the README's Windows drivers section in the browser. Only offered while the status
        /// is <see cref="PhaseKind.CantOpen"/>.
        /// </summary>
        public void OpenDriverHelp()
        {
            if (!_platform.OpenUrl(UsbText.DriverHelpUrl))
            {
                Log("Couldn't open the browser. Open " + UsbText.DriverHelpUrl + " yourself.");
            }
        }

        // ---- UI-thread publishing -----------------------------------------------------------------

        private void ApplyUsb(Device? phone, IReadOnlyList<UsbDevice> candidates, string detail, bool cantOpen)
        {
            bool changed = Phone != phone
                || !UsbCandidates.SequenceEqual(candidates)
                || UsbDetail != detail
                || UsbCantOpen != cantOpen;
            if (Phone != phone)
            {
                Log(phone != null ? "USB phone: " + phone.Title : "No USB phone.");
            }
            if (cantOpen && !UsbCantOpen)
            {
                Log("Login Relay can't open the phone over USB (no WinUSB driver for it, or no access).");
            }
            Phone = phone;
            UsbCandidates = candidates;
            UsbDetail = detail;
            UsbCantOpen = cantOpen;
            if (changed)
            {
                RaiseChanged();
            }
            if (phone == null && Active != null)
            {
                CancelActive("Phone not available", false);
            }
        }

        private void UsbAliveChanged(bool alive, IPhoneConnection link)
        {
            if (!ReferenceEquals(_currentLink, link) || UsbAlive == alive)
            {
                return;
            }
            UsbAlive = alive;
            if (alive)
            {
                _usbEverAlive = true;
            }
            Log(alive
                ? "Login Relay on the phone is answering."
                : "Login Relay on the phone went idle (it starts again by itself when an app asks to sign in).");
            if (!alive && Active != null)
            {
                CancelActive("Login Relay on the phone stopped answering.", false);
            }
            RaiseChanged();
        }

        // ---- delivery -----------------------------------------------------------------------------

        private void SendCallbackOverUsb(Flow flow, string callback)
        {
            _awaitingDelivery = flow;
            CancelDeliveryTimer();
            _deliveryTimer = new Timer(
                state => _ui.Post(() => DeliveryTimedOut(flow)),
                null,
                UsbDeliveryTimeout,
                Timeout.InfiniteTimeSpan);
            var fields = new Dictionary<string, string>();
            fields["type"] = "callback";
            fields["url"] = callback;
            flow.Link.SendFields(fields);
        }

        private void DeliveryTimedOut(Flow flow)
        {
            if (!ReferenceEquals(_awaitingDelivery, flow))
            {
                return;
            }
            _awaitingDelivery = null;
            Log("The phone didn't confirm it handed the result to the app.");
        }

        private void DeliveryConfirmed()
        {
            Flow? flow = _awaitingDelivery;
            if (flow == null)
            {
                return;
            }
            _awaitingDelivery = null;
            CancelDeliveryTimer();
            Log("Done: " + flow.Request.AppName + " is finishing the " + flow.Request.Verb + ".");
        }

        private void PhoneReportedError(string text)
        {
            Log("The phone says: " + text);
            if (_awaitingDelivery != null)
            {
                _awaitingDelivery = null;
                CancelDeliveryTimer();
            }
        }

        private void CancelDeliveryTimer()
        {
            Timer? t = _deliveryTimer;
            _deliveryTimer = null;
            if (t != null)
            {
                try
                {
                    t.Dispose();
                }
                catch (Exception)
                {
                }
            }
        }

        private void CancelDelivery()
        {
            _awaitingDelivery = null;
            CancelDeliveryTimer();
        }

        private void ShutdownUsb()
        {
            CancelDelivery();
        }

        // ---- wording ------------------------------------------------------------------------------

        private (string, string) DescribeUsbNoPhone()
        {
            if (UsbDetail.Length > 0)
            {
                return (UsbText.NoPhoneHeadline, UsbDetail);
            }
            return (UsbText.NoPhoneHeadline, UsbText.NoPhoneDetail);
        }

        private (string, string) DescribeUsbConnecting(PhaseInfo phase)
        {
            Device d = phase.Phone!;
            // Once the USB pipe is open the phone is plugged in and connected; it's just not answering yet.
            string headline = Linked ? d.Title + " is connected over USB" : "Connecting to " + d.Title + "…";
            string detail = phase.Detail.Length == 0 ? "Waiting for the phone." : phase.Detail;
            return (headline, detail);
        }

        // ---- the background loop ------------------------------------------------------------------

        private bool UsbCurrent(int gen)
        {
            return !_stopping && gen == Volatile.Read(ref _generation);
        }

        private void PublishUsb(int gen, Device? phone, IReadOnlyList<UsbDevice> candidates, string detail, bool cantOpen = false)
        {
            _ui.Post(() =>
            {
                if (gen == Volatile.Read(ref _generation))
                {
                    ApplyUsb(phone, candidates, detail, cantOpen);
                }
            });
        }

        private static UsbDevice? FindAccessory(IReadOnlyList<UsbDevice> list, UsbDevice phone)
        {
            UsbDevice? same = list.FirstOrDefault(d => d.IsAccessory && d.Location == phone.Location);
            if (same != null)
            {
                return same;
            }
            // The port path should survive the switch; if it didn't, the only accessory on the bus is ours.
            List<UsbDevice> accessories = list.Where(d => d.IsAccessory).ToList();
            return accessories.Count == 1 ? accessories[0] : null;
        }

        /// <summary>
        /// Background loop for USB accessory mode: find a phone, switch it to accessory mode, open the pipe,
        /// and keep it open until the cable comes out.
        /// </summary>
        private void UsbLoop(int gen)
        {
            IUsbBackend? backend = null;
            var limiter = new SwitchLimiter(UsbSwitchInterval);
            string? lastError = null;

            try
            {
                backend = UsbBackendFactory();
                // True while the last switch attempt ended in "probably a missing driver". The next attempt's
                // "Asking … to switch" status keeps the hint on screen instead of flickering it off for the
                // whole time the app waits for the accessory to appear.
                bool driverHint = false;
                while (UsbCurrent(gen))
                {
                    try
                    {
                        Tick();
                        lock (_stateLock)
                        {
                            _forceCheck = false;
                        }

                        IReadOnlyList<UsbDevice> all = backend.ListDevices();
                        // A phone that was unplugged gets a fresh try when it comes back.
                        limiter.Forget(all.Select(d => d.Location));
                        List<UsbDevice> usable = all.Where(d => d.Openable).ToList();
                        List<UsbDevice> candidates = usable.Where(d => d.LooksAndroid && !d.IsAccessory).ToList();
                        UsbDevice? target = usable.FirstOrDefault(d => d.IsAccessory);
                        if (target == null)
                        {
                            string? selected = _prefs.UsbSelected;
                            UsbDevice? chosen = selected == null ? null : candidates.FirstOrDefault(c => c.Id == selected);
                            if (chosen != null)
                            {
                                target = chosen;
                            }
                            else if (candidates.Count == 1)
                            {
                                target = candidates[0];
                            }
                        }

                        if (target == null)
                        {
                            // A phone (or its accessory-mode device) that libusb lists but can't open: Windows has
                            // no WinUSB driver for it. Say so plainly instead of "no phone".
                            bool blocked = candidates.Count == 0
                                && all.Any(d => !d.Openable
                                    && (d.IsAccessory
                                        || (d.LooksAndroid && !UsbHeuristics.IsDownloadMode(d.VendorId, d.ProductId))));
                            PublishUsb(
                                gen, null, candidates,
                                candidates.Count > 1 ? "Choose which phone to use from the menu." : "",
                                blocked);
                            _wake.WaitOne(RetryDelay);
                            continue;
                        }

                        var phone = new Device("usb:" + target.Id, target.Name);
                        string openLocation = target.Location;

                        if (!target.IsAccessory)
                        {
                            if (!limiter.TryAcquire(target.Location, DateTime.UtcNow))
                            {
                                _wake.WaitOne(RetryDelay);
                                continue;
                            }
                            PublishUsb(gen, phone, candidates, "Asking " + target.Name + " to switch to USB accessory mode…", driverHint);
                            UsbSwitchResult r = backend.SwitchToAccessory(target.Location, HostName);
                            if (r.Code != UsbCode.Ok)
                            {
                                driverHint = UsbText.IsCantOpen(r.Code);
                                PublishUsb(gen, phone, candidates, UsbText.ErrorText(r, target.Name), driverHint);
                                _wake.WaitOne(TimeSpan.FromSeconds(3));
                                continue;
                            }

                            // Android gives up if the host hasn't reconnected within 10 s.
                            UsbDevice? appeared = null;
                            IReadOnlyList<UsbDevice> after = all;
                            DateTime deadline = DateTime.UtcNow + UsbAppearTimeout;
                            while (DateTime.UtcNow < deadline)
                            {
                                if (!UsbCurrent(gen))
                                {
                                    return;
                                }
                                Thread.Sleep(200);
                                Tick();
                                after = backend.ListDevices();
                                appeared = FindAccessory(after, target);
                                if (appeared != null)
                                {
                                    break;
                                }
                            }
                            if (appeared == null || !appeared.Openable)
                            {
                                // Either the accessory device is there but libusb can't open it, or the phone accepted
                                // the switch and never came back as an accessory. On Windows both are what a missing
                                // WinUSB driver for the accessory device looks like.
                                driverHint = true;
                                PublishUsb(gen, phone, candidates, UsbText.CantOpenDetail, true);
                                continue;
                            }
                            driverHint = false;
                            openLocation = appeared.Location;
                        }

                        IUsbConnection? conn = null;
                        UsbException? openError = null;
                        for (int attempt = 0; attempt < UsbOpenAttempts; attempt++)
                        {
                            if (!UsbCurrent(gen))
                            {
                                return;
                            }
                            try
                            {
                                conn = backend.OpenAccessory(openLocation);
                                break;
                            }
                            catch (UsbException ex)
                            {
                                openError = ex;
                                if (ex.Code == UsbCode.NoDriver || ex.Code == UsbCode.Permission)
                                {
                                    break;
                                }
                            }
                            Thread.Sleep(UsbOpenRetryDelay);
                        }
                        if (conn == null)
                        {
                            int code = openError != null ? openError.Code : UsbCode.Open;
                            if (openError != null && (code == UsbCode.NoDriver || code == UsbCode.Permission))
                            {
                                PublishUsb(gen, phone, candidates, UsbText.CantOpenDetail, true);
                            }
                            else if (code == UsbCode.Busy)
                            {
                                PublishUsb(gen, phone, candidates, UsbText.BusyDetail);
                            }
                            else
                            {
                                string more = openError != null && openError.Detail.Length > 0 ? ": " + openError.Detail : "";
                                PublishUsb(gen, phone, candidates, "Couldn't open the accessory connection (error " + code + more + ").");
                            }
                            _wake.WaitOne(TimeSpan.FromSeconds(3));
                            continue;
                        }

                        var link = new UsbPhoneLink(conn, UsbPingInterval, UsbSilence);
                        var closed = new ManualResetEventSlim(false);
                        link.OnMessage = msg => HandleMessage(msg, link, phone);
                        link.OnAlive = alive => _ui.Post(() => UsbAliveChanged(alive, link));
                        link.OnClose = () =>
                        {
                            _ui.Post(() => LinkClosed(link));
                            closed.Set();
                        };
                        PublishUsb(
                            gen, phone, candidates,
                            "Login Relay on the phone isn't answering yet. If Android asks to allow USB access, tap OK; otherwise just start a sign-in on the phone and Login Relay starts by itself.");
                        _ui.Post(() => LinkOpened(link));
                        link.Start();
                        while (!closed.Wait(CheckInterval))
                        {
                            Tick();
                            if (!UsbCurrent(gen))
                            {
                                link.Close();
                                break;
                            }
                            bool present = true;
                            try
                            {
                                IReadOnlyList<UsbDevice> here = backend.ListDevices();
                                present = here.Any(d => d.IsAccessory && d.Location == openLocation);
                            }
                            catch (Exception)
                            {
                                // A failed look isn't proof the phone is gone; the read loop notices a real unplug.
                            }
                            if (!present)
                            {
                                link.Close();
                                break;
                            }
                        }
                        // Let the read thread release the pipe before anything tries to open it again.
                        closed.Wait(TimeSpan.FromSeconds(3));
                    }
                    catch (UsbException ex)
                    {
                        string text = UsbText.ErrorText(new UsbSwitchResult(ex.Code, 0, ex.Detail), "the phone");
                        PublishUsb(gen, null, new List<UsbDevice>(), text);
                        if (text != lastError)
                        {
                            lastError = text;
                            LogAsync(text);
                        }
                        Tick();
                        _wake.WaitOne(TimeSpan.FromSeconds(3));
                    }
                    catch (Exception ex)
                    {
                        // Never let the loop die silently; say so once per distinct problem and carry on.
                        string message = "Checking for the phone failed: " + ex.Message;
                        if (message != lastError)
                        {
                            lastError = message;
                            LogAsync(message);
                        }
                        Tick();
                        if (!_stopping)
                        {
                            _wake.WaitOne(RetryDelay);
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                LogAsync("USB mode couldn't start: " + ex.Message);
            }
            finally
            {
                IDisposable? disposable = backend as IDisposable;
                if (disposable != null)
                {
                    try
                    {
                        disposable.Dispose();
                    }
                    catch (Exception)
                    {
                    }
                }
            }
        }
    }
}
