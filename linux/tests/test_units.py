# SPDX-License-Identifier: GPL-3.0-or-later
"""adb runner, phone link, installer, prefs: the non-UI pieces, one at a time."""
import hashlib
import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeWorld, open_fds, wait_until  # noqa: E402

from loginrelay import adb as adbmod  # noqa: E402
from loginrelay import installer  # noqa: E402
from loginrelay.phonelink import PhoneLink  # noqa: E402
from loginrelay.prefs import Prefs  # noqa: E402


def script(body):
    d = tempfile.mkdtemp(prefix="lr-script-")
    p = os.path.join(d, "tool")
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(p, 0o755)
    return p


class ExecProcessTests(unittest.TestCase):
    def test_captures_output_and_status(self):
        r = adbmod.exec_process(script('echo out; echo err >&2; exit 3'), [])
        self.assertEqual((r.status, r.out, r.err, r.ok), (3, "out", "err", False))

    def test_does_not_wait_for_a_daemon_that_inherited_the_output(self):
        # `adb start-server` forks a daemon that keeps stdout open. Reading until EOF would hang.
        tool = script('(sleep 8 >/dev/null 2>&1 &) ; echo started')
        t0 = time.time()
        r = adbmod.exec_process(tool, [], timeout=20)
        self.assertLess(time.time() - t0, 4)
        self.assertEqual((r.status, r.out), (0, "started"))

    def test_kills_after_the_timeout(self):
        t0 = time.time()
        r = adbmod.exec_process(script("echo partial; sleep 30"), [], timeout=1)
        self.assertLess(time.time() - t0, 6)
        self.assertFalse(r.ok)
        self.assertEqual(r.out, "partial")
        self.assertIn("timed out", r.err)

    def test_missing_program(self):
        r = adbmod.exec_process("/nonexistent/adb", [])
        self.assertEqual(r.status, -1)

    def test_no_file_descriptor_leak(self):
        tool = script("echo hi")
        adbmod.exec_process(tool, [])
        before = open_fds()
        for _ in range(60):
            adbmod.exec_process(tool, [])
        slow = script("sleep 30")
        for _ in range(3):
            adbmod.exec_process(slow, [], timeout=0.2)
        adbmod.exec_process("/nonexistent/adb", [])
        self.assertLessEqual(open_fds(), before)


class AdbTests(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        self.addCleanup(self.world.close)
        self.adb = adbmod.Adb(self.world.adb)

    def test_devices(self):
        self.world.write("devices.txt", "* daemon started successfully\n"
                                       "ABC123\tdevice usb:1-1 product:p model:Pixel_7 device:d transport_id:2\n"
                                       "DEF456\tunauthorized usb:1-2 transport_id:3")
        devs = self.adb.devices()
        self.assertEqual(devs, [adbmod.Device("ABC123", "device", "Pixel_7"), adbmod.Device("DEF456", "unauthorized", "")])
        self.assertEqual(devs[0].title, "Pixel 7")
        self.assertEqual(devs[1].title, "DEF456")
        self.assertTrue(devs[0].is_usable and not devs[1].is_usable)

    def test_installed_version_and_forward(self):
        self.assertEqual(self.adb.installed_version("FAKE0001", "io.github.clippycoder.oauthrelay"), "0.3.0")
        self.world.write("no_app")
        self.assertIsNone(self.adb.installed_version("FAKE0001", "io.github.clippycoder.oauthrelay"))
        self.assertIsNone(self.adb.existing_forward("FAKE0001"))
        self.assertIsNone(self.adb.forward("FAKE0001"))   # nothing listening
        self.world.write("phone_port", "4321")
        self.assertEqual(self.adb.existing_forward("FAKE0001"), 4321)
        self.assertIsNone(self.adb.existing_forward("OTHER"))
        self.assertEqual(self.adb.forward("FAKE0001"), 4321)
        self.assertEqual(self.adb.ensure_forward("FAKE0001"), 4321)

    def test_deliver_quotes_the_address_for_the_phones_shell(self):
        url = "com.example.relaytest://cb?code=a'b&state=x y"
        r = self.adb.deliver("FAKE0001", url, "com.example.app")
        self.assertTrue(r.ok)
        call = self.world.lines("am_calls.jsonl")[0]
        self.assertEqual(call["-d"], url)
        self.assertEqual(call["-p"], "com.example.app")
        self.assertEqual(call["-a"], "android.intent.action.VIEW")

    def test_deliver_without_a_package(self):
        self.adb.deliver("FAKE0001", "a://b", None)
        self.assertNotIn("-p", self.world.lines("am_calls.jsonl")[0])

    def test_install(self):
        r = self.adb.install("FAKE0001", "/some/login-relay.apk")
        self.assertIn("Success", r.out)
        self.assertEqual(self.world.lines("installs.jsonl")[0]["apk"], "/some/login-relay.apk")

    def test_locate_order(self):
        tmp = tempfile.mkdtemp(prefix="lr-loc-")
        exe = os.path.join(tmp, "adb")
        open(exe, "w").close()
        os.chmod(exe, 0o755)
        # The test hook wins, even over a user choice, and is returned as is.
        self.assertEqual(adbmod.Adb.locate("/x/y", {"LOGINRELAY_ADB": "/not/there"}), "/not/there")
        env = {"HOME": tmp, "PATH": ""}
        self.assertEqual(adbmod.Adb.locate(exe, env, installed="/none"), exe)
        self.assertEqual(adbmod.Adb.locate(None, dict(env, ANDROID_HOME=os.path.join(tmp, "sdk")), installed=exe), exe)
        sdk = os.path.join(tmp, "sdk", "platform-tools")
        os.makedirs(sdk)
        sdk_adb = os.path.join(sdk, "adb")
        open(sdk_adb, "w").close()
        os.chmod(sdk_adb, 0o755)
        self.assertEqual(adbmod.Adb.locate(None, dict(env, ANDROID_SDK_ROOT=os.path.join(tmp, "sdk")),
                                           installed="/none"), sdk_adb)
        self.assertEqual(adbmod.Adb.locate(None, {"HOME": tmp, "PATH": tmp}, installed="/none"), exe)


class PhoneLinkTests(unittest.TestCase):
    def serve(self, handler):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        self.addCleanup(srv.close)
        self.servers = getattr(self, "servers", []) + [srv]

        def run():
            conn, _ = srv.accept()
            with conn:
                handler(conn)

        threading.Thread(target=run, daemon=True).start()
        return srv.getsockname()[1]

    def test_connection_that_adb_drops_at_once_does_not_count(self):
        port = self.serve(lambda c: None)
        link = PhoneLink()
        self.assertFalse(link.open(port))

    def test_nothing_listening(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(PhoneLink().open(port))

    def test_quiet_but_open_connection_counts_after_400ms(self):
        done = threading.Event()
        port = self.serve(lambda c: done.wait(5))
        link = PhoneLink()
        t0 = time.time()
        self.assertTrue(link.open(port))
        self.assertGreaterEqual(time.time() - t0, 0.35)
        closed = threading.Event()
        link.on_close = closed.set
        link.close()
        self.assertTrue(closed.wait(3))
        self.assertTrue(link.closed)
        done.set()

    def test_messages_both_ways_and_close_by_the_phone(self):
        got = []
        received = threading.Event()

        def phone(conn):
            conn.sendall(b'{"type":"open","url":"u"}\nnot json\n[1]\n{"type":"cancel"}\n')
            got.append(conn.makefile("rb").readline())
            received.set()

        port = self.serve(phone)
        link = PhoneLink()
        msgs, closed = [], threading.Event()
        link.on_message = msgs.append
        link.on_close = closed.set
        self.assertTrue(link.open(port))
        wait_until(lambda: len(msgs) == 2, 5, "two messages")
        self.assertEqual(msgs, [{"type": "open", "url": "u"}, {"type": "cancel"}])
        link.send({"type": "done"})
        self.assertTrue(received.wait(3))
        self.assertEqual(json.loads(got[0]), {"type": "done"})
        self.assertTrue(closed.wait(3))
        link.send({"type": "late"})  # harmless after close

    def test_sockets_are_closed(self):
        before = open_fds()
        for _ in range(10):
            port = self.serve(lambda c: c.sendall(b'{"a":1}\n'))
            link = PhoneLink()
            closed = threading.Event()
            link.on_close = closed.set
            link.open(port)
            link.close()
            closed.wait(3)
        for srv in self.servers:  # the test's own listeners
            srv.close()
        time.sleep(0.3)
        self.assertLessEqual(open_fds(), before)


class InstallerTests(unittest.TestCase):
    def manifest(self, sha1):
        return ('<root><remotePackage path="platform-tools"><archives>'
                '<archive><complete><size>1</size><checksum type="sha1">%s</checksum>'
                '<url>platform-tools_r35.0.2-darwin.zip</url></complete><host-os>macosx</host-os></archive>'
                '<archive><complete><size>1</size><checksum type="sha1">%s</checksum>'
                '<url>platform-tools_r35.0.2-linux.zip</url></complete><host-os>linux</host-os></archive>'
                '</archives></remotePackage></root>' % ("0" * 40, sha1))

    def make_zip(self, names=("platform-tools/adb",)):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in names:
                info = zipfile.ZipInfo(n)
                info.external_attr = (stat.S_IFREG | 0o755) << 16
                z.writestr(info, "#!/bin/sh\necho adb\n")
        return buf.getvalue()

    def fetcher(self, blob, manifest_sha=None):
        sha = manifest_sha or hashlib.sha1(blob).hexdigest()
        seen = []

        def fetch(url, timeout):
            seen.append(url)
            if url == installer.MANIFEST_URL:
                return self.manifest(sha).encode()
            return blob

        fetch.seen = seen
        return fetch

    def test_picks_the_linux_archive(self):
        sha, name = installer.linux_archive(self.manifest("a" * 40))
        self.assertEqual((sha, name), ("a" * 40, "platform-tools_r35.0.2-linux.zip"))

    def test_missing_entries(self):
        with self.assertRaises(installer.InstallError):
            installer.linux_archive("<root/>")
        with self.assertRaises(installer.InstallError):
            installer.linux_archive('<remotePackage path="platform-tools"></remotePackage>')

    def test_install_verifies_and_unpacks(self):
        dest = tempfile.mkdtemp(prefix="lr-inst-")
        f = self.fetcher(self.make_zip())
        steps = []
        installer.install(steps.append, fetcher=f, dest=dest)
        adb = os.path.join(dest, "platform-tools", "adb")
        self.assertTrue(os.access(adb, os.X_OK))
        self.assertEqual(f.seen[1], installer.ARCHIVE_BASE + "platform-tools_r35.0.2-linux.zip")
        self.assertEqual(steps[-1], "Installed.")
        installer.install(steps.append, fetcher=f, dest=dest)  # replaces an existing copy

    def test_checksum_mismatch_is_refused(self):
        dest = tempfile.mkdtemp(prefix="lr-inst-")
        f = self.fetcher(self.make_zip(), manifest_sha="1" * 40)
        with self.assertRaisesRegex(installer.InstallError, "checksum"):
            installer.install(lambda s: None, fetcher=f, dest=dest)
        self.assertFalse(os.path.exists(os.path.join(dest, "platform-tools")))

    def test_path_traversal_is_refused(self):
        dest = tempfile.mkdtemp(prefix="lr-inst-")
        f = self.fetcher(self.make_zip(("platform-tools/adb", "../evil")))
        with self.assertRaisesRegex(installer.InstallError, "unsafe"):
            installer.install(lambda s: None, fetcher=f, dest=dest)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(dest), "evil")))

    def test_archive_without_adb(self):
        dest = tempfile.mkdtemp(prefix="lr-inst-")
        f = self.fetcher(self.make_zip(("platform-tools/fastboot",)))
        with self.assertRaisesRegex(installer.InstallError, "adb"):
            installer.install(lambda s: None, fetcher=f, dest=dest)

    def test_paths_follow_xdg(self):
        env = {"XDG_DATA_HOME": "/x/data", "HOME": "/h"}
        self.assertEqual(installer.installed_path(env), "/x/data/loginrelay/platform-tools/adb")
        self.assertEqual(installer.installed_path({"HOME": "/h"}), "/h/.local/share/loginrelay/platform-tools/adb")


class PrefsTests(unittest.TestCase):
    def test_round_trip_and_defaults(self):
        d = tempfile.mkdtemp(prefix="lr-prefs-")
        path = os.path.join(d, "sub", "prefs.json")
        p = Prefs(path)
        self.assertEqual((p.adb_path, p.selected_serial, p.notifications, p.has_launched_before),
                         (None, None, False, False))
        p.adb_path = "/opt/adb"
        p.selected_serial = "ABC"
        p.notifications = True
        p.has_launched_before = True
        q = Prefs(path)
        self.assertEqual((q.adb_path, q.selected_serial, q.notifications, q.has_launched_before),
                         ("/opt/adb", "ABC", True, True))
        q.adb_path = None
        self.assertIsNone(Prefs(path).adb_path)
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
