#!/usr/bin/env python
"""ClipForge - turn a Twitch VOD into ready-to-post vertical clips.

  python clip.py "C:\\...\\Stream1.mp4"                 # local detection, render, write project.json
  python clip.py Stream1.mp4 --ai                       # Claude re-rank + captions (needs API key)
  python clip.py Stream1.mp4 --limit-secs 900 --count 3 # fast test on the first 15 min
  python clip.py Stream1.mp4 --dry-run                  # detect + project.json only, no render
  python clip.py Stream1.mp4 --dashboard                # render then open the review dashboard
  python dashboard.py                                   # just open the dashboard on an existing job

All tunables: pipeline/config.py.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline import branding, config, detect, project as proj, render, transcribe, util  # noqa: E402
from pipeline.util import log  # noqa: E402

DEFAULT_VOD = os.path.join(os.path.expanduser("~"), "Videos", "Stream1.mp4")  # fallback; pass a VOD path instead


def ensure_wav(cfg: config.Config, vod: str, limit_secs):
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


def main() -> int:
    cfg = config.load_config()
    ap = argparse.ArgumentParser(description="VOD -> vertical clips")
    ap.add_argument("vod", nargs="?", default=DEFAULT_VOD)
    ap.add_argument("--count", type=int, default=cfg.clip_count)
    ap.add_argument("--ai", action="store_true", help="Claude re-rank (needs an API key)")
    ap.add_argument("--limit-secs", type=int, default=None)
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default=cfg.device)
    ap.add_argument("--model", default=cfg.model)
    ap.add_argument("--no-autodetect-cam", action="store_true")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--force-transcribe", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="detect + project.json only")
    ap.add_argument("--dashboard", action="store_true", help="open the review dashboard when done")
    ap.add_argument("--keep", type=int, default=cfg.keep_for_rerank)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = cfg.with_overrides(device=args.device, model=args.model, clip_count=args.count,
                             keep_for_rerank=args.keep, autodetect_cam=not args.no_autodetect_cam,
                             ai_enabled=args.ai)
    vod = os.path.abspath(args.vod)
    if not os.path.exists(vod):
        log(f"ERROR: VOD not found: {vod}")
        return 2
    out_dir = args.out or os.path.join(os.path.dirname(vod), "clips")
    util.ensure_dirs(out_dir, config.WORK_DIR)

    wav, tag = ensure_wav(cfg, vod, args.limit_secs)
    transcript = transcribe.transcribe(cfg, wav, vod, force=args.force_transcribe, tag=tag)
    if args.limit_secs:
        transcript = slice_transcript(transcript, args.limit_secs)

    log(f"[detect] scoring {len(transcript['words'])} words ...")
    cands = detect.detect(cfg, transcript, wav)
    if not cands:
        log("[detect] no candidates found.")
        return 1

    if args.ai:
        from pipeline import rerank
        try:
            cands = rerank.rerank(cfg, cands, args.count)
        except Exception as e:
            log(f"[rerank] failed ({e}); using local order.")
    selected = cands[:args.count]

    project = proj.build_project(cfg, vod, selected, args.ai)
    for c in project["clips"]:
        c["file"] = f"{c['id']}.mp4"
        if args.no_captions:
            c["captions_enabled"] = False

    log(f"[detect] {len(selected)} clips:")
    for c in project["clips"]:
        p0 = next(iter(c["metadata"]))
        dur = sum(s["end"] - s["start"] for s in c["segments"])
        log(f"  #{c['rank']:2d} score {c['score']:5.1f}  {util.hhmmss(c['segments'][0]['start'])}  "
            f"{dur:4.1f}s  {c['metadata'][p0]['title']}")

    if not args.dry_run:
        if util.ensure_fonts() == 0 and not args.no_captions:
            log("[fonts] WARNING: no Poppins for libass; captions may fall back.")
        watermark = branding.render_watermark(cfg, force=True)
        for c in project["clips"]:
            spec = proj.spec_from_dict(c, cfg)
            outp = os.path.join(out_dir, spec.file)
            try:
                render.render_spec(cfg, vod, spec, transcript, watermark, outp)
                c["render_status"], c["needs_render"] = "done", False
                log(f"  rendered {spec.file}")
            except Exception as e:
                c["render_status"], c["render_error"] = "error", str(e)
                log(f"  RENDER FAILED {spec.id}: {e}")

    proj.save_project(out_dir, project)
    _write_md(out_dir, project)
    log(f"\nDone -> {out_dir}\n  project.json (dashboard), clips.md (review sheet)")
    log(f"Open the dashboard:  python dashboard.py \"{vod}\"")

    if args.dashboard:
        from pipeline import server
        server.serve(cfg, out_dir)
    return 0


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
            lines.append(f"    - caption: {md['caption']}")
            lines.append(f"    - hashtags: {' '.join(md['hashtags'])}")
        lines.append(f"- **Transcript:** {c.get('transcript','')[:300]}")
        lines.append("")
    with open(os.path.join(out_dir, "clips.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
