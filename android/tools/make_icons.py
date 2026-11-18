#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render the launcher icon as PNGs (needs Pillow).

Android 8+ uses the vector adaptive icon in res/mipmap-anydpi-v26; these PNGs are for Android
5-7 and for listings (README, MDM console). The geometry mirrors
res/drawable/ic_launcher_foreground.xml, in the same 108-unit viewport.

    python3 android/tools/make_icons.py
"""
import os
import shutil
import subprocess

from PIL import Image, ImageChops, ImageDraw

BRAND = (0x39, 0x49, 0xAB, 255)
WHITE = (255, 255, 255, 255)
SS = 8  # supersampling factor for smooth edges

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "..", "res")
DOCS = os.path.join(HERE, "..", "..", "docs")
MAC = os.path.join(HERE, "..", "..", "macos", "Resources")

LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def draw_foreground(d, tx, body=WHITE, arrows=BRAND, shackle=WHITE):
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
    stroke([(42, 50), (42, 41)], w, shackle)
    stroke([(66, 41), (66, 50)], w, shackle)
    r_out, r_in = 12 + w / 2, 12 - w / 2
    d.arc((tx(54 - r_out), tx(41 - r_out), tx(54 + r_out), tx(41 + r_out)),
          start=180, end=360, fill=shackle, width=round((r_out - r_in) * s))
    # body
    d.rounded_rectangle((tx(34), tx(48), tx(74), tx(78)), radius=round(5 * s), fill=body)
    # arrows
    aw = 3.2
    stroke([(44, 57.5), (63, 57.5)], aw, arrows)
    stroke([(58.5, 53), (63, 57.5), (58.5, 62)], aw, arrows)
    stroke([(64, 68.5), (45, 68.5)], aw, arrows)
    stroke([(49.5, 64), (45, 68.5), (49.5, 73)], aw, arrows)


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


def mac_app_icon(size=1024):
    """macOS icon: rounded square on a transparent 1024 canvas (Apple's grid: 824 px body)."""
    big = size * 2
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    body = int(big * 824 / 1024)
    off = (big - body) // 2
    grad = Image.new("RGBA", (body, body))
    gd = ImageDraw.Draw(grad)
    top, bot = (0x4A, 0x5B, 0xD0), (0x2B, 0x37, 0x8C)
    for y in range(body):
        t = y / (body - 1)
        gd.line([(0, y), (body, y)], fill=tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (body, body), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, body - 1, body - 1), radius=round(body * 0.2237), fill=255)
    img.paste(grad, (off, off), mask)
    d = ImageDraw.Draw(img)
    scale = body / 72  # the central 72 units of the 108 canvas fill the body
    draw_foreground(d, lambda v: off + (v - 18) * scale)
    return img.resize((size, size), Image.LANCZOS)


def menubar_icon(px, active):
    """Black-on-transparent template glyph: lock with the arrows cut out. The active variant is
    a smaller lock tucked down-left with a dot badge in the free top-right corner."""
    big = px * 16
    mask = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(mask)
    scale = big / (78 if active else 66)
    tx = lambda v: big / 2 + (v - 54) * scale
    draw_foreground(d, tx, body=255, arrows=0, shackle=255)
    if active:
        mask = ImageChops.offset(mask, -round(big * 0.08), round(big * 0.08))
        d = ImageDraw.Draw(mask)
        bx, by, br = big * 0.83, big * 0.20, big * 0.15
        ring = br + big * 0.04
        d.ellipse((bx - ring, by - ring, bx + ring, by + ring), fill=0)
        d.ellipse((bx - br, by - br, bx + br, by + br), fill=255)
    mask = mask.resize((px, px), Image.LANCZOS)
    img = Image.new("RGBA", (px, px), (0, 0, 0, 255))
    img.putalpha(mask)
    return img


def main():
    for density, px in LEGACY.items():
        out = os.path.join(RES, f"mipmap-{density}")
        os.makedirs(out, exist_ok=True)
        legacy_icon(px).save(os.path.join(out, "ic_launcher.png"), optimize=True)
    os.makedirs(DOCS, exist_ok=True)
    listing_icon(512).save(os.path.join(DOCS, "icon-512.png"), optimize=True)
    os.makedirs(MAC, exist_ok=True)
    master = mac_app_icon(1024)
    iconset = os.path.join(MAC, "AppIcon.iconset")
    os.makedirs(iconset, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            master.resize((base * scale, base * scale), Image.LANCZOS).save(os.path.join(iconset, name))
    for active, tag in ((False, "MenuBarIcon"), (True, "MenuBarIconActive")):
        menubar_icon(22, active).save(os.path.join(MAC, f"{tag}.png"))
        menubar_icon(44, active).save(os.path.join(MAC, f"{tag}@2x.png"))
    if shutil.which("iconutil"):
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(MAC, "AppIcon.icns")], check=True)
        shutil.rmtree(iconset)
    print("wrote mipmap-*/ic_launcher.png, docs/icon-512.png and macos/Resources icons")


if __name__ == "__main__":
    main()
