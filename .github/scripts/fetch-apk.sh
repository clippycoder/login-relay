#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Downloads the phone app from a GitHub release into ./apk and exports where it is.
#
# The APK is signed with a release key that lives only on the maintainer's machine, so CI doesn't
# build it; it bundles the one attached to a release. With no tag, the newest release (including
# pre-releases) is used.
set -euo pipefail
tag="${1:-}"
if [ -z "$tag" ]; then
  tag="$(gh release list --repo "$GITHUB_REPOSITORY" --limit 1 --json tagName -q '.[0].tagName')"
fi
[ -n "$tag" ] || { echo "no release to take the phone app from" >&2; exit 1; }
rm -rf apk && mkdir -p apk
gh release download "$tag" --repo "$GITHUB_REPOSITORY" --pattern 'login-relay-*.apk' --dir apk
apk="$(ls apk/login-relay-*.apk | head -1)"
version="$(basename "$apk" .apk)"; version="${version#login-relay-}"
echo "phone app $version from release $tag"
{
  echo "LOGINRELAY_APK=$(pwd)/$apk"
  echo "PHONE_VERSION=$version"
  echo "RELEASE_TAG_USED=$tag"
} >> "$GITHUB_ENV"
