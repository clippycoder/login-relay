#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end test of a Login Relay desktop app against a fake phone and a fake adb.

    python3 tests/e2e/run_e2e.py -- <command that starts the app>

For example:
    python3 tests/e2e/run_e2e.py -- "macos/build/Login Relay.app/Contents/MacOS/LoginRelay" --verbose
    python3 tests/e2e/run_e2e.py -- python3 -m loginrelay        (Linux, from linux/)
    python3 tests/e2e/run_e2e.py -- windows\\bin\\LoginRelay.exe   (Windows)

The app is started with LOGINRELAY_ADB (the fake adb wrapper) and LOGINRELAY_BROWSER=none (don't
open a browser) set. The driver plays the part of the browser: it hands "return addresses" to the
operating system exactly as a browser would, so the app's claim on the URL scheme is exercised for
real. Scenarios:

  signin    a page arrives; a return address with the wrong state is ignored; the right one is handed
            to the phone (am start ... -p <package>); the phone gets "done"; the claim is released
  cancel    the phone cancels mid-login: nothing is handed over, the claim is released
  signout   same as signin, for a page whose return address is in returnTo
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEME = "com.example.relaytest"
PACKAGE = "com.example.app"


def authorize_url(state, redirect_param="redirect_uri"):
    redirect = f"{SCHEME}://login.example.com/android/callback"
    q = {"client_id": "e2e", "response_type": "code", "state": state, "code_challenge": "abc",
         redirect_param: redirect}
    if redirect_param == "returnTo":
        q = {"client_id": "e2e", "returnTo": redirect}
    return "https://login.example.com/authorize?" + urllib.parse.urlencode(q), redirect


# ---- platform bits ---------------------------------------------------------------------------

def open_like_a_browser(url):
    if sys.platform.startswith("win"):
        os.startfile(url)  # ShellExecute, which is what a browser ends up calling
    elif sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    else:
        # With no handler registered (after a cancel, say) xdg-open can hunt for a browser and hang
        # on a headless machine; that's not the app's problem, so don't wait for it.
        opener = subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            opener.wait(10)
        except subprocess.TimeoutExpired:
            opener.kill()


def claim_holder():
    """What the OS currently has registered for SCHEME ('' if nothing)."""
    if sys.platform.startswith("win"):
        r = subprocess.run(["reg", "query", rf"HKCU\Software\Classes\{SCHEME}"], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    if sys.platform == "darwin":
        swiftc = shutil.which("swiftc")
        if not swiftc:
            return None  # can't check
        d = tempfile.mkdtemp(prefix="lr-claim-")
        src = os.path.join(d, "main.swift")
        with open(src, "w") as f:
            f.write('import Foundation\nimport CoreServices\nlet s = "%s"\n'
                    'print((LSCopyDefaultHandlerForURLScheme(s as CFString)?.takeRetainedValue() as String?) ?? "")\n' % SCHEME)
        exe = os.path.join(d, "claim")
        subprocess.run([swiftc, src, "-o", exe], capture_output=True)
        out = subprocess.run([exe], capture_output=True, text=True).stdout.strip()
        shutil.rmtree(d, ignore_errors=True)
        return out
    r = subprocess.run(["xdg-mime", "query", "default", f"x-scheme-handler/{SCHEME}"], capture_output=True, text=True)
    return r.stdout.strip()


# ---- plumbing --------------------------------------------------------------------------------

class Run:
    def __init__(self, app_cmd, phone_args):
        self.state = tempfile.mkdtemp(prefix="lr-e2e-")
        self.failures = 0
        self.app = None
        bindir = os.path.join(self.state, "bin")
        os.makedirs(bindir)
        fake = os.path.join(HERE, "fake_adb.py")
        if sys.platform.startswith("win"):
            self.adb = os.path.join(bindir, "adb.cmd")
            with open(self.adb, "w") as f:
                f.write(f'@"{sys.executable}" "{fake}" %*\r\n')
        else:
            self.adb = os.path.join(bindir, "adb")
            with open(self.adb, "w") as f:
                f.write(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
            os.chmod(self.adb, 0o755)
        self.phone = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_phone.py"), "--state", self.state,
                                       "--referrer", f"android-app://{PACKAGE}", *phone_args])
        self.wait(lambda: os.path.exists(self.p("phone_port")), 10, "fake phone to listen")
        env = dict(os.environ, LOGINRELAY_ADB=self.adb, LOGINRELAY_BROWSER="none", LOGINRELAY_TRANSPORT="adb",
                   FAKE_ADB_STATE=self.state)
        self.app = subprocess.Popen(app_cmd, env=env)

    def p(self, name):
        return os.path.join(self.state, name)

    def lines(self, name):
        try:
            with open(self.p(name), encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def wait(self, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            if self.app is not None and self.app.poll() is not None:
                self.fail(f"the app exited early (code {self.app.returncode}) while waiting for {what}")
                return False
            time.sleep(0.25)
        self.fail(f"timed out waiting for {what}")
        return False

    def fail(self, msg):
        self.failures += 1
        print(f"  FAIL {msg}")

    def check(self, cond, msg):
        if cond:
            print(f"  ok   {msg}")
        else:
            self.fail(msg)

    def close(self):
        for proc in (self.app, self.phone):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        shutil.rmtree(self.state, ignore_errors=True)


def phone_got(run, kind):
    return any(m.get("type") == kind for m in run.lines("phone_msgs.jsonl"))


def scenario_login(app_cmd, kind):
    print(f"[{kind}]")
    param = "returnTo" if kind == "signout" else "redirect_uri"
    state = "E2E-STATE-1"
    url, redirect = authorize_url(state, param)
    r = Run(app_cmd, ["--url", url, "--label", "Example App"])
    try:
        # The app claims the scheme as soon as the page arrives.
        # (claim_holder() is None means this machine can't tell, so don't fail on that.)
        r.check(r.wait(lambda: claim_holder() != "", 40, "the app to claim the return scheme"),
                "the app claimed the return scheme")
        decoy = f"{redirect}?code=EVIL&state=other" if kind == "signin" else None
        if decoy:
            open_like_a_browser(decoy)
            time.sleep(2.5)
            r.check(not r.lines("am_calls.jsonl"), "a return address with the wrong state was ignored")
        good = f"{redirect}?code=GOOD&state={state}" if kind == "signin" else redirect
        open_like_a_browser(good)
        r.check(r.wait(lambda: r.lines("am_calls.jsonl"), 20, "the return address to reach the phone"),
                "the return address was handed to the phone")
        calls = r.lines("am_calls.jsonl")
        if calls:
            c = calls[0]
            r.check(c.get("-d") == good, f"it arrived intact: {c.get('-d')!r}")
            r.check(c.get("-p") == PACKAGE, f"it went to the app that asked: {c.get('-p')!r}")
            r.check(c.get("-a") == "android.intent.action.VIEW", "as a VIEW intent")
        r.check(r.wait(lambda: phone_got(r, "done"), 15, "Login Relay to be told it's done"),
                "the phone was told 'done'")
        r.check(phone_got(r, "status"), "the phone got a status line")
        holder = claim_holder()
        r.check(holder in ("", None), f"the claim was released (holder now: {holder!r})")
    finally:
        r.close()
    return r.failures


def scenario_cancel(app_cmd):
    print("[cancel]")
    url, redirect = authorize_url("E2E-STATE-2")
    r = Run(app_cmd, ["--url", url, "--cancel-after", "4"])
    try:
        r.wait(lambda: claim_holder() != "", 40, "the app to claim the return scheme")
        r.check(r.wait(lambda: claim_holder() in ("", None), 20, "the claim to be released after the phone cancelled"),
                "the claim was released when the phone cancelled")
        open_like_a_browser(f"{redirect}?code=LATE&state=E2E-STATE-2")
        time.sleep(2)
        r.check(not r.lines("am_calls.jsonl"), "a late return address was not handed over")
    finally:
        r.close()
    return r.failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["signin", "signout", "cancel"])
    ap.add_argument("app", nargs=argparse.REMAINDER, help="-- command that starts the app")
    a = ap.parse_args()
    cmd = [x for x in a.app if x != "--"] if a.app and a.app[0] == "--" else a.app
    if not cmd:
        ap.error("give the command that starts the app after --")
    failures = 0
    if a.only in (None, "signin"):
        failures += scenario_login(cmd, "signin")
    if a.only in (None, "signout"):
        failures += scenario_login(cmd, "signout")
    if a.only in (None, "cancel"):
        failures += scenario_cancel(cmd)
    print("all end-to-end checks passed" if failures == 0 else f"{failures} end-to-end check(s) FAILED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
