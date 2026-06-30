"""Long-form: turn a VOD into 20-90 min, 16:9, chaptered/captioned YouTube videos.

See LONGFORM_PLAN.md. We reuse the cached transcript + audio engagement signals to
split the VOD into coherent session blocks, trim dead air at the edges, auto-generate
YouTube chapters, a title/description/tags, and a sidecar .srt, then render each block
16:9 (keeping the stream's own facecam+gameplay layout) via the cancellable ffmpeg runner.

Pure-stdlib + numpy (no new deps). The heavy clip pipeline is untouched.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import numpy as np

from . import audio, config, detect, meta, render, util
from .util import log


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
@dataclass
class LongSeg:
    idx: int
    start: float                       # VOD seconds
    end: float
    score: float = 0.0
    title: str = ""
    description: str = ""
    tags: list = field(default_factory=list)
    game: object = None
    keywords: list = field(default_factory=list)
    chapters: list = field(default_factory=list)   # [{"t": rel_seconds, "label": str}]
    file: str = ""

    @property
    def dur(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {"idx": self.idx, "start": round(self.start, 2), "end": round(self.end, 2),
                "dur": round(self.dur, 2), "score": self.score, "title": self.title,
                "description": self.description, "tags": self.tags, "game": self.game,
                "keywords": self.keywords, "chapters": self.chapters, "file": self.file}


# --------------------------------------------------------------------------- #
# signals
# --------------------------------------------------------------------------- #
def _signals(cfg: config.Config, transcript: dict, wav: str) -> dict:
    sr, x = audio.load_pcm(wav)
    feats = audio.per_second_features(x, sr, 1.0)        # 1s bins -> index == second
    n = feats["n_bins"]
    sec_db = feats["sec_db"]
    ftimes, fdb = audio.frame_rms_db(x, sr, cfg.frame_ms, cfg.hop_ms)
    ref = audio.speech_ref_db(fdb, cfg.speech_pct)
    excite = (sec_db - ref).astype(np.float64)           # per-second loudness vs typical speech

    talk = np.zeros(n)
    laugh = np.zeros(n)
    for w in transcript.get("words", []):
        s = int(w["start"])
        if 0 <= s < n:
            talk[s] += 1.0
            if detect._clean(w["word"]) in detect.LAUGH_TOKENS:
                laugh[s] += 1.0
    active = ((talk > 0) | (excite > 2.0)).astype(np.float64)     # content present this second
    interest = np.maximum(excite, 0.0) + 1.5 * laugh + 0.5 * talk
    return {"n": n, "dur": float(n), "excite": excite, "talk": talk,
            "laugh": laugh, "active": active, "interest": interest}


# --------------------------------------------------------------------------- #
# segmentation (-> 20-90 min minute-blocks)
# --------------------------------------------------------------------------- #
def _minute_activity(sig: dict) -> np.ndarray:
    n = sig["n"]
    active = sig["active"]
    minutes = max(1, int(math.ceil(n / 60.0)))
    act = np.zeros(minutes)
    for m in range(minutes):
        seg = active[m * 60:(m + 1) * 60]
        act[m] = float(seg.mean()) if len(seg) else 0.0
    return act


def _split_at_breaks(cfg: config.Config, act_min: np.ndarray) -> list:
    """Content minute-blocks separated by runs of >= lf_break_min DEAD minutes."""
    minutes = len(act_min)
    dead = act_min < cfg.lf_dead_frac
    breaks = []
    i = 0
    while i < minutes:
        if dead[i]:
            j = i
            while j < minutes and dead[j]:
                j += 1
            if (j - i) >= cfg.lf_break_min:
                breaks.append((i, j))
            i = j
        else:
            i += 1
    blocks, cur = [], 0
    for (a, b) in breaks:
        if a > cur:
            blocks.append((cur, a))
        cur = b
    if cur < minutes:
        blocks.append((cur, minutes))
    # trim dead minutes at the block edges
    out = []
    for (a, b) in blocks:
        while a < b and dead[a]:
            a += 1
        while b > a and dead[b - 1]:
            b -= 1
        if b > a:
            out.append((a, b))
    return out


def _enforce_length(cfg: config.Config, blocks: list, act_min: np.ndarray) -> list:
    minn, maxn, tgt = cfg.lf_min_min, cfg.lf_max_min, cfg.lf_target_min
    out, stack = [], list(blocks)
    guard = 0
    while stack:
        guard += 1
        if guard > 1000:
            break
        a, b = stack.pop(0)
        if (b - a) <= maxn:
            out.append((a, b))
            continue
        lo = a + int(minn)
        hi = min(b - int(minn), a + int(maxn))
        if hi <= lo:
            cut = a + int(maxn)
        else:
            target = a + tgt
            best, bestcost = lo, 1e18
            for m in range(lo, hi):
                cost = act_min[m] + 0.0008 * abs(m - target)   # quiet + near target
                if cost < bestcost:
                    bestcost, best = cost, m
            cut = best
        out.append((a, cut))
        stack.insert(0, (cut, b))
    out.sort()
    # merge / drop under-length blocks
    merged = []
    for blk in out:
        if (blk[1] - blk[0]) >= minn:
            merged.append(blk)
        elif (merged and (blk[0] - merged[-1][1]) <= cfg.lf_merge_gap_min
              and (blk[1] - merged[-1][0]) <= maxn):
            merged[-1] = (merged[-1][0], blk[1])
        # else: too short and not mergeable -> drop
    return merged


def _fine_trim(cfg: config.Config, sig: dict, start: float, end: float) -> tuple:
    """Shave dead seconds at the very start/end so the video opens on action."""
    active = sig["active"]
    n = sig["n"]
    s, e = int(start), min(int(end), n)
    while s < e - 60 and float(active[s:s + 10].mean()) < 0.2:
        s += 1
    while e > s + 60 and float(active[max(0, e - 10):e].mean()) < 0.2:
        e -= 1
    return float(s), float(e)


# --------------------------------------------------------------------------- #
# enrichment (chapters / title / tags / description)
# --------------------------------------------------------------------------- #
# generic filler/contractions to keep OUT of titles & chapter labels (apostrophes stripped)
_FILLER = {s.replace("'", "") for s in detect.STOPWORDS} | {
    "were", "weve", "youre", "theyre", "thats", "theres", "heres", "whats", "hes", "shes",
    "dont", "didnt", "cant", "wont", "isnt", "arent", "wasnt", "wouldnt", "couldnt", "shouldnt",
    "aint", "gonna", "wanna", "gotta", "kinda", "sorta", "lemme", "gimme", "lets", "its", "ive",
    "yeah", "yep", "yup", "nope", "okay", "alright", "right", "really", "actually", "literally",
    "basically", "probably", "maybe", "honestly", "obviously", "definitely", "seriously",
    "stuff", "thing", "things", "guys", "dude", "bro", "bruh", "like", "just", "know", "think",
    "mean", "want", "need", "good", "nice", "cool", "well", "look", "looking", "going", "come",
    "coming", "gets", "getting", "said", "says", "tell", "told", "feel", "feels", "kind", "sort",
    "time", "times", "cause", "because", "there", "here", "that", "this", "they", "them", "then",
    "than", "over", "very", "much", "more", "some", "what", "when", "where", "which", "while",
    "with", "your", "yours", "about", "into", "back", "down", "still", "even", "also", "every",
    "little", "whole", "everyone", "everybody", "somebody", "something", "anything", "nothing",
    "everything", "yourself", "gonna", "yeah", "wait", "stop", "okay", "stuff",
    "have", "game", "gaming", "give", "given", "doing", "play", "playing", "played", "thank",
    "thanks", "should", "keep", "last", "stand", "call", "called", "anyone", "anybody", "looked",
    "wonder", "wondering", "place", "places", "make", "makes", "made", "take", "takes", "took",
    "turn", "turns", "talk", "talking", "talked", "hello", "join", "joined", "watch", "watching",
    "watched", "early", "inside", "tips", "chat", "second", "third", "first", "display", "level",
}


def _kw_clean(word: str) -> str:
    return detect._clean(word).replace("'", "")


def _global_counts(transcript: dict):
    """(token -> count over the whole VOD, total) for IDF weighting of keywords."""
    from collections import Counter
    c = Counter()
    for w in transcript.get("words", []):
        t = _kw_clean(w["word"])
        if len(t) > 3 and t not in _FILLER and t not in detect.LAUGH_TOKENS:
            c[t] += 1
    return c, max(1, sum(c.values()))


def _content_keywords(transcript: dict, a: float, b: float, k: int, gc=None, total: int = 1) -> list:
    """Top DISTINCTIVE keywords in [a,b): TF-in-window x IDF-over-the-VOD, so generic
    filler (frequent everywhere) is downweighted and topic/game words surface."""
    from collections import Counter
    tf = Counter()
    for w in transcript.get("words", []):
        if a <= w["start"] < b:
            t = _kw_clean(w["word"])
            if len(t) > 3 and t not in _FILLER and t not in detect.LAUGH_TOKENS:
                tf[t] += 1
    scored = []
    for t, f in tf.items():
        g = gc.get(t, 0) if gc else f
        if g < 2:                          # skip near-hapax (likely noise / mis-hears)
            continue
        idf = math.log(1.0 + total / (1.0 + g))
        scored.append((f * idf, f, t))
    scored.sort(reverse=True)
    return [t for _, _, t in scored[:k]]


def _snap_sentence(transcript: dict, abs_t: float, window: float = 18.0) -> float:
    best, bestd = abs_t, window
    for sg in transcript.get("segments", []):
        for cand in (sg["start"], sg["end"]):
            d = abs(cand - abs_t)
            if d < bestd:
                bestd, best = d, cand
    return best


def _chapters(cfg: config.Config, transcript: dict, sig: dict, seg: LongSeg, gc=None, total: int = 1) -> list:
    interest = sig["interest"]
    s, e = int(seg.start), min(int(seg.end), sig["n"])
    dur = max(1, e - s)
    step = max(60, int(cfg.lf_chapter_min * 60))
    n_ch = max(3, min(int(dur / step) + 1, 20))
    pts = [int(round(k * dur / n_ch)) for k in range(n_ch)]   # evenly spaced, first at 0
    chapters, used = [], []
    for rel in pts:
        if rel <= 0:
            t, label = 0, "Intro"
        else:
            lo, hi = max(1, rel - 45), min(dur - 2, min(rel + 45, len(interest) - s))
            if hi <= lo:
                t = rel
            else:
                t = lo + int(np.argmax(interest[s + lo:s + hi]))   # snap to a local peak
            t = int(max(0, min(dur - 1, _snap_sentence(transcript, seg.start + t) - seg.start)))
            kws = _content_keywords(transcript, seg.start + t, seg.start + min(dur, t + step),
                                    cfg.lf_chapter_label_words, gc, total)
            label = " ".join(kws).title() or "Moment"
        if any(abs(t - u) < 10 for u in used):       # YouTube: chapters >= 10s apart
            continue
        used.append(t)
        chapters.append({"t": t, "label": label})
    chapters.sort(key=lambda c: c["t"])
    if not chapters or chapters[0]["t"] != 0:
        chapters.insert(0, {"t": 0, "label": "Intro"})
    clean = [chapters[0]]                             # guarantee strictly increasing & >= 10s apart
    for c in chapters[1:]:
        if c["t"] - clean[-1]["t"] >= 10:
            clean.append(c)
    chapters = clean
    if len(chapters) < 3:                            # YouTube needs >= 3
        third = max(10, int(dur / 3))
        chapters = [{"t": 0, "label": "Intro"},
                    {"t": min(third, max(10, dur - 11)), "label": "Part 2"},
                    {"t": min(2 * third, dur - 1), "label": "Part 3"}]
    return chapters


def _title(cfg: config.Config, seg: LongSeg) -> str:
    g = seg.game or "Stream"
    kw = " ".join(seg.keywords[:3]).title().strip()
    title = f"{g} Highlights" if not kw else f"{g} — {kw}"
    return title[:100].strip(" —")


def _tags(cfg: config.Config, seg: LongSeg) -> list:
    raw = (["gaming", cfg.wm_handle, "twitch", "live stream", "gameplay", "funny moments"])
    if seg.game:
        raw.insert(0, seg.game)
    raw += seg.keywords[:4]
    seen, out = set(), []
    for t in raw:
        t = str(t).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:15]


def _description(cfg: config.Config, seg: LongSeg) -> str:
    lines = [seg.title, "",
             f"Live on Twitch: https://twitch.tv/{cfg.wm_handle}", "",
             "Chapters:"]
    for ch in seg.chapters:
        lines.append(f"{util.hhmmss(ch['t'])} {ch['label']}")
    lines += ["", " ".join("#" + str(t).replace(" ", "") for t in seg.tags[:8])]
    return "\n".join(lines)


def _enrich(cfg: config.Config, transcript: dict, sig: dict, seg: LongSeg, gc=None, total: int = 1) -> None:
    text = " ".join(w["word"].strip() for w in transcript.get("words", [])
                    if w["start"] >= seg.start and w["start"] < seg.end)
    seg.keywords = _content_keywords(transcript, seg.start, seg.end, 8, gc, total)
    seg.game = meta.guess_game(text, seg.keywords)
    seg.chapters = _chapters(cfg, transcript, sig, seg, gc, total)
    seg.title = _title(cfg, seg)
    seg.tags = _tags(cfg, seg)
    seg.description = _description(cfg, seg)


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def plan(cfg: config.Config, transcript: dict, wav: str, vod_duration: float) -> list:
    sig = _signals(cfg, transcript, wav)
    gc, total = _global_counts(transcript)
    act_min = _minute_activity(sig)
    blocks = _enforce_length(cfg, _split_at_breaks(cfg, act_min), act_min)
    floor = cfg.lf_min_min * 60 * 0.6                 # tolerate a little under after fine-trim
    segs = []
    for (a, b) in blocks:
        start = float(a * 60)
        end = float(min(b * 60, sig["dur"], vod_duration))
        start, end = _fine_trim(cfg, sig, start, end)
        if (end - start) < floor:
            continue
        sc = float(np.mean(sig["interest"][int(start):int(end)])) if end > start else 0.0
        seg = LongSeg(idx=0, start=round(start, 2), end=round(end, 2), score=round(sc * 10, 1))
        _enrich(cfg, transcript, sig, seg, gc, total)
        segs.append(seg)
    segs.sort(key=lambda s: s.score, reverse=True)
    segs = segs[:cfg.lf_count]
    segs.sort(key=lambda s: s.start)                  # chronological for output/numbering
    for i, s in enumerate(segs, 1):
        s.idx = i
        s.file = f"seg{i:02d}.mp4"
    return segs


# --------------------------------------------------------------------------- #
# .srt sidecar
# --------------------------------------------------------------------------- #
def _srt_ts(s: float) -> str:
    ms = int(round(max(0.0, s) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def build_srt(cfg: config.Config, transcript: dict, seg: LongSeg) -> str:
    words = [w for w in transcript.get("words", []) if w["end"] > seg.start and w["start"] < seg.end]
    lines, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        tok = w["word"].strip()
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 0.0
        # break on the word cap, terminal punctuation, OR a speech pause — the pause fallback
        # keeps cues phrase-shaped on the whisper.cpp/AMD path (which emits little punctuation)
        if (len(cur) >= cfg.lf_srt_max_words or tok.endswith((".", "?", "!"))
                or gap > cfg.cap_break_pause_s):
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    dur = seg.dur
    blocks = []
    for grp in lines:
        a = max(0.0, min(grp[0]["start"] - seg.start, dur - 0.3))
        b = max(a + 0.3, min(grp[-1]["end"] - seg.start, dur))   # never run past the segment end
        text = " ".join(x["word"].strip() for x in grp).strip()
        if text:
            blocks.append([a, b, text])
    for i in range(len(blocks) - 1):                  # no overlap into the next line
        if blocks[i][1] > blocks[i + 1][0] - 0.05:
            blocks[i][1] = max(blocks[i][0] + 0.2, blocks[i + 1][0] - 0.05)
    blocks = [b for b in blocks if b[1] > b[0]]       # drop any zero/negative-duration cue
    out = []
    for i, (a, b, text) in enumerate(blocks, 1):
        out += [str(i), f"{_srt_ts(a)} --> {_srt_ts(b)}", text, ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# render + package
# --------------------------------------------------------------------------- #
def render_segment(cfg: config.Config, vod: str, seg: LongSeg, watermark: str, out_path: str,
                   progress_cb=None, should_cancel=None) -> str:
    h = int(cfg.lf_height)
    inputs = ["-ss", f"{seg.start:.3f}", "-i", vod]
    if cfg.lf_watermark and watermark and os.path.exists(watermark):
        inputs += ["-i", watermark]
        fc = f"[0:v]scale=-2:{h}[v];[v][1:v]overlay=24:24:eof_action=pass[outv]"
    else:
        fc = f"[0:v]scale=-2:{h}[outv]"
    dur = seg.dur
    base = [util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            *inputs, "-filter_complex", fc, "-map", "[outv]", "-map", "0:a:0?", "-t", f"{dur:.3f}"]
    tail = ["-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000",
            "-r", str(cfg.fps), "-movflags", "+faststart", out_path]
    util.ensure_dirs(os.path.dirname(out_path) or ".")
    hw = None if render._FORCE_CPU else render._hw_encoder(cfg.use_nvenc)   # NVENC->AMF->libx264
    for enc in ([hw, None] if hw else [None]):
        rc, err = render._run_ffmpeg(base + render._encode_args(cfg, enc) + tail, dur, progress_cb, should_cancel)
        ok = rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10000
        if ok and enc and not render._decode_ok(out_path, max_secs=30):     # AMF can silently corrupt
            log(f"[longform] {enc} reported success but the segment is CORRUPT — re-rendering on CPU")
            render._FORCE_CPU = True
            ok = False
        if ok:
            return out_path
        log(f"[longform] {enc or 'libx264'} failed: {err.strip()[-300:]}")
    raise RuntimeError(f"longform render failed: {out_path}")


def write_package(cfg: config.Config, transcript: dict, seg: LongSeg, out_dir: str) -> None:
    base = os.path.splitext(seg.file)[0]
    with open(os.path.join(out_dir, base + ".description.txt"), "w", encoding="utf-8") as fh:
        fh.write(seg.description + "\n")
    with open(os.path.join(out_dir, base + ".srt"), "w", encoding="utf-8") as fh:
        fh.write(build_srt(cfg, transcript, seg))


def write_manifest(out_dir: str, vod: str, segs: list) -> None:
    util.write_json(os.path.join(out_dir, "longform.json"),
                    {"vod": os.path.basename(vod), "segments": [s.to_dict() for s in segs]})


def write_md(out_dir: str, vod: str, segs: list) -> None:
    lines = [f"# Long-form videos from {os.path.basename(vod)}", "",
             "Each is a 16:9 session video ready for YouTube. Upload the `.mp4`, attach the "
             "`.srt` as subtitles, and paste the matching `.description.txt` (it contains the "
             "title, chapters and tags).", ""]
    for s in segs:
        lines.append(f"## seg{s.idx:02d} — {s.title}")
        lines.append(f"- **Source:** {util.hhmmss(s.start)}–{util.hhmmss(s.end)}  "
                     f"(**{s.dur/60:.1f} min**)   **Score:** {s.score}   **Game:** {s.game or '-'}")
        lines.append(f"- **Files:** `{s.file}`, `seg{s.idx:02d}.srt`, `seg{s.idx:02d}.description.txt`")
        lines.append(f"- **Chapters ({len(s.chapters)}):** " +
                     " · ".join(f"{util.hhmmss(c['t'])} {c['label']}" for c in s.chapters))
        lines.append("")
    with open(os.path.join(out_dir, "longform.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def build(cfg: config.Config, vod: str, transcript: dict, wav: str, out_dir: str,
          plan_only: bool = False, watermark: str = "") -> list:
    """Plan + package (+ render unless plan_only). Returns the LongSeg list."""
    util.ensure_dirs(out_dir)
    segs = plan(cfg, transcript, wav, util.probe_duration(vod))
    if not segs:
        log("[longform] no segments found.")
        return []
    for s in segs:
        write_package(cfg, transcript, s, out_dir)
    write_manifest(out_dir, vod, segs)
    write_md(out_dir, vod, segs)
    log(f"[longform] planned {len(segs)} videos:")
    for s in segs:
        log(f"  seg{s.idx:02d}  {s.dur/60:5.1f} min  score {s.score:5.1f}  "
            f"{len(s.chapters)} ch  {s.title}")
    if plan_only:
        return segs
    if cfg.lf_watermark and not watermark:
        from . import branding
        watermark = branding.render_watermark(cfg)
    for s in segs:
        outp = os.path.join(out_dir, s.file)
        log(f"[longform] rendering seg{s.idx:02d} ({s.dur/60:.1f} min) -> {s.file}")
        try:
            render_segment(cfg, vod, s, watermark, outp)
            log(f"  done: {s.file}")
        except Exception as e:  # noqa: BLE001
            log(f"  RENDER FAILED seg{s.idx:02d}: {e}")
    return segs
