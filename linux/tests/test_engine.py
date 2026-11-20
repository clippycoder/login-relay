# SPDX-License-Identifier: GPL-3.0-or-later
"""Drives engine.py end to end through the LOGINRELAY_FAKE_PHONE test hook: a fake Host, a fake claim, and
a fake phone listening on a local TCP port, reached by the same USB-mode engine logic as a real phone."""
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import SCHEME, PACKAGE, FakePhoneServer, authorize_url, stable_fds, assert_fds_flat, wait_until  # noqa: E402

from loginrelay import engine as eng  # noqa: E402
from loginrelay.prefs import Prefs  # noqa: E402
from loginrelay import fakephone  # noqa: E402
from loginrelay.redirect import RelayRequest  # noqa: E402
from loginrelay.usblink import UsbPhoneLink  # noqa: E402


class FakeClaim:
    def __init__(self, scheme):
        self.scheme = scheme
        self.released = False

    def release(self):
        self.released = True


class FakeHost(eng.Host):
    """Has a real "UI thread" (a queue pump) so the engine's thread discipline is exercised."""

    def __init__(self):
        self.q = queue.Queue()
        self.ui = threading.Thread(target=self._pump, name="fake-ui", daemon=True)
        self.ui.start()
        self.opened, self.notifications, self.errors = [], [], []
        self.claims = []
        self.refuse_claims = False
        self.pkce_answer = False
        self.pkce_asked = []
        self.pasted_asked = []
        self.pasted_closed = []
        self.wrong_thread = []

    def _pump(self):
        while True:
            fn = self.q.get()
            if fn is None:
                return
            try:
                fn()
            except Exception as e:  # surface engine bugs in the test
                self.wrong_thread.append("exception on UI thread: %r" % e)

    def stop(self):
        self.q.put(None)

    def on_ui(self):
        return threading.current_thread() is self.ui

    def post(self, fn):
        self.q.put(fn)

    def ask(self, fn):
        """Run fn on the UI thread and return its result (what a button click would do)."""
        box = queue.Queue()
        self.post(lambda: box.put(fn()))
        return box.get(timeout=10)

    def open_url(self, url):
        self.opened.append(url)

    def claim(self, scheme):
        if not self.on_ui():
            self.wrong_thread.append("claim off the UI thread")
        if self.refuse_claims:
            return None
        c = FakeClaim(scheme)
        self.claims.append(c)
        return c

    def notify(self, title, body):
        self.notifications.append((title, body))

    def show_error(self, title, detail):
        self.errors.append((title, detail))

    def confirm_without_pkce(self, request):
        self.pkce_asked.append(request)
        return self.pkce_answer

    def ask_for_pasted_address(self, flow):
        self.pasted_asked.append(flow)

    def close_pasted_address(self, flow):
        self.pasted_closed.append(flow)


class EngineCase(unittest.TestCase):
    engine_args = dict(usb_idle_pause=0.1, usb_recheck=0.2, usb_open_wait=0.05, usb_error_pause=0.3,
                       delivery_timeout=3.0, usb_link_args=dict(ping_interval=0.2, silence=1.0))

    def setUp(self):
        saved = {k: os.environ.get(k) for k in ("LOGINRELAY_FAKE_PHONE", "LOGINRELAY_BROWSER")}
        self.addCleanup(self._restore_env, saved)
        os.environ["LOGINRELAY_BROWSER"] = "none"
        os.environ.pop("LOGINRELAY_FAKE_PHONE", None)
        self.phone = None
        self.host = FakeHost()
        self.addCleanup(self.host.stop)
        self.tmp = tempfile.mkdtemp(prefix="lr-eng-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prefs = Prefs(os.path.join(self.tmp, "prefs.json"))
        self.changes = 0

    @staticmethod
    def _restore_env(saved):
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def start_phone(self, **kw):
        """Starts a fake phone and points the hook at it. Call before start()."""
        self.phone = FakePhoneServer(**kw)
        self.addCleanup(self.phone.stop)
        os.environ["LOGINRELAY_FAKE_PHONE"] = self.phone.address
        return self.phone

    def make_engine(self, **extra):
        args = dict(self.engine_args)
        args.update(extra)
        # The engine gets no usb_backend: it must pick the fake phone up from the environment itself.
        self.engine = eng.RelayEngine(self.host, self.prefs, **args)

        def changed():
            if not self.host.on_ui():
                self.host.wrong_thread.append("on_change off the UI thread")
            self.changes += 1

        self.engine.on_change = changed
        return self.engine

    def start(self, **extra):
        self.make_engine(**extra)
        self.host.ask(self.engine.start)
        self.addCleanup(lambda: self.host.ask(self.engine.shutdown))

    def ui(self, fn):
        return self.host.ask(fn)

    def logs(self):
        return [t for _, t in self.engine.log_lines]

    def wait_log(self, text, timeout=10):
        wait_until(lambda: text in self.logs(), timeout, "log line %r" % text)

    def wait_phase(self, kind, timeout=15):
        wait_until(lambda: self.engine.phase.kind == kind, timeout, "phase %s (is %s)" % (kind, self.engine.phase.kind))

    def wait_active(self):
        wait_until(lambda: self.engine.active is not None, 20, "a login to start")
        return self.engine.active

    def tearDown(self):
        self.assertEqual(self.host.wrong_thread, [])


class LoginTests(EngineCase):
    def test_sign_in_with_a_decoy_then_the_right_return_address(self):
        url, redirect = authorize_url("STATE-1")
        phone = self.start_phone(url=url, label="Example App")
        self.prefs.notifications = True
        self.start()
        flow = self.wait_active()
        self.assertEqual(self.engine.phase.kind, eng.WORKING)
        self.assertEqual((flow.request.app_name, flow.request.caller), ("Example App", PACKAGE))
        # The claim was taken for the right scheme; LOGINRELAY_BROWSER=none keeps the browser shut.
        self.assertEqual([c.scheme for c in self.host.claims], [SCHEME])
        self.assertEqual(self.host.opened, [])
        self.assertIn(("Example App sign-in", "Continue in your browser."), self.host.notifications)
        self.assertIn("Example App wants to open a sign-in page at login.example.com.", self.logs())
        wait_until(lambda: phone.messages("status"), 5, "the status line")
        self.assertEqual(phone.messages("status")[0]["text"], "Finish signing in on your computer.")

        # A decoy with the wrong state, as a browser would hand it over: ignored.
        self.ui(lambda: self.engine.receive("%s?code=EVIL&state=other" % redirect))
        self.ui(lambda: self.engine.receive("com.other.app://x?state=STATE-1"))   # unrelated: silent
        time.sleep(0.5)
        self.assertIn("Ignored a return address from a different sign-in (state mismatch).", self.logs())
        self.assertEqual(phone.messages("callback"), [])
        self.assertIs(self.engine.active, flow)
        self.assertFalse(self.host.claims[0].released)

        good = "%s?code=GOOD&state=STATE-1" % redirect
        self.ui(lambda: self.engine.receive(good))
        self.assertIsNone(self.engine.active)
        self.assertTrue(self.host.claims[0].released)
        wait_until(lambda: phone.messages("callback"), 10, "the return address to reach the phone")
        self.assertEqual(phone.messages("callback"), [{"type": "callback", "url": good}])
        self.wait_log("Done: Example App is finishing the sign-in.")
        self.assertIn("Returning to Example App on the phone\u2026", self.logs())
        self.assertEqual(self.engine.phase.kind, eng.READY)
        # The phone forgot its pending page, so nothing starts another login.
        time.sleep(1.0)
        self.assertIsNone(self.engine.active)
        self.assertEqual(len(phone.messages("callback")), 1)

    def test_sign_out(self):
        url, redirect = authorize_url("", kind="signout")
        phone = self.start_phone(url=url)
        self.start()
        flow = self.wait_active()
        self.assertEqual(flow.request.verb, "sign-out")
        wait_until(lambda: phone.messages("status"), 5, "the status line")
        self.assertEqual(phone.messages("status")[0]["text"], "Finishing sign-out on your computer.")
        self.ui(lambda: self.engine.receive(redirect))
        wait_until(lambda: phone.messages("callback"), 10, "the callback")
        self.assertEqual(phone.messages("callback")[0]["url"], redirect)
        self.assertTrue(self.host.claims[0].released)
        self.assertEqual(self.host.notifications, [])   # notifications are off by default

    def test_page_with_error_is_reported_but_still_handed_over(self):
        url, redirect = authorize_url("S9")
        phone = self.start_phone(url=url)
        self.start()
        self.wait_active()
        self.ui(lambda: self.engine.receive("%s?error=access_denied&error_description=No+way&state=S9" % redirect))
        self.assertIn("The page returned an error: No way", self.logs())
        wait_until(lambda: phone.messages("callback"), 10, "the callback")

    def test_the_phone_reports_an_error_and_a_missing_confirmation_is_logged(self):
        url, redirect = authorize_url("S10")
        phone = self.start_phone(url=url, delivery_reply="error:That isn't the sign-in in progress.")
        self.start(delivery_timeout=0.8)
        self.wait_active()
        self.ui(lambda: self.engine.receive("%s?code=X&state=S10" % redirect))
        self.wait_log("The phone says: That isn't the sign-in in progress.")
        self.assertNotIn("Done: Example App is finishing the sign-in.", self.logs())
        time.sleep(1.2)
        self.assertNotIn("The phone didn't confirm it handed the result to the app.", self.logs())

    def test_delivery_timeout(self):
        url, redirect = authorize_url("S11")
        self.start_phone(url=url, delivery_reply=None)
        self.start(delivery_timeout=0.6)
        self.wait_active()
        self.ui(lambda: self.engine.receive("%s?code=X&state=S11" % redirect))
        self.wait_log("The phone didn't confirm it handed the result to the app.")
        self.assertNotIn("Done: Example App is finishing the sign-in.", self.logs())

    def test_cancel_from_the_phone(self):
        url, redirect = authorize_url("STATE-2")
        phone = self.start_phone(url=url, cancel_after=1.0)
        self.start()
        self.wait_active()
        wait_until(lambda: self.engine.active is None, 15, "the phone's cancel")
        self.assertIn("Cancelled on the phone.", self.logs())
        self.assertTrue(self.host.claims[0].released)
        self.ui(lambda: self.engine.receive("%s?code=LATE&state=STATE-2" % redirect))
        time.sleep(0.5)
        self.assertEqual(phone.messages("callback"), [])
        self.assertEqual(phone.messages("done"), [])   # the phone cancelled, nobody to tell

    def test_cancel_from_the_computer_closes_the_phones_screen(self):
        url, _ = authorize_url("STATE-3")
        phone = self.start_phone(url=url)
        self.start()
        self.wait_active()
        self.ui(lambda: self.engine.cancel_active("Cancelled on the computer.", notify_phone=True))
        self.assertTrue(self.host.claims[0].released)
        wait_until(lambda: phone.messages("done"), 5, "done")
        self.assertIn("Cancelled on the computer.", self.logs())

    def test_a_newer_request_replaces_the_one_in_progress(self):
        url, _ = authorize_url("STATE-4")
        self.start_phone(url=url)
        self.start()
        first = self.wait_active()
        request = RelayRequest(authorize_url("STATE-5")[0], "android-app://" + PACKAGE, None)
        self.ui(lambda: self.engine._begin(request, first.link, first.device))
        self.assertTrue(self.host.claims[0].released)
        self.assertIn("Replaced by a newer request.", self.logs())
        self.assertIsNot(self.engine.active, first)
        self.assertEqual(len(self.host.claims), 2)

    def test_phone_vanishing_ends_the_login(self):
        url, _ = authorize_url("STATE-6")
        phone = self.start_phone(url=url)
        self.start(usb_error_pause=60)
        self.wait_active()
        phone.stop()                               # unplugged for good
        wait_until(lambda: self.engine.active is None, 15, "the login to end")
        self.assertTrue(self.host.claims[0].released)
        self.assertIn("Link to the phone closed.", self.logs())
        wait_until(lambda: not self.engine.linked, 5, "the link to be gone")

    def test_login_times_out(self):
        url, _ = authorize_url("STATE-7")
        phone = self.start_phone(url=url)
        self.start(login_timeout=1.0)
        self.wait_active()
        wait_until(lambda: self.engine.active is None, 10, "the timeout")
        self.assertIn("Timed out waiting for the browser.", self.logs())
        wait_until(lambda: phone.messages("error"), 5, "the error message")
        self.assertEqual(phone.messages("error")[0]["text"], "Timed out waiting for the browser.")
        self.assertTrue(self.host.claims[0].released)

    def test_plain_link_opens_in_the_browser(self):
        phone = self.start_phone(url="https://example.com/terms")
        self.start()
        wait_until(lambda: self.host.opened, 15, "the link to open")
        self.assertEqual(self.host.opened, ["https://example.com/terms"])
        wait_until(lambda: phone.messages("done"), 10, "done")
        self.assertEqual(phone.messages("status")[0]["text"], "Opened on your computer.")
        self.assertIsNone(self.engine.active)

    def test_plain_link_with_a_strange_scheme_is_not_opened(self):
        phone = self.start_phone(url="file:///etc/passwd")
        self.start()
        wait_until(lambda: phone.messages("status"), 15, "the status")
        self.assertEqual(self.host.opened, [])

    def test_sign_in_without_pkce_is_declined(self):
        url, _ = authorize_url("S", pkce=False)
        phone = self.start_phone(url=url)
        self.start()
        wait_until(lambda: self.host.pkce_asked, 15, "the PKCE question")
        wait_until(lambda: phone.messages("error"), 5, "the error message")
        self.assertEqual(phone.messages("error")[0]["text"], "Cancelled on the computer.")
        self.assertIn("Declined: the sign-in doesn't use PKCE.", self.logs())
        self.assertIsNone(self.engine.active)
        self.assertEqual(self.host.claims, [])

    def test_sign_in_without_pkce_can_continue(self):
        self.host.pkce_answer = True
        url, _ = authorize_url("S", pkce=False)
        self.start_phone(url=url)
        self.start()
        self.wait_active()
        self.assertEqual(len(self.host.pkce_asked), 1)

    def test_refused_claim_falls_back_to_pasting(self):
        self.host.refuse_claims = True
        url, redirect = authorize_url("STATE-8")
        phone = self.start_phone(url=url)
        self.start()
        flow = self.wait_active()
        wait_until(lambda: self.host.pasted_asked, 10, "the paste prompt")
        self.assertIn("Linux wouldn't let Login Relay take over %s://, so you'll paste the address instead." % SCHEME,
                      self.logs())
        title, body, placeholder = eng.paste_prompt(flow)
        self.assertEqual(title, "Paste the address you ended up on")
        self.assertIn("It starts with %s://login.example.com/android/callback." % SCHEME, body)
        self.assertEqual(placeholder, redirect)
        self.assertEqual(self.ui(lambda: self.engine.submit_pasted(flow, "https://nope.example/")), "unrelated")
        self.assertEqual(self.ui(lambda: self.engine.submit_pasted(flow, redirect + "?state=bad")), "stateMismatch")
        self.assertEqual(eng.paste_feedback("stateMismatch"), "That address is from a different sign-in. Try again.")
        self.assertEqual(eng.paste_feedback("unrelated"), "That isn't this app's return address. Try again.")
        self.assertIs(self.engine.active, flow)
        good = "  %s?code=G&state=STATE-8\n" % redirect
        self.assertEqual(self.ui(lambda: self.engine.submit_pasted(flow, good)), "ok")
        wait_until(lambda: phone.messages("callback"), 10, "the callback")
        self.assertEqual(phone.messages("callback")[0]["url"], good.strip())
        self.assertEqual(self.host.pasted_closed, [flow])

    def test_cancelling_the_paste_prompt_cancels_the_login(self):
        self.host.refuse_claims = True
        url, _ = authorize_url("STATE-9")
        phone = self.start_phone(url=url)
        self.start()
        flow = self.wait_active()
        wait_until(lambda: self.host.pasted_asked, 10, "the paste prompt")
        self.ui(lambda: self.engine.cancel_pasted(flow))
        self.assertIsNone(self.engine.active)
        wait_until(lambda: phone.messages("done"), 5, "done")
        self.assertEqual(self.host.pasted_closed, [])   # the prompt closed itself

    def test_https_return_address_asks_for_the_pasted_address(self):
        url = ("https://login.example.com/authorize?response_type=code&code_challenge=x&state=Q"
               "&redirect_uri=https%3A%2F%2Fapp.example%2Fcb")
        self.start_phone(url=url)
        self.start()
        self.wait_active()
        wait_until(lambda: self.host.pasted_asked, 10, "the paste prompt")
        self.assertEqual(self.host.claims, [])


class FakePhoneHookTests(EngineCase):
    """LOGINRELAY_FAKE_PHONE=host:port: one USB phone called "Fake Phone", reached over TCP, no pyusb."""

    def test_the_hook_makes_exactly_one_ready_phone(self):
        phone = self.start_phone()
        self.start()
        self.wait_phase(eng.READY)
        self.assertEqual(self.engine.device.title, "Fake Phone")
        self.assertEqual(self.engine.device.serial, "usb:fake")
        self.assertEqual(eng.describe(self.engine),
                         ("Fake Phone is ready", "Sign in to an app on the phone and the page opens here."))
        self.assertEqual(self.engine.phone_version, "0.6.0")
        self.assertIn("Login Relay 0.6.0 on the phone says hello (usb).", self.logs())
        self.assertIn("USB phone: Fake Phone", self.logs())
        self.assertEqual(self.engine.usb_candidates, [])      # never offers a choice
        self.assertEqual(eng.usb_phone_choices(self.engine), ())
        self.assertEqual(phone.connections, 1)

    def test_pyusb_is_never_touched(self):
        patcher = mock.patch.dict(sys.modules, {"usb": None, "usb.core": None, "usb.util": None})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.start_phone()
        self.start()
        self.wait_phase(eng.READY)

    def test_default_liveness_is_a_ping_every_two_seconds(self):
        phone = self.start_phone()
        self.start(usb_link_args={})
        self.wait_phase(eng.READY, 6)
        wait_until(lambda: len(phone.messages("ping")) >= 2, 8, "two pings")
        times = [t for t, m in phone.received if m.get("type") == "ping"]
        self.assertGreater(times[1] - times[0], 1.7)
        self.assertLess(times[1] - times[0], 3.0)
        self.assertTrue(self.engine.usb_alive)

    def test_a_phone_that_stops_answering_goes_idle_but_stays_connected(self):
        phone = self.start_phone()
        self.start()
        self.wait_phase(eng.READY)
        phone.answer_ping = False
        self.wait_log("Login Relay on the phone went idle (it starts again by itself when an app asks to sign in).", 8)
        self.assertTrue(self.engine.linked)
        self.assertEqual(self.engine.phase.kind, eng.READY)
        phone.answer_ping = True
        wait_until(lambda: self.engine.usb_alive, 5, "alive again")

    def test_no_hello_means_connecting_until_it_answers(self):
        phone = self.start_phone(hello=False, answer_ping=False)
        self.start()
        wait_until(lambda: self.engine.linked, 10, "the link")
        self.assertEqual(self.engine.phase.kind, eng.CONNECTING)
        self.assertEqual(eng.describe(self.engine)[0], "Fake Phone is connected over USB")
        phone.answer_ping = True
        self.wait_phase(eng.READY)

    def test_nothing_listening_is_reported_not_fatal(self):
        phone = self.start_phone()
        phone.stop()
        self.start()
        wait_until(lambda: self.engine.usb_detail == "Couldn't open the accessory connection (error -2).",
                   10, "the failure text")
        self.assertEqual(self.engine.phase.kind, eng.CONNECTING)
        self.assertEqual(eng.describe(self.engine)[0], "Connecting to Fake Phone\u2026")

    def test_a_malformed_value_is_reported(self):
        os.environ["LOGINRELAY_FAKE_PHONE"] = "not-an-address"
        self.start()
        wait_until(lambda: self.engine.usb_detail, 5, "the detail")
        self.assertEqual(self.engine.usb_detail, "LOGINRELAY_FAKE_PHONE must look like host:port")
        self.assertEqual(self.engine.phase.kind, eng.NO_PHONE)

    def test_address_parsing(self):
        self.assertEqual(fakephone.parse_address("127.0.0.1:4000"), ("127.0.0.1", 4000))
        self.assertEqual(fakephone.parse_address("[::1]:80"), ("::1", 80))
        for bad in ("", "x", "x:", ":80", "x:y", "x:0", "x:70000"):
            with self.assertRaises(ValueError, msg=bad):
                fakephone.parse_address(bad)

    def test_messages_are_plain_lines_with_no_padding(self):
        phone = self.start_phone()
        self.start()
        self.wait_phase(eng.READY)
        # A message whose JSON plus newline is exactly 64 bytes: over real USB it would get a second
        # newline; over TCP it must not.
        prefix = '{"type":"status","text":"'
        text = "x" * (63 - len(prefix) - len('"}'))
        link = self.engine._current_link
        link.send({"type": "status", "text": text})
        wait_until(lambda: phone.messages("status"), 5, "the status")
        self.assertEqual(len(prefix) + len(text) + len('"}') + 1, 64)
        self.assertNotIn(b"\n\n", bytes(phone.raw))
        self.assertTrue(all(line for line in bytes(phone.raw).split(b"\n")[:-1]))

    def test_the_link_frames_usb_pipes_but_not_tcp_ones(self):
        class Pipe:
            def __init__(self, needs_padding=None):
                if needs_padding is not None:
                    self.needs_padding = needs_padding
                self.written = b""

            def write(self, data, timeout_ms):
                self.written += data
                return len(data)

        text = "x" * (63 - len('{"type":"status","text":"') - len('"}'))
        for pipe, expected in ((Pipe(), b"\n\n"), (Pipe(True), b"\n\n"), (Pipe(False), b"\n")):
            UsbPhoneLink(pipe).send({"type": "status", "text": text})
            self.assertTrue(pipe.written.endswith(expected) and not pipe.written.endswith(b"\n\n\n"))
            self.assertEqual(len(pipe.written), 64 + len(expected) - 1)

    def test_a_phone_that_hangs_up_straight_away_never_becomes_ready(self):
        self.start_phone(idle=True)
        self.start()
        self.wait_log("Link to the phone closed.")
        time.sleep(0.5)
        self.assertNotEqual(self.engine.phase.kind, eng.READY)    # it never said hello or answered a ping
        self.assertFalse(self.engine.usb_ever_alive)

    def test_reconnects_after_the_connection_drops(self):
        phone = self.start_phone()
        self.start()
        self.wait_phase(eng.READY)
        phone.hang_up()
        self.wait_log("Link to the phone closed.")
        wait_until(lambda: phone.connections >= 2 and self.engine.linked, 15, "the second connection")
        self.wait_phase(eng.READY)


class RobustnessTests(EngineCase):
    def test_polling_does_not_leak_file_descriptors(self):
        phone = self.start_phone()
        self.start(usb_recheck=0.1, usb_link_args=dict(ping_interval=0.1, silence=1.0))
        self.wait_phase(eng.READY)
        wait_until(lambda: self.engine.linked, 10, "the link")
        time.sleep(1.5)                    # warm-up
        before = stable_fds()
        pings = len(phone.messages("ping"))
        time.sleep(5)
        self.assertGreater(len(phone.messages("ping")) - pings, 20, "the link should have been pinging")
        self.assertTrue(self.engine.linked)
        assert_fds_flat(self, before)

    def test_reconnecting_repeatedly_does_not_leak_either(self):
        phone = self.start_phone()
        self.start()
        self.wait_phase(eng.READY)
        time.sleep(1.0)
        before = stable_fds()
        for i in range(4):
            seen = phone.connections
            phone.hang_up()
            wait_until(lambda: phone.connections > seen and self.engine.linked, 15, "a reconnection")
        assert_fds_flat(self, before)

    def test_check_again_wakes_the_loop(self):
        phone = self.start_phone()
        phone.stop()
        self.start(usb_error_pause=60)
        wait_until(lambda: self.engine.usb_detail, 10, "the failure text")
        self.ui(self.engine.check_again)
        self.wait_log("Checking for the phone\u2026")


class WatchdogTests(EngineCase):
    def test_stuck_loop_is_replaced(self):
        # The first open hangs; the watchdog should start a fresh loop that works.
        self.start_phone()
        real_open = fakephone.TcpPhoneBackend.open
        hung = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_open(backend, location):
            if not hung.is_set():
                hung.set()
                release.wait(8)
            return real_open(backend, location)

        patcher = mock.patch.object(fakephone.TcpPhoneBackend, "open", slow_open)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.start(watchdog_interval=0.3, watchdog_after=1.0)
        wait_until(lambda: "Checking for the phone got stuck; starting over." in self.logs(), 10, "the watchdog")
        self.wait_phase(eng.READY, 10)
        self.assertEqual(self.engine._generation, 2)


class ShutdownTests(EngineCase):
    def test_shutdown_cancels_the_login_releases_the_claim_and_hangs_up(self):
        url, _ = authorize_url("STATE-S")
        phone = self.start_phone(url=url)
        self.start()
        self.wait_active()
        self.ui(self.engine.shutdown)
        self.assertTrue(self.host.claims[0].released)
        self.assertIn("Quit", self.logs())
        wait_until(lambda: phone.messages("done"), 5, "done")
        wait_until(lambda: not self.engine.linked or self.engine._current_link is None
                   or self.engine._current_link.closed, 5, "the link to close")


if __name__ == "__main__":
    unittest.main()
