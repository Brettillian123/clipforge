# Editing clips with Claude Code

This project (ClipForge) turns a Twitch VOD into vertical clips. **You (Claude Code) can edit
clips directly from a chat prompt** — all clip data is plain numbers, so an edit is just changing
data + re-rendering. When the user asks for an edit like "add a gold arrow pointing at the gun in
clip 3 from 2–5s," do it with the `cfedit.py` CLI.

## Where the data lives
`clips/project.json` (next to the VOD, i.e. `..\Stream1.mp4` → `..\clips\project.json`). One entry
per clip. Everything is pure data: pixel coordinates, hex colors, seconds.

## How to run things
Use the project's venv python, from the `clipper` directory:
```
& C:\Users\Brett\clipforge\.venv\Scripts\python.exe cfedit.py <command> ...
```
`cfedit.py` auto-detects the running dashboard (http://127.0.0.1:8765): if it's up, edits go through
it so the dashboard updates live (user clicks **↻ Reload**); if it's down, it edits project.json and
renders standalone.

## The CLI
- `cfedit.py schema` — full schema + coordinate system (READ THIS FIRST if unsure).
- `cfedit.py list` — all clips (id, time, #elements, title).
- `cfedit.py show <id>` — a clip's full spec JSON.
- `cfedit.py add-text <id> "TEXT" --x --y --w --size --color --bg --start --end [--upper]`
- `cfedit.py add-shape <id> --shape arrow|rect|roundrect|ellipse|line --x --y --w --h --color --strokew --orient h|v|diag --dir right|left|up|down --start --end`
- `cfedit.py add-emoji <id> "🔥" --x --y --w --start --end`
- `cfedit.py add-image <id> "C:\path.png" --x --y --w --h`
- `cfedit.py chat <id> --on|--off --grab-t --pos --scale --start --end`
- `cfedit.py trim <id> --start <vodSec> --end <vodSec>`
- `cfedit.py addseg <id> --start <vodSec> --end <vodSec> [--before]`
- `cfedit.py rm <id> --el <elementId> | --all`
- `cfedit.py set <id> --json '<partial spec JSON>'`  (generic merge: captions_enabled, caption_style, metadata, …)
- `cfedit.py render <id>` (base preview) | `cfedit.py export <id>` (final downloadable file)

Add commands auto-render the base preview unless you pass `--no-render`. Batch several edits with
`--no-render`, then `render` once.

## Coordinate system (memorize)
- Canvas **1080×1920**. `x`: 0=left … 1080=right. `y`: 0=top … 1920=bottom. The seam is at y=768
  (facecam fills y 0–768, gameplay fills y 768–1920).
- `geom` = top-left `x,y` + `w,h` in px (text/emoji use `h:null` = auto height).
- Colors = 6-hex, no `#` (gold `E7C58A`, lavender `C3BBE6`, yellow `FFD93D`, white `FFFFFF`).
- Times = seconds. Element `timing.start/end` are relative to the clip; `end:null` = until clip end.
  `segments`, `chat.grab_t`, and `trim` use absolute **VOD** seconds.

## Workflow
1. `cfedit.py list` to find the clip id the user means (they reference clips by # in the dashboard).
2. `cfedit.py show <id>` if you need the current state (e.g. to position relative to existing elements).
3. Apply the edit(s) with the commands above.
4. Tell the user to click **↻ Reload** in the dashboard (or it shows on next open).

Keep edits to pure numbers; don't hand-edit rendered files. See `README.md` for the full design.

## Long-form (separate feature)
ClipForge also turns the VOD into **20–90 min 16:9 YouTube videos** (chaptered, captioned,
titled) — see `LONGFORM_PLAN.md`. It's independent of the vertical-clip pipeline above.
- **CLI:** `& C:\Users\Brett\clipforge\.venv\Scripts\python.exe longform.py "<vod>"`
  (`--plan-only` for a fast plan + review sheet, `--count N`, `--height 720`, `--no-watermark`).
- **Dashboard:** the **🎬 Long-form** button (header) opens `/longform` — Plan → per-video
  Render & package (live progress) → Download + Copy description.
- **Output:** `longform/` next to the VOD: `seg##.mp4` + `.srt` + `.description.txt` (paste-ready
  title/chapters/tags) + `longform.json` (manifest) + `longform.md` (review sheet).
- **Code:** `pipeline/longform.py` (plan/render/package). Knobs are the `lf_*` fields in `config.py`.
