"""Generate all TinyReadAloud brand assets (icons, shortcut ICO, social banners)."""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

from app import _lerp_rgb, create_tray_icon

ASSETS = "assets"

# Gemini palette (matches floating bar)
_BG_TOP = (30, 31, 32)
_BG_BOT = (40, 42, 48)
_BLUE = (71, 150, 227)
_PURPLE = (145, 119, 199)
_ROSE = (202, 102, 115)
_FG = (232, 234, 237)
_DIM = (154, 160, 166)


def _fonts_dir():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    return ""


def _load_font(size: int, bold: bool = False):
    names = ("segoeuib.ttf", "Segoe UI Bold.ttf") if bold else ("segoeui.ttf", "Segoe UI.ttf")
    base = _fonts_dir()
    for name in names:
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    for name in ("arialbd.ttf", "Arial Bold.ttf") if bold else ("arial.ttf", "Arial.ttf"):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _draw_aurora_strip(draw: ImageDraw.ImageDraw, x0: int, x1: int, y: int, h: int, speaking: bool = False):
    span = max(x1 - x0, 1)
    for i in range(span):
        t = i / max(span - 1, 1)
        if speaking:
            rgb = _lerp_rgb((52, 211, 153), _BLUE, t)
        elif t <= 0.55:
            rgb = _lerp_rgb(_BLUE, _PURPLE, t / 0.55)
        else:
            rgb = _lerp_rgb(_PURPLE, _ROSE, (t - 0.55) / 0.45)
        draw.line([(x0 + i, y), (x0 + i, y + h)], fill=rgb + (255,), width=1)


def _gradient_background(width: int, height: int) -> Image.Image:
    img = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(height - 1, 1)
        rgb = _lerp_rgb(_BG_TOP, _BG_BOT, t)
        draw.line([(0, y), (width, y)], fill=rgb + (255,))
    return img


def render_banner(width: int, height: int) -> Image.Image:
    """Social / GitHub repo banner with logo + wordmark."""
    img = _gradient_background(width, height)
    draw = ImageDraw.Draw(img)

    margin = max(24, width // 32)
    _draw_aurora_strip(draw, margin, width - margin, margin // 2, max(3, height // 120), speaking=False)
    _draw_aurora_strip(
        draw, margin, width - margin, height - margin, max(2, height // 160), speaking=False
    )

    icon_size = min(height - margin * 2, int(height * 0.62), 320)
    icon = create_tray_icon(size=icon_size, speaking=False)
    ix = margin + max(16, width // 40)
    iy = (height - icon_size) // 2
    img.paste(icon, (ix, iy), icon)

    tx = ix + icon_size + max(32, width // 28)
    title_size = max(36, min(72, height // 7))
    sub_size = max(18, min(32, height // 14))
    tag_size = max(14, min(22, height // 22))

    font_title = _load_font(title_size, bold=True)
    font_sub = _load_font(sub_size, bold=False)
    font_tag = _load_font(tag_size, bold=False)

    title = "TinyReadAloud"
    subtitle = "Select  ·  OCR  ·  Listen"
    tagline = "GPU-ready Kokoro TTS  ·  Gemini floating bar  ·  Markdown-aware read"

    ty = height // 2 - title_size
    draw.text((tx, ty), title, fill=_FG + (255,), font=font_title)
    draw.text((tx, ty + title_size + 8), subtitle, fill=_DIM + (255,), font=font_sub)

    line_y = ty + title_size + sub_size + 20
    line_w = min(width - tx - margin, int(width * 0.38))
    _draw_aurora_strip(draw, tx, tx + line_w, line_y, 4, speaking=False)

    draw.text((tx, line_y + 16), tagline, fill=_DIM + (255,), font=font_tag)

    return img


def _save_ico(path: str, sizes: list[int], speaking: bool = False):
    images = [create_tray_icon(size=s, speaking=speaking) for s in sizes]
    images[0].save(
        path,
        format="ICO",
        append_images=images[1:],
        sizes=[(s, s) for s in sizes],
    )


def main():
    os.makedirs(ASSETS, exist_ok=True)
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]

    _save_ico(os.path.join(ASSETS, "app.ico"), ico_sizes)
    _save_ico(os.path.join(ASSETS, "shortcut.ico"), ico_sizes)

    for sz in (16, 32, 48, 64, 128, 256, 512):
        create_tray_icon(size=sz, speaking=False).save(
            os.path.join(ASSETS, f"icon-{sz}.png"), format="PNG"
        )
    create_tray_icon(size=256, speaking=True).save(
        os.path.join(ASSETS, "icon-speaking-256.png"), format="PNG"
    )

    render_banner(1280, 640).save(os.path.join(ASSETS, "banner-github.png"), format="PNG")
    render_banner(1200, 630).save(os.path.join(ASSETS, "banner-opengraph.png"), format="PNG")
    render_banner(1280, 400).save(os.path.join(ASSETS, "banner-readme.png"), format="PNG")

    print("Brand assets written to assets/:")
    for name in sorted(os.listdir(ASSETS)):
        path = os.path.join(ASSETS, name)
        kb = os.path.getsize(path) // 1024
        print(f"  {name} ({kb} KB)")


if __name__ == "__main__":
    main()
