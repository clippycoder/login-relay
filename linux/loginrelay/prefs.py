# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent preferences (JSON) and the XDG folders this app uses."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Dict, List, Optional


def _xdg(var: str, default_rel: str, env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    value = env.get(var, "")
    if value and os.path.isabs(value):
        return value
    home = env.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, default_rel)


def config_home(env=None) -> str:
    return _xdg("XDG_CONFIG_HOME", ".config", env)


def data_home(env=None) -> str:
    return _xdg("XDG_DATA_HOME", ".local/share", env)


def state_home(env=None) -> str:
    return _xdg("XDG_STATE_HOME", ".local/state", env)


def default_prefs_path(env=None) -> str:
    return os.path.join(config_home(env), "loginrelay", "prefs.json")


def share_dirs(env=None) -> List[str]:
    """Folders that may hold the tray icons (and, if shipped, login-relay.apk for people to find)."""
    env = os.environ if env is None else env
    dirs: List[str] = []
    if env.get("LOGINRELAY_SHARE"):
        dirs.append(env["LOGINRELAY_SHARE"])
    # Running from a source tree: linux/data next to linux/loginrelay.
    dirs.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
    dirs.append(os.path.join(data_home(env), "loginrelay"))
    dirs += ["/usr/local/share/loginrelay", "/usr/share/loginrelay"]
    return dirs


def find_share_file(relative: str, env=None) -> Optional[str]:
    for d in share_dirs(env):
        p = os.path.join(d, relative)
        if os.path.exists(p):
            return p
    return None


class Prefs:
    """Selected USB phone, notifications, launch-at-login, first-launch flag. (Keys written by older versions
    are ignored and left alone.)"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_prefs_path()
        self._lock = threading.Lock()
        self._data: Dict[str, object] = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self._data = loaded
        except (OSError, ValueError):
            pass

    def _get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def _set(self, key, value) -> None:
        with self._lock:
            if value is None:
                self._data.pop(key, None)
            else:
                self._data[key] = value
            snapshot = dict(self._data)
        self._save(snapshot)

    def _save(self, data: Dict[str, object]) -> None:
        tmp = None
        try:
            folder = os.path.dirname(self.path)
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".prefs-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
            tmp = None
        except OSError:
            pass
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @property
    def usb_selected(self) -> Optional[str]:
        """Which USB phone to use when several are plugged in (a port location such as "1-4.2")."""
        v = self._get("usbSelected")
        return v if isinstance(v, str) and v else None

    @usb_selected.setter
    def usb_selected(self, value: Optional[str]) -> None:
        self._set("usbSelected", value)

    @property
    def notifications(self) -> bool:
        return bool(self._get("notifications", False))

    @notifications.setter
    def notifications(self, value: bool) -> None:
        self._set("notifications", bool(value))

    @property
    def launch_at_login(self) -> bool:
        return bool(self._get("launchAtLogin", False))

    @launch_at_login.setter
    def launch_at_login(self, value: bool) -> None:
        self._set("launchAtLogin", bool(value))

    @property
    def has_launched_before(self) -> bool:
        return bool(self._get("hasLaunchedBefore", False))

    @has_launched_before.setter
    def has_launched_before(self, value: bool) -> None:
        self._set("hasLaunchedBefore", bool(value))
