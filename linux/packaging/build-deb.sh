#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Builds linux/dist/loginrelay_<version>_all.deb.
#
#   linux/packaging/build-deb.sh
#
# Environment:
#   LOGINRELAY_APK      the phone app to bundle (default: ../android/build/login-relay.apk)
#   PHONE_VERSION       its version (default: VERSION_NAME= in ../android/build.sh)
#   LOGINRELAY_NO_APK=1 build without the phone app
#   STAGE_ONLY=1        stop after staging the package tree and print its path (no dpkg-deb needed)
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

work="$(mktemp -d "${TMPDIR:-/tmp}/loginrelay-deb.XXXXXX")"
trap '[ "${STAGE_ONLY:-}" = 1 ] || rm -rf "$work"' EXIT
stage="$work/loginrelay_${VERSION}_all"

# The Python package
install -d -m 755 "$stage/usr/lib/loginrelay/loginrelay"
for f in "$linux"/loginrelay/*.py; do
  install -m 644 "$f" "$stage/usr/lib/loginrelay/loginrelay/$(basename "$f")"
done

# The launcher
install -d -m 755 "$stage/usr/bin"
cat > "$stage/usr/bin/loginrelay" <<'LAUNCHER'
#!/bin/sh
export PYTHONPATH="/usr/lib/loginrelay${PYTHONPATH:+:$PYTHONPATH}"
export LOGINRELAY_SHARE=/usr/share/loginrelay
export LOGINRELAY_LAUNCHER=/usr/bin/loginrelay
exec python3 -m loginrelay "$@"
LAUNCHER
chmod 755 "$stage/usr/bin/loginrelay"

# Menu entry, icons, tray icons, phone app
put 644 "$linux/data/io.github.clippycoder.LoginRelay.desktop" \
  "$stage/usr/share/applications/io.github.clippycoder.LoginRelay.desktop"
(cd "$linux/data/icons" && find hicolor -type f -name '*.png') | while read -r icon; do
  put 644 "$linux/data/icons/$icon" "$stage/usr/share/icons/$icon"
done
for f in "$linux"/data/tray/*.png; do
  put 644 "$f" "$stage/usr/share/loginrelay/tray/$(basename "$f")"
done
if [ "${LOGINRELAY_NO_APK:-}" != 1 ]; then
  put 644 "$APK" "$stage/usr/share/loginrelay/login-relay.apk"
  printf '%s\n' "$PHONE_VERSION" > "$stage/usr/share/loginrelay/phone-app-version.txt"
  chmod 644 "$stage/usr/share/loginrelay/phone-app-version.txt"
fi
put 644 "$root/LICENSE" "$stage/usr/share/doc/loginrelay/copyright"

# Control file
size="$(du -sk "$stage" | cut -f1)"
install -d -m 755 "$stage/DEBIAN"
cat > "$stage/DEBIAN/control" <<CONTROL
Package: loginrelay
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Installed-Size: $size
Maintainer: clippycoder <clippycoder@users.noreply.github.com>
Depends: python3 (>= 3.9), python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, adb | android-tools-adb
Recommends: xdg-utils
Homepage: https://github.com/clippycoder/oauth-relay
Description: Browser sign-ins for Android phones that have no browser
 Login Relay answers an Android app's request to open a sign-in page on a
 USB-connected phone by opening the page in this computer's browser, catching
 the redirect back to the app, and handing it to the phone with adb.
 .
 It runs as a tray icon. For the length of one sign-in it becomes the default
 handler for the app's return address (a custom URL scheme) and puts the
 previous handler back afterwards.
CONTROL

if [ "${STAGE_ONLY:-}" = 1 ]; then
  echo "$stage"
  exit 0
fi

out="$linux/dist"
mkdir -p "$out"
dpkg-deb --root-owner-group --build "$stage" "$out/loginrelay_${VERSION}_all.deb"
echo "built $out/loginrelay_${VERSION}_all.deb"
