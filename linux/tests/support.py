# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared test plumbing: the real tests/e2e fake adb and fake phone, run as subprocesses."""
import gc
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
E2E = os.path.join(HERE, "..", "..", "tests", "e2e")
SCHEME = "com.example.relaytest"
PACKAGE = "com.example.app"


def open_fds():
    for d in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(d):
            return len(os.listdir(d))
    raise RuntimeError("no way to count file descriptors here")


def stable_fds(samples=5, gap=0.1):
    """The median of several descriptor counts taken a moment apart (after a garbage collection), so
    that a descriptor the interpreter or a finishing thread holds for an instant doesn't count."""
    gc.collect()
    counts = []
    for i in range(samples):
        counts.append(open_fds())
        if i < samples - 1:
            time.sleep(gap)
    return sorted(counts)[len(counts) // 2]


def assert_fds_flat(case, before, slack=3):
    """`before` came from stable_fds(). A real leak (one or more descriptors per poll or per call) adds
    tens over these tests, so a small fixed slack still catches it."""
    now = stable_fds()
    case.assertLessEqual(now, before + slack, "file descriptors went from %d to %d" % (before, now))


def wait_until(cond, timeout=15, what="condition", interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    raise AssertionError("timed out waiting for " + what)


def authorize_url(state, kind="signin", pkce=True, response_type="code"):
    redirect = "%s://login.example.com/android/callback" % SCHEME
    if kind == "signout":
        q = {"client_id": "e2e", "returnTo": redirect}
    else:
        q = {"client_id": "e2e", "response_type": response_type, "state": state, "redirect_uri": redirect}
        if pkce:
            q["code_challenge"] = "abc"
    return "https://login.example.com/authorize?" + urllib.parse.urlencode(q), redirect


class FakeWorld:
    """A state directory, a fake adb wrapper script and (optionally) a fake phone process."""

    def __init__(self):
        self.state = tempfile.mkdtemp(prefix="lr-test-")
        bindir = os.path.join(self.state, "bin")
        os.makedirs(bindir)
        self.adb = os.path.join(bindir, "adb")
        with open(self.adb, "w") as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, os.path.join(E2E, "fake_adb.py")))
        os.chmod(self.adb, 0o755)
        self.phone = None
        self._saved = {}
        for key, value in (("LOGINRELAY_ADB", self.adb), ("FAKE_ADB_STATE", self.state),
                           ("LOGINRELAY_BROWSER", "none")):
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value

    def p(self, name):
        return os.path.join(self.state, name)

    def write(self, name, text=""):
        with open(self.p(name), "w", encoding="utf-8") as f:
            f.write(text)

    def start_phone(self, *args):
        self.phone = subprocess.Popen([sys.executable, os.path.join(E2E, "fake_phone.py"), "--state", self.state,
                                       "--referrer", "android-app://" + PACKAGE, *args])
        wait_until(lambda: os.path.exists(self.p("phone_port")), 10, "the fake phone to listen")

    def lines(self, name):
        try:
            with open(self.p(name), encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def phone_messages(self, kind=None):
        return [m for m in self.lines("phone_msgs.jsonl") if kind is None or m.get("type") == kind]

    def close(self):
        if self.phone is not None and self.phone.poll() is None:
            self.phone.terminate()
            try:
                self.phone.wait(5)
            except subprocess.TimeoutExpired:
                self.phone.kill()
                self.phone.wait()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.state, ignore_errors=True)
