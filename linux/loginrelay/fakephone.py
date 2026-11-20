# SPDX-License-Identifier: GPL-3.0-or-later
"""Test hook: a "USB phone" that is really a TCP socket.

Setting LOGINRELAY_FAKE_PHONE=host:port makes the app behave as if exactly one phone, called "Fake
Phone", were plugged in and already in accessory mode. pyusb is never imported. "Opening" the phone
connects to host:port, and the engine then runs exactly the logic it runs over a real USB pipe (the
phone says hello, the app pings every 2 s and expects pongs, results go back as a `callback` message
and the phone answers `delivered` or `error`). The messages are the same newline-delimited JSON; only
the 64-byte packet padding rule is skipped, because TCP has no packet boundaries to care about.
"""
from __future__ import annotations

import os
import socket
from typing import List, Optional, Tuple

from . import usbaoa
from .usbaoa import UsbConnection, UsbDevice, UsbError

ENV_VAR = "LOGINRELAY_FAKE_PHONE"
LOCATION = "fake"
NAME = "Fake Phone"
CONNECT_TIMEOUT = 3.0


def configured(env=None) -> Optional[str]:
    """The raw LOGINRELAY_FAKE_PHONE value, or None if the hook isn't in use."""
    value = (os.environ if env is None else env).get(ENV_VAR)
    return value or None


def parse_address(value: str) -> Tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("%s must look like host:port" % ENV_VAR)
    return host.strip("[]"), int(port)


class TcpConnection(UsbConnection):
    """The pipe over a socket. Reads time out the way a bulk transfer does (empty result); a closed
    socket is reported as the phone being unplugged."""

    needs_padding = False   # UsbPhoneLink skips the "extra newline at 64 bytes" rule for this pipe

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._closed = False

    def read(self, capacity: int, timeout_ms: int) -> Optional[bytes]:
        try:
            self._sock.settimeout(timeout_ms / 1000.0)
            data = self._sock.recv(capacity)
        except socket.timeout:
            return b""
        except OSError as e:
            raise UsbError(usbaoa.AOA_ERR_NO_DEVICE, str(e)) from None
        if not data:
            raise UsbError(usbaoa.AOA_ERR_NO_DEVICE, "the fake phone hung up")
        return data

    def write(self, data: bytes, timeout_ms: int) -> int:
        try:
            self._sock.settimeout(timeout_ms / 1000.0)
            return self._sock.send(data)
        except socket.timeout:
            return 0
        except OSError as e:
            raise UsbError(usbaoa.AOA_ERR_NO_DEVICE, str(e)) from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class TcpPhoneBackend(usbaoa.UsbBackend):
    """A bus with one accessory-mode phone on it, reached over TCP."""

    def __init__(self, value: str):
        self._value = value

    def _address(self) -> Tuple[str, int]:
        try:
            return parse_address(self._value)
        except ValueError as e:
            raise UsbError(usbaoa.AOA_ERR_UNAVAILABLE, str(e)) from None

    def list_devices(self) -> List[UsbDevice]:
        self._address()     # complain about a malformed value
        return [UsbDevice(usbaoa.AOA_VENDOR_GOOGLE, 0x2D01, LOCATION, True, True, NAME, "FAKE0001")]

    def switch(self, location: str, host_name: str) -> usbaoa.SwitchResult:
        return usbaoa.SwitchResult(usbaoa.AOA_OK, 2)    # already an accessory; never asked

    def open(self, location: str) -> UsbConnection:
        host, port = self._address()
        try:
            sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        except OSError as e:
            raise UsbError(usbaoa.AOA_ERR_OPEN, str(e)) from None
        return TcpConnection(sock)
