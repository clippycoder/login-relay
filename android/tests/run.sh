#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Runs tests/vectors.json through the phone app's Redirect class on a plain JVM (needs a JDK).
set -euo pipefail
cd "$(dirname "$0")"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
python3 gen_vectors.py "$out/vectors.tsv"
javac -d "$out" ../src/io/github/clippycoder/oauthrelay/Redirect.java io/github/clippycoder/oauthrelay/RedirectTest.java
java -cp "$out" io.github.clippycoder.oauthrelay.RedirectTest "$out/vectors.tsv"
