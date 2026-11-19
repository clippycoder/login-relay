# SPDX-License-Identifier: GPL-3.0-or-later
"""Makes this app the default handler for one URL scheme (an app's sign-in return address) for
the length of one login, then puts the previous handler back.

How: a .desktop file ~/.local/share/applications/loginrelay-claim-<scheme>.desktop declares
MimeType=x-scheme-handler/<scheme> and Exec=<launcher> --url %u, and `xdg-mime default` makes it the
default (that edits ~/.config/mimeapps.list, which `xdg-mime query default` and `xdg-open` read).
The browser's hand-off then starts a second process with --url, which forwards the address to
the running app (see app.py).

The claim is recorded in $XDG_STATE_HOME/loginrelay/claims.json (scheme -> previous default or "")
BEFORE anything is changed, so a crash mid-login is cleaned up by release_stale() on the next launch.

Everything that touches the system is in `Platform`; the bookkeeping is plain files and can be tested
with a fake Platform (linux/tests/test_claim.py). No gi imports here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional

from .prefs import config_home, data_home, state_home

DESKTOP_PREFIX = "loginrelay-claim-"
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*$")
AUTOSTART_NAME = "io.github.clippycoder.LoginRelay.desktop"
MIMEAPPS_SECTIONS = ("Default Applications", "Added Associations")


# ---- Desktop Entry helpers -------------------------------------------------------------------

_EXEC_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def exec_quote(arg: str) -> str:
    """Quotes one argument for the Exec key of a .desktop file (Desktop Entry spec: double quotes,
    backslash-escape `"`, backtick, `$` and `\\`, then the string-value layer doubles every
    backslash again; a literal % is written %%)."""
    if arg == "":
        return '""'
    if not any(c in _EXEC_RESERVED for c in arg):
        return arg.replace("%", "%%")
    out = []
    for c in arg:
        if c in '"`$':
            out.append("\\\\" + c)
        elif c == "\\":
            out.append("\\\\\\\\")
        elif c == "%":
            out.append("%%")
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def exec_line(args: List[str]) -> str:
    return " ".join(exec_quote(a) for a in args)


def launcher_command(env=None) -> List[str]:
    """The app's own command line. Installed packages start through a launcher script that sets
    LOGINRELAY_LAUNCHER; from a source tree it is `env PYTHONPATH=<dir> python3 -m loginrelay`."""
    env = os.environ if env is None else env
    launcher = env.get("LOGINRELAY_LAUNCHER", "")
    if launcher and os.path.isfile(launcher) and os.access(launcher, os.X_OK):
        return [launcher]
    parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return ["env", "PYTHONPATH=" + parent, sys.executable or "python3", "-m", "loginrelay"]


def claim_desktop_id(scheme: str) -> str:
    return "%s%s.desktop" % (DESKTOP_PREFIX, scheme)


def claim_desktop_text(scheme: str, launcher: List[str]) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Login Relay (sign-in return)\n"
        "NoDisplay=true\n"
        "Terminal=false\n"
        "Exec=%s --url %%u\n"
        "MimeType=x-scheme-handler/%s;\n" % (exec_line(launcher), scheme))


# ---- mimeapps.list editing -------------------------------------------------------------------

def strip_entries(text: str, mime: str, desktop_id: str) -> str:
    """Removes `desktop_id` from the `mime` lines of [Default Applications] and [Added Associations].
    A line that ends up empty is dropped; everything else is preserved exactly."""
    out: List[str] = []
    section = ""
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            out.append(line)
            continue
        if section in MIMEAPPS_SECTIONS and "=" in line and not stripped.startswith("#"):
            key, _, value = line.partition("=")
            if key.strip() == mime:
                items = [v for v in value.strip().split(";") if v]
                kept = [v for v in items if v != desktop_id]
                if len(kept) == len(items):
                    out.append(line)
                elif kept:
                    ending = line[len(line.rstrip("\r\n")):]
                    out.append("%s=%s;%s" % (key, ";".join(kept), ending))
                continue
        out.append(line)
    return "".join(out)


def strip_file(path: str, mime: str, desktop_id: str) -> bool:
    """Applies strip_entries to a mimeapps.list on disk. True if the file changed."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return False
    new = strip_entries(text, mime, desktop_id)
    if new == text:
        return False
    _atomic_write(path, new)
    return True


def _atomic_write(path: str, text: str) -> None:
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".loginrelay-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- the system layer ------------------------------------------------------------------------

class Platform:
    """The real thing: xdg-mime, update-desktop-database and the XDG folders. Tests substitute a
    fake with the same methods."""

    def __init__(self, env=None):
        self.env = dict(os.environ if env is None else env)

    @property
    def applications_dir(self) -> str:
        return os.path.join(data_home(self.env), "applications")

    def mimeapps_files(self) -> List[str]:
        """Files `xdg-mime default` may have written our line to."""
        cfg, apps = config_home(self.env), self.applications_dir
        files = [os.path.join(cfg, "mimeapps.list"), os.path.join(apps, "mimeapps.list")]
        for de in self.env.get("XDG_CURRENT_DESKTOP", "").lower().split(":"):
            if de:
                files += [os.path.join(cfg, de + "-mimeapps.list"), os.path.join(apps, de + "-mimeapps.list")]
        return files

    def _run(self, cmd: List[str], timeout: float = 20):
        try:
            return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=timeout, env=self.env)
        except (OSError, subprocess.SubprocessError):
            return None

    def query_default(self, mime: str) -> str:
        r = self._run(["xdg-mime", "query", "default", mime])
        return r.stdout.strip() if r is not None and r.returncode == 0 else ""

    def set_default(self, desktop_id: str, mime: str) -> bool:
        r = self._run(["xdg-mime", "default", desktop_id, mime])
        return r is not None and r.returncode == 0

    def refresh_database(self) -> None:
        if os.path.isdir(self.applications_dir):
            self._run(["update-desktop-database", self.applications_dir])


# ---- claims ----------------------------------------------------------------------------------

class ClaimStore:
    """claims.json: scheme -> the previous default handler, or "" if there was none."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(state_home(), "loginrelay", "claims.json")

    def load(self) -> Dict[str, str]:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}

    def save(self, claims: Dict[str, str]) -> None:
        if claims:
            _atomic_write(self.path, json.dumps(claims, indent=2, sort_keys=True))
        else:
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def set(self, scheme: str, previous: str) -> None:
        d = self.load()
        d[scheme] = previous
        self.save(d)

    def remove(self, scheme: str) -> None:
        d = self.load()
        if d.pop(scheme, None) is not None:
            self.save(d)


class Claim:
    """A scheme this app currently handles. release() puts the previous handler back."""

    def __init__(self, manager: "ClaimManager", scheme: str, previous: str):
        self.scheme = scheme
        self.previous = previous
        self._manager = manager
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self._manager.restore(self.scheme, self.previous)
        self._manager.store.remove(self.scheme)


class ClaimManager:
    def __init__(self, platform: Optional[Platform] = None, store: Optional[ClaimStore] = None,
                 launcher: Optional[List[str]] = None):
        self.platform = platform or Platform()
        self.store = store or ClaimStore()
        self._launcher = launcher

    @property
    def launcher(self) -> List[str]:
        return self._launcher or launcher_command()

    def _desktop_path(self, scheme: str) -> str:
        return os.path.join(self.platform.applications_dir, claim_desktop_id(scheme))

    def claim(self, scheme: str) -> Optional[Claim]:
        """Returns None if the system refuses, so the caller can fall back to pasting the address."""
        scheme = scheme.lower()
        if not SCHEME_RE.match(scheme):
            return None
        mime = "x-scheme-handler/" + scheme
        desktop_id = claim_desktop_id(scheme)
        current = self.platform.query_default(mime)
        if current == desktop_id:
            # A leftover claim from a crashed run isn't a "previous handler" worth restoring.
            previous = self.store.load().get(scheme, "")
        else:
            previous = current
        try:
            self.store.set(scheme, previous)  # recorded before anything changes
            _atomic_write(self._desktop_path(scheme), claim_desktop_text(scheme, self.launcher))
            self.platform.refresh_database()
            ok = self.platform.set_default(desktop_id, mime) and self.platform.query_default(mime) == desktop_id
        except OSError:
            ok = False
        if not ok:
            self.restore(scheme, previous)
            self.store.remove(scheme)
            return None
        return Claim(self, scheme, previous)

    def restore(self, scheme: str, previous: str) -> None:
        """Previous handler back if there was one, otherwise our line out of mimeapps.list; then our
        .desktop file goes away."""
        mime = "x-scheme-handler/" + scheme
        desktop_id = claim_desktop_id(scheme)
        if previous:
            self.platform.set_default(previous, mime)
        for path in self.platform.mimeapps_files():
            try:
                strip_file(path, mime, desktop_id)
            except OSError:
                pass
        try:
            os.unlink(self._desktop_path(scheme))
        except OSError:
            pass
        self.platform.refresh_database()

    def release_stale(self) -> List[str]:
        """Undoes claims left behind by a previous run that didn't get to clean up (at startup nothing
        is claimed by this process). Returns the schemes cleaned up."""
        recorded = self.store.load()
        schemes = set(recorded)
        try:
            for name in os.listdir(self.platform.applications_dir):
                if name.startswith(DESKTOP_PREFIX) and name.endswith(".desktop"):
                    schemes.add(name[len(DESKTOP_PREFIX):-len(".desktop")])
        except OSError:
            pass
        cleaned = []
        for scheme in sorted(schemes):
            if not SCHEME_RE.match(scheme):
                continue
            mime = "x-scheme-handler/" + scheme
            previous = recorded.get(scheme, "")
            if self.platform.query_default(mime) != claim_desktop_id(scheme):
                previous = ""  # someone else has taken over since; only remove our traces
            self.restore(scheme, previous)
            cleaned.append(scheme)
        self.store.save({})
        return cleaned


_default: Optional[ClaimManager] = None


def default_manager() -> ClaimManager:
    global _default
    if _default is None:
        _default = ClaimManager()
    return _default


# ---- launch at login -------------------------------------------------------------------------

def autostart_path(env=None) -> str:
    return os.path.join(config_home(env), "autostart", AUTOSTART_NAME)


def autostart_enabled(env=None) -> bool:
    try:
        with open(autostart_path(env), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    return not re.search(r"^(Hidden=true|X-GNOME-Autostart-enabled=false)\s*$", text, re.M)


def set_autostart(enabled: bool, launcher: Optional[List[str]] = None, env=None) -> None:
    path = autostart_path(env)
    if not enabled:
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    cmd = (launcher or launcher_command(env)) + ["--background"]
    _atomic_write(path, (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Login Relay\n"
        "Comment=Finishes browser sign-ins for Android phones that have no browser\n"
        "Exec=%s\n"
        "Icon=io.github.clippycoder.LoginRelay\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n" % exec_line(cmd)))
