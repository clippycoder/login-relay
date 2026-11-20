# SPDX-License-Identifier: GPL-3.0-or-later
"""Thin wrapper over the adb command-line tool. Everything here blocks; call it off the UI thread."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional

from .redirect import shell_quote


@dataclass(frozen=True)
class Device:
    serial: str
    state: str  # "device", "unauthorized", "offline", ...
    model: str = ""

    @property
    def is_usable(self) -> bool:
        return self.state == "device"

    @property
    def is_usb(self) -> bool:
        """A phone reached over a USB accessory pipe rather than adb (serial "usb:<location>")."""
        return self.serial.startswith("usb:")

    @property
    def title(self) -> str:
        return self.serial if not self.model else self.model.replace("_", " ")


@dataclass(frozen=True)
class ProcessResult:
    status: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 0


def _text(f) -> str:
    try:
        f.seek(0)
        return f.read().decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return ""


def exec_process(path: str, args: List[str], timeout: float = 20, env=None) -> ProcessResult:
    """Runs a program and returns its output.

    Output goes to temp files, not pipes. When adb has to start its server it forks a daemon that
    inherits our stdout/stderr; with pipes we'd wait for end-of-file until that daemon exits, i.e.
    forever. We wait for the process itself, kill it (and its group) after `timeout`, and read
    whatever it wrote. Every handle opened here is closed before returning.
    """
    try:
        out_f = tempfile.TemporaryFile(prefix="loginrelay-")
    except OSError as e:
        return ProcessResult(-1, "", "couldn't create temp files: %s" % e)
    try:
        err_f = tempfile.TemporaryFile(prefix="loginrelay-")
    except OSError as e:
        out_f.close()
        return ProcessResult(-1, "", "couldn't create temp files: %s" % e)
    try:
        try:
            proc = subprocess.Popen(
                [path] + list(args), stdin=subprocess.DEVNULL, stdout=out_f, stderr=err_f,
                env=env, start_new_session=(os.name == "posix"))
        except (OSError, ValueError) as e:
            return ProcessResult(-1, "", str(e))
        try:
            status = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.wait()
            return ProcessResult(-1, _text(out_f), (_text(err_f) + " timed out after %gs" % timeout).strip())
        return ProcessResult(status, _text(out_f), _text(err_f))
    finally:
        out_f.close()
        err_f.close()


def _kill(proc: "subprocess.Popen") -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


class Adb:
    def __init__(self, path: str):
        self.path = path

    # ---- finding adb ----------------------------------------------------------------------

    @staticmethod
    def locate(user_choice: Optional[str] = None, env=None, installed: Optional[str] = None) -> Optional[str]:
        """GUI apps start with a minimal PATH, so look in the usual places explicitly."""
        env = os.environ if env is None else env
        # Test hook: the end-to-end tests (tests/e2e) point the app at a fake adb.
        forced = env.get("LOGINRELAY_ADB")
        if forced:
            return forced
        if installed is None:
            from . import installer
            installed = installer.installed_path(env)
        home = env.get("HOME") or os.path.expanduser("~")
        candidates: List[str] = []
        if user_choice:
            candidates.append(user_choice)
        candidates.append(installed)
        for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            if env.get(key):
                candidates.append(os.path.join(env[key], "platform-tools", "adb"))
        candidates += [
            os.path.join(home, "Android", "Sdk", "platform-tools", "adb"),
            os.path.join(home, "Library", "Android", "sdk", "platform-tools", "adb"),
            "/usr/bin/adb",
            "/usr/local/bin/adb",
            "/usr/lib/android-sdk/platform-tools/adb",
            "/opt/android-sdk/platform-tools/adb",
            "/opt/platform-tools/adb",
            "/snap/bin/adb",
            os.path.join(home, ".local", "bin", "adb"),
        ]
        for c in candidates:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return shutil.which("adb", path=env.get("PATH"))

    # ---- running --------------------------------------------------------------------------

    def run(self, args: List[str], serial: Optional[str] = None, timeout: float = 20) -> ProcessResult:
        full = (["-s", serial] if serial else []) + list(args)
        return exec_process(self.path, full, timeout=timeout)

    # ---- phone queries --------------------------------------------------------------------

    def devices(self) -> List[Device]:
        r = self.run(["devices", "-l"], timeout=15)
        if not r.ok:
            return []
        result = []
        for line in r.out.splitlines():
            if line.startswith("*") or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            model = ""
            for p in parts:
                if p.startswith("model:"):
                    model = p[len("model:"):]
                    break
            result.append(Device(parts[0], parts[1], model))
        return result

    def shell(self, serial: str, command: str, timeout: float = 20) -> ProcessResult:
        return self.run(["shell", command], serial=serial, timeout=timeout)

    def installed_version(self, serial: str, package: str) -> Optional[str]:
        """Version name of the phone-side app, or None if it isn't installed."""
        r = self.shell(serial, "dumpsys package %s" % package)
        if not r.ok or "versionName=" not in r.out:
            return None
        for line in r.out.splitlines():
            t = line.strip()
            if t.startswith("versionName="):
                return t[len("versionName="):]
        return None

    def existing_forward(self, serial: str, timeout: float = 20) -> Optional[int]:
        """Reuses an existing forward for this phone rather than adding a new one every few seconds."""
        r = self.run(["forward", "--list"], timeout=timeout)
        if not r.ok:
            return None
        for line in r.out.splitlines():
            p = line.split()
            if len(p) == 3 and p[0] == serial and p[2] == "localabstract:oauthrelay" and p[1].startswith("tcp:"):
                try:
                    return int(p[1][4:])
                except ValueError:
                    return None
        return None

    def forward(self, serial: str) -> Optional[int]:
        """Sets up the tunnel to the phone app's abstract socket and returns the local TCP port."""
        r = self.run(["forward", "tcp:0", "localabstract:oauthrelay"], serial=serial)
        if not r.ok:
            return None
        try:
            return int(r.out.split()[-1])
        except (ValueError, IndexError):
            return None

    def ensure_forward(self, serial: str) -> Optional[int]:
        port = self.existing_forward(serial)
        return port if port is not None else self.forward(serial)

    def remove_forward(self, port: int, serial: str, timeout: float = 20) -> None:
        self.run(["forward", "--remove", "tcp:%d" % port], serial=serial, timeout=timeout)

    def deliver(self, serial: str, url: str, package: Optional[str]) -> ProcessResult:
        """Hands a return address to the app that asked for the page."""
        cmd = "am start -a android.intent.action.VIEW -d %s" % shell_quote(url)
        if package:
            cmd += " -p %s" % shell_quote(package)
        return self.shell(serial, cmd)

    def install(self, serial: str, apk: str) -> ProcessResult:
        return self.run(["install", "-r", apk], serial=serial, timeout=120)
