# SPDX-License-Identifier: GPL-3.0-or-later
"""Drives engine.py end to end on this machine: a fake Host, a fake claim, and the real
tests/e2e/fake_adb.py + fake_phone.py started as subprocesses."""
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import SCHEME, PACKAGE, FakeWorld, authorize_url, open_fds, wait_until  # noqa: E402

from loginrelay import engine as eng  # noqa: E402
from loginrelay.prefs import Prefs  # noqa: E402
from loginrelay.redirect import RelayRequest  # noqa: E402


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
    engine_args = {}

    def setUp(self):
        self.world = FakeWorld()
        self.addCleanup(self.world.close)
        self.host = FakeHost()
        self.addCleanup(self.host.stop)
        self.tmp = tempfile.mkdtemp(prefix="lr-eng-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prefs = Prefs(os.path.join(self.tmp, "prefs.json"))
        args = dict(check_interval=0.4, link_recheck=0.5, done_delay=0.3)
        args.update(self.engine_args)
        self.engine = eng.RelayEngine(self.host, self.prefs, **args)
        self.changes = 0

        def changed():
            if not self.host.on_ui():
                self.host.wrong_thread.append("on_change off the UI thread")
            self.changes += 1

        self.engine.on_change = changed

    def start(self):
        self.host.ask(self.engine.start)
        self.addCleanup(lambda: self.host.ask(self.engine.shutdown))

    def ui(self, fn):
        return self.host.ask(fn)

    def logs(self):
        return [t for _, t in self.engine.log_lines]

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
        self.world.start_phone("--url", url, "--label", "Example App")
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
        wait_until(lambda: self.world.phone_messages("status"), 5, "the status line")
        self.assertEqual(self.world.phone_messages("status")[0]["text"], "Finish signing in on your computer.")

        # A decoy with the wrong state, as a browser would hand it over: ignored.
        self.ui(lambda: self.engine.receive("%s?code=EVIL&state=other" % redirect))
        self.ui(lambda: self.engine.receive("com.other.app://x?state=STATE-1"))   # unrelated: silent
        time.sleep(0.5)
        self.assertIn("Ignored a return address from a different sign-in (state mismatch).", self.logs())
        self.assertEqual(self.world.lines("am_calls.jsonl"), [])
        self.assertIs(self.engine.active, flow)
        self.assertFalse(self.host.claims[0].released)

        good = "%s?code=GOOD&state=STATE-1" % redirect
        self.ui(lambda: self.engine.receive(good))
        self.assertIsNone(self.engine.active)
        self.assertTrue(self.host.claims[0].released)
        wait_until(lambda: self.world.lines("am_calls.jsonl"), 10, "the return address to reach the phone")
        call = self.world.lines("am_calls.jsonl")[0]
        self.assertEqual(call["-d"], good)
        self.assertEqual(call["-p"], PACKAGE)
        self.assertEqual(call["-a"], "android.intent.action.VIEW")
        wait_until(lambda: self.world.phone_messages("done"), 10, "the phone to be told it's done")
        self.assertIn("Returning to Example App on the phone…", self.logs())
        self.assertIn("Done: Example App is finishing the sign-in.", self.logs())
        self.assertEqual(self.engine.phase.kind, eng.READY)
        # The phone forgot its pending page, so a reconnect doesn't start another login.
        time.sleep(1.2)
        self.assertIsNone(self.engine.active)
        self.assertEqual(len(self.world.lines("am_calls.jsonl")), 1)

    def test_sign_out(self):
        url, redirect = authorize_url("", kind="signout")
        self.world.start_phone("--url", url)
        self.start()
        flow = self.wait_active()
        self.assertEqual(flow.request.verb, "sign-out")
        wait_until(lambda: self.world.phone_messages("status"), 5, "the status line")
        self.assertEqual(self.world.phone_messages("status")[0]["text"], "Finishing sign-out on your computer.")
        self.ui(lambda: self.engine.receive(redirect))
        wait_until(lambda: self.world.lines("am_calls.jsonl"), 10, "am start")
        self.assertEqual(self.world.lines("am_calls.jsonl")[0]["-d"], redirect)
        wait_until(lambda: self.world.phone_messages("done"), 10, "done")
        self.assertTrue(self.host.claims[0].released)
        self.assertEqual(self.host.notifications, [])   # notifications are off by default

    def test_page_with_error_is_reported_but_still_handed_over(self):
        url, redirect = authorize_url("S9")
        self.world.start_phone("--url", url)
        self.start()
        self.wait_active()
        self.ui(lambda: self.engine.receive("%s?error=access_denied&error_description=No+way&state=S9" % redirect))
        self.assertIn("The page returned an error: No way", self.logs())
        wait_until(lambda: self.world.lines("am_calls.jsonl"), 10, "am start")

    def test_cancel_from_the_phone(self):
        url, redirect = authorize_url("STATE-2")
        self.world.start_phone("--url", url, "--cancel-after", "1.5")
        self.start()
        self.wait_active()
        wait_until(lambda: self.engine.active is None, 15, "the phone's cancel")
        self.assertIn("Cancelled on the phone.", self.logs())
        self.assertTrue(self.host.claims[0].released)
        self.ui(lambda: self.engine.receive("%s?code=LATE&state=STATE-2" % redirect))
        time.sleep(0.5)
        self.assertEqual(self.world.lines("am_calls.jsonl"), [])
        self.assertEqual(self.world.phone_messages("done"), [])   # the phone cancelled, nobody to tell

    def test_cancel_from_the_computer_closes_the_phones_screen(self):
        url, _ = authorize_url("STATE-3")
        self.world.start_phone("--url", url)
        self.start()
        self.wait_active()
        self.ui(lambda: self.engine.cancel_active("Cancelled on the computer.", notify_phone=True))
        self.assertTrue(self.host.claims[0].released)
        wait_until(lambda: self.world.phone_messages("done"), 5, "done")
        self.assertIn("Cancelled on the computer.", self.logs())

    def test_a_newer_request_replaces_the_one_in_progress(self):
        url, _ = authorize_url("STATE-4")
        self.world.start_phone("--url", url)
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
        self.world.start_phone("--url", url)
        self.start()
        self.wait_active()
        self.world.write("devices.txt", "")      # unplugged
        wait_until(lambda: self.engine.active is None, 15, "the login to end")
        self.assertTrue(self.host.claims[0].released)
        self.wait_phase(eng.NO_PHONE)
        self.assertIn("Link to the phone closed.", self.logs())

    def test_login_times_out(self):
        self.engine._login_timeout = 1.0
        url, _ = authorize_url("STATE-7")
        self.world.start_phone("--url", url)
        self.start()
        self.wait_active()
        wait_until(lambda: self.engine.active is None, 10, "the timeout")
        self.assertIn("Timed out waiting for the browser.", self.logs())
        wait_until(lambda: self.world.phone_messages("error"), 5, "the error message")
        self.assertEqual(self.world.phone_messages("error")[0]["text"], "Timed out waiting for the browser.")
        self.assertTrue(self.host.claims[0].released)

    def test_plain_link_opens_in_the_browser(self):
        self.world.start_phone("--url", "https://example.com/terms")
        self.start()
        wait_until(lambda: self.host.opened, 15, "the link to open")
        self.assertEqual(self.host.opened, ["https://example.com/terms"])
        wait_until(lambda: self.world.phone_messages("done"), 10, "done")
        self.assertEqual(self.world.phone_messages("status")[0]["text"], "Opened on your computer.")
        self.assertIsNone(self.engine.active)

    def test_plain_link_with_a_strange_scheme_is_not_opened(self):
        self.world.start_phone("--url", "file:///etc/passwd")
        self.start()
        wait_until(lambda: self.world.phone_messages("status"), 15, "the status")
        self.assertEqual(self.host.opened, [])

    def test_sign_in_without_pkce_is_declined(self):
        url, _ = authorize_url("S", pkce=False)
        self.world.start_phone("--url", url)
        self.start()
        wait_until(lambda: self.host.pkce_asked, 15, "the PKCE question")
        wait_until(lambda: self.world.phone_messages("error"), 5, "the error message")
        self.assertEqual(self.world.phone_messages("error")[0]["text"], "Cancelled on the computer.")
        self.assertIn("Declined: the sign-in doesn't use PKCE.", self.logs())
        self.assertIsNone(self.engine.active)
        self.assertEqual(self.host.claims, [])

    def test_sign_in_without_pkce_can_continue(self):
        self.host.pkce_answer = True
        url, _ = authorize_url("S", pkce=False)
        self.world.start_phone("--url", url)
        self.start()
        self.wait_active()
        self.assertEqual(len(self.host.pkce_asked), 1)

    def test_refused_claim_falls_back_to_pasting(self):
        self.host.refuse_claims = True
        url, redirect = authorize_url("STATE-8")
        self.world.start_phone("--url", url)
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
        wait_until(lambda: self.world.lines("am_calls.jsonl"), 10, "am start")
        self.assertEqual(self.world.lines("am_calls.jsonl")[0]["-d"], good.strip())
        self.assertEqual(self.host.pasted_closed, [flow])

    def test_cancelling_the_paste_prompt_cancels_the_login(self):
        self.host.refuse_claims = True
        url, _ = authorize_url("STATE-9")
        self.world.start_phone("--url", url)
        self.start()
        flow = self.wait_active()
        wait_until(lambda: self.host.pasted_asked, 10, "the paste prompt")
        self.ui(lambda: self.engine.cancel_pasted(flow))
        self.assertIsNone(self.engine.active)
        wait_until(lambda: self.world.phone_messages("done"), 5, "done")
        self.assertEqual(self.host.pasted_closed, [])   # the prompt closed itself

    def test_https_return_address_asks_for_the_pasted_address(self):
        url = ("https://login.example.com/authorize?response_type=code&code_challenge=x&state=Q"
               "&redirect_uri=https%3A%2F%2Fapp.example%2Fcb")
        self.world.start_phone("--url", url)
        self.start()
        self.wait_active()
        wait_until(lambda: self.host.pasted_asked, 10, "the paste prompt")
        self.assertEqual(self.host.claims, [])


class PhaseTests(EngineCase):
    def test_no_phone(self):
        self.world.write("devices.txt", "")
        self.world.start_phone()
        self.start()
        self.wait_phase(eng.NO_PHONE)
        self.assertEqual(eng.describe(self.engine)[0], "No phone connected")
        self.assertEqual(eng.describe(self.engine)[1],
                         "Plug the phone in with USB and turn on USB debugging (Settings → Developer options).")

    def test_unauthorized_and_offline(self):
        self.world.write("devices.txt", "ABC\tunauthorized usb:1 model:Pixel_7 transport_id:1")
        self.start()
        self.wait_phase(eng.UNAUTHORIZED)
        self.assertEqual(eng.describe(self.engine),
                         ("Pixel 7 is waiting for you", "Tap Allow on the phone's \"Allow USB debugging?\" prompt."))
        self.world.write("devices.txt", "ABC\toffline model:Pixel_7")
        wait_until(lambda: eng.describe(self.engine)[0] == "Pixel 7 isn't responding (offline)", 10, "offline text")
        self.assertEqual(eng.describe(self.engine)[1],
                         "Unplug the cable and plug it back in, then choose Check Again.")
        self.assertEqual([e.label for e in eng.menu_model(self.engine) if e.kind == "action"],
                         ["Check Again", "Show Activity…", "About Login Relay", "Quit Login Relay"])

    def test_app_missing_then_install_it(self):
        apk = os.path.join(self.tmp, "login-relay.apk")
        with open(apk, "wb") as f:
            f.write(b"apk")
        self.engine._bundled_apk = apk
        self.engine.bundled_phone_version = "0.3.0"
        self.world.write("no_app")
        self.world.start_phone()
        self.start()
        self.wait_phase(eng.NEEDS_APP)
        self.assertEqual(eng.describe(self.engine),
                         ("Login Relay isn't on Fake Phone", "Install it on the phone to start."))
        self.assertEqual(eng.window_actions(self.engine), (("Install on Phone", eng.A_INSTALL), None, True))
        labels = [e.label for e in eng.menu_model(self.engine) if e.kind == "action"]
        self.assertEqual(labels[0], "Install Login Relay on Phone")

        self.ui(self.engine.install_phone_app)
        self.assertEqual(eng.describe(self.engine)[0], "Installing Login Relay on Fake Phone…")
        wait_until(lambda: self.world.lines("installs.jsonl"), 10, "the install")
        self.assertEqual(self.world.lines("installs.jsonl")[0], {"apk": apk, "exists": True})
        wait_until(lambda: "Installed." in self.logs(), 10, "install done")
        os.unlink(self.world.p("no_app"))          # the "install" makes the app appear
        self.wait_phase(eng.READY)
        self.assertEqual(self.engine.phone_version, "0.3.0")
        self.assertIsNone(self.engine.busy)

    def test_app_missing_without_a_bundled_apk(self):
        self.world.write("no_app")
        self.start()
        self.wait_phase(eng.NEEDS_APP)
        self.assertEqual(eng.describe(self.engine)[1], "Install login-relay.apk on the phone.")
        self.assertEqual(eng.window_actions(self.engine)[0], None)
        self.ui(self.engine.install_phone_app)       # nothing to install: no-op
        self.assertIsNone(self.engine.busy)

    def test_outdated_phone_app_offers_an_update(self):
        apk = os.path.join(self.tmp, "login-relay.apk")
        open(apk, "wb").close()
        self.engine._bundled_apk = apk
        self.engine.bundled_phone_version = "0.3.0"
        self.world.write("phone_version", "0.2.2")
        self.world.start_phone()
        self.start()
        self.wait_phase(eng.READY)
        self.assertTrue(self.engine.phone_app_is_outdated)
        self.assertEqual(eng.window_actions(self.engine),
                         (("Open on Phone", eng.A_OPEN), ("Update on Phone", eng.A_INSTALL), True))
        self.assertEqual(eng.describe(self.engine),
                         ("Fake Phone is ready",
                          "Sign in to an app on the phone and the page opens here. An update for the phone app is available."))
        menu = eng.menu_model(self.engine, launch_at_login=True)
        self.assertEqual([e.label for e in menu],
                         ["Fake Phone is ready", "", "Open Login Relay on Phone",
                          "Update Login Relay on Phone (0.2.2 → 0.3.0)", "", "Show Activity…", "",
                          "Launch at Login", "Notify When a Sign-in Arrives", "", "About Login Relay",
                          "Quit Login Relay"])
        self.assertTrue(menu[0].good)
        self.assertTrue([e for e in menu if e.label == "Launch at Login"][0].checked)

    def test_open_on_phone(self):
        self.world.start_phone()
        self.start()
        self.wait_phase(eng.READY)
        self.ui(self.engine.open_on_phone)
        wait_until(lambda: any("RelayActivity" in c["command"] for c in self.world.lines("shell_calls.jsonl")),
                   10, "the shell command")
        cmd = [c["command"] for c in self.world.lines("shell_calls.jsonl") if "RelayActivity" in c["command"]][0]
        self.assertEqual(cmd, "input keyevent KEYCODE_WAKEUP; am start -n io.github.clippycoder.oauthrelay/.RelayActivity")

    def test_no_adb(self):
        os.environ["LOGINRELAY_ADB"] = ""
        self.engine._locate = lambda choice: None
        self.start()
        self.wait_phase(eng.NO_ADB)
        self.assertEqual([e.label for e in eng.menu_model(self.engine) if e.kind == "action"][:2],
                         ["Download Android Platform-Tools…", "Choose adb…"])
        self.assertEqual(eng.window_actions(self.engine)[:2],
                         (("Download Platform-Tools…", eng.A_DOWNLOAD), ("Choose adb…", eng.A_CHOOSE_ADB)))

    def test_two_phones_get_a_submenu_and_the_choice_sticks(self):
        self.world.write("devices.txt", "AAA\tdevice model:One\nBBB\tdevice model:Two")
        self.start()
        self.wait_phase(eng.READY)
        self.assertEqual(self.engine.device.serial, "AAA")
        sub = [e for e in eng.menu_model(self.engine) if e.kind == "submenu"][0]
        self.assertEqual([(c.label, c.arg, c.checked) for c in sub.children],
                         [("One (AAA)", "AAA", True), ("Two (BBB)", "BBB", False)])
        self.ui(lambda: self.engine.select_device("BBB"))
        wait_until(lambda: self.engine.device and self.engine.device.serial == "BBB", 10, "the new choice")
        self.assertEqual(Prefs(self.prefs.path).selected_serial, "BBB")

    def test_choose_adb_is_remembered(self):
        self.ui(lambda: self.engine.choose_adb("/opt/adb"))
        self.assertEqual(self.prefs.adb_path, "/opt/adb")

    def test_working_menu(self):
        url, _ = authorize_url("STATE-M")
        self.world.start_phone("--url", url, "--label", "Example App")
        self.start()
        self.wait_active()
        self.assertEqual(eng.describe(self.engine),
                         ("Example App sign-in in progress",
                          "Finish in your browser. If it asks to open Login Relay, allow it."))
        self.assertEqual([e.label for e in eng.menu_model(self.engine) if e.kind == "action"][0], "Cancel sign-in")
        self.assertEqual(eng.window_actions(self.engine)[0], ("Cancel", eng.A_CANCEL))


class RobustnessTests(EngineCase):
    engine_args = dict(check_interval=0.2, link_recheck=0.4)

    def test_polling_does_not_leak_file_descriptors(self):
        self.world.start_phone()           # idle phone: linked, nothing to do
        self.start()
        self.wait_phase(eng.READY)
        wait_until(lambda: self.engine.linked, 10, "the link")
        time.sleep(1.5)                    # warm-up
        before = open_fds()
        calls_before = len(self.world.lines("adb_calls.jsonl"))
        time.sleep(7)
        calls = len(self.world.lines("adb_calls.jsonl")) - calls_before
        self.assertGreater(calls, 5, "the loop should have been polling")
        self.assertTrue(self.engine.linked)
        self.assertLessEqual(open_fds(), before + 1)

    def test_polling_without_a_phone_does_not_leak_either(self):
        self.world.write("devices.txt", "")
        self.start()
        self.wait_phase(eng.NO_PHONE)
        time.sleep(1.0)
        before = open_fds()
        time.sleep(4)
        self.assertLessEqual(open_fds(), before + 1)

    def test_losing_the_phone_while_linked_is_noticed_within_the_recheck(self):
        self.world.start_phone()
        self.start()
        wait_until(lambda: self.engine.linked, 15, "the link")
        self.world.write("devices.txt", "")
        t0 = time.time()
        wait_until(lambda: not self.engine.linked, 8, "the link to be dropped")
        self.assertLess(time.time() - t0, 4)
        self.wait_phase(eng.NO_PHONE)

    def test_phone_that_hangs_up_straight_away_is_not_linked(self):
        self.world.start_phone("--idle")
        self.start()
        self.wait_phase(eng.READY)
        time.sleep(2)
        self.assertFalse(self.engine.linked)

    def test_check_again_wakes_the_loop(self):
        self.engine._check_interval = 60
        self.world.write("devices.txt", "")
        self.start()
        self.wait_phase(eng.NO_PHONE)
        os.unlink(self.world.p("devices.txt"))
        self.world.start_phone()
        self.ui(self.engine.check_again)
        self.wait_phase(eng.READY, 5)
        self.assertIn("Checking for the phone…", self.logs())


class WatchdogTests(EngineCase):
    engine_args = dict(check_interval=0.3, watchdog_interval=0.3, watchdog_after=1.0)

    def test_stuck_loop_is_replaced(self):
        # The first adb call hangs; the watchdog should start a fresh loop that works.
        marker = os.path.join(self.tmp, "hung-once")
        wrapper = os.path.join(self.tmp, "slow-adb")
        with open(wrapper, "w") as f:
            f.write('#!/bin/sh\nif [ ! -e "%s" ]; then : > "%s"; exec sleep 6; fi\nexec "%s" "$@"\n'
                    % (marker, marker, self.world.adb))
        os.chmod(wrapper, 0o755)
        os.environ["LOGINRELAY_ADB"] = wrapper
        self.world.start_phone()
        self.start()
        wait_until(lambda: "Checking for the phone got stuck; starting over." in self.logs(), 10, "the watchdog")
        self.wait_phase(eng.READY, 10)
        self.assertEqual(self.engine._generation, 2)


class ShutdownTests(EngineCase):
    def test_shutdown_cancels_the_login_releases_the_claim_and_removes_the_forward(self):
        url, _ = authorize_url("STATE-S")
        self.world.start_phone("--url", url)
        self.start()
        self.wait_active()
        self.ui(self.engine.shutdown)
        self.assertTrue(self.host.claims[0].released)
        self.assertIn("Quit", self.logs())
        wait_until(lambda: self.world.phone_messages("done"), 5, "done")
        with open(self.world.p("phone_port")) as f:
            port = f.read().strip()
        calls = [c["argv"] for c in self.world.lines("adb_calls.jsonl")]
        self.assertIn(["-s", "FAKE0001", "forward", "--remove", "tcp:" + port], calls)


if __name__ == "__main__":
    unittest.main()
