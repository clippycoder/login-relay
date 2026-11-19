#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Login Relay host launcher: fetches host/oauth_relay.py and runs it.
#
#   curl -fsSL https://raw.githubusercontent.com/clippycoder/login-relay/main/relay.sh | sh
#
# Pass options after `sh -s --`, e.g.  ... | sh -s -- --serial 1234ABCD
#
# Everything runs inside main(), called on the last line, so a truncated download runs nothing.

main() {
    set -eu
    repo=clippycoder/login-relay
    path=host/oauth_relay.py

    if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys' >/dev/null 2>&1; then
        echo "Login Relay needs python3. On macOS run: xcode-select --install" >&2
        exit 1
    fi

    tmp=$(mktemp -d "${TMPDIR:-/tmp}/login-relay.XXXXXX")
    trap 'rm -rf "$tmp"' EXIT
    trap 'exit 130' INT TERM

    if ! curl -fsSL "https://raw.githubusercontent.com/$repo/main/$path" -o "$tmp/oauth_relay.py"; then
        echo "Could not download $path from github.com/$repo." >&2
        exit 1
    fi

    # Our stdin is the piped script, so give the helper the terminal for its prompts.
    if [ -r /dev/tty ] && (: </dev/tty) 2>/dev/null; then
        python3 "$tmp/oauth_relay.py" "$@" </dev/tty
    else
        python3 "$tmp/oauth_relay.py" "$@"
    fi
}

main "$@"
