"""Generate the persistent corner watermark PNG (Twitch glitch logo + handle).

No mascot (brand rule: the chicken is emotes-only). White handle with a dark
stroke for legibility over the facecam, a small gold 'FOLLOW ON TWITCH' line,
and the Twitch-purple glitch mark. Rendered 4x then downscaled for clean edges.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

from . import config

SS = 4  # supersample

# Twitch glitch outline, normalized to a unit width (height ~1.166x width).
_GLITCH = [
    (0.00, 0.143), (0.143, 0.00), (1.00, 0.00), (1.00, 0.572),
    (0.715, 0.857), (0.50, 0.857), (0.357, 1.00), (0.357, 0.857), (0.00, 0.857),
]
_EYES = [  # white negative-space bars
    (0.572, 0.214, 0.643, 0.500),
    (0.786, 0.214, 0.857, 0.500),
]


def _font_file(face: str) -> str | None:
    parts = face.split()
    family = parts[0]
    style = "".join(parts[1:]) or "Regular"
    fname = f"{family}-{style}.ttf"
    dirs = [
        config.FONTS_DIR,
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
        r"C:\Windows\Fonts",
    ]
    for d in dirs:
        p = os.path.join(d, fname)
        if os.path.exists(p):
            return p
    return None


def _font(face: str, size: int) -> ImageFont.FreeTypeFont:
    fp = _font_file(face)
    return ImageFont.truetype(fp, size) if fp else ImageFont.load_default(size)


def _draw_glitch(canvas: Image.Image, x: int, y: int, h: int) -> None:
    w = int(h * 0.857)
    purple = tuple(int(config.TWITCH_PURPLE[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
    body = [(x + int(px * w), y + int(py * (h / 1.166))) for px, py in _GLITCH]
    d = ImageDraw.Draw(canvas)
    d.polygon(body, fill=purple)
    for (x0, y0, x1, y1) in _EYES:
        d.rectangle(
            [x + int(x0 * w), y + int(y0 * (h / 1.166)),
             x + int(x1 * w), y + int(y1 * (h / 1.166))],
            fill=(255, 255, 255, 255),
        )


def _text(draw, xy, s, font, fill, stroke):
    draw.text(xy, s, font=font, fill=fill, stroke_width=stroke, stroke_fill=(11, 9, 18, 235))


def render_watermark(cfg: config.Config, force: bool = False) -> str:
    config_dir = config.ASSETS_DIR
    os.makedirs(config_dir, exist_ok=True)
    out = os.path.join(config_dir, "watermark.png")
    if os.path.exists(out) and not force:
        return out

    handle_px = cfg.wm_text_px * SS
    cta_px = int(handle_px * 0.56)
    f_handle = _font(cfg.wm_font, handle_px)
    f_cta = _font(cfg.wm_font, cta_px)
    handle = cfg.wm_handle
    cta = cfg.wm_cta

    tmp = Image.new("RGBA", (10, 10))
    md = ImageDraw.Draw(tmp)
    hb = md.textbbox((0, 0), handle, font=f_handle, stroke_width=SS * 2)
    cb = md.textbbox((0, 0), cta, font=f_cta, stroke_width=SS)
    handle_w, handle_h = hb[2] - hb[0], hb[3] - hb[1]
    cta_w, cta_h = cb[2] - cb[0], cb[3] - cb[1]

    icon_h = int(handle_px * 1.18)
    icon_w = int(icon_h * 0.857)
    gap = int(handle_px * 0.45)
    pad = int(handle_px * 0.30)
    line1_h = max(icon_h, handle_h)
    text_x = pad + icon_w + gap
    total_w = text_x + max(handle_w, cta_w) + pad
    cta_gap = int(handle_px * 0.18)
    total_h = pad + line1_h + cta_gap + cta_h + pad

    canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    _draw_glitch(canvas, pad, pad + (line1_h - icon_h) // 2, icon_h)
    _text(draw, (text_x - hb[0], pad + (line1_h - handle_h) // 2 - hb[1]),
          handle, f_handle, (255, 255, 255, 255), SS * 2)
    gold = tuple(int(config.GOLD[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
    _text(draw, (text_x - cb[0], pad + line1_h + cta_gap - cb[1]),
          cta, f_cta, gold, SS)

    canvas = canvas.resize((total_w // SS, total_h // SS), Image.LANCZOS)
    # apply global opacity
    r, g, b, a = canvas.split()
    a = a.point(lambda v: int(v * cfg.wm_opacity))
    canvas = Image.merge("RGBA", (r, g, b, a))
    canvas.save(out)
    return out
