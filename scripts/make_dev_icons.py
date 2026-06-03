"""Regenerate the badged dev icons from the prod icons.

One-off dev tool — NOT imported by the app at runtime (the app just serves the
committed PNGs as static files). Requires Pillow: `pip install pillow`.

Usage:  python scripts/make_dev_icons.py
"""
from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",        # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",     # Linux
]


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def make_dev_icon(src: str, dst: str) -> None:
    img = Image.open(src).convert("RGBA")
    size = img.size[0]  # square

    # Orange tint so the whole icon reads "not prod".
    overlay = Image.new("RGBA", img.size, (251, 146, 60, 90))  # amber-400 @ ~35%
    img = Image.alpha_composite(img, overlay)

    # "DEV" pill badge, bottom-right.
    draw = ImageDraw.Draw(img)
    badge_h = max(28, size // 7)
    badge_w = max(64, size // 3)
    badge_x = size - badge_w - 4
    badge_y = size - badge_h - 4
    draw.rounded_rectangle(
        [badge_x, badge_y, size - 4, size - 4],
        radius=badge_h // 3,
        fill=(234, 88, 12, 230),  # orange-600
    )
    font = _load_font(int(badge_h * 0.58))
    bbox = draw.textbbox((0, 0), "DEV", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = badge_x + (badge_w - tw) // 2
    ty = badge_y + (badge_h - th) // 2 - bbox[1]
    draw.text((tx, ty), "DEV", fill=(255, 255, 255, 255), font=font)

    img.save(dst, format="PNG")
    print("wrote", dst)


if __name__ == "__main__":
    make_dev_icon("static/icons/icon-192.png", "static/icons/icon-dev-192.png")
    make_dev_icon("static/icons/icon-512.png", "static/icons/icon-dev-512.png")
