#!/usr/bin/env python
"""ClipForge edit CLI - script clip edits with pure numbers (for Claude Code or you).

Every clip is plain data in clips/project.json. This CLI edits it with explicit
numbers: positions are pixels in a 1080x1920 canvas (x,y = top-left; x 0..1080,
y 0..1920), colors are 6-digit hex (no '#'), times are seconds from the clip start.

If the dashboard server is running it routes edits through it (so the dashboard
updates live); otherwise it edits project.json directly and renders standalone.

Examples:
  python cfedit.py list
  python cfedit.py show clip03
  python cfedit.py schema
  python cfedit.py add-text clip03 "LET'S GO" --x 90 --y 300 --w 900 --size 80 --color E7C58A --start 0 --end 3
  python cfedit.py add-shape clip03 --shape arrow --x 400 --y 800 --w 240 --h 180 --color FFD93D --orient diag --dir right --start 2 --end 5
  python cfedit.py add-emoji clip03 "🔥" --x 460 --y 300 --w 170 --start 1 --end 4
  python cfedit.py trim clip03 --start 11171 --end 11190
  python cfedit.py chat clip03 --on --grab-t 11175 --pos bottom-left --scale 0.45 --start 0 --end 6
  python cfedit.py rm clip03 --el e1a2b3        (or --all)
  python cfedit.py set clip03 --json "{\"captions_enabled\": false}"
  python cfedit.py render clip03               (base preview)   |   export clip03  (final file)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import config  # noqa: E402

DEFAULT_VOD = r"C:\Users\Brett\OneDrive\Documents\StreamingProject\Stream1.mp4"
BASE = f"http://127.0.0.1:{config.Config().dash_port}"
CANVAS = "1080x1920"

SCHEMA = f"""ClipForge clip schema (clips/project.json)
Canvas: {CANVAS}  (x: 0=left .. 1080=right ; y: 0=top .. 1920=bottom ; seam at y=768)
Layout: facecam fills the top (y 0-768), gameplay fills the bottom (y 768-1920).
Colors: 6-hex like E7C58A (brand gold), C3BBE6 (lavender), FFFFFF, FFD93D (yellow). No '#'.
Times: seconds from the clip's start. Element timing.end=null means until the clip ends.

A clip (one entry in project.json "clips"):
  id, rank, score, approved(null/true/false)
  segments: [{{"start": vodSec, "end": vodSec}}]   # 1+ VOD ranges concatenated; [0] is the main cut
  captions_enabled: bool
  caption_style: {{size, max_words, fill(hex), highlight(hex), margin_v(px from bottom)}}
  caption_words: [{{start,end,word}}]   # optional hand/AI-fixed words (single-segment only)
  chat: {{enabled, grab_t(vodSec), pos(bottom-left|bottom-right|top-left|top-right|center), scale(0-1), t_start, t_end}}
  metadata: {{tiktok:{{title,caption,hashtags[]}}, shorts:{{...}}}}
  elements: [ ELEMENT, ... ]

ELEMENT envelope:
  {{ id, type, z(int, higher=front), visible(bool), seg_index(int),
     geom: {{x, y, w, h(or null for text/emoji = auto)}},
     timing: {{start, end(or null), fadeIn, fadeOut}},
     style: {{...}}, data: {{...}} }}
Element types + their style/data:
  text  : style{{size,color,outline,outlineColor,bg(hex or ""),bgAlpha,radius,align(left|center|right),uppercase}} data{{text}}
  emoji : style{{}} data{{text:"🔥"}}  (rendered as Twemoji; size via geom.w)
  rect / roundrect / ellipse : style{{fill(hex or ""),fillAlpha,strokeColor,strokeW,radius}} data{{shape}}
  line / arrow : style{{strokeColor,strokeW}} data{{shape, orient(h|v|diag), dir(right|left|up|down)}}
  image : style{{radius}} data{{src:"C:\\\\path\\\\img.png"}}
"""


# ---------- server bridge ----------
def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=4) as r:
        return json.loads(r.read())


def _post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def server_up() -> bool:
    try:
        _get("/api/project")
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------- direct project.json access (server down) ----------
def _out_dir():
    return os.path.join(os.path.dirname(DEFAULT_VOD), "clips")


def _load_project():
    from pipeline import project as proj
    return proj.load_project(_out_dir())


def _save_project(p):
    from pipeline import project as proj
    proj.save_project(_out_dir(), p)


def get_clip(cid):
    if server_up():
        for c in _get("/api/project")["clips"]:
            if c["id"] == cid:
                return c
        raise SystemExit(f"no such clip: {cid}")
    for c in _load_project()["clips"]:
        if c["id"] == cid:
            return c
    raise SystemExit(f"no such clip: {cid}")


def update_clip(cid, fields):
    """Apply field changes to a clip (via server if up, else file)."""
    if server_up():
        _post(f"/api/clip/{cid}", fields)
        return
    p = _load_project()
    for c in p["clips"]:
        if c["id"] == cid:
            c.update(fields)
            _save_project(p)
            return
    raise SystemExit(f"no such clip: {cid}")


def render_clip(cid, export=False):
    if server_up():
        kind = "export" if export else "render"
        job = _post(f"/api/clip/{cid}/{kind}", {})["job"]
        while True:
            time.sleep(2)
            s = _get(f"/api/job/{job}")
            if s["status"] in ("done", "error"):
                print(f"render {s['status']}" + (": " + s.get("error", "") if s["status"] == "error" else ""))
                return
    # standalone
    from pipeline import branding, project as proj, render, transcribe, util
    cfg = config.load_config()
    p = _load_project()
    vod = p["vod"]
    transcript = util.read_json(transcribe.transcript_path(cfg, vod))
    wm = branding.render_watermark(cfg)
    spec = proj.spec_from_dict(get_clip(cid), cfg)
    fn = f"{cid}.export.mp4" if export else f"{cid}.mp4"
    out = os.path.join(_out_dir(), fn)
    render.render_spec(cfg, vod, spec, transcript, wm, out, include_overlays=export)
    print(f"rendered {out}")


def _uid():
    import random
    return "e" + "".join(random.choice("0123456789abcdef") for _ in range(6))


def _next_z(cid):
    return max([e.get("z", 1) for e in get_clip(cid).get("elements", [])] + [0]) + 1


def add_element(cid, el):
    el.setdefault("id", _uid())
    el.setdefault("z", _next_z(cid))
    el.setdefault("visible", True)
    el.setdefault("seg_index", 0)
    cur = get_clip(cid).get("elements", []) or []
    cur.append(el)
    update_clip(cid, {"elements": cur})
    print(f"added {el['type']} {el['id']} to {cid}")


def _timing(a):
    return {"start": a.start, "end": a.end, "fadeIn": a.fadein, "fadeOut": a.fadeout}


# ---------- commands ----------
def main():
    ap = argparse.ArgumentParser(description="Edit ClipForge clips by the numbers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("schema")
    sub.add_parser("list")
    sh = sub.add_parser("show"); sh.add_argument("clip")

    def common(p):
        p.add_argument("clip")
        p.add_argument("--start", type=float, default=0.0)
        p.add_argument("--end", type=float, default=None)
        p.add_argument("--fadein", type=float, default=0.3)
        p.add_argument("--fadeout", type=float, default=0.3)
        p.add_argument("--z", type=int, default=None)
        p.add_argument("--no-render", action="store_true")

    t = sub.add_parser("add-text"); common(t); t.add_argument("text")
    t.add_argument("--x", type=int, default=90); t.add_argument("--y", type=int, default=300)
    t.add_argument("--w", type=int, default=900); t.add_argument("--size", type=int, default=78)
    t.add_argument("--color", default="FFFFFF"); t.add_argument("--outline", type=int, default=9)
    t.add_argument("--bg", default=""); t.add_argument("--bgalpha", type=float, default=0.55)
    t.add_argument("--align", default="center"); t.add_argument("--upper", action="store_true")

    s = sub.add_parser("add-shape"); common(s)
    s.add_argument("--shape", required=True, choices=["rect", "roundrect", "ellipse", "line", "arrow"])
    s.add_argument("--x", type=int, default=560); s.add_argument("--y", type=int, default=980)
    s.add_argument("--w", type=int, default=360); s.add_argument("--h", type=int, default=240)
    s.add_argument("--color", default="E7C58A"); s.add_argument("--fill", default="")
    s.add_argument("--fillalpha", type=float, default=0.25); s.add_argument("--strokew", type=int, default=8)
    s.add_argument("--radius", type=int, default=20)
    s.add_argument("--orient", default="diag", choices=["h", "v", "diag"])
    s.add_argument("--dir", default="right", choices=["right", "left", "up", "down"])

    e = sub.add_parser("add-emoji"); common(e); e.add_argument("emoji")
    e.add_argument("--x", type=int, default=460); e.add_argument("--y", type=int, default=300); e.add_argument("--w", type=int, default=170)

    im = sub.add_parser("add-image"); common(im); im.add_argument("src")
    im.add_argument("--x", type=int, default=340); im.add_argument("--y", type=int, default=740)
    im.add_argument("--w", type=int, default=400); im.add_argument("--h", type=int, default=400); im.add_argument("--radius", type=int, default=0)

    ch = sub.add_parser("chat"); ch.add_argument("clip")
    ch.add_argument("--on", action="store_true"); ch.add_argument("--off", action="store_true")
    ch.add_argument("--grab-t", type=float, default=None); ch.add_argument("--pos", default=None)
    ch.add_argument("--scale", type=float, default=None); ch.add_argument("--start", type=float, default=None)
    ch.add_argument("--end", type=float, default=None); ch.add_argument("--no-render", action="store_true")

    tr = sub.add_parser("trim"); tr.add_argument("clip"); tr.add_argument("--start", type=float, required=True)
    tr.add_argument("--end", type=float, required=True); tr.add_argument("--no-render", action="store_true")

    asg = sub.add_parser("addseg"); asg.add_argument("clip"); asg.add_argument("--start", type=float, required=True)
    asg.add_argument("--end", type=float, required=True); asg.add_argument("--before", action="store_true"); asg.add_argument("--no-render", action="store_true")

    rm = sub.add_parser("rm"); rm.add_argument("clip"); rm.add_argument("--el", default=None); rm.add_argument("--all", action="store_true"); rm.add_argument("--no-render", action="store_true")

    st = sub.add_parser("set"); st.add_argument("clip"); st.add_argument("--json", required=True); st.add_argument("--no-render", action="store_true")

    rn = sub.add_parser("render"); rn.add_argument("clip")
    ex = sub.add_parser("export"); ex.add_argument("clip")

    a = ap.parse_args()
    cmd = a.cmd

    if cmd == "schema":
        print(SCHEMA); return
    if cmd == "list":
        clips = (_get("/api/project")["clips"] if server_up() else _load_project()["clips"])
        for c in clips:
            seg = c["segments"][0]
            print(f"{c['id']:8} rank {c.get('rank','?'):>2} | {seg['start']:.0f}-{seg['end']:.0f}s "
                  f"| {len(c.get('elements',[]))} elements | chat={'on' if (c.get('chat') or {}).get('enabled') else 'off'} "
                  f"| {c.get('metadata',{}).get('tiktok',{}).get('title','')[:40]}")
        return
    if cmd == "show":
        print(json.dumps(get_clip(a.clip), indent=2, ensure_ascii=False)); return

    render_after = not getattr(a, "no_render", False)
    if cmd == "add-text":
        el = {"type": "text", "geom": {"x": a.x, "y": a.y, "w": a.w, "h": None}, "timing": _timing(a),
              "style": {"size": a.size, "color": a.color, "outline": a.outline, "outlineColor": "000000",
                        "bg": a.bg, "bgAlpha": a.bgalpha, "radius": 18, "align": a.align, "uppercase": a.upper},
              "data": {"text": a.text}}
        if a.z is not None:
            el["z"] = a.z
        add_element(a.clip, el)
    elif cmd == "add-shape":
        el = {"type": a.shape, "geom": {"x": a.x, "y": a.y, "w": a.w, "h": a.h}, "timing": _timing(a),
              "style": {"fill": a.fill, "fillAlpha": a.fillalpha, "strokeColor": a.color, "strokeW": a.strokew, "radius": a.radius},
              "data": {"shape": a.shape, "orient": a.orient, "dir": a.dir}}
        if a.z is not None:
            el["z"] = a.z
        add_element(a.clip, el)
    elif cmd == "add-emoji":
        el = {"type": "emoji", "geom": {"x": a.x, "y": a.y, "w": a.w, "h": None}, "timing": _timing(a),
              "style": {}, "data": {"text": a.emoji}}
        if a.z is not None:
            el["z"] = a.z
        add_element(a.clip, el)
    elif cmd == "add-image":
        el = {"type": "image", "geom": {"x": a.x, "y": a.y, "w": a.w, "h": a.h}, "timing": _timing(a),
              "style": {"radius": a.radius}, "data": {"src": a.src}}
        if a.z is not None:
            el["z"] = a.z
        add_element(a.clip, el)
    elif cmd == "chat":
        ch = dict(get_clip(a.clip).get("chat") or {})
        if a.on:
            ch["enabled"] = True
        if a.off:
            ch["enabled"] = False
        for k, v in (("grab_t", a.grab_t), ("pos", a.pos), ("scale", a.scale), ("t_start", a.start), ("t_end", a.end)):
            if v is not None:
                ch[k] = v
        update_clip(a.clip, {"chat": ch})
        print(f"chat updated on {a.clip}")
    elif cmd == "trim":
        update_clip(a.clip, {"segments": [{"start": round(a.start, 2), "end": round(a.end, 2)}]})
        print(f"trimmed {a.clip} -> {a.start}-{a.end}")
    elif cmd == "addseg":
        segs = list(get_clip(a.clip)["segments"])
        seg = {"start": round(a.start, 2), "end": round(a.end, 2)}
        segs.insert(0, seg) if a.before else segs.append(seg)
        update_clip(a.clip, {"segments": segs})
        print(f"added segment to {a.clip}")
    elif cmd == "rm":
        els = get_clip(a.clip).get("elements", []) or []
        els = [] if a.all else [e for e in els if e.get("id") != a.el]
        update_clip(a.clip, {"elements": els})
        print(f"removed element(s) from {a.clip}")
    elif cmd == "set":
        update_clip(a.clip, json.loads(a.json))
        print(f"updated {a.clip}")
    elif cmd in ("render", "export"):
        render_clip(a.clip, export=(cmd == "export"))
        return

    if render_after:
        render_clip(a.clip, export=False)


if __name__ == "__main__":
    main()
