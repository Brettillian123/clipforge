# ClipForge Studio — app & CLI reference

Turn a Twitch VOD into ready-to-post **vertical clips** for TikTok and YouTube Shorts — then review and
fine-tune every one in a local editor, and post or schedule them without leaving the app.

> **New here? Start with the [repository README](../README.md)** for install + a quick tour, and
> **[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)** for how it all works. This page is the in-depth
> reference for the editor, the CLI, posting setup, and tuning.

It transcribes the stream, finds your most clippable moments, and renders each as a 9:16 video:
**facecam on top, gameplay on the bottom, animated word-by-word captions, a follow watermark, and a hook
titlecard.** The **Studio** then lets you trim, restyle captions, add text/shapes/emojis/images, drop in
a chat screenshot, splice in more of the stream, write viral titles with AI, approve, batch-render, and
post — all locally. Clips use the raw clip audio (no added music), so they're safe to post as-is.

---

## Quick start

```powershell
# one-time setup (creates a local venv under %USERPROFILE%\clipforge, installs deps, copies fonts):
powershell -ExecutionPolicy Bypass -File .\setup.ps1

# launch the Studio (or just double-click ClipForge.bat):
$env:PYTHONPATH = (Get-Location)
& "$env:USERPROFILE\clipforge\.venv\Scripts\python.exe" dashboard.py
```

The Studio opens as its own desktop window at `http://127.0.0.1:8765`. Drop stream recordings (`.mp4`)
in your **Videos** folder and they show up on the Home screen — pick one and hit **🎬 Make clips**.

**CLI (one-off, no UI):**

```powershell
$env:PYTHONPATH = (Get-Location)
& "$env:USERPROFILE\clipforge\.venv\Scripts\python.exe" clip.py "C:\path\to\your-vod.mp4"
```

`clip.py` writes the clips + a `project.json` to `"<vod> - clips\"`. Options: `--count N` ·
`--ai` (Claude picks + titles) · `--limit-secs 900` (fast test on first 15 min) · `--dry-run`
(detect only) · `--dashboard` (render then open the Studio).

---

## The Studio editor — what each part does

**Left — clip browser.** Thumbnails of every clip with score + length. An "edits" tag shows when a clip
has changes not yet in its downloaded file; a green **✓ posted** tag shows once a clip has been uploaded
(and the post box lists where + when, so you don't double-post). Filter by New / Approved / Rejected. **✨ AI titles (all)**
writes **platform-tailored** copy for every clip in one click: a searchable YouTube title, a
high-energy TikTok caption, and hashtags, in an authentic voice with **no emojis or em dashes** (needs an API key).
**Export approved (N)** renders the final files for everything you've approved. **✅ Finish review** is
the end-of-pass one-stop: it posts every **approved** clip to your connected accounts (rendering any that
aren't downloaded yet, then auto-posting) and deletes the **rejected** ones in one place.

**Center — the live stage.**
- Click a clip and it **just plays**. Whatever you add — text, shapes, emoji, chat — appears
  **instantly on the video**; you never render to preview. Drag to move, drag the gold dot to resize,
  arrow-keys to nudge.
- A **floating toolbar** sits on the selected element: color, size (A− / A+), duplicate, delete.
- The **add-element toolbar**: Text, Box, Circle, **Arrow** (→ ← ↑ ↓ ↗), Line, Emoji, Image, Hook
  (intro text), End CTA, Safe-zone overlay.

**Right — panels.**
- **Element** — edit the selected element (text/size/color/outline/background pill/alignment; shape
  stroke/fill/radius; position X/Y/W/H; show-from/to + fades). Duplicate or Delete.
- **Length / parts** — adjust in/out, and **splice in another part of the stream** by VOD timecodes
  (same layout + captions, with or without that part's audio).
- **Layers** — every element, front-to-back; show/hide, reorder, see on-screen time.
- **Chat inset** — turn it on and a chat screenshot shows **live** on the video; choose corner, size,
  grab-time, and when it appears.
- **Captions** — toggle burn-in; change size, colors, max words per line, vertical position;
  **Fix words…** corrects a mis-transcribed word; **✨ AI clean up** fixes filler/stutters in sync.
- **Post / schedule** — per-platform title/caption/hashtags (with **Copy**), plus **Post now /
  Schedule** to your connected YouTube + TikTok accounts.
- **AI assist** — plain-English edits ("start where I first spot the prop", "punchier captions").

**The flow:** everything autosaves per clip (switching clips never loses edits). The stage is a **live
preview**; when you want the finished file, click **⬇ Download** (renders everything burned-in). A badge
warns "edits not downloaded" whenever your latest changes aren't in the downloaded file yet.

**Shortcuts:** Space play/pause · `,`/`.` step a frame · `[`/`]` prev/next clip · A/X approve/reject ·
arrow keys nudge a selected element · Del delete · Ctrl+Z / Ctrl+Shift+Z undo/redo · **? = full list**.

---

## Posting setup

ClipForge posts finished clips to **your own** YouTube + TikTok accounts. It's **semi-automated and
free**: YouTube uploads land **private** (flip to public in YouTube Studio), and TikTok lands in your
**inbox** (tap the app notification to add a caption and post). You register a developer app on each platform once
and paste the client keys into the Studio's **🚀 Posting** panel — OAuth runs through the app's own
loopback redirect (`http://127.0.0.1:8765/oauth2/<platform>/callback`), so no public website is needed.

Copy the exact redirect URI from each card in the Posting panel (it's authoritative if your port differs).

**YouTube — [Google Cloud Console](https://console.cloud.google.com):**
1. New project → **APIs & Services → Library** → enable **YouTube Data API v3**.
2. **APIs & Services → OAuth consent screen** (opens the **Google Auth Platform**): **Get started** →
   App name + support email → **Audience: External** → developer email → Create. On **Audience**,
   confirm **Testing** and add yourself under **Test users**.
3. **Data Access → Add or remove scopes** → add `…/auth/youtube.upload` and `…/auth/youtube.readonly` → Save.
4. **Clients → Create client** → application type **Web application** (⚠️ *not* Desktop — only a Web
   client lets you register the exact loopback path) → **Authorized redirect URIs** → paste the
   YouTube redirect URI (exact, no trailing slash) → Create.
5. Paste the **Client ID + secret** (shown once — also Download JSON) into the Posting panel →
   **Save** → **Connect** → on the "unverified app" screen choose **Advanced → Go to ClipForge**.

*Expect:* uploads land **Private** (flip to public in YouTube Studio until you pass Google's audit), and
Testing-mode sign-in expires ~weekly (just reconnect).

**TikTok — [TikTok for Developers](https://developers.tiktok.com):**
1. **Manage apps → Connect an app** → fill basic info, and under **Platforms select Desktop**
   (⚠️ *critical* — the **Web** platform rejects `http://127.0.0.1`; **Desktop** allows the loopback URI
   natively, no tunnel). Paste your privacy/terms URLs if prompted.
2. **Add products: Login Kit + Content Posting API.**
3. Add scopes **`user.info.basic` + `video.upload`** only — do **not** add `video.publish` or enable
   **Direct Post** (those trigger the public-posting audit; `video.upload` = the inbox/draft path, no audit).
4. Under **Login Kit → Redirect URI**, paste the TikTok redirect URI exactly (match what the app sends).
5. Toggle to **Sandbox → Create Sandbox**, then **Target users → Add account** and log into your own
   TikTok (a new target user can take up to ~1 hour to activate).
6. Paste the **Client key + secret** (use the **Sandbox** credentials if shown) into the Posting panel →
   **Save** → **Connect**.

*Expect:* clips go to your **TikTok inbox** — open the app, tap the notification to add a caption and post (public is your
choice there). Sandbox caps uploads at ~128 MB (clips are far under).

Tokens are stored locally in `<clipforge-home>/posting.json` and are never committed. Scheduled posts
fire from a local timer, so the app must be running at the scheduled time.

---

## How it works (short version)

Pipeline (`pipeline/`): **audio** (16 kHz PCM + loudness/HF features) → **transcribe**
(whisper.cpp/Vulkan on AMD, or faster-whisper/CUDA on NVIDIA, cached) → **detect** (score every second
on six signals, snap to pauses, dedupe) → optional **rerank/ai_edit/write_titles** (Claude) →
**render** (one ffmpeg pass per segment: facecam-top + gameplay-bottom, divider, ASS karaoke captions,
then every overlay composited, with NVENC/AMF + a decode-verify libx264 fallback).

The **element system** is the spine: chat, watermark, text, shapes, images, emojis, and the titlecard
are all "elements" with the same shape (geometry in 1080×1920 render space, timing, style, type-data).
The Studio is a stdlib HTTP server (`pipeline/server.py`) + one vanilla-JS page (`dashboard.html`); clip
state is `project.json`. See **[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)** for the full picture.

### Platform format guide (researched, 2026)
| | TikTok | YouTube Shorts |
|---|---|---|
| Aspect / res | 9:16 / 1080×1920 | 9:16 / 1080×1920 |
| Length sweet spot | 15–30 s | 21–34 s (avoid sub-15 s) |
| #1 signal | completion rate | viewed-vs-swiped + avg view % |
| Hook | first 3 s, peak-first | first 3 s; the **title** is also a search lever |
| Hashtags | 3–5: broad + niche | 3–5 in description; include `#Shorts` |
| Captions | mandatory (sound-off) | mandatory |

---

## Make it yours & tuning

- **Personalise without editing code:** drop a `config.json` in your ClipForge home
  (`%USERPROFILE%\clipforge\config.json`) to set `wm_handle`, `channel_name`, `channel_persona`, or any
  other `Config` field. See the [repo README](../README.md#make-it-yours).
- **All defaults** live in `pipeline/config.py` (geometry, caption defaults, detection weights, chat
  region, AI model, encoder). Detection lexicons are in `pipeline/detect.py`.
- **Output per VOD:** `"<vod> - clips\"` → `clipNN.mp4` (editable base), `Ready to post\clipNN.mp4`
  (final burn-in) + `clipNN.jpg` cover, `project.json` (Studio state).

## Notes / troubleshooting
- **Use a GPU machine for the heavy transcription.** No CUDA/Vulkan → CPU-only (slow on a long VOD).
- **AI features** need an Anthropic API key — paste it in the Studio header once (stored locally in
  `<clipforge-home>/secret.json`, never synced). Model is Sonnet by default; change `ai_model` in config.
- **`cannot load cudnn_ops64_9.dll`** — handled automatically; if it persists the run falls back to CPU.
- **Captions in the wrong font** — re-run `setup.ps1` (copies Poppins to `<clipforge-home>/fonts`).
- **A clip's cam crop looks off** — it was a non-gameplay scene; the gold-border detector skipped it and
  used the default. Tune `cam_*` in config or use the Studio's manual cam mode.
