// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using System.Threading;

namespace LoginRelay.Core
{
    /// <summary>One line of the activity log.</summary>
    public sealed class LogLine
    {
        public DateTime Time { get; }
        public string Text { get; }

        public LogLine(DateTime time, string text)
        {
            Time = time;
            Text = text;
        }
    }

    public enum PhaseKind
    {
        NoAdb,
        NoPhone,
        Unauthorized,
        NeedsApp,
        Ready,
        Working,
    }

    /// <summary>Derived, never stored: what the app can do right now.</summary>
    public sealed class PhaseInfo
    {
        public PhaseKind Kind { get; }
        public Device? Phone { get; }
        public Flow? Flow { get; }

        public PhaseInfo(PhaseKind kind, Device? phone = null, Flow? flow = null)
        {
            Kind = kind;
            Phone = phone;
            Flow = flow;
        }
    }

    /// <summary>One sign-in (or sign-out) in progress. Only touched on the UI thread.</summary>
    public sealed class Flow
    {
        public RelayRequest Request { get; }
        public Device Phone { get; }
        public PhoneLink Link { get; }
        public IClaim? Claim { get; set; }
        public Timer? TimeoutTimer { get; set; }
        public IPromptHandle? Prompt { get; set; }

        public Flow(RelayRequest request, Device phone, PhoneLink link)
        {
            Request = request;
            Phone = phone;
            Link = link;
        }
    }

    /// <summary>
    /// Watches for a phone, keeps a link to the Login Relay app on it, and runs each login: opens the page
    /// in the browser, catches the redirect, and hands it back to the phone.
    ///
    /// All state lives on the UI thread (reached through <see cref="IUiDispatcher"/>); the public members
    /// below must be called there. One background thread polls adb and owns the link.
    /// </summary>
    public sealed class RelayEngine
    {
        public const string PhonePackage = "io.github.clippycoder.oauthrelay";

        private readonly IUiDispatcher _ui;
        private readonly IPlatform _platform;
        private readonly Prefs _prefs;

        // ---- tunables (tests shorten them; set before Start) --------------------------------------

        public TimeSpan LoginTimeout { get; set; } = TimeSpan.FromMinutes(15);
        public TimeSpan PlainLinkDoneDelay { get; set; } = TimeSpan.FromSeconds(1);
        public TimeSpan DoneDelay { get; set; } = TimeSpan.FromSeconds(1.5);
        public TimeSpan CheckInterval { get; set; } = TimeSpan.FromSeconds(3);
        public TimeSpan RetryDelay { get; set; } = TimeSpan.FromSeconds(1);
        public TimeSpan WatchdogInterval { get; set; } = TimeSpan.FromSeconds(10);
        public TimeSpan WatchdogLimit { get; set; } = TimeSpan.FromSeconds(30);

        /// <summary>Test hook: LOGINRELAY_BROWSER=none means don't open the sign-in page in a browser.</summary>
        public bool BrowserDisabled { get; set; } =
            string.Equals(Environment.GetEnvironmentVariable("LOGINRELAY_BROWSER"), "none", StringComparison.Ordinal);

        /// <summary>How to find adb (given the user's chosen path, if any). Replaced by tests.</summary>
        public Func<string?, IAdb?> AdbFactory { get; set; } = DefaultAdbFactory;

        /// <summary>Gets every activity-log line, formatted with a timestamp (for LOGINRELAY_LOG and --verbose).</summary>
        public Action<string>? LogSink { get; set; }

        /// <summary>Raised on the UI thread whenever something the UI shows may have changed.</summary>
        public event Action? Changed;

        private static IAdb? DefaultAdbFactory(string? userChoice)
        {
            string? path = Adb.Locate(userChoice, AdbInstaller.InstalledPath);
            return path == null ? null : new Adb(path);
        }

        // ---- state (UI thread) --------------------------------------------------------------------

        public IAdb? AdbTool { get; private set; }
        public IReadOnlyList<Device> Devices { get; private set; } = new List<Device>();
        public Device? Phone { get; private set; }
        public string? PhoneVersion { get; private set; }
        public bool Linked { get; private set; }
        public Flow? Active { get; private set; }
        public string? Busy { get; private set; }

        private readonly List<LogLine> _log = new List<LogLine>();

        public IReadOnlyList<LogLine> LogLines
        {
            get { return _log.ToArray(); }
        }

        public PhaseInfo Phase
        {
            get
            {
                if (AdbTool == null)
                {
                    return new PhaseInfo(PhaseKind.NoAdb);
                }
                if (Phone == null)
                {
                    Device? blocked = Devices.FirstOrDefault(d => !d.IsUsable);
                    if (blocked != null)
                    {
                        return new PhaseInfo(PhaseKind.Unauthorized, blocked);
                    }
                    return new PhaseInfo(PhaseKind.NoPhone);
                }
                if (PhoneVersion == null)
                {
                    return new PhaseInfo(PhaseKind.NeedsApp, Phone);
                }
                if (Active != null)
                {
                    return new PhaseInfo(PhaseKind.Working, Phone, Active);
                }
                return new PhaseInfo(PhaseKind.Ready, Phone);
            }
        }

        /// <summary>The version of the phone app bundled with this app, if any.</summary>
        public string? BundledPhoneVersion
        {
            get { return _platform.BundledPhoneVersion; }
        }

        public string? BundledApk
        {
            get { return _platform.BundledApkPath; }
        }

        public bool PhoneAppIsOutdated
        {
            get
            {
                string? have = PhoneVersion;
                string? bundled = BundledPhoneVersion;
                if (have == null || bundled == null)
                {
                    return false;
                }
                return Redirect.VersionLess(have, bundled);
            }
        }

        // ---- background loop state ----------------------------------------------------------------

        private volatile bool _stopping;
        private int _generation;                    // bumped to abandon a stuck loop thread
        private long _lastTickTicks = DateTime.UtcNow.Ticks;   // last time the loop made progress
        private bool _forceCheck;                   // guarded by _stateLock
        private readonly object _stateLock = new object();
        private readonly AutoResetEvent _wake = new AutoResetEvent(false);
        private PhoneLink? _currentLink;            // UI thread
        private Timer? _watchdog;
        private readonly List<Timer> _delays = new List<Timer>();
        private readonly object _timerLock = new object();

        public RelayEngine(IUiDispatcher ui, IPlatform platform, Prefs prefs)
        {
            _ui = ui;
            _platform = platform;
            _prefs = prefs;
        }

        // ---- lifecycle ----------------------------------------------------------------------------

        public void Start()
        {
            StartLoop();
            // If the polling thread ever stops making progress (adb wedged, a missed wake-up), say so
            // and start over instead of showing a stale state forever.
            _watchdog = new Timer(watchdogState => _ui.Post(WatchdogCheck), null, WatchdogInterval, WatchdogInterval);
            Log("Started.");
        }

        private void WatchdogCheck()
        {
            if (_stopping || SecondsSinceTick <= WatchdogLimit.TotalSeconds)
            {
                return;
            }
            Log("Checking for the phone got stuck; starting over.");
            if (_currentLink != null)
            {
                _currentLink.Close();
            }
            StartLoop();
        }

        private void StartLoop()
        {
            int gen = Interlocked.Increment(ref _generation);
            Tick();
            var thread = new Thread(() => Loop(gen));
            thread.IsBackground = true;
            thread.Name = "relay-loop";
            thread.Start();
        }

        public void Shutdown()
        {
            _stopping = true;
            CancelActive("Quit", true);
            if (_currentLink != null)
            {
                _currentLink.Close();
            }
            _wake.Set();
            Timer? watchdog = _watchdog;
            _watchdog = null;
            if (watchdog != null)
            {
                watchdog.Dispose();
            }
            lock (_timerLock)
            {
                foreach (Timer t in _delays)
                {
                    t.Dispose();
                }
                _delays.Clear();
            }
            IAdb? adb = AdbTool;
            Device? phone = Phone;
            if (adb != null && phone != null)
            {
                try
                {
                    int? port = adb.ExistingForward(phone.Serial);
                    if (port.HasValue)
                    {
                        adb.RemoveForward(port.Value, phone.Serial);
                    }
                }
                catch (Exception)
                {
                }
            }
        }

        /// <summary>"Check Again" in the menu.</summary>
        public void CheckAgain()
        {
            Log("Checking for the phone…");
            RefreshNow();
        }

        public void RefreshNow()
        {
            lock (_stateLock)
            {
                _forceCheck = true;
            }
            _wake.Set();
        }

        // ---- log ----------------------------------------------------------------------------------

        public void Log(string text)
        {
            DateTime now = DateTime.Now;
            _log.Add(new LogLine(now, text));
            if (_log.Count > 300)
            {
                _log.RemoveRange(0, _log.Count - 300);
            }
            Action<string>? sink = LogSink;
            if (sink != null)
            {
                try
                {
                    sink("[" + now.ToString("yyyy-MM-ddTHH:mm:ss") + "] " + text);
                }
                catch (Exception)
                {
                }
            }
            RaiseChanged();
        }

        private void LogAsync(string text)
        {
            _ui.Post(() => Log(text));
        }

        private void RaiseChanged()
        {
            Action? handler = Changed;
            if (handler != null)
            {
                handler();
            }
        }

        // ---- timers (kept referenced; an unreferenced System.Threading.Timer can be collected) ----

        /// <summary>Runs <paramref name="action"/> on the UI thread after a delay.</summary>
        private void After(TimeSpan delay, Action action)
        {
            Timer timer = null!;
            timer = new Timer(
                state =>
                {
                    lock (_timerLock)
                    {
                        _delays.Remove(timer);
                    }
                    try
                    {
                        timer.Dispose();
                    }
                    catch (Exception)
                    {
                    }
                    _ui.Post(action);
                },
                null,
                Timeout.Infinite,
                Timeout.Infinite);
            lock (_timerLock)
            {
                _delays.Add(timer);
            }
            timer.Change(delay, Timeout.InfiniteTimeSpan);
        }

        // ---- progress marker for the watchdog -----------------------------------------------------

        /// <summary>
        /// Written from the loop thread itself (not via the UI thread), so a long modal dialog on the UI
        /// thread can't make a healthy loop look stuck.
        /// </summary>
        private void Tick()
        {
            Interlocked.Exchange(ref _lastTickTicks, DateTime.UtcNow.Ticks);
        }

        private double SecondsSinceTick
        {
            get
            {
                long last = Interlocked.Read(ref _lastTickTicks);
                return (DateTime.UtcNow - new DateTime(last, DateTimeKind.Utc)).TotalSeconds;
            }
        }

        // ---- background loop ----------------------------------------------------------------------

        private void Loop(int gen)
        {
            DateTime lastCheck = DateTime.MinValue;
            int? port = null;
            Device? ready = null;
            IAdb? adb = null;
            string? lastError = null;

            while (!_stopping && gen == Volatile.Read(ref _generation))
            {
                try
                {
                    Tick();
                    bool due;
                    lock (_stateLock)
                    {
                        due = _forceCheck || (DateTime.UtcNow - lastCheck) >= CheckInterval;
                        _forceCheck = false;
                    }

                    if (due)
                    {
                        lastCheck = DateTime.UtcNow;
                        IAdb? found = AdbFactory(_prefs.AdbPath);
                        adb = found;
                        IReadOnlyList<Device> devs = new List<Device>();
                        Device? chosen = null;
                        string? version = null;
                        port = null;
                        if (found != null)
                        {
                            devs = found.Devices();
                            Tick();
                            chosen = Choose(devs);
                            if (chosen != null)
                            {
                                version = found.InstalledVersion(chosen.Serial, PhonePackage);
                                Tick();
                                if (version != null)
                                {
                                    port = found.ExistingForward(chosen.Serial);
                                    Tick();
                                    if (!port.HasValue)
                                    {
                                        port = found.Forward(chosen.Serial);
                                    }
                                }
                            }
                        }
                        ready = (version != null && port.HasValue) ? chosen : null;

                        IAdb? publishAdb = found;
                        IReadOnlyList<Device> publishDevs = devs;
                        Device? publishPhone = chosen;
                        string? publishVersion = version;
                        _ui.Post(() =>
                        {
                            if (gen == Volatile.Read(ref _generation))
                            {
                                Publish(publishAdb, publishDevs, publishPhone, publishVersion);
                            }
                        });
                    }

                    if (port.HasValue && ready != null)
                    {
                        Device readyPhone = ready;
                        var link = new PhoneLink();
                        var closed = new ManualResetEventSlim(false);
                        link.OnMessage = msg => HandleMessage(msg, link, readyPhone);
                        link.OnClose = () =>
                        {
                            _ui.Post(() => LinkClosed(link));
                            closed.Set();
                        };
                        if (link.Open(port.Value))
                        {
                            _ui.Post(() => LinkOpened(link));
                            // adb can hold a connection open without it reaching the phone (for instance
                            // while a replugged phone is still reconnecting), so keep checking that the
                            // phone is really there rather than waiting for the link to end on its own.
                            while (!closed.Wait(CheckInterval))
                            {
                                Tick();
                                if (_stopping || gen != Volatile.Read(ref _generation))
                                {
                                    link.Close();
                                    break;
                                }
                                IReadOnlyList<Device> here = adb != null ? adb.Devices() : new List<Device>();
                                Tick();
                                if (!here.Any(h => h.Serial == readyPhone.Serial && h.IsUsable))
                                {
                                    link.Close();
                                    break;
                                }
                            }
                            lastCheck = DateTime.MinValue; // look at the phone again straight away
                            continue;
                        }
                    }
                    _wake.WaitOne(RetryDelay);
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

        private Device? Choose(IReadOnlyList<Device> devs)
        {
            List<Device> usable = devs.Where(d => d.IsUsable).ToList();
            string? selected = _prefs.SelectedSerial;
            if (selected != null)
            {
                Device? match = usable.FirstOrDefault(d => d.Serial == selected);
                if (match != null)
                {
                    return match;
                }
            }
            return usable.FirstOrDefault();
        }

        // ---- UI-thread publishing -----------------------------------------------------------------

        private void Publish(IAdb? adb, IReadOnlyList<Device> devices, Device? phone, string? version)
        {
            bool adbChanged = AdbTool?.ExePath != adb?.ExePath;
            bool devicesChanged = !Devices.SequenceEqual(devices);
            bool changed = adbChanged || devicesChanged || Phone != phone || PhoneVersion != version;
            if (adbChanged)
            {
                Log(adb == null ? "adb not found." : "Using adb at " + adb.ExePath + ".");
            }
            if (devicesChanged && devices.Count > 0)
            {
                Log("adb lists: " + string.Join(", ", devices.Select(d => d.Serial + " (" + d.State + ")")));
            }
            if (Phone != phone)
            {
                Log(phone != null ? "Phone: " + phone.Title + " (" + phone.Serial + ")" : "No phone.");
            }
            if (PhoneVersion != version && phone != null)
            {
                Log(version != null ? "Login Relay " + version + " is on the phone." : "Login Relay isn't on the phone.");
            }
            AdbTool = adb;
            Devices = devices;
            Phone = phone;
            PhoneVersion = version;
            if (changed)
            {
                RaiseChanged();
            }
            if ((phone == null || version == null) && Active != null)
            {
                CancelActive("Phone not available", false);
            }
        }

        private void LinkOpened(PhoneLink link)
        {
            if (_stopping)
            {
                return;
            }
            _currentLink = link;
            Linked = true;
            Log("Linked to Login Relay on the phone.");
            RaiseChanged();
        }

        private void LinkClosed(PhoneLink link)
        {
            if (ReferenceEquals(_currentLink, link))
            {
                _currentLink = null;
                Linked = false;
                if (!_stopping)
                {
                    Log("Link to the phone closed.");
                }
            }
            if (Active != null && ReferenceEquals(Active.Link, link))
            {
                CancelActive("The phone closed Login Relay.", false);
            }
            RaiseChanged();
        }

        // ---- messages from the phone --------------------------------------------------------------

        private static string? StringProperty(JsonElement element, string name)
        {
            if (element.ValueKind == JsonValueKind.Object
                && element.TryGetProperty(name, out JsonElement value)
                && value.ValueKind == JsonValueKind.String)
            {
                return value.GetString();
            }
            return null;
        }

        /// <summary>Runs on the link's read thread; everything real happens on the UI thread.</summary>
        private void HandleMessage(JsonElement msg, PhoneLink link, Device phone)
        {
            string? type = StringProperty(msg, "type");
            if (type == "open")
            {
                string? url = StringProperty(msg, "url");
                if (url == null)
                {
                    return;
                }
                var request = new RelayRequest(url, StringProperty(msg, "referrer"), StringProperty(msg, "label"));
                _ui.Post(() => Begin(request, link, phone));
            }
            else if (type == "cancel")
            {
                _ui.Post(() =>
                {
                    if (Active != null && ReferenceEquals(Active.Link, link))
                    {
                        CancelActive("Cancelled on the phone.", false);
                    }
                });
            }
            // Unknown messages are ignored.
        }

        // ---- one login ----------------------------------------------------------------------------

        private void Begin(RelayRequest request, PhoneLink link, Device phone)
        {
            if (_stopping)
            {
                return;
            }
            if (Active != null)
            {
                CancelActive("Replaced by a newer request.", false);
            }
            Log(request.AppName + " wants to open a " + request.Verb + " page"
                + (request.Host != null ? " at " + request.Host : "") + ".");

            if (request.Kind == RequestKind.PlainLink)
            {
                if (_platform.OpenUrl(request.Url))
                {
                    link.Send("status", "Opened on your computer.");
                    After(PlainLinkDoneDelay, () => link.Send("done"));
                }
                else
                {
                    Log("Couldn't open that address on this computer.");
                    link.Send("error", "Couldn't open the page on the computer.");
                }
                return;
            }

            if (request.LacksPkce && !_platform.ConfirmWithoutPkce(request.AppName))
            {
                Log("Declined: the sign-in doesn't use PKCE.");
                link.Send("error", "Cancelled on the computer.");
                return;
            }

            var flow = new Flow(request, phone, link);
            Active = flow;
            string? scheme = request.CatchableScheme;
            if (scheme != null)
            {
                flow.Claim = TryClaim(scheme);
                if (flow.Claim == null)
                {
                    Log("Windows wouldn't let Login Relay take over " + scheme + "://, so you'll paste the address instead.");
                }
            }
            flow.TimeoutTimer = new Timer(
                timeoutState => _ui.Post(() => LoginTimedOut(flow)),
                null,
                LoginTimeout,
                Timeout.InfiniteTimeSpan);
            link.Send("status", request.Kind == RequestKind.SignIn
                ? "Finish signing in on your computer."
                : "Finishing sign-out on your computer.");

            // Test hook: the end-to-end tests set LOGINRELAY_BROWSER=none so no browser opens.
            if (!BrowserDisabled)
            {
                if (!_platform.OpenUrl(request.Url))
                {
                    Log("Couldn't open the page in your browser.");
                }
            }
            Log("Opened the page in your browser. If it asks to open Login Relay, allow it.");
            Notify(request.AppName + " " + request.Verb, "Continue in your browser.");
            RaiseChanged();
            if (flow.Claim == null)
            {
                _ui.Post(() => AskForPastedAddress(flow));
            }
        }

        private IClaim? TryClaim(string scheme)
        {
            try
            {
                return _platform.Claim(scheme);
            }
            catch (Exception ex)
            {
                Log("Claiming " + scheme + ":// failed: " + ex.Message);
                return null;
            }
        }

        private void LoginTimedOut(Flow flow)
        {
            if (!ReferenceEquals(Active, flow))
            {
                return;
            }
            flow.Link.Send("error", "Timed out waiting for the browser.");
            CancelActive("Timed out waiting for the browser.", true);
        }

        /// <summary>
        /// Called with every address the system hands us (a URL the browser opened, or one pasted). Only the
        /// one that matches the login in progress counts. UI thread.
        /// </summary>
        public void ReceiveCallback(string callback)
        {
            Flow? flow = Active;
            if (flow == null)
            {
                return;
            }
            switch (flow.Request.Accepts(callback))
            {
                case CallbackVerdict.Unrelated:
                    return;
                case CallbackVerdict.StateMismatch:
                    Log("Ignored a return address from a different sign-in (state mismatch).");
                    return;
                default:
                    Finish(flow, callback);
                    return;
            }
        }

        private void Teardown(Flow flow)
        {
            Timer? timer = flow.TimeoutTimer;
            flow.TimeoutTimer = null;
            if (timer != null)
            {
                try
                {
                    timer.Dispose();
                }
                catch (Exception)
                {
                }
            }
            IClaim? claim = flow.Claim;
            flow.Claim = null;
            if (claim != null)
            {
                try
                {
                    claim.Release();
                }
                catch (Exception ex)
                {
                    Log("Couldn't release the claim: " + ex.Message);
                }
            }
            IPromptHandle? prompt = flow.Prompt;
            flow.Prompt = null;
            if (prompt != null)
            {
                try
                {
                    prompt.Close();
                }
                catch (Exception)
                {
                }
            }
        }

        private void Finish(Flow flow, string callback)
        {
            if (!ReferenceEquals(Active, flow))
            {
                return;
            }
            Active = null;
            Teardown(flow);
            Dictionary<string, string> p = Redirect.UrlParams(callback);
            if (p.TryGetValue("error", out string? err))
            {
                string description = p.TryGetValue("error_description", out string? d) ? d : err;
                Log("The page returned an error: " + description);
            }
            Log("Returning to " + flow.Request.AppName + " on the phone…");
            RaiseChanged();
            IAdb? adb = AdbTool;
            if (adb == null)
            {
                flow.Link.Send("error", "Couldn't hand the result back to the app.");
                return;
            }
            ThreadPool.QueueUserWorkItem(queued =>
            {
                ProcessResult r;
                try
                {
                    r = adb.Deliver(flow.Phone.Serial, callback, flow.Request.Caller);
                }
                catch (Exception ex)
                {
                    r = new ProcessResult(-1, "", ex.Message);
                }
                _ui.Post(() =>
                {
                    if (!r.Ok || r.Out.Contains("Error", StringComparison.Ordinal))
                    {
                        Log("The phone couldn't take the result: " + (r.Out.Length == 0 ? r.Err : r.Out));
                        flow.Link.Send("error", "Couldn't hand the result back to the app.");
                        return;
                    }
                    Log("Done: " + flow.Request.AppName + " is finishing the " + flow.Request.Verb + ".");
                    // Let the app's own screen take the result before Login Relay closes.
                    After(DoneDelay, () => flow.Link.Send("done"));
                });
            });
        }

        public void CancelActive(string reason, bool notifyPhone)
        {
            Flow? flow = Active;
            if (flow == null)
            {
                return;
            }
            Active = null;
            Teardown(flow);
            Log(reason);
            if (notifyPhone)
            {
                flow.Link.Send("done");
            }
            RaiseChanged();
        }

        // ---- prompts ------------------------------------------------------------------------------

        /// <summary>For return addresses that are ordinary web links, which the system can't hand to us.</summary>
        private void AskForPastedAddress(Flow flow)
        {
            if (!ReferenceEquals(Active, flow) || flow.Request.RedirectAddress == null)
            {
                return;
            }
            string redirect = flow.Request.RedirectAddress;
            string[] pieces = redirect.Split('?', StringSplitOptions.RemoveEmptyEntries);
            string startsWith = pieces.Length > 0 ? pieces[0] : redirect;
            flow.Prompt = _platform.AskForPastedAddress(
                startsWith,
                pasted => CheckPasted(flow, pasted),
                () =>
                {
                    if (ReferenceEquals(Active, flow))
                    {
                        CancelActive("Cancelled on the computer.", true);
                    }
                });
        }

        /// <summary>Null when the pasted address was accepted (or the login is already over), else what to tell the user.</summary>
        private string? CheckPasted(Flow flow, string pasted)
        {
            string text = pasted.Trim();
            if (!ReferenceEquals(Active, flow))
            {
                return null;
            }
            switch (flow.Request.Accepts(text))
            {
                case CallbackVerdict.Ok:
                    Finish(flow, text);
                    return null;
                case CallbackVerdict.StateMismatch:
                    return "That address is from a different sign-in. Try again.";
                default:
                    return "That isn't this app's return address. Try again.";
            }
        }

        // ---- phone-side app management ------------------------------------------------------------

        public void InstallPhoneApp()
        {
            IAdb? adb = AdbTool;
            Device? phone = Phone;
            string? apk = BundledApk;
            if (adb == null || phone == null || apk == null)
            {
                return;
            }
            Busy = "Installing Login Relay on " + phone.Title + "…";
            Log(Busy);
            RaiseChanged();
            ThreadPool.QueueUserWorkItem(queued =>
            {
                ProcessResult r;
                try
                {
                    r = adb.Install(phone.Serial, apk);
                }
                catch (Exception ex)
                {
                    r = new ProcessResult(-1, "", ex.Message);
                }
                _ui.Post(() =>
                {
                    Busy = null;
                    if (r.Ok && r.Out.Contains("Success", StringComparison.Ordinal))
                    {
                        Log("Installed.");
                    }
                    else
                    {
                        string why = string.Join(" ", new[] { r.Out, r.Err }.Where(s => s.Length > 0));
                        Log("Install failed: " + why);
                        _platform.ShowError("Couldn't install Login Relay on the phone", why);
                    }
                    RaiseChanged();
                    RefreshNow();
                });
            });
        }

        public void OpenOnPhone()
        {
            IAdb? adb = AdbTool;
            Device? phone = Phone;
            if (adb == null || phone == null)
            {
                return;
            }
            ThreadPool.QueueUserWorkItem(queued =>
            {
                try
                {
                    adb.Shell(phone.Serial, "input keyevent KEYCODE_WAKEUP; am start -n " + PhonePackage + "/.RelayActivity");
                }
                catch (Exception)
                {
                }
            });
        }

        public void DownloadPlatformTools()
        {
            Busy = "Downloading Android platform-tools…";
            Log(Busy);
            RaiseChanged();
            ThreadPool.QueueUserWorkItem(queued =>
            {
                string? failure = null;
                try
                {
                    AdbInstaller.Install(step => LogAsync(step));
                }
                catch (Exception ex)
                {
                    failure = ex.Message;
                }
                _ui.Post(() =>
                {
                    Busy = null;
                    if (failure != null)
                    {
                        Log("Download failed: " + failure);
                        _platform.ShowError("Couldn't download platform-tools", failure);
                    }
                    RaiseChanged();
                    RefreshNow();
                });
            });
        }

        public void SelectDevice(string serial)
        {
            _prefs.SelectedSerial = serial;
            RefreshNow();
        }

        public void ChooseAdb(string path)
        {
            _prefs.AdbPath = path;
            RefreshNow();
        }

        // ---- notifications ------------------------------------------------------------------------

        public void Notify(string title, string body)
        {
            if (!_prefs.Notifications)
            {
                return;
            }
            try
            {
                _platform.Notify(title, body);
            }
            catch (Exception)
            {
            }
        }

        // ---- wording shared by the window and the menu (word for word from the macOS app) ---------

        /// <summary>Headline and one line of explanation for the current phase.</summary>
        public (string Status, string Detail) Describe()
        {
            if (Busy != null)
            {
                return (Busy, "This takes a moment.");
            }
            PhaseInfo phase = Phase;
            switch (phase.Kind)
            {
                case PhaseKind.NoAdb:
                    return ("adb not found", "Login Relay talks to the phone through adb, part of Android platform-tools.");
                case PhaseKind.NoPhone:
                    return ("No phone connected", "Plug the phone in with USB and turn on USB debugging (Settings → Developer options).");
                case PhaseKind.Unauthorized:
                {
                    Device d = phase.Phone!;
                    if (d.State == "unauthorized")
                    {
                        return (d.Title + " is waiting for you", "Tap Allow on the phone's \"Allow USB debugging?\" prompt.");
                    }
                    return (d.Title + " isn't responding (" + d.State + ")", "Unplug the cable and plug it back in, then choose Check Again.");
                }
                case PhaseKind.NeedsApp:
                {
                    Device d = phase.Phone!;
                    return ("Login Relay isn't on " + d.Title,
                        BundledApk != null ? "Install it on the phone to start." : "Install login-relay.apk on the phone.");
                }
                case PhaseKind.Ready:
                {
                    Device d = phase.Phone!;
                    string outdated = PhoneAppIsOutdated ? " An update for the phone app is available." : "";
                    return (d.Title + " is ready", "Sign in to an app on the phone and the page opens here." + outdated);
                }
                default:
                {
                    Flow flow = phase.Flow!;
                    return (flow.Request.AppName + " " + flow.Request.Verb + " in progress",
                        "Finish in your browser. If it asks to open Login Relay, allow it.");
                }
            }
        }
    }
}
