# ClipForge AMD-GPU transcription backend — build plan (v2, post-review)

Goal: GPU Whisper transcription on the **AMD RX 9070 XT** (this desktop), behind ClipForge's
existing `transcribe.py` contract, faster-whisper (CUDA/CPU) preserved for the NVIDIA laptop.
Free/OSS only. v2 incorporates the 5-lens adversarial review (3 blockers + factual corrections).

## 0. Verified system facts (this desktop)
- GPU: RX 9070 XT (RDNA4, 16 GB) + integrated Radeon iGPU. Vulkan device live (api 1.4.349,
  driver 26.6.2). Vulkan **runtime** present; **SDK absent**; **no C/C++ compiler**. git+winget+Py3.13.
- ffmpeg **installed** (Gyan 8.1.1 full), `h264_amf`+libass verified; venv built (numpy/pillow/anthropic).
- Models **downloaded & magic-verified** to `C:\Users\Brett\clipforge\models\`:
  `ggml-large-v3-turbo.bin` (1.51 GB) + `ggml-tiny.en.bin` (74 MB, smoke model).
- Test material: `C:\Users\Brett\Videos\2026-06-21 23-35-05.mp4` (~40 MB short) + two multi-hour VODs.

## 1. Backend decision (KEEP — confirmed by all 5 lenses)
whisper.cpp (Vulkan) driven from Python via subprocess — only no-compiler path to the AMD GPU with
word timestamps. **Two modes:** `whisper-server.exe` kept warm for the per-clip hot path
`clip_words()` (blocker #2); one-shot `whisper-cli.exe` for the full-VOD `transcribe()` (one cold
start is fine). `GGML_VK_VISIBLE_DEVICES` is the only device-pin lever (no `--device` flag exists).

## 2. Binary sourcing — CORRECTED ladder (needs user approval; unsigned binary)
The only ready source of the `.exe` tools is the **unofficial jerryshell prebuilt**.
- **Tier A (preferred): jerryshell, as-shipped.**
  `https://github.com/jerryshell/whisper.cpp-windows-vulkan-bin/releases/download/v1.0.0/whisper.cpp-windows-vulkan.zip`
  — **whisper.cpp v1.8.5** (commit f24588a, 2026-05-29; current, already has the #3455 fix, supports
  `--dtw large.v3.turbo`). Ships `whisper-cli.exe` + `whisper-server.exe` + matched ggml-vulkan DLLs.
  **SHA256 zip** = `a5d408c72e460433b39875f74a0b6e27e60a3724301d478fe9873db7ff4098e0` (18,340,920 bytes).
  Controls: verify SHA256 on download → **VirusTotal the two .exes** → run **offline only** →
  validate output numerically (§6). Wrapper repo has no LICENSE; redistribution rests on upstream
  whisper.cpp **MIT** + our hash/scan.
- **Tier B (alternate prebuilt, if A fails on RDNA4):** another *matched whole-package* build —
  `DomoticX/whisper.cpp-windows-vulkan` or StarWhisper. **Never a DLL graft** (the v1.9.1 NuGet
  renames DLLs `*-whisper.dll` for P/Invoke → jerryshell's exe import table can't resolve them).
- **Tier C (last resort): build from source.** `winget install Kitware.CMake`,
  `Microsoft.VisualStudio.2022.BuildTools` + "Desktop development with C++" workload (~2-7 GB),
  `KhronosGroup.VulkanSDK` (~300 MB); `cmake -B build -DGGML_VULKAN=1 && cmake --build build --config Release`.
- **Tier C′ (alt): CI build** in a GitHub fork (no local toolchain; uses the user's GitHub, ~15 min).

## 3. Model (KEEP)
`ggml-large-v3-turbo.bin` (f16, downloaded). Repo **`ggerganov/whisper.cpp`** (NOT ggml-org — the
research URL was wrong; corrected & verified). 16 GB VRAM → no quantization. DTW alias **must** match:
`--dtw large.v3.turbo`. **SHA256** = `1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69`.
Fallback for loose timing: `ggml-large-v3.bin` + `--dtw large.v3`.

## 4. CLI/server invocation + JSON → contract
Full-VOD (CLI): `whisper-cli.exe -m <ggml> -f <wav> --dtw large.v3.turbo -ojf -of <STEM_no_ext> -l en -bs <beam> -nc [-t N]`
- `-ojf` alone enables token_timestamps (cli.cpp: `token_timestamps = output_jsn_full || ...`) — **do
  NOT add `-ml 1`/`-oj`/`-otxt`** (`-ojf` implies `-oj`; `-ml 1` would destroy segmentation).
- `-nc`/`--no-context` = parity with faster-whisper `condition_on_previous_text=False` (stops
  hallucination loops on multi-hour VODs). REQUIRED.
- `-of` **appends** `.json` (no extension replacement) → pass an **extensionless stem** (derive from
  cache-key stem, not the wav name) and read `<stem>.json` via `util.read_json`.
- **segments** = `[{start: seg.offsets.from/1000, end: seg.offsets.to/1000, text: seg.text.strip()}]`.
- **words** from `seg.tokens`: skip `id >= 50257` (whisper_token_eot, 51865-vocab) AND text matching
  `^\[_.*_\]$`; new word when `token.text` starts with a space (strip it); `word.start` =
  `t_dtw/100` if `t_dtw > -1` **and** `abs(t_dtw) < 1e6` else `offsets.from/1000`; `word.end` =
  last token `offsets.to/1000`; clamp `end >= start`, round 3 dp, clamp first `start >= 0`.
- **UNITS (locked):** `offsets` = ms (÷1000); raw `t0/t1/t_dtw` = centiseconds (÷100). Code comment
  must say so. `t_dtw` is written via `value_f` (float, 6 sig-digits) → **>= 1e6 cs (~2h47m) loses
  precision / goes sci-notation** → the `<1e6` guard above falls back to exact `offsets`. Word
  start uses the DTW clock, end uses the offsets clock — different sources; the `end>=start` clamp +
  captions.py's next-line clamp handle it.
- Raw output has **no punctuation** → captions fall back clause→pause chunking (matches today's
  `vad_filter=False`). Accepted detection delta: `detect._boundaries` loses `.?!` snap points and the
  `?` question signal goes dead vs the laptop (silence-map boundaries remain). Documented, not fixed
  (optional `--ai` punctuation pass could remediate later — adds API cost).
- **Returncode:** `==3` → unknown DTW alias (refuse/raise, don't silently degrade). Non-zero or
  missing/empty `<stem>.json` → raise. Valid JSON with empty `transcription[]` on silence (exit 0) =
  zero words (legitimate).

Per-clip slice (server): POST the sliced WAV to `http://127.0.0.1:<port>/inference`
(`response_format=verbose_json, token_timestamps=true, no_timestamps=false, language=en,
temperature=0`); server returns `segments[]` + `words[]` already in seconds → same mapping/guards.

## 5. Integration (code)
- **New module** `clipper/pipeline/transcribe_whispercpp.py`:
  - `transcribe(cfg, wav, vod, force, tag)` → CLI one-shot; returns the full 6-key dict:
    `language` (from `-l` or 'en'), `duration` (`util.probe_duration(vod)` or WAV length),
    `model = cfg.model` (the faster-whisper id, **NOT** the ggml filename — keeps cache/project.json
    consistent), `device='vulkan'`, `words` + `segments` **always present** (even empty).
  - `clip_words(cfg, wav, start, end)` → **warm server**; reuse the existing `clipwords/<key>.json`
    cache + the existing slice, but **re-encode** the slice `-ar 16000 -ac 1 -c:a pcm_s16le -f wav`
    (whisper-cli strictly requires 16k mono s16le; `-c copy` worked only by luck). Round 3 dp, clamp
    `start>=0`. **RAISE** (not return `[]`) when a slice with segments yields zero words → engages
    `render.py:357-361` fallback to `_words_in(transcript)`; true silence (no segments) returns `[]`.
  - **Warm server:** module-global handle mirroring `_get_model()`; lazy `Popen([server_exe,'-m',
    model,'--host','127.0.0.1','--port',<free>,'-l','en'])` with `cwd=wcpp_dir`,
    `env={**os.environ,'PATH':wcpp_dir+os.pathsep+PATH,'GGML_VK_VISIBLE_DEVICES':str(dgpu_idx)}`;
    health-check `GET /`; `atexit`+`try/finally` teardown; 127.0.0.1 only (no VRAM leak).
- **Selector** in `transcribe.py`: `_use_whispercpp(cfg)` honoring `cfg.transcribe_backend`
  (`auto|whispercpp|faster_whisper`, kill-switch). At the **top of `transcribe()` and `clip_words()`**
  delegate when true (verified sufficient for all call sites: clip.py:83, longform.py:53,
  render.py:358, server.py:524/655); else current faster-whisper code unchanged.
- **Engine-discriminated caches (blocker #3):** add `_engine(cfg)` ('wcpp'|'fw') to BOTH keys —
  `clip_words` key `md5(f"{cfg.model}|{engine}|{start:.2f}|{end:.2f}")` and
  `transcript_path` `<stem>.transcript.<model>.<engine>.json`. Lets both coexist for A/B.
- **Device pin (blocker #1):** parse `vulkaninfo --summary` once → discrete index (deviceType
  DISCRETE_GPU / deviceName contains "9070") → `GGML_VK_VISIBLE_DEVICES=<idx>`; cache per session.
  **Prove GPU used by measured RTF** (turbo many×realtime; CPU ~0.1×) + stderr has no "no GPU".
- **Config:** add `transcribe_backend`, `wcpp_dir`, `wcpp_model` (→ggml file), `model→dtw-alias` map.
  Leave `cfg.model` untouched.
- **Subprocess hygiene:** argv as **list** (never `shell=True`); read JSON from file; stderr capture
  `text=True, encoding='utf-8', errors='replace'`; set `PYTHONUTF8=1` for the venv.
- **util.ffmpeg()/ffprobe():** make them **glob now** — `shutil.which` → newest
  `...\Gyan.FFmpeg_*\ffmpeg-*-full_build\bin\ffmpeg.exe` → literal 8.1.1 fallback (survives upgrades).
- **Guard** `tools/diag_timing.py` top-level `from faster_whisper import` (laptop-only diagnostic).

## 6. Desktop bootstrap + test (ordered)
1. venv: `py -3.13` explicitly (NOT 3.14-first). Deps: numpy/pillow/anthropic ✅ done. faster-whisper
   = **open user decision** (CPU-only safety net + A/B baseline).
2. Unzip verified binary → `wcpp_dir`; download model (✅); copy Poppins fonts; fetch emoji; render
   watermark; set `PYTHONPATH`.
3. **Device + GPU-use smoke:** `vulkaninfo` index; run tiny.en on a few-sec WAV; assert RTF ≫ 1
   (GPU) and stderr clean. Confirm `--dtw large.v3.turbo` accepted (no exit 3).
4. **Numeric acceptance (no faster-whisper needed):** word starts monotonic, all in `[0, clip_dur]`,
   a unit-slip assert (`max start < duration`), median `|word.start − audio energy-onset|` under a
   stated threshold. Plus a human eyeball of karaoke sync on a rendered short clip. (If faster-whisper
   installed, also A/B against CPU int8 large-v3.)
5. **DONE for this task** = `clip.py --limit-secs … "<40MB test mp4>"` produces **captioned vertical
   clips** end-to-end on the AMD GPU. Then run a **full multi-hour VOD** through `transcribe()` to
   confirm throughput + stable VRAM + clean server shutdown.

## 7. Deferred (separate, optimize phase)
- `render.py`: add `h264_amf` branch (`-c:v h264_amf -usage transcoding -quality quality -rc cqp
  -qp_i 22 -qp_p 24 -qp_b 26`) alongside the NVENC probe, libx264 last → AMD GPU render not CPU x264.
  (`h264_amf` verified working here.) Independent of transcription.

## 8. Open decisions for the USER
1. **Binary source** (security): unsigned community prebuilt (Tier A, verified) vs build-from-source
   (Tier C toolchain) vs CI build (Tier C′). + fallback preference if Tier A fails on RDNA4.
2. **Install faster-whisper CPU** on the desktop? (A/B ground truth + kill-switch/CPU safety net;
   ~couple-min one-off; free) — recommend yes.
3. (Decided by us, not blocking) server warm-mode for clip_words = yes; punctuation detection delta =
   accept as documented for now.
