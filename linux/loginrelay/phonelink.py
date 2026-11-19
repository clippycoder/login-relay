# SPDX-License-Identifier: GPL-3.0-or-later
"""The connection to the Login Relay app on the phone, through `adb forward`.

adb accepts the TCP connection even when nothing is listening on the phone, then closes it.
So a connect only counts once it stays open for a moment (400 ms).
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import traceback
from typing import Callable, Optional

MAX_LINE = 1 << 20


class PhoneLink:
    def __init__(self):
        # Called on the read thread for each JSON object from the phone.
        self.on_message: Optional[Callable[[dict], None]] = None
        # Called once, on the read thread, when an established link ends.
        self.on_close: Optional[Callable[[], None]] = None
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def open(self, port: int, stay_open: float = 0.4, connect_timeout: float = 3.0) -> bool:
        """Connects to 127.0.0.1:port. Returns False if the phone app isn't listening right now."""
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=connect_timeout)
        except OSError:
            return False
        try:
            # Give adb a moment to drop us if the phone side isn't there. A timeout here means the
            # connection stayed open and quiet, which is what a healthy idle link looks like.
            s.settimeout(stay_open)
            try:
                if s.recv(1, socket.MSG_PEEK) == b"":
                    s.close()
                    return False  # closed straight away
            except socket.timeout:
                pass
            s.settimeout(None)
        except OSError:
            s.close()
            return False
        with self._lock:
            self._sock = s
            self._closed = False
        threading.Thread(target=self._read_loop, args=(s,), name="phone-link", daemon=True).start()
        return True

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            s = self._sock
            if s is None or self._closed:
                return
            try:
                s.sendall(data)
            except OSError:
                pass

    def close(self) -> None:
        """Hangs up; the read thread then ends and reports on_close."""
        with self._lock:
            s = self._sock
            self._closed = True
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _read_loop(self, s: socket.socket) -> None:
        buf = b""
        try:
            while True:
                try:
                    chunk = s.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        obj = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and self.on_message:
                        self._call(self.on_message, obj)
                if len(buf) > MAX_LINE:
                    break
        finally:
            with self._lock:
                self._closed = True
                self._sock = None
            try:
                s.close()
            except OSError:
                pass
            if self.on_close:
                self._call(self.on_close)

    @staticmethod
    def _call(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:  # a bug in a callback must not kill the read thread silently
            traceback.print_exc(file=sys.stderr)
