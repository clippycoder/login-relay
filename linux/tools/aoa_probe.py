#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Command-line test rig for the USB accessory layer (loginrelay/usbaoa.py). Not shipped in the package.
Port of macos/Tools/aoa_probe.c; needs pyusb (Debian/Ubuntu: python3-usb; elsewhere: pip install pyusb)
and, on Linux, permission to use the phone (the udev rule in packaging/, or sudo).

  aoa_probe.py list                 show USB devices, marking likely Android phones and accessory mode
  aoa_probe.py run [location]       switch a phone into accessory mode, open it, and talk to Login Relay:
                                    prints every line the phone sends, pings it every 2 s, and sends
                                    what you type: "callback <url>", "done", or any raw JSON line.

A location is the port path shown by `list` (for example 1-4.2). Run it from the linux/ folder:
  python3 tools/aoa_probe.py list
"""
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loginrelay import usbaoa  # noqa: E402
from loginrelay.usbframing import LineSplitter, usb_frame  # noqa: E402

T0 = time.monotonic()


def log(text):
    print("[%6.2fs] %s" % (time.monotonic() - T0, text), flush=True)


def cmd_list(backend):
    devices = backend.list_devices()
    print("%-10s %-10s %-9s %-4s %s" % ("location", "vid:pid", "android?", "aoa", "name / serial"))
    for d in devices:
        tail = d.name + (" / " + d.serial if d.serial else "")
        print("%-10s %04x:%04x   %-9s %-4s %s" % (d.location, d.vendor_id, d.product_id,
                                                   "likely" if d.looks_android else "-",
                                                   "yes" if d.is_accessory else "-", tail))
    return 0


def find_phone(backend, wanted):
    devices = backend.list_devices()
    if wanted:
        return [d for d in devices if d.location == wanted]
    return [d for d in devices if d.looks_android]


def send_line(conn, text):
    data = usb_frame(text.encode("utf-8"))
    for i in range(0, len(data), usbaoa.WRITE_CHUNK):
        try:
            n = conn.write(data[i:i + usbaoa.WRITE_CHUNK], 1000)
        except usbaoa.UsbError as e:
            log("write failed: %s (code %d)" % (e.detail, e.code))
            return
        if n <= 0:
            log("write timed out (the phone isn't reading)")
            return


def stdin_lines(out):
    for line in sys.stdin:
        out.put(line.rstrip("\r\n"))


def cmd_run(backend, wanted):
    try:
        matches = find_phone(backend, wanted)
    except usbaoa.UsbError as e:
        log(e.detail or "USB error %d" % e.code)
        return 1
    if not matches:
        log("No candidate phone. Plug it in and pick \"File transfer\" in its USB notification, or run `list`.")
        return 1
    if len(matches) > 1:
        log("%d candidates; give a location from `list`." % len(matches))
        return 1
    phone = matches[0]
    log("Phone: %s  %04x:%04x at %s%s" % (phone.name, phone.vendor_id, phone.product_id, phone.location,
                                            " (already in accessory mode)" if phone.is_accessory else ""))

    if not phone.is_accessory:
        host = usbaoa.local_host_name()
        started = time.monotonic()
        r = backend.switch(phone.location, host)
        log("switch -> %d (AOA protocol %d%s) in %.2fs"
            % (r.code, r.protocol_version, ", " + r.detail if r.detail else "", time.monotonic() - started))
        if r.code != usbaoa.AOA_OK:
            log(usbaoa.error_text(r.code, phone.name, r.detail))
            return 1
        appeared = False
        for _ in range(60):     # up to 12 s: Android gives up after 10
            time.sleep(0.2)
            try:
                now = find_phone(backend, phone.location)
            except usbaoa.UsbError:
                continue
            if now and now[0].is_accessory:
                log("accessory device appeared: %04x:%04x after %.2fs"
                    % (now[0].vendor_id, now[0].product_id, time.monotonic() - started))
                appeared = True
                break
        if not appeared:
            log("the phone never reappeared as an accessory")
            return 1

    conn = None
    for attempt in range(6):
        try:
            conn = backend.open(phone.location)
            break
        except usbaoa.UsbError as e:
            log("open attempt %d failed (%d): %s" % (attempt + 1, e.code, e.detail))
            if e.code == usbaoa.AOA_ERR_PERMISSION:
                log(usbaoa.NO_PERMISSION)
                return 1
            time.sleep(0.5)
    if conn is None:
        return 1
    log("opened. Waiting for Login Relay on the phone (it launches by itself; the first time Android asks to allow it).")
    log("type: callback <url> | done | <raw json>")

    typed = queue.Queue()
    threading.Thread(target=stdin_lines, args=(typed,), daemon=True).start()
    splitter = LineSplitter()
    last_ping = 0.0
    try:
        while True:
            try:
                data = conn.read(usbaoa.READ_BUFFER, usbaoa.READ_TIMEOUT_MS)
            except usbaoa.UsbError as e:
                log("read error %d (%s): the phone was unplugged or the link broke" % (e.code, e.detail))
                break
            if data:
                lines = splitter.feed(data)
                if lines is None:
                    log("the phone sent a line that never ends; giving up")
                    break
                for line in lines:
                    log("phone: " + line.decode("utf-8", "replace"))
            if time.monotonic() - last_ping > 2:
                send_line(conn, '{"type":"ping"}')
                last_ping = time.monotonic()
            while True:
                try:
                    text = typed.get_nowait()
                except queue.Empty:
                    break
                if text.startswith("callback "):
                    send_line(conn, json.dumps({"type": "callback", "url": text[9:]}, separators=(",", ":")))
                elif text == "done":
                    send_line(conn, '{"type":"done"}')
                elif text:
                    send_line(conn, text)
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        conn.close()
    return 0


def main(argv):
    backend = usbaoa.default_backend()
    try:
        if len(argv) > 1 and argv[1] == "list":
            return cmd_list(backend)
        if len(argv) > 1 and argv[1] == "run":
            return cmd_run(backend, argv[2] if len(argv) > 2 else None)
    except usbaoa.UsbError as e:
        sys.stderr.write("%s\n" % (e.detail or "USB error %d" % e.code))
        return 1
    sys.stderr.write("usage: aoa_probe.py list | run [location]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
