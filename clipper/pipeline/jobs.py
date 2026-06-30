"""The clip-making pipeline as a reusable function (so both clip.py and the dashboard
can run it). Turns a VOD into rendered vertical clips + a project.json.
"""
from __future__ import annotations

import copy
import os
import subprocess

from . import branding, config, detect, project as proj, render, transcribe, util
from .util import log


def ensure_wav(cfg: config.Config, vod: str, limit_secs=None):
    """Extract (and cache) the 16 kHz mono WAV the pipeline scores + transcribes."""
    util.ensure_dirs(config.WORK_DIR)
    stem = os.path.splitext(os.path.basename(vod))[0]
    full = os.path.join(config.WORK_DIR, f"{stem}_16k.wav")
    if not os.path.exists(full) and os.path.exists(os.path.join(config.WORK_DIR, "stream1_16k.wav")):
        full = os.path.join(config.WORK_DIR, "stream1_16k.wav")
    if not os.path.exists(full):
        log(f"[audio] extracting 16k wav -> {full}")
        subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                        "-i", vod, "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", full], check=True)
    if not limit_secs:
        return full, ""
    sliced = os.path.join(config.WORK_DIR, f"{stem}_16k.limit{limit_secs}.wav")
    if not os.path.exists(sliced):
        log(f"[audio] slicing first {limit_secs}s -> {sliced}")
        subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                        "-t", str(limit_secs), "-i", full, "-c", "copy", sliced], check=True)
    return sliced, f"limit{limit_secs}"


def slice_transcript(t, limit):
    return {**t, "words": [w for w in t["words"] if w["start"] < limit],
            "segments": [s for s in t["segments"] if s["start"] < limit]}


def clips_dir_for(vod: str) -> str:
    """Where a VOD's clips live: a sibling '<stem> - clips' folder (per-VOD, no collisions)."""
    stem = os.path.splitext(os.path.basename(vod))[0]
    return os.path.join(os.path.dirname(os.path.abspath(vod)), f"{stem} - clips")


def longform_dir_for(vod: str) -> str:
    """Where a VOD's long-form videos live: a sibling '<stem> - longform' folder — SEPARATE from
    that VOD's clips, so the two outputs never mix."""
    stem = os.path.splitext(os.path.basename(vod))[0]
    return os.path.join(os.path.dirname(os.path.abspath(vod)), f"{stem} - longform")


READY_SUBDIR = "Ready to post"


def ready_dir(out_dir: str) -> str:
    return os.path.join(out_dir, READY_SUBDIR)


def review_strip(cfg, vod: str, thumb_w: int = 320) -> str | None:
    """Build + cache a horizontal filmstrip of evenly-spaced thumbnails across the WHOLE VOD — the
    'scene bar' under the review player (hover to read the time, click to seek). Frames are grabbed
    with fast input-seek (cheap even on a 14 GB file) in parallel, then tiled into one JPG. Cached
    per VOD so re-opening is instant. Returns the JPG path, or None."""
    import concurrent.futures as cf
    from PIL import Image
    dur = util.probe_duration(vod)
    if dur <= 1:
        return None
    n = max(12, min(40, round(dur / 360)))               # ~1 thumb / 6 min, clamped 12..40
    thumb_h = round(thumb_w * 9 / 16)
    stem = os.path.splitext(os.path.basename(vod))[0]
    pdir = os.path.join(config.WORK_DIR, "preview")
    util.ensure_dirs(pdir)
    out = os.path.join(pdir, f"strip_{stem}_{thumb_w}.jpg")   # thumb_w in name -> resize busts cache
    try:
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(vod):
            return out
    except OSError:
        pass
    tdir = os.path.join(pdir, f"strip_{stem}")
    util.ensure_dirs(tdir)

    def grab(i):
        t = dur * (i + 0.5) / n                           # sample the middle of each segment
        fp = os.path.join(tdir, f"{i:03d}.jpg")
        subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{t:.2f}", "-i", vod, "-frames:v", "1",
                        "-vf", f"scale={thumb_w}:{thumb_h}:force_original_aspect_ratio=increase,"
                               f"crop={thumb_w}:{thumb_h}", "-q:v", "5", fp],
                       capture_output=True, text=True)
        return i, (fp if os.path.exists(fp) else None)

    got = {}
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for i, fp in ex.map(grab, range(n)):
            if fp:
                got[i] = fp
    if not got:
        return None
    strip = Image.new("RGB", (thumb_w * n, thumb_h), (13, 11, 20))
    for i, fp in got.items():
        try:
            strip.paste(Image.open(fp).convert("RGB"), (i * thumb_w, 0))
        except Exception:  # noqa: BLE001
            pass
    strip.save(out, quality=80)
    return out


def publish_clip(out_dir: str, clip_id: str, src: str) -> str | None:
    """Copy a finished clip into the clean 'Ready to post' folder as <clip_id>.mp4, REPLACING any
    previous version (so re-downloading never makes duplicates / leaves old versions). Also writes a
    matching <clip_id>.jpg cover = the clip's FIRST frame, so the posted thumbnail (and any manual
    cover upload) shows whatever you placed at 0s, exactly as the video opens."""
    import shutil
    dst_dir = ready_dir(out_dir)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"{clip_id}.mp4")
    try:
        shutil.copy2(src, dst)            # copy2 onto an existing file overwrites it in place
    except OSError as e:
        log(f"[publish] could not copy {src} -> {dst}: {e}")
        return None
    _write_cover(dst, os.path.join(dst_dir, f"{clip_id}.jpg"))
    return dst


def _write_cover(mp4: str, jpg: str) -> None:
    """Grab the very first frame of the rendered clip as a high-quality JPG cover (overwrites)."""
    try:
        subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                        "-i", mp4, "-frames:v", "1", "-q:v", "2", jpg],
                       check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as e:  # noqa: BLE001
        log(f"[publish] cover frame failed for {mp4}: {e}")


def make_clips(cfg: config.Config, vod: str, out_dir: str | None = None, count: int | None = None,
               ai: bool = False, limit_secs=None, do_render: bool = True, force_transcribe: bool = False,
               progress_cb=None) -> dict | None:
    """VOD -> detected clips -> rendered mp4s + project.json. Returns the project dict (or None
    if no clips were found). progress_cb(phase:str, frac:float|None) reports coarse progress."""
    def prog(phase, frac=None):
        log(f"[job] {phase}" + (f" ({int(frac*100)}%)" if frac is not None else ""))
        if progress_cb:
            progress_cb(phase, frac)

    count = count or cfg.clip_count
    out_dir = out_dir or clips_dir_for(vod)
    vod = os.path.abspath(vod)
    util.ensure_dirs(out_dir, config.WORK_DIR)

    prog("Extracting audio", 0.0)
    wav, tag = ensure_wav(cfg, vod, limit_secs)
    prog("Transcribing on GPU")
    transcript = transcribe.transcribe(cfg, wav, vod, force=force_transcribe, tag=tag)
    if limit_secs:
        transcript = slice_transcript(transcript, limit_secs)

    prog("Finding the best moments")
    cands = detect.detect(cfg, transcript, wav)
    if not cands:
        prog("No clips found", 1.0)
        return None
    if ai:
        try:
            from . import rerank
            cands = rerank.rerank(cfg, cands, count)
        except Exception as e:  # noqa: BLE001
            log(f"[rerank] failed ({e}); using local order.")
    selected = cands[:count]

    project = proj.build_project(cfg, vod, selected, ai)
    for c in project["clips"]:
        c["file"] = f"{c['id']}.mp4"

    if do_render:
        if util.ensure_fonts() == 0:
            log("[fonts] WARNING: no Poppins for libass; captions may fall back.")
        watermark = branding.render_watermark(cfg, force=True)
        from . import server as _srv          # _export_sig / EXPORT_FIELDS (lazy: avoids an import cycle)
        n = len(project["clips"])
        for i, c in enumerate(project["clips"]):
            prog(f"Rendering clip {i + 1} of {n}", i / max(1, n))
            spec = proj.spec_from_dict(c, cfg)
            base_out = os.path.join(out_dir, spec.file)                 # clipNN.mp4 = editable BASE preview
            try:
                # The studio plays the base clip and draws elements (e.g. the titlecard) live as HTML on
                # top, so the base must NOT have them baked in — otherwise they'd show DOUBLED in preview.
                render.render_spec(cfg, vod, spec, transcript, watermark, base_out, include_overlays=False)
                c["render_status"], c["needs_render"], c["file"] = "done", False, spec.file
                # full burn (titlecard + chat + elements) -> the clean 'Ready to post' copy
                export_out = os.path.join(out_dir, f"{spec.id}.export.mp4")
                render.render_spec(cfg, vod, spec, transcript, watermark, export_out, include_overlays=True)
                publish_clip(out_dir, c["id"], export_out)
                c["export_status"], c["export_file"] = "done", f"{spec.id}.export.mp4"
                c["export_sig"] = _srv._export_sig(c)
                c["export_spec"] = copy.deepcopy({k: c.get(k) for k in _srv.EXPORT_FIELDS})
            except Exception as e:  # noqa: BLE001
                c["render_status"], c["render_error"] = "error", str(e)
                log(f"  RENDER FAILED {spec.id}: {e}")

    proj.save_project(out_dir, project)
    _write_md(out_dir, project)
    prog("Done", 1.0)
    return project


def make_longform(cfg: config.Config, vod: str, out_dir: str | None = None,
                  progress_cb=None, force_transcribe: bool = False) -> list | None:
    """VOD -> planned long-form videos rendered 16:9 into '<stem> - longform' (each with a sidecar
    .srt, YouTube description + chapters). Fully independent of the clip pipeline. Returns the list of
    segment dicts, or None if nothing worth making. progress_cb(phase, frac) reports coarse progress."""
    from . import longform
    def prog(phase, frac=None):
        log(f"[longform-job] {phase}" + (f" ({int(frac * 100)}%)" if frac is not None else ""))
        if progress_cb:
            progress_cb(phase, frac)

    vod = os.path.abspath(vod)
    out_dir = out_dir or longform_dir_for(vod)
    util.ensure_dirs(out_dir, config.WORK_DIR)

    prog("Extracting audio", 0.0)
    wav, _ = ensure_wav(cfg, vod)
    prog("Transcribing on GPU")
    transcript = transcribe.transcribe(cfg, wav, vod, force=force_transcribe)

    prog("Planning long-form videos")
    segs = longform.plan(cfg, transcript, wav, util.probe_duration(vod))
    if not segs:
        prog("No long-form segments found", 1.0)
        return None

    watermark = branding.render_watermark(cfg, force=True) if getattr(cfg, "lf_watermark", False) else ""
    n = len(segs)
    for i, s in enumerate(segs):
        prog(f"Rendering long-form {i + 1} of {n}", i / max(1, n))
        longform.write_package(cfg, transcript, s, out_dir)
        try:
            longform.render_segment(cfg, vod, s, watermark, os.path.join(out_dir, s.file))
        except Exception as e:  # noqa: BLE001
            log(f"  LONGFORM RENDER FAILED seg{s.idx:02d}: {e}")
    longform.write_manifest(out_dir, vod, segs)
    longform.write_md(out_dir, vod, segs)
    prog("Done", 1.0)
    return [s.to_dict() for s in segs]


def _write_md(out_dir, project):
    lines = [f"# Clips from {os.path.basename(project['vod'])}", "",
             "Audio is the raw clip audio (no added music). Use the dashboard to trim/edit; "
             "copy the per-platform caption below when you post.", ""]
    for c in project["clips"]:
        seg = c["segments"][0]
        dur = sum(s["end"] - s["start"] for s in c["segments"])
        lines.append(f"## #{c['rank']}  -  {c.get('file','')}")
        lines.append(f"- **When:** {util.hhmmss(seg['start'])}  ({dur:.1f}s)   **Score:** {c['score']}")
        lines.append(f"- **Keywords:** {', '.join(c.get('keywords', [])) or '-'}")
        for p, md in c["metadata"].items():
            lines.append(f"- **{p}** — title: `{md['title']}`")
        lines.append("")
    with open(os.path.join(out_dir, "clips.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
