"""Natural-language clip editing via Claude.

Given the current ClipSpec + the surrounding transcript and a free-text
instruction ('make it start where I pick up the gun', 'tighten the ending',
'rewrite the captions punchier'), return the changed spec fields as JSON. The
dashboard applies them and re-renders.
"""
from __future__ import annotations

import json

from . import config, util
from .util import log

SYSTEM = (
    "You edit short-form vertical clips for a gaming streamer. You are given one "
    "clip's current spec plus the surrounding VOD transcript with ABSOLUTE timestamps "
    "(seconds). Apply the user's instruction by returning ONLY the spec fields that "
    "change, as JSON. Times are absolute VOD seconds. Keep clips self-contained with a "
    "hook in the first ~3s and a clear payoff. Do not cut mid-word: snap segment "
    "boundaries to the start/end of nearby transcript words."
)

SCHEMA_HINT = {
    "segments": "optional [{start,end}] absolute VOD seconds; replace the whole list. Add a second "
                "segment to splice in context; reorder for before/after.",
    "metadata": "optional {tiktok:{title,caption,hashtags[]}, shorts:{...}} - rewrite copy here",
    "captions_enabled": "optional bool",
    "chat": "optional {enabled,bool; seg_index; grab_t (abs sec); t_start; t_end; pos; scale}",
    "note": "one short line describing what you changed (always include this)",
}


def _loads(text: str) -> dict:
    t = (text or "").strip()
    if "{" in t and "}" in t:
        t = t[t.find("{"):t.rfind("}") + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        raise RuntimeError("AI returned unparseable output; please try again.") from e


CLEAN_SYSTEM = (
    "You clean up auto-generated spoken captions for a comedic gaming streamer's short clip. "
    "Remove stutters, false starts, and filler (um, uh, like, accidental repeats); fix obvious "
    "mis-transcriptions; add natural punctuation (commas, periods, question marks, exclamation "
    "points); and group the words into short, readable phrases so they don't look choppy. Make it "
    "read clearly and punchy. Stay 100% faithful to what was actually said - never invent or "
    "embellish. Keep words in chronological order."
)


def clean_captions(cfg: config.Config, words: list) -> list:
    """Clean auto-captions while preserving word-level timing. Returns [{start,end,word}]
    (times relative to clip start, same shape as caption_words)."""
    if not words:
        return []
    key = util.get_api_key()
    if not key:
        raise RuntimeError("No Anthropic API key. Add it in the dashboard.")
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic not installed. Run: pip install anthropic") from e

    src = [{"i": i, "s": round(float(w["start"]), 2), "e": round(float(w["end"]), 2),
            "w": (w.get("word") or "").strip()} for i, w in enumerate(words)]
    payload = {
        "original_words": src,
        "task": "Return cleaned captions as JSON {\"words\":[{\"w\":word,\"s\":start,\"e\":end}, ...]} "
                "in reading order. Each w is ONE word and MUST carry the punctuation that belongs on it "
                "(commas, periods, question marks, exclamation points). Punctuation marks where a phrase "
                "or sentence ENDS and is how lines get grouped, so place it carefully so whole phrases "
                "stay together (e.g. 'where','are','you?' -> the '?' on 'you?' keeps 'where are you?' on "
                "one line). Take s/e from the original word times the cleaned word covers so the karaoke "
                "highlight stays in sync. Drop filler entirely; merge a stutter ('I-I-I','the the') into "
                "one word spanning its time; never add words that weren't said.",
        "output": "Return ONLY the JSON object.",
    }
    client = anthropic.Anthropic(api_key=key)
    log(f"[clean_captions] cleaning {len(src)} words with {cfg.ai_model}")
    msg = client.messages.create(model=cfg.ai_model, max_tokens=4000, system=CLEAN_SYSTEM,
                                 messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    data = _loads(text)
    clip_end = float(words[-1]["end"])
    out = []
    for it in data.get("words", []):
        w = str(it.get("w", "")).strip()
        if not w:
            continue
        try:
            s = float(it.get("s", 0)); e = float(it.get("e", s + 0.3))
        except (TypeError, ValueError):
            continue
        s = max(0.0, min(s, clip_end)); e = max(s + 0.05, min(e, clip_end + 0.5))
        br = 1 if it.get("br") else 0
        parts = w.split()
        if len(parts) > 1:                       # split an accidental multi-word entry, share the span
            span = (e - s) / len(parts)
            for k, p in enumerate(parts):
                d = {"start": round(s + k * span, 3), "end": round(s + (k + 1) * span, 3), "word": p}
                if br and k == len(parts) - 1:
                    d["br"] = 1
                out.append(d)
        else:
            d = {"start": round(s, 3), "end": round(e, 3), "word": w}
            if br:
                d["br"] = 1
            out.append(d)
    out.sort(key=lambda x: x["start"])
    return out


def _context_words(transcript: dict, s: float, e: float, pad: float = 45.0):
    a, b = s - pad, e + pad
    return [{"t": round(w["start"], 1), "w": w["word"].strip()}
            for w in transcript["words"] if w["end"] > a and w["start"] < b]


def edit(cfg: config.Config, vod_duration: float, spec, transcript: dict, instruction: str) -> dict:
    key = util.get_api_key()
    if not key:
        raise RuntimeError("No Anthropic API key. Add it in the dashboard (or set ANTHROPIC_API_KEY).")
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic not installed. Run: pip install anthropic") from e

    cur = {
        "id": spec.id,
        "segments": spec.segments,
        "captions_enabled": spec.captions_enabled,
        "metadata": spec.metadata,
        "chat": spec.chat,
        "transcript": spec.transcript[:800],
    }
    payload = {
        "instruction": instruction,
        "vod_duration_s": round(vod_duration, 1),
        "current_spec": cur,
        "surrounding_transcript": _context_words(transcript, spec.start, spec.start + spec.duration),
        "return_format": SCHEMA_HINT,
        "output": "Return ONLY JSON with the changed fields. No prose.",
    }
    client = anthropic.Anthropic(api_key=key)
    log(f"[ai_edit] {spec.id}: {instruction!r}")
    msg = client.messages.create(
        model=cfg.ai_model, max_tokens=2048, system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    if "{" in text and "}" in text:
        text = text[text.find("{"):text.rfind("}") + 1]
    try:
        changes = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError("AI returned unparseable output; try rephrasing the request.") from e
    if not isinstance(changes, dict):
        raise RuntimeError("AI returned an unexpected format; try rephrasing.")

    # sanitize segment times
    if "segments" in changes and isinstance(changes["segments"], list):
        clean = []
        for seg in changes["segments"]:
            s = max(0.0, float(seg.get("start", 0)))
            e = min(vod_duration, float(seg.get("end", s + 1)))
            if e - s >= 1.0:
                clean.append({"start": round(s, 2), "end": round(e, 2)})
        if clean:
            changes["segments"] = clean
        else:
            changes.pop("segments", None)
    return changes
