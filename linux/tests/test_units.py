# SPDX-License-Identifier: GPL-3.0-or-later
"""The small non-UI pieces, one at a time: the stop-the-adb-server helper, the TCP stand-in for a USB pipe, prefs."""
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import stable_fds, assert_fds_flat, wait_until  # noqa: E402

from loginrelay import adbserver, fakephone, usbaoa  # noqa: E402
from loginrelay.prefs import Prefs  # noqa: E402


def make_adb(directory, body, name="adb"):
    p = os.path.join(directory, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(p, 0o755)
    return p


class AdbServerHelperTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lr-adb-")
        self.log = os.path.join(self.dir, "calls")

    def env(self, **kw):
        env = {"HOME": self.dir, "PATH": self.dir}
        env.update(kw)
        return env

    def test_runs_kill_server(self):
        make_adb(self.dir, 'echo "$@" >> "%s"' % self.log)
        self.assertTrue(adbserver.stop_adb_server(env=self.env()))
        with open(self.log) as f:
            self.assertEqual(f.read().strip(), "kill-server")

    def test_no_adb_is_silent(self):
        empty = tempfile.mkdtemp(prefix="lr-empty-")
        # Some machines (CI runners) really have an SDK in a system folder, so take those out of the search.
        with mock.patch.object(adbserver, "SYSTEM_SDK_ROOTS", ()):
            self.assertIsNone(adbserver.find_adb({"HOME": empty, "PATH": empty}))
            self.assertFalse(adbserver.stop_adb_server(env={"HOME": empty, "PATH": empty}))

    def test_does_not_wait_for_a_daemon_that_inherited_the_output(self):
        # `adb kill-server` / start-server can leave a daemon holding stdout. With pipes we would wait for
        # end-of-file until it exits.
        make_adb(self.dir, '(sleep 20) &\nexit 0')
        t0 = time.time()
        self.assertTrue(adbserver.stop_adb_server(env=self.env()))
        self.assertLess(time.time() - t0, 5)

    def test_gives_up_on_an_adb_that_hangs(self):
        make_adb(self.dir, "sleep 30")
        t0 = time.time()
        self.assertTrue(adbserver.stop_adb_server(timeout=0.5, env=self.env()))
        self.assertLess(time.time() - t0, 5)

    def test_a_program_that_cannot_run_is_silent(self):
        path = os.path.join(self.dir, "adb")
        with open(path, "w") as f:
            f.write("not a program")
        os.chmod(path, 0o755)
        self.assertFalse(adbserver.stop_adb_server(env=self.env()))

    def test_search_order(self):
        home = tempfile.mkdtemp(prefix="lr-home-")
        sdk = os.path.join(home, "sdk", "platform-tools")
        os.makedirs(sdk)
        in_sdk = make_adb(sdk, "exit 0")
        self.assertEqual(adbserver.find_adb({"HOME": home, "PATH": "/nonexistent", "ANDROID_HOME": os.path.join(home, "sdk")}),
                         in_sdk)
        self.assertEqual(adbserver.find_adb({"HOME": home, "PATH": "/nonexistent",
                                             "ANDROID_SDK_ROOT": os.path.join(home, "sdk")}), in_sdk)
        default_sdk = os.path.join(home, "Android", "Sdk", "platform-tools")
        os.makedirs(default_sdk)
        in_default = make_adb(default_sdk, "exit 0")
        self.assertEqual(adbserver.find_adb({"HOME": home, "PATH": "/nonexistent"}), in_default)
        on_path = make_adb(self.dir, "exit 0")
        self.assertEqual(adbserver.find_adb({"HOME": home, "PATH": self.dir}), on_path)   # PATH first

    def test_no_file_descriptor_leak(self):
        make_adb(self.dir, 'exit 0')
        env = self.env()
        adbserver.stop_adb_server(env=env)
        before = stable_fds()
        for _ in range(10):
            adbserver.stop_adb_server(env=env)
        assert_fds_flat(self, before)


class TcpConnectionTests(unittest.TestCase):
    def pair(self):
        a, b = socket.socketpair()
        self.addCleanup(b.close)
        conn = fakephone.TcpConnection(a)
        self.addCleanup(conn.close)
        return conn, b

    def test_reads_and_writes(self):
        conn, other = self.pair()
        self.assertFalse(conn.needs_padding)
        self.assertEqual(conn.read(usbaoa.READ_BUFFER, 50), b"")          # a timeout is not an error
        other.sendall(b'{"type":"pong"}\n')
        self.assertEqual(conn.read(usbaoa.READ_BUFFER, 500), b'{"type":"pong"}\n')
        self.assertEqual(conn.write(b"hello\n", 500), 6)
        self.assertEqual(other.recv(100), b"hello\n")

    def test_the_other_side_hanging_up_is_an_unplug(self):
        conn, other = self.pair()
        other.close()
        with self.assertRaises(usbaoa.UsbError) as ctx:
            conn.read(usbaoa.READ_BUFFER, 500)
        self.assertEqual(ctx.exception.code, usbaoa.AOA_ERR_NO_DEVICE)
        with self.assertRaises(usbaoa.UsbError):
            for _ in range(5):
                conn.write(b"x" * 65536, 200)

    def test_close_is_idempotent_and_ends_reads(self):
        conn, other = self.pair()
        conn.close()
        conn.close()
        with self.assertRaises(usbaoa.UsbError):
            conn.read(usbaoa.READ_BUFFER, 50)

    def test_backend_lists_one_accessory_phone(self):
        backend = fakephone.TcpPhoneBackend("127.0.0.1:1")
        devices = backend.list_devices()
        self.assertEqual(len(devices), 1)
        d = devices[0]
        self.assertEqual((d.name, d.is_accessory, d.location), ("Fake Phone", True, "fake"))
        self.assertEqual(backend.switch("fake", "x").code, usbaoa.AOA_OK)

    def test_backend_open_connects_or_reports(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        conn = fakephone.TcpPhoneBackend("127.0.0.1:%d" % srv.getsockname()[1]).open("fake")
        self.addCleanup(conn.close)
        peer, _ = srv.accept()
        self.addCleanup(peer.close)
        peer.sendall(b"hi\n")
        self.assertEqual(conn.read(usbaoa.READ_BUFFER, 1000), b"hi\n")
        port = srv.getsockname()[1]
        srv.close()
        with self.assertRaises(usbaoa.UsbError) as ctx:
            fakephone.TcpPhoneBackend("127.0.0.1:%d" % port).open("fake")
        self.assertEqual(ctx.exception.code, usbaoa.AOA_ERR_OPEN)

    def test_the_env_var_name(self):
        self.assertEqual(fakephone.ENV_VAR, "LOGINRELAY_FAKE_PHONE")
        self.assertEqual(fakephone.configured({"LOGINRELAY_FAKE_PHONE": "h:1"}), "h:1")
        self.assertIsNone(fakephone.configured({}))
        self.assertIsNone(fakephone.configured({"LOGINRELAY_FAKE_PHONE": ""}))


class PrefsTests(unittest.TestCase):
    def test_round_trip_and_defaults(self):
        d = tempfile.mkdtemp(prefix="lr-prefs-")
        path = os.path.join(d, "sub", "prefs.json")
        p = Prefs(path)
        self.assertEqual((p.usb_selected, p.notifications, p.launch_at_login, p.has_launched_before),
                         (None, False, False, False))
        p.usb_selected = "1-4.2"
        p.notifications = True
        p.has_launched_before = True
        q = Prefs(path)
        self.assertEqual((q.usb_selected, q.notifications, q.has_launched_before), ("1-4.2", True, True))
        q.usb_selected = None
        self.assertIsNone(Prefs(path).usb_selected)
        with open(path) as f:
            self.assertIsInstance(json.load(f), dict)

    def test_corrupt_file_is_ignored(self):
        d = tempfile.mkdtemp(prefix="lr-prefs-")
        path = os.path.join(d, "prefs.json")
        with open(path, "w") as f:
            f.write("{nope")
        self.assertFalse(Prefs(path).notifications)

    def test_default_path_follows_xdg(self):
        from loginrelay.prefs import default_prefs_path
        self.assertEqual(default_prefs_path({"XDG_CONFIG_HOME": "/c", "HOME": "/h"}), "/c/loginrelay/prefs.json")
        self.assertEqual(default_prefs_path({"HOME": "/h"}), "/h/.config/loginrelay/prefs.json")


if __name__ == "__main__":
    unittest.main()
