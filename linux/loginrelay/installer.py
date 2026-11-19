# SPDX-License-Identifier: GPL-3.0-or-later
"""Downloads Google's Android platform-tools (which contains adb) for people who don't have it.

It comes straight from dl.google.com and is checked against the SHA-1 in Google's own repository
manifest. Nothing is redistributed by this project.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from typing import Callable, Optional, Tuple

from .prefs import data_home

MANIFEST_URL = "https://dl.google.com/android/repository/repository2-3.xml"
ARCHIVE_BASE = "https://dl.google.com/android/repository/"
LICENSE_URL = "https://developer.android.com/studio/terms"


class InstallError(Exception):
    pass


def install_dir(env=None) -> str:
    return os.path.join(data_home(env), "loginrelay")


def installed_path(env=None) -> str:
    return os.path.join(install_dir(env), "platform-tools", "adb")


def linux_archive(xml: str) -> Tuple[str, str]:
    """Pulls the Linux archive's checksum and file name out of the platform-tools entry."""
    start = xml.find('<remotePackage path="platform-tools"')
    end = xml.find("</remotePackage>", start) if start >= 0 else -1
    if start < 0 or end < 0:
        raise InstallError("Google's download list has no platform-tools entry.")
    block = xml[start:end]
    m = re.search(r'<checksum type="sha1">([0-9a-f]{40})</checksum>\s*<url>([^<]*linux[^<]*)</url>', block)
    if not m:
        raise InstallError("Google's download list has no Linux platform-tools.")
    return m.group(1), m.group(2).strip()


def fetch(url: str, timeout: float) -> bytes:
    if not url.startswith("https://"):
        raise InstallError("Refusing a non-HTTPS download.")
    req = urllib.request.Request(url, headers={"User-Agent": "loginrelay"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise InstallError("%s answered %s." % (url.split("/")[2], resp.status))
            return resp.read()
    except InstallError:
        raise
    except Exception as e:  # URLError, timeouts, ...
        raise InstallError(str(e))


def _unpack(zip_path: str, into: str) -> None:
    root = os.path.realpath(into)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = os.path.realpath(os.path.join(into, info.filename))
            if target != root and not target.startswith(root + os.sep):
                raise InstallError("The download contained an unsafe path.")
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(target, mode | stat.S_IRUSR | stat.S_IWUSR)


def install(progress: Callable[[str], None], fetcher: Callable[[str, float], bytes] = fetch,
            dest: Optional[str] = None) -> None:
    """Blocking; call off the UI thread. `dest` defaults to the app's own data folder."""
    dest = dest or install_dir()
    progress("Reading Google's download list…")
    manifest = fetcher(MANIFEST_URL, 60).decode("utf-8", "replace")
    sha1, name = linux_archive(manifest)
    if "/" in name or ".." in name:
        raise InstallError("Google's download list named an unexpected file.")

    progress("Downloading platform-tools…")
    blob = fetcher(ARCHIVE_BASE + name, 300)
    if hashlib.sha1(blob).hexdigest() != sha1:
        raise InstallError("The download didn't match Google's checksum, so it was discarded.")

    progress("Unpacking…")
    os.makedirs(dest, exist_ok=True)
    staging = tempfile.mkdtemp(prefix="loginrelay-pt-")
    try:
        zip_file = os.path.join(staging, "pt.zip")
        with open(zip_file, "wb") as f:
            f.write(blob)
        out = os.path.join(staging, "out")
        os.makedirs(out)
        try:
            _unpack(zip_file, out)
        except zipfile.BadZipFile as e:
            raise InstallError("Couldn't unpack the download: %s" % e)
        fresh = os.path.join(out, "platform-tools")
        adb = os.path.join(fresh, "adb")
        if not (os.path.isfile(adb) and os.access(adb, os.X_OK)):
            raise InstallError("The download didn't contain adb.")
        target = os.path.join(dest, "platform-tools")
        if os.path.exists(target):
            shutil.rmtree(target)
        shutil.move(fresh, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    progress("Installed.")
