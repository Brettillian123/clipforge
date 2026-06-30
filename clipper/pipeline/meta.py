"""Local (no-API) metadata: guess the game and draft a title/caption/hashtags.

Heuristic only - the optional Claude re-rank (rerank.py) writes much better copy.
"""
from __future__ import annotations

import re

from . import config

# We never put emoji in generated titles/captions (they read as AI spam and the burned-in titlecard
# font has no glyphs for them). This strips any the model slips in. Keeps normal text + punctuation
# like — … ' (all below U+2190); same range the titlecard cleaner uses.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002190-\U00002BFF\U0000FE00-\U0000FE0F‍™ℹ]", re.UNICODE)


def strip_emoji(s: str) -> str:
    """Remove emoji/pictographs and tidy the whitespace they leave behind."""
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", s or "")).strip(" -—|·")


# Em dash (and a spaced hyphen used like one) -> comma; en dash -> hyphen. The user's style rule is
# "never use em dashes" in generated copy; the model is told this too, this just enforces it.
_EMDASH_RE = re.compile(r"\s*—\s*|\s+-\s+")


def clean_copy(s: str) -> str:
    """Style-clean generated copy: strip emoji, remove em dashes, tidy whitespace. KEEPS caps,
    normal punctuation, and in-word hyphens (1v3, drop-spot, 20-kill)."""
    s = _EMOJI_RE.sub("", s or "")
    s = re.sub(r"\s*–\s*", "-", s)        # en dash -> hyphen (ranges/compounds)
    s = _EMDASH_RE.sub(", ", s)                # em dash / spaced hyphen -> comma
    s = re.sub(r"\s*,(?:\s*,)+", ",", s)       # collapse doubled commas
    s = re.sub(r"\s+,", ",", s)                # no space before a comma
    return re.sub(r"\s{2,}", " ", s).strip(" ,|·")


# Capitalize the first letter of the text and of each new sentence (after . ! ?), allowing leading
# quotes/brackets. Only ever UPPERCASES a sentence-initial letter -> mid-line CAPS (BRO, INSANE) and
# number-led starts (1v3) are left untouched.
_SENT_CAP = re.compile(r"""(^|[.!?]\s+)(["'(\[\{]*)([a-z])""")


def sentence_case_starts(s: str) -> str:
    """Ensure every caption/sentence starts with a capital letter, without lowercasing anything else."""
    return _SENT_CAP.sub(lambda m: m.group(1) + m.group(2) + m.group(3).upper(), s or "")


# Capitalize the standalone pronoun "i" (and contractions i'm / i'll / i've / i'd). \b before the
# apostrophe means the "i" in i'm is its own token, so this catches contractions too. Words like
# "in", "is", "it", "into" are untouched (no boundary after their leading i).
_PRONOUN_I = re.compile(r"\bi\b")


def fix_pronoun_i(s: str) -> str:
    """Uppercase the lone pronoun 'i' -> 'I' (incl. i'm, i'll, i've), leaving real words alone."""
    return _PRONOUN_I.sub("I", s or "")


GAME_HINTS = {
    "Escape from Tarkov": {"tarkov", "scav", "raid", "extract", "pmc", "loot", "magazine", "stash"},
    "Meccha Chameleon": {"prop", "hunt", "hider", "hunter", "chameleon", "caught", "infection", "hide", "seek"},
    "Roblox": {"roblox", "robux", "obby"},
}

# templates keyed by which signal dominated the clip
HOOKS = {
    "laugh": ["I COULDN'T STOP LAUGHING", "THIS BROKE ME", "WE LOST IT"],
    "spike": ["NO WAY THIS HAPPENED", "WATCH WHAT HAPPENS", "I WAS NOT READY"],
    "text": ["YOU HAD TO BE THERE", "CHAT WENT CRAZY", "LISTEN TO THIS"],
    "energy": ["IT GOT INTENSE", "FULL SEND", "PURE CHAOS"],
}


def coerce_hashtags(v, limit: int = 6) -> list:
    """Accept a list or a space/comma string; return clean '#tag' list."""
    if isinstance(v, str):
        v = v.replace(",", " ").split()
    out = []
    for h in (v or []):
        h = str(h).strip()
        if h:
            out.append("#" + h.lstrip("#"))
    return out[:limit]


def guess_game(text: str, keywords: list) -> str | None:
    blob = (text + " " + " ".join(keywords)).lower()
    best, best_hits = None, 0
    for game, words in GAME_HINTS.items():
        hits = sum(1 for w in words if w in blob)
        if hits > best_hits:
            best, best_hits = game, hits
    return best if best_hits >= 1 else None


def _dominant(components: dict) -> str:
    order = [("laugh", "LAU"), ("spike", "SP"), ("text", "TXT"), ("energy", "E")]
    return max(order, key=lambda kv: components.get(kv[1], 0))[0]


def _hashtags(platform: str, game: str | None) -> list[str]:
    game_tag = None
    if game:
        game_tag = "#" + game.lower().replace("'", "").replace(" ", "")
    if platform == "shorts":
        tags = ["#Shorts", "#gaming", game_tag or "#twitch", "#twitchclips"]
    else:
        tags = ["#twitchclips", "#gaming", game_tag or "#fyp", "#fyp"]
    # dedupe, keep order
    seen, out = set(), []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:4]


def local_metadata(cfg: config.Config, cand, platform: str) -> dict:
    game = guess_game(cand.text, cand.keywords)
    idx = (int(cand.start) // 7) % 3
    hook = HOOKS[_dominant(cand.components)][idx]
    title = hook if not game else f"{hook} ({game})"
    title = title[:60]
    handle = (cfg.wm_handle or "").strip()
    cta = f" | live on Twitch @{handle}" if handle else ""
    caption = f"{hook.capitalize()}{cta}"
    return {
        "title": title,
        "caption": caption,
        "hashtags": _hashtags(platform, game),
        "game": game,
    }
