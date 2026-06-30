"""Optional Claude re-rank of the local candidate shortlist (--ai).

Sends the top candidates (transcript + scores + timestamps) in ONE call and asks
for the best clips with titles/captions/hashtags + per-platform profanity notes.
Final order blends local and LLM scores. Requires ANTHROPIC_API_KEY and the
`anthropic` package (pip install anthropic).
"""
from __future__ import annotations

import json
import os

from . import config
from .util import log

def _extract_json(text: str) -> dict:
    """Pull the JSON object out of a model reply (handles fences/prose); raise friendly on failure."""
    t = (text or "").strip()
    if "{" in t and "}" in t:
        t = t[t.find("{"):t.rfind("}") + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        raise RuntimeError("Claude returned unparseable output; please try again.") from e


def _channel_descriptor(cfg) -> str:
    """'a comedic gaming streamer (Name)' — persona + optional name, both from config."""
    persona = (getattr(cfg, "channel_persona", "") or "a gaming streamer").strip()
    name = (getattr(cfg, "channel_name", "") or "").strip()
    return f"{persona} ({name})" if name else persona


def _cta(cfg) -> str:
    """The caption call-to-action; uses your @handle when set, else a neutral fallback."""
    h = (getattr(cfg, "wm_handle", "") or "").strip()
    return f"live on Twitch @{h}" if h else "follow for more"


def _voice() -> str:
    """The anti-AI-copy rules: sound like the streamer, be specific, no emoji. This is the core of the
    'make captions feel authentic' request."""
    return (
        "VOICE — write like a real person, never like AI marketing copy. Sound the way this streamer would "
        "actually talk: casual, specific, a little dry and funny. The #1 rule is to be SPECIFIC to what "
        "literally happens in THIS clip — name the actual thing ('chat voted to sell my whole stash', "
        "'third wipe on the same boss', 'he proposed to me mid-raid') instead of vague hype. Generic hype "
        "is the dead giveaway of fake copy, so BAN it: no 'you won't believe', 'this was insane', 'pure "
        "chaos', 'wait for it', 'broke me', 'absolute cinema', 'the way that…', and no empty intensifiers "
        "(insane / crazy / epic / wild / unbelievable) used as filler. "
        "NO EMOJIS anywhere — not in titles, captions, descriptions, or hashtags. None, ever. "
        "Keep hashtags out of the caption sentence. Don't Title-Case Every Word, don't stack exclamation "
        "points, and don't oversell — a slightly understated, real line beats a hyped one. Vary the "
        "phrasing across clips; never reuse one template."
    )


def _platforms(cfg) -> str:
    """TikTok and YouTube need DIFFERENT copy — the user explicitly wants them tailored, not identical."""
    return (
        "TWO PLATFORMS, DIFFERENT COPY — never write identical text for both; duplicate copy hurts reach "
        "and search on each. "
        "TikTok caption: conversational and authentic, first person, lowercase is fine, like a caption a "
        "real gamer would actually post. One short line (sometimes two), about 70-140 characters. Lead with "
        f"the specific moment, then end by pointing viewers to the stream naturally (e.g. {_cta(cfg)}). "
        "YouTube Shorts title: a clear, SEARCHABLE headline — put the GAME NAME and the specific "
        "moment/outcome in the first ~40 characters (the feed truncates there), so it works in search and "
        "in-feed. Curiosity is good, but stay accurate; no bait. The YouTube description is one plain, "
        "readable line of context."
    )


def _system(cfg) -> str:
    return (
        f"You are a short-form clip editor for {_channel_descriptor(cfg)} "
        "who streams a rotating variety of games (it changes often) to Twitch, and posts clips to "
        "TikTok and YouTube Shorts. You are given candidate moments auto-detected from a VOD (transcript "
        "snippet, component scores, timestamps, profanity flag). ALWAYS infer the ACTUAL game from each "
        "clip's transcript and keywords (e.g. champion names, 'recall', 'minion', 'rooted' => League of "
        "Legends; 'raid', 'scav', 'extract' => Tarkov) and use that specific, correct game in the title "
        "and hashtags — NEVER assume a default game or tag a game the transcript doesn't support. Judge "
        "each clip on real viral potential for vertical short-form: a self-contained story, a hook in the "
        "first 3 seconds, and a clear payoff (a laugh, a clutch, a fail, a hot take). Reward genuine comedy "
        "and surprise; do NOT reward mere loudness. "
        "CRITICAL: strongly PREFER moments of ACTUAL GAMEPLAY/action (a play, a kill, a clutch, a fail, a "
        "scare, an in-game interaction) over downtime. AVOID — and score low — pre-game lobbies, menus, "
        "queues, loading, and idle social chit-chat with no game stakes (tells: 'are you ready', 'who's "
        "queuing', 'waiting for', 'one more game', talking about unrelated real-life stuff). If a candidate "
        "is just the streamer chatting in a lobby with nothing happening in the game, it is NOT a good clip "
        "no matter how much talking there is. Re-rank by viral potential, not by the input order.\n\n"
        + _voice() + "\n\n" + _platforms(cfg)
    )


def _platform_meta(cfg, item: dict) -> dict:
    """Map one AI result into the per-platform metadata the app stores: a punchy on-screen hook +
    conversational TikTok caption, and a searchable YouTube title + plain description. Distinct copy per
    platform; emoji stripped defensively; #Shorts added to the YouTube tags automatically."""
    from . import meta as _meta
    clean = _meta.strip_emoji
    hook = clean(item.get("hook") or item.get("youtube_title") or "")[:60]
    tt_cap = clean(item.get("tiktok_caption") or "")
    yt_title = clean(item.get("youtube_title") or hook)[:60]
    yt_desc = clean(item.get("youtube_desc") or "")
    tags = []
    for t in _meta.coerce_hashtags(item.get("hashtags"), limit=6):
        t = clean(t)                                # strip emoji out of tags too
        if t and t != "#" and t.lower() != "#shorts":
            tags.append(t)
    tags = tags[:5]
    yt_tags = (["#Shorts"] + tags)[:5]
    return {
        "tiktok": {"title": hook, "caption": tt_cap, "hashtags": tags[:5]},
        "shorts": {"title": yt_title, "caption": yt_desc, "hashtags": yt_tags},
    }


def _user_payload(cfg: config.Config, cands: list, count: int) -> str:
    items = []
    for i, c in enumerate(cands):
        items.append({
            "id": i,
            "start_s": c.start, "end_s": c.end, "dur_s": round(c.duration, 1),
            "local_score": c.score, "profanity": c.profanity,
            "components": c.components, "keywords": c.keywords,
            "transcript": c.text[:700],
        })
    instr = {
        "task": f"Select the {count} best clips and rank them best-first. For each, write post copy that "
                "follows the VOICE and TWO-PLATFORM rules exactly (TikTok and YouTube must read differently).",
        "for_each": {
            "id": "the candidate id you are selecting",
            "llm_score": "0-100 your viral-potential score",
            "hook": "<=55 chars: the punchy on-screen hook = the specific moment (also burned in as the "
                    "titlecard); no emoji",
            "tiktok_caption": "conversational TikTok caption, ~70-140 chars: specific moment first, then a "
                              f"natural nod to the stream ({_cta(cfg)}); no emoji, no hashtags in the text",
            "youtube_title": "<=60 chars (aim 25-45): searchable — game name + the specific moment/outcome "
                             "in the first ~40 chars; no emoji, no clickbait",
            "youtube_desc": "one plain, readable line of context for the YouTube description; no emoji",
            "hashtags": "4-5 tags as a JSON array, mix 1-2 broad + 2-3 niche/game-specific; do NOT include "
                        "#Shorts (added automatically); no emoji",
            "trim_start_delta_s": "optional, -3..3, nudge start (will be re-validated against pauses)",
            "trim_end_delta_s": "optional, -3..3",
            "platform_notes": {"tiktok": "profanity/safety note or ''", "shorts": "note or ''"},
            "reason": "one line why this clips well",
        },
        "candidates": items,
        "output": "Return ONLY JSON: {\"clips\":[ ... ]}. No prose.",
    }
    return json.dumps(instr, ensure_ascii=False)


def rerank(cfg: config.Config, cands: list, count: int) -> list:
    """Return candidates re-ordered + annotated with .meta (per-platform). Falls back
    to the local order on any failure."""
    from .util import get_api_key
    key = get_api_key()
    if not key:
        raise RuntimeError("No Anthropic API key. Set ANTHROPIC_API_KEY, add it in the dashboard, or run without --ai.")
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic not installed. Run: pip install anthropic") from e

    client = anthropic.Anthropic(api_key=key)
    payload = _user_payload(cfg, cands, count)
    log(f"[rerank] asking {cfg.ai_model} to judge {len(cands)} candidates ...")
    msg = client.messages.create(
        model=cfg.ai_model,
        max_tokens=4096,
        system=_system(cfg),
        messages=[{"role": "user", "content": payload}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    data = _extract_json(text)

    chosen: list = []
    for item in data.get("clips", []):
        i = item.get("id")
        if not isinstance(i, int) or not (0 <= i < len(cands)):
            continue
        c = cands[i]
        llm = float(item.get("llm_score", c.score))
        c.score = round(cfg.ai_blend * llm + (1 - cfg.ai_blend) * c.score, 1)
        # apply clamped trims
        ds = max(-cfg.ai_max_trim_s, min(cfg.ai_max_trim_s, float(item.get("trim_start_delta_s", 0) or 0)))
        de = max(-cfg.ai_max_trim_s, min(cfg.ai_max_trim_s, float(item.get("trim_end_delta_s", 0) or 0)))
        c.start = round(max(0.0, c.start + ds), 2)
        c.end = round(c.end + de, 2)
        c.meta = _platform_meta(cfg, item)          # {"tiktok": {...}, "shorts": {...}}
        c.meta["platform_notes"] = item.get("platform_notes", {})
        c.meta["reason"] = item.get("reason", "")
        c.meta["source"] = "claude"
        chosen.append(c)
    log(f"[rerank] Claude selected {len(chosen)} clips.")
    return chosen or cands


def write_titles(cfg: config.Config, clips: list) -> dict:
    """Write per-platform post copy for an EXISTING set of clips (no re-ranking, no render). Returns
    {clip_id: {"tiktok": {title,caption,hashtags}, "shorts": {title,caption,hashtags}}}. One Claude call."""
    from .util import get_api_key
    key = get_api_key()
    if not key:
        raise RuntimeError("No Anthropic API key.")
    import anthropic
    items = [{"id": c["id"], "keywords": c.get("keywords", []),
              "profanity": c.get("profanity", False), "transcript": (c.get("transcript", "") or "")[:700]}
             for c in clips]
    payload = {
        "task": f"For EACH clip id, write short-form post copy for {_channel_descriptor(cfg)}. Follow the "
                "VOICE and TWO-PLATFORM rules exactly — the TikTok and YouTube copy must read differently.",
        "for_each": {
            "hook": "<=55 chars: the specific on-screen moment; no emoji",
            "tiktok_caption": "conversational, ~70-140 chars: specific moment first, then a natural nod to "
                              f"the stream ({_cta(cfg)}); no emoji, no hashtags in the text",
            "youtube_title": "<=60 chars (aim 25-45): searchable — game + specific moment in the first ~40 "
                             "chars; no emoji, no bait",
            "youtube_desc": "one plain, readable line of context; no emoji",
            "hashtags": "3-5 tags as a JSON ARRAY; broad + niche/game; do NOT include #Shorts; no emoji",
        },
        "clips": items,
        "output": "Return ONLY JSON: {\"clips\":[{\"id\":\"clipNN\",\"hook\":\"...\",\"tiktok_caption\":\"...\","
                  "\"youtube_title\":\"...\",\"youtube_desc\":\"...\",\"hashtags\":[\"#a\",\"#b\"]}]}. "
                  "hashtags MUST be a JSON array, never a string.",
    }
    client = anthropic.Anthropic(api_key=key)
    log(f"[titles] writing per-platform metadata for {len(items)} clips with {cfg.ai_model} ...")
    msg = client.messages.create(model=cfg.ai_model, max_tokens=3500, system=_system(cfg),
                                 messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    data = _extract_json(text)
    out = {}
    for it in data.get("clips", []):
        if it.get("id"):
            out[it["id"]] = _platform_meta(cfg, it)
    return out
