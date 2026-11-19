# SPDX-License-Identifier: GPL-3.0-or-later
"""claim.py's bookkeeping, stale-claim clean-up and restore, with a fake xdg-mime on temp folders."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from loginrelay import claim  # noqa: E402

SCHEME = "com.example.relaytest"
MIME = "x-scheme-handler/" + SCHEME
DID = "loginrelay-claim-%s.desktop" % SCHEME


def read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class FakePlatform:
    """Behaves like xdg-mime on top of a ~/.config/mimeapps.list in a temp dir."""

    def __init__(self, root):
        self.cfg = os.path.join(root, "config")
        self.apps = os.path.join(root, "share", "applications")
        os.makedirs(self.cfg)
        os.makedirs(self.apps)
        self.mimeapps = os.path.join(self.cfg, "mimeapps.list")
        self.refreshes = 0
        self.refuse = False       # xdg-mime default fails
        self.ignore = False       # xdg-mime default "succeeds" but the query doesn't change
        self.calls = []

    @property
    def applications_dir(self):
        return self.apps

    def mimeapps_files(self):
        return [self.mimeapps, os.path.join(self.apps, "mimeapps.list")]

    def _read(self):
        try:
            with open(self.mimeapps, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def query_default(self, mime):
        section = ""
        for line in self._read().splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line[1:-1]
            elif section == "Default Applications" and line.startswith(mime + "="):
                return line.split("=", 1)[1].split(";")[0]
        return ""

    def set_default(self, desktop_id, mime):
        self.calls.append(("default", desktop_id, mime))
        if self.refuse:
            return False
        if self.ignore:
            return True
        lines = self._read().splitlines()
        out, section, done = [], "", False
        for line in lines:
            s = line.strip()
            if s.startswith("["):
                if section == "Default Applications" and not done:
                    out.append("%s=%s;" % (mime, desktop_id))
                    done = True
                section = s[1:-1]
            elif section == "Default Applications" and s.startswith(mime + "="):
                if not done:
                    out.append("%s=%s;" % (mime, desktop_id))
                    done = True
                continue
            out.append(line)
        if not done:
            if section != "Default Applications":
                out.append("[Default Applications]")
            out.append("%s=%s;" % (mime, desktop_id))
        with open(self.mimeapps, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        return True

    def refresh_database(self):
        self.refreshes += 1


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lr-claim-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.platform = FakePlatform(self.root)
        self.store = claim.ClaimStore(os.path.join(self.root, "state", "claims.json"))
        self.mgr = claim.ClaimManager(self.platform, self.store, launcher=["/usr/bin/loginrelay"])

    def desktop(self):
        return os.path.join(self.platform.apps, DID)

    def mimeapps(self):
        return self.platform._read()

    # -- claim and release

    def test_claim_writes_desktop_file_records_and_sets_default(self):
        c = self.mgr.claim(SCHEME)
        self.assertIsNotNone(c)
        text = read_file(self.desktop())
        self.assertIn("NoDisplay=true", text)
        self.assertIn("MimeType=x-scheme-handler/%s;" % SCHEME, text)
        self.assertIn("Exec=/usr/bin/loginrelay --url %u", text)
        self.assertEqual(self.platform.query_default(MIME), DID)
        self.assertEqual(self.store.load(), {SCHEME: ""})
        self.assertGreaterEqual(self.platform.refreshes, 1)

    def test_release_without_previous_removes_our_line_and_file_and_keeps_the_rest(self):
        original = ("[Default Applications]\ntext/html=firefox.desktop;\n\n"
                    "[Added Associations]\nimage/png=gimp.desktop;\n")
        with open(self.platform.mimeapps, "w", encoding="utf-8") as f:
            f.write(original)
        c = self.mgr.claim(SCHEME)
        self.assertIn(DID, self.mimeapps())
        c.release()
        self.assertEqual(self.platform.query_default(MIME), "")
        self.assertFalse(os.path.exists(self.desktop()))
        self.assertEqual(self.store.load(), {})
        self.assertFalse(os.path.exists(self.store.path))
        # Only the Default Applications section gained and lost a line; everything else is intact.
        self.assertEqual(self.mimeapps().replace("\n\n", "\n"), original.replace("\n\n", "\n"))
        self.assertIn("text/html=firefox.desktop;", self.mimeapps())
        self.assertIn("image/png=gimp.desktop;", self.mimeapps())

    def test_release_restores_the_previous_handler(self):
        self.platform.set_default("other.desktop", MIME)
        c = self.mgr.claim(SCHEME)
        self.assertEqual(c.previous, "other.desktop")
        self.assertEqual(self.store.load(), {SCHEME: "other.desktop"})
        self.assertEqual(self.platform.query_default(MIME), DID)
        c.release()
        self.assertEqual(self.platform.query_default(MIME), "other.desktop")
        self.assertFalse(os.path.exists(self.desktop()))
        self.assertEqual(self.store.load(), {})

    def test_release_is_idempotent(self):
        c = self.mgr.claim(SCHEME)
        c.release()
        n = len(self.platform.calls)
        c.release()
        self.assertEqual(len(self.platform.calls), n)

    def test_record_is_written_before_the_system_is_touched(self):
        seen = []
        orig = self.platform.set_default

        def spy(did, mime):
            seen.append(self.store.load())
            return orig(did, mime)

        self.platform.set_default = spy
        self.mgr.claim(SCHEME)
        self.assertEqual(seen[0], {SCHEME: ""})

    # -- refusal

    def test_refused_claim_returns_none_and_leaves_nothing_behind(self):
        self.platform.set_default("other.desktop", MIME)
        self.platform.refuse = True
        self.assertIsNone(self.mgr.claim(SCHEME))
        self.assertFalse(os.path.exists(self.desktop()))
        self.assertEqual(self.store.load(), {})

    def test_claim_that_does_not_verify_is_refused(self):
        self.platform.ignore = True
        self.assertIsNone(self.mgr.claim(SCHEME))
        self.assertFalse(os.path.exists(self.desktop()))
        self.assertEqual(self.store.load(), {})

    def test_hostile_scheme_is_refused(self):
        for bad in ("../evil", "a b", "", "x/y", "1abc", "a;b"):
            self.assertIsNone(self.mgr.claim(bad), bad)
        self.assertEqual(os.listdir(self.platform.apps), [])

    # -- stale claims

    def test_stale_claim_from_a_crash_is_cleaned_up(self):
        self.platform.set_default("other.desktop", MIME)
        self.mgr.claim(SCHEME)           # ... and the app "crashes" here
        fresh = claim.ClaimManager(self.platform, claim.ClaimStore(self.store.path), launcher=["x"])
        self.assertEqual(fresh.release_stale(), [SCHEME])
        self.assertEqual(self.platform.query_default(MIME), "other.desktop")
        self.assertFalse(os.path.exists(self.desktop()))
        self.assertEqual(self.store.load(), {})

    def test_stale_claim_with_no_previous_handler(self):
        self.mgr.claim(SCHEME)
        fresh = claim.ClaimManager(self.platform, claim.ClaimStore(self.store.path), launcher=["x"])
        fresh.release_stale()
        self.assertEqual(self.platform.query_default(MIME), "")
        self.assertNotIn(DID, self.mimeapps())

    def test_orphaned_desktop_file_without_a_record_is_removed(self):
        with open(self.desktop(), "w") as f:
            f.write("[Desktop Entry]\n")
        self.mgr.release_stale()
        self.assertFalse(os.path.exists(self.desktop()))

    def test_stale_cleanup_does_not_clobber_someone_elses_handler(self):
        self.mgr.claim(SCHEME)
        self.platform.set_default("newer.desktop", MIME)   # the user picked another app meanwhile
        self.mgr.release_stale()
        self.assertEqual(self.platform.query_default(MIME), "newer.desktop")

    def test_claiming_again_over_a_stale_claim_keeps_the_original_previous(self):
        self.platform.set_default("other.desktop", MIME)
        self.mgr.claim(SCHEME)
        c2 = self.mgr.claim(SCHEME)
        self.assertEqual(c2.previous, "other.desktop")
        c2.release()
        self.assertEqual(self.platform.query_default(MIME), "other.desktop")

    # -- mimeapps.list editing

    def test_strip_entries_edits_only_our_value(self):
        text = ("# comment\n[Default Applications]\n"
                "x-scheme-handler/a=%s;keep.desktop;\n"
                "x-scheme-handler/b=%s;\n"
                "[Added Associations]\nx-scheme-handler/a=%s;\n" % (DID, DID, DID))
        got = claim.strip_entries(text, "x-scheme-handler/a", DID)
        self.assertEqual(got, "# comment\n[Default Applications]\n"
                              "x-scheme-handler/a=keep.desktop;\n"
                              "x-scheme-handler/b=%s;\n"
                              "[Added Associations]\n" % DID)

    def test_strip_entries_preserves_crlf_and_missing_final_newline(self):
        text = "[Default Applications]\r\nx-scheme-handler/a=%s;\r\nfoo/bar=z.desktop;" % DID
        self.assertEqual(claim.strip_entries(text, "x-scheme-handler/a", DID),
                         "[Default Applications]\r\nfoo/bar=z.desktop;")

    def test_strip_entries_ignores_other_sections(self):
        text = "[Removed Associations]\nx-scheme-handler/a=%s;\n" % DID
        self.assertEqual(claim.strip_entries(text, "x-scheme-handler/a", DID), text)

    def test_strip_file_leaves_an_untouched_file_alone(self):
        p = os.path.join(self.root, "m.list")
        with open(p, "w") as f:
            f.write("[Default Applications]\nfoo/bar=a.desktop;\n")
        before = os.stat(p).st_mtime_ns
        self.assertFalse(claim.strip_file(p, MIME, DID))
        self.assertEqual(os.stat(p).st_mtime_ns, before)
        self.assertFalse(claim.strip_file(os.path.join(self.root, "missing"), MIME, DID))


class ExecQuotingTests(unittest.TestCase):
    @staticmethod
    def parse(value):
        """What a Desktop Entry reader does: undo string escapes, then split Exec arguments."""
        s, out, i = value, [], 0
        while i < len(s):
            if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in "sntr\\":
                out.append({"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}[s[i + 1]])
                i += 2
            else:
                out.append(s[i])
                i += 1
        s = "".join(out)
        args, cur, quoted, have, i = [], [], False, False, 0
        while i < len(s):
            c = s[i]
            if quoted:
                if c == "\\" and i + 1 < len(s):
                    cur.append(s[i + 1])
                    i += 1
                elif c == '"':
                    quoted = False
                else:
                    cur.append(c)
            elif c == '"':
                quoted, have = True, True
            elif c in " \t":
                if have or cur:
                    args.append("".join(cur))
                cur, have = [], False
            else:
                cur.append(c)
            i += 1
        if have or cur:
            args.append("".join(cur))
        return [a.replace("%%", "%") for a in args]

    def test_round_trip(self):
        for args in (["/usr/bin/loginrelay"],
                     ["env", "PYTHONPATH=/home/a b/src", "/usr/bin/python3", "-m", "loginrelay"],
                     ["/opt/My Apps/lr", "--x"],
                     ['/weird/"quote"/$HOME/`x`/back\\slash/100%', "a;b", "(paren)", ""],
                     ["/tmp/tab\there"]):
            with self.subTest(args):
                line = claim.exec_line(args)
                self.assertEqual(self.parse(line), args)

    def test_plain_words_are_not_quoted(self):
        self.assertEqual(claim.exec_line(["/usr/bin/loginrelay", "--url"]), "/usr/bin/loginrelay --url")

    def test_desktop_text_keeps_the_url_field_code_unquoted(self):
        text = claim.claim_desktop_text("a.b", ["/opt/My Apps/lr"])
        self.assertIn('Exec="/opt/My Apps/lr" --url %u\n', text)

    def test_launcher_from_environment_and_source_tree(self):
        exe = os.path.join(tempfile.mkdtemp(prefix="lr-l-"), "loginrelay")
        self.addCleanup(shutil.rmtree, os.path.dirname(exe), True)
        with open(exe, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        self.assertEqual(claim.launcher_command({"LOGINRELAY_LAUNCHER": exe}), [exe])
        cmd = claim.launcher_command({})
        self.assertEqual(cmd[0], "env")
        self.assertTrue(cmd[1].startswith("PYTHONPATH="))
        self.assertTrue(os.path.isdir(os.path.join(cmd[1][len("PYTHONPATH="):], "loginrelay")))
        self.assertEqual(cmd[-2:], ["-m", "loginrelay"])


class AutostartTests(unittest.TestCase):
    def test_enable_and_disable(self):
        root = tempfile.mkdtemp(prefix="lr-auto-")
        self.addCleanup(shutil.rmtree, root, True)
        env = {"XDG_CONFIG_HOME": root, "HOME": root}
        self.assertFalse(claim.autostart_enabled(env))
        claim.set_autostart(True, launcher=["/usr/bin/loginrelay"], env=env)
        path = claim.autostart_path(env)
        self.assertTrue(path.endswith("autostart/io.github.clippycoder.LoginRelay.desktop"))
        self.assertIn("Exec=/usr/bin/loginrelay --background", read_file(path))
        self.assertTrue(claim.autostart_enabled(env))
        claim.set_autostart(False, env=env)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(claim.autostart_enabled(env))


if __name__ == "__main__":
    unittest.main()
