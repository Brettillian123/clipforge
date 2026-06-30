#!/usr/bin/env python
"""ClipForge Long-form - turn a Twitch VOD into 20-90 min 16:9 YouTube videos.

  python longform.py "C:\\...\\Stream1.mp4"            # plan + render all (16:9, chapters, .srt, descriptions)
  python longform.py Stream1.mp4 --plan-only           # just plan + write the review sheet (fast, no render)
  python longform.py Stream1.mp4 --count 4 --height 720
  python longform.py Stream1.mp4 --no-watermark

Output: a `longform/` folder next to the VOD with seg##.mp4 + .srt + .description.txt,
plus longform.json (manifest) and longform.md (review sheet). See LONGFORM_PLAN.md.
Reuses the cached transcript from clip.py if present (re-transcribes only if missing).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline import config, longform, transcribe, util  # noqa: E402
from pipeline.util import log  # noqa: E402

DEFAULT_VOD = os.path.join(os.path.expanduser("~"), "Videos", "Stream1.mp4")  # fallback; pass a VOD path instead


def main() -> int:
    cfg = config.load_config()
    ap = argparse.ArgumentParser(description="VOD -> long-form 16:9 YouTube videos")
    ap.add_argument("vod", nargs="?", default=DEFAULT_VOD)
    ap.add_argument("--plan-only", action="store_true", help="plan + review sheet only (no render)")
    ap.add_argument("--count", type=int, default=cfg.lf_count, help="max videos to keep")
    ap.add_argument("--height", type=int, default=cfg.lf_height, help="output height (e.g. 1080 or 720)")
    ap.add_argument("--no-watermark", action="store_true")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default=cfg.device)
    ap.add_argument("--model", default=cfg.model)
    ap.add_argument("--force-transcribe", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = cfg.with_overrides(device=args.device, model=args.model, lf_count=args.count,
                             lf_height=args.height, lf_watermark=not args.no_watermark)
    vod = os.path.abspath(args.vod)
    if not os.path.exists(vod):
        log(f"ERROR: VOD not found: {vod}")
        return 2
    out_dir = args.out or os.path.join(os.path.dirname(vod), "longform")
    util.ensure_dirs(out_dir, config.WORK_DIR)

    # reuse clip.py's wav + transcript cache (only transcribes if missing)
    import clip
    wav, tag = clip.ensure_wav(cfg, vod, None)
    transcript = transcribe.transcribe(cfg, wav, vod, force=args.force_transcribe, tag=tag)

    longform.build(cfg, vod, transcript, wav, out_dir, plan_only=args.plan_only)
    log(f"\nDone -> {out_dir}\n  longform.md (review sheet), longform.json (manifest)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
