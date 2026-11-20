# SPDX-License-Identifier: GPL-3.0-or-later
"""Best-effort helper: stop a running adb server that is holding the phone's USB interface.

Login Relay talks to the phone through USB accessory mode and has nothing to do with adb. But while
an adb server is running it can keep the accessory interface claimed, and then opening it fails with
"busy". When that happens the engine calls stop_adb_server(), which finds an `adb` program (PATH or the
usual Android SDK folders) and runs `adb kill-server`. No adb installed is the normal case and silent.

The program's output is never read through pipes: starting or stopping the server forks a daemon that
inherits stdout/stderr, so a pipe would not reach end-of-file until that daemon exits.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
from typing import List, Optional


# Where an Android SDK commonly lives on a Linux system (besides the user's home); tests clear this.
SYSTEM_SDK_ROOTS = ("/opt/android-sdk", "/usr/lib/android-sdk", "/usr/local/lib/android/sdk")


def find_adb(env=None) -> Optional[str]:
    env = os.environ if env is None else env
    found = shutil.which("adb", path=env.get("PATH") or None)
    if found:
        return found
    home = env.get("HOME") or os.path.expanduser("~")
    roots: List[str] = [env[k] for k in ("ANDROID_HOME", "ANDROID_SDK_ROOT") if env.get(k)]
    roots += [os.path.join(home, "Android", "Sdk"), os.path.join(home, "Android", "sdk")]
    roots += list(SYSTEM_SDK_ROOTS)
    for root in roots:
        candidate = os.path.join(root, "platform-tools", "adb")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def stop_adb_server(timeout: float = 5.0, env=None) -> bool:
    """Runs `adb kill-server` if adb can be found. Returns whether it ran (not whether it helped)."""
    path = find_adb(env)
    if path is None:
        return False
    try:
        proc = subprocess.Popen([path, "kill-server"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=(os.name == "posix"))
    except (OSError, ValueError):
        return False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            pass
    return True
