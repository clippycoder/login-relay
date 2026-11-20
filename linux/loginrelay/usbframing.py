# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure framing logic for the USB accessory link; no USB library, so the tests cover it anywhere.

Port of macos/Sources/UsbFraming.swift.
"""
from __future__ import annotations

from typing import List, Optional

# A line longer than this means the other side isn't speaking our protocol.
MAX_LINE = 1024 * 1024


def usb_frame(json_bytes: bytes) -> bytes:
    """The bytes to write for one JSON message: the JSON, a newline, and, if that length is an exact
    multiple of 64 (the full-speed bulk packet size; 512 and 1024 are multiples of it), one more
    newline. A bulk transfer that ends on a packet boundary has no short packet to mark its end, so
    the other side's read doesn't complete until more data arrives. The extra blank line is skipped
    by the reader."""
    data = bytes(json_bytes) + b"\n"
    if len(data) % 64 == 0:
        data += b"\n"
    return data


class LineSplitter:
    """Collects bytes and hands back complete, non-empty lines."""

    def __init__(self) -> None:
        self._pending = bytearray()

    def feed(self, data: bytes) -> Optional[List[bytes]]:
        """Returns the complete lines in `data` (plus anything held over), or None if the stream is
        broken (a line that never ends)."""
        lines: List[bytes] = []
        start = 0
        while True:
            end = data.find(b"\n", start)
            if end < 0:
                break
            self._pending += data[start:end]
            if len(self._pending) > MAX_LINE:
                return None
            if self._pending:
                lines.append(bytes(self._pending))
                self._pending = bytearray()
            start = end + 1
        self._pending += data[start:]
        if len(self._pending) > MAX_LINE:
            return None
        return lines
