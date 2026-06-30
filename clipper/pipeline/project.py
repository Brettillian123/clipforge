"""Project + ClipSpec: the editable description of every clip in a job.

A project.json lives in the output clips/ folder. The dashboard reads it,
the user edits specs (trim, captions, chat, context segments, approval), and the
renderer turns each spec into an mp4. One spec can have several VOD segments
(for added context) and an optional chat inset.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field

# Strip emoji / pictographic symbols (the caption font has no glyphs for them -> tofu boxes on the
# titlecard). Keeps normal text + punctuation like — … ' (all below U+2190). The posting metadata
# keeps its emoji; only the burned-in titlecard is cleaned.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002190-\U00002BFF\U0000FE00-\U0000FE0F‍™ℹ]", re.UNICODE)


def _clean_titlecard_text(s: str) -> str:
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", s or "")).strip(" -—|·")

from . import config, util


def _chat_default(cfg: config.Config) -> dict:
    return {
        "enabled": False,
        "src": "auto",            # "auto" = grab from VOD; or a path to an uploaded image
        "image": None,
        "seg_index": 0,           # which segment the inset belongs to
        "grab_t": None,           # absolute VOD time to screenshot (None = segment start)
        "t_start": 0.0,           # show from this offset within the segment
        "t_end": cfg.chat_default_dur,
        "pos": cfg.chat_default_pos,
        "scale": cfg.chat_default_scale,
        "rect": list(cfg.chat_rect),  # source crop rectangle [x,y,w,h]
    }


@dataclass
class ClipSpec:
    id: str
    rank: int
    segments: list                     # [{"start": s, "end": e}, ...] in VOD seconds, concatenated in order
    score: float = 0.0
    keywords: list = field(default_factory=list)
    approved: object = None            # None | True | False
    captions_enabled: bool = True
    caption_style: dict = field(default_factory=dict)   # per-clip overrides (size/colors/position/...)
    caption_words: list = field(default_factory=list)   # optional fixed words [{start,end,word}] (single-segment)
    cam_mode: str = "auto"             # "auto" (gold-border detect) | "manual"
    cam: object = None                 # {"x","y","w","h"} when cam_mode == manual
    chat: dict = field(default_factory=dict)
    elements: list = field(default_factory=list)   # overlay elements: text/shape/image/emoji (see elements.py)
    metadata: dict = field(default_factory=dict)   # {"tiktok":{title,caption,hashtags}, "shorts":{...}}
    transcript: str = ""
    file: str = ""                     # rendered filename (relative to the clips dir)
    needs_render: bool = True
    render_status: str = "pending"     # pending | rendering | done | error
    render_error: str = ""
    notes: str = ""                    # AI-edit / user log

    @property
    def start(self) -> float:
        return self.segments[0]["start"] if self.segments else 0.0

    @property
    def duration(self) -> float:
        return round(sum(s["end"] - s["start"] for s in self.segments), 2)


def project_path(out_dir: str) -> str:
    return os.path.join(out_dir, "project.json")


def _titlecard_element(cfg: config.Config, title: str) -> dict:
    """A hook headline element auto-seeded over the first few seconds of a clip. A normal text element,
    so the user can reword / restyle / move / delete it in the studio like anything else."""
    return {
        "id": "titlecard", "type": "text", "visible": True, "seg_index": 0, "z": 50,
        "geom": {"x": cfg.tc_margin_lr, "y": cfg.tc_y, "w": cfg.out_w - 2 * cfg.tc_margin_lr, "h": None},
        "timing": {"start": 0.0, "end": float(cfg.titlecard_secs), "fadeIn": 0.0, "fadeOut": 0.3},
        "style": {"font": cfg.tc_font, "size": cfg.tc_size, "color": cfg.tc_color, "align": "center",
                  "outline": cfg.tc_outline, "outlineColor": "#000000", "bg": cfg.tc_bg,
                  "bgAlpha": cfg.tc_bg_alpha, "radius": 22, "pad": 22},
        "data": {"text": _clean_titlecard_text(title)},
    }


def build_project(cfg: config.Config, vod: str, cands: list, ai: bool) -> dict:
    from . import meta
    clips = []
    for rank, c in enumerate(cands, 1):
        cid = f"clip{rank:02d}"
        md = {}
        if ai and getattr(c, "meta", None):
            m = c.meta                                  # already per-platform (rerank._platform_meta)
            md = {"tiktok": dict(m.get("tiktok") or {}), "shorts": dict(m.get("shorts") or {})}
        else:
            md = {p: meta.local_metadata(cfg, c, p) for p in ("tiktok", "shorts")}
        elements = []
        if getattr(cfg, "titlecard", False):
            hook = ((md.get("tiktok") or {}).get("title") or "").strip()
            if hook:                       # seed a hook titlecard over the first few seconds
                elements.append(_titlecard_element(cfg, hook))
        spec = ClipSpec(
            id=cid, rank=rank,
            segments=[{"start": c.start, "end": c.end}],
            score=c.score, keywords=c.keywords,
            metadata=md, transcript=c.text, elements=elements,
            chat=_chat_default(cfg),
        )
        clips.append(asdict(spec))
    return {"vod": vod, "model": cfg.model, "clips": clips}


def load_project(out_dir: str) -> dict:
    """Load project.json, recovering from the last-good .bak if the main file is
    corrupt (e.g. a OneDrive sync conflict) so a damaged file never strands all the
    user's clip edits."""
    p = project_path(out_dir)
    try:
        return util.read_json(p)
    except Exception as e:                      # noqa: BLE001  (JSON/OS errors -> try the backup)
        bak = p + ".bak"
        if os.path.exists(bak):
            util.log(f"[project] {p} unreadable ({e}); recovering from {bak}")
            data = util.read_json(bak)
            try:
                util.write_json(p, data)        # restore the main file from the backup
            except OSError:
                pass
            return data
        raise


def save_project(out_dir: str, project: dict) -> None:
    p = project_path(out_dir)
    util.write_json(p, project)                 # atomic (tmp + os.replace)
    try:
        import shutil
        shutil.copy2(p, p + ".bak")             # keep a last-good snapshot for recovery
    except OSError:
        pass


def spec_from_dict(d: dict, cfg: config.Config) -> ClipSpec:
    known = {f for f in ClipSpec.__dataclass_fields__}
    spec = ClipSpec(**{k: v for k, v in d.items() if k in known})
    if not spec.chat:
        spec.chat = _chat_default(cfg)
    return spec


def chat_default(cfg: config.Config) -> dict:
    return _chat_default(cfg)
