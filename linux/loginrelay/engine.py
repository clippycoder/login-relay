# SPDX-License-Identifier: GPL-3.0-or-later
"""Watches for a phone, keeps a link to the Login Relay app on it, and runs each login: opens the
page in the browser, catches the redirect, and hands it back to the phone.

Port of macos/Sources/RelayEngine.swift. UI-agnostic: it talks to the world only through a `Host`.
State lives on the UI thread (everything the engine changes goes through host.post); one background
thread polls adb (or the USB bus, in USB accessory mode) and owns the link.
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
from typing import Callable, Dict, List, Optional, Tuple

from . import PHONE_PACKAGE, usbaoa
from .adb import Adb, Device
from .phonelink import PhoneLink
from .prefs import Prefs, find_share_file
from .redirect import (OK, PLAIN_LINK, SIGN_IN, STATE_MISMATCH, UNRELATED, RelayRequest,
                       url_params, url_scheme, version_less)
from .usblink import UsbPhoneLink

LOGIN_TIMEOUT = 15 * 60

NO_ADB = "noAdb"
NO_PHONE = "noPhone"
UNAUTHORIZED = "unauthorized"
NEEDS_APP = "needsApp"
CONNECTING = "connecting"   # USB mode: a phone is plugged in but Login Relay on it isn't answering yet
READY = "ready"
WORKING = "working"

ADB = "adb"
USB = "usb"

USB_WAITING = ("Login Relay on the phone isn't answering yet. If Android asks to allow USB access, tap OK; "
               "otherwise just start a sign-in on the phone and Login Relay starts by itself.")
DELIVERY_TIMEOUT = 10.0       # how long to wait for the phone to confirm it handed the result to the app
USB_SWITCH_INTERVAL = 20.0    # at most one accessory switch attempt per phone per this many seconds

PhaseInfo = namedtuple("PhaseInfo", "kind device flow detail", defaults=(None,))


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

    def __init__(self, request: RelayRequest, device: Device, link):
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
                 locate: Optional[Callable[[Optional[str]], Optional[str]]] = None,
                 usb_backend: Optional[usbaoa.UsbBackend] = None, usb_host_name: Optional[str] = None,
                 usb_idle_pause: float = 1.0, usb_recheck: float = 1.0,
                 usb_switch_interval: float = USB_SWITCH_INTERVAL, usb_switch_poll: float = 0.2,
                 usb_switch_polls: int = 60, usb_open_attempts: int = 6, usb_open_wait: float = 0.5,
                 usb_error_pause: float = 3.0, delivery_timeout: float = DELIVERY_TIMEOUT,
                 usb_link_args: Optional[dict] = None):
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
        self._usb_backend = usb_backend or usbaoa.default_backend()
        self._usb_host_name = usb_host_name or usbaoa.local_host_name()
        self._usb_idle_pause = usb_idle_pause
        self._usb_recheck = usb_recheck
        self._usb_switch_interval = usb_switch_interval
        self._usb_switch_poll = usb_switch_poll
        self._usb_switch_polls = usb_switch_polls
        self._usb_open_attempts = usb_open_attempts
        self._usb_open_wait = usb_open_wait
        self._usb_error_pause = usb_error_pause
        self._delivery_timeout = delivery_timeout
        self._usb_link_args = usb_link_args or {}

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
        # USB accessory mode (UI thread)
        self.usb_candidates: List[usbaoa.UsbDevice] = []
        self.usb_detail = ""
        self.usb_alive = False
        self.usb_ever_alive = False   # the phone app has answered on this connection (it may have gone idle since)
        self._awaiting_delivery: Optional[Flow] = None
        self._delivery_timer = None

        # background loop state
        self._stopping = False
        self._generation = 0  # bumped to abandon a stuck loop thread
        self._last_tick = time.monotonic()  # last time the loop made progress
        self._watchdog = None
        self._force_check = False
        self._wake = threading.Event()
        self._current_link = None  # PhoneLink or UsbPhoneLink
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ---- derived state ----------------------------------------------------------------------

    @property
    def transport(self) -> str:
        """"adb" or "usb". Test hook: LOGINRELAY_TRANSPORT=adb|usb overrides the saved choice."""
        forced = os.environ.get("LOGINRELAY_TRANSPORT")
        if forced in (ADB, USB):
            return forced
        return self.prefs.transport

    @property
    def phase(self) -> PhaseInfo:
        if self.transport == USB:
            return self._usb_phase
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
    def _usb_phase(self) -> PhaseInfo:
        if self.device is None:
            return PhaseInfo(NO_PHONE, None, None)
        if self.active is not None:
            return PhaseInfo(WORKING, self.device, self.active)
        if self.usb_alive or self.usb_ever_alive:
            return PhaseInfo(READY, self.device, None)
        return PhaseInfo(CONNECTING, self.device, None, self.usb_detail)

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
        usb = self.transport == USB
        t = threading.Thread(target=self._loop, args=(gen, usb), name="relay-loop", daemon=True)
        t.start()
        self._thread = t

    def shutdown(self) -> None:
        self._stopping = True
        _cancel(self._watchdog)
        self.cancel_active("Quit", notify_phone=True)
        _cancel(self._delivery_timer)
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

    def _loop(self, gen: int, usb: bool = False) -> None:
        try:
            if usb:
                self._usb_loop_body(gen)
            else:
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

    def _link_opened(self, link) -> None:
        if link.closed:
            return  # already over (its close notice may have been handled first); nothing to track
        self._current_link = link
        self.linked = True
        self.log("USB connection open." if isinstance(link, UsbPhoneLink) else "Linked to Login Relay on the phone.")
        self._changed()

    def _link_closed(self, link) -> None:
        if self._current_link is link:
            self._current_link = None
            self.linked = False
            if isinstance(link, UsbPhoneLink):
                self.usb_alive = False
                self.usb_ever_alive = False
                self.phone_version = None
            self.log("Link to the phone closed.")
        if self.active is not None and self.active.link is link:
            self.cancel_active("The phone closed Login Relay.", notify_phone=False)
        self._changed()

    # ---- messages from the phone (read thread) ----------------------------------------------

    def _handle(self, msg: dict, link, device: Device) -> None:
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
        elif kind == "hello":
            version = msg.get("version")
            version = version if isinstance(version, str) else None
            transport = msg.get("transport")

            def hello():
                if device.is_usb:
                    self.phone_version = version
                self.log("Login Relay %s on the phone says hello (%s)."
                         % (version if version is not None else "?",
                            transport if isinstance(transport, str) else "?"))
            self.host.post(hello)
        elif kind == "delivered":
            self.host.post(self._delivery_confirmed)
        elif kind == "error":
            text = msg.get("text")
            self.host.post(lambda: self._phone_reported_error(text if isinstance(text, str) else "unknown error"))

    # ---- one login --------------------------------------------------------------------------

    def _begin(self, request: RelayRequest, link, device: Device) -> None:
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
        if flow.device.is_usb:
            # No adb here: the phone checks the result itself and hands it to the app.
            self._awaiting_delivery = flow
            _cancel(self._delivery_timer)

            def not_confirmed():
                if self._awaiting_delivery is None:
                    return
                self._awaiting_delivery = None
                self.log("The phone didn't confirm it handed the result to the app.")

            self._delivery_timer = self.host.call_later(self._delivery_timeout, not_confirmed)
            flow.link.send({"type": "callback", "url": callback})
            return
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

    def _delivery_confirmed(self) -> None:
        flow = self._awaiting_delivery
        if flow is None:
            return
        self._awaiting_delivery = None
        _cancel(self._delivery_timer)
        self.log("Done: %s is finishing the %s." % (flow.request.app_name, flow.request.verb))

    def _phone_reported_error(self, text: str) -> None:
        self.log("The phone says: %s" % text)
        if self._awaiting_delivery is not None:
            self._awaiting_delivery = None
            _cancel(self._delivery_timer)

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

    # ---- USB accessory mode -----------------------------------------------------------------

    def set_transport(self, transport: str) -> None:
        """Switches between adb and USB accessory mode."""
        if transport not in (ADB, USB) or transport == self.transport:
            return
        self.cancel_active("Switching connection type.", notify_phone=True)
        _cancel(self._delivery_timer)
        self._awaiting_delivery = None
        if self._current_link is not None:
            self._current_link.close()
            self._current_link = None   # its late close notice then changes nothing
        self.prefs.transport = transport
        self.adb = None
        self.devices = []
        self.device = None
        self.phone_version = None
        self.linked = False
        self.usb_candidates = []
        self.usb_alive = False
        self.usb_ever_alive = False
        self.usb_detail = ""
        self.log("Connecting over USB accessory mode (no USB debugging needed)." if transport == USB
                 else "Connecting over adb.")
        self._start_loop()
        self._changed()

    def select_usb(self, location: str) -> None:
        self.prefs.usb_selected = location
        self.refresh_now()

    def _apply_usb(self, device: Optional[Device], candidates: List[usbaoa.UsbDevice], detail: str) -> None:
        changed = self.device != device or self.usb_candidates != candidates or self.usb_detail != detail
        if self.device != device:
            self.log("USB phone: %s" % device.title if device is not None else "No USB phone.")
        self.device = device
        self.usb_candidates = candidates
        self.usb_detail = detail
        if changed:
            self._changed()
        if device is None and self.active is not None:
            self.cancel_active("Phone not available", notify_phone=False)

    def _usb_alive_changed(self, alive: bool, link) -> None:
        if self._current_link is not link or self.usb_alive == alive:
            return
        self.usb_alive = alive
        if alive:
            self.usb_ever_alive = True
        # Android reclaims an idle Login Relay on a small phone; the next sign-in starts it again and it
        # reopens the pipe and says hello by itself, so going quiet is not a reason to drop the connection.
        self.log("Login Relay on the phone is answering." if alive
                 else "Login Relay on the phone went idle (it starts again by itself when an app asks to sign in).")
        if not alive:
            self.phone_version = None
            if self.active is not None:
                self.cancel_active("Login Relay on the phone stopped answering.", notify_phone=False)
        self._changed()

    def _usb_loop_body(self, gen: int) -> None:
        """Background loop for USB accessory mode: find a phone, switch it to accessory mode, open the
        pipe, and keep it open until the cable comes out."""
        backend = self._usb_backend
        last_switch: Dict[str, float] = {}

        def current() -> bool:
            return not self._stopping and gen == self._generation

        def pause(seconds: float) -> None:
            if self._wake.wait(seconds):
                self._wake.clear()

        def publish(device, candidates, detail) -> None:
            self.host.post(lambda: self._apply_usb(device, candidates, detail) if gen == self._generation else None)

        def listing() -> Optional[List[usbaoa.UsbDevice]]:
            """The bus, or None (after publishing why) if it can't be read."""
            try:
                return backend.list_devices()
            except usbaoa.UsbError as e:
                if e.code in (usbaoa.AOA_ERR_UNAVAILABLE, usbaoa.AOA_ERR_PERMISSION):
                    publish(None, [], usbaoa.error_text(e.code, "", e.detail))
                else:
                    publish(None, [], "Couldn't read the USB devices (%s)." % (e.detail or "error %d" % e.code))
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                publish(None, [], "Couldn't read the USB devices (%s)." % (e or e.__class__.__name__))
            return None

        while current():
            self._tick()
            with self._lock:
                forced = self._force_check
                self._force_check = False
            if forced:
                last_switch.clear()     # "Check Again" means try the switch again now
            everything = listing()
            if everything is None:
                pause(self._usb_error_pause)
                continue
            # A phone that was unplugged gets a fresh try when it comes back.
            for location in list(last_switch):
                if not any(d.location == location for d in everything):
                    del last_switch[location]
            candidates = [d for d in everything if d.looks_android and not d.is_accessory]
            target = next((d for d in everything if d.is_accessory), None)
            if target is None:
                selected = self.prefs.usb_selected
                picked = next((c for c in candidates if c.id == selected), None) if selected else None
                if picked is not None:
                    target = picked
                elif len(candidates) == 1:
                    target = candidates[0]
            if target is None:
                publish(None, candidates, "Choose which phone to use from the menu." if len(candidates) > 1 else "")
                pause(self._usb_idle_pause)
                continue
            device = Device("usb:" + target.id, "device", target.name.replace(" ", "_"))

            if not target.is_accessory:
                last = last_switch.get(target.location)
                if last is not None and time.monotonic() - last < self._usb_switch_interval:
                    pause(self._usb_idle_pause)
                    continue
                last_switch[target.location] = time.monotonic()
                publish(device, candidates, "Asking %s to switch to USB accessory mode…" % target.name)
                try:
                    r = backend.switch(target.location, self._usb_host_name)
                except Exception as e:
                    traceback.print_exc(file=sys.stderr)
                    r = usbaoa.SwitchResult(usbaoa.AOA_ERR_IO, 0, str(e))
                if r.code != usbaoa.AOA_OK:
                    publish(device, candidates, usbaoa.error_text(r.code, target.name, r.detail))
                    pause(self._usb_error_pause)
                    continue
                # Android gives up if the host hasn't reconnected within 10 s.
                appeared = False
                for _ in range(self._usb_switch_polls):
                    if not current():
                        return
                    time.sleep(self._usb_switch_poll)
                    self._tick()
                    try:
                        now_on_bus = backend.list_devices()
                    except Exception:
                        continue
                    if any(d.location == target.location and d.is_accessory for d in now_on_bus):
                        appeared = True
                        break
                if not appeared:
                    publish(device, candidates, "The phone didn't switch to accessory mode.")
                    continue

            conn = None
            for attempt in range(self._usb_open_attempts):
                if not current():
                    return
                failure = None
                try:
                    conn = backend.open(target.location)
                    break
                except usbaoa.UsbError as e:
                    failure = e
                except Exception as e:
                    traceback.print_exc(file=sys.stderr)
                    failure = usbaoa.UsbError(usbaoa.AOA_ERR_OPEN, str(e))
                if failure.code in (usbaoa.AOA_ERR_PERMISSION, usbaoa.AOA_ERR_UNAVAILABLE):
                    publish(device, candidates, usbaoa.error_text(failure.code, target.name, failure.detail))
                    break   # retrying won't help
                if attempt == self._usb_open_attempts - 1:
                    publish(device, candidates,
                            "Couldn't open the accessory connection (error %d)." % failure.code)
                time.sleep(self._usb_open_wait)
            if conn is None:
                pause(self._usb_error_pause)
                continue

            link = UsbPhoneLink(conn, **self._usb_link_args)
            closed = threading.Event()
            link.on_message = lambda msg, l=link, d=device: self._handle(msg, l, d)
            link.on_alive = lambda alive, l=link: self.host.post(lambda: self._usb_alive_changed(alive, l))

            def on_close(l=link, ev=closed):
                self.host.post(lambda: self._link_closed(l))
                ev.set()

            link.on_close = on_close
            publish(device, candidates, USB_WAITING)
            self.host.post(lambda l=link: self._link_opened(l))
            link.start()
            while not closed.wait(self._usb_recheck):
                self._tick()
                if not current():
                    link.close()
                    break
                try:
                    here = backend.list_devices()
                except Exception:
                    continue    # can't tell; the read loop notices a dead pipe on its own
                if not any(d.location == target.location and d.is_accessory for d in here):
                    link.close()
                    break
            closed.wait(2)      # let the pipe be released before the next open

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
        if engine.transport == USB:
            if len(engine.usb_candidates) > 1:
                return ("More than one phone found over USB",
                        engine.usb_detail or "Choose which phone to use from the menu.")
            return ("No phone found over USB",
                    engine.usb_detail or
                    "Plug the phone in and choose \u201cFile transfer\u201d in its USB notification. "
                    "USB debugging isn't needed.")
        return ("No phone connected",
                "Plug the phone in with USB and turn on USB debugging (Settings → Developer options).")
    if ph.kind == CONNECTING:
        # Once the USB pipe is open the phone is plugged in and connected; it's just not answering yet.
        return ("%s is connected over USB" % d.title if engine.linked else "Connecting to %s…" % d.title,
                ph.detail or "Waiting for the phone.")
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
        outdated = (" An update for the phone app is available."
                    if engine.transport == ADB and engine.phone_app_is_outdated else "")
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
A_SELECT_USB = "select_usb_phone"
A_SET_TRANSPORT = "set_transport"


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
        # Opening and updating the phone app go through adb, so they're for the adb connection only.
        if engine.transport == ADB:
            primary = ("Open on Phone", A_OPEN)
            if engine.phone_app_is_outdated:
                secondary = ("Update on Phone", A_INSTALL)
    elif ph.kind == WORKING:
        primary = ("Cancel", A_CANCEL)
    else:  # no phone, unauthorized, connecting
        primary = ("Check Again", A_CHECK)
    return primary, secondary, engine.busy is None


def transport_choices(engine: RelayEngine) -> Tuple["MenuEntry", ...]:
    """The "Connect Over" choices: how the computer reaches the phone. Used by the tray menu (as a
    submenu) and by the window (as a drop-down)."""
    current = engine.transport
    return tuple(MenuEntry("toggle", label, A_SET_TRANSPORT, t, checked=(t == current))
                 for t, label in ((ADB, "adb (needs USB debugging)"),
                                  (USB, "USB accessory (no USB debugging) \u2014 beta")))


def usb_phone_choices(engine: RelayEngine) -> Tuple["MenuEntry", ...]:
    """In USB mode with several candidate phones: one entry per phone, the chosen one checked."""
    if engine.transport != USB or len(engine.usb_candidates) <= 1:
        return ()
    current = engine.device.serial if engine.device is not None else None
    return tuple(MenuEntry("toggle", "%s (%s)" % (p.name, p.id) if p.name else p.id, A_SELECT_USB, p.id,
                           checked=("usb:" + p.id) == current) for p in engine.usb_candidates)


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
            if engine.transport == ADB:
                items.append(MenuEntry("action", "Open Login Relay on Phone", A_OPEN))
                if engine.phone_app_is_outdated and engine.phone_version and engine.bundled_phone_version:
                    items.append(MenuEntry("action", "Update Login Relay on Phone (%s → %s)"
                                           % (engine.phone_version, engine.bundled_phone_version), A_INSTALL))
        elif ph.kind == WORKING:
            items.append(MenuEntry("action", "Cancel %s" % ph.flow.request.verb, A_CANCEL))
        else:
            items.append(MenuEntry("action", "Check Again", A_CHECK))

    usb_phones = usb_phone_choices(engine)
    if usb_phones:
        items.append(MenuEntry("submenu", "Phone", children=usb_phones))
    usable = [d for d in engine.devices if d.is_usable]
    if len(usable) > 1:
        kids = tuple(MenuEntry("toggle", "%s (%s)" % (d.title, d.serial), A_SELECT_PHONE, d.serial,
                               checked=(d == engine.device)) for d in usable)
        items.append(MenuEntry("submenu", "Phone", children=kids))
    if items[-1].kind != "separator":
        items.append(MenuEntry("separator"))

    items.append(MenuEntry("submenu", "Connect Over", children=transport_choices(engine)))
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
