#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render the launcher icon as PNGs (needs Pillow).

Android 8+ uses the vector adaptive icon in res/mipmap-anydpi-v26; these PNGs are for Android
5-7 and for listings (README, MDM console). The geometry mirrors
res/drawable/ic_launcher_foreground.xml, in the same 108-unit viewport.

    python3 android/tools/make_icons.py
"""
import os

from PIL import Image, ImageDraw

BRAND = (0x39, 0x49, 0xAB, 255)
WHITE = (255, 255, 255, 255)
SS = 8  # supersampling factor for smooth edges

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "..", "res")
DOCS = os.path.join(HERE, "..", "..", "docs")

LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def draw_foreground(d, tx):
    """tx maps a 108-viewport coordinate to pixels; s is pixels per unit."""
    s = tx(1) - tx(0)

    def stroke(points, width, color):
        pts = [(tx(x), tx(y)) for x, y in points]
        d.line(pts, fill=color, width=round(width * s), joint="curve")
        r = width * s / 2
        for x, y in pts:  # round caps and joins
            d.ellipse((x - r, y - r, x + r, y + r), fill=color)

    # shackle: two legs and the top half of a circle (centre 54,41 r 12), width 6
    w = 6
    stroke([(42, 50), (42, 41)], w, WHITE)
    stroke([(66, 41), (66, 50)], w, WHITE)
    r_out, r_in = 12 + w / 2, 12 - w / 2
    d.arc((tx(54 - r_out), tx(41 - r_out), tx(54 + r_out), tx(41 + r_out)),
          start=180, end=360, fill=WHITE, width=round((r_out - r_in) * s))
    # body
    d.rounded_rectangle((tx(34), tx(48), tx(74), tx(78)), radius=round(5 * s), fill=WHITE)
    # arrows
    aw = 3.2
    stroke([(44, 57.5), (63, 57.5)], aw, BRAND)
    stroke([(58.5, 53), (63, 57.5), (58.5, 62)], aw, BRAND)
    stroke([(64, 68.5), (45, 68.5)], aw, BRAND)
    stroke([(49.5, 64), (45, 68.5), (49.5, 73)], aw, BRAND)


def legacy_icon(size):
    """Round icon: a brand circle with the adaptive icon's central 72 units scaled to fit."""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    margin = big * 0.04
    d.ellipse((margin, margin, big - margin, big - margin), fill=BRAND)
    scale = (big - 2 * margin) / 72
    draw_foreground(d, lambda v: margin + (v - 18) * scale)
    return img.resize((size, size), Image.LANCZOS)


def listing_icon(size):
    """Square, full-bleed icon for listings: the central 84 units, so the lock reads larger."""
    big = size * SS
    img = Image.new("RGBA", (big, big), BRAND)
    d = ImageDraw.Draw(img)
    scale = big / 84
    draw_foreground(d, lambda v: (v - 12) * scale)
    return img.resize((size, size), Image.LANCZOS)


def main():
    for density, px in LEGACY.items():
        out = os.path.join(RES, f"mipmap-{density}")
        os.makedirs(out, exist_ok=True)
        legacy_icon(px).save(os.path.join(out, "ic_launcher.png"), optimize=True)
    os.makedirs(DOCS, exist_ok=True)
    listing_icon(512).save(os.path.join(DOCS, "icon-512.png"), optimize=True)
    print("wrote mipmap-*/ic_launcher.png and docs/icon-512.png")


if __name__ == "__main__":
    main()
