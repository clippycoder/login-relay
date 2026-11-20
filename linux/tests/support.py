# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared test plumbing: helpers, and a fake phone that listens on a TCP port (the LOGINRELAY_FAKE_PHONE hook)."""
import gc
import json
import os
import socket
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
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


class FakePhoneServer:
    """The Login Relay app on a phone, listening on 127.0.0.1. Point LOGINRELAY_FAKE_PHONE at `address`.

    Plain newline-delimited JSON, no padding. On every connection it says hello, then (if `url` is
    set and hasn't been finished) sends an `open` for it, answers pings with pongs, and answers a
    `callback` with `delivered` (or an `error`). Everything it receives is kept in `received`."""

    def __init__(self, url=None, referrer="android-app://" + PACKAGE, label="Example App", version="0.6.0",
                 hello=True, idle=False, answer_ping=True, delivery_reply="delivered", cancel_after=None):
        self.pending = url
        self.referrer, self.label, self.version = referrer, label, version
        self.hello, self.idle, self.answer_ping = hello, idle, answer_ping
        self.delivery_reply = delivery_reply     # "delivered", "error:<text>" or None (stay silent)
        self.cancel_after = cancel_after
        self.received = []                       # (monotonic time, message)
        self.raw = bytearray()                   # every byte the app sent
        self.connections = 0
        self._conns = []
        self._lock = threading.Lock()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stopped = False
        threading.Thread(target=self._accept_loop, name="fake-phone", daemon=True).start()

    @property
    def address(self):
        return "127.0.0.1:%d" % self.port

    def _accept_loop(self):
        while not self._stopped:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with self._lock:
                self._conns.append(conn)
                self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def send(self, obj, conn=None):
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self._lock:
            targets = [conn] if conn is not None else list(self._conns)
        for c in targets:
            try:
                c.sendall(data)
            except OSError:
                pass

    def _serve(self, conn):
        if self.idle:
            conn.close()
            return
        if self.hello:
            self.send({"v": 1, "type": "hello", "transport": "usb", "version": self.version}, conn)
        if self.pending:
            self.send({"v": 1, "type": "open", "url": self.pending, "referrer": self.referrer,
                       "label": self.label}, conn)
            if self.cancel_after is not None:
                threading.Timer(self.cancel_after, lambda: self.send({"type": "cancel"}, conn)).start()
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                self.raw += chunk
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    self.received.append((time.monotonic(), msg))
                    self._react(msg, conn)
        except OSError:
            return
        finally:
            with self._lock:
                if conn in self._conns:
                    self._conns.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _react(self, msg, conn):
        kind = msg.get("type")
        if kind == "ping" and self.answer_ping:
            self.send({"type": "pong"}, conn)
        elif kind == "callback":
            self.pending = None
            if self.delivery_reply == "delivered":
                self.send({"type": "delivered"}, conn)
            elif self.delivery_reply:
                self.send({"type": "error", "text": self.delivery_reply.split(":", 1)[1]}, conn)
        elif kind == "done":
            self.pending = None

    def messages(self, kind=None):
        return [m for _, m in list(self.received) if kind is None or m.get("type") == kind]

    def hang_up(self):
        """Close every connection (the cable came out); the phone can still be reached again."""
        with self._lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    def stop(self):
        """Hang up and stop listening, for good."""
        self._stopped = True
        try:
            # On Linux close() alone doesn't wake a thread blocked in accept(), and the kernel keeps the
            # port listening until it returns; shutdown() does wake it, so "stopped" really means refused.
            self._srv.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._srv.close()
        except OSError:
            pass
        self.hang_up()
