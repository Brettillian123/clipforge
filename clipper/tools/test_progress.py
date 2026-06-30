"""Verify render_spec streams real ffmpeg progress to a callback.

Renders a tiny 2s clip and records the progress fractions the callback receives.
Confirms they are monotonic-ish and climb toward ~1.0. Writes to a temp file.
"""
import json
import os
import time

from pipeline import config, branding, render, util
from pipeline.project import ClipSpec

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # StreamingProject/
proj = json.load(open(os.path.join(ROOT, "clips", "project.json"), encoding="utf-8"))
vod = proj["vod"]
cfg = config.load_config()
wm = branding.render_watermark(cfg)

spec = ClipSpec(id="__progtest__", rank=999,
                segments=[{"start": 11171.0, "end": 11173.0}],
                captions_enabled=False)
out = os.path.join(config.WORK_DIR, "preview", "__progtest__.mp4")
util.ensure_dirs(os.path.dirname(out))

samples = []
t0 = time.time()
def cb(frac):
    samples.append((round(time.time() - t0, 2), round(frac, 3)))

print("rendering 2s clip with progress callback ...")
render.render_spec(cfg, vod, spec, {"words": []}, wm, out, include_overlays=False, progress_cb=cb)
dt = time.time() - t0
print(f"done in {dt:.1f}s, output {os.path.getsize(out)} bytes")
print(f"progress callbacks fired: {len(samples)}")
if samples:
    print("first:", samples[0], " last:", samples[-1], " max:", max(f for _, f in samples))
    print("sample trail:", samples[:: max(1, len(samples) // 8)])
fracs = [f for _, f in samples]
monotonic = all(b >= a - 0.001 for a, b in zip(fracs, fracs[1:]))
print("monotonic non-decreasing:", monotonic)
try:
    os.remove(out)
except OSError:
    pass
print("RESULT:", "OK" if (len(samples) >= 2 and monotonic) else "FAIL")
