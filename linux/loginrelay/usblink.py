# SPDX-License-Identifier: GPL-3.0-or-later
"""The connection to the Login Relay app on the phone over a USB accessory pipe.

Same line-delimited JSON as the adb link (phonelink.py). Bulk USB has no "connection closed", so the
link pings the phone and tracks whether anything has come back recently ("alive").
Port of UsbPhoneLink in macos/Sources/Usb.swift.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from typing import Callable, Optional

from .usbaoa import READ_BUFFER, READ_TIMEOUT_MS, WRITE_CHUNK, WRITE_TIMEOUT_MS, UsbConnection, UsbError
from .usbframing import LineSplitter, usb_frame

PING_INTERVAL = 2.0
SILENCE = 8.0


class UsbPhoneLink:
    def __init__(self, conn: UsbConnection, *, ping_interval: float = PING_INTERVAL, silence: float = SILENCE,
                 clock: Callable[[], float] = time.monotonic):
        # Called on the read thread for each JSON object from the phone.
        self.on_message: Optional[Callable[[dict], None]] = None
        # Called once, on the read thread, when the link ends.
        self.on_close: Optional[Callable[[], None]] = None
        # Called on the read thread when the phone app starts or stops answering.
        self.on_alive: Optional[Callable[[bool], None]] = None
        self._conn = conn
        self._ping_interval = ping_interval
        self._silence = silence
        self._clock = clock
        self._write_lock = threading.Lock()
        self._closed = False             # the pipe is closed (guarded by _write_lock)
        self._stopping = threading.Event()

    @property
    def closed(self) -> bool:
        return self._stopping.is_set() or self._closed

    def start(self) -> None:
        threading.Thread(target=self._read_loop, name="usb-link", daemon=True).start()

    def send(self, obj: dict) -> None:
        frame = usb_frame(json.dumps(obj, separators=(",", ":")).encode("utf-8"))
        with self._write_lock:
            if self._closed or self._stopping.is_set():
                return
            offset = 0
            while offset < len(frame):
                chunk = frame[offset:offset + WRITE_CHUNK]
                try:
                    written = self._conn.write(chunk, WRITE_TIMEOUT_MS)
                except UsbError:
                    return      # the read loop will see the same failure and end the link
                # A timeout means the phone app isn't reading; drop the message. Liveness covers the rest.
                if written <= 0:
                    return
                offset += written

    def close(self) -> None:
        """Ends the link; the read thread notices within a fraction of a second."""
        self._stopping.set()

    def _read_loop(self) -> None:
        splitter = LineSplitter()
        last_heard = None            # never
        last_ping = None
        alive = False
        try:
            while not self._stopping.is_set():
                try:
                    data = self._conn.read(READ_BUFFER, READ_TIMEOUT_MS)
                except UsbError:
                    return
                now = self._clock()
                if data:
                    last_heard = now
                    lines = splitter.feed(data)
                    if lines is None:
                        return
                    for line in lines:
                        try:
                            obj = json.loads(line.decode("utf-8"))
                        except ValueError:
                            continue
                        if isinstance(obj, dict) and self.on_message:
                            self._call(self.on_message, obj)
                if last_ping is None or now - last_ping > self._ping_interval:
                    self.send({"type": "ping"})
                    last_ping = now
                now_alive = last_heard is not None and now - last_heard < self._silence
                if now_alive != alive:
                    alive = now_alive
                    if self.on_alive:
                        self._call(self.on_alive, alive)
        finally:
            with self._write_lock:
                self._closed = True
                try:
                    self._conn.close()
                except Exception:
                    traceback.print_exc(file=sys.stderr)
            if self.on_close:
                self._call(self.on_close)

    @staticmethod
    def _call(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:  # a bug in a callback must not kill the read thread silently
            traceback.print_exc(file=sys.stderr)
