# SPDX-License-Identifier: GPL-3.0-or-later
"""The USB accessory framing (port of the USB framing checks in macos/Tests/main.swift)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loginrelay.usbframing import MAX_LINE, LineSplitter, usb_frame  # noqa: E402


def frame(n):
    return usb_frame(b"A" * n)


class FrameTests(unittest.TestCase):
    def test_adds_a_newline(self):
        self.assertEqual(len(frame(10)), 11)
        self.assertTrue(frame(10).endswith(b"\n"))

    def test_pads_when_the_transfer_would_end_on_a_packet_boundary(self):
        self.assertEqual(len(frame(63)), 65)        # 63 + newline = 64 bytes needs a pad
        self.assertEqual(frame(63)[63:], b"\n\n")
        self.assertEqual(len(frame(511)), 513)      # 512
        self.assertEqual(len(frame(1023)), 1025)    # 1024
        self.assertEqual(len(frame(16383)), 16385)

    def test_no_padding_otherwise(self):
        self.assertEqual(len(frame(64)), 65)        # 64 + newline = 65: not a multiple of 64
        self.assertEqual(frame(64)[64], 0x0A)
        self.assertEqual(len(frame(62)), 63)
        self.assertEqual(len(frame(0)), 1)

    def test_a_framed_message_never_has_a_length_multiple_of_64(self):
        for n in range(0, 2200):
            self.assertNotEqual(len(frame(n)) % 64, 0, n)

    def test_accepts_bytes_like_input(self):
        self.assertEqual(usb_frame(bytearray(b"{}")), b"{}\n")


class SplitterTests(unittest.TestCase):
    def test_complete_lines_come_out_and_the_rest_is_held(self):
        s = LineSplitter()
        self.assertEqual(s.feed(b'{"a":1}\n{"b"'), [b'{"a":1}'])
        self.assertEqual(s.feed(b':2}\n\n\n{"c":3}\n'), [b'{"b":2}', b'{"c":3}'])

    def test_blank_lines_are_skipped(self):
        s = LineSplitter()
        self.assertEqual(s.feed(b"\n\n\n"), [])
        self.assertEqual(s.feed(b"x\n\n"), [b"x"])

    def test_a_padded_frame_yields_one_line(self):
        s = LineSplitter()
        self.assertEqual(s.feed(frame(63)), [b"A" * 63])

    def test_one_byte_at_a_time(self):
        s = LineSplitter()
        out = []
        for b in b'{"x":1}\n{"y":2}\n':
            out += s.feed(bytes([b]))
        self.assertEqual(out, [b'{"x":1}', b'{"y":2}'])

    def test_a_line_that_never_ends_breaks_the_stream(self):
        s = LineSplitter()
        self.assertIsNone(s.feed(b"A" * (MAX_LINE + 1)))

    def test_a_line_of_exactly_the_maximum_is_fine(self):
        s = LineSplitter()
        self.assertEqual(s.feed(b"A" * MAX_LINE + b"\n"), [b"A" * MAX_LINE])

    def test_a_long_line_arriving_in_pieces_is_rejected_too(self):
        s = LineSplitter()
        self.assertEqual(s.feed(b"A" * MAX_LINE), [])
        self.assertIsNone(s.feed(b"A"))


if __name__ == "__main__":
    unittest.main()
