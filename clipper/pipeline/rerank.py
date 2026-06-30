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
        "no matter how much talking there is. Re-rank by viral potential, not by the input order."
    )


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
        "task": f"Select the {count} best clips and rank them best-first.",
        "for_each": {
            "id": "the candidate id you are selecting",
            "llm_score": "0-100 your viral-potential score",
            "title": "<=60 chars, punchy, front-load the payoff/keyword (works as a YouTube Shorts title)",
            "caption": f"1 short line for TikTok; end with: {_cta(cfg)}",
            "hashtags": "4-5 tags; mix 1-2 broad + 2-3 niche/game-specific; include #Shorts for youtube",
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
        from . import meta as _meta
        c.meta = {
            "title": item.get("title", "")[:60],
            "caption": item.get("caption", ""),
            "hashtags": _meta.coerce_hashtags(item.get("hashtags")),
            "platform_notes": item.get("platform_notes", {}),
            "reason": item.get("reason", ""),
            "source": "claude",
        }
        chosen.append(c)
    log(f"[rerank] Claude selected {len(chosen)} clips.")
    return chosen or cands


def write_titles(cfg: config.Config, clips: list) -> dict:
    """Write viral title/caption/hashtags for an EXISTING set of clips (no re-ranking,
    no render). Returns {clip_id: {title, caption, hashtags[]}}. One Claude call."""
    from .util import get_api_key
    key = get_api_key()
    if not key:
        raise RuntimeError("No Anthropic API key.")
    import anthropic
    items = [{"id": c["id"], "keywords": c.get("keywords", []),
              "profanity": c.get("profanity", False), "transcript": (c.get("transcript", "") or "")[:700]}
             for c in clips]
    payload = {
        "task": f"For EACH clip id, write short-form metadata for {_channel_descriptor(cfg)}. "
                "Title <=60 chars, punchy, front-load the payoff; 1-2 emoji ok. "
                f"Caption: one line, end with '{_cta(cfg)}'. "
                "hashtags: 3-4 as a JSON ARRAY of strings (e.g. ['#Roblox','#Shorts']), mix broad + niche/game.",
        "clips": items,
        "output": "Return ONLY JSON: {\"clips\":[{\"id\":\"clipNN\",\"title\":\"...\",\"caption\":\"...\","
                  "\"hashtags\":[\"#a\",\"#b\"]}]}. hashtags MUST be a JSON array, never a string.",
    }
    client = anthropic.Anthropic(api_key=key)
    log(f"[titles] writing metadata for {len(items)} clips with {cfg.ai_model} ...")
    msg = client.messages.create(model=cfg.ai_model, max_tokens=3000, system=_system(cfg),
                                 messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    data = _extract_json(text)
    out = {}
    for it in data.get("clips", []):
        if it.get("id"):
            out[it["id"]] = {"title": (it.get("title", "") or "")[:60],
                             "caption": it.get("caption", ""),
                             "hashtags": it.get("hashtags", [])}
    return out
