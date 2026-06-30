"""whisper.cpp (Vulkan) transcription backend — GPU transcription on AMD GPUs.

faster-whisper (CTranslate2) only supports CUDA/CPU, so the AMD desktop (RX 9070 XT)
can't use it on the GPU. This module drives a prebuilt Vulkan `whisper-cli.exe` as a
subprocess and parses its `-ojf` JSON into the SAME contract the rest of the pipeline
consumes, so it's a drop-in for `transcribe.py`'s two entry points:

    transcribe(cfg, wav, vod, force, tag) -> {language,duration,model,device,words,segments}
    clip_words(cfg, wav, start, end)      -> [{start,end,word}]   (clip-relative)

Selected at runtime by transcribe._use_whispercpp(cfg) when a Vulkan binary + device are
present; the NVIDIA laptop keeps using faster-whisper unchanged.

Word timing: whisper.cpp's DTW token-level timestamps (`--dtw <alias>`) are the accurate
source for karaoke. DTW requires flash-attention OFF (`-nfa`) — flash-attn doesn't expose
the cross-attention weights DTW needs, and silently leaves t_dtw = -1. We pass `-nfa`.

JSON units (verified against whisper.cpp cli.cpp): token `offsets.from/to` are MILLISECONDS
(t0*10); raw `t0/t1/t_dtw` are CENTISECONDS. So seconds = offsets_ms/1000 = t_dtw/100.
t_dtw is written as a float (6 sig-digits) so it loses precision past ~1e6 cs (~2h47m) —
we fall back to the exact integer `offsets` there.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import subprocess
import time

from . import config, util
from .util import log

# Special tokens are written INTO tokens[] and must be filtered client-side.
# whisper_token_eot == 50257 for the 51865-entry multilingual vocab (large-v3 family):
# text tokens are 0..50256; [_SOT_]/[_BEG_]/timestamp tokens are all >= 50257.
_EOT = 50257
_SPECIAL_RE = re.compile(r"^\[_.*_\]$")          # e.g. [_BEG_], [_TT_271]
_T_DTW_FLOAT_LIMIT = 1_000_000                    # centiseconds (~2h47m) past which t_dtw float loses precision

# model id (cfg.wcpp_model / cfg.model) -> (ggml filename, --dtw alias).
# The --dtw alias MUST match the model's decoder or alignment is wrong (and an
# unknown alias hard-fails whisper-cli with exit code 3).
_MODEL_MAP = {
    "large-v3-turbo": ("ggml-large-v3-turbo.bin", "large.v3.turbo"),
    "large-v3":       ("ggml-large-v3.bin",       "large.v3"),
    "large-v2":       ("ggml-large-v2.bin",       "large.v2"),
    "large-v1":       ("ggml-large-v1.bin",       "large.v1"),
    "medium":         ("ggml-medium.bin",         "medium"),
    "medium.en":      ("ggml-medium.en.bin",      "medium.en"),
    "small":          ("ggml-small.bin",          "small"),
    "small.en":       ("ggml-small.en.bin",       "small.en"),
    "tiny.en":        ("ggml-tiny.en.bin",        "tiny.en"),
}


def _wcpp_dir(cfg) -> str:
    return getattr(cfg, "wcpp_dir", None) or config.WCPP_DIR


def cli_exe(cfg) -> str:
    return os.path.join(_wcpp_dir(cfg), "whisper-cli.exe")


def _model_for(cfg):
    """Return (ggml_path, dtw_alias) for the configured whisper.cpp model id."""
    name = getattr(cfg, "wcpp_model", None) or cfg.model
    ggml, alias = _MODEL_MAP.get(name, (f"ggml-{name}.bin", None))
    return os.path.join(config.MODEL_DIR, ggml), alias


def available(cfg) -> bool:
    """True iff a Vulkan whisper.cpp binary, the model file, and a Vulkan device exist."""
    exe = cli_exe(cfg)
    ggml, _alias = _model_for(cfg)
    if not (os.path.exists(exe) and os.path.exists(ggml)):
        return False
    # a Vulkan runtime/device must be present (vulkan-1.dll loader + an enumerable GPU)
    return _vulkan_present()


@functools.lru_cache(maxsize=1)
def _vulkan_present() -> bool:
    dll = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "vulkan-1.dll")
    return os.path.exists(dll)


@functools.lru_cache(maxsize=1)
def _dgpu_index() -> str:
    """Discrete-GPU index for GGML_VK_VISIBLE_DEVICES.

    ggml-vulkan enumerates physical devices in the same order as `vulkaninfo --summary`;
    pick the DISCRETE_GPU block's index so we never run on the integrated Radeon. Defaults
    to '0' (whisper.cpp's own default, which already picks the dGPU here) on any failure.
    """
    try:
        out = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=20).stdout
    except Exception:
        return "0"
    idx, gpu = "0", -1
    for line in out.splitlines():
        s = line.strip()
        m = re.match(r"GPU(\d+)\s*:", s)
        if m:
            gpu = int(m.group(1))
        elif "deviceType" in s and "DISCRETE_GPU" in s and gpu >= 0:
            return str(gpu)
    return idx


def _env(cfg) -> dict:
    """Subprocess env: prepend the binary dir to PATH (DLL resolution) + pin the dGPU."""
    e = dict(os.environ)
    wdir = _wcpp_dir(cfg)
    e["PATH"] = wdir + os.pathsep + e.get("PATH", "")
    e["GGML_VK_VISIBLE_DEVICES"] = _dgpu_index()
    e.setdefault("PYTHONUTF8", "1")
    return e


# --------------------------------------------------------------------------- #
# whisper-cli invocation + -ojf JSON parsing
# --------------------------------------------------------------------------- #
def _run_cli(cfg, wav: str, out_stem: str):
    """Run whisper-cli -ojf on `wav`. Returns (json_path, elapsed_seconds, stderr_text).

    json_path is <out_stem>.json — out_stem must be EXTENSIONLESS (whisper-cli APPENDS '.json',
    it does not replace the extension). Raises RuntimeError on failure (incl. exit 3 = unknown
    --dtw alias, or a hung process past the timeout).
    """
    if not os.path.exists(wav) or os.path.getsize(wav) < 1024:
        raise RuntimeError(f"input wav missing/empty (OneDrive stub?): {wav}")
    exe = cli_exe(cfg)
    ggml, alias = _model_for(cfg)
    argv = [exe, "-m", ggml, "-f", wav,
            "-ojf", "-of", out_stem,
            "-l", cfg.language or "en",
            "-bs", str(cfg.beam_size),
            "-mc", "0",            # max-context 0 == condition_on_previous_text=False (anti-hallucination)
            "-nfa"]                # flash-attn OFF so DTW token timestamps populate
    if alias:
        argv += ["--dtw", alias]
    else:
        log(f"[wcpp] WARNING: no --dtw alias for model '{cfg.model}'; word timing will be coarse (offsets only).")
    # transcription runs far faster than realtime (tens of x); allow up to ~1x the audio
    # duration before declaring a hung Vulkan/driver call (estimate dur from the 16k mono wav).
    try:
        dur_est = os.path.getsize(wav) / (16000 * 2)
    except OSError:
        dur_est = 600.0
    timeout = max(300.0, dur_est)
    t0 = time.time()
    try:
        proc = subprocess.run(argv, cwd=_wcpp_dir(cfg), env=_env(cfg),
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"whisper-cli timed out after {timeout:.0f}s (hung Vulkan/driver call?)")
    dt = time.time() - t0
    if proc.returncode == 3:
        raise RuntimeError(f"whisper-cli exit 3: unknown --dtw preset '{alias}' for model '{cfg.model}'")
    out_json = out_stem + ".json"
    if proc.returncode != 0 or not os.path.exists(out_json):
        tail = (proc.stderr or "").strip()[-600:]
        raise RuntimeError(f"whisper-cli failed (rc={proc.returncode}): {tail}")
    _verify_gpu(proc.stderr)
    return out_json, dt, proc.stderr


def _verify_gpu(stderr: str) -> None:
    """Warn (once) if the run did not actually use the Vulkan GPU backend."""
    s = stderr or ""
    if "using Vulkan" not in s and "Vulkan0 backend" not in s:
        log("[wcpp] WARNING: Vulkan backend not confirmed in whisper-cli log — may have run on CPU.")


def _parse_ojf(out_json: str):
    """Parse whisper-cli -ojf JSON -> (segments, words) in the pipeline contract.

    segments: [{start,end,text}] (seconds).  words: [{start,end,word}] (seconds).
    Word grouping: a token whose text starts with a space begins a new word; tokens
    with no leading space continue it (whisper BPE leading-space convention). word.start
    = t_dtw/100 (when valid) else offsets.from/1000; word.end = last token offsets.to/1000.
    """
    with open(out_json, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    tr = data.get("transcription") or []
    segments, words = [], []
    for seg in tr:
        off = seg.get("offsets") or {}
        segments.append({
            "start": (off.get("from", 0) or 0) / 1000.0,
            "end": (off.get("to", 0) or 0) / 1000.0,
            "text": (seg.get("text") or "").strip(),
        })
        cur = None
        for tk in seg.get("tokens") or []:
            txt = tk.get("text", "")
            if tk.get("id", 0) >= _EOT or _SPECIAL_RE.match(txt.strip()) or not txt.strip():
                continue
            toff = tk.get("offsets") or {}
            tdtw = tk.get("t_dtw", -1)
            if tdtw is not None and tdtw > -1 and abs(tdtw) < _T_DTW_FLOAT_LIMIT:
                start = tdtw / 100.0
            else:
                start = (toff.get("from", 0) or 0) / 1000.0
            end = (toff.get("to", 0) or 0) / 1000.0
            if txt.startswith(" ") or cur is None:
                if cur is not None:
                    words.append(cur)
                cur = {"start": start, "end": end, "word": txt}
            else:
                cur["word"] += txt
                cur["end"] = end
        if cur is not None:
            words.append(cur)
    # clamp end>=start (start uses the DTW clock, end the offsets clock — different sources)
    for w in words:
        if w["end"] < w["start"]:
            w["end"] = w["start"]
    return segments, words


def _finalize_words(words: list, lead: float) -> list:
    """Apply the DTW lead-correction, clamp start>=0, round to 3dp, keep end>=start.
    A uniform negative shift preserves monotonicity; only the clamp at 0 touches the head."""
    for w in words:
        w["start"] = round(max(0.0, w["start"] - lead), 3)
        w["end"] = round(max(w["end"], w["start"]), 3)
    return words


# --------------------------------------------------------------------------- #
# public API — mirrors transcribe.transcribe / transcribe.clip_words
# --------------------------------------------------------------------------- #
def transcribe(cfg, wav: str, vod: str, force: bool = False, tag: str = "") -> dict:
    """Full-VOD transcript via whisper-cli (one-shot). Cached like the faster-whisper path,
    but with an engine discriminator so wcpp and faster-whisper caches never collide."""
    from . import transcribe as _t  # for transcript_path (engine-aware)
    out = _t.transcript_path(cfg, vod, tag)
    if os.path.exists(out) and not force:
        log(f"[wcpp] using cached transcript: {out}")
        return util.read_json(out)

    util.ensure_dirs(config.WORK_DIR)
    # raw -ojf dump goes to a DISTINCT stem so it never collides with the final transcript (`out`)
    raw_stem = os.path.join(config.WORK_DIR, os.path.splitext(os.path.basename(out))[0] + ".wcppraw")
    log(f"[wcpp] transcribing {wav} on Vulkan ({os.path.basename(_model_for(cfg)[0])}) ...")
    out_json, dt, _err = _run_cli(cfg, wav, raw_stem)
    segments, words = _parse_ojf(out_json)
    # NO lead-correction here: the full-VOD transcript must keep TRUE audio timing (detection +
    # clip-boundary snapping rely on it; the karaoke lead shifts starts earlier and makes words
    # overlap). The lead is applied only in clip_words() where karaoke is actually rendered.
    _finalize_words(words, 0.0)
    try:
        duration = util.probe_duration(vod)
    except Exception:
        duration = (segments[-1]["end"] if segments else 0.0)
    rtf = (duration / dt) if dt > 0 else 0.0
    log(f"[wcpp] done: {len(words)} words, {len(segments)} segments in {dt:.0f}s "
        f"({rtf:.1f}x realtime) -> {out}")
    data = {
        "language": cfg.language or "en",
        "duration": duration,
        "model": cfg.model,          # keep the faster-whisper-style id (cache/project.json consistency)
        "device": "vulkan",
        "words": words,
        "segments": segments,
    }
    util.write_json(out, data)
    try:
        os.remove(out_json)          # the raw -ojf dump; we keep our normalized transcript
    except OSError:
        pass
    return data


def clip_words(cfg, wav: str, start: float, end: float) -> list:
    """Word-level timestamps for ONE clip range (clip-relative seconds). Re-encodes the slice
    to 16k mono s16le (whisper-cli is strict about WAV format) and caches per (model,engine,range).

    Raises on empty-but-expected output (segments present, zero words) so the render fallback
    (render.py -> _words_in(full transcript)) engages; returns [] only for true silence."""
    from . import transcribe as _t
    end = max(start + 0.5, end)
    key = _t._clipwords_key(cfg, start, end)      # engine-discriminated (see transcribe.py)
    cdir = os.path.join(config.WORK_DIR, "clipwords")
    cpath = os.path.join(cdir, key + ".json")
    if os.path.exists(cpath):
        return util.read_json(cpath)
    util.ensure_dirs(cdir)
    slice_wav = os.path.join(cdir, key + ".wav")
    # re-encode (NOT -c copy): whisper-cli requires 16kHz mono s16le and rejects anything else
    subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{start:.3f}", "-i", wav, "-t", f"{end - start:.3f}",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", slice_wav],
                   check=True)
    try:
        # raw dump stem MUST differ from cpath (cdir/<key>.json) or the finally-cleanup deletes the cache
        out_json, _dt, _err = _run_cli(cfg, slice_wav, os.path.join(cdir, key + ".raw"))
        segments, words = _parse_ojf(out_json)
        if segments and not words:
            raise RuntimeError("whisper.cpp produced segments but no words for this slice")
        _finalize_words(words, getattr(cfg, "wcpp_word_lead_s", 0.0))
        util.write_json(cpath, words)
        return words
    finally:
        for p in (slice_wav, os.path.join(cdir, key + ".raw.json")):
            try:
                os.remove(p)
            except OSError:
                pass
