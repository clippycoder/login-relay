#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Builds "Login Relay.app" (universal) and a zip, with the Command Line Tools only; no Xcode.
#
# Bundles ../android/build/login-relay.apk if it exists (run android/build.sh first), so the app
# can install Login Relay on the phone itself.
#
# Signing: CODESIGN_IDENTITY="Developer ID Application: …" for a real signature; otherwise ad-hoc,
# which macOS will warn about on first launch of a downloaded copy.
set -euo pipefail
cd "$(dirname "$0")"

VERSION=0.3.0
BUILD=1
APK=../android/build/login-relay.apk
PHONE_VERSION="$(sed -n 's/^VERSION_NAME=//p' ../android/build.sh)"
APP="build/Login Relay.app"

rm -rf build
mkdir -p build

for arch in arm64 x86_64; do
  swiftc -O -swift-version 5 -target "$arch-apple-macos13.0" -o "build/LoginRelay-$arch" Sources/*.swift
done
lipo -create -output build/LoginRelay build/LoginRelay-arm64 build/LoginRelay-x86_64

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp build/LoginRelay "$APP/Contents/MacOS/LoginRelay"
sed -e "s/@VERSION@/$VERSION/" -e "s/@BUILD@/$BUILD/" -e "s/@PHONE_VERSION@/$PHONE_VERSION/" \
  Info.plist.in > "$APP/Contents/Info.plist"
cp Resources/AppIcon.icns Resources/MenuBarIcon*.png "$APP/Contents/Resources/"
if [ -f "$APK" ]; then
  cp "$APK" "$APP/Contents/Resources/login-relay.apk"
else
  echo "note: $APK not found; the app will be built without the phone app inside" >&2
fi

codesign --force --options runtime --sign "${CODESIGN_IDENTITY:--}" "$APP"
codesign --verify --strict "$APP"

ZIP="build/Login-Relay-macOS-$VERSION.zip"
ditto -c -k --keepParent "$APP" "$ZIP"
echo "built $APP ($VERSION, phone app ${PHONE_VERSION})"
echo "      $ZIP"
