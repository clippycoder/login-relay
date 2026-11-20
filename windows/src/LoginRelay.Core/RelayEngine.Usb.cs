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

        /// <summary>How often to ask Windows (WMI, slow) what it sees while no phone is usable.</summary>
        public TimeSpan UsbDiagnosticsInterval { get; set; } = TimeSpan.FromSeconds(5);

        /// <summary>Shown on the phone as the accessory's description.</summary>
        public string HostName { get; set; } = Environment.MachineName;

        /// <summary>How to reach libusb. Replaced by tests.</summary>
        public Func<IUsbBackend> UsbBackendFactory { get; set; } = () => new LibUsbBackend();

        /// <summary>Windows' own view of the USB devices (for the driver guide). Null means not available.</summary>
        public IUsbDiagnostics? UsbDiagnostics { get; set; }

        /// <summary>Stops the adb server, which holds a phone exclusively. Replaced by tests.</summary>
        public Func<bool> StopAdbServer { get; set; } = DefaultStopAdbServer;

        // ---- state (UI thread) --------------------------------------------------------------------

        public TransportKind Transport
        {
            get { return _prefs.Transport; }
        }

        public IReadOnlyList<UsbDevice> UsbCandidates { get; private set; } = new List<UsbDevice>();
        public string UsbDetail { get; private set; } = "";
        public bool UsbAlive { get; private set; }

        /// <summary>The latest driver diagnosis (USB mode only).</summary>
        public UsbSetupInfo? UsbSetup { get; private set; }

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
                return new PhaseInfo(PhaseKind.NoPhone);
            }
            if (Active != null)
            {
                return new PhaseInfo(PhaseKind.Working, phone, Active);
            }
            if (UsbAlive || _usbEverAlive)
            {
                return new PhaseInfo(PhaseKind.Ready, phone);
            }
            return new PhaseInfo(PhaseKind.Connecting, phone, null, UsbDetail);
        }

        private static bool DefaultStopAdbServer()
        {
            string? path = Adb.Locate(null, AdbInstaller.InstalledPath);
            if (path == null)
            {
                return false;
            }
            Adb.Exec(path, new[] { "kill-server" }, 10);
            return true;
        }

        // ---- switching and choosing ---------------------------------------------------------------

        /// <summary>Switches between adb and USB accessory mode.</summary>
        public void SetTransport(TransportKind kind)
        {
            if (kind == _prefs.Transport)
            {
                return;
            }
            CancelActive("Switching connection type.", true);
            IPhoneConnection? link = _currentLink;
            if (link != null)
            {
                link.Close();
            }
            CancelDelivery();
            _prefs.Transport = kind;
            AdbTool = null;
            Devices = new List<Device>();
            Phone = null;
            PhoneVersion = null;
            Linked = false;
            UsbCandidates = new List<UsbDevice>();
            UsbAlive = false;
            _usbEverAlive = false;
            UsbDetail = "";
            UsbSetup = null;
            Log(kind == TransportKind.Usb
                ? "Connecting over USB accessory mode (no USB debugging needed)."
                : "Connecting over adb.");
            StartLoop();
            RaiseChanged();
        }

        public void SelectUsb(string id)
        {
            _prefs.UsbSelected = id;
            RefreshNow();
        }

        // ---- UI-thread publishing -----------------------------------------------------------------

        private void ApplyUsb(Device? phone, IReadOnlyList<UsbDevice> candidates, string detail, UsbSetupInfo? setup)
        {
            bool changed = Phone != phone
                || !UsbCandidates.SequenceEqual(candidates)
                || UsbDetail != detail
                || !Equals(UsbSetup, setup);
            if (Phone != phone)
            {
                Log(phone != null ? "USB phone: " + phone.Title : "No USB phone.");
            }
            Phone = phone;
            UsbCandidates = candidates;
            UsbDetail = detail;
            UsbSetup = setup;
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
            if (!alive)
            {
                PhoneVersion = null;
                if (Active != null)
                {
                    CancelActive("Login Relay on the phone stopped answering.", false);
                }
            }
            RaiseChanged();
        }

        // ---- delivery without adb -----------------------------------------------------------------

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

        private (string, string) DescribeUsbNoPhone(bool brief)
        {
            UsbSetupInfo? setup = UsbSetup;
            if (setup != null && setup.IsDriverProblem)
            {
                return (UsbSetupText.Headline(setup)!, brief ? UsbSetupText.Summary(setup) : UsbSetupText.Steps(setup));
            }
            if (UsbDetail.Length > 0)
            {
                return ("No phone found over USB", UsbDetail);
            }
            return ("No phone found over USB",
                "Plug the phone in and choose “File transfer” in its USB notification. USB debugging isn't needed."
                + " If Windows doesn't see it yet, choose “" + UsbSetupText.SetupMenuName + "” in the tray menu.");
        }

        private (string, string) DescribeUsbConnecting(PhaseInfo phase, bool brief)
        {
            Device d = phase.Phone!;
            UsbSetupInfo? setup = UsbSetup;
            if (setup != null && setup.State == UsbSetupState.NeedsAccessoryDriver)
            {
                return ("Connecting to " + d.Title + "…", brief ? UsbSetupText.Summary(setup) : UsbSetupText.Steps(setup));
            }
            // Once the USB pipe is open the phone is plugged in and connected; it's just not answering yet.
            string headline = Linked ? d.Title + " is connected over USB" : "Connecting to " + d.Title + "…";
            string detail = phase.Detail.Length == 0 ? "Waiting for the phone." : phase.Detail;
            return (headline, detail + " Not working? Choose “" + UsbSetupText.SetupMenuName + "” in the tray menu.");
        }

        // ---- the background loop ------------------------------------------------------------------

        private bool UsbCurrent(int gen)
        {
            return !_stopping && gen == Volatile.Read(ref _generation);
        }

        private void PublishUsb(int gen, Device? phone, IReadOnlyList<UsbDevice> candidates, string detail, UsbSetupInfo? setup)
        {
            _ui.Post(() =>
            {
                if (gen == Volatile.Read(ref _generation))
                {
                    ApplyUsb(phone, candidates, detail, setup);
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
            var noWindows = new List<PnpDevice>();
            IReadOnlyList<PnpDevice> windows = noWindows;
            DateTime lastDiagnostics = DateTime.MinValue;
            string? lastError = null;
            bool diagnosticsFailed = false;

            try
            {
                backend = UsbBackendFactory();
                while (UsbCurrent(gen))
                {
                    try
                    {
                        Tick();
                        bool force;
                        lock (_stateLock)
                        {
                            force = _forceCheck;
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

                        IUsbDiagnostics? diagnostics = UsbDiagnostics;
                        if (target == null)
                        {
                            // Nothing usable: ask Windows what it sees, so we can tell the user which driver step is next.
                            if (diagnostics != null && (force || DateTime.UtcNow - lastDiagnostics >= UsbDiagnosticsInterval))
                            {
                                lastDiagnostics = DateTime.UtcNow;
                                try
                                {
                                    windows = diagnostics.QueryUsbDevices();
                                    diagnosticsFailed = false;
                                }
                                catch (Exception ex)
                                {
                                    windows = noWindows;
                                    if (!diagnosticsFailed)
                                    {
                                        diagnosticsFailed = true;
                                        LogAsync("Couldn't ask Windows about USB devices: " + ex.Message);
                                    }
                                }
                                Tick();
                            }
                            UsbSetupInfo none = UsbDiagnosisLogic.Diagnose(all, windows);
                            PublishUsb(gen, null, candidates, candidates.Count > 1 ? "Choose which phone to use from the menu." : "", none);
                            _wake.WaitOne(RetryDelay);
                            continue;
                        }

                        UsbSetupInfo ready = UsbDiagnosisLogic.Diagnose(all, noWindows);
                        var phone = new Device("usb:" + target.Id, "device", target.Name.Replace(' ', '_'));
                        string openLocation = target.Location;

                        if (!target.IsAccessory)
                        {
                            if (!limiter.TryAcquire(target.Location, DateTime.UtcNow))
                            {
                                _wake.WaitOne(RetryDelay);
                                continue;
                            }
                            PublishUsb(gen, phone, candidates, "Asking " + target.Name + " to switch to USB accessory mode…", ready);
                            UsbSwitchResult r = backend.SwitchToAccessory(target.Location, HostName);
                            if (r.Code == UsbCode.Busy && StopAdbServerOnce())
                            {
                                _wake.WaitOne(RetryDelay);
                                r = backend.SwitchToAccessory(target.Location, HostName);
                            }
                            if (r.Code != UsbCode.Ok)
                            {
                                UsbSetupInfo? failSetup = r.Code == UsbCode.NoDriver
                                    ? new UsbSetupInfo(UsbSetupState.NeedsPhoneDriver, target.VendorId, target.ProductId, target.Name)
                                    : (UsbSetupInfo?)null;
                                string text = failSetup != null
                                    ? UsbSetupText.Summary(failSetup)
                                    : UsbText.ErrorText(r, target.Name);
                                PublishUsb(gen, phone, candidates, text, failSetup);
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
                                // Listed but not usable, or not listed at all: ask Windows whether it has the accessory device.
                                if (diagnostics != null)
                                {
                                    try
                                    {
                                        windows = diagnostics.QueryUsbDevices();
                                        lastDiagnostics = DateTime.UtcNow;
                                    }
                                    catch (Exception)
                                    {
                                        windows = noWindows;
                                    }
                                    Tick();
                                }
                                UsbSetupInfo diagnosis = UsbDiagnosisLogic.Diagnose(after, windows);
                                if (diagnosis.State == UsbSetupState.NeedsAccessoryDriver)
                                {
                                    PublishUsb(gen, phone, candidates, UsbSetupText.Summary(diagnosis), diagnosis);
                                }
                                else
                                {
                                    PublishUsb(gen, phone, candidates, "The phone didn't switch to accessory mode.", ready);
                                }
                                continue;
                            }
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
                                if (ex.Code == UsbCode.NoDriver)
                                {
                                    break;
                                }
                            }
                            Thread.Sleep(UsbOpenRetryDelay);
                        }
                        if (conn == null)
                        {
                            if (openError != null && openError.Code == UsbCode.NoDriver)
                            {
                                var needs = new UsbSetupInfo(
                                    UsbSetupState.NeedsAccessoryDriver, target.VendorId, target.ProductId,
                                    UsbDiagnosisLogic.AccessoryName, openError.Detail);
                                PublishUsb(gen, phone, candidates, UsbSetupText.Summary(needs), needs);
                            }
                            else
                            {
                                int code = openError != null ? openError.Code : UsbCode.Open;
                                string more = openError != null && openError.Detail.Length > 0 ? ": " + openError.Detail : "";
                                PublishUsb(gen, phone, candidates, "Couldn't open the accessory connection (error " + code + more + ").", ready);
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
                            "Login Relay on the phone isn't answering yet. If Android asks to allow USB access, tap OK; otherwise just start a sign-in on the phone and Login Relay starts by itself.",
                            ready);
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
                        PublishUsb(gen, null, new List<UsbDevice>(), text, null);
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

        private bool StopAdbServerOnce()
        {
            try
            {
                LogAsync("Another program has the phone open, probably the adb server. Stopping it (it restarts when needed).");
                return StopAdbServer();
            }
            catch (Exception)
            {
                return false;
            }
        }
    }
}
