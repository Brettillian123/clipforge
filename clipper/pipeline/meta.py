"""Local (no-API) metadata: guess the game and draft a title/caption/hashtags.

Heuristic only - the optional Claude re-rank (rerank.py) writes much better copy.
"""
from __future__ import annotations

from . import config

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
