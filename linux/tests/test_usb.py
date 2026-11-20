# SPDX-License-Identifier: GPL-3.0-or-later
"""USB accessory mode: the helpers, the link, the pyusb backend (against a stand-in for pyusb) and the
engine, driven end to end by a fake USB bus with fake phones. Standard library only; pyusb is not needed."""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import SCHEME, authorize_url, stable_fds, assert_fds_flat, wait_until  # noqa: E402
from fakeusb import (FakeConnection, FakeDevice, FakeInterface, FakePhone, FakeUsbError,  # noqa: E402
                     FakeUsbWorld, FakeTimeout, accessory_interfaces, make_fake_usb, phone_interfaces)
from test_engine import FakeHost  # noqa: E402

from loginrelay import engine as eng  # noqa: E402
from loginrelay import usbaoa  # noqa: E402
from loginrelay.prefs import Prefs  # noqa: E402
from loginrelay.usblink import UsbPhoneLink  # noqa: E402

NO_PERMISSION = "No permission to use the phone over USB. Install the udev rule from the package, or run with sudo."


# ---- pure helpers ----------------------------------------------------------------------------

class HelperTests(unittest.TestCase):
    def test_accessory_ids(self):
        self.assertTrue(all(usbaoa.is_accessory_ids(0x18D1, p) for p in range(0x2D00, 0x2D06)))
        self.assertFalse(usbaoa.is_accessory_ids(0x18D1, 0x2D06))
        self.assertFalse(usbaoa.is_accessory_ids(0x18D1, 0x4EE1))
        self.assertFalse(usbaoa.is_accessory_ids(0x05C6, 0x2D00))

    def test_which_devices_look_like_phones(self):
        self.assertTrue(usbaoa.looks_like_phone([(0x06, 0x01, 0x01)]))             # PTP
        self.assertTrue(usbaoa.looks_like_phone([(0x06, 0x01, 0x00)]))
        self.assertTrue(usbaoa.looks_like_phone([(0xFF, 0x42, 0x01)]))             # ADB interface class (recognises the phone only)
        self.assertTrue(usbaoa.looks_like_phone([(0xFF, 0xFF, 0x00)]))             # vendor MTP
        self.assertTrue(usbaoa.looks_like_phone([(0x08, 0x06, 0x50), (0xFF, 0x42, 0x01)]))
        self.assertFalse(usbaoa.looks_like_phone([(0xFF, 0xFF, 0xFF)]))            # Qualcomm download mode
        self.assertFalse(usbaoa.looks_like_phone([(0x09, 0x00, 0x00)]))            # hub
        self.assertFalse(usbaoa.looks_like_phone([(0x03, 0x01, 0x02)]))            # mouse
        self.assertFalse(usbaoa.looks_like_phone([(0xFF, 0x42, 0x02)]))
        self.assertFalse(usbaoa.looks_like_phone([]))

    def test_locations_are_stable_port_paths(self):
        self.assertEqual(usbaoa.format_location(1, (4, 2), 17), "1-4.2")
        self.assertEqual(usbaoa.format_location(3, [1], 5), "3-1")
        self.assertEqual(usbaoa.format_location(2, None, 9), "2:9")      # no port path: best effort
        self.assertEqual(usbaoa.format_location(2, (), 9), "2:9")

    def test_the_six_accessory_strings(self):
        self.assertEqual(usbaoa.accessory_strings("1-4.2", "My PC"),
                         ["Login Relay", "Desktop", "My PC", "1", "https://github.com/clippycoder/login-relay",
                          "1-4.2"])

    def test_protocol_constants(self):
        self.assertEqual((usbaoa.AOA_REQ_GET_PROTOCOL, usbaoa.AOA_REQ_SEND_STRING, usbaoa.AOA_REQ_START), (51, 52, 53))
        self.assertEqual((usbaoa.READ_BUFFER, usbaoa.WRITE_CHUNK), (16384, 16384))
        self.assertEqual((usbaoa.READ_TIMEOUT_MS, usbaoa.WRITE_TIMEOUT_MS), (200, 500))

    def test_importing_the_engine_does_not_import_pyusb(self):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("import sys; sys.path.insert(0, %r); import loginrelay.engine, loginrelay.usbaoa, "
                "loginrelay.usblink, loginrelay.usbframing; "
                "sys.exit(1 if any(m == 'usb' or m.startswith('usb.') for m in sys.modules) else 0)" % root)
        self.assertEqual(subprocess.run([sys.executable, "-I", "-c", code]).returncode, 0)

    def test_error_texts(self):
        self.assertEqual(usbaoa.error_text(usbaoa.AOA_ERR_PERMISSION, "X"), NO_PERMISSION)
        self.assertEqual(usbaoa.error_text(usbaoa.AOA_ERR_UNAVAILABLE, "X"), "USB mode needs the python3-usb package")
        self.assertEqual(usbaoa.error_text(usbaoa.AOA_ERR_UNSUPPORTED, "Pixel"),
                         "Pixel doesn't answer the USB accessory protocol.")
        self.assertEqual(usbaoa.error_text(usbaoa.AOA_ERR_BUSY, "Pixel"), "Another program has Pixel open.")
        self.assertEqual(usbaoa.error_text(usbaoa.AOA_ERR_OPEN, "Pixel"), "Couldn't open Pixel over USB.")
        self.assertEqual(usbaoa.error_text(usbaoa.AOA_ERR_NOT_FOUND, "Pixel"), "Pixel disappeared.")
        self.assertEqual(usbaoa.error_text(usbaoa.AOA_ERR_REQUEST, "Pixel"),
                         "Couldn't switch Pixel to accessory mode (error -4).")


# ---- the link --------------------------------------------------------------------------------

class UsbLinkTests(unittest.TestCase):
    def make(self, **kw):
        phone = FakePhone()
        conn = FakeConnection(phone)
        args = dict(ping_interval=0.15, silence=0.8)
        args.update(kw)
        link = UsbPhoneLink(conn, **args)
        self.messages, self.alive, self.closed = [], [], threading.Event()
        link.on_message = self.messages.append
        link.on_alive = self.alive.append
        link.on_close = self.closed.set
        self.addCleanup(self.finish, link, conn)
        return phone, conn, link

    def finish(self, link, conn):
        link.close()
        self.closed.wait(3)

    def test_defaults_match_the_spec(self):
        link = UsbPhoneLink(FakeConnection(FakePhone()))
        self.assertEqual((link._ping_interval, link._silence), (2.0, 8.0))

    def test_messages_in_and_out_and_pings(self):
        phone, conn, link = self.make()
        link.start()
        phone.send({"type": "hello", "version": "9"})
        wait_until(lambda: self.messages, 3, "the hello")
        self.assertEqual(self.messages[0]["version"], "9")
        link.send({"type": "status", "text": "hi"})
        wait_until(lambda: phone.messages("status"), 3, "the status")
        wait_until(lambda: len(phone.messages("ping")) >= 3, 3, "pings")
        self.assertTrue(all(c >= 16384 for c in conn.read_capacities))

    def test_messages_split_across_reads_and_blank_lines(self):
        phone, conn, link = self.make()
        phone.answer_ping = False
        link.start()
        phone.send({"type": "open", "url": "https://example.com/" + "x" * 300}, split=True)
        phone.to_host.put(b"\n\n   not json\n[1,2]\n")
        phone.send({"type": "cancel"})
        wait_until(lambda: len(self.messages) >= 2, 3, "both messages")
        self.assertEqual([m["type"] for m in self.messages], ["open", "cancel"])

    def test_alive_follows_what_the_phone_says(self):
        phone, conn, link = self.make()
        phone.answer_ping = False
        link.start()
        time.sleep(0.5)
        self.assertEqual(self.alive, [])                    # nothing heard yet
        phone.send({"type": "pong"})
        wait_until(lambda: self.alive == [True], 3, "alive")
        wait_until(lambda: self.alive == [True, False], 3, "silence")   # 0.8 s without a word
        phone.send({"type": "pong"})
        wait_until(lambda: self.alive == [True, False, True], 3, "alive again")

    def test_writes_are_framed_padded_and_chunked(self):
        phone, conn, link = self.make(ping_interval=1000)
        link.start()
        wait_until(lambda: conn.write_sizes, 3, "the first ping")
        conn.write_sizes.clear()
        # {"type":"status","text":"..."} with a text sized so that json + newline is exactly 64 bytes.
        base = len('{"type":"status","text":""}') + 1
        link.send({"type": "status", "text": "x" * (64 - base)})
        wait_until(lambda: conn.write_sizes, 3, "the write")
        self.assertEqual(conn.write_sizes, [65])            # padded with a second newline
        conn.write_sizes.clear()
        link.send({"type": "callback", "url": "https://x/" + "a" * 40000})
        wait_until(lambda: len(conn.write_sizes) >= 3, 3, "the chunks")
        self.assertTrue(all(n <= 16384 for n in conn.write_sizes))
        self.assertEqual(conn.write_sizes[:2], [16384, 16384])
        wait_until(lambda: phone.messages("callback"), 3, "the callback to arrive whole")
        self.assertEqual(len(phone.messages("callback")[0]["url"]), 40000 + len("https://x/"))

    def test_unplugging_ends_the_link_and_closes_the_pipe(self):
        phone, conn, link = self.make()
        link.start()
        phone.unplug()
        self.assertTrue(self.closed.wait(3))
        self.assertTrue(conn.closed)
        self.assertTrue(link.closed)
        link.send({"type": "ping"})                         # after the end: silently dropped

    def test_close_ends_the_read_thread(self):
        phone, conn, link = self.make()
        link.start()
        link.close()
        self.assertTrue(self.closed.wait(2))
        self.assertTrue(conn.closed)

    def test_a_line_that_never_ends_breaks_the_link(self):
        phone, conn, link = self.make()
        link.start()
        for _ in range(65):
            phone.to_host.put(b"A" * 16384)
        self.assertTrue(self.closed.wait(5))

    def test_a_blocked_write_is_dropped_not_fatal(self):
        phone, conn, link = self.make(ping_interval=1000)
        conn.write = lambda data, timeout_ms: 0              # the phone isn't reading
        link.start()
        link.send({"type": "status", "text": "x"})
        time.sleep(0.4)
        self.assertFalse(link.closed)


# ---- the pyusb backend, against a stand-in for the package -----------------------------------

class PyUsbBackendTests(unittest.TestCase):
    def setUp(self):
        self.phone = FakeDevice(0x18D1, 0x4EE1, 1, 12, (4, 2), phone_interfaces())
        self.other = FakeDevice(0x05C6, 0x9008, 1, 13, (7,), [FakeInterface(0, 0xFF, 0xFF, 0xFF)], "QUSB__BULK", "")
        self.hub = FakeDevice(0x1D6B, 0x0002, 1, 1, None, [FakeInterface(0, 0x09, 0x00, 0x00)], "Root hub", "")
        self.usb, self.core, self.util = make_fake_usb([self.phone, self.other, self.hub])
        patcher = mock.patch.dict(sys.modules, {"usb": self.usb, "usb.core": self.core, "usb.util": self.util})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = usbaoa.PyUsbBackend()

    def test_list_marks_phones_and_never_opens_anything_else(self):
        devs = {d.location: d for d in self.backend.list_devices()}
        self.assertEqual(set(devs), {"1-4.2", "1-7", "1:1"})
        p = devs["1-4.2"]
        self.assertEqual((p.vendor_id, p.product_id, p.looks_android, p.is_accessory, p.name, p.serial),
                         (0x18D1, 0x4EE1, True, False, "Pixel 7", "SER123"))
        self.assertFalse(devs["1-7"].looks_android)
        self.assertFalse(devs["1:1"].looks_android)
        self.assertEqual((self.other.product_reads, self.hub.product_reads), (0, 0))   # strings only for phones
        self.assertEqual(self.other.ctrl + self.hub.ctrl + self.phone.ctrl, [])        # no traffic at all
        self.assertTrue(all(d.disposed >= 1 for d in (self.phone, self.other, self.hub)))   # handles released

    def test_names_are_cached_between_polls(self):
        self.backend.list_devices()
        self.backend.list_devices()
        self.assertEqual(self.phone.product_reads, 1)

    def test_a_name_that_cant_be_read_falls_back(self):
        self.phone.string_error = FakeUsbError("Access denied (insufficient permissions)", 13)
        p = [d for d in self.backend.list_devices() if d.location == "1-4.2"][0]
        self.assertEqual((p.name, p.serial), ("Android phone (18d1:4ee1)", ""))
        self.assertTrue(p.looks_android)

    def test_accessory_mode_device_is_recognised(self):
        acc = FakeDevice(0x18D1, 0x2D01, 1, 14, (4, 2), accessory_interfaces(True))
        self.usb.state.devices = [acc]
        d = self.backend.list_devices()[0]
        self.assertTrue(d.is_accessory and d.looks_android)
        self.assertEqual(d.location, "1-4.2")        # same location as before the switch

    def test_switch_sends_the_aoa_requests(self):
        r = self.backend.switch("1-4.2", "Test Mac")
        self.assertEqual((r.code, r.protocol_version), (usbaoa.AOA_OK, 2))
        c = self.phone.ctrl
        self.assertEqual(c[0], (0xC0, 51, 0, 0, 2))
        self.assertEqual([(x[0], x[1], x[2], x[3]) for x in c[1:7]], [(0x40, 52, 0, i) for i in range(6)])
        self.assertEqual([x[4] for x in c[1:7]],
                         [b"Login Relay\0", b"Desktop\0", b"Test Mac\0", b"1\0",
                          b"https://github.com/clippycoder/login-relay\0", b"1-4.2\0"])
        self.assertEqual(c[7], (0x40, 53, 0, 0, None))
        self.assertEqual(len(c), 8)
        self.assertGreaterEqual(self.phone.disposed, 1)

    def test_an_error_on_the_start_request_is_expected(self):
        self.phone.ctrl_error[53] = FakeUsbError("No such device (it may have been disconnected)", 19)
        self.assertEqual(self.backend.switch("1-4.2", "x").code, usbaoa.AOA_OK)

    def test_old_protocol_or_no_answer_is_unsupported(self):
        self.phone.protocol = (0).to_bytes(2, "little")
        r = self.backend.switch("1-4.2", "x")
        self.assertEqual((r.code, r.protocol_version), (usbaoa.AOA_ERR_UNSUPPORTED, 0))
        self.assertEqual(len(self.phone.ctrl), 1)        # nothing was sent after the protocol check
        self.phone.ctrl.clear()
        self.phone.ctrl_error[51] = FakeUsbError("Pipe error", 32)
        self.assertEqual(self.backend.switch("1-4.2", "x").code, usbaoa.AOA_ERR_UNSUPPORTED)

    def test_switch_permission_denied(self):
        self.phone.ctrl_error[51] = FakeUsbError("Access denied (insufficient permissions)", 13)
        r = self.backend.switch("1-4.2", "x")
        self.assertEqual(r.code, usbaoa.AOA_ERR_PERMISSION)

    def test_a_failing_string_request(self):
        self.phone.ctrl_error[52] = FakeUsbError("Input/Output Error", 5)
        self.assertEqual(self.backend.switch("1-4.2", "x").code, usbaoa.AOA_ERR_REQUEST)

    def test_switch_unknown_location(self):
        self.assertEqual(self.backend.switch("9-9", "x").code, usbaoa.AOA_ERR_NOT_FOUND)

    def test_open_claims_only_interface_zero_and_finds_the_bulk_pair(self):
        acc = FakeDevice(0x18D1, 0x2D01, 1, 14, (4, 2), accessory_interfaces(True))
        self.usb.state.devices = [acc]
        conn = self.backend.open("1-4.2")
        self.assertEqual(acc.claimed, [0])
        self.assertEqual(acc.detached, [])               # no kernel driver was active
        conn.close()
        self.assertEqual(acc.released, [0])
        self.assertGreaterEqual(acc.disposed, 1)

    def test_open_detaches_an_active_kernel_driver_and_gives_it_back(self):
        acc = FakeDevice(0x18D1, 0x2D00, 1, 14, (4, 2), accessory_interfaces())
        acc.kernel_driver = True
        self.usb.state.devices = [acc]
        conn = self.backend.open("1-4.2")
        self.assertEqual(acc.detached, [0])
        conn.close()
        self.assertEqual(acc.attached, [0])

    def test_open_survives_a_platform_without_kernel_driver_calls(self):
        acc = FakeDevice(0x18D1, 0x2D00, 1, 14, (4, 2), accessory_interfaces())

        def not_implemented(n):
            raise NotImplementedError()

        acc.is_kernel_driver_active = not_implemented
        self.usb.state.devices = [acc]
        self.backend.open("1-4.2").close()

    def test_open_refuses_a_device_that_is_not_in_accessory_mode(self):
        with self.assertRaises(usbaoa.UsbError) as cm:
            self.backend.open("1-4.2")
        self.assertEqual(cm.exception.code, usbaoa.AOA_ERR_NOT_FOUND)
        self.assertGreaterEqual(self.phone.disposed, 1)

    def test_open_without_a_bulk_pair(self):
        acc = FakeDevice(0x18D1, 0x2D00, 1, 14, (4, 2), [FakeInterface(0, 0xFF, 0, 0, [])])
        self.usb.state.devices = [acc]
        with self.assertRaises(usbaoa.UsbError) as cm:
            self.backend.open("1-4.2")
        self.assertEqual(cm.exception.code, usbaoa.AOA_ERR_ENDPOINTS)
        self.assertGreaterEqual(acc.disposed, 1)

    def test_open_permission_denied(self):
        acc = FakeDevice(0x18D1, 0x2D00, 1, 14, (4, 2), accessory_interfaces())

        def deny(dev, n):
            raise FakeUsbError("Access denied (insufficient permissions)", 13)

        self.util.claim_interface = deny
        self.usb.state.devices = [acc]
        with self.assertRaises(usbaoa.UsbError) as cm:
            self.backend.open("1-4.2")
        self.assertEqual(cm.exception.code, usbaoa.AOA_ERR_PERMISSION)
        self.assertEqual(cm.exception.detail, NO_PERMISSION)

    def test_read_and_write_semantics(self):
        acc = FakeDevice(0x18D1, 0x2D00, 1, 14, (4, 2), accessory_interfaces())
        self.usb.state.devices = [acc]
        conn = self.backend.open("1-4.2")
        ep_in = acc.interfaces[0].endpoints[0]
        ep_out = acc.interfaces[0].endpoints[1]
        ep_in.to_read = [b"abc\n"]
        self.assertEqual(conn.read(100, 200), b"abc\n")
        self.assertEqual(acc.reads[0], (16384, 200))     # never a buffer under 16384
        self.assertEqual(conn.read(16384, 200), b"")      # a timeout is not an error
        self.assertEqual(conn.write(b"xyz\n", 500), 4)
        self.assertEqual(ep_out.written, [(b"xyz\n", 500)])
        ep_out.write_error = FakeTimeout("Operation timed out", 110)
        self.assertEqual(conn.write(b"q", 500), 0)
        ep_out.write_error = FakeUsbError("No such device (it may have been disconnected)", 19)
        with self.assertRaises(usbaoa.UsbError) as cm:
            conn.write(b"q", 500)
        self.assertEqual(cm.exception.code, usbaoa.AOA_ERR_NO_DEVICE)
        ep_in.to_read = [FakeUsbError("Input/Output Error", 5)]
        with self.assertRaises(usbaoa.UsbError) as cm:
            conn.read(16384, 200)
        self.assertEqual(cm.exception.code, usbaoa.AOA_ERR_IO)
        ep_in.to_read = [FakeUsbError("Pipe error", 32)]
        self.assertEqual(conn.read(16384, 200), b"")      # a stall is cleared and carried on from
        self.assertEqual(len(acc.halts), 1)

    def test_no_libusb(self):
        self.usb.state.find_error = self.core.NoBackendError("No backend available")
        with self.assertRaises(usbaoa.UsbError) as cm:
            self.backend.list_devices()
        self.assertEqual(cm.exception.code, usbaoa.AOA_ERR_UNAVAILABLE)
        self.assertIn("libusb", cm.exception.detail)

    def test_without_pyusb_installed(self):
        with mock.patch.dict(sys.modules, {"usb": None, "usb.core": None, "usb.util": None}):
            for call in (lambda: usbaoa.PyUsbBackend().list_devices(),
                         lambda: usbaoa.PyUsbBackend().switch("1-4", "x"),
                         lambda: usbaoa.PyUsbBackend().open("1-4")):
                with self.assertRaises(usbaoa.UsbError) as cm:
                    call()
                self.assertEqual(cm.exception.code, usbaoa.AOA_ERR_UNAVAILABLE)
                self.assertEqual(cm.exception.detail, "USB mode needs the python3-usb package")

    def test_polling_releases_every_handle(self):
        for _ in range(20):
            self.backend.list_devices()
        for d in (self.phone, self.other, self.hub):
            self.assertEqual(d.disposed, 20)


# ---- the engine over a fake bus --------------------------------------------------------------

class UsbEngineCase(unittest.TestCase):
    engine_args = {}

    def setUp(self):
        saved = {k: os.environ.get(k) for k in ("LOGINRELAY_FAKE_PHONE", "LOGINRELAY_BROWSER")}
        self.addCleanup(self._restore_env, saved)
        os.environ.pop("LOGINRELAY_FAKE_PHONE", None)
        os.environ["LOGINRELAY_BROWSER"] = "none"
        self.world = FakeUsbWorld()
        self.host = FakeHost()
        self.addCleanup(self.host.stop)
        self.tmp = tempfile.mkdtemp(prefix="lr-usb-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prefs = Prefs(os.path.join(self.tmp, "prefs.json"))
        self.adb_stops = []     # calls to the (fake) "stop the adb server" helper; the real one never runs in tests
        args = dict(usb_backend=self.world, usb_host_name="Test Mac", usb_idle_pause=0.1, usb_recheck=0.2,
                    usb_switch_poll=0.05, usb_switch_polls=40, usb_open_wait=0.05, usb_error_pause=0.3,
                    delivery_timeout=0.8, usb_link_args=dict(ping_interval=0.2, silence=1.0),
                    stop_adb=self._stop_adb)
        args.update(self.engine_args)
        self.engine = eng.RelayEngine(self.host, self.prefs, **args)

        def changed():
            if not self.host.on_ui():
                self.host.wrong_thread.append("on_change off the UI thread")

        self.engine.on_change = changed

    @staticmethod
    def _restore_env(saved):
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _stop_adb(self):
        self.adb_stops.append(time.monotonic())
        return True

    def tearDown(self):
        self.assertEqual(self.host.wrong_thread, [])

    def start(self):
        self.host.ask(self.engine.start)
        self.addCleanup(lambda: self.host.ask(self.engine.shutdown))

    def ui(self, fn):
        return self.host.ask(fn)

    def logs(self):
        return [t for _, t in self.engine.log_lines]

    def wait_phase(self, kind, timeout=15):
        wait_until(lambda: self.engine.phase.kind == kind, timeout,
                   "phase %s (is %s)" % (kind, self.engine.phase.kind))

    def wait_log(self, text, timeout=10):
        wait_until(lambda: text in self.logs(), timeout, "log line %r" % text)

    def wait_active(self):
        wait_until(lambda: self.engine.active is not None, 10, "a login to start")
        return self.engine.active

    def ready_phone(self, location="1-4", **kw):
        phone = self.world.add_phone(location, **kw)
        self.start()
        self.wait_phase(eng.READY)
        return phone

    def begin_login(self, state="S1", **kw):
        phone = self.ready_phone(**kw)
        url, redirect = authorize_url(state)
        phone.send_open(url)
        flow = self.wait_active()
        return phone, flow, redirect


class CandidateTests(UsbEngineCase):
    def test_no_phone_says_so(self):
        self.start()
        self.wait_phase(eng.NO_PHONE)
        self.assertEqual(eng.describe(self.engine),
                         ("No phone found over USB",
                          "Plug the phone in and choose “File transfer” in its USB notification. "
                          "USB debugging isn't needed."))
        self.assertEqual(self.world.switch_calls, [])
        self.assertEqual(self.adb_stops, [])             # nothing was busy, so adb is never touched

    def test_unrelated_devices_are_never_touched(self):
        self.world.add_other("1-7")                       # Qualcomm download mode
        self.start()
        self.wait_phase(eng.NO_PHONE)
        time.sleep(0.6)
        self.assertEqual(self.world.switch_calls, [])
        self.assertEqual(self.world.open_calls, [])
        self.assertEqual(self.engine.usb_candidates, [])

    def test_a_single_candidate_is_used_without_asking(self):
        self.ready_phone("1-4")
        self.assertEqual(self.engine.device.serial, "usb:1-4")
        self.assertEqual(self.engine.device.title, "Pixel 7")
        self.assertIn("USB phone: Pixel 7", self.logs())

    def test_several_candidates_wait_for_the_user_to_choose(self):
        self.world.add_phone("1-4", name="Pixel 7")
        p2 = self.world.add_phone("1-5", name="Galaxy S22")
        self.start()
        wait_until(lambda: len(self.engine.usb_candidates) == 2, 5, "both candidates")
        time.sleep(0.4)
        self.assertEqual(self.engine.phase.kind, eng.NO_PHONE)
        self.assertEqual(self.world.switch_calls, [])
        self.assertEqual(eng.describe(self.engine),
                         ("More than one phone found over USB", "Choose which phone to use from the menu."))
        sub = [e for e in eng.menu_model(self.engine) if e.kind == "submenu" and e.label == "Phone"][0]
        self.assertEqual([(c.label, c.action, c.arg, c.checked) for c in sub.children],
                         [("Pixel 7 (1-4)", eng.A_SELECT_USB, "1-4", False),
                          ("Galaxy S22 (1-5)", eng.A_SELECT_USB, "1-5", False)])
        self.assertEqual([c.arg for c in eng.usb_phone_choices(self.engine)], ["1-4", "1-5"])

        self.ui(lambda: self.engine.select_usb("1-5"))
        self.assertEqual(Prefs(self.prefs.path).usb_selected, "1-5")
        self.wait_phase(eng.READY)
        self.assertEqual([c[0] for c in self.world.switch_calls], ["1-5"])
        self.assertEqual(self.engine.device.serial, "usb:1-5")
        self.assertTrue(p2.connections >= 1)
        # The menu keeps offering both phones, with the chosen one ticked.
        self.assertEqual([(c.arg, c.checked) for c in eng.usb_phone_choices(self.engine)],
                         [("1-4", False), ("1-5", True)])

    def test_a_phone_already_in_accessory_mode_is_used_directly(self):
        self.world.add_phone("1-4", accessory=True, vid=0x18D1, pid=0x2D01)
        self.start()
        self.wait_phase(eng.READY)
        self.assertEqual(self.world.switch_calls, [])

    def test_the_selected_phone_is_remembered_across_restarts(self):
        self.prefs.usb_selected = "1-5"
        self.world.add_phone("1-4", name="Pixel 7")
        self.world.add_phone("1-5", name="Galaxy S22")
        self.start()
        self.wait_phase(eng.READY)
        self.assertEqual([c[0] for c in self.world.switch_calls], ["1-5"])


class SwitchTests(UsbEngineCase):
    def test_switch_then_reenumeration_then_open(self):
        self.world.reenumerate_after = 0.4
        self.world.add_phone("1-4.2")
        self.start()
        wait_until(lambda: self.world.switch_calls, 5, "the switch")
        self.assertEqual(self.world.switch_calls, [("1-4.2", "Test Mac")])
        self.assertIn("Asking Pixel 7 to switch to USB accessory mode…", self.engine.usb_detail)
        self.assertEqual(self.engine.phase.kind, eng.CONNECTING)
        self.assertEqual(self.world.open_calls, [])      # not before it has reappeared as an accessory
        self.wait_phase(eng.READY)
        self.assertEqual(self.world.open_calls, ["1-4.2"])
        self.assertEqual(len(self.world.switch_calls), 1)

    def test_a_phone_that_never_switches_is_reported_and_retried_no_sooner_than_the_limit(self):
        self.world.ignore_switch = True
        self.engine._usb_switch_interval = 1.2
        self.engine._usb_switch_polls = 4
        self.world.add_phone("1-4")
        self.start()
        self.wait_until_detail("The phone didn't switch to accessory mode.")
        self.assertEqual(len(self.world.switch_calls), 1)
        time.sleep(0.7)
        self.assertEqual(len(self.world.switch_calls), 1)       # rate limited
        wait_until(lambda: len(self.world.switch_calls) >= 2, 5, "the second attempt")
        gap = self.world.switch_times[1] - self.world.switch_times[0]
        self.assertGreaterEqual(gap, 1.2)

    def wait_until_detail(self, text):
        wait_until(lambda: self.engine.usb_detail == text, 10, "detail %r (is %r)" % (text, self.engine.usb_detail))

    def test_the_default_rate_limit_is_twenty_seconds(self):
        self.assertEqual(eng.USB_SWITCH_INTERVAL, 20.0)
        e = eng.RelayEngine(FakeHost(), self.prefs, usb_backend=self.world)
        self.assertEqual(e._usb_switch_interval, 20.0)

    def test_the_rate_limit_forgets_a_phone_that_left_the_bus(self):
        self.world.ignore_switch = True
        self.engine._usb_switch_interval = 60
        self.engine._usb_switch_polls = 3
        self.world.add_phone("1-4")
        self.start()
        wait_until(lambda: len(self.world.switch_calls) == 1, 5, "first switch")
        self.wait_until_detail("The phone didn't switch to accessory mode.")
        self.world.unplug("1-4")
        self.wait_phase(eng.NO_PHONE)
        self.world.add_phone("1-4")
        wait_until(lambda: len(self.world.switch_calls) == 2, 5, "the immediate retry after replugging")

    def test_check_again_retries_the_switch_at_once(self):
        self.world.ignore_switch = True
        self.engine._usb_switch_interval = 60
        self.engine._usb_switch_polls = 3
        self.world.add_phone("1-4")
        self.start()
        self.wait_until_detail("The phone didn't switch to accessory mode.")
        self.assertEqual(len(self.world.switch_calls), 1)
        self.ui(self.engine.check_again)
        wait_until(lambda: len(self.world.switch_calls) == 2, 5, "the retry")

    def test_permission_denied_on_the_switch(self):
        self.world.switch_error = usbaoa.SwitchResult(usbaoa.AOA_ERR_PERMISSION, 0, "denied")
        self.world.add_phone("1-4")
        self.start()
        self.wait_until_detail(NO_PERMISSION)
        self.assertEqual(self.engine.phase.kind, eng.CONNECTING)
        self.assertEqual(eng.describe(self.engine), ("Connecting to Pixel 7…", NO_PERMISSION))

    def test_unsupported_phone(self):
        self.world.switch_error = usbaoa.SwitchResult(usbaoa.AOA_ERR_UNSUPPORTED, 0)
        self.world.add_phone("1-4")
        self.start()
        self.wait_until_detail("Pixel 7 doesn't answer the USB accessory protocol.")

    def test_busy_on_open_stops_the_adb_server_and_retries(self):
        self.world.open_error = usbaoa.UsbError(usbaoa.AOA_ERR_BUSY, "Resource busy")
        self.world.add_phone("1-4", accessory=True, pid=0x2D01)
        self.start()
        wait_until(lambda: self.adb_stops, 5, "the adb server to be stopped")
        self.wait_log("Pixel 7 is busy. Stopped the adb server that was probably holding it.")
        wait_until(lambda: len(self.world.open_calls) >= 3, 5, "the retries")
        self.world.open_error = None                      # adb let go
        self.wait_phase(eng.READY)
        self.assertEqual(len(self.adb_stops), 1)          # once, not once per attempt

    def test_busy_on_the_switch_stops_the_adb_server(self):
        self.world.switch_error = usbaoa.SwitchResult(usbaoa.AOA_ERR_BUSY, 0, "Resource busy")
        self.world.add_phone("1-4")
        self.start()
        self.wait_until_detail("Another program has Pixel 7 open.")
        self.assertEqual(len(self.adb_stops), 1)

    def test_the_adb_server_is_not_stopped_again_and_again(self):
        self.engine._adb_stop_interval = 60
        self.world.open_error = usbaoa.UsbError(usbaoa.AOA_ERR_BUSY, "Resource busy")
        self.world.add_phone("1-4", accessory=True, pid=0x2D01)
        self.start()
        wait_until(lambda: len(self.world.open_calls) >= 14, 10, "two rounds of retries")
        self.assertEqual(len(self.adb_stops), 1)

    def test_no_adb_installed_is_silent(self):
        self.engine._stop_adb = lambda: False
        self.world.open_error = usbaoa.UsbError(usbaoa.AOA_ERR_BUSY, "Resource busy")
        self.world.add_phone("1-4", accessory=True, pid=0x2D01)
        self.start()
        wait_until(lambda: len(self.world.open_calls) >= 3, 5, "the retries")
        self.assertFalse([t for t in self.logs() if "adb" in t])
        self.wait_until_detail("Another program has Pixel 7 open.")

    def test_permission_denied_on_open_is_not_retried_forever(self):
        self.world.open_error = usbaoa.UsbError(usbaoa.AOA_ERR_PERMISSION, NO_PERMISSION)
        self.world.add_phone("1-4", accessory=True, pid=0x2D01)
        self.start()
        self.wait_until_detail(NO_PERMISSION)
        self.assertEqual(self.world.open_calls[:1], ["1-4"])

    def test_other_open_failures_are_retried_then_reported(self):
        self.world.open_error = usbaoa.UsbError(usbaoa.AOA_ERR_OPEN, "boom")
        self.world.add_phone("1-4", accessory=True, pid=0x2D01)
        self.start()
        self.wait_until_detail("Couldn't open the accessory connection (error -2).")
        self.assertGreaterEqual(len(self.world.open_calls), 6)

    def test_a_bus_that_cant_be_read_is_reported_not_fatal(self):
        self.world.list_error = usbaoa.UsbError(usbaoa.AOA_ERR_IO, "boom")
        self.start()
        wait_until(lambda: self.engine.usb_detail == "Couldn't read the USB devices (boom).", 5, "detail")
        self.assertEqual(self.engine.phase.kind, eng.NO_PHONE)
        self.world.list_error = None
        self.world.add_phone("1-4")
        self.wait_phase(eng.READY)


class SessionTests(UsbEngineCase):
    def test_connect_hello_alive_ready(self):
        phone = self.world.add_phone("1-4")
        phone.hello_on_connect = False       # first look at the "connecting" state
        phone.app_running = False
        self.start()
        wait_until(lambda: self.world.connections, 10, "the pipe to open")
        wait_until(lambda: self.engine.linked, 5, "the link")
        self.assertEqual(self.engine.phase.kind, eng.CONNECTING)
        self.assertEqual(self.engine.phase.detail,
                         "Login Relay on the phone isn't answering yet. If Android asks to allow USB access, tap OK; "
                         "otherwise just start a sign-in on the phone and Login Relay starts by itself.")
        self.assertEqual(eng.describe(self.engine),
                         ("Pixel 7 is connected over USB", self.engine.phase.detail))
        self.assertEqual(eng.window_actions(self.engine), (("Check Again", eng.A_CHECK), None))
        self.assertIn("USB connection open.", self.logs())
        self.assertFalse(self.engine.usb_alive)
        # The app comes up on the phone: hello, then it answers pings.
        phone.app_running = True
        phone.send({"v": 1, "type": "hello", "transport": "usb", "version": "0.5.0"})
        self.wait_phase(eng.READY)
        self.assertEqual(self.engine.phone_version, "0.5.0")
        self.assertIn("Login Relay 0.5.0 on the phone says hello (usb).", self.logs())
        self.assertIn("Login Relay on the phone is answering.", self.logs())
        self.assertEqual(eng.describe(self.engine),
                         ("Pixel 7 is ready", "Sign in to an app on the phone and the page opens here."))
        self.assertGreaterEqual(len(phone.messages("ping")), 1)

    def test_open_flow_callback_sent_delivered_logged(self):
        phone, flow, redirect = self.begin_login("STATE-U1")
        self.assertEqual(flow.request.app_name, "Example App")
        self.assertEqual(self.engine.phase.kind, eng.WORKING)
        self.assertEqual([c.scheme for c in self.host.claims], [SCHEME])
        wait_until(lambda: phone.messages("status"), 5, "the status line")
        self.assertEqual(phone.messages("status")[0]["text"], "Finish signing in on your computer.")
        self.assertIn("Example App wants to open a sign-in page at login.example.com.", self.logs())

        good = "%s?code=GOOD&state=STATE-U1" % redirect
        self.ui(lambda: self.engine.receive(good))
        self.assertIsNone(self.engine.active)
        self.assertTrue(self.host.claims[0].released)
        self.assertIn("Returning to Example App on the phone…", self.logs())
        wait_until(lambda: phone.messages("callback"), 5, "the callback message")
        self.assertEqual(phone.messages("callback"), [{"type": "callback", "url": good}])
        self.wait_log("Done: Example App is finishing the sign-in.")
        self.assertIsNone(self.engine._awaiting_delivery)
        time.sleep(1.0)
        self.assertNotIn("The phone didn't confirm it handed the result to the app.", self.logs())
        self.assertEqual(self.adb_stops, [])
        self.assertEqual(self.engine.phase.kind, eng.READY)

    def test_a_decoy_return_address_is_ignored(self):
        phone, flow, redirect = self.begin_login("STATE-U2")
        self.ui(lambda: self.engine.receive("%s?code=EVIL&state=other" % redirect))
        time.sleep(0.3)
        self.assertIn("Ignored a return address from a different sign-in (state mismatch).", self.logs())
        self.assertEqual(phone.messages("callback"), [])
        self.assertIs(self.engine.active, flow)

    def test_the_phone_reports_a_wrong_state_error(self):
        phone, flow, redirect = self.begin_login("STATE-U3")
        phone.delivery_reply = "error:That isn't the sign-in in progress."
        self.ui(lambda: self.engine.receive("%s?code=X&state=STATE-U3" % redirect))
        self.wait_log("The phone says: That isn't the sign-in in progress.")
        self.assertNotIn("Done: Example App is finishing the sign-in.", self.logs())
        self.assertIsNone(self.engine._awaiting_delivery)       # the timer is gone with it
        time.sleep(1.2)
        self.assertNotIn("The phone didn't confirm it handed the result to the app.", self.logs())

    def test_an_unrelated_error_from_the_phone_is_just_logged(self):
        phone = self.ready_phone()
        phone.send({"type": "error", "text": "Something odd"})
        self.wait_log("The phone says: Something odd")
        phone.send({"type": "error"})
        self.wait_log("The phone says: unknown error")
        self.assertEqual(self.engine.phase.kind, eng.READY)

    def test_delivered_timeout(self):
        phone, flow, redirect = self.begin_login("STATE-U4")
        phone.delivery_reply = None                      # the phone never confirms
        self.ui(lambda: self.engine.receive("%s?code=X&state=STATE-U4" % redirect))
        self.wait_log("The phone didn't confirm it handed the result to the app.", 5)
        self.assertNotIn("Done: Example App is finishing the sign-in.", self.logs())
        self.assertIsNone(self.engine._awaiting_delivery)
        phone.send({"type": "delivered"})                # a late one changes nothing
        time.sleep(0.3)
        self.assertNotIn("Done: Example App is finishing the sign-in.", self.logs())

    def test_the_page_error_is_still_reported(self):
        phone, flow, redirect = self.begin_login("STATE-U5")
        self.ui(lambda: self.engine.receive("%s?error=access_denied&error_description=No+way&state=STATE-U5"
                                            % redirect))
        self.assertIn("The page returned an error: No way", self.logs())
        wait_until(lambda: phone.messages("callback"), 5, "the callback")

    def test_cancel_from_the_phone_and_from_the_computer(self):
        phone, flow, redirect = self.begin_login("STATE-U6")
        phone.send({"type": "cancel"})
        wait_until(lambda: self.engine.active is None, 5, "the phone's cancel")
        self.assertIn("Cancelled on the phone.", self.logs())
        self.assertTrue(self.host.claims[0].released)
        url, _ = authorize_url("STATE-U7")
        phone.send_open(url)
        self.wait_active()
        self.ui(lambda: self.engine.cancel_active("Cancelled on the computer.", notify_phone=True))
        wait_until(lambda: phone.messages("done"), 5, "done")

    def test_a_non_web_link_is_not_opened(self):
        phone = self.ready_phone()
        phone.send_open("file:///etc/passwd")
        wait_until(lambda: phone.messages("status"), 5, "the status")
        self.assertEqual(self.host.opened, [])

    def test_a_plain_link_opens_in_the_browser(self):
        phone = self.ready_phone()
        phone.send_open("https://example.com/terms")
        wait_until(lambda: self.host.opened, 5, "the link")
        wait_until(lambda: phone.messages("done"), 5, "done")

    def test_liveness_lost_mid_login_cancels_the_login(self):
        phone, flow, redirect = self.begin_login("STATE-U8")
        phone.answer_ping = False                        # the app stops answering; the cable stays in
        wait_until(lambda: self.engine.active is None, 8, "the login to be cancelled")
        self.assertIn("Login Relay on the phone stopped answering.", self.logs())
        self.assertTrue(self.host.claims[0].released)
        self.assertFalse(self.engine.usb_alive)
        self.assertIsNone(self.engine.phone_version)
        self.assertTrue(self.engine.linked)                  # the connection itself stays up
        self.assertEqual(self.engine.phase.kind, eng.READY)   # it has answered on this connection before
        self.assertEqual(phone.messages("callback"), [])
        # ... and it recovers when the app answers again.
        phone.answer_ping = True
        wait_until(lambda: self.engine.usb_alive, 5, "alive again")
        self.assertEqual(self.logs().count("Login Relay on the phone is answering."), 2)

    def test_idle_phone_app_stays_connected_and_comes_back_by_itself(self):
        phone = self.ready_phone()
        idle = "Login Relay on the phone went idle (it starts again by itself when an app asks to sign in)."
        # Android reclaims the idle app: no more pongs, but the accessory stays attached.
        phone.app_running = False
        self.wait_log(idle, 8)
        self.assertNotIn("Login Relay on the phone stopped answering.", self.logs())   # no login to cancel
        self.assertFalse(self.engine.usb_alive)
        self.assertTrue(self.engine.usb_ever_alive)
        self.assertIsNone(self.engine.phone_version)
        self.assertTrue(self.engine.linked)
        self.assertEqual(self.engine.phase.kind, eng.READY)
        self.assertEqual(eng.describe(self.engine)[0], "Pixel 7 is ready")
        time.sleep(1.5)
        self.assertNotIn("Link to the phone closed.", self.logs())
        self.assertFalse(self.world.connections[0].closed)
        self.assertEqual(len(self.world.connections), 1)
        # A sign-in starts the app again; it says hello over the same pipe and answers pings.
        phone.app_running = True
        phone.send({"v": 1, "type": "hello", "transport": "usb", "version": "0.5.0"})
        wait_until(lambda: self.engine.usb_alive, 5, "alive again")
        self.assertEqual(self.engine.phone_version, "0.5.0")
        url, redirect = authorize_url("STATE-IDLE")
        phone.send_open(url)
        self.wait_active()
        good = "%s?code=OK&state=STATE-IDLE" % redirect
        self.ui(lambda: self.engine.receive(good))
        self.wait_log("Done: Example App is finishing the sign-in.")
        self.assertEqual(len(self.world.connections), 1)

    def test_a_login_in_progress_is_cancelled_when_the_phone_goes_quiet_but_the_link_stays(self):
        phone, flow, redirect = self.begin_login("STATE-IDLE2")
        phone.app_running = False
        wait_until(lambda: self.engine.active is None, 8, "the login to be cancelled")
        self.assertIn("Login Relay on the phone went idle (it starts again by itself when an app asks to sign in).",
                      self.logs())
        self.assertIn("Login Relay on the phone stopped answering.", self.logs())
        self.assertTrue(self.engine.linked)

    def test_unplugging_mid_login(self):
        phone, flow, redirect = self.begin_login("STATE-U9")
        self.world.unplug("1-4")
        wait_until(lambda: self.engine.active is None, 8, "the login to end")
        self.assertTrue(self.host.claims[0].released)
        self.wait_phase(eng.NO_PHONE)
        self.assertFalse(self.engine.usb_alive)
        self.assertFalse(self.engine.usb_ever_alive)         # a new connection starts from scratch
        self.assertFalse(self.engine.linked)
        self.assertIn("Link to the phone closed.", self.logs())
        self.assertTrue(self.world.connections[0].closed)

    def test_replugging_starts_over(self):
        self.ready_phone()
        self.world.unplug("1-4")
        self.wait_phase(eng.NO_PHONE)
        self.world.add_phone("1-4")
        self.wait_phase(eng.READY)
        self.assertEqual(len(self.world.switch_calls), 2)
        self.assertEqual(len(self.world.connections), 2)

    def test_shutdown_tells_the_phone_and_releases_the_pipe(self):
        phone, flow, redirect = self.begin_login("STATE-U10")
        self.ui(self.engine.shutdown)
        self.assertIn("Quit", self.logs())
        self.assertTrue(self.host.claims[0].released)
        wait_until(lambda: phone.messages("done"), 5, "done")
        wait_until(lambda: self.world.connections[0].closed, 5, "the pipe to close")


class MenuTests(UsbEngineCase):
    def test_prefs_round_trip(self):
        p = Prefs(os.path.join(self.tmp, "p.json"))
        self.assertIsNone(p.usb_selected)
        p.usb_selected = "1-4.2"
        self.assertEqual(Prefs(p.path).usb_selected, "1-4.2")

    def test_prefs_from_older_versions_are_ignored_and_kept(self):
        path = os.path.join(self.tmp, "old.json")
        with open(path, "w") as f:
            f.write('{"transport": "adb", "adbPath": "/opt/adb", "selectedSerial": "ABC", "notifications": true}')
        p = Prefs(path)
        self.assertTrue(p.notifications)
        for name in ("transport", "adb_path", "selected_serial"):
            self.assertFalse(hasattr(p, name), name)
        e = eng.RelayEngine(FakeHost(), p, usb_backend=self.world, stop_adb=self._stop_adb)
        self.assertFalse(hasattr(e, "transport"))
        self.assertFalse(hasattr(e, "adb"))

    def test_the_menu_and_window_data_with_no_phone(self):
        self.start()
        self.wait_phase(eng.NO_PHONE)
        menu = eng.menu_model(self.engine)
        self.assertEqual([e.label for e in menu],
                         ["No phone found over USB",
                          "Plug the phone in and choose \u201cFile transfer\u201d in its USB notification. "
                          "USB debugging isn't needed.",
                          "", "Check Again", "", "Show Activity…", "", "Launch at Login",
                          "Notify When a Sign-in Arrives", "", "About Login Relay", "Quit Login Relay"])
        self.assertEqual(menu[1].kind, "hint")
        self.assertFalse(menu[0].good)
        self.assertEqual(eng.window_actions(self.engine), (("Check Again", eng.A_CHECK), None))

    def test_the_menu_and_window_data_when_ready(self):
        self.ready_phone()
        menu = eng.menu_model(self.engine, launch_at_login=True)
        self.assertEqual([e.label for e in menu],
                         ["Pixel 7 is ready", "", "Show Activity…", "", "Launch at Login",
                          "Notify When a Sign-in Arrives", "", "About Login Relay", "Quit Login Relay"])
        self.assertTrue(menu[0].good)
        self.assertTrue([e for e in menu if e.label == "Launch at Login"][0].checked)
        self.assertEqual(eng.window_actions(self.engine), (None, None))
        self.assertEqual(eng.describe(self.engine),
                         ("Pixel 7 is ready", "Sign in to an app on the phone and the page opens here."))

    def test_nothing_in_the_ui_mentions_adb_or_installing(self):
        self.ready_phone()
        for e in eng.menu_model(self.engine):
            self.assertNotIn("adb", e.label.lower())
            self.assertNotIn("install", e.label.lower())
            self.assertNotIn("connect over", e.label.lower())
        for name in ("A_DOWNLOAD", "A_CHOOSE_ADB", "A_INSTALL", "A_OPEN", "A_SET_TRANSPORT", "A_SELECT_PHONE",
                     "NO_ADB", "NEEDS_APP", "UNAUTHORIZED", "ADB", "transport_choices", "find_bundled"):
            self.assertFalse(hasattr(eng, name), name)

    def test_working_menu(self):
        phone, flow, redirect = self.begin_login("STATE-T2")
        self.assertEqual(eng.window_actions(self.engine)[0], ("Cancel", eng.A_CANCEL))
        self.assertEqual([e.label for e in eng.menu_model(self.engine) if e.kind == "action"][0], "Cancel sign-in")
        self.assertEqual(eng.describe(self.engine),
                         ("Example App sign-in in progress",
                          "Finish in your browser. If it asks to open Login Relay, allow it."))


class WatchdogTests(UsbEngineCase):
    engine_args = dict(watchdog_interval=0.3, watchdog_after=1.0)

    def test_stuck_loop_is_replaced(self):
        # The first look at the bus hangs; the watchdog should start a fresh loop that works.
        real = self.world.list_devices
        hung = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def list_devices():
            if not hung.is_set():
                hung.set()
                release.wait(8)
            return real()

        self.world.list_devices = list_devices
        self.world.add_phone("1-4")
        self.start()
        wait_until(lambda: "Checking for the phone got stuck; starting over." in self.logs(), 10, "the watchdog")
        self.wait_phase(eng.READY, 10)
        self.assertEqual(self.engine._generation, 2)


class NoPyUsbTests(UsbEngineCase):
    def test_usb_mode_without_pyusb_reports_it_and_keeps_running(self):
        patcher = mock.patch.dict(sys.modules, {"usb": None, "usb.core": None, "usb.util": None})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine._usb_backend = usbaoa.default_backend()
        self.start()
        self.wait_phase(eng.NO_PHONE)
        wait_until(lambda: self.engine.usb_detail, 5, "the detail text")
        self.assertEqual(self.engine.usb_detail, "USB mode needs the python3-usb package")
        self.assertEqual(eng.describe(self.engine),
                         ("No phone found over USB", "USB mode needs the python3-usb package"))
        menu = eng.menu_model(self.engine)
        self.assertEqual(menu[1].label, "USB mode needs the python3-usb package")
        self.assertIn("Check Again", [e.label for e in menu])
        time.sleep(0.5)
        self.assertEqual(self.host.wrong_thread, [])


class DescriptorLeakTests(UsbEngineCase):
    engine_args = dict(usb_idle_pause=0.05, usb_recheck=0.1, usb_link_args=dict(ping_interval=0.1, silence=1.0))

    def test_polling_a_connected_phone_keeps_the_file_descriptor_count_flat(self):
        self.ready_phone()
        wait_until(lambda: self.engine.linked, 10, "the link")
        time.sleep(1.5)                                   # warm-up
        before = stable_fds()
        lists, pings = self.world.list_calls, len(self.world.phones["1-4"].messages("ping"))
        time.sleep(5)
        self.assertGreater(self.world.list_calls - lists, 10, "the loop should have been polling the bus")
        self.assertGreater(len(self.world.phones["1-4"].messages("ping")) - pings, 20, "and pinging")
        self.assertTrue(self.engine.linked)
        assert_fds_flat(self, before)

    def test_polling_an_empty_bus_keeps_it_flat_too(self):
        self.start()
        self.wait_phase(eng.NO_PHONE)
        time.sleep(1.0)
        before = stable_fds()
        lists = self.world.list_calls
        time.sleep(4)
        self.assertGreater(self.world.list_calls - lists, 20)
        assert_fds_flat(self, before)

    def test_switching_and_reconnecting_repeatedly_keeps_it_flat(self):
        self.world.add_phone("1-4")
        self.start()
        self.wait_phase(eng.READY)
        time.sleep(1.0)
        before = stable_fds()
        for i in range(4):
            self.world.unplug("1-4")
            self.wait_phase(eng.NO_PHONE)
            self.world.add_phone("1-4")
            self.wait_phase(eng.READY)
        assert_fds_flat(self, before)
        self.assertEqual(len(self.world.connections), 5)
        self.assertTrue(all(c.closed for c in self.world.connections[:-1]))


class ProbeTests(unittest.TestCase):
    """tools/aoa_probe.py against the fake bus."""

    def setUp(self):
        tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
        sys.path.insert(0, tools)
        self.addCleanup(sys.path.remove, tools)
        import aoa_probe
        self.probe = aoa_probe
        self.world = FakeUsbWorld(reenumerate_after=0.1)

    def test_list_shows_phones_and_other_devices(self):
        import io
        self.world.add_phone("1-4.2", name="Pixel 7")
        self.world.add_other("1-7")
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(self.probe.cmd_list(self.world), 0)
        text = out.getvalue()
        self.assertIn("1-4.2", text)
        self.assertIn("18d1:4ee1   likely", text)
        self.assertIn("Pixel 7 / SER123", text)
        self.assertRegex(text, r"1-7\s+05c6:9008\s+-\s+-")

    def test_run_switches_opens_pings_and_sends_what_is_typed(self):
        import io
        phone = self.world.add_phone("1-4.2")
        threading.Timer(3.0, phone.unplug).start()
        out = io.StringIO()
        typed = io.StringIO('callback https://example.com/cb?x=1\ndone\n{"type":"status","text":"raw"}\n')
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stdin", typed):
            rc = self.probe.cmd_run(self.world, None)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.world.switch_calls), 1)
        text = out.getvalue()
        self.assertIn("accessory device appeared", text)
        self.assertIn('phone: {"v": 1, "type": "hello"', text)
        self.assertEqual(phone.messages("callback"), [{"type": "callback", "url": "https://example.com/cb?x=1"}])
        self.assertEqual(len(phone.messages("done")), 1)
        self.assertEqual(phone.messages("status")[0]["text"], "raw")
        self.assertGreaterEqual(len(phone.messages("ping")), 1)
        self.assertTrue(self.world.connections[0].closed)

    def test_run_needs_exactly_one_candidate(self):
        import io
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(self.probe.cmd_run(self.world, None), 1)
            self.world.add_phone("1-4")
            self.world.add_phone("1-5")
            self.assertEqual(self.probe.cmd_run(self.world, None), 1)
        self.assertIn("No candidate phone", out.getvalue())
        self.assertIn("2 candidates", out.getvalue())


if __name__ == "__main__":
    unittest.main()
