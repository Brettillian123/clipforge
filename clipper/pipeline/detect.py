"""Local best-moment detection.

Scores every second of the VOD on six signals (energy, spike, sustain, text,
laughter, variance), snaps clip boundaries to sentence pauses, then greedily
picks well-spaced, topically-diverse clips. No model/API required; the optional
Claude re-rank (rerank.py) refines this shortlist.
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field

import numpy as np

from . import audio, config

# --------------------------------------------------------------------------- #
# Lexicons tuned for a comedic gaming streamer (Tarkov / prop-hunt / party).
# --------------------------------------------------------------------------- #
SINGLE_LEX = {
    "what": 1.0, "no": 0.6, "stop": 0.9, "wait": 0.9, "bro": 0.8, "dude": 0.8,
    "insane": 1.4, "crazy": 1.2, "clutch": 1.5, "actually": 0.6, "huge": 1.0,
    "easy": 0.9, "gg": 1.1, "rage": 1.2, "scared": 1.0, "run": 0.9, "help": 1.0,
    "nope": 0.9, "yikes": 1.0, "bruh": 1.0, "hilarious": 1.4, "funny": 1.1,
    "dying": 1.1, "how": 0.6, "why": 0.6, "win": 1.0, "won": 1.0, "victory": 1.2,
    "kill": 0.9, "killed": 1.0, "found": 1.0, "caught": 1.1, "hider": 0.8,
    "hunter": 0.8, "prop": 0.7, "unbelievable": 1.3, "panic": 1.1, "screaming": 1.3,
    "terrifying": 1.1, "perfect": 0.8, "first": 0.5,
}
PHRASE_LEX = {
    "oh my god": 1.8, "oh my gosh": 1.6, "no way": 1.6, "what the": 1.4,
    "let's go": 1.7, "lets go": 1.7, "no no no": 1.6, "hold on": 1.0,
    "are you kidding": 1.6, "you're kidding": 1.5, "that's crazy": 1.4,
    "oh no": 1.3, "i can't": 1.3, "i cant": 1.3, "what is happening": 1.6,
    "got him": 1.3, "got em": 1.3, "got them": 1.2, "first try": 1.6,
    "no shot": 1.4, "what is that": 1.2, "are you serious": 1.5,
}
LAUGH_TOKENS = {"haha", "hahaha", "hahahaha", "lol", "lmao", "lmfao", "laughing", "laughter", "laughs", "hehe"}
PROFANITY = {"shit", "fuck", "fucking", "damn", "hell", "ass", "crap", "bitch", "wtf"}
STOPWORDS = set("the a an and or but to of in on it is im i'm you he she they we are was were be been "
                "this that these those my your his her our their for with at by from as so if then just "
                "like get got go going gonna yeah yes no not do dont don't can cant can't will would there "
                "here what when how why who oh okay ok up out off now know think really very".split())

_word_re = re.compile(r"[a-z']+")


def _clean(tok: str) -> str:
    m = _word_re.findall(tok.lower())
    return m[0] if m else ""


@dataclass
class Candidate:
    start: float
    end: float
    peak: float
    score: float                       # 0..100
    components: dict = field(default_factory=dict)
    text: str = ""
    keywords: list = field(default_factory=list)
    profanity: bool = False
    autodetected_cam: bool = False     # filled in later by render
    words: list = field(default_factory=list)  # [{start,end,word}] relative to clip start
    meta: dict = field(default_factory=dict)   # title/caption/hashtags (local or Claude)

    @property
    def duration(self) -> float:
        return self.end - self.start


def _robust_norm(a: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(a, 5), np.percentile(a, 95)
    if hi - lo < 1e-6:
        return np.zeros_like(a)
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0)


def _build_text_arrays(n: int, words: list, segs: list):
    lex = np.zeros(n); laugh = np.zeros(n); wps = np.zeros(n)
    rep = np.zeros(n); q = np.zeros(n, dtype=bool)
    prev = None
    for w in words:
        sec = int(w["start"])
        if not (0 <= sec < n):
            prev = None
            continue
        tok = _clean(w["word"])
        if not tok:
            continue
        wps[sec] += 1
        if tok in LAUGH_TOKENS:
            laugh[sec] += 1.0
        if tok in SINGLE_LEX:
            lex[sec] += SINGLE_LEX[tok]
        if tok in PROFANITY:
            lex[sec] += 0.5
        if tok == prev:
            rep[sec] += 1.0
        prev = tok
    for seg in segs:
        a = int(seg["start"]); b = int(seg["end"]); txt = seg["text"].lower()
        if not txt:
            continue
        if txt.rstrip().endswith("?"):
            q[a:min(n, b + 1)] = True
        span = max(1, b - a + 1)
        for phrase, wt in PHRASE_LEX.items():
            if phrase in txt:
                for s in range(a, min(n, b + 1)):
                    lex[s] += wt / span
    return lex, laugh, wps, rep, q


def _boundaries(cfg: config.Config, segs: list, smap: audio.SilenceMap) -> list:
    bset = set()
    for seg in segs:
        t = seg["text"].strip()
        if t[-1:] in ".?!":
            bset.add(round(seg["end"], 2))
        bset.add(round(seg["start"], 2))
    for (s, e) in smap.runs(cfg.pause_sentence_ms):
        bset.add(round((s + e) / 2.0, 2))
    return sorted(bset)


def _word_safe_start(words: list, t: float) -> float:
    """Never begin a clip mid-word: if a word is being spoken at t, back up to its onset."""
    prev = None
    for w in words:
        if w["start"] > t:
            break
        prev = w
    if prev is not None and prev["end"] > t:   # t falls inside prev word -> start at its onset
        return round(prev["start"], 2)
    return t


def _word_safe_end(words: list, t: float) -> float:
    """Never end a clip mid-word: if a word is being spoken at t, extend to its end."""
    for w in words:
        if w["start"] >= t:
            break
        if w["end"] > t:                       # w.start < t < w.end -> t is inside this word
            return round(w["end"], 2)
    return t


def _snap(boundaries: list, target: float, lo: float, hi: float, prefer: str) -> float:
    """Return the boundary nearest `target` within [target+lo, target+hi]."""
    a, b = target + lo, target + hi
    i = bisect.bisect_left(boundaries, a)
    best, bestd = None, 1e9
    while i < len(boundaries) and boundaries[i] <= b:
        cand = boundaries[i]
        d = abs(cand - target) - (0.5 if (prefer == "earlier" and cand <= target) or
                                          (prefer == "later" and cand >= target) else 0.0)
        if d < bestd:
            best, bestd = cand, d
        i += 1
    return best if best is not None else target


def detect(cfg: config.Config, transcript: dict, wav: str) -> list[Candidate]:
    sr, x = audio.load_pcm(wav)
    dur = len(x) / sr
    feats = audio.per_second_features(x, sr, cfg.score_bin_s)
    n = feats["n_bins"]
    sec_db, sec_hf = feats["sec_db"], feats["sec_hf"]

    ftimes, fdb = audio.frame_rms_db(x, sr, cfg.frame_ms, cfg.hop_ms)
    smap = audio.silence_map(ftimes, fdb, cfg.pause_drop_db, cfg.hop_ms)
    ref = audio.speech_ref_db(fdb, cfg.speech_pct)
    excite = (sec_db - ref).astype(np.float64)             # per-second relative loudness (dB)

    words, segs = transcript["words"], transcript["segments"]
    lex, laugh, wps, rep, q = _build_text_arrays(n, words, segs)
    boundaries = _boundaries(cfg, segs, smap)

    # rolling rise (the "freakout"): bin minus trailing-8s median
    rise = np.zeros(n)
    for i in range(n):
        j = max(0, i - 8)
        rise[i] = excite[i] - (np.median(excite[j:i]) if i > j else excite[i])
    hf_hot = ((sec_hf > 0.33) & (excite > 4.0)).astype(float)  # laughter/scream proxy
    interest = np.maximum(excite, 0) + lex + 2.0 * laugh + 1.5 * hf_hot  # for peak/hook work

    def components(a: int, b: int) -> dict:
        a = max(0, a); b = min(n, b)
        if b - a < 2:
            return dict(E=0, SP=0, SUS=0, TXT=0, LAU=0, VAR=0)
        e = excite[a:b]
        rate_spike = max(0.0, float(wps[a:b].max()) - 3.0)
        txt = float(lex[a:b].sum()) + 1.2 * rate_spike + (1.5 if q[a:b].any() else 0.0) + 0.8 * float(rep[a:b].sum())
        return dict(
            E=float(e.mean()),
            SP=float(rise[a:b].max()),
            SUS=float((e > 4.0).mean()),
            TXT=txt,
            LAU=float(laugh[a:b].sum()) + 0.5 * float(hf_hot[a:b].sum()),
            VAR=float(e.std()),
        )

    # score every second with both a 30s and a 15s centered window; keep the higher
    keys = ["E", "SP", "SUS", "TXT", "LAU", "VAR"]
    weights = np.array([cfg.w_energy, cfg.w_spike, cfg.w_sustain, cfg.w_text, cfg.w_laugh, cfg.w_var])
    finals = []
    for W in (cfg.win_primary_s, cfg.win_punchy_s):
        raw = {k: np.zeros(n) for k in keys}
        half = W // 2
        for t in range(n):
            c = components(t - half, t + (W - half))
            for k in keys:
                raw[k][t] = c[k]
        norm = np.stack([_robust_norm(raw[k]) for k in keys], axis=1)  # n x 6
        finals.append(norm @ weights)
    final = np.maximum(finals[0], finals[1])
    # light smoothing
    if cfg.smooth_bins > 1:
        k = np.ones(cfg.smooth_bins) / cfg.smooth_bins
        final = np.convolve(final, k, mode="same")

    # greedy non-max suppression on centers
    order = np.argsort(final)[::-1]
    centers: list[int] = []
    for t in order:
        t = int(t)
        if final[t] <= 0:
            break
        if all(abs(t - c) >= cfg.dedupe_gap_s for c in centers):
            centers.append(t)
        if len(centers) >= cfg.keep_for_rerank * 3:
            break

    cands: list[Candidate] = []
    for c in centers:
        # refine peak within +-10s by interest
        lo, hi = max(0, c - 10), min(n, c + 10)
        peak = lo + int(np.argmax(interest[lo:hi])) if hi > lo else c
        seed_s = peak - cfg.seed_back_s
        seed_e = peak + cfg.seed_fwd_s
        start = _snap(boundaries, seed_s, -6, 4, "earlier")
        end = _snap(boundaries, seed_e, -4, 10, "later")
        # clamp length
        if end - start < cfg.clip_min_s:
            end = start + cfg.clip_min_s
        if end - start > cfg.clip_max_s:
            end = _snap(boundaries, start + cfg.clip_max_s, -6, 0, "earlier")
            if end - start > cfg.clip_max_s or end <= start:
                end = start + cfg.clip_max_s
        # hook guard: trim dead air at the very start
        gs = int(start)
        while gs < int(end) - cfg.clip_min_s and interest[gs:gs + 3].sum() < 0.5:
            gs += 1
        start = float(gs) if gs > int(start) else start
        # never cut mid-laugh: extend end past a laugh at the boundary
        ei = min(n - 1, int(end))
        if laugh[ei] > 0 or hf_hot[ei] > 0:
            end = min(dur, _snap(boundaries, end + 2.5, 0, 4, "later"))
        start = max(0.0, start)
        end = min(dur, end)
        # final safety: the integer-second hook guard / a raw snap fallback can land mid-word —
        # pull the start back to the current word's onset and push the end to the current word's end
        # so a clip never begins or ends on a sliced word.
        start = max(0.0, _word_safe_start(words, start))
        end = min(dur, _word_safe_end(words, end))
        if end - start < cfg.clip_min_s:
            continue

        clip_words = [w for w in words if w["end"] > start and w["start"] < end]
        text = " ".join(w["word"].strip() for w in clip_words).strip()
        toks = [_clean(w["word"]) for w in clip_words]
        content = [t for t in toks if t and t not in STOPWORDS and len(t) > 3]
        kw = [w for w, _ in _top_counts(content, 6)]
        prof = any(t in PROFANITY for t in toks)
        comp = components(int(start), int(end))
        rel_words = [{"start": round(w["start"] - start, 3),
                      "end": round(w["end"] - start, 3),
                      "word": w["word"]} for w in clip_words]
        cands.append(Candidate(
            start=round(start, 2), end=round(end, 2), peak=round(float(peak), 2),
            score=round(float(final[c]) * 100, 1), components={k: round(comp[k], 3) for k in keys},
            text=text, keywords=kw, profanity=prof, words=rel_words,
        ))

    cands.sort(key=lambda c: c.score, reverse=True)
    return _diversify(cands, cfg.keep_for_rerank)


def _top_counts(items: list, k: int):
    counts: dict = {}
    for it in items:
        counts[it] = counts.get(it, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:k]


def _diversify(cands: list[Candidate], keep: int) -> list[Candidate]:
    """Drop the lower of two clips sharing >50% of their top keywords."""
    out: list[Candidate] = []
    for c in cands:
        cset = set(c.keywords)
        dup = False
        for k in out:
            kset = set(k.keywords)
            if cset and kset:
                overlap = len(cset & kset) / max(1, min(len(cset), len(kset)))
                if overlap > 0.5:
                    dup = True
                    break
        if not dup:
            out.append(c)
        if len(out) >= keep:
            break
    return out
