# SPDX-License-Identifier: GPL-3.0-or-later
"""Android Open Accessory (AOA) host side, on pyusb. No kernel module, no USB debugging.

An Android phone can be switched into "accessory mode" over USB without USB debugging: the computer
sends a few vendor control requests, the phone reappears as Google 18D1:2D00..2D05 with one bulk IN
and one bulk OUT endpoint on interface 0, and Android connects that pipe to the app that matches the
strings we sent. See https://source.android.com/docs/core/interaction/accessories/aoa

Port of macos/Sources/aoa_mac.c and Usb.swift. Everything that touches pyusb is in `PyUsbBackend`;
the engine only sees the small `UsbBackend` / `UsbConnection` interfaces below, so tests (and any
other OS) can supply their own. pyusb is imported lazily, inside the backend: without it this module
still imports and USB mode reports "USB mode needs the python3-usb package".
"""
from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

AOA_VENDOR_GOOGLE = 0x18D1
AOA_PID_FIRST = 0x2D00
AOA_PID_LAST = 0x2D05

AOA_REQ_GET_PROTOCOL = 51
AOA_REQ_SEND_STRING = 52
AOA_REQ_START = 53

# Android gives up on the accessory switch if the host hasn't reconnected within 10 s.
READ_BUFFER = 16384        # reads must use at least this much
WRITE_CHUNK = 16384        # writes are split into pieces of at most this many bytes
READ_TIMEOUT_MS = 200
WRITE_TIMEOUT_MS = 500

MANUFACTURER = "Login Relay"   # these two must match res/xml/accessory_filter.xml in the phone app
MODEL = "Desktop"
VERSION = "1"
URI = "https://github.com/clippycoder/login-relay"

# Result / error codes (same meaning as AOA_* in aoa_mac.h; PERMISSION, BUSY and UNAVAILABLE are new).
AOA_OK = 0
AOA_ERR_NOT_FOUND = -1     # no such device on the bus
AOA_ERR_OPEN = -2          # couldn't open the device
AOA_ERR_UNSUPPORTED = -3   # the device doesn't answer the AOA protocol request
AOA_ERR_REQUEST = -4       # a control request failed
AOA_ERR_ENDPOINTS = -5     # the accessory interface has no bulk IN/OUT pair
AOA_ERR_IO = -6            # a transfer failed
AOA_ERR_NO_DEVICE = -7     # unplugged
AOA_ERR_PERMISSION = -8    # the operating system won't let us use the device
AOA_ERR_BUSY = -9          # another program has it open
AOA_ERR_UNAVAILABLE = -10  # pyusb (or libusb) isn't installed

NEEDS_PYUSB = "USB mode needs the python3-usb package"
NO_PERMISSION = "No permission to use the phone over USB. Install the udev rule from the package, or run with sudo."


class UsbError(Exception):
    """A USB operation failed. `code` is one of the AOA_ERR_* values."""

    def __init__(self, code: int, detail: str = ""):
        super().__init__(detail or "USB error %d" % code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class UsbDevice:
    vendor_id: int
    product_id: int
    location: str               # bus + port path ("1-4.2"); stays the same across the mode switch
    is_accessory: bool          # already in accessory mode (18D1:2D00..2D05)
    looks_android: bool         # exposes an MTP/PTP (or debugging-interface) interface, so worth asking about AOA
    name: str = ""
    serial: str = ""

    @property
    def id(self) -> str:
        return self.location


@dataclass(frozen=True)
class SwitchResult:
    code: int
    protocol_version: int = 0
    detail: str = ""


class UsbConnection:
    """An open accessory pipe."""

    def read(self, capacity: int, timeout_ms: int) -> Optional[bytes]:
        """Up to `capacity` bytes (use at least READ_BUFFER). Empty bytes (or None) on a timeout,
        which is not an error. Raises UsbError if the transfer failed or the phone is gone."""
        raise NotImplementedError

    def write(self, data: bytes, timeout_ms: int) -> int:
        """Writes `data`; returns the count written, 0 on a timeout. Raises UsbError on failure."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class UsbBackend:
    """Everything the engine needs from the operating system's USB stack."""

    def list_devices(self) -> List[UsbDevice]:
        """The USB devices on the bus. Reads descriptors only; sends no traffic to devices that don't
        look like a phone. Raises UsbError(AOA_ERR_UNAVAILABLE) if there is no USB library."""
        raise NotImplementedError

    def switch(self, location: str, host_name: str) -> SwitchResult:
        """Asks the phone at `location` to switch to accessory mode. It then drops off the bus and
        reappears at the same location as an accessory."""
        raise NotImplementedError

    def open(self, location: str) -> UsbConnection:
        """Opens the accessory-mode device at `location`. Raises UsbError."""
        raise NotImplementedError


# ---- pure helpers (no USB library) -----------------------------------------------------------

def is_accessory_ids(vendor_id: int, product_id: int) -> bool:
    return vendor_id == AOA_VENDOR_GOOGLE and AOA_PID_FIRST <= product_id <= AOA_PID_LAST


def looks_like_phone(interfaces: Iterable[Tuple[int, int, int]]) -> bool:
    """Does this device expose an interface that Android phones use for MTP/PTP, or the interface class
    that marks an Android debugging interface? (Only used to recognise a phone.)
    `interfaces` are (class, subclass, protocol) triples."""
    for cls, sub, proto in interfaces:
        if (cls == 0x06 and sub == 0x01                  # still image / PTP / MTP
                or cls == 0xFF and sub == 0x42 and proto == 0x01   # debugging interface class (FF/42/01)
                or cls == 0xFF and sub == 0xFF and proto == 0x00):  # vendor-specific MTP
            return True
    return False


def format_location(bus: int, ports: Optional[Sequence[int]], address: int = 0) -> str:
    """A stable name for a physical port: bus number and port path, like Linux's "1-4.2". It
    survives the re-enumeration after the accessory switch (the device address does not). A device
    with no port path (a root hub) falls back to bus:address."""
    if ports:
        return "%d-%s" % (bus, ".".join(str(p) for p in ports))
    return "%d:%d" % (bus, address)


def fallback_name(vendor_id: int, product_id: int) -> str:
    return "Android phone (%04x:%04x)" % (vendor_id, product_id)


def accessory_strings(location: str, host_name: str) -> List[str]:
    """The six AOA strings, in index order. The first two must match the phone app's accessory
    filter; the description (shown on the phone) is the computer's name."""
    return [MANUFACTURER, MODEL, host_name or "Computer", VERSION, URI, location]


def error_text(code: int, name: str, detail: str = "") -> str:
    """What to tell the user when `code` came back for the phone called `name`."""
    if code == AOA_ERR_UNAVAILABLE:
        return detail or NEEDS_PYUSB
    if code == AOA_ERR_PERMISSION:
        return NO_PERMISSION
    if code == AOA_ERR_UNSUPPORTED:
        return "%s doesn't answer the USB accessory protocol." % name
    if code == AOA_ERR_BUSY:
        return "Another program has %s open. Close it, then choose Check Again." % name
    if code == AOA_ERR_OPEN:
        return "Couldn't open %s over USB." % name
    if code == AOA_ERR_NOT_FOUND:
        return "%s disappeared." % name
    return "Couldn't switch %s to accessory mode (error %d)." % (name, code)


def local_host_name() -> str:
    try:
        return socket.gethostname() or "Computer"
    except OSError:
        return "Computer"


# ---- the pyusb backend -----------------------------------------------------------------------

def _import_usb():
    """Imports pyusb lazily. Raises UsbError(AOA_ERR_UNAVAILABLE) if it isn't installed."""
    try:
        import usb.core  # noqa: F401
        import usb.util  # noqa: F401
    except ImportError:
        raise UsbError(AOA_ERR_UNAVAILABLE, NEEDS_PYUSB) from None
    import usb
    return usb


def _is_timeout(usb, e: Exception) -> bool:
    timeout_cls = getattr(usb.core, "USBTimeoutError", None)
    if timeout_cls is not None and isinstance(e, timeout_cls):
        return True
    if getattr(e, "errno", None) in (110, 60, 10060):   # ETIMEDOUT on Linux, macOS, Windows
        return True
    return "timed out" in str(e).lower() or "timeout" in str(e).lower()


def _classify(usb, e: Exception, default: int) -> UsbError:
    """Turns a pyusb exception into a UsbError with the most useful code."""
    errno = getattr(e, "errno", None)
    text = str(e)
    low = text.lower()
    if errno in (1, 13) or "access denied" in low or "permission" in low or "insufficient permissions" in low:
        return UsbError(AOA_ERR_PERMISSION, NO_PERMISSION)
    if errno in (16,) or "busy" in low:
        return UsbError(AOA_ERR_BUSY, text)
    if errno == 19 or "no such device" in low or "no device" in low or "not found" in low:
        return UsbError(AOA_ERR_NO_DEVICE, text)
    return UsbError(default, text)


class PyUsbConnection(UsbConnection):
    """The accessory interface (interface 0) of one device, with its bulk IN and OUT endpoints."""

    def __init__(self, usb, dev, ep_in, ep_out, detached: bool):
        self._usb = usb
        self._dev = dev
        self._in = ep_in
        self._out = ep_out
        self._detached = detached
        self._closed = False

    def read(self, capacity: int, timeout_ms: int) -> Optional[bytes]:
        usb = self._usb
        try:
            data = self._in.read(max(capacity, READ_BUFFER), timeout_ms)
        except usb.core.USBError as e:
            if _is_timeout(usb, e):
                return b""
            if getattr(e, "errno", None) == 32:    # EPIPE: the endpoint stalled; clear it and carry on
                try:
                    self._dev.clear_halt(self._in)
                except usb.core.USBError:
                    pass
                return b""
            raise _classify(usb, e, AOA_ERR_IO) from None
        return bytes(data)

    def write(self, data: bytes, timeout_ms: int) -> int:
        usb = self._usb
        try:
            return int(self._out.write(data, timeout_ms))
        except usb.core.USBError as e:
            if _is_timeout(usb, e):
                return 0
            if getattr(e, "errno", None) == 32:
                try:
                    self._dev.clear_halt(self._out)
                except usb.core.USBError:
                    pass
                return 0
            raise _classify(usb, e, AOA_ERR_IO) from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        usb = self._usb
        try:
            usb.util.release_interface(self._dev, 0)
        except Exception:
            pass
        if self._detached:
            try:
                self._dev.attach_kernel_driver(0)
            except Exception:
                pass
        try:
            usb.util.dispose_resources(self._dev)
        except Exception:
            pass


class PyUsbBackend(UsbBackend):
    def __init__(self):
        self._names: Dict[tuple, Tuple[str, str]] = {}   # (location, vid, pid, address) -> (name, serial)

    # -- finding devices

    def _find_all(self, usb) -> list:
        try:
            return list(usb.core.find(find_all=True))
        except usb.core.NoBackendError:
            raise UsbError(AOA_ERR_UNAVAILABLE,
                           "USB mode needs the libusb library (Debian/Ubuntu: libusb-1.0-0)") from None
        except usb.core.USBError as e:
            raise _classify(usb, e, AOA_ERR_IO) from None

    @staticmethod
    def _location(dev) -> str:
        ports = None
        try:
            ports = dev.port_numbers
        except Exception:
            pass
        return format_location(int(dev.bus or 0), ports, int(dev.address or 0))

    @staticmethod
    def _interfaces(dev) -> List[Tuple[int, int, int]]:
        found: List[Tuple[int, int, int]] = []
        try:
            for cfg in dev:
                for intf in cfg:
                    found.append((intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol))
        except Exception:      # descriptors we can't read: treat as "not a phone"
            pass
        return found

    def _strings(self, usb, dev, location: str) -> Tuple[str, str]:
        """Product and serial strings. Reading them opens the device, which can fail without
        permission; fall back to a generic name. Cached per attachment so polling doesn't reopen."""
        key = (location, dev.idVendor, dev.idProduct, dev.address)
        cached = self._names.get(key)
        if cached is not None:
            return cached
        name = serial = ""
        try:
            name = (dev.product or "") if dev.iProduct else ""
        except Exception:      # no permission, no language id, device busy...
            name = ""
        try:
            serial = (dev.serial_number or "") if dev.iSerialNumber else ""
        except Exception:
            serial = ""
        result = (name.strip() or fallback_name(dev.idVendor, dev.idProduct), serial.strip())
        self._names[key] = result
        return result

    def list_devices(self) -> List[UsbDevice]:
        usb = _import_usb()
        devs = self._find_all(usb)
        out: List[UsbDevice] = []
        seen = set()
        try:
            for dev in devs:
                try:
                    vid, pid = int(dev.idVendor), int(dev.idProduct)
                    location = self._location(dev)
                    accessory = is_accessory_ids(vid, pid)
                    phone = accessory or looks_like_phone(self._interfaces(dev))
                    name = serial = ""
                    if phone:   # never open anything that isn't a phone
                        name, serial = self._strings(usb, dev, location)
                        seen.add((location, vid, pid, dev.address))
                    out.append(UsbDevice(vid, pid, location, accessory, phone, name, serial))
                except Exception:
                    continue
        finally:
            for dev in devs:
                try:
                    usb.util.dispose_resources(dev)   # no handle may outlive a poll
                except Exception:
                    pass
        for key in [k for k in self._names if k not in seen]:
            del self._names[key]
        return out

    def _find(self, usb, location: str):
        for dev in self._find_all(usb):
            try:
                if self._location(dev) == location:
                    return dev
            except Exception:
                continue
        return None

    # -- the switch

    def switch(self, location: str, host_name: str) -> SwitchResult:
        usb = _import_usb()
        dev = self._find(usb, location)
        if dev is None:
            return SwitchResult(AOA_ERR_NOT_FOUND)
        version = 0
        try:
            try:
                raw = dev.ctrl_transfer(0xC0, AOA_REQ_GET_PROTOCOL, 0, 0, 2, 1000)
            except usb.core.USBError as e:
                err = _classify(usb, e, AOA_ERR_UNSUPPORTED)
                if err.code in (AOA_ERR_PERMISSION, AOA_ERR_BUSY, AOA_ERR_NO_DEVICE):
                    return SwitchResult(AOA_ERR_NOT_FOUND if err.code == AOA_ERR_NO_DEVICE else err.code,
                                        0, err.detail)
                return SwitchResult(AOA_ERR_UNSUPPORTED, 0, str(e))
            if len(raw) >= 2:
                version = raw[0] | (raw[1] << 8)
            if version < 1:
                return SwitchResult(AOA_ERR_UNSUPPORTED, version)
            for index, text in enumerate(accessory_strings(location, host_name)):
                try:
                    dev.ctrl_transfer(0x40, AOA_REQ_SEND_STRING, 0, index, text.encode("utf-8") + b"\0", 1000)
                except usb.core.USBError as e:
                    return SwitchResult(AOA_ERR_REQUEST, version, str(e))
            try:
                dev.ctrl_transfer(0x40, AOA_REQ_START, 0, 0, None, 1000)
            except usb.core.USBError:
                pass    # the phone drops off the bus while handling this, so an error is expected
            return SwitchResult(AOA_OK, version)
        finally:
            try:
                usb.util.dispose_resources(dev)
            except Exception:
                pass

    # -- the pipe

    def open(self, location: str) -> UsbConnection:
        usb = _import_usb()
        dev = self._find(usb, location)
        if dev is None:
            raise UsbError(AOA_ERR_NOT_FOUND, "no device at %s" % location)
        if not is_accessory_ids(int(dev.idVendor), int(dev.idProduct)):
            usb.util.dispose_resources(dev)
            raise UsbError(AOA_ERR_NOT_FOUND, "the device at %s isn't in accessory mode" % location)
        detached = False
        try:
            try:
                cfg = dev.get_active_configuration()
            except usb.core.USBError:
                dev.set_configuration()
                cfg = dev.get_active_configuration()
            # The accessory is interface 0 (with 18D1:2D01 there is a second, unrelated interface: leave it alone).
            intf = usb.util.find_descriptor(cfg, bInterfaceNumber=0)
            if intf is None:
                raise UsbError(AOA_ERR_ENDPOINTS, "no accessory interface")

            def bulk(direction):
                return lambda e: (usb.util.endpoint_direction(e.bEndpointAddress) == direction
                                  and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)

            ep_in = usb.util.find_descriptor(intf, custom_match=bulk(usb.util.ENDPOINT_IN))
            ep_out = usb.util.find_descriptor(intf, custom_match=bulk(usb.util.ENDPOINT_OUT))
            if ep_in is None or ep_out is None:
                raise UsbError(AOA_ERR_ENDPOINTS, "the accessory interface has no bulk endpoint pair")
            # Detach a kernel driver only if one is active (not implemented on every platform).
            try:
                if dev.is_kernel_driver_active(0):
                    dev.detach_kernel_driver(0)
                    detached = True
            except (NotImplementedError, usb.core.USBError, AttributeError):
                pass
            usb.util.claim_interface(dev, 0)
            return PyUsbConnection(usb, dev, ep_in, ep_out, detached)
        except UsbError:
            self._dispose(usb, dev)
            raise
        except usb.core.USBError as e:
            self._dispose(usb, dev)
            raise _classify(usb, e, AOA_ERR_OPEN) from None
        except Exception as e:      # ValueError from descriptors that went away mid-read, etc.
            self._dispose(usb, dev)
            raise UsbError(AOA_ERR_OPEN, str(e)) from None

    @staticmethod
    def _dispose(usb, dev) -> None:
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


def default_backend() -> UsbBackend:
    """The backend the app uses. Cheap: pyusb is only imported when the backend is first used."""
    return PyUsbBackend()
