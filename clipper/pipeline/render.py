"""Render clips with a single-pass overlay compositor.

A clip is one or more VOD segments (concatenated for context). Each segment is laid
out facecam-top / gameplay-bottom, captioned (ASS karaoke), then any number of timed
OVERLAYS are composited in one ffmpeg pass: the Twitch watermark, an optional chat
inset, and the clip's elements (text / shapes / images / emoji). Overlays are
pre-rendered to PNGs (Pillow) and composited with fades; the graph is written to a
script file so long graphs never hit the Windows command-line limit.
"""
from __future__ import annotations

import functools
import os
import subprocess
import threading

from PIL import Image

from . import camdetect, captions, config, elements, transcribe, util
from .util import log

SRC_W, SRC_H = 1920, 1080

# An element whose start time is within this of 0 counts as a "cover"/hook element: it is forced
# solid on the first frame so it shows up on the posted thumbnail. Also used to build the cover.jpg.
COVER_START_S = 0.05


# --------------------------------------------------------------------------- #
# probes + small helpers
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=8)
def _encoder_ok(name: str) -> bool:
    """Probe whether an ffmpeg video encoder actually initializes on this machine."""
    r = subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                        "-c:v", name, "-f", "null", "-"], capture_output=True, text=True)
    return r.returncode == 0


@functools.lru_cache(maxsize=1)
def _hw_encoder(use_hw: bool):
    """Best available HARDWARE H.264 encoder, probed once: NVENC (NVIDIA laptop) ->
    AMF (this AMD RX 9070 XT desktop) -> None (CPU libx264 fallback). One codepath,
    both machines. h264_amf is ~10-20x faster than libx264 here."""
    if not use_hw:
        return None
    for name in ("h264_nvenc", "h264_amf"):
        if _encoder_ok(name):
            return name
    return None


def _clamp_rect(rect):
    x, y, w, h = [int(round(float(v))) for v in rect]
    x = max(0, min(x, SRC_W - 10)); y = max(0, min(y, SRC_H - 10))
    w = max(10, min(w, SRC_W - x)); h = max(10, min(h, SRC_H - y))
    return [x, y, w, h]


def _img_aspect(png: str) -> float:
    try:
        w, h = Image.open(png).size
        return h / max(1, w)
    except Exception:
        return 0.6


def _fit_crop(box, tw, th, maxw=SRC_W, maxh=SRC_H):
    x, y, w, h = box
    target = tw / th
    if w / h > target:
        nw = max(2, round(h * target)); x += (w - nw) // 2; w = nw
    else:
        nh = max(2, round(w / target)); y += (h - nh) // 2; h = nh
    x = max(0, min(int(x), maxw - int(w)))
    y = max(0, min(int(y), maxh - int(h)))
    return int(x), int(y), int(w), int(h)


def _gameplay_crop(cfg, cam_box, autodetected):
    if not autodetected:
        return _fit_crop((cfg.gp_x, cfg.gp_y, cfg.gp_w, cfg.gp_h), cfg.out_w, cfg.gp_panel_h)
    cam_right = cam_box[0] + cam_box[2] + 12
    band_w = SRC_W - cam_right
    crop_w = round(SRC_H * (cfg.out_w / cfg.gp_panel_h))
    if band_w >= crop_w:
        cx = cam_right + (band_w - crop_w) // 2
        return _fit_crop((cx, 0, crop_w, SRC_H), cfg.out_w, cfg.gp_panel_h)
    return _fit_crop((cfg.gp_x, cfg.gp_y, cfg.gp_w, cfg.gp_h), cfg.out_w, cfg.gp_panel_h)


def _overlay_xy(cfg, pos, cw, ch):
    m, bottom_keepout = 40, 410
    y_bottom = max(cfg.seam_y + 10, cfg.out_h - bottom_keepout - ch)
    table = {
        "bottom-left": (m, y_bottom), "bottom-right": (cfg.out_w - cw - m, y_bottom),
        "top-left": (m, cfg.seam_y + 20), "top-right": (cfg.out_w - cw - m, cfg.seam_y + 20),
        "center": ((cfg.out_w - cw) // 2, (cfg.out_h - ch) // 2),
    }
    x, y = table.get(pos, table["bottom-left"])
    return max(0, int(x)), max(0, int(y))


# Tag bt709/limited-range on every encode (source is OBS bt709 Partial) so players don't
# wash out / shift colors, and force CFR so captions/audio can't drift on any VFR input.
_VFLAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-color_range", "tv", "-fps_mode", "cfr"]


def _encode_args(cfg, encoder):
    if encoder == "h264_nvenc":
        v = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23",
             "-b:v", "0", "-pix_fmt", "yuv420p"]
    elif encoder == "h264_amf":
        # AMD AMF (RDNA4): CQP quality mode; qp tuned for crisp 1080x1920 short-form over busy gameplay.
        v = ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", "quality",
             "-rc", "cqp", "-qp_i", "22", "-qp_p", "22", "-qp_b", "24", "-pix_fmt", "yuv420p"]
    else:
        v = ["-c:v", "libx264", "-preset", "medium", "-crf", str(cfg.x264_crf), "-pix_fmt", "yuv420p"]
    return v + _VFLAGS


# Once a hardware encoder is caught emitting a corrupt clip this run, stay on the CPU for the rest of
# the batch (don't waste a doomed HW attempt + verify on every remaining clip). Resets per process.
_FORCE_CPU = False


def _decode_ok(path: str, max_secs: float | None = None) -> bool:
    """Verify a freshly rendered file ACTUALLY decodes. AMD AMF sometimes returns success (rc=0,
    full-size file) while writing a corrupt H.264 bitstream — decoding it is the only reliable way to
    catch it. max_secs only decodes the first chunk (AMF corruption spans the whole stream, so a short
    sample catches it — used for long-form so we don't fully decode a 60-min video). True == clean."""
    cmd = [util.ffmpeg(), "-hide_banner", "-v", "error"]
    if max_secs:
        cmd += ["-t", str(max_secs)]
    cmd += ["-i", path, "-map", "0:v:0", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return True   # never block a render on a flaky probe
    return sum(1 for ln in r.stderr.splitlines() if ln.strip()) <= 3


def _words_in(transcript, s, e):
    return [{"start": round(w["start"] - s, 3), "end": round(w["end"] - s, 3), "word": w["word"]}
            for w in transcript["words"] if w["end"] > s and w["start"] < e]


# --------------------------------------------------------------------------- #
# single-pass compositor
# --------------------------------------------------------------------------- #
def _filtergraph(cfg, cam, gp, ass_path, overlays):
    """Base layout -> captions -> watermark([1]) -> each overlay([2+i]) -> [outv]."""
    gold = config.hex_to_ffmpeg(config.GOLD)
    lav = config.hex_to_ffmpeg(config.LAVENDER)
    parts = [
        f"[0:v]crop={cam[2]}:{cam[3]}:{cam[0]}:{cam[1]},scale={cfg.out_w}:{cfg.seam_y},setsar=1[top]",
        f"[0:v]crop={gp[2]}:{gp[3]}:{gp[0]}:{gp[1]},scale={cfg.out_w}:{cfg.gp_panel_h},setsar=1[gp]",
        "[top][gp]vstack=inputs=2[stack]",
        (f"[stack]drawbox=x=0:y={cfg.div_gold_y}:w={cfg.out_w}:h={cfg.div_gold_h}:color={gold}@1:t=fill,"
         f"drawbox=x=0:y={cfg.div_lav_y}:w={cfg.out_w}:h={cfg.div_lav_h}:color={lav}@1:t=fill[dv]"),
    ]
    last = "dv"
    if ass_path:
        esc = util.ff_escape_path(ass_path)
        fonts = util.ff_escape_path(config.FONTS_DIR)
        parts.append(f"[{last}]subtitles={esc}:fontsdir={fonts}[sub]")
        last = "sub"
    # watermark: ffmpeg overlay expr — W/w are main/overlay widths, so right-align = W-w-margin
    wm_x = f"W-w-{cfg.wm_x}" if getattr(cfg, "wm_align", "left") == "right" else f"{cfg.wm_x}"
    parts.append(f"[{last}][1:v]overlay={wm_x}:{cfg.wm_y}[wm]")
    last = "wm"
    for i, ov in enumerate(overlays):
        idx = 2 + i
        win = max(0.1, ov["t_end"] - ov["t_start"])
        fi = min(ov.get("fade_in", 0) or 0, win)
        fo = min(ov.get("fade_out", 0) or 0, win)
        chain = f"[{idx}:v]scale={ov['w']}:{ov['h']},format=rgba"
        if fi > 0:
            chain += f",fade=t=in:st={ov['t_start']:.2f}:d={fi:.2f}:alpha=1"
        if fo > 0:
            chain += f",fade=t=out:st={max(ov['t_start'], ov['t_end'] - fo):.2f}:d={fo:.2f}:alpha=1"
        chain += f"[ov{i}]"
        parts.append(chain)
        out = "outv" if i == len(overlays) - 1 else f"st{i}"
        parts.append(f"[{last}][ov{i}]overlay={ov['x']}:{ov['y']}:"
                     f"enable='between(t,{ov['t_start']:.2f},{ov['t_end']:.2f})':eof_action=pass[{out}]")
        last = out
    if not overlays:
        parts.append(f"[{last}]null[outv]")
    return ";".join(parts)


class RenderCancelled(Exception):
    """Raised when a render is superseded/cancelled mid-flight (a newer render took over)."""


def _run_ffmpeg(cmd, dur, progress_cb=None, should_cancel=None):
    """Run ffmpeg streaming its `-progress` output (on stdout) to progress_cb(seconds_done).
    stderr is drained on a side thread (avoids pipe-buffer deadlock). If should_cancel()
    becomes true, the ffmpeg process is killed and RenderCancelled is raised.
    Returns (returncode, stderr_text)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    errbuf = []
    def _drain():
        try:
            for ln in proc.stderr:
                errbuf.append(ln)
        except Exception:  # noqa: BLE001
            pass
    th = threading.Thread(target=_drain, daemon=True)
    th.start()
    cancelled = False
    try:
        for ln in proc.stdout:
            if should_cancel is not None and should_cancel():
                cancelled = True
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                break
            if progress_cb is None:
                continue
            ln = ln.strip()
            if ln.startswith("out_time_us="):
                try:
                    progress_cb(int(ln.split("=", 1)[1]) / 1e6)
                except (ValueError, IndexError):
                    pass
            elif ln == "progress=end":
                progress_cb(dur)
    finally:
        proc.wait()
        th.join(timeout=1.0)
    if cancelled:
        raise RenderCancelled()
    return proc.returncode, "".join(errbuf)


def _render_one(cfg, vod, seg_start, dur, words, cam, gp, overlays, watermark, out_path, cap_style=None, mute=False, progress_cb=None, should_cancel=None):
    if should_cancel is not None and should_cancel():
        raise RenderCancelled()
    ass_path = None
    if words:
        ass_path = os.path.join(config.WORK_DIR, "ass",
                                os.path.splitext(os.path.basename(out_path))[0] + ".ass")
        util.ensure_dirs(os.path.dirname(ass_path))
        captions.build_ass(cfg, words, dur, ass_path, cap_style)

    fg = _filtergraph(cfg, cam, gp, ass_path, overlays)
    fc_path = os.path.join(config.WORK_DIR, "fc",
                           os.path.splitext(os.path.basename(out_path))[0] + ".txt")
    util.ensure_dirs(os.path.dirname(fc_path))
    with open(fc_path, "w", encoding="utf-8") as fh:
        fh.write(fg)

    inputs = ["-ss", f"{seg_start:.3f}", "-i", vod, "-i", watermark]
    for ov in overlays:
        inputs += ["-loop", "1", "-framerate", str(cfg.fps), "-i", ov["png"]]
    amap = "0:a:0?"
    if mute:
        # silent track (concat needs every segment to carry an audio stream)
        inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        amap = f"{2 + len(overlays)}:a"
    base = [util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            *inputs, "-filter_complex_script", fc_path, "-map", "[outv]", "-map", amap, "-t", f"{dur:.3f}"]
    tail = ["-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000",
            "-r", str(cfg.fps), "-movflags", "+faststart", out_path]
    global _FORCE_CPU
    hw = None if _FORCE_CPU else _hw_encoder(cfg.use_nvenc)
    for enc in ([hw, None] if hw else [None]):
        rc, errtxt = _run_ffmpeg(base + _encode_args(cfg, enc) + tail, dur, progress_cb, should_cancel)
        ok = rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10000
        if ok and enc and not _decode_ok(out_path):
            # AMD AMF can return success while emitting a CORRUPT bitstream (GPU/driver glitch, e.g.
            # right after a Vulkan transcription). Treat as failure -> fall through to CPU libx264,
            # and stay on CPU for the rest of this run so the whole batch isn't garbage.
            log(f"[render] {enc} reported success but the clip is CORRUPT — re-rendering on CPU "
                f"(staying on CPU for the rest of this batch)")
            _FORCE_CPU = True
            ok = False
        if ok:
            return out_path
        log(f"[render] {enc or 'libx264'} failed/empty: {errtxt.strip()[-400:]}")
    raise RuntimeError(f"render failed: {out_path}")


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def render_clip(cfg, vod, cand, watermark, out_path):
    """One-shot render from a detector Candidate (single segment, no extra elements)."""
    cam_box, auto = camdetect.cam_crop_for_clip(cfg, vod, cand.peak)
    cand.autodetected_cam = auto
    cam = _fit_crop(cam_box, cfg.out_w, cfg.seam_y)
    gp = _gameplay_crop(cfg, cam_box, auto)
    words = cand.words if not getattr(cand, "no_captions", False) else []
    return _render_one(cfg, vod, cand.start, cand.duration, words, cam, gp, [], watermark, out_path)


def base_frame(cfg, vod, spec, t_clip, watermark, out_png):
    """Render ONE clean base-layout frame (facecam-top + gameplay-bottom + divider +
    watermark, no captions/elements) at clip-relative time t_clip of segment 0. The
    dashboard uses this as the WYSIWYG drag-stage background."""
    seg0 = spec.segments[0]
    s, e = float(seg0["start"]), float(seg0["end"])
    abs_t = max(s, min(s + max(0.0, t_clip), e - 0.1))
    # detect the cam at the SAME canonical timestamp render_spec uses, so the
    # drag-stage framing matches the rendered output (only the shown frame follows the playhead)
    canon_t = s + min(5.0, (e - s) / 2)
    if spec.cam_mode == "manual" and spec.cam:
        cam_box = (spec.cam["x"], spec.cam["y"], spec.cam["w"], spec.cam["h"])
    else:
        cam_box, _ = camdetect.cam_crop_for_clip(cfg, vod, canon_t)
    cam = _fit_crop(cam_box, cfg.out_w, cfg.seam_y)
    gp = _gameplay_crop(cfg, cam_box, spec.cam_mode != "manual")
    fg = _filtergraph(cfg, cam, gp, None, [])
    fc_path = os.path.join(config.WORK_DIR, "fc", "baseframe.txt")
    util.ensure_dirs(os.path.dirname(fc_path), os.path.dirname(out_png))
    with open(fc_path, "w", encoding="utf-8") as fh:
        fh.write(fg)
    subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{abs_t:.3f}", "-i", vod, "-i", watermark,
                    "-filter_complex_script", fc_path, "-map", "[outv]",
                    "-frames:v", "1", "-q:v", "3", out_png], check=True)
    return out_png


def _chat_overlay(cfg, vod, spec, i, s, dur, tmp_dir):
    ch = spec.chat or {}
    if not (ch.get("enabled") and int(ch.get("seg_index", 0) or 0) == i):
        return None
    try:
        scale = float(ch.get("scale", cfg.chat_default_scale))
    except (TypeError, ValueError):
        scale = cfg.chat_default_scale
    if scale != scale:
        scale = cfg.chat_default_scale
    scale = min(0.95, max(0.1, scale))
    cw = max(40, int(cfg.out_w * scale))
    rect = _clamp_rect(ch.get("rect", list(cfg.chat_rect)))
    raw = os.path.join(tmp_dir, f"{spec.id}_chatraw{i}.png")
    if ch.get("src", "auto") == "auto" or not ch.get("image"):
        gt = ch.get("grab_t")
        gt = s if gt is None else max(0.0, float(gt))
        camdetect.grab_region(cfg, vod, gt, rect, raw)
        src_png = raw
    else:
        src_png = ch["image"]
    bordered = os.path.join(tmp_dir, f"{spec.id}_chat{i}.png")
    elements.chat_inset_png(cfg, src_png, bordered)
    aspect = _img_aspect(bordered)
    chx = max(20, int(cw * aspect))
    x, y = _overlay_xy(cfg, ch.get("pos", cfg.chat_default_pos), cw, chx)
    x = max(0, min(x, cfg.out_w - cw)); y = max(cfg.seam_y, min(y, cfg.out_h - chx))
    ts = max(0.0, float(ch.get("t_start", 0.0)))
    te = min(float(ch.get("t_end", cfg.chat_default_dur)), dur)
    ts = min(ts, max(0.0, dur - 0.2))
    if te <= ts:
        te = min(dur, ts + 1.0)
    return {"png": bordered, "x": x, "y": y, "w": cw, "h": chx, "t_start": ts, "t_end": te,
            "fade_in": 0.25, "fade_out": 0.25, "z": int(ch.get("z", 40))}


def _element_overlays(cfg, spec, i, dur, tmp_dir):
    out = []
    for el in (spec.elements or []):
        if not el.get("visible", True) or int(el.get("seg_index", 0) or 0) != i:
            continue
        png_path = os.path.join(tmp_dir, f"{spec.id}_el_{el.get('id', 'x')}_{i}.png")
        res = elements.render_element_png(cfg, el, png_path)
        if not res:
            continue
        png, w, h = res
        g = el.get("geom", {})
        tm = el.get("timing", {})
        # keep at least partially on-canvas even if a coordinate was typed out of range
        x = max(-int(w * 0.5), min(int(g.get("x", 0)), cfg.out_w - int(w * 0.5)))
        y = max(0, min(int(g.get("y", 0)), cfg.out_h - 20))
        ts = max(0.0, float(tm.get("start", 0.0)))
        te = float(tm["end"]) if tm.get("end") is not None else dur
        te = min(te, dur); ts = min(ts, max(0.0, dur - 0.2))
        if te <= ts:
            te = min(dur, ts + 1.0)
        fade_in = float(tm.get("fadeIn", 0) or 0)
        # An element placed at 0s (only on the first segment) is a cover/hook element: it must be fully
        # opaque on the VERY FIRST frame so it lands on the posted thumbnail (platforms grab frame 0).
        # A fade-in would make frame 0 transparent, so drop it for these.
        if i == 0 and ts <= COVER_START_S:
            ts, fade_in = 0.0, 0.0
        out.append({"png": png, "x": x, "y": y, "w": w, "h": h, "t_start": ts, "t_end": te,
                    "fade_in": fade_in, "fade_out": float(tm.get("fadeOut", 0) or 0),
                    "z": int(el.get("z", 1))})
    return out


def render_spec(cfg, vod, spec, transcript, watermark, out_path, include_overlays=True, progress_cb=None, should_cancel=None):
    """Render a ClipSpec: segments laid out + captioned, optionally with overlays.

    include_overlays=False -> the BASE PREVIEW (layout + captions + watermark only);
    the dashboard plays this and draws chat/elements live as HTML on top, so editing
    is instant. include_overlays=True -> the full EXPORT with everything burned in.

    progress_cb(frac) (optional) is called with overall 0..1 progress as ffmpeg works.
    """
    segs = spec.segments
    total_dur = sum(max(0.3, float(s["end"]) - float(s["start"])) for s in segs) or 1.0
    base_done = 0.0
    tmp_dir = os.path.join(config.WORK_DIR, "segtmp")
    util.ensure_dirs(tmp_dir, os.path.dirname(out_path) or ".")
    seg_files = []
    for i, seg in enumerate(segs):
        if should_cancel is not None and should_cancel():
            raise RenderCancelled()
        s, e = float(seg["start"]), float(seg["end"])
        dur = max(0.3, e - s)
        if not spec.captions_enabled:
            words = []
        elif getattr(spec, "caption_words", None) and len(segs) == 1:
            words = spec.caption_words            # user-fixed words (single-segment)
        else:
            try:
                words = transcribe.clip_words(cfg, transcribe.wav_path(vod), s, e)
            except Exception as ex:  # noqa: BLE001
                log(f"[render] clip_words failed ({ex}); using full transcript")
                words = _words_in(transcript, s, e)

        if spec.cam_mode == "manual" and spec.cam:
            cam_box = (spec.cam["x"], spec.cam["y"], spec.cam["w"], spec.cam["h"])
        else:
            cam_box, _ = camdetect.cam_crop_for_clip(cfg, vod, s + min(5.0, dur / 2))
        cam = _fit_crop(cam_box, cfg.out_w, cfg.seam_y)
        gp = _gameplay_crop(cfg, cam_box, spec.cam_mode != "manual")

        overlays = []
        if include_overlays:
            chat = _chat_overlay(cfg, vod, spec, i, s, dur, tmp_dir)
            if chat:
                overlays.append(chat)
            overlays += _element_overlays(cfg, spec, i, dur, tmp_dir)
            overlays.sort(key=lambda o: o.get("z", 1))   # higher z drawn last (on top)

        seg_cb = None
        if progress_cb:
            seg_cb = (lambda st, _bd=base_done, _d=dur:
                      progress_cb(min(0.99, (_bd + min(max(st, 0.0), _d)) / total_dur)))
        seg_out = out_path if len(segs) == 1 else os.path.join(tmp_dir, f"{spec.id}_seg{i}.mp4")
        _render_one(cfg, vod, s, dur, words, cam, gp, overlays, watermark, seg_out,
                    cap_style=spec.caption_style, mute=bool(seg.get("mute")), progress_cb=seg_cb,
                    should_cancel=should_cancel)
        base_done += dur
        seg_files.append(seg_out)

    if len(seg_files) == 1:
        return out_path
    if should_cancel is not None and should_cancel():
        raise RenderCancelled()
    return _concat(cfg, seg_files, out_path)


def _concat(cfg, seg_files, out_path):
    """Concatenate by re-encoding (uniform params), verify, then drop temps."""
    n = len(seg_files)
    inputs = []
    for f in seg_files:
        inputs += ["-i", f]
    fc = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    ok = False
    hw = _hw_encoder(cfg.use_nvenc)
    for enc in ([hw, None] if hw else [None]):
        res = subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *inputs,
                              "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                              *_encode_args(cfg, enc), "-c:a", "aac", "-b:a", cfg.audio_bitrate,
                              "-ar", "48000", "-r", str(cfg.fps), "-movflags", "+faststart", out_path],
                             text=True, capture_output=True, encoding="utf-8", errors="replace")
        if res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
            ok = True
            break
        log(f"[concat] {enc or 'libx264'} failed: {res.stderr.strip()[-300:]}")
    if not ok:
        raise RuntimeError("concat failed")
    for f in seg_files:
        try:
            os.remove(f)
        except OSError:
            pass
    return out_path
