#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Builds login-relay.apk with the bare SDK tools (aapt2, javac, d8, zipalign, apksigner); no Gradle.
#
# Signing: set KEYSTORE (path) and KEYSTORE_PASS (password). Without them the APK is signed with
# the SDK debug key (~/.android/debug.keystore), which is fine for local testing.
set -euo pipefail
cd "$(dirname "$0")"

VERSION_CODE=1
VERSION_NAME=0.1.0
MIN_SDK=21
TARGET_SDK=30

SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
BT="$(ls -d "$SDK"/build-tools/* | sort -V | tail -1)"
ANDROID_JAR="$(ls -d "$SDK"/platforms/android-* | sort -V | tail -1)/android.jar"
[ -f "$ANDROID_JAR" ] || { echo "no android.jar under $SDK/platforms" >&2; exit 1; }

rm -rf build
mkdir -p build/classes build/dex

"$BT/aapt2" link -o build/res.apk -I "$ANDROID_JAR" --manifest AndroidManifest.xml \
  --min-sdk-version "$MIN_SDK" --target-sdk-version "$TARGET_SDK" \
  --version-code "$VERSION_CODE" --version-name "$VERSION_NAME"

javac -source 8 -target 8 -bootclasspath "$ANDROID_JAR" -Xlint:-options \
  -d build/classes $(find src -name '*.java')

"$BT/d8" --release --min-api "$MIN_SDK" --lib "$ANDROID_JAR" --output build/dex \
  $(find build/classes -name '*.class')

cp build/res.apk build/unsigned.apk
(cd build/dex && zip -q ../unsigned.apk classes.dex)
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk

if [ -n "${KEYSTORE:-}" ]; then
  "$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass env:KEYSTORE_PASS \
    --out build/login-relay.apk build/aligned.apk
else
  "$BT/apksigner" sign --ks "$HOME/.android/debug.keystore" --ks-pass pass:android \
    --out build/login-relay.apk build/aligned.apk
fi
"$BT/apksigner" verify build/login-relay.apk
echo "built android/build/login-relay.apk ($VERSION_NAME)"
