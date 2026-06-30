"""Exhaustive test of the long-form pipeline against the real cached transcript.

Validates: planning (segment count + 20-90 min lengths), YouTube-valid chapters,
title/tags/description, .srt correctness, and a real (short) 16:9 render. Renders to
a temp dir; touches nothing in the live clips/ or longform/ output.
"""
import json
import os
import re
import subprocess
import sys

from pipeline import config, longform, transcribe, util

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # StreamingProject/
proj = json.load(open(os.path.join(ROOT, "clips", "project.json"), encoding="utf-8"))
VOD = proj["vod"]
cfg = config.load_config()
wav = transcribe.wav_path(VOD)
transcript = transcribe.transcribe(cfg, wav, VOD)        # cached
vod_dur = util.probe_duration(VOD)

fails = []
def check(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond:
        fails.append(msg)

print(f"VOD {os.path.basename(VOD)}  dur {vod_dur/60:.1f} min  words {len(transcript['words'])}")
print("\n== PLAN ==")
segs = longform.plan(cfg, transcript, wav, vod_dur)
check(len(segs) >= 1, f"produced >=1 segment (got {len(segs)})")
check(len(segs) <= cfg.lf_count, f"<= lf_count segments ({len(segs)} <= {cfg.lf_count})")

prev_start = -1
for s in segs:
    mins = s.dur / 60.0
    print(f"  seg{s.idx:02d}  {util.hhmmss(s.start)}-{util.hhmmss(s.end)}  {mins:.1f} min  "
          f"score {s.score}  {len(s.chapters)} ch  game={s.game}  | {s.title}")
    check(s.dur > 0, f"seg{s.idx} positive duration")
    check(mins <= cfg.lf_max_min + 0.6, f"seg{s.idx} <= max length ({mins:.1f} <= {cfg.lf_max_min})")
    check(mins >= cfg.lf_min_min * 0.6, f"seg{s.idx} not tiny ({mins:.1f} min)")
    check(0 <= s.start < s.end <= vod_dur + 1, f"seg{s.idx} bounds within VOD")
    check(s.start > prev_start, f"seg{s.idx} chronological & non-overlapping start")
    prev_start = s.start
    # title / tags / desc
    check(bool(s.title.strip()), f"seg{s.idx} has a title")
    check(len(s.title) <= 100, f"seg{s.idx} title <= 100 chars")
    check(len(s.tags) >= 3, f"seg{s.idx} has tags")
    check("Chapters:" in s.description and s.description.strip().startswith(s.title),
          f"seg{s.idx} description well-formed")
    # chapters: YouTube rules — first at 0, >=3, >=10s apart, within [0,dur], sorted
    ch = s.chapters
    ts = [c["t"] for c in ch]
    check(len(ch) >= 3, f"seg{s.idx} >=3 chapters (got {len(ch)})")
    check(ts and ts[0] == 0, f"seg{s.idx} first chapter at 0:00")
    check(ts == sorted(ts), f"seg{s.idx} chapters sorted")
    check(all(0 <= t < s.dur for t in ts), f"seg{s.idx} chapter times within duration")
    check(all(ts[i + 1] - ts[i] >= 10 for i in range(len(ts) - 1)), f"seg{s.idx} chapters >=10s apart")
    check(all(c["label"].strip() for c in ch), f"seg{s.idx} all chapters labeled")

print("\n== SRT ==")
s0 = segs[0]
srt = longform.build_srt(cfg, transcript, s0)
blocks = srt.strip().split("\n\n") if srt.strip() else []
check(len(blocks) >= 1, f"seg01 srt has cues ({len(blocks)})")
ts_re = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})$")
def _sec(g, o): return int(g[o]) * 3600 + int(g[o + 1]) * 60 + int(g[o + 2]) + int(g[o + 3]) / 1000.0
ok_ts, mono, within = True, True, True
last_end = 0.0
for b in blocks:
    rows = b.splitlines()
    if len(rows) < 3:
        ok_ts = False; continue
    m = ts_re.match(rows[1])
    if not m:
        ok_ts = False; continue
    g = m.groups(); a, bb = _sec(g, 0), _sec(g, 4)
    if not (a < bb):
        mono = False
    if a < last_end - 0.06:
        mono = False
    if bb > s0.dur + 1:
        within = False
    last_end = bb
check(ok_ts, "seg01 srt timestamps well-formed")
check(mono, "seg01 srt cues non-overlapping & ordered")
check(within, "seg01 srt cues within segment duration")

print("\n== RENDER (short 16:9 sample) ==")
out_dir = os.path.join(config.WORK_DIR, "preview")
util.ensure_dirs(out_dir)
sample = longform.LongSeg(idx=99, start=s0.start, end=s0.start + 25.0, file="seg99.mp4")
outp = os.path.join(out_dir, "__lftest__.mp4")
from pipeline import branding
wm = branding.render_watermark(cfg)
try:
    longform.render_segment(cfg, VOD, sample, wm, outp)
    size = os.path.getsize(outp)
    pr = subprocess.run([util.ffprobe(), "-v", "error", "-show_entries",
                         "stream=codec_type,width,height", "-of", "json", outp],
                        capture_output=True, text=True)
    info = json.loads(pr.stdout)
    streams = info.get("streams", [])
    kinds = [s["codec_type"] for s in streams]
    vid = next((s for s in streams if s["codec_type"] == "video"), {})
    durp = subprocess.run([util.ffprobe(), "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=nw=1:nk=1", outp], capture_output=True, text=True).stdout.strip()
    dur = float(durp) if durp else 0
    check(size > 10000, f"render produced a file ({size} bytes)")
    check("video" in kinds and "audio" in kinds, f"has video+audio streams ({kinds})")
    check(vid.get("height") == cfg.lf_height, f"height == lf_height ({vid.get('height')})")
    check(abs((vid.get("width", 0) / max(1, vid.get("height", 1))) - 16 / 9) < 0.02,
          f"16:9 aspect ({vid.get('width')}x{vid.get('height')})")
    check(23.5 <= dur <= 26.5, f"duration ~25s ({dur:.2f})")
finally:
    try:
        os.remove(outp)
    except OSError:
        pass

print("\n== PACKAGE ==")
pkg_dir = os.path.join(config.WORK_DIR, "preview", "__lfpkg__")
util.ensure_dirs(pkg_dir)
longform.write_package(cfg, transcript, s0, pkg_dir)
base = os.path.splitext(s0.file)[0]
desc_p = os.path.join(pkg_dir, base + ".description.txt")
srt_p = os.path.join(pkg_dir, base + ".srt")
check(os.path.exists(desc_p) and os.path.getsize(desc_p) > 0, "description.txt written")
check(os.path.exists(srt_p) and os.path.getsize(srt_p) > 0, "srt written")
desc = open(desc_p, encoding="utf-8").read()
check("0:00" in desc, "description starts chapters at 0:00")
for f in (desc_p, srt_p):
    try:
        os.remove(f)
    except OSError:
        pass
try:
    os.rmdir(pkg_dir)
except OSError:
    pass

print("\n== RESULT ==")
print("ALL PASS" if not fails else f"{len(fails)} FAILURES:\n  - " + "\n  - ".join(fails))
sys.exit(1 if fails else 0)
