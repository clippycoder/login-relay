# SPDX-License-Identifier: GPL-3.0-or-later
"""Watches for a phone, keeps a link to the Login Relay app on it, and runs each login: opens the
page in the browser, catches the redirect, and hands it back to the phone.

Port of macos/Sources/RelayEngine.swift. UI-agnostic: it talks to the world only through a `Host`.
State lives on the UI thread (everything the engine changes goes through host.post); one background
thread polls adb and owns the link.
"""
from __future__ import annotations

import datetime
import os
import sys
import threading
import time
import traceback
from collections import namedtuple
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from . import PHONE_PACKAGE
from .adb import Adb, Device
from .phonelink import PhoneLink
from .prefs import Prefs, find_share_file
from .redirect import (OK, PLAIN_LINK, SIGN_IN, STATE_MISMATCH, UNRELATED, RelayRequest,
                       url_params, url_scheme, version_less)

LOGIN_TIMEOUT = 15 * 60

NO_ADB = "noAdb"
NO_PHONE = "noPhone"
UNAUTHORIZED = "unauthorized"
NEEDS_APP = "needsApp"
READY = "ready"
WORKING = "working"

PhaseInfo = namedtuple("PhaseInfo", "kind device flow")


class Host:
    """What the engine needs from the surrounding application. This base class is a working
    headless default; app.py (GTK) and the tests override it.

    Every method is called on the UI thread, except `post` and `call_later`, which may be called
    from any thread."""

    def post(self, fn: Callable[[], None]) -> None:
        """Run fn on the UI thread (soon)."""
        fn()

    def call_later(self, delay: float, fn: Callable[[], None]):
        """Run fn on the UI thread after `delay` seconds. Returns an object with cancel()."""
        t = threading.Timer(delay, lambda: self.post(fn))
        t.daemon = True
        t.start()
        return t

    def open_url(self, url: str) -> None:
        """Open a web page in the user's browser."""

    def claim(self, scheme: str):
        """Make this app the system handler for `scheme`. Returns an object with release(), or None
        if the system refuses (the engine then asks for the address to be pasted)."""
        return None

    def notify(self, title: str, body: str) -> None:
        """Show a desktop notification."""

    def show_error(self, title: str, detail: str) -> None:
        """Tell the user something failed."""

    def confirm_without_pkce(self, request: RelayRequest) -> bool:
        """Ask whether to continue a sign-in that doesn't use PKCE. May block (modal)."""
        return False

    def ask_for_pasted_address(self, flow: "Flow") -> None:
        """Show a non-modal prompt for the final address. The host calls engine.submit_pasted(flow,
        text) (returns a verdict) or engine.cancel_pasted(flow)."""

    def close_pasted_address(self, flow: "Flow") -> None:
        """The login ended some other way; close the prompt shown by ask_for_pasted_address."""


class Flow:
    """One sign-in (or sign-out) in progress."""

    def __init__(self, request: RelayRequest, device: Device, link: PhoneLink):
        self.request = request
        self.device = device
        self.link = link
        self.claim = None
        self.timeout = None
        self.paste_open = False


def _cancel(handle) -> None:
    if handle is not None:
        try:
            handle.cancel()
        except Exception:
            pass


def _openable(url: str) -> bool:
    """Only web pages go to the browser; the phone doesn't get to launch arbitrary schemes here."""
    return url_scheme(url) in ("http", "https")


class RelayEngine:
    def __init__(self, host: Host, prefs: Prefs, *, bundled_apk: Optional[str] = None,
                 bundled_phone_version: Optional[str] = None, check_interval: float = 3.0,
                 link_recheck: float = 3.0, watchdog_interval: float = 10.0, watchdog_after: float = 30.0,
                 login_timeout: float = LOGIN_TIMEOUT, done_delay: float = 1.5,
                 locate: Optional[Callable[[Optional[str]], Optional[str]]] = None):
        self.host = host
        self.prefs = prefs
        self._bundled_apk = bundled_apk
        self.bundled_phone_version = bundled_phone_version
        self._check_interval = check_interval
        self._link_recheck = link_recheck
        self._watchdog_interval = watchdog_interval
        self._watchdog_after = watchdog_after
        self._login_timeout = login_timeout
        self._done_delay = done_delay
        self._locate = locate or (lambda choice: Adb.locate(user_choice=choice))

        # state (UI thread)
        self.adb: Optional[Adb] = None
        self.devices: List[Device] = []
        self.device: Optional[Device] = None
        self.phone_version: Optional[str] = None
        self.linked = False
        self.active: Optional[Flow] = None
        self.busy: Optional[str] = None
        self.log_lines: List[Tuple[datetime.datetime, str]] = []
        self.on_change: Optional[Callable[[], None]] = None
        self.log_hooks: List[Callable[[datetime.datetime, str], None]] = []
        self.verbose = False

        # background loop state
        self._stopping = False
        self._generation = 0  # bumped to abandon a stuck loop thread
        self._last_tick = time.monotonic()  # last time the loop made progress
        self._watchdog = None
        self._force_check = False
        self._wake = threading.Event()
        self._current_link: Optional[PhoneLink] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ---- derived state ----------------------------------------------------------------------

    @property
    def phase(self) -> PhaseInfo:
        if self.adb is None:
            return PhaseInfo(NO_ADB, None, None)
        if self.device is None:
            blocked = next((d for d in self.devices if not d.is_usable), None)
            if blocked is not None:
                return PhaseInfo(UNAUTHORIZED, blocked, None)
            return PhaseInfo(NO_PHONE, None, None)
        if self.phone_version is None:
            return PhaseInfo(NEEDS_APP, self.device, None)
        if self.active is not None:
            return PhaseInfo(WORKING, self.device, self.active)
        return PhaseInfo(READY, self.device, None)

    @property
    def bundled_apk(self) -> Optional[str]:
        return self._bundled_apk if self._bundled_apk and os.path.isfile(self._bundled_apk) else None

    @property
    def phone_app_is_outdated(self) -> bool:
        have, bundled = self.phone_version, self.bundled_phone_version
        return bool(have and bundled and version_less(have, bundled))

    # ---- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        self._start_loop()
        # If the polling thread ever stops making progress (adb wedged, a missed wake-up), say so
        # and start over instead of showing a stale state forever.
        self._arm_watchdog()
        self.log("Started.")

    def _arm_watchdog(self) -> None:
        self._watchdog = self.host.call_later(self._watchdog_interval, self._watchdog_fired)

    def _watchdog_fired(self) -> None:
        if self._stopping:
            return
        if self._seconds_since_tick() > self._watchdog_after:
            self.log("Checking for the phone got stuck; starting over.")
            if self._current_link is not None:
                self._current_link.close()
            self._start_loop()
        self._arm_watchdog()

    def _start_loop(self) -> None:
        self._generation += 1
        gen = self._generation
        self._tick()
        t = threading.Thread(target=self._loop, args=(gen,), name="relay-loop", daemon=True)
        t.start()
        self._thread = t

    def shutdown(self) -> None:
        self._stopping = True
        _cancel(self._watchdog)
        self.cancel_active("Quit", notify_phone=True)
        if self._current_link is not None:
            self._current_link.close()
        self._wake.set()
        if self.adb is not None and self.device is not None:
            port = self.adb.existing_forward(self.device.serial, timeout=5)
            if port is not None:
                self.adb.remove_forward(port, self.device.serial, timeout=5)

    def check_again(self) -> None:
        """"Check Again" in the menu."""
        self.log("Checking for the phone…")
        self.refresh_now()

    def refresh_now(self) -> None:
        with self._lock:
            self._force_check = True
        self._wake.set()

    # ---- log --------------------------------------------------------------------------------

    def log(self, text: str) -> None:
        now = datetime.datetime.now()
        self.log_lines.append((now, text))
        if len(self.log_lines) > 300:
            del self.log_lines[:len(self.log_lines) - 300]
        if self.verbose:
            sys.stderr.write("[%s] %s\n" % (now.astimezone().isoformat(timespec="seconds"), text))
            sys.stderr.flush()
        for hook in list(self.log_hooks):
            try:
                hook(now, text)
            except Exception:
                traceback.print_exc(file=sys.stderr)
        self._changed()

    def _log_async(self, text: str) -> None:
        self.host.post(lambda: self.log(text))

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                traceback.print_exc(file=sys.stderr)

    # ---- background loop --------------------------------------------------------------------

    def _tick(self) -> None:
        """Progress marker for the watchdog. Written from the loop thread itself (not via the UI
        thread), so a long modal dialog on the UI thread can't make a healthy loop look stuck."""
        with self._lock:
            self._last_tick = time.monotonic()

    def _seconds_since_tick(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_tick

    def _loop(self, gen: int) -> None:
        try:
            self._loop_body(gen)
        except Exception:
            # Let the watchdog notice (no more ticks) and start over.
            traceback.print_exc(file=sys.stderr)

    def _loop_body(self, gen: int) -> None:
        last_check = float("-inf")
        port: Optional[int] = None
        ready: Optional[Device] = None
        adb: Optional[Adb] = None
        while not self._stopping and gen == self._generation:
            self._tick()
            self._wake.clear()
            with self._lock:
                due = self._force_check or time.monotonic() - last_check >= self._check_interval
                self._force_check = False

            if due:
                last_check = time.monotonic()
                located = self._locate(self.prefs.adb_path)
                found = Adb(located) if located else None
                adb = found
                devs: List[Device] = []
                chosen: Optional[Device] = None
                version: Optional[str] = None
                port = None
                if found is not None:
                    devs = found.devices()
                    chosen = self._choose(devs)
                    if chosen is not None:
                        version = found.installed_version(chosen.serial, PHONE_PACKAGE)
                        if version is not None:
                            port = found.ensure_forward(chosen.serial)
                ready = chosen if (version is not None and port is not None) else None
                self._tick()
                self.host.post(lambda f=found, d=devs, c=chosen, v=version, g=gen:
                               self._publish(f, d, c, v) if g == self._generation else None)

            if port is not None and ready is not None and adb is not None:
                link = PhoneLink()
                closed = threading.Event()
                device = ready
                link.on_message = lambda msg, l=link, d=device: self._handle(msg, l, d)

                def on_close(l=link, ev=closed):
                    self.host.post(lambda: self._link_closed(l))
                    ev.set()

                link.on_close = on_close
                if link.open(port):
                    self.host.post(lambda l=link: self._link_opened(l))
                    # adb can hold a connection open without it reaching the phone (for instance
                    # while a replugged phone is still reconnecting), so keep checking that the
                    # phone is really there rather than waiting for the link to end on its own.
                    while not closed.wait(self._link_recheck):
                        self._tick()
                        if self._stopping or gen != self._generation:
                            link.close()
                            break
                        here = adb.devices()
                        if not any(d.serial == device.serial and d.is_usable for d in here):
                            link.close()
                            break
                    last_check = float("-inf")  # look at the phone again straight away
                    continue
            self._wake.wait(1)

    def _choose(self, devs: List[Device]) -> Optional[Device]:
        usable = [d for d in devs if d.is_usable]
        selected = self.prefs.selected_serial
        if selected:
            for d in usable:
                if d.serial == selected:
                    return d
        return usable[0] if usable else None

    # ---- UI-thread publishing ---------------------------------------------------------------

    def _publish(self, adb: Optional[Adb], devices: List[Device], device: Optional[Device],
                 version: Optional[str]) -> None:
        old_path = self.adb.path if self.adb else None
        new_path = adb.path if adb else None
        changed = (old_path != new_path or self.devices != devices or self.device != device
                   or self.phone_version != version)
        if old_path != new_path:
            self.log("adb not found." if adb is None else "Using adb at %s." % adb.path)
        if self.devices != devices and devices:
            self.log("adb lists: " + ", ".join("%s (%s)" % (d.serial, d.state) for d in devices))
        if self.device != device:
            self.log("Phone: %s (%s)" % (device.title, device.serial) if device else "No phone.")
        if self.phone_version != version and device is not None:
            self.log("Login Relay %s is on the phone." % version if version is not None
                     else "Login Relay isn't on the phone.")
        self.adb = adb
        self.devices = devices
        self.device = device
        self.phone_version = version
        if changed:
            self._changed()
        if (device is None or version is None) and self.active is not None:
            self.cancel_active("Phone not available", notify_phone=False)

    def _link_opened(self, link: PhoneLink) -> None:
        if link.closed:
            return  # already over (its close notice may have been handled first); nothing to track
        self._current_link = link
        self.linked = True
        self.log("Linked to Login Relay on the phone.")
        self._changed()

    def _link_closed(self, link: PhoneLink) -> None:
        if self._current_link is link:
            self._current_link = None
            self.linked = False
            self.log("Link to the phone closed.")
        if self.active is not None and self.active.link is link:
            self.cancel_active("The phone closed Login Relay.", notify_phone=False)
        self._changed()

    # ---- messages from the phone (read thread) ----------------------------------------------

    def _handle(self, msg: dict, link: PhoneLink, device: Device) -> None:
        kind = msg.get("type")
        if kind == "open":
            url = msg.get("url")
            if not isinstance(url, str):
                return
            referrer, label = msg.get("referrer"), msg.get("label")
            request = RelayRequest(url, referrer if isinstance(referrer, str) else None,
                                   label if isinstance(label, str) else None)
            self.host.post(lambda: self._begin(request, link, device))
        elif kind == "cancel":
            def cancel():
                if self.active is not None and self.active.link is link:
                    self.cancel_active("Cancelled on the phone.", notify_phone=False)
            self.host.post(cancel)

    # ---- one login --------------------------------------------------------------------------

    def _begin(self, request: RelayRequest, link: PhoneLink, device: Device) -> None:
        if self._stopping:
            return
        if self.active is not None:
            self.cancel_active("Replaced by a newer request.", notify_phone=False)
        at = " at %s" % request.host if request.host else ""
        self.log("%s wants to open a %s page%s." % (request.app_name, request.verb, at))

        if request.kind == PLAIN_LINK:
            if _openable(request.url):
                self.host.open_url(request.url)
            link.send({"type": "status", "text": "Opened on your computer."})
            self.host.call_later(1.0, lambda: link.send({"type": "done"}))
            return

        if request.lacks_pkce and not self.host.confirm_without_pkce(request):
            self.log("Declined: the sign-in doesn't use PKCE.")
            link.send({"type": "error", "text": "Cancelled on the computer."})
            return
        if link.closed:
            return  # the phone hung up while the question was open

        flow = Flow(request, device, link)
        self.active = flow
        scheme = request.catchable_scheme
        if scheme is not None:
            try:
                flow.claim = self.host.claim(scheme)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                flow.claim = None
            if flow.claim is None:
                self.log("Linux wouldn't let Login Relay take over %s://, so you'll paste the address instead."
                         % scheme)

        def timed_out():
            if self.active is flow:
                flow.link.send({"type": "error", "text": "Timed out waiting for the browser."})
                self.cancel_active("Timed out waiting for the browser.", notify_phone=True)

        flow.timeout = self.host.call_later(self._login_timeout, timed_out)
        link.send({"type": "status",
                   "text": "Finish signing in on your computer." if request.kind == SIGN_IN
                   else "Finishing sign-out on your computer."})
        # Test hook: the end-to-end tests set LOGINRELAY_BROWSER=none so no browser opens.
        if os.environ.get("LOGINRELAY_BROWSER") != "none" and _openable(request.url):
            self.host.open_url(request.url)
        self.log("Opened the page in your browser. If it asks to open Login Relay, allow it.")
        self.notify("%s %s" % (request.app_name, request.verb), "Continue in your browser.")
        self._changed()
        if flow.claim is None:
            self.host.post(lambda: self._ask_for_pasted_address(flow))

    def receive(self, callback: str) -> None:
        """Called with every address the system hands us. Only the one that matches the login in
        progress counts."""
        flow = self.active
        if flow is None:
            return
        verdict = flow.request.accepts(callback)
        if verdict == UNRELATED:
            return
        if verdict == STATE_MISMATCH:
            self.log("Ignored a return address from a different sign-in (state mismatch).")
        elif verdict == OK:
            self._finish(flow, callback)

    def _finish(self, flow: Flow, callback: str) -> None:
        if self.active is not flow:
            return
        self._end(flow)
        params = url_params(callback)
        if "error" in params:
            self.log("The page returned an error: %s" % params.get("error_description", params["error"]))
        self.log("Returning to %s on the phone…" % flow.request.app_name)
        self._changed()
        adb = self.adb
        if adb is None:
            return

        def work():
            r = adb.deliver(flow.device.serial, callback, flow.request.caller)

            def done():
                if not r.ok or "Error" in r.out:
                    self.log("The phone couldn't take the result: %s" % (r.out or r.err))
                    flow.link.send({"type": "error", "text": "Couldn't hand the result back to the app."})
                    return
                self.log("Done: %s is finishing the %s." % (flow.request.app_name, flow.request.verb))
                # Let the app's own screen take the result before Login Relay closes.
                self.host.call_later(self._done_delay, lambda: flow.link.send({"type": "done"}))

            self.host.post(done)

        self._background(work)

    def _end(self, flow: Flow) -> None:
        """Common clean-up when a login stops being active."""
        self.active = None
        _cancel(flow.timeout)
        if flow.claim is not None:
            try:
                flow.claim.release()
            except Exception:
                traceback.print_exc(file=sys.stderr)
        if flow.paste_open:
            flow.paste_open = False
            self.host.close_pasted_address(flow)

    def cancel_active(self, reason: str, notify_phone: bool) -> None:
        flow = self.active
        if flow is None:
            return
        self._end(flow)
        self.log(reason)
        if notify_phone:
            flow.link.send({"type": "done"})
        self._changed()

    # ---- prompts ----------------------------------------------------------------------------

    def _ask_for_pasted_address(self, flow: Flow) -> None:
        """For return addresses that are ordinary web links, which the system can't hand to us."""
        if self.active is not flow or flow.request.redirect is None:
            return
        flow.paste_open = True
        self.host.ask_for_pasted_address(flow)

    def submit_pasted(self, flow: Flow, text: str) -> str:
        """The user pasted an address. Returns "ok" (the login finishes), "unrelated" or "stateMismatch"."""
        pasted = text.strip()
        if self.active is not flow:
            return UNRELATED
        verdict = flow.request.accepts(pasted)
        if verdict == OK:
            self._finish(flow, pasted)
        return verdict

    def cancel_pasted(self, flow: Flow) -> None:
        flow.paste_open = False  # the prompt is already closing itself
        if self.active is flow:
            self.cancel_active("Cancelled on the computer.", notify_phone=True)

    # ---- phone-side app management ----------------------------------------------------------

    def _background(self, fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name="relay-work", daemon=True).start()

    def install_phone_app(self) -> None:
        adb, device, apk = self.adb, self.device, self.bundled_apk
        if adb is None or device is None or apk is None:
            return
        self.busy = "Installing Login Relay on %s…" % device.title
        self.log(self.busy)
        self._changed()

        def work():
            r = adb.install(device.serial, apk)

            def done():
                self.busy = None
                if r.ok and "Success" in r.out:
                    self.log("Installed.")
                else:
                    why = " ".join(x for x in (r.out, r.err) if x)
                    self.log("Install failed: %s" % why)
                    self.host.show_error("Couldn't install Login Relay on the phone", why)
                self._changed()
                self.refresh_now()

            self.host.post(done)

        self._background(work)

    def open_on_phone(self) -> None:
        adb, device = self.adb, self.device
        if adb is None or device is None:
            return
        self._background(lambda: adb.shell(
            device.serial, "input keyevent KEYCODE_WAKEUP; am start -n %s/.RelayActivity" % PHONE_PACKAGE))

    def download_platform_tools(self) -> None:
        from . import installer
        self.busy = "Downloading Android platform-tools…"
        self.log(self.busy)
        self._changed()

        def work():
            failure = None
            try:
                installer.install(self._log_async)
            except Exception as e:
                failure = str(e) or e.__class__.__name__

            def done():
                self.busy = None
                if failure is not None:
                    self.log("Download failed: %s" % failure)
                    self.host.show_error("Couldn't download platform-tools", failure)
                self._changed()
                self.refresh_now()

            self.host.post(done)

        self._background(work)

    def select_device(self, serial: str) -> None:
        self.prefs.selected_serial = serial
        self.refresh_now()

    def choose_adb(self, path: str) -> None:
        self.prefs.adb_path = path
        self.refresh_now()

    # ---- notifications ----------------------------------------------------------------------

    def notify(self, title: str, body: str) -> None:
        if self.prefs.notifications:
            self.host.notify(title, body)

    def show_error(self, title: str, detail: str) -> None:
        self.host.show_error(title, detail)


# ---- bundled phone app -----------------------------------------------------------------------

def find_bundled(env=None) -> Tuple[Optional[str], Optional[str]]:
    """(path of login-relay.apk, its version) as shipped with this app, if present."""
    apk = find_share_file("login-relay.apk", env)
    version = None
    vf = find_share_file("phone-app-version.txt", env)
    if vf:
        try:
            with open(vf, encoding="utf-8") as f:
                version = f.read().strip() or None
        except OSError:
            pass
    return apk, version


# ---- text and layout shared by the window and the menu (ActivityWindowController.describe etc.) ----

def describe(engine: RelayEngine) -> Tuple[str, str]:
    """Headline and one line of explanation for the current phase."""
    if engine.busy:
        return engine.busy, "This takes a moment."
    ph = engine.phase
    d = ph.device
    if ph.kind == NO_ADB:
        return ("adb not found", "Login Relay talks to the phone through adb, part of Android platform-tools.")
    if ph.kind == NO_PHONE:
        return ("No phone connected",
                "Plug the phone in with USB and turn on USB debugging (Settings → Developer options).")
    if ph.kind == UNAUTHORIZED:
        if d.state == "unauthorized":
            return ("%s is waiting for you" % d.title, "Tap Allow on the phone's \"Allow USB debugging?\" prompt.")
        return ("%s isn't responding (%s)" % (d.title, d.state),
                "Unplug the cable and plug it back in, then choose Check Again.")
    if ph.kind == NEEDS_APP:
        return ("Login Relay isn't on %s" % d.title,
                "Install it on the phone to start." if engine.bundled_apk
                else "Install login-relay.apk on the phone.")
    if ph.kind == READY:
        outdated = " An update for the phone app is available." if engine.phone_app_is_outdated else ""
        return ("%s is ready" % d.title, "Sign in to an app on the phone and the page opens here.%s" % outdated)
    flow = ph.flow
    return ("%s %s in progress" % (flow.request.app_name, flow.request.verb),
            "Finish in your browser. If it asks to open Login Relay, allow it.")


# Action ids understood by the UI layer.
A_DOWNLOAD = "download_platform_tools"
A_CHOOSE_ADB = "choose_adb"
A_INSTALL = "install_phone_app"
A_OPEN = "open_on_phone"
A_CANCEL = "cancel"
A_CHECK = "check_again"
A_SHOW = "show_activity"
A_LAUNCH_AT_LOGIN = "toggle_launch_at_login"
A_NOTIFY = "toggle_notifications"
A_ABOUT = "about"
A_QUIT = "quit"
A_SELECT_PHONE = "select_phone"


def window_actions(engine: RelayEngine) -> Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]], bool]:
    """(primary button, secondary button, enabled) for the activity window; a button is (label, action)."""
    primary = secondary = None
    ph = engine.phase
    if ph.kind == NO_ADB:
        primary, secondary = ("Download Platform-Tools…", A_DOWNLOAD), ("Choose adb…", A_CHOOSE_ADB)
    elif ph.kind == NEEDS_APP:
        if engine.bundled_apk:
            primary = ("Install on Phone", A_INSTALL)
    elif ph.kind == READY:
        primary = ("Open on Phone", A_OPEN)
        if engine.phone_app_is_outdated:
            secondary = ("Update on Phone", A_INSTALL)
    elif ph.kind == WORKING:
        primary = ("Cancel", A_CANCEL)
    else:  # no phone, unauthorized
        primary = ("Check Again", A_CHECK)
    return primary, secondary, engine.busy is None


@dataclass(frozen=True)
class MenuEntry:
    kind: str  # "status" | "hint" | "separator" | "action" | "toggle" | "submenu"
    label: str = ""
    action: str = ""
    arg: str = ""
    checked: bool = False
    good: bool = False
    children: tuple = ()


def menu_model(engine: RelayEngine, launch_at_login: bool = False) -> Tuple[MenuEntry, ...]:
    """The tray menu (AppDelegate.menuNeedsUpdate), as plain data so it can be tested and compared."""
    items: List[MenuEntry] = []
    status, detail = describe(engine)
    ph = engine.phase
    good = ph.kind in (READY, WORKING)
    items.append(MenuEntry("status", status, good=good))
    if not good and engine.busy is None:
        items.append(MenuEntry("hint", detail))
    items.append(MenuEntry("separator"))

    if engine.busy is None:
        if ph.kind == NO_ADB:
            items.append(MenuEntry("action", "Download Android Platform-Tools…", A_DOWNLOAD))
            items.append(MenuEntry("action", "Choose adb…", A_CHOOSE_ADB))
        elif ph.kind == NEEDS_APP:
            if engine.bundled_apk:
                items.append(MenuEntry("action", "Install Login Relay on Phone", A_INSTALL))
        elif ph.kind == READY:
            items.append(MenuEntry("action", "Open Login Relay on Phone", A_OPEN))
            if engine.phone_app_is_outdated and engine.phone_version and engine.bundled_phone_version:
                items.append(MenuEntry("action", "Update Login Relay on Phone (%s → %s)"
                                       % (engine.phone_version, engine.bundled_phone_version), A_INSTALL))
        elif ph.kind == WORKING:
            items.append(MenuEntry("action", "Cancel %s" % ph.flow.request.verb, A_CANCEL))
        else:
            items.append(MenuEntry("action", "Check Again", A_CHECK))

    usable = [d for d in engine.devices if d.is_usable]
    if len(usable) > 1:
        kids = tuple(MenuEntry("toggle", "%s (%s)" % (d.title, d.serial), A_SELECT_PHONE, d.serial,
                               checked=(d == engine.device)) for d in usable)
        items.append(MenuEntry("submenu", "Phone", children=kids))
    if items[-1].kind != "separator":
        items.append(MenuEntry("separator"))

    items.append(MenuEntry("action", "Show Activity…", A_SHOW))
    items.append(MenuEntry("separator"))
    items.append(MenuEntry("toggle", "Launch at Login", A_LAUNCH_AT_LOGIN, checked=launch_at_login))
    items.append(MenuEntry("toggle", "Notify When a Sign-in Arrives", A_NOTIFY, checked=engine.prefs.notifications))
    items.append(MenuEntry("separator"))
    items.append(MenuEntry("action", "About Login Relay", A_ABOUT))
    items.append(MenuEntry("action", "Quit Login Relay", A_QUIT))
    return tuple(items)


def paste_prompt(flow: Flow) -> Tuple[str, str, str]:
    """(title, explanation, placeholder) for the "paste the address" prompt."""
    redirect = flow.request.redirect or ""
    first = redirect.split("?")[0] if redirect else ""
    return ("Paste the address you ended up on",
            "After you sign in, copy the full address from your browser's address bar. It starts with %s."
            % (first or redirect), redirect)


def paste_feedback(verdict: str) -> str:
    if verdict == STATE_MISMATCH:
        return "That address is from a different sign-in. Try again."
    return "That isn't this app's return address. Try again."
