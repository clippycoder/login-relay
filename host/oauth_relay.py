#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""oauth-relay host: finish a phone app's browser sign-in on this computer.

The Login Relay app on the phone stands in for a browser. When an app opens a sign-in page,
Login Relay serves it on the abstract socket @oauthrelay. This script reaches that socket with
`adb forward`, opens the page in your normal browser, catches the redirect back to the app
(by registering itself, for the duration of one login, as the handler for the app's URL scheme),
and delivers it to the app with `adb shell am start`. The app then finishes the login itself;
with PKCE the code is useless without a secret that never leaves the phone.

Standard library only. Run:  python3 oauth_relay.py [--serial SERIAL] [--paste]
"""
import argparse
import ctypes
import json
import os
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.parse
import uuid
import webbrowser

PKG = "io.github.clippycoder.oauthrelay"
SOCKET_NAME = "oauthrelay"
RELEASES = "https://github.com/clippycoder/oauth-relay/releases"
LOGIN_TIMEOUT = 15 * 60


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- adb

def find_adb():
    cands = [os.environ.get("ADB"), shutil.which("adb")]
    home = os.path.expanduser("~")
    cands += [
        os.path.join(home, "Library/Android/sdk/platform-tools/adb"),
        os.path.join(home, "Android/Sdk/platform-tools/adb"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android/Sdk/platform-tools/adb.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    sys.exit("adb not found. Install Android platform-tools, or set ADB=/path/to/adb.")


class Adb:
    def __init__(self, path, serial=None):
        self.path, self.serial = path, serial

    def run(self, *args, check=True):
        cmd = [self.path] + (["-s", self.serial] if self.serial else []) + list(args)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(args)}: {(r.stderr or r.stdout).strip()}")
        return r.stdout.strip()

    def shell(self, command, check=True):
        return self.run("shell", command, check=check)


def pick_device(adb, serial):
    while True:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True).stdout
        devs = [l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("\tdevice")]
        if serial:
            if serial in devs:
                return serial
        elif len(devs) == 1:
            return devs[0]
        elif len(devs) > 1:
            sys.exit("Several phones are connected; pick one with --serial: " + ", ".join(devs))
        log("Waiting for a phone over adb (USB debugging or wireless debugging)…")
        time.sleep(3)


# ---------------------------------------------------------------- callback catchers
#
# Each catcher makes this computer the handler for one URL scheme (e.g. com.example.app) for the
# length of one login. The OS-level handler only appends the URL it was opened with to a file;
# the main process watches that file. Previous handlers are put back afterwards.

class Catcher:
    base_dir = None  # None = system temp dir

    def __init__(self, scheme):
        self.scheme = scheme
        if self.base_dir:
            os.makedirs(self.base_dir, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="oauth-relay-", dir=self.base_dir)
        self.file = os.path.join(self.dir, "callbacks.txt")
        open(self.file, "w").close()
        self.seen = 0

    def register(self):
        raise NotImplementedError

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def poll(self):
        with open(self.file, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f if l.strip()]
        new, self.seen = lines[self.seen:], len(lines)
        return new


class MacCatcher(Catcher):
    LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                  "LaunchServices.framework/Support/lsregister")
    # LaunchServices registers apps under /var/folders (the temp dir) but never routes URLs to
    # them, so the catcher app has to live somewhere ordinary.
    base_dir = os.path.expanduser("~/Library/Caches/oauth-relay")

    def register(self):
        self.app = os.path.join(self.dir, "Login Relay.app")
        self.bundle_id = f"{PKG}.catcher.{uuid.uuid4().hex[:8]}"
        script = textwrap.dedent(f"""\
            on open location theURL
            \tdo shell script "printf '%s\\\\n' " & quoted form of theURL & " >> " & quoted form of "{self.file}"
            end open location
            """)
        src = os.path.join(self.dir, "catcher.applescript")
        with open(src, "w") as f:
            f.write(script)
        subprocess.run(["osacompile", "-o", self.app, src], check=True, capture_output=True)
        plist_path = os.path.join(self.app, "Contents", "Info.plist")
        with open(plist_path, "rb") as f:
            info = plistlib.load(f)
        info.update({
            "CFBundleIdentifier": self.bundle_id,
            "CFBundleName": "Login Relay",
            "LSUIElement": True,
            "CFBundleURLTypes": [{"CFBundleURLName": "Login Relay callback",
                                  "CFBundleURLSchemes": [self.scheme]}],
        })
        with open(plist_path, "wb") as f:
            plistlib.dump(info, f)
        subprocess.run(["codesign", "-f", "-s", "-", self.app], check=True, capture_output=True)
        subprocess.run([self.LSREGISTER, "-f", self.app], check=True, capture_output=True)
        # If something else already handles this scheme, make us the default and remember it.
        self.previous = self._ls_default()
        if self.previous and self.previous.lower() != self.bundle_id.lower():
            self._ls_set_default(self.bundle_id)
        return True

    def cleanup(self):
        try:
            if self.previous and self.previous.lower() != self.bundle_id.lower():
                self._ls_set_default(self.previous)
            subprocess.run([self.LSREGISTER, "-u", self.app], capture_output=True)
        finally:
            super().cleanup()

    # LaunchServices via ctypes (no PyObjC needed).
    def _cf(self):
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ls = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreServices.framework/CoreServices")
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                                          ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        ls.LSCopyDefaultHandlerForURLScheme.restype = ctypes.c_void_p
        ls.LSCopyDefaultHandlerForURLScheme.argtypes = [ctypes.c_void_p]
        ls.LSSetDefaultHandlerForURLScheme.restype = ctypes.c_int32
        ls.LSSetDefaultHandlerForURLScheme.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return cf, ls

    def _cfstr(self, cf, s):
        return cf.CFStringCreateWithCString(None, s.encode(), 0x08000100)

    def _ls_default(self):
        try:
            cf, ls = self._cf()
            ref = ls.LSCopyDefaultHandlerForURLScheme(self._cfstr(cf, self.scheme))
            if not ref:
                return None
            buf = ctypes.create_string_buffer(512)
            ok = cf.CFStringGetCString(ref, buf, 512, 0x08000100)
            cf.CFRelease(ref)
            return buf.value.decode() if ok else None
        except OSError:
            return None

    def _ls_set_default(self, bundle_id):
        try:
            cf, ls = self._cf()
            ls.LSSetDefaultHandlerForURLScheme(self._cfstr(cf, self.scheme),
                                               self._cfstr(cf, bundle_id))
        except OSError:
            pass


class LinuxCatcher(Catcher):
    def register(self):
        if not shutil.which("xdg-mime"):
            return False
        apps = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
                            "applications")
        os.makedirs(apps, exist_ok=True)
        handler = os.path.join(self.dir, "handler.sh")
        with open(handler, "w") as f:
            f.write(f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {shlex.quote(self.file)}\n")
        os.chmod(handler, 0o755)
        self.desktop_name = f"oauth-relay-{uuid.uuid4().hex[:8]}.desktop"
        self.desktop = os.path.join(apps, self.desktop_name)
        with open(self.desktop, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Login Relay\nNoDisplay=true\n"
                    f"Exec=\"{handler}\" %u\nMimeType=x-scheme-handler/{self.scheme};\n")
        self.mime = f"x-scheme-handler/{self.scheme}"
        self.previous = subprocess.run(["xdg-mime", "query", "default", self.mime],
                                       capture_output=True, text=True).stdout.strip()
        subprocess.run(["xdg-mime", "default", self.desktop_name, self.mime], check=True)
        return True

    def cleanup(self):
        try:
            if self.previous:
                subprocess.run(["xdg-mime", "default", self.previous, self.mime])
            else:
                # Drop our association line from mimeapps.list.
                conf = os.path.join(os.environ.get("XDG_CONFIG_HOME",
                                                   os.path.expanduser("~/.config")),
                                    "mimeapps.list")
                if os.path.exists(conf):
                    with open(conf) as f:
                        lines = f.readlines()
                    with open(conf, "w") as f:
                        f.writelines(l for l in lines if self.desktop_name not in l)
            if os.path.exists(self.desktop):
                os.remove(self.desktop)
        finally:
            super().cleanup()


class WindowsCatcher(Catcher):
    def register(self):
        import winreg
        self.winreg = winreg
        handler = os.path.join(self.dir, "handler.py")
        with open(handler, "w") as f:
            f.write("import sys\n"
                    f"open({self.file!r}, 'a', encoding='utf-8').write(sys.argv[1] + '\\n')\n")
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
        self.key = rf"Software\Classes\{self.scheme}"
        self.backup = self._dump(self.key)
        if self.backup is not None:
            self._delete(self.key)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.key) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Login Relay")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.key + r"\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" "{handler}" "%1"')
        return True

    def cleanup(self):
        try:
            self._delete(self.key)
            if self.backup is not None:
                self._restore(self.key, self.backup)
        finally:
            super().cleanup()

    def _dump(self, path):
        w = self.winreg
        try:
            k = w.OpenKey(w.HKEY_CURRENT_USER, path)
        except OSError:
            return None
        with k:
            values, subs, i = [], [], 0
            while True:
                try:
                    values.append(w.EnumValue(k, i))
                    i += 1
                except OSError:
                    break
            i = 0
            while True:
                try:
                    subs.append(w.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
        return {"values": values, "subs": {s: self._dump(path + "\\" + s) for s in subs}}

    def _delete(self, path):
        w = self.winreg
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, path) as k:
                subs = []
                i = 0
                while True:
                    try:
                        subs.append(w.EnumKey(k, i))
                        i += 1
                    except OSError:
                        break
            for s in subs:
                self._delete(path + "\\" + s)
            w.DeleteKey(w.HKEY_CURRENT_USER, path)
        except OSError:
            pass

    def _restore(self, path, tree):
        w = self.winreg
        with w.CreateKey(w.HKEY_CURRENT_USER, path) as k:
            for name, data, typ in tree["values"]:
                w.SetValueEx(k, name, 0, typ, data)
        for s, sub in tree["subs"].items():
            self._restore(path + "\\" + s, sub)


def make_catcher(scheme):
    if sys.platform == "darwin":
        return MacCatcher(scheme)
    if sys.platform.startswith("win"):
        return WindowsCatcher(scheme)
    return LinuxCatcher(scheme)


# ---------------------------------------------------------------- one login

def same_redirect(callback, redirect):
    """True if callback is the redirect URI plus a query/fragment (scheme case-insensitive)."""
    base = redirect.split("?")[0].split("#")[0]
    s1, _, r1 = base.partition(":")
    cb_base = callback.split("?")[0].split("#")[0]
    s2, _, r2 = cb_base.partition(":")
    return s1.lower() == s2.lower() and r1.rstrip("/") == r2.rstrip("/")


def params(url):
    u = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(u.query)
    q.update(urllib.parse.parse_qs(u.fragment))
    return {k: v[0] for k, v in q.items()}


class Session:
    def __init__(self, sock):
        self.sock = sock
        self.reader = sock.makefile("r", encoding="utf-8", newline="\n")

    def send(self, **msg):
        try:
            self.sock.sendall((json.dumps(msg) + "\n").encode())
        except OSError:
            pass

    def status(self, text):
        self.send(type="status", text=text)


def handle_open(args, adb, sess, url, referrer):
    q = params(url)
    redirect = q.get("redirect_uri")
    caller = None
    if referrer and referrer.startswith("android-app://"):
        caller = referrer[len("android-app://"):].split("/")[0] or None

    if not redirect:
        log(f"\nOpening a plain link from {caller or 'an app'}: {url}")
        webbrowser.open(url)
        sess.status("Opened on your computer.")
        time.sleep(1)
        sess.send(type="done")
        return

    host = urllib.parse.urlsplit(url).hostname
    log(f"\nSign-in requested by {caller or 'an app'} at {host}")
    if "code_challenge" not in q:
        log("  Warning: this sign-in does not use PKCE, so the code passing through this computer "
            "could be used on its own.")
        if not args.yes and input("  Continue anyway? [y/N] ").strip().lower() != "y":
            sess.send(type="error", text="Cancelled on the computer.")
            return

    scheme = urllib.parse.urlsplit(redirect).scheme.lower()
    catcher = None
    if not args.paste and scheme not in ("http", "https"):
        catcher = make_catcher(scheme)
        try:
            if not catcher.register():
                catcher.cleanup()
                catcher = None
        except Exception as e:  # fall back to pasting rather than failing the login
            log(f"  Could not register as handler for {scheme}:// ({e}); falling back to paste.")
            catcher.cleanup()
            catcher = None

    try:
        sess.status("Finish signing in on your computer.")
        log("  Opening the sign-in page in your browser…")
        webbrowser.open(url)
        callback = None
        if catcher:
            log(f"  If your browser asks to open \"Login Relay\", allow it.")
            deadline = time.time() + LOGIN_TIMEOUT
            while callback is None and time.time() < deadline:
                for cb in catcher.poll():
                    cbp = params(cb)
                    if not same_redirect(cb, redirect):
                        continue
                    if q.get("state") and cbp.get("state") != q["state"]:
                        log("  Ignoring a callback from a different sign-in (state mismatch).")
                        continue
                    callback = cb
                    break
                time.sleep(0.3)
        else:
            log("  After signing in, copy the full address your browser ends up on (it starts "
                f"with {redirect.split('?')[0]}) and paste it here:")
            while callback is None:
                cb = input("  > ").strip()
                if same_redirect(cb, redirect):
                    callback = cb
                else:
                    log("  That isn't the app's return address; try again.")
        if callback is None:
            sess.send(type="error", text="Timed out waiting for the sign-in.")
            log("  Timed out.")
            return
    finally:
        if catcher:
            catcher.cleanup()

    if "error" in params(callback):
        log(f"  The sign-in page returned an error: {params(callback).get('error_description') or params(callback)['error']}")
    cmd = "am start -a android.intent.action.VIEW -d " + shlex.quote(callback)
    if caller:
        cmd += " -p " + shlex.quote(caller)
    out = adb.shell(cmd, check=False)
    if "Error" in out:
        log(f"  The phone could not deliver the result: {out}")
        sess.send(type="error", text="Could not hand the result back to the app.")
        return
    log("  Sent back to the phone; the app is finishing the sign-in.")
    # Let the app's own activity take the result before Login Relay closes.
    time.sleep(1.5)
    sess.send(type="done")


def main():
    ap = argparse.ArgumentParser(description="Finish a phone app's browser sign-in on this "
                                             "computer, over adb.")
    ap.add_argument("--serial", help="adb serial of the phone, if several are connected")
    ap.add_argument("--paste", action="store_true",
                    help="don't register a URL handler; paste the final address by hand")
    ap.add_argument("--yes", action="store_true", help="don't ask before non-PKCE sign-ins")
    args = ap.parse_args()

    adb_path = find_adb()
    adb = Adb(adb_path, pick_device(adb_path, args.serial))
    if not adb.shell(f"pm path {PKG}", check=False).startswith("package:"):
        sys.exit(f"Login Relay isn't installed on the phone. Get login-relay.apk from {RELEASES} "
                 f"and run: adb install login-relay.apk")

    port = int(adb.run("forward", "tcp:0", f"localabstract:{SOCKET_NAME}"))
    log(f"Connected to {adb.serial}. Waiting for an app on the phone to open a sign-in page. "
        "Ctrl-C to stop.")
    try:
        while True:
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            except OSError:
                time.sleep(1)
                continue
            sock.settimeout(None)
            sess = Session(sock)
            try:
                # adb accepts the TCP connection even when Login Relay isn't open; it closes
                # straight away in that case, which reads as EOF here.
                for line in sess.reader:
                    msg = json.loads(line)
                    if msg.get("type") == "open":
                        handle_open(args, adb, sess, msg["url"], msg.get("referrer"))
            except (OSError, ValueError) as e:
                log(f"Connection to the phone dropped ({e}).")
            finally:
                sock.close()
            time.sleep(1)
    except KeyboardInterrupt:
        log("\nStopped.")
    finally:
        adb.run("forward", "--remove", f"tcp:{port}", check=False)


if __name__ == "__main__":
    main()
