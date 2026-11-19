#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Builds login-relay.apk with the bare SDK tools (aapt2, javac, d8, zipalign, apksigner); no Gradle.
#
# Signing: KEYSTORE (path) and KEYSTORE_PASS (password), else ../.signing/release.jks with
# ../.signing/password, else the SDK debug key (~/.android/debug.keystore) for local testing.
set -euo pipefail
cd "$(dirname "$0")"

VERSION_CODE=6
VERSION_NAME=0.3.1
MIN_SDK=22
TARGET_SDK=34

SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
BT="$(ls -d "$SDK"/build-tools/* | sort -V | tail -1)"
ANDROID_JAR="$(ls -d "$SDK"/platforms/android-* | sort -V | tail -1)/android.jar"
[ -f "$ANDROID_JAR" ] || { echo "no android.jar under $SDK/platforms" >&2; exit 1; }

rm -rf build
mkdir -p build/classes build/dex build/gen

"$BT/aapt2" compile --dir res -o build/res.zip
"$BT/aapt2" link -o build/res.apk -I "$ANDROID_JAR" --manifest AndroidManifest.xml \
  --min-sdk-version "$MIN_SDK" --target-sdk-version "$TARGET_SDK" \
  --version-code "$VERSION_CODE" --version-name "$VERSION_NAME" \
  --java build/gen build/res.zip

javac -source 8 -target 8 -bootclasspath "$ANDROID_JAR" -Xlint:-options \
  -d build/classes $(find src build/gen -name '*.java')

"$BT/d8" --release --min-api "$MIN_SDK" --lib "$ANDROID_JAR" --output build/dex \
  $(find build/classes -name '*.class')

cp build/res.apk build/unsigned.apk
(cd build/dex && zip -q ../unsigned.apk classes.dex)
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk

# A local release key in ../.signing (gitignored) is used automatically when present.
if [ -z "${KEYSTORE:-}" ] && [ -f ../.signing/release.jks ]; then
  KEYSTORE=../.signing/release.jks
  KEYSTORE_PASS="$(cat ../.signing/password)"
  export KEYSTORE_PASS
fi

if [ -n "${KEYSTORE:-}" ]; then
  "$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass env:KEYSTORE_PASS \
    --out build/login-relay.apk build/aligned.apk
else
  "$BT/apksigner" sign --ks "$HOME/.android/debug.keystore" --ks-pass pass:android \
    --out build/login-relay.apk build/aligned.apk
fi
"$BT/apksigner" verify build/login-relay.apk
echo "built android/build/login-relay.apk ($VERSION_NAME)"
