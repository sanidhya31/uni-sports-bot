"""Generate a 512x512 PNG avatar for the bot (upload via BotFather /setuserpic).

A dark emblem with a shuttlecock (badminton) + speed streaks, on-brand green.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
OUT = Path(__file__).resolve().parents[1] / "assets" / "slothawk.png"

NAVY = (15, 23, 42)        # #0f172a
NAVY2 = (30, 41, 59)       # #1e293b
GREEN = (34, 197, 94)      # #22c55e
WHITE = (241, 245, 249)    # #f1f5f9
GREY = (148, 163, 184)     # #94a3b8


def radial_bg(img: Image.Image) -> None:
    px = img.load()
    cx = cy = SIZE / 2
    maxd = math.hypot(cx, cy)
    for y in range(SIZE):
        for x in range(SIZE):
            t = math.hypot(x - cx, y - cy) / maxd
            r = int(NAVY2[0] * (1 - t) + NAVY[0] * t)
            g = int(NAVY2[1] * (1 - t) + NAVY[1] * t)
            b = int(NAVY2[2] * (1 - t) + NAVY[2] * t)
            px[x, y] = (r, g, b)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    radial_bg(img)
    d = ImageDraw.Draw(img, "RGBA")

    # Accent ring.
    d.ellipse([28, 28, SIZE - 28, SIZE - 28], outline=GREEN, width=10)

    cx = SIZE / 2

    # Speed streaks (left side) suggesting it strikes fast.
    for i, yy in enumerate((250, 300, 350)):
        d.line([(70, yy), (70 + 70 + i * 12, yy)], fill=(34, 197, 94, 160), width=10)

    # --- Shuttlecock ---
    cork_cx, cork_cy, cork_r = cx + 20, 360, 46
    # Feather skirt: lines fanning up from the cork to a wide top.
    top_y, top_half = 150, 120
    feathers = 9
    for i in range(feathers + 1):
        t = i / feathers
        tx = (cork_cx - top_half) + t * (2 * top_half)
        d.line([(cork_cx, cork_cy), (tx, top_y)], fill=(241, 245, 249, 235), width=6)
    # Cross bands across the feathers.
    for band_y, half in ((210, 92), (270, 64)):
        d.line([(cork_cx - half, band_y), (cork_cx + half, band_y)], fill=(148, 163, 184, 200), width=5)
    # Cork (rounded base).
    d.ellipse([cork_cx - cork_r, cork_cy - cork_r, cork_cx + cork_r, cork_cy + cork_r], fill=GREEN)
    d.ellipse([cork_cx - cork_r, cork_cy - cork_r - 8, cork_cx + cork_r, cork_cy + cork_r - 8],
              outline=(255, 255, 255, 60), width=4)

    img.save(OUT, "PNG")
    print(f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
