"""Overlay elements: render text / shapes / images / emoji to transparent PNGs.

Every on-clip extra is an "element" with one canonical envelope (render-space px,
1080x1920) so the dashboard's drag stage maps 1:1 with the output and Python/ffmpeg
consume the numbers with no translation:

  {
    "id": "e1", "type": "text|rect|ellipse|line|arrow|image|emoji",
    "z": 1, "visible": true, "seg_index": 0,
    "geom":   {"x": px, "y": px, "w": px, "h": px|null},   # top-left + size
    "timing": {"start": s, "end": s, "fadeIn": s, "fadeOut": s},   # within the segment
    "style":  {"font","size","color","align","outline","outlineColor","bg","bgAlpha",
               "radius","fill","fillAlpha","stroke","strokeColor","strokeW","opacity","uppercase"},
    "data":   {"text","shape","src","orient","dir"}
  }

elements.py only draws the *static* visual; timing/fade/z-order are applied by the
compositor in render.py.
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import branding, config

SS = 2  # supersample for crisp text/shapes


def hex_rgba(h: str, opacity: float = 1.0) -> tuple:
    h = (h or "FFFFFF").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 255, 255, 255
    return (r, g, b, max(0, min(255, int(opacity * 255))))


def _font(face: str, size: int) -> ImageFont.FreeTypeFont:
    return branding._font(face or "Poppins ExtraBold", size)


def _emoji_png(char: str):
    """Path to the downloaded Twemoji PNG for an emoji char, or None (see tools/fetch_emoji.py)."""
    if not char:
        return None
    cp = "-".join(format(ord(c), "x") for c in char if ord(c) != 0xFE0F)
    p = os.path.join(config.LOCAL_ROOT, "emoji", cp + ".png")
    return p if os.path.exists(p) else None


def _emoji_font(size: int):
    p = os.path.join(r"C:\Windows\Fonts", "seguiemj.ttf")
    if os.path.exists(p):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return _font("Poppins ExtraBold", size)


def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def _render_text(el, w_px, out_png, emoji=False):
    g, s, d = el.get("geom", {}), el.get("style", {}), el.get("data", {})
    text = str(d.get("text", "")).strip()
    if s.get("uppercase"):
        text = text.upper()
    if not text:
        text = " "
    size = max(8, int(s.get("size", 70))) * SS
    pad = int(s.get("pad", max(10, size // SS // 4))) * SS
    outline = int(s.get("outline", 6)) * SS
    font = _emoji_font(size) if emoji else _font(s.get("font", "Poppins ExtraBold"), size)
    inner_w = max(10, w_px * SS - 2 * pad)

    pd = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lines = [text] if emoji else _wrap(pd, text, font, inner_w)
    asc, desc = font.getmetrics()
    line_h = asc + desc + int(6 * SS)
    W = w_px * SS
    H = line_h * len(lines) + 2 * pad

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    if s.get("bg"):
        dr.rounded_rectangle([0, 0, W - 1, H - 1], radius=int(s.get("radius", 18)) * SS,
                             fill=hex_rgba(s["bg"], float(s.get("bgAlpha", 0.6))))
    fill = hex_rgba(s.get("color", "FFFFFF"), float(s.get("opacity", 1.0)))
    ocol = hex_rgba(s.get("outlineColor", "000000"))
    align = s.get("align", "center")
    y = pad
    for ln in lines:
        lw = dr.textlength(ln, font=font)
        x = pad if align == "left" else (W - pad - lw if align == "right" else (W - lw) / 2)
        if emoji:
            dr.text((x, y), ln, font=font, embedded_color=True)
        else:
            dr.text((x, y), ln, font=font, fill=fill, stroke_width=outline, stroke_fill=ocol)
        y += line_h
    img = img.resize((max(1, W // SS), max(1, H // SS)), Image.LANCZOS)
    img.save(out_png)
    return out_png, img.width, img.height


def _render_shape(el, w_px, h_px, out_png):
    s, d = el.get("style", {}), el.get("data", {})
    W, H = max(2, w_px * SS), max(2, h_px * SS)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    fill = hex_rgba(s["fill"], float(s.get("fillAlpha", 1.0))) if s.get("fill") else None
    stroke = hex_rgba(s.get("strokeColor", "000000")) if s.get("stroke", s.get("strokeColor")) else None
    sw = max(0, int(s.get("strokeW", 0)) * SS)
    shape = d.get("shape", "rect")
    m = sw // 2 + 1
    if shape in ("rect", "roundrect"):
        dr.rounded_rectangle([m, m, W - m - 1, H - m - 1],
                             radius=int(s.get("radius", 0)) * SS, fill=fill, outline=stroke, width=sw)
    elif shape == "ellipse":
        dr.ellipse([m, m, W - m - 1, H - m - 1], fill=fill, outline=stroke, width=sw)
    elif shape in ("line", "arrow"):
        col = stroke or fill or hex_rgba(s.get("color", config.GOLD))
        lw = max(2, int(s.get("strokeW", 8)) * SS)
        orient = d.get("orient", "h")
        if orient == "v":
            p0, p1 = (W // 2, m), (W // 2, H - m)
        elif orient == "diag":
            p0, p1 = (m, H - m), (W - m, m)
        else:
            p0, p1 = (m, H // 2), (W - m, H // 2)
        if d.get("dir") in ("left", "up"):
            p0, p1 = p1, p0
        dr.line([p0, p1], fill=col, width=lw)
        if shape == "arrow":
            _arrowhead(dr, p0, p1, lw, col)
    img = img.resize((max(1, W // SS), max(1, H // SS)), Image.LANCZOS)
    img.save(out_png)
    return out_png, img.width, img.height


def _arrowhead(dr, p0, p1, lw, col):
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    size = lw * 3.2
    for da in (math.radians(150), math.radians(-150)):
        x = p1[0] + size * math.cos(ang + da)
        y = p1[1] + size * math.sin(ang + da)
        dr.line([p1, (x, y)], fill=col, width=lw)


def _render_image(el, w_px, h_px, out_png):
    s, d = el.get("style", {}), el.get("data", {})
    src = d.get("src")
    if not src or not os.path.exists(src):
        return None
    im = Image.open(src).convert("RGBA")
    if h_px:
        im = im.resize((max(1, w_px), max(1, h_px)), Image.LANCZOS)
    else:
        im = im.resize((max(1, w_px), max(1, int(w_px * im.height / max(1, im.width)))), Image.LANCZOS)
    if s.get("radius"):
        im = _round_corners(im, int(s["radius"]))
    if float(s.get("opacity", 1.0)) < 1.0:
        a = im.getchannel("A").point(lambda v: int(v * float(s["opacity"])))
        im.putalpha(a)
    im.save(out_png)
    return out_png, im.width, im.height


def _round_corners(im, radius):
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=radius, fill=255)
    im.putalpha(mask)
    return im


def render_element_png(cfg: config.Config, el: dict, out_png: str):
    """Render one element to a transparent PNG. Returns (path, w_px, h_px) or None."""
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    g = el.get("geom", {})
    t = el.get("type", "text")
    w_px = max(8, int(g.get("w") or 0.4 * cfg.out_w))
    h_px = int(g["h"]) if g.get("h") else None
    try:
        if t == "text":
            return _render_text(el, w_px, out_png)
        if t == "emoji":
            png = _emoji_png(str(el.get("data", {}).get("text", "")).strip())
            if png:                                  # Twemoji image -> identical in editor + render
                im = Image.open(png).convert("RGBA")
                h = max(1, int(w_px * im.height / max(1, im.width)))
                im.resize((w_px, h), Image.LANCZOS).save(out_png)
                return out_png, w_px, h
            return _render_text(el, w_px, out_png, emoji=True)   # fallback to system emoji font
        if t in ("rect", "roundrect", "ellipse", "line", "arrow"):
            if t in ("rect", "roundrect", "ellipse", "line", "arrow") and "shape" not in el.get("data", {}):
                el.setdefault("data", {})["shape"] = "roundrect" if t == "roundrect" else t
            return _render_shape(el, w_px, h_px or int(0.1 * cfg.out_h), out_png)
        if t == "image":
            return _render_image(el, w_px, h_px, out_png)
    except Exception as e:  # noqa: BLE001
        from .util import log
        log(f"[elements] {t} render failed: {e}")
    return None


def chat_inset_png(cfg: config.Config, src_png: str, out_png: str, radius: int = 22,
                   border: int = 5, border_hex: str = config.GOLD) -> str:
    """Rounded corners + gold border + soft shadow on a grabbed chat screenshot."""
    im = Image.open(src_png).convert("RGBA")
    im = _round_corners(im, radius)
    pad = border + 12
    canvas = Image.new("RGBA", (im.width + 2 * pad, im.height + 2 * pad), (0, 0, 0, 0))
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([pad, pad + 5, pad + im.width, pad + im.height + 5],
                                         radius=radius + border, fill=(0, 0, 0, 120))
    canvas = Image.alpha_composite(canvas, sh.filter(ImageFilter.GaussianBlur(8)))
    ImageDraw.Draw(canvas).rounded_rectangle(
        [pad - border, pad - border, pad + im.width + border, pad + im.height + border],
        radius=radius + border, outline=hex_rgba(border_hex, 0.95), width=border)
    canvas.alpha_composite(im, (pad, pad))
    canvas.save(out_png)
    return out_png
