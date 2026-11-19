#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in for `adb`, for the end-to-end tests. Needs FAKE_ADB_STATE (a directory).

Understands exactly what the Login Relay desktop apps run:
  devices [-l]                       one phone, FAKE0001 (or the raw lines in <state>/devices.txt)
  [-s SERIAL] forward tcp:0 localabstract:oauthrelay   prints the port in <state>/phone_port
  forward --list / forward --remove tcp:N
  [-s SERIAL] shell <command>        dumpsys package / pm path / am start / anything else
  [-s SERIAL] install [-r] <apk>     records it and bumps the phone app to FAKE_ADB_INSTALL_VERSION
  version, start-server, kill-server, reconnect

`shell` arguments are joined with spaces and split like a POSIX shell would split them on the
phone, so a wrongly quoted return address shows up as a wrong URL in <state>/am_calls.jsonl.
Every invocation is appended to <state>/adb_calls.jsonl.
"""
import json
import os
import shlex
import sys

STATE = os.environ.get("FAKE_ADB_STATE")
PKG = "io.github.clippycoder.oauthrelay"
if not STATE:
    sys.exit("fake adb: FAKE_ADB_STATE is not set")


def path(name):
    return os.path.join(STATE, name)


def read(name, default=""):
    try:
        with open(path(name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default


def append(name, obj):
    with open(path(name), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def main():
    argv = sys.argv[1:]
    append("adb_calls.jsonl", {"argv": argv})
    serial = "FAKE0001"
    if argv[:1] == ["-s"] and len(argv) >= 2:
        serial = argv[1]
        argv = argv[2:]
    cmd = argv[0] if argv else ""
    rest = argv[1:]

    if cmd == "version":
        print("Android Debug Bridge version 1.0.41\nVersion 99.0.0-fake")
    elif cmd in ("start-server", "kill-server", "reconnect"):
        pass
    elif cmd == "devices":
        print("List of devices attached")
        raw = read("devices.txt", None)
        if raw is None:
            print(f"{serial}\tdevice usb:1-1 product:fake model:Fake_Phone device:fake transport_id:1")
        elif raw:
            print(raw)
    elif cmd == "forward":
        if rest[:1] == ["--list"]:
            port = read("phone_port")
            if port:
                print(f"{serial} tcp:{port} localabstract:oauthrelay")
        elif rest[:1] == ["--remove"]:
            pass
        else:
            port = read("phone_port")
            if not port:
                print("adb: error: fake phone isn't listening", file=sys.stderr)
                sys.exit(1)
            print(port)
    elif cmd == "install":
        apk = rest[-1] if rest else ""
        append("installs.jsonl", {"apk": apk, "exists": os.path.exists(apk)})
        with open(path("phone_version"), "w", encoding="utf-8") as f:
            f.write(os.environ.get("FAKE_ADB_INSTALL_VERSION", "0.3.0"))
        print("Performing Streamed Install\nSuccess")
    elif cmd == "shell":
        remote = " ".join(rest)
        append("shell_calls.jsonl", {"command": remote})
        try:
            words = shlex.split(remote)
        except ValueError as e:
            print(f"sh: syntax error: {e}", file=sys.stderr)
            sys.exit(2)
        if words[:2] == ["dumpsys", "package"] or words[:2] == ["pm", "path"]:
            version = read("phone_version", "0.3.0")
            if os.path.exists(path("no_app")) or not version:
                print(f"Unable to find package: {PKG}")
            elif words[0] == "pm":
                print(f"package:/data/app/{PKG}/base.apk")
            else:
                print(f"Packages:\n  Package [{PKG}] (abc):\n    versionName={version}")
        elif words[:2] == ["am", "start"]:
            call = {"words": words}
            for flag in ("-d", "-p", "-a", "-n"):
                if flag in words:
                    call[flag] = words[words.index(flag) + 1]
            append("am_calls.jsonl", call)
            print("Starting: Intent { ... }")
    else:
        print(f"fake adb: unsupported command {cmd!r}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
