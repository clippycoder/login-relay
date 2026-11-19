#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in for the Login Relay phone app's socket, for the end-to-end tests.

Listens on 127.0.0.1 (the port goes to <state>/phone_port, where fake_adb.py reads it) and speaks
the line-delimited JSON protocol from the README. Every message it receives is appended to
<state>/phone_msgs.jsonl.

  --url URL [--referrer R] [--label L]   a pending page, sent on every connection until "done"
  --idle                                 emulate "Login Relay isn't open": accept, then hang up
  --cancel-after SECONDS                 after sending the page, send {"type":"cancel"}
  --timeout SECONDS                      exit after this long (default 120)
"""
import argparse
import json
import os
import socket
import threading
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--url")
    ap.add_argument("--referrer", default="android-app://com.example.app")
    ap.add_argument("--label", default="Example App")
    ap.add_argument("--idle", action="store_true")
    ap.add_argument("--cancel-after", type=float)
    ap.add_argument("--timeout", type=float, default=120)
    a = ap.parse_args()

    pending = {"url": a.url}
    lock = threading.Lock()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    with open(os.path.join(a.state, "phone_port"), "w") as f:
        f.write(str(port))

    def log(msg):
        with open(os.path.join(a.state, "phone_msgs.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(msg) + "\n")

    def serve(conn):
        with conn:
            if a.idle:
                return
            with lock:
                url = pending["url"]
            if url:
                msg = {"v": 1, "type": "open", "url": url, "referrer": a.referrer, "label": a.label}
                conn.sendall((json.dumps(msg) + "\n").encode())
                if a.cancel_after is not None:
                    threading.Timer(a.cancel_after, lambda: _send(conn, {"type": "cancel"})).start()
            for line in conn.makefile("r", encoding="utf-8"):
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                log(m)
                if m.get("type") == "done":
                    with lock:
                        pending["url"] = None

    def _send(conn, obj):
        try:
            conn.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass

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
