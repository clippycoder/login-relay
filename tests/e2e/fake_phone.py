#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in for the Login Relay phone app over USB accessory mode, for the end-to-end tests.

It listens on 127.0.0.1 and writes the port to <state>/phone_port. A desktop app started with
LOGINRELAY_FAKE_PHONE=127.0.0.1:<port> treats that socket as a plugged-in phone and runs its normal USB
logic over it (the same newline-delimited JSON as over the cable, minus the 64-byte padding rule).

Like the real phone it says hello on connect, answers "ping" with "pong", sends the pending page as
"open", and, when it gets a "callback", checks it against the page's return address and state before
answering "delivered" (or "error"). Every message it receives is appended to <state>/phone_msgs.jsonl
and every accepted callback to <state>/callbacks.jsonl.

  --url URL [--referrer R] [--label L]   a pending page, sent on every connection until "done"
  --cancel-after SECONDS                 after sending the page, send {"type":"cancel"}
  --timeout SECONDS                      exit after this long (default 120)
"""
import argparse
import json
import os
import socket
import threading
import time
import urllib.parse


def _params(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--url")
    ap.add_argument("--referrer", default="android-app://com.example.app")
    ap.add_argument("--label", default="Example App")
    ap.add_argument("--cancel-after", type=float)
    ap.add_argument("--timeout", type=float, default=120)
    a = ap.parse_args()

    pending = {"url": a.url}
    lock = threading.Lock()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    with open(os.path.join(a.state, "phone_port"), "w") as f:
        f.write(str(srv.getsockname()[1]))

    def record(name, msg):
        with open(os.path.join(a.state, name), "a", encoding="utf-8") as f:
            f.write(json.dumps(msg) + "\n")

    def send(conn, obj):
        try:
            conn.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass

    def vet(callback):
        """The phone's own check: same return address as the page asked for, and the same state."""
        with lock:
            url = pending["url"]
        if not url:
            return "No sign-in is waiting."
        q = _params(url)
        redirect = (q.get("redirect_uri") or q.get("returnTo") or [None])[0]
        if redirect is None:
            return "The page has no return address."
        got = urllib.parse.urlsplit(callback)
        want = urllib.parse.urlsplit(redirect)
        if (got.scheme.lower(), got.netloc, got.path) != (want.scheme.lower(), want.netloc, want.path):
            return "That isn't the return address the app asked for."
        state = (q.get("state") or [None])[0]
        if state is not None and (_params(callback).get("state") or [None])[0] != state:
            return "The state doesn't match."
        return None

    def serve(conn):
        with conn:
            send(conn, {"v": 1, "type": "hello", "transport": "usb", "version": "0.4.0"})
            with lock:
                url = pending["url"]
            if url:
                send(conn, {"v": 1, "type": "open", "url": url, "referrer": a.referrer, "label": a.label})
                if a.cancel_after is not None:
                    threading.Timer(a.cancel_after, lambda: send(conn, {"type": "cancel"})).start()
            for line in conn.makefile("r", encoding="utf-8"):
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                record("phone_msgs.jsonl", m)
                kind = m.get("type")
                if kind == "ping":
                    send(conn, {"type": "pong"})
                elif kind == "callback":
                    why = vet(m.get("url", ""))
                    if why is None:
                        record("callbacks.jsonl", m)
                        send(conn, {"type": "delivered"})
                    else:
                        send(conn, {"type": "error", "text": why})
                elif kind == "done":
                    with lock:
                        pending["url"] = None

    deadline = time.time() + a.timeout
    srv.settimeout(0.5)
    while time.time() < deadline:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
