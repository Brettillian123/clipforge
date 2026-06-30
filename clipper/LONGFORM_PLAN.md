# Long-form YouTube videos from a VOD — plan, design & build

Turn one Twitch VOD into a handful of **20–90 minute, upload-ready long-form YouTube
videos** (16:9, chaptered, captioned, titled), reusing the ClipForge transcript +
audio-engagement signals. This complements the existing short/vertical clip pipeline.

## 1. What the research says (so the format is right)

YouTube long-form in 2025–26 rewards **satisfaction-weighted watch time**: a video with
high average-view-duration (AVD) and retention ranks far above a high-view, low-retention
one. Concretely, for a streamer:

- **Raw, unedited VOD uploads are dead** for growth. Lightly *packaged* sessions win:
  trimmed of dead air, **chaptered**, with a strong **title + thumbnail** and a hook in
  the first ~15s (videos that hook in 15s keep ~65% to the 3-min mark).
- **Chapters increase watch time** (viewers feel in control, navigate instead of leaving)
  and double as SEO. YouTube needs: first chapter at `0:00`, **≥3 chapters**, each **≥10s**.
- **Length:** edited highlight/session videos of ~20–60 min are the sweet spot for a
  growing channel; 60–90 min suits full sessions for an existing audience. We target
  **20–90 min**, aiming ~30–60.
- **Captions** (uploaded `.srt`) add accessibility + SEO and lift retention.
- Sources: AIR Media-Tech retention editing; influenceflow 2026 long-form guide;
  usevisuals/chapter-generator chapter best-practices; humbleandbrag retention benchmarks.

**Implication for the build:** produce *clean session cuts* — one 16:9 video per
game-session/engagement block, **leading/trailing dead air trimmed**, **auto-chapters**
(YouTube format), a **sidecar `.srt`**, and a ready-to-paste **title + description + tags**.
Heavier edits (internal dead-air removal, cold-open montage) are staged as v2 (§6).

## 2. What we reuse (no new heavy deps)

- **Transcript** (`transcribe`, cached): word timestamps + segments. Used for activity,
  chapter labels, titles, and the `.srt`.
- **Audio signals** (`audio`): `per_second_features` (loudness dB), `frame_rms_db` +
  `silence_map` (dead-gap detection), `speech_ref_db` (speech baseline).
- **`meta.guess_game`** for the game name; **`branding.render_watermark`** for the corner mark.
- **`render._run_ffmpeg`** (cancellable, progress-streaming), `_encode_args`, `_nvenc_ok`.

## 3. Algorithm (in `pipeline/longform.py`)

### 3.1 Engagement & activity (per second)
- `excite[t] = sec_db[t] - speech_ref` (relative loudness).
- `talk[t]` = words/sec from the transcript; `laugh[t]` = laugh-token count.
- `active[t]` = talking OR loud (content present) vs **dead** (BRB/AFK/intermission).
- `interest[t] = max(excite,0) + 1.5*laugh + 0.5*talk` (for scoring + chapter peaks).

### 3.2 Session segmentation (→ 20–90 min blocks)
1. Collapse to **per-minute activity**; a minute is **dead** if `active` fraction < `lf_dead_frac`.
2. Split the VOD at runs of **≥ `lf_break_min` consecutive dead minutes** (stream breaks /
   game switches) → raw session blocks.
3. Enforce length: drop blocks `< lf_min_min` (merge into a neighbor if the gap is small);
   split blocks `> lf_max_min` at their lowest-activity interior minute, recursively, aiming
   near `lf_target_min`.
4. **Trim** leading/trailing dead minutes inside each block (start on action).
5. **Score** each block by mean `interest` (for ordering / which to upload first).

### 3.3 Chapters (per segment, YouTube-valid)
- Place a chapter every ~`lf_chapter_min` minutes, **snapped** to a nearby engagement peak,
  then to the nearest sentence boundary; always include `0:00`.
- Enforce ≥3 chapters and ≥10s spacing (merge too-close ones).
- **Label** each chapter from the top content keywords in its window (stop-word filtered),
  e.g. `Tarkov raid`, `prop hunt chaos`; fallback `Part N`. First chapter = `Intro`.
- Times are **relative to the segment start**, formatted `h:mm:ss`.

### 3.4 Packaging
- **Title** (local heuristic; AI optional later): `{Game} — {top keywords}` / `Funniest
  {Game} Moments` style, ≤100 chars.
- **Description**: 1–2 line blurb + **chapter list** (the timestamps YouTube reads) +
  follow CTA (`@handle`) + tags line.
- **Tags**: game + `gaming` + handle + generics.
- **`.srt`**: transcript words in `[start,end]`, offset to segment start, grouped into
  readable lines (by sentence/≤N words) → standard SRT for YouTube subtitle upload.

### 3.5 Render (16:9, keep the stream's own layout)
- One ffmpeg pass per segment: `-ss start -t dur`, keep source `1920×1080` (or `lf_height`),
  optional corner **watermark** overlay, NVENC→x264 fallback, AAC audio.
- Uses `render._run_ffmpeg` for **live progress** and **cancellation** (same infra as clips).
- Output is the *clean cut*; chapters/`.srt` map 1:1 to it.

## 4. Outputs — `<vod_dir>/longform/`
- `seg01.mp4` … (the videos)
- `seg01.srt` (subtitles), `seg01.description.txt` (paste-ready title + description + chapters + tags)
- `longform.json` (manifest), `longform.md` (review sheet: pick which to upload)

## 5. How it's used (easy)
- **One command:** `python longform.py "C:\…\Stream1.mp4"`
  (`--plan-only` to just plan + write the review sheet fast; `--count N`, `--height 720`, `--watermark/--no-watermark`).
- **Dashboard:** a **Long-form** tab — *Plan long-form videos* button → list of planned
  videos (title, length, chapters, score) → per-video **Render & package** (live progress,
  cancellable, reusing the clip render infra) → **Download** + copy the description.

## 6. Staged enhancements (v2, intentionally not in v1 so v1 stays correct & tested)
- **Internal dead-air removal** (concat kept-spans) with chapter/`.srt` timestamp remapping.
- **Cold-open hook**: prepend a ~20–30s montage of the segment's top moments.
- **AI titles/descriptions/thumbnails** (reuse the Anthropic key like the clip pipeline).
- **Auto game-boundary detection** via scene-change + on-stream overlay OCR.

## 7. Config knobs (`pipeline/config.py`, `lf_*`)
`lf_min_min=20, lf_max_min=90, lf_target_min=45, lf_break_min=2.5, lf_dead_frac=0.12,
lf_chapter_min=6, lf_height=1080, lf_watermark=True, lf_count=6`.
