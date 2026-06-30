# ClipForge AMD-GPU build — report & how-to

This build makes ClipForge transcribe on the **AMD RX 9070 XT** desktop (it previously
could only use NVIDIA/CPU), then audits and optimizes the rest of the pipeline for this
machine. Everything below was measured on your hardware, not estimated.

---

## 1. How to use it (nothing extra to do day-to-day)

**Just double-click `ClipForge` on your Desktop** — no commands. Your browser opens the ClipForge
**Home** screen:
- **🎬 Make clips from a video** — lists the videos in your `Videos` folder; click **Make clips** and
  it transcribes on the GPU, finds the best moments, renders the vertical clips, and drops you into
  the editor (live progress, a few minutes for a full stream).
- **📁 Previous clip batches** — **Open ▸** to jump back into a batch you already made.
- The **🏠 Home** button (top-right of the editor) returns to the picker.

Close the console window (or `Ctrl+C`) to stop. New clips land in a `<video name> - clips` folder next
to the video. The CLI still works if you prefer it:

```powershell
$env:PYTHONPATH="C:\Users\Brett\OneDrive\Documents\StreamingProject\clipper"
$py = "C:\Users\Brett\clipforge\.venv\Scripts\python.exe"
& $py "$env:PYTHONPATH\clip.py" "C:\Users\Brett\Videos\<your-vod>.mp4"   # batch from the CLI
& $py "$env:PYTHONPATH\dashboard.py"                                     # open the Studio (Home picker)
```

ClipForge **auto-detects** this AMD desktop and routes transcription through the whisper.cpp (Vulkan)
backend.

- The **NVIDIA laptop still works exactly as before** — it auto-selects faster-whisper.
  The selector is `transcribe_backend` in `pipeline/config.py` (`auto` | `whispercpp` |
  `faster_whisper`) if you ever want to force one.
- New keyboard shortcuts in the Studio: **`,` / `.`** step one frame · **`[` / `]`** prev/next
  clip · **`Esc`** deselect (press **`?`** for the full list).

### Where things live (all local, kept out of OneDrive)
| What | Path |
|---|---|
| Python venv | `C:\Users\Brett\clipforge\.venv` |
| whisper.cpp Vulkan binary | `C:\Users\Brett\clipforge\whispercpp\` (`whisper-cli.exe` + DLLs) |
| Models | `C:\Users\Brett\clipforge\models\` (`ggml-large-v3-turbo.bin` default) |
| ffmpeg | winget Gyan.FFmpeg 8.1.1 (AMF + libass) |
| Work cache | `C:\Users\Brett\clipforge\work\` |

---

## 2. Performance (measured on your RX 9070 XT)

| Task | Result |
|---|---|
| Transcribe (large-v3-turbo + DTW) | **45–68× realtime** |
| 151-min VOD → words + clips selected | **~2.4 min total** (132s transcribe + detect) |
| 6-min segment → 2 captioned clips | ~28s end-to-end |
| Render encode | **AMD `h264_amf` hardware** (was CPU libx264) |
| Word-timing accuracy vs faster-whisper ground truth | **median 0.22s** error after lead-correction |

---

## 3. The transcription backend — what & why

**Decision tree, with the evidence behind each call:**

- **Engine: whisper.cpp (Vulkan), driven via `whisper-cli.exe` subprocess.** faster-whisper
  (CTranslate2) is CUDA/CPU-only and can't touch the AMD GPU. ONNX-DirectML has no turnkey
  word timestamps; ROCm PyTorch is Python-3.12-only (we're on 3.13). whisper.cpp Vulkan is the
  only no-compiler path to the GPU **with** word timestamps.
- **Binary: community prebuilt `jerryshell/whisper.cpp-windows-vulkan` v1.8.5** (no official
  prebuilt Vulkan exists). It's an unsigned third-party build of the MIT project — verified by
  **SHA256** (`a5d408c7…`, matches the published hash), runs **offline only**. VirusTotal:
  `https://www.virustotal.com/gui/file/a5d408c72e460433b39875f74a0b6e27e60a3724301d478fe9873db7ff4098e0`.
- **Model: `large-v3-turbo` (f16, no quantization)** + `--dtw large.v3.turbo`. On 16 GB VRAM
  there's no reason to quantize. Turbo runs ~2× faster than large-v3 and — after the lead-correction
  below — **matches large-v3's word-timing accuracy**, so it's the best "accuracy *and* speed" pick.
  `large-v3` is downloaded too as a max-accuracy fallback (set `wcpp_model="large-v3"`).
- **Flash-attention OFF (`-nfa`) — critical.** Flash-attn silently disables whisper.cpp's DTW
  (t_dtw stays empty), which collapses word timing to a coarse clock that put "What" at 5.5s when
  the truth was 12.3s — karaoke would desync by *seconds*. With `-nfa`, DTW populates 89% of tokens.
- **Lead-correction (`wcpp_word_lead_s = 0.40`).** whisper.cpp's DTW onsets run a consistent ~0.4s
  late vs faster-whisper; shifting word starts earlier by 0.40s drops median error 0.32s → 0.22s.
- **`-mc 0` (no context).** Mirrors faster-whisper's `condition_on_previous_text=False` to avoid
  hallucination loops on long noisy VOD audio.
- **CLI, not server mode.** Measured cold-start is only ~1.1s (not the 8–20s assumed), and parsing
  the CLI's `-ojf` tokens ourselves gives **cleaner karaoke words** than the server's verbose_json
  (which splits punctuation into separate words and emits end<start). Per-clip results are cached, so
  the dashboard rarely re-runs it. (Optional warm-server mode noted as a future dashboard tweak.)

**Word-timestamp parsing** (in `transcribe_whispercpp.py`): one `-ojf` pass → segments from
`offsets` (ms ÷1000) and words rebuilt from `tokens` by the leading-space rule; word start =
`t_dtw/100` (centiseconds) with a fallback to exact `offsets` past ~2h47m (where t_dtw's float
form loses precision); special tokens (`id ≥ 50257`, `[_BEG_]`/`[_TT_]`) filtered.

---

## 4. Everything that changed (file by file)

### New
- **`pipeline/transcribe_whispercpp.py`** — the whisper.cpp/Vulkan backend (`transcribe()` +
  `clip_words()`), device pinning, `-ojf` parsing, lead-correction, GPU-use verification, timeout.
- **`AMD_BACKEND_PLAN.md`**, **`AMD_BUILD_REPORT.md`** (this file) — plan + report.

### Modified — transcription
- **`pipeline/transcribe.py`** — runtime backend selector (`_use_whispercpp`), **engine- and
  actual-model-discriminated cache keys** (`_engine`/`_model_tag`) so whisper.cpp and faster-whisper
  transcripts never collide and switching models busts the cache; delegates `transcribe()`/`clip_words()`.
- **`pipeline/config.py`** — `WCPP_DIR`; `transcribe_backend`, `wcpp_model`, `wcpp_word_lead_s`.
- **`pipeline/util.py`** — `ffmpeg()`/`ffprobe()` now glob the winget Gyan path (survives version
  upgrades; doesn't depend on a stale in-process PATH).
- **`tools/diag_timing.py`** — guarded its faster-whisper import (it's an NVIDIA-only diagnostic).

### Modified — clip framing (tuned to your real OBS scene)
- **`pipeline/camdetect.py` + `config.py`** — your webcam is framed **wide** (bed/chair/wall), so the
  gold-border autodetect was filling the top panel with empty room. Added a **face-focus crop**
  (`cam_face_*`) that tightens the detected cam onto **head + shoulders** — you're now the focus of the
  clip. Verified across multiple points in your VOD (holds when you lean/move). Retune the four
  `cam_face_*` fractions in `config.py` if you reframe your webcam, or override per-clip with the
  Studio's manual cam mode. Captions (legible white→gold karaoke with a dark plate) and the gameplay
  crop were reviewed against your real footage and read well.
- **Watermark moved to the upper-RIGHT** (`wm_align="right"`, `wm_y=250`) so TikTok's top search bar /
  tabs no longer cover it and it stays above the right-side action rail. Tunable via `wm_align`/`wm_x`/
  `wm_y` in `config.py` (right-align uses ffmpeg's `W-w-margin` overlay expression in `render.py`).

### Modified — renderer & pipeline (from the ClipForge audit)
- **`pipeline/render.py`** — **AMD `h264_amf` hardware encoder** (NVENC → AMF → libx264 fallback,
  one codepath for both machines); **bt709 / limited-range color tags + forced CFR** on every encode
  (prevents washed-out/shifted colors and caption drift).
- **`pipeline/longform.py`** — same AMF/encoder fix for long-form; **`.srt` now breaks on speech
  pauses** (not just punctuation) so the AMD path's lightly-punctuated transcripts stay phrase-shaped.
- **`pipeline/project.py`** — `project.json` now keeps a **`.bak` and auto-recovers** from it if the
  main file is corrupted (e.g. a OneDrive sync conflict), so a bad file never strands your clip edits.
- **`dashboard.html`** — frame-step / clip-nav / Esc-deselect shortcuts + help text; **corrected
  safe-zone rectangle** to the conservative TikTok/Reels/Shorts union (the old one gave false confidence).
- **`serve_preview.py`** + **`.claude/launch.json`** — preview server path is argv-driven instead of
  hardcoded to a nonexistent folder.

---

## 5. ClipForge audit — fixed vs. recommended

A 6-agent audit (UI/UX + bug-class research + full code review) produced 66 findings. **Fixed this
pass** (verified): AMD AMF encode (flagged *critical* by 3 lenses), color/CFR tagging, longform SRT
pause-fallback, cache-key correctness, project.json recovery, safe-zone rect, frame-step/clip-nav/Esc
shortcuts, ffmpeg discovery, whisper-cli timeout, serve_preview decoupling.

**Recommended next** (real, but larger or lower-risk — left for a focused pass with browser testing):
- Full **JKL transport** + **set-in/out (I/O)** shortcuts; **per-word caption timing nudge**;
  element edge/peer/safe-zone **snapping**.
- **Autosave**: idempotency token + rollback on failed save; fix the selectClip flush race.
- **AI path** (only matters once you add an API key): robust JSON extraction (brace-slice breaks on
  braces inside strings), escape transcript text in prompts (prompt-injection), re-validate AI trim
  deltas against pauses/duration.
- Chat-image file-serving path containment (localhost-only, so low risk); render cancel-polling on a
  stalled ffmpeg; undo history scoped per-clip.

---

## 6. Notes & housekeeping
- **Free/OSS only**, as required — whisper.cpp (MIT), ggml models (MIT), ffmpeg, faster-whisper (CPU
  safety net). No new paid services.
- faster-whisper is installed **CPU-only** here as the A/B ground-truth + a kill-switch
  (`transcribe_backend="faster_whisper"`).
- Disk: models ~4.4 GB (turbo 1.5 GB + large-v3 2.9 GB), binary 18 MB. Safe to delete if needed:
  `clipforge\work\*_16k.wav` (regenerated on next run), `Videos\testclip6min.mp4` + `Videos\clips\`
  (the test artifacts), and `ggml-large-v3.bin` (only the max-accuracy fallback).
- Tune knobs in `config.py`: `wcpp_model`, `wcpp_word_lead_s` (raise if karaoke still reads late,
  lower if early), AMF quality via `_encode_args` qp in `render.py`.
