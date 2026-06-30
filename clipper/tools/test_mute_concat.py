"""Verify per-part audio mute renders silent audio and concats cleanly.

Renders a 2-part clip: part 1 with real VOD audio, part 2 with mute=True.
Then ffprobes the result and runs volumedetect on each half to confirm part 2
is actually silent while part 1 is not. Writes to a temp file (no clip touched).
"""
import json
import os
import subprocess
import sys

from pipeline import config, branding, render, util
from pipeline.project import ClipSpec

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # StreamingProject/
proj = json.load(open(os.path.join(ROOT, "clips", "project.json"), encoding="utf-8"))
vod = proj["vod"]
cfg = config.load_config()
wm = branding.render_watermark(cfg)

spec = ClipSpec(
    id="__mutetest__", rank=999,
    segments=[{"start": 11171.0, "end": 11174.0},
              {"start": 11500.0, "end": 11503.0, "mute": True}],
    captions_enabled=False,
)
out = os.path.join(config.WORK_DIR, "preview", "__mutetest__.mp4")
util.ensure_dirs(os.path.dirname(out))
print("rendering 2-part clip (part2 muted) ...")
render.render_spec(cfg, vod, spec, {"words": []}, wm, out, include_overlays=True)
print("rendered ->", out, os.path.getsize(out), "bytes")

def probe(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")

streams = probe([util.ffprobe(), "-v", "error", "-show_entries",
                 "stream=codec_type", "-of", "csv=p=0", out]).stdout.split()
dur = probe([util.ffprobe(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", out]).stdout.strip()

def mean_db(ss, t):
    r = probe([util.ffmpeg(), "-hide_banner", "-ss", str(ss), "-t", str(t), "-i", out,
               "-af", "volumedetect", "-f", "null", "-"])
    for line in r.stderr.splitlines():
        if "mean_volume" in line:
            return line.split("mean_volume:")[1].strip()
    return "?"

print("streams:", streams)
print("duration:", dur)
print("part1 (0-3s) mean_volume:", mean_db(0.2, 2.5))
print("part2 (3-6s) mean_volume:", mean_db(3.2, 2.5))
try:
    os.remove(out)
except OSError:
    pass
print("OK")
