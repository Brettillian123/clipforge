# ClipForge Studio

Turn a Twitch VOD into ready-to-post **vertical clips** for TikTok and YouTube Shorts —
then review and fine-tune every one in a local web editor.

It transcribes the stream, finds your most clippable moments, and renders each as a 9:16
video: **facecam on top, gameplay on the bottom, animated word-by-word captions, and a
persistent Twitch follow watermark.** Then the **Studio** dashboard lets you trim, restyle
captions, add text/shapes/emojis/images, drop in a chat screenshot, splice in more of the
stream for context, write viral titles with AI, approve, and batch-render — all locally.

Built for **TheEnchantingChicken**. Clips use the raw clip audio (no added music), so they're
safe to post as-is. Code lives in OneDrive (syncs to your Desktop); the venv, model, and
working files stay local in `C:\Users\Brett\clipforge`.

---

## Quick start

```powershell
# one-time setup (on the NVIDIA laptop):
powershell -ExecutionPolicy Bypass -File .\setup.ps1

# 1) make the first-pass clips from a VOD (transcribe + detect + render):
$env:PYTHONPATH="C:\Users\Brett\OneDrive\Documents\StreamingProject\clipper"
& C:\Users\Brett\clipforge\.venv\Scripts\python.exe clip.py "C:\Users\Brett\OneDrive\Documents\StreamingProject\Stream1.mp4"

# 2) open the Studio to review & edit:
& C:\Users\Brett\clipforge\.venv\Scripts\python.exe dashboard.py
```

`clip.py` writes the clips + a `project.json` next to the VOD (`...\clips\`). `dashboard.py`
opens the Studio at http://127.0.0.1:8765.

`clip.py` options: `--count N` (how many clips) · `--ai` (Claude picks + titles) ·
`--limit-secs 900` (fast test on first 15 min) · `--dry-run` (detect only) ·
`--dashboard` (render then open Studio).

---

## The Studio editor — what each part does

**Left — clip browser.** Thumbnails of every clip with score + length. An "edits" tag shows when
a clip has changes not yet in its downloaded file. Filter by New / Approved / Rejected.
**✨ AI titles (all)** writes a viral title + caption + hashtags for every clip in one click (needs
an API key). **Export approved (N)** renders the final files for everything you've approved.

**Center — the live stage.**
- Click a clip and it **just plays** (normal video controls). Whatever you add — text, shapes,
  emoji, chat — appears **instantly on the video**; you never render to preview. Drag to move, drag
  the gold dot to resize, arrow-keys to nudge.
- A **floating toolbar** sits on the selected element: color, size (A− / A+), duplicate, delete.
- The **add-element toolbar**: Text, Box, Circle, **Arrow** (opens a style popup: → ← ↑ ↓ ↗),
  Line, Emoji, Image, Hook (intro text), End CTA ("follow" card at the end), Safe-zone overlay.

**Right — panels.**
- **Element** — edit the selected element (text, size, color, outline, background pill, alignment;
  shape stroke/fill/radius; position X/Y/W/H; show-from/to time + fades). Duplicate or Delete.
- **Length / context** — adjust in/out, and **add another part of the stream** by typing VOD
  timecodes (it gets stitched in with the same layout + captions).
- **Layers** — every element, front-to-back; show/hide, reorder, and see its on-screen time.
- **Chat inset** — turn it on and the chat screenshot shows **live** on the video; choose corner,
  size, grab-time, and when it appears.
- **Captions** — toggle burn-in; change size, colors (word + active word), max words per line, and
  vertical position; **Fix words…** corrects any mis-transcribed word. (Caption/trim changes
  re-render the base preview — a few seconds.)
- **Post text (copy)** — per-platform title/caption/hashtags with a **Copy** button.
- **AI assist** — plain-English edits ("start where I first spot the prop", "punchier captions").

**The flow:** everything autosaves (per clip — switching clips never loses edits). The stage is a
**live preview** — add/move/recolor and you see it immediately, no rendering. When you want the
finished file, click **⬇ Download** (it renders everything burned-in and saves the .mp4). A badge
warns "edits not downloaded" whenever your latest changes aren't in the downloaded file yet, so you
always know to re-download. **Export approved (N)** does this in bulk for approved clips. Approve
(**A**) / reject (**X**) as you go; trimming/captions re-render the base preview, everything else is
instant.

**Shortcuts:** Space play/pause · ←/→ nudge element · I set in-point · A/X approve/reject ·
R render · Del delete element · Ctrl+Z / Ctrl+Shift+Z undo/redo · **? = full list**.

---

## How it works

Pipeline (`pipeline/`):
1. **audio** — decode to 16 kHz mono; numpy loudness + hi-freq features (no librosa/torch).
2. **transcribe** — faster-whisper `large-v3`, word timestamps, GPU (~12× realtime). Cached.
3. **detect** — score every second on six signals (text/energy/spike/laughter/sustain/variance),
   snap to sentence pauses, dedupe, keep the best (default ~30s clips).
4. **rerank / ai_edit / write_titles** *(optional, Claude)* — pick the best, write copy, and apply
   plain-English edits. Everything else is deterministic and needs no AI.
5. **render** — one ffmpeg pass per segment: facecam-top + gameplay-bottom, gold/lavender divider,
   ASS karaoke captions, then every overlay (watermark, chat, your elements) composited with fades.
   Segments are concatenated for multi-part clips. NVENC with an x264 fallback.

**The element system** is the spine: chat, watermark, text, shapes, images, and emojis are all
"elements" with the same shape — geometry in 1080×1920 render space, timing, style, and type-specific
data (`pipeline/elements.py`). The dashboard stores them in `clips\project.json`; the renderer turns
each into a Pillow PNG and overlays it. Because the stage uses the same render-space coordinates, the
preview is a true WYSIWYG of the output.

**Dashboard** is a dependency-free stdlib server (`pipeline/server.py`) + one vanilla-JS page
(`dashboard.html`). State is `clips\project.json` (one entry per clip — inspectable and recoverable).

### Platform format guide (researched, 2026)
| | TikTok | YouTube Shorts |
|---|---|---|
| Aspect / res | 9:16 / 1080×1920 | 9:16 / 1080×1920 |
| Length sweet spot | 15–30 s | 21–34 s (avoid sub-15 s) |
| #1 signal | completion rate (~70% = viral) | viewed-vs-swiped + avg view % |
| Hook | first 3 s, peak-first | first 3 s; the **title** is also a search lever |
| Hashtags | 3–5: broad + niche | 3–5 in description; include `#Shorts` |
| Captions | mandatory (sound-off viewing) | mandatory |

One 1080×1920 master serves both; only the title/caption/hashtags differ.

---

## Tuning & files
- All defaults live in `pipeline/config.py` (geometry, caption defaults, detection weights, chat
  region, AI model). Detection lexicons are in `pipeline/detect.py`.
- Output per VOD: `<vod folder>\clips\` → `clipNN.mp4`, `project.json` (Studio state), `clips.md`
  (quick review sheet).

## Notes / troubleshooting
- **Run heavy jobs on the NVIDIA laptop.** The Desktop's Radeon has no CUDA → CPU-only transcription
  (hours). Transcribe on the laptop; clips sync to the Desktop via OneDrive.
- **AI features** need an Anthropic API key — paste it in the Studio header once (stored locally in
  `C:\Users\Brett\clipforge\secret.json`, never synced) and install `anthropic` (`setup.ps1` does the
  rest). Model is Sonnet by default (cheap, fast); change `ai_model` in config for Opus.
- **`cannot load cudnn_ops64_9.dll`** — handled automatically; if it persists the run falls back to CPU.
- **Captions in the wrong font** — re-run `setup.ps1` (copies Poppins to `clipforge\fonts` for libass).
- **A clip's cam crop looks off** — it was a non-gameplay scene; the gold-border detector skipped it and
  used the default. Tune `cam_*` in config or use a manual cam box.
