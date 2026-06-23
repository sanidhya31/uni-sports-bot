"""Render Telegram-style mock chat screenshots for the README.

Produces assets/demo-user.png and assets/demo-admin.png. Text is kept
emoji-free so it renders crisply with Segoe UI.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

BG = (14, 22, 33)          # #0e1621
HEADER = (23, 33, 43)      # #17212b
IN_BUBBLE = (24, 37, 51)   # #182533
OUT_BUBBLE = (43, 82, 120) # #2b5278
TEXT = (236, 240, 243)
SUBTLE = (122, 139, 153)
GREEN = (60, 192, 96)
RED = (200, 80, 80)

W = 820
PAD = 18
MAXW = 540
FONT = "C:/Windows/Fonts/segoeui.ttf"
FONTB = "C:/Windows/Fonts/segoeuib.ttf"
F = ImageFont.truetype(FONT, 22)
FB = ImageFont.truetype(FONTB, 24)
FS = ImageFont.truetype(FONT, 16)


def wrap(draw, text, font, maxw):
    out = []
    for para in text.split("\n"):
        words, line = para.split(" "), ""
        for w in words:
            trial = (line + " " + w).strip()
            if draw.textlength(trial, font=font) <= maxw or not line:
                line = trial
            else:
                out.append(line)
                line = w
        out.append(line)
    return out


def render(path, messages, title):
    scratch = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    # Measure total height first.
    y = 80 + PAD
    laid = []
    for side, text, buttons in messages:
        lines = wrap(scratch, text, F, MAXW)
        bw = max([scratch.textlength(ln, font=F) for ln in lines] + [120]) + 2 * 16
        bh = len(lines) * 30 + 2 * 12 + 18
        if buttons:
            bh += 46
        laid.append((side, lines, int(bw), int(bh), buttons))
        y += bh + 12
    H = y + PAD

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Header.
    d.rectangle([0, 0, W, 70], fill=HEADER)
    avatar = ASSETS / "slothawk.png"
    if avatar.exists():
        a = Image.open(avatar).convert("RGB").resize((50, 50))
        mask = Image.new("L", (50, 50), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, 50, 50], fill=255)
        img.paste(a, (16, 10), mask)
    d.text((78, 14), title, font=FB, fill=TEXT)
    d.text((78, 42), "bot", font=FS, fill=SUBTLE)

    y = 80 + PAD
    for side, lines, bw, bh, buttons in laid:
        x = W - PAD - bw if side == "out" else PAD
        color = OUT_BUBBLE if side == "out" else IN_BUBBLE
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=16, fill=color)
        ty = y + 12
        for ln in lines:
            d.text((x + 16, ty), ln, font=F, fill=TEXT)
            ty += 30
        if buttons:
            bx = x + 16
            for label, kind in buttons:
                col = GREEN if kind == "ok" else RED
                lw = int(scratch.textlength(label, font=FS)) + 28
                d.rounded_rectangle([bx, ty + 4, bx + lw, ty + 38], radius=10, outline=col, width=2)
                d.text((bx + 14, ty + 10), label, font=FS, fill=col)
                bx += lw + 12
        d.text((x + bw - 52, y + bh - 22), "09:00", font=FS, fill=SUBTLE)
        y += bh + 12

    img.save(path, "PNG")
    print(f"Wrote {path} ({path.stat().st_size // 1024} KB, {W}x{H})")


def main():
    ASSETS.mkdir(exist_ok=True)
    render(ASSETS / "demo-user.png", [
        ("out", "/register max@uni-trier.de hunter2", None),
        ("in", "Poking the portal to see if these creds are real, one sec.", None),
        ("in", "Creds check out. Request's with the admin now, you'll get a ping the moment you're in.", None),
        ("in", "You're in! The bouncer (admin) let you past the velvet rope. Now hit /slots, point me at a slot, and /mystart.", None),
        ("out", "/mystart", None),
        ("in", "I'm on it. The instant that slot cracks open, it's yours.", None),
        ("in", "GOT IT. Badminton, Donnerstag 14:00 is yours. Go act surprised at how athletic you are. Booking done.", None),
    ], "SlotHawk_Bot")

    render(ASSETS / "demo-admin.png", [
        ("in", "New access request\nName: Max\nTelegram: @max (id 12345)\nUni email: max@uni-trier.de\nApprove this user?",
         [("Approve", "ok"), ("Reject", "no")]),
        ("out", "/users", None),
        ("in", "12345 @max [approved/active] p1 . Badminton Donnerstag 14:00", None),
        ("in", "12345 @max just bagged Badminton . Donnerstag 14:00.", None),
    ], "SlotHawk Admin")


if __name__ == "__main__":
    main()
