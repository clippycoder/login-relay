#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Compiles the pure-logic sources with the tests and runs them. No Xcode needed.
set -euo pipefail
cd "$(dirname "$0")"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
swiftc -swift-version 5 -o "$out/tests" ../Sources/Redirect.swift ../Sources/UsbFraming.swift main.swift
"$out/tests" ../../tests/vectors.json
