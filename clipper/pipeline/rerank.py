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
    """Authentic-but-HYPE rules: keep the fun/energy, just tie it to the specific moment so it reads
    real instead of like generic AI marketing. (User: don't flatten the tone, never use em dashes.)"""
    return (
        "VOICE — match how this streamer talks when they're hyped: high-energy, funny, punchy, and "
        "SPECIFIC. This is short-form for a comedic gaming channel, so KEEP IT FUN and full of hype; that "
        "energy is what makes people watch and share. The thing to avoid is FAKE, copy-paste AI-marketing "
        "hype, not excitement itself. So tie the hype to what literally happens in THIS clip: name the "
        "actual thing ('chat voted to sell my whole stash', 'third wipe on the same boss', 'he proposed "
        "to me mid raid') instead of generic lines that could sit on any clip. CAPS for emphasis and big "
        "reactions are great, and CURRENT internet slang, memes, and trending phrases are encouraged "
        "whenever they genuinely fit the clip (e.g. 'absolute cinema', 'it's giving', 'caught in 4k') "
        "because they read as in-the-know and funny. What IS banned is generic corporate or AI-marketing "
        "filler that could sit on any clip: 'you won't believe', 'wait for it', 'this broke the internet', "
        "'this changes everything'. Words like insane / clutch / chaos / unhinged are welcome when the "
        "moment truly earns them. "
        "HARD RULES: no emojis anywhere (titles, captions, descriptions, hashtags), and never use em "
        "dashes; use commas or periods instead. Keep hashtags out of the caption sentence, vary the "
        "phrasing across clips, and START every caption, title, and sentence with a capital letter "
        "(mid-line CAPS for emphasis are still welcome)."
    )


def _platforms(cfg) -> str:
    """TikTok and YouTube need DIFFERENT copy — the user explicitly wants them tailored, not identical."""
    return (
        "TWO PLATFORMS, DIFFERENT COPY. Never write identical text for both; duplicate copy hurts reach "
        "and search on each. "
        "TikTok caption: punchy and high-energy, first person, like a hyped gamer posting their own clip. "
        "One or two short lines, about 70-140 characters. Lead with the specific moment (big energy is "
        f"good), then end with a natural nod to the stream (e.g. {_cta(cfg)}). "
        "YouTube Shorts title: a clear, SEARCHABLE headline. Put the GAME NAME and the specific moment or "
        "outcome in the first ~40 characters (the feed truncates there) so it works in search and in-feed. "
        "Punchy and exciting is good; keep it accurate, no bait. The YouTube description is one readable "
        "line of context."
    )


def _trends(cfg) -> str:
    """Optional 'what's trending now' block for the system prompt, read from a local trends.json
    (refreshed by a research pass). Looks at cfg.trends_path, then <LOCAL_ROOT>/trends.json, then the
    seed bundled next to this package. Returns '' if none found / empty / unreadable, so generation
    behaves exactly as before when there is no list."""
    import time
    for p in ((getattr(cfg, "trends_path", "") or "").strip(),
              os.path.join(config.LOCAL_ROOT, "trends.json"),
              os.path.join(os.path.dirname(__file__), "trends.json")):
        if p and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                break
            except (OSError, ValueError):
                continue
    else:
        return ""
    if not isinstance(d, dict):
        return ""
    pick = lambda k: [str(x).strip() for x in (d.get(k) or []) if str(x).strip()][:12]
    gen, tt, yt = pick("general"), pick("tiktok"), pick("youtube")
    if not (gen or tt or yt):
        return ""
    updated = str(d.get("updated") or "").strip()
    stale = ""
    try:
        if updated and (time.time() - time.mktime(time.strptime(updated, "%Y-%m-%d"))) / 86400.0 > 30:
            stale = " (may be stale; treat as a loose guide)"
    except ValueError:
        pass
    out = ["CURRENTLY TRENDING" + (f" as of {updated}" if updated else "") + stale +
           ". These formats/phrases are doing numbers on short-form right now. Use any that GENUINELY "
           "fit this clip; never force one in, and use at most one trend phrase per caption."]
    if gen:
        out.append("Both platforms: " + "; ".join(gen) + ".")
    if tt:
        out.append("TikTok-leaning: " + "; ".join(tt) + ".")
    if yt:
        out.append("YouTube Shorts-leaning: " + "; ".join(yt) + ".")
    return "\n".join(out)


def _system(cfg) -> str:
    base = (
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
    tr = _trends(cfg)
    return base + ("\n\n" + tr if tr else "")


def _platform_meta(cfg, item: dict) -> dict:
    """Map one AI result into the per-platform metadata the app stores: a punchy on-screen hook +
    conversational TikTok caption, and a searchable YouTube title + plain description. Distinct copy per
    platform; emoji stripped defensively; #Shorts added to the YouTube tags automatically."""
    from . import meta as _meta
    clean = _meta.clean_copy                        # strips emoji + em dashes, keeps CAPS
    txt = lambda v: _meta.fix_pronoun_i(_meta.sentence_case_starts(clean(v)))  # caps sentence starts + lone "i"
    hook = txt(item.get("hook") or item.get("youtube_title") or "")[:60]
    tt_cap = txt(item.get("tiktok_caption") or "")
    yt_title = txt(item.get("youtube_title") or hook)[:60]
    yt_desc = txt(item.get("youtube_desc") or "")
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
            "hook": "<=55 chars: punchy, high-energy on-screen hook = the specific moment (also burned in "
                    "as the titlecard); CAPS ok; no emoji, no em dashes",
            "tiktok_caption": "punchy, high-energy TikTok caption, ~70-140 chars: specific moment first, "
                              f"then a natural nod to the stream ({_cta(cfg)}); no emoji, no em dashes, no "
                              "hashtags in the text",
            "youtube_title": "<=60 chars (aim 25-45): searchable, game name + the specific moment/outcome "
                             "in the first ~40 chars; exciting but accurate; no emoji, no em dashes, no clickbait",
            "youtube_desc": "one readable line of context for the YouTube description; no emoji, no em dashes",
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
            "hook": "<=55 chars: punchy, high-energy on-screen moment; CAPS ok; no emoji, no em dashes",
            "tiktok_caption": "punchy, high-energy, ~70-140 chars: specific moment first, then a natural "
                              f"nod to the stream ({_cta(cfg)}); no emoji, no em dashes, no hashtags in the text",
            "youtube_title": "<=60 chars (aim 25-45): searchable, game + specific moment in the first ~40 "
                             "chars; exciting but accurate; no emoji, no em dashes, no bait",
            "youtube_desc": "one readable line of context; no emoji, no em dashes",
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
