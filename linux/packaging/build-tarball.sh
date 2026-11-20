#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Builds linux/dist/loginrelay-<version>-linux.tar.gz: a portable copy that runs in place
# (./loginrelay) or installs per user under ~/.local (./install.sh).
#
# Environment: LOGINRELAY_APK, PHONE_VERSION, LOGINRELAY_NO_APK=1 as for build-deb.sh.
set -euo pipefail

# install -D is GNU-only; this works with BSD install too.
put() { mkdir -p "$(dirname "$3")" && install -m "$1" "$2" "$3"; }

here="$(cd "$(dirname "$0")" && pwd)"
linux="$(cd "$here/.." && pwd)"
root="$(cd "$linux/.." && pwd)"

VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$linux/loginrelay/__init__.py")"
[ -n "$VERSION" ] || { echo "can't read VERSION from loginrelay/__init__.py" >&2; exit 1; }
APK="${LOGINRELAY_APK:-$root/android/build/login-relay.apk}"
PHONE_VERSION="${PHONE_VERSION:-$(sed -n 's/^VERSION_NAME=//p' "$root/android/build.sh" 2>/dev/null | head -n1 | tr -d "\"'")}"

if [ "${LOGINRELAY_NO_APK:-}" != 1 ]; then
  [ -f "$APK" ] || { echo "phone app not found at $APK (build it with android/build.sh, set LOGINRELAY_APK, or set LOGINRELAY_NO_APK=1)" >&2; exit 1; }
  [ -n "$PHONE_VERSION" ] || { echo "can't work out the phone app's version; set PHONE_VERSION" >&2; exit 1; }
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/loginrelay-tar.XXXXXX")"
trap 'rm -rf "$work"' EXIT
name="loginrelay-${VERSION}-linux"
stage="$work/$name"

install -d -m 755 "$stage/lib/loginrelay" "$stage/data/tray"
for f in "$linux"/loginrelay/*.py; do
  install -m 644 "$f" "$stage/lib/loginrelay/$(basename "$f")"
done
(cd "$linux/data/icons" && find hicolor -type f -name '*.png') | while read -r icon; do
  put 644 "$linux/data/icons/$icon" "$stage/data/icons/$icon"
done
for f in "$linux"/data/tray/*.png; do
  install -m 644 "$f" "$stage/data/tray/$(basename "$f")"
done
install -m 644 "$linux/data/io.github.clippycoder.LoginRelay.desktop" "$stage/data/"
install -m 644 "$here/70-loginrelay.rules" "$stage/data/70-loginrelay.rules"
if [ "${LOGINRELAY_NO_APK:-}" != 1 ]; then
  install -m 644 "$APK" "$stage/data/login-relay.apk"
  printf '%s\n' "$PHONE_VERSION" > "$stage/data/phone-app-version.txt"
fi
install -m 644 "$root/LICENSE" "$stage/LICENSE"

# Runs in place from wherever the archive was unpacked.
cat > "$stage/loginrelay" <<'LAUNCHER'
#!/bin/sh
self="$0"
if command -v readlink >/dev/null 2>&1; then
  resolved="$(readlink -f "$self" 2>/dev/null)" && [ -n "$resolved" ] && self="$resolved"
fi
here="$(cd "$(dirname "$self")" && pwd)"
export PYTHONPATH="$here/lib${PYTHONPATH:+:$PYTHONPATH}"
export LOGINRELAY_SHARE="$here/data"
export LOGINRELAY_LAUNCHER="$here/loginrelay"
exec python3 -m loginrelay "$@"
LAUNCHER
chmod 755 "$stage/loginrelay"

# Per-user install under ~/.local (or $PREFIX)
cat > "$stage/install.sh" <<'INSTALL'
#!/usr/bin/env bash
# Installs Login Relay for the current user:  ./install.sh   (remove again with ./install.sh --uninstall)
# PREFIX defaults to ~/.local.
set -euo pipefail

src="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
lib="$PREFIX/lib/loginrelay"
share="$PREFIX/share/loginrelay"
bin="$PREFIX/bin/loginrelay"
apps="$PREFIX/share/applications"
icons="$PREFIX/share/icons"
desktop="$apps/io.github.clippycoder.LoginRelay.desktop"

refresh() {
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$apps" 2>/dev/null || true
  command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -f -t "$icons/hicolor" 2>/dev/null || true
}

if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$lib" "$share"
  rm -f "$bin" "$desktop"
  find "$icons/hicolor" -name 'io.github.clippycoder.LoginRelay.png' -delete 2>/dev/null || true
  refresh
  echo "Login Relay removed from $PREFIX."
  [ -f /etc/udev/rules.d/70-loginrelay.rules ] \
    && echo "The udev rule is still installed; remove it with: sudo rm /etc/udev/rules.d/70-loginrelay.rules"
  exit 0
fi

case "$PREFIX" in
  *[!A-Za-z0-9/._+@%-]*) echo "PREFIX contains characters this installer doesn't handle: $PREFIX" >&2; exit 1 ;;
esac

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || { echo "Login Relay needs Python 3.9 or newer." >&2; exit 1; }
python3 -c 'import gi' 2>/dev/null \
  || echo "note: PyGObject isn't installed (Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1)" >&2
python3 -c 'import usb.core' 2>/dev/null \
  || echo "note: pyusb isn't installed, so Login Relay won't be able to reach the phone (Debian/Ubuntu: sudo apt install python3-usb)." >&2

rm -rf "$lib" "$share"
mkdir -p "$lib/loginrelay" "$share" "$(dirname "$bin")" "$apps"
cp "$src"/lib/loginrelay/*.py "$lib/loginrelay/"
cp -R "$src/data/tray" "$share/tray"
[ -f "$src/data/login-relay.apk" ] && cp "$src/data/login-relay.apk" "$share/login-relay.apk"
[ -f "$src/data/phone-app-version.txt" ] && cp "$src/data/phone-app-version.txt" "$share/phone-app-version.txt"
cp "$src/data/70-loginrelay.rules" "$share/70-loginrelay.rules"
mkdir -p "$icons"
cp -R "$src/data/icons/hicolor" "$icons/"

q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
{
  echo '#!/bin/sh'
  echo "export PYTHONPATH=$(q "$lib")\${PYTHONPATH:+:\$PYTHONPATH}"
  echo "export LOGINRELAY_SHARE=$(q "$share")"
  echo "export LOGINRELAY_LAUNCHER=$(q "$bin")"
  echo 'exec python3 -m loginrelay "$@"'
} > "$bin"
chmod 755 "$bin"

while IFS= read -r line; do
  case "$line" in
    Exec=*) echo "Exec=$bin" ;;
    *) echo "$line" ;;
  esac
done < "$src/data/io.github.clippycoder.LoginRelay.desktop" > "$desktop"
chmod 644 "$desktop"
refresh

echo "Installed to $PREFIX. Start it from your applications menu or run: $bin"
echo
echo "Login Relay talks to the phone over a plain USB cable (USB accessory mode, no USB debugging) and needs"
echo "access to the phone's USB device. A per-user install can't grant that; without it Login Relay reports"
echo "a permission error. To allow it, run these two commands once, as root:"
echo
echo "  sudo install -m 644 $(q "$share/70-loginrelay.rules") /etc/udev/rules.d/70-loginrelay.rules"
echo "  sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb"
echo
echo "Then unplug and replug the phone. (Undo with: sudo rm /etc/udev/rules.d/70-loginrelay.rules)"
case ":$PATH:" in *":$PREFIX/bin:"*) ;; *) echo "($PREFIX/bin isn't on your PATH.)" ;; esac
INSTALL
chmod 755 "$stage/install.sh"

out="$linux/dist"
mkdir -p "$out"
if tar --version 2>/dev/null | grep -qi gnu; then
  owner=(--owner=0 --group=0 --numeric-owner)
else
  owner=(--uid 0 --gid 0 --uname root --gname root)
fi
tar -C "$work" "${owner[@]}" -czf "$out/$name.tar.gz" "$name"
echo "built $out/$name.tar.gz"
