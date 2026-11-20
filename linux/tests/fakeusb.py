# SPDX-License-Identifier: GPL-3.0-or-later
"""Fakes for the USB accessory layer: a backend whose "phones" speak the Login Relay protocol through
in-memory queues, and a stand-in for the `usb` (pyusb) package for testing PyUsbBackend."""
import json
import os
import queue
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loginrelay import usbaoa  # noqa: E402
from loginrelay.usbframing import LineSplitter, usb_frame  # noqa: E402


class FakePhone:
    """The Login Relay app on a phone, as seen through the accessory pipe."""

    def __init__(self, version="0.5.0"):
        self.version = version
        self.app_running = True          # False: nothing answers on the other end of the pipe
        self.answer_ping = True
        self.hello_on_connect = True
        self.delivery_reply = "delivered"   # "delivered", "error:<text>" or None (stay silent)
        self.received = []               # every JSON message the computer sent
        self.to_host = queue.Queue()     # byte chunks (or None = unplugged)
        self.connections = 0
        self._splitter = LineSplitter()
        self._lock = threading.Lock()
        self.unplugged = False

    # -- phone -> computer
    def send(self, obj, split=False):
        data = usb_frame(json.dumps(obj).encode())
        if split:
            third = max(1, len(data) // 3)
            for i in range(0, len(data), third):
                self.to_host.put(data[i:i + third])
        else:
            self.to_host.put(data)

    def send_open(self, url, referrer="android-app://com.example.app", label="Example App"):
        self.send({"v": 1, "type": "open", "url": url, "referrer": referrer, "label": label})

    # -- computer -> phone
    def receive(self, data):
        with self._lock:
            lines = self._splitter.feed(data) or []
        for line in lines:
            msg = json.loads(line)
            self.received.append(msg)
            if not self.app_running:
                continue
            kind = msg.get("type")
            if kind == "ping" and self.answer_ping:
                self.send({"type": "pong"})
            elif kind == "callback" and self.delivery_reply:
                if self.delivery_reply == "delivered":
                    self.send({"type": "delivered"})
                else:
                    self.send({"type": "error", "text": self.delivery_reply.split(":", 1)[1]})

    def messages(self, kind):
        return [m for m in list(self.received) if m.get("type") == kind]

    def unplug(self):
        self.unplugged = True
        self.to_host.put(None)


class FakeConnection(usbaoa.UsbConnection):
    def __init__(self, phone):
        self.phone = phone
        self.closed = False
        self.reads = 0
        self.write_sizes = []
        self.read_capacities = []

    def read(self, capacity, timeout_ms):
        self.read_capacities.append(capacity)
        self.reads += 1
        if self.phone.unplugged:
            raise usbaoa.UsbError(usbaoa.AOA_ERR_NO_DEVICE, "unplugged")
        try:
            chunk = self.phone.to_host.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return b""
        if chunk is None:
            self.phone.to_host.put(None)
            raise usbaoa.UsbError(usbaoa.AOA_ERR_NO_DEVICE, "unplugged")
        return chunk

    def write(self, data, timeout_ms):
        if self.phone.unplugged:
            raise usbaoa.UsbError(usbaoa.AOA_ERR_NO_DEVICE, "unplugged")
        assert len(data) <= usbaoa.WRITE_CHUNK, "writes must be chunked to at most 16384 bytes"
        self.write_sizes.append(len(data))
        self.phone.receive(data)
        return len(data)

    def close(self):
        self.closed = True


class FakeUsbWorld(usbaoa.UsbBackend):
    """A USB bus with phones on it. `reenumerate_after` is how long a phone takes to come back as an
    accessory after the switch (None = never comes back)."""

    def __init__(self, reenumerate_after=0.15):
        self.lock = threading.Lock()
        self.entries = {}                # location -> dict
        self.phones = {}                 # location -> FakePhone
        self.reenumerate_after = reenumerate_after
        self.switch_calls = []           # (location, host_name)
        self.switch_times = []
        self.open_calls = []
        self.connections = []
        self.list_calls = 0
        self.ignore_switch = False       # accept the switch request but stay as we are
        self.switch_error = None         # a SwitchResult to return instead of switching
        self.open_error = None           # a UsbError to raise from open()
        self.list_error = None           # a UsbError to raise from list_devices()

    # -- building the bus
    def add_phone(self, location="1-4", name="Pixel 7", vid=0x18D1, pid=0x4EE1, accessory=False, phone=None,
                  serial="SER123"):
        phone = phone or FakePhone()
        with self.lock:
            self.entries[location] = dict(vid=vid, pid=pid, name=name, serial=serial, looks_android=True,
                                          accessory=accessory, back_at=None)
            self.phones[location] = phone
        return phone

    def add_other(self, location="1-7", vid=0x05C6, pid=0x9008, name="QUSB__BULK"):
        """A device that is not a phone (Qualcomm download mode): looks_android is False."""
        with self.lock:
            self.entries[location] = dict(vid=vid, pid=pid, name=name, serial="", looks_android=False,
                                          accessory=False, back_at=None)

    def unplug(self, location):
        with self.lock:
            self.entries.pop(location, None)
            phone = self.phones.pop(location, None)
        if phone is not None:
            phone.unplug()

    # -- the backend interface
    def list_devices(self):
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        out = []
        now = time.monotonic()
        with self.lock:
            for location, e in self.entries.items():
                if e["back_at"] is not None:
                    if now < e["back_at"]:
                        continue            # off the bus while it re-enumerates
                    e.update(back_at=None, accessory=True, vid=0x18D1, pid=0x2D01)
                out.append(usbaoa.UsbDevice(e["vid"], e["pid"], location, e["accessory"], e["looks_android"],
                                            e["name"], e["serial"]))
        return out

    def switch(self, location, host_name):
        self.switch_calls.append((location, host_name))
        self.switch_times.append(time.monotonic())
        if self.switch_error is not None:
            return self.switch_error
        if self.ignore_switch:
            return usbaoa.SwitchResult(usbaoa.AOA_OK, 2)
        with self.lock:
            e = self.entries.get(location)
            if e is None:
                return usbaoa.SwitchResult(usbaoa.AOA_ERR_NOT_FOUND)
            if self.reenumerate_after is not None:
                e["back_at"] = time.monotonic() + self.reenumerate_after
            else:
                e["back_at"] = time.monotonic() + 10 ** 6
        return usbaoa.SwitchResult(usbaoa.AOA_OK, 2)

    def open(self, location):
        self.open_calls.append(location)
        if self.open_error is not None:
            raise self.open_error
        with self.lock:
            e = self.entries.get(location)
            phone = self.phones.get(location)
            if e is None or phone is None or not e["accessory"]:
                raise usbaoa.UsbError(usbaoa.AOA_ERR_NOT_FOUND, "no accessory at %s" % location)
        phone.connections += 1
        conn = FakeConnection(phone)
        self.connections.append(conn)
        if phone.app_running and phone.hello_on_connect:
            phone.send({"v": 1, "type": "hello", "transport": "usb", "version": phone.version})
        return conn


# ---- a stand-in for the pyusb package --------------------------------------------------------

class FakeUsbError(Exception):
    def __init__(self, strerror="", errno=None):
        super().__init__(strerror)
        self.errno = errno


class FakeTimeout(FakeUsbError):
    pass


class FakeNoBackend(FakeUsbError):
    pass


class FakeEndpoint:
    def __init__(self, address, attributes, owner):
        self.bEndpointAddress = address
        self.bmAttributes = attributes
        self.owner = owner
        self.to_read = []        # items: bytes, or an exception to raise
        self.written = []
        self.write_error = None

    def read(self, size, timeout=None):
        self.owner.reads.append((size, timeout))
        if not self.to_read:
            raise FakeTimeout("Operation timed out", 110)
        item = self.to_read.pop(0)
        if isinstance(item, Exception):
            raise item
        return list(item)

    def write(self, data, timeout=None):
        if self.write_error is not None:
            raise self.write_error
        self.written.append((bytes(data), timeout))
        return len(data)


class FakeInterface:
    def __init__(self, number, cls, sub, proto, endpoints=()):
        self.bInterfaceNumber = number
        self.bInterfaceClass = cls
        self.bInterfaceSubClass = sub
        self.bInterfaceProtocol = proto
        self.endpoints = list(endpoints)

    def __iter__(self):
        return iter(self.endpoints)


class FakeConfig:
    def __init__(self, interfaces):
        self.interfaces = interfaces

    def __iter__(self):
        return iter(self.interfaces)


class FakeDevice:
    def __init__(self, vid, pid, bus, address, ports, interfaces, product="Pixel 7", serial="SER123"):
        self.idVendor, self.idProduct = vid, pid
        self.bus, self.address, self.port_numbers = bus, address, ports
        self.iProduct, self.iSerialNumber = (1 if product else 0), (2 if serial else 0)
        self._product, self._serial = product, serial
        self.interfaces = interfaces
        self.product_reads = 0
        self.string_error = None
        self.ctrl = []           # (bmRequestType, bRequest, wValue, wIndex, data_or_length)
        self.ctrl_error = {}     # bRequest -> exception
        self.protocol = (2).to_bytes(2, "little")
        self.reads = []
        self.kernel_driver = False
        self.detached = []
        self.attached = []
        self.claimed, self.released = [], []
        self.disposed = 0
        self.configured = True
        for intf in interfaces:
            for ep in intf.endpoints:
                ep.owner = self

    @property
    def product(self):
        self.product_reads += 1
        if self.string_error:
            raise self.string_error
        return self._product

    @property
    def serial_number(self):
        if self.string_error:
            raise self.string_error
        return self._serial

    def __iter__(self):
        return iter([FakeConfig(self.interfaces)])

    def ctrl_transfer(self, bmRequestType, bRequest, wValue=0, wIndex=0, data_or_wLength=None, timeout=None):
        self.ctrl.append((bmRequestType, bRequest, wValue, wIndex,
                          bytes(data_or_wLength) if isinstance(data_or_wLength, (bytes, bytearray)) else data_or_wLength))
        if bRequest in self.ctrl_error:
            raise self.ctrl_error[bRequest]
        if bmRequestType & 0x80:
            return list(self.protocol[:data_or_wLength])
        return len(data_or_wLength or b"")

    def get_active_configuration(self):
        if not self.configured:
            raise FakeUsbError("no configuration", 2)
        return FakeConfig(self.interfaces)

    def set_configuration(self):
        self.configured = True

    def is_kernel_driver_active(self, n):
        return self.kernel_driver

    def detach_kernel_driver(self, n):
        self.detached.append(n)
        self.kernel_driver = False

    def attach_kernel_driver(self, n):
        self.attached.append(n)

    def clear_halt(self, ep):
        self.halts = getattr(self, "halts", []) + [ep]


def make_fake_usb(devices):
    """Returns the (usb, usb.core, usb.util) modules, finding `devices`."""
    usb = types.ModuleType("usb")
    core = types.ModuleType("usb.core")
    util = types.ModuleType("usb.util")
    usb.core, usb.util = core, util
    core.USBError, core.USBTimeoutError, core.NoBackendError = FakeUsbError, FakeTimeout, FakeNoBackend
    state = types.SimpleNamespace(devices=devices, find_error=None)
    usb.state = state

    def find(find_all=False, **_kw):
        if state.find_error:
            raise state.find_error
        return list(state.devices)

    core.find = find
    util.ENDPOINT_IN, util.ENDPOINT_OUT, util.ENDPOINT_TYPE_BULK = 0x80, 0x00, 0x02
    util.endpoint_direction = lambda addr: addr & 0x80
    util.endpoint_type = lambda attrs: attrs & 0x03

    def dispose_resources(dev):
        dev.disposed += 1

    def claim_interface(dev, n):
        dev.claimed.append(n)

    def release_interface(dev, n):
        dev.released.append(n)

    def find_descriptor(desc, find_all=False, custom_match=None, **args):
        for item in desc:
            if all(getattr(item, k) == v for k, v in args.items()) and (custom_match is None or custom_match(item)):
                return item
        return None

    util.dispose_resources, util.claim_interface, util.release_interface = (
        dispose_resources, claim_interface, release_interface)
    util.find_descriptor = find_descriptor
    return usb, core, util


def phone_interfaces():
    """A phone in file-transfer mode: MTP and ADB interfaces."""
    return [FakeInterface(0, 0xFF, 0xFF, 0x00), FakeInterface(1, 0xFF, 0x42, 0x01)]


def accessory_interfaces(with_extra_interface=False):
    eps = [FakeEndpoint(0x81, 0x02, None), FakeEndpoint(0x01, 0x02, None)]
    out = [FakeInterface(0, 0xFF, 0x00, 0x00, eps)]
    if with_extra_interface:
        out.append(FakeInterface(1, 0xFF, 0x42, 0x01, [FakeEndpoint(0x82, 0x02, None), FakeEndpoint(0x02, 0x02, None)]))
    return out
