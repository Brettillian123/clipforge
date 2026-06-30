"""Locate the facecam by its peachy-gold border (#E7C58A) in a sampled frame.

The OBS gameplay scene draws a gold rectangle around the webcam. Detecting that
rectangle lets us crop the cam precisely per clip, so the tool still works when
the cam moves between scenes/games. Falls back to the configured default box.
"""
from __future__ import annotations

import os
import subprocess

import numpy as np
from PIL import Image

from . import config, util


def grab_frame(cfg: config.Config, vod: str, t: float) -> str:
    util.ensure_dirs(os.path.join(config.WORK_DIR, "frames"))
    out = os.path.join(config.WORK_DIR, "frames", f"probe_{util.stamp_for_name(t)}.png")
    subprocess.run(
        [util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{t:.3f}", "-i", vod, "-frames:v", "1", "-q:v", "3", out],
        check=True,
    )
    return out


def grab_region(cfg: config.Config, vod: str, t: float, rect, out_png: str) -> str:
    """Screenshot a source rectangle [x,y,w,h] from the VOD at time t (for chat insets)."""
    x, y, w, h = [int(v) for v in rect]
    util.ensure_dirs(os.path.dirname(out_png))
    subprocess.run(
        [util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{t:.3f}", "-i", vod, "-frames:v", "1",
         "-vf", f"crop={w}:{h}:{x}:{y}", "-q:v", "2", out_png],
        check=True,
    )
    return out_png


def _gold_mask(arr: np.ndarray) -> np.ndarray:
    """Boolean mask of peachy-gold pixels. arr is HxWx3 int."""
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    # target #E7C58A = (231,197,138); allow tolerance + enforce warm ordering r>g>b.
    return (
        (np.abs(r - 231) < 45) & (np.abs(g - 197) < 45) & (np.abs(b - 138) < 50)
        & (r > g) & (g > b) & ((r.astype(int) - b.astype(int)) > 55)
    )


def detect_cam_box(cfg: config.Config, frame_png: str) -> tuple[int, int, int, int] | None:
    """Return (x, y, w, h) of the cam CONTENT (inside the gold border), or None.

    Strategy: find the gold border as long horizontal/vertical runs within the
    region the cam can occupy, then validate the rectangle's size/position.
    """
    img = Image.open(frame_png).convert("RGB")
    arr = np.asarray(img).astype(int)
    H, W = arr.shape[:2]
    mask = _gold_mask(arr)

    # restrict to where the cam can live (left/middle), avoids gold HUD/text elsewhere
    x0c, x1c = max(0, cfg.cam_src_xmin - 20), min(W, cfg.cam_src_xmax + 20)
    y0c, y1c = max(0, cfg.cam_src_ymin - 20), min(H, cfg.cam_src_ymax + 20)
    region = np.zeros_like(mask)
    region[y0c:y1c, x0c:x1c] = mask[y0c:y1c, x0c:x1c]

    row_counts = region.sum(axis=1)
    col_counts = region.sum(axis=0)
    # border edges = rows/cols with a long gold run (cam border is ~380w x ~270h)
    rows = np.where(row_counts > 150)[0]
    cols = np.where(col_counts > 120)[0]
    if rows.size < 2 or cols.size < 2:
        return None
    y_top, y_bot = int(rows.min()), int(rows.max())
    x_left, x_right = int(cols.min()), int(cols.max())
    w, h = x_right - x_left, y_bot - y_top

    # validate plausibility against the known cam geometry
    if not (300 <= w <= 560 and 200 <= h <= 360):
        return None
    if not (cfg.cam_src_xmin - 25 <= x_left <= cfg.cam_src_xmax):
        return None
    if not (cfg.cam_src_ymin - 25 <= y_top <= cfg.cam_src_ymax):
        return None

    # inset by the ~3px border to get cam content
    inset = 4
    return (x_left + inset, y_top + inset, max(1, w - 2 * inset), max(1, h - 2 * inset))


def _face_focus(cfg: config.Config, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Tighten a cam box onto head+shoulders (the webcam is framed wide). No-op if disabled."""
    if not getattr(cfg, "cam_face_crop", False):
        return box
    x, y, w, h = box
    fx = int(round(x + cfg.cam_face_x * w))
    fy = int(round(y + cfg.cam_face_y * h))
    fw = max(1, int(round(cfg.cam_face_w * w)))
    fh = max(1, int(round(cfg.cam_face_h * h)))
    return (fx, fy, fw, fh)


def cam_crop_for_clip(cfg: config.Config, vod: str, peak_t: float) -> tuple[tuple[int, int, int, int], bool]:
    """Resolve the cam crop for a clip. Returns ((x,y,w,h), autodetected?).

    Samples a couple of frames near the peak; uses the first valid detection,
    otherwise the configured default crop. Either way the box is tightened onto the
    face (cam_face_* config) so the top panel isn't mostly empty room.
    """
    default = (cfg.cam_x, cfg.cam_y, cfg.cam_w, cfg.cam_h)
    if not cfg.autodetect_cam:
        return _face_focus(cfg, default), False
    for dt in (0.0, 1.5, -1.5, 3.0):
        t = max(0.1, peak_t + dt)
        try:
            png = grab_frame(cfg, vod, t)
            box = detect_cam_box(cfg, png)
        except Exception:
            box = None
        if box:
            return _face_focus(cfg, box), True
    return _face_focus(cfg, default), False
