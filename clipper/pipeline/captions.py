"""Generate animated word-by-word (karaoke) captions as an ASS subtitle file.

For each on-screen line we emit one Dialogue event per word; during a word's
time the whole line shows with that word recolored to the gold highlight (and
optionally scaled). libass burns it in via ffmpeg's subtitles filter.

ASS color note: inline overrides use &Hbbggrr& (bytes reversed from RGB).
"""
from __future__ import annotations

import math
import re

from . import config, util

_SENT_END = tuple(".?!")


def _inline_color(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#").upper()
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&"


def _clean_word(w: str, uppercase: bool) -> str:
    # Strip ASS-significant characters from spoken text so a transcribed brace /
    # backslash / newline can't corrupt or blank the caption line (libass treats
    # '{' as an override block and '\' as an escape).
    t = w.strip().replace("\\", "/").replace("{", "(").replace("}", ")")
    t = t.replace("\r", " ").replace("\n", " ")
    return t.upper() if uppercase else t


def _split_long(ph: list, max_words: int) -> list[list]:
    """Split an over-long clause into BALANCED lines of <=max_words words (even sizes,
    so we don't strand a one-word orphan)."""
    n = len(ph)
    if n <= max_words:
        return [ph]
    pieces = math.ceil(n / max_words)
    size = math.ceil(n / pieces)
    return [ph[i:i + size] for i in range(0, n, size)]


def _merge_orphans(lines: list, max_words: int) -> list:
    """Pull a stranded single FRAGMENT word back onto the previous line when there's room.
    Don't merge across a completed clause (a line/word ending in punctuation is intentional)."""
    out: list = []
    for ln in lines:
        prev = out[-1] if out else None
        if (prev and len(ln) == 1 and len(prev) < max_words
                and ln[0]["word"].strip()[-1:] not in _CLAUSE_END
                and prev[-1]["word"].strip()[-1:] not in _CLAUSE_END):
            prev.extend(ln)
        else:
            out.append(ln)
    return out


_CLAUSE_END = tuple(".?!,;:—…")   # . ? ! , ; : em-dash ellipsis


def _chunk(words: list, max_words: int, break_pause: float) -> list[list]:
    """Group words into on-screen lines as natural phrases.

    If the captions are punctuated (AI cleanup adds punctuation), group by CLAUSE
    boundaries (a word ending in . ? ! , ; : ends a line) so phrases like "where are
    you?" stay on one line regardless of speech pauses. Long clauses are split at their
    biggest internal gaps; a stranded single word is merged back. If there's no
    punctuation (raw transcript), fall back to grouping by speech pauses.
    """
    if not words:
        return []
    has_punct = any(w["word"].strip()[-1:] in _CLAUSE_END for w in words)
    if has_punct:
        clauses, cur = [], []
        for w in words:
            cur.append(w)
            if w["word"].strip()[-1:] in _CLAUSE_END:
                clauses.append(cur)
                cur = []
        if cur:
            clauses.append(cur)
        groups = clauses
    else:
        groups, cur = [], [words[0]]
        for w in words[1:]:
            if (w["start"] - cur[-1]["end"]) > break_pause:
                groups.append(cur)
                cur = [w]
            else:
                cur.append(w)
        groups.append(cur)
    lines = []
    for g in groups:
        lines.extend(_split_long(g, max_words))
    return _merge_orphans(lines, max_words)


def build_ass(cfg: config.Config, words: list, clip_dur: float, out_path: str, style: dict | None = None) -> str:
    """Write an ASS file for one clip. `words` times are relative to clip start.

    `style` (optional per-clip override) keys: font, size, fill, highlight, outline_w,
    margin_v, max_words, uppercase, pop, pop_scale.
    """
    st = style or {}
    font = st.get("font", cfg.cap_font)
    size = int(st.get("size", cfg.cap_size))
    fill_hex = st.get("fill", cfg.cap_fill)
    hi_hex = st.get("highlight", cfg.cap_highlight)
    outline_w = int(st.get("outline_w", cfg.cap_outline_w))
    margin_v = int(st.get("margin_v", cfg.cap_margin_v))
    max_words = max(1, int(st.get("max_words", cfg.cap_max_words)))
    uppercase = bool(st.get("uppercase", cfg.cap_uppercase))
    pop = bool(st.get("pop", cfg.cap_pop))
    pop_scale = int(st.get("pop_scale", cfg.cap_pop_scale))

    fill = config.hex_to_ass(fill_hex)                # &H00BBGGRR for the Style line
    highlight = config.hex_to_ass(hi_hex)
    outline = config.hex_to_ass(cfg.cap_outline_color)
    fill_inline = _inline_color(fill_hex)
    gold_inline = _inline_color(hi_hex)
    bold = -1

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {cfg.out_w}
PlayResY: {cfg.out_h}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},{fill},{highlight},{outline},&H00000000,{bold},0,0,0,100,100,0,0,1,{outline_w},{cfg.cap_shadow},{cfg.cap_alignment},{cfg.cap_margin_lr},{cfg.cap_margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    # ONE Dialogue event per line (no per-word re-spawn => no vertical jitter). Each word is
    # hidden until it's spoken (alpha reveal) and turns gold while active, via \t transforms
    # whose times are milliseconds relative to the event's own start. Hidden words still take
    # layout space, so revealing them never shifts the line (no horizontal jitter either).
    events: list[str] = []
    lines = _chunk(words, max_words, cfg.cap_break_pause_s)
    for i, line in enumerate(lines):
        t0 = max(0.0, line[0]["start"])
        t_end = min(clip_dur, line[-1]["end"] + 0.3)
        if i + 1 < len(lines):                      # never overlap the next line (no stacking)
            t_end = min(t_end, max(t0 + 0.1, lines[i + 1][0]["start"] - 0.02))
        spans = []
        for w in line:
            tok = _clean_word(w["word"], uppercase)
            if not tok:
                continue
            a = max(0, int(round((w["start"] - t0) * 1000)))
            b = max(a + 1, int(round((w["end"] - t0) * 1000)))
            spans.append(
                f"{{\\alpha&HFF&\\t({a},{a + 40},\\alpha&H00&)\\1c{fill_inline}"
                f"\\t({a},{a + 1},\\1c{gold_inline})\\t({b},{b + 1},\\1c{fill_inline})}}{tok}{{\\r}}"
            )
        if not spans:
            continue
        events.append(f"Dialogue: 0,{util.ass_time(t0)},{util.ass_time(t_end)},Cap,,0,0,0,,{' '.join(spans)}")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(header)
        fh.write("\n".join(events))
        fh.write("\n")
    return out_path
