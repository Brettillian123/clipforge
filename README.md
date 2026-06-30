# ClipForge

A free, local, open-source pipeline that turns Twitch VODs into vertical short-form clips (and
long-form videos), with an in-app editor and one-click posting to **YouTube Shorts** and **TikTok**.
Built to run entirely on the streamer's own PC — the only paid dependency is an optional Anthropic
API key for AI title/caption writing and clip re-ranking.

## What it does

- **Transcribes** the whole VOD on the local GPU (whisper.cpp / Vulkan on AMD; faster-whisper on NVIDIA).
- **Finds the best moments** with per-second audio scoring, optionally re-ranked by Claude.
- **Edits** clips in a native desktop window (pywebview): trim, captions, chat insets, titlecards,
  text/shape/emoji overlays — all live preview, then a one-click burned-in export.
- **Long-form**: assembles session blocks into 16:9 videos with chapters + captions.
- **Posts / schedules** finished clips to YouTube Shorts + TikTok from inside the app
  (semi-automated, free — see [`clipper/README.md`](clipper/README.md)).

## Layout

| Path | What |
|------|------|
| `clipper/` | the application (Python pipeline + dashboard UI) |
| `clipper/pipeline/` | transcription, detection, render, posting, server |
| `clipper/dashboard.html` | the Studio UI |
| `docs/` | privacy policy + terms pages (for the posting API app registrations) |

## Notes

- Secrets (Anthropic key, YouTube/TikTok OAuth tokens) are stored **locally** in
  `C:\Users\<you>\clipforge\` and are **never** committed (see `.gitignore`).
- Large media (source VODs, rendered clips, scene videos) is intentionally **not** tracked here.

See [`clipper/README.md`](clipper/README.md) for setup and usage.
