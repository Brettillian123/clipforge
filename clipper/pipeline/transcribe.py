"""Transcription via faster-whisper -> word-level timestamps, cached to JSON.

The transcript JSON is the backbone of the whole pipeline: detection scores the
transcript grid, and the captions module turns word timestamps into ASS karaoke.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time

from . import config, util
from .util import log

_MODEL = None


# --------------------------------------------------------------------------- #
# backend selection (whisper.cpp/Vulkan on AMD vs faster-whisper/CUDA-CPU on the laptop)
# --------------------------------------------------------------------------- #
def _use_whispercpp(cfg) -> bool:
    """Route to the whisper.cpp (Vulkan) backend? Honors cfg.transcribe_backend
    (auto|whispercpp|faster_whisper); 'auto' uses it when its binary+model+device exist."""
    pref = getattr(cfg, "transcribe_backend", "auto")
    if pref == "faster_whisper":
        return False
    if pref == "whispercpp":
        return True
    try:
        from . import transcribe_whispercpp as _w
        return _w.available(cfg)
    except Exception:
        return False


def _engine(cfg) -> str:
    """Cache discriminator: 'wcpp' vs 'fw' so whisper.cpp and faster-whisper
    transcripts/clip-words never collide (different engines, same cfg.model id)."""
    return "wcpp" if _use_whispercpp(cfg) else "fw"


def _model_tag(cfg) -> str:
    """The model id each engine ACTUALLY loads (cfg.wcpp_model for whisper.cpp), so the
    cache busts when the real model changes — cfg.model alone wouldn't (it's the fw id)."""
    if _use_whispercpp(cfg):
        return getattr(cfg, "wcpp_model", None) or cfg.model
    return cfg.model


def _clipwords_key(cfg, start: float, end: float) -> str:
    return hashlib.md5(f"{_model_tag(cfg)}|{_engine(cfg)}|{start:.2f}|{end:.2f}".encode()).hexdigest()[:16]


def _get_model(cfg: config.Config):
    """Lazy, reused WhisperModel (so per-clip caption transcription doesn't reload it)."""
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        device, compute = _resolve_device(cfg)
        log(f"[transcribe] loading {cfg.model} on {device} ({compute}) for clip captions ...")
        _MODEL = WhisperModel(cfg.model, device=device, compute_type=compute, download_root=config.MODEL_DIR)
    return _MODEL


def wav_path(vod: str) -> str:
    stem = os.path.splitext(os.path.basename(vod))[0]
    p = os.path.join(config.WORK_DIR, f"{stem}_16k.wav")
    if not os.path.exists(p):
        alt = os.path.join(config.WORK_DIR, "stream1_16k.wav")
        if os.path.exists(alt):
            return alt
    return p


def clip_words(cfg: config.Config, wav: str, start: float, end: float) -> list:
    """Word-level timestamps for ONE clip range, by transcribing just that audio slice
    (far more accurate than slicing the full-VOD transcript, whose word alignment drifts).
    Times are relative to the clip start. Cached on disk."""
    if _use_whispercpp(cfg):
        from . import transcribe_whispercpp as _w
        return _w.clip_words(cfg, wav, start, end)
    end = max(start + 0.5, end)
    key = _clipwords_key(cfg, start, end)
    cpath = os.path.join(config.WORK_DIR, "clipwords", key + ".json")
    if os.path.exists(cpath):
        return util.read_json(cpath)
    util.ensure_dirs(os.path.dirname(cpath))
    sl = os.path.join(config.WORK_DIR, "clipwords", key + ".wav")
    subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{start:.3f}", "-i", wav, "-t", f"{end - start:.3f}",
                    "-c", "copy", sl], check=True)
    segs, _ = _get_model(cfg).transcribe(
        sl, language=cfg.language, beam_size=cfg.beam_size, word_timestamps=True,
        vad_filter=False, condition_on_previous_text=False)
    words = []
    for s in segs:
        for w in (s.words or []):
            words.append({"start": round(max(0.0, w.start), 3), "end": round(w.end, 3), "word": w.word})
    util.write_json(cpath, words)
    try:
        os.remove(sl)
    except OSError:
        pass
    return words


def _resolve_device(cfg: config.Config):
    """Return (device, compute_type), preferring CUDA, falling back to CPU."""
    if cfg.device == "cpu":
        return "cpu", cfg.compute_type_cpu
    # try cuda
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            util.add_cuda_dll_dirs()
            return "cuda", cfg.compute_type_gpu
    except Exception as exc:  # pragma: no cover
        log(f"[transcribe] CUDA probe failed ({exc}); using CPU.")
    if cfg.device == "cuda":
        log("[transcribe] WARNING: --device cuda requested but no CUDA device; using CPU.")
    return "cpu", cfg.compute_type_cpu


def transcript_path(cfg: config.Config, vod: str, tag: str = "") -> str:
    stem = os.path.splitext(os.path.basename(vod))[0]
    suffix = f".{tag}" if tag else ""
    # actual-model + engine in the name so transcripts never collide across models/engines
    return os.path.join(config.WORK_DIR, f"{stem}.transcript.{_model_tag(cfg)}.{_engine(cfg)}{suffix}.json")


def transcribe(cfg: config.Config, wav: str, vod: str, force: bool = False, tag: str = "") -> dict:
    """Transcribe `wav`; cache keyed by VOD stem + model. Returns the transcript dict.

    Shape: {"language", "duration", "model", "device",
            "words":[{"start","end","word"}...],
            "segments":[{"start","end","text"}...]}
    """
    if _use_whispercpp(cfg):
        from . import transcribe_whispercpp as _w
        return _w.transcribe(cfg, wav, vod, force=force, tag=tag)
    out = transcript_path(cfg, vod, tag)
    if os.path.exists(out) and not force:
        log(f"[transcribe] using cached transcript: {out}")
        return util.read_json(out)

    from faster_whisper import WhisperModel

    device, compute = _resolve_device(cfg)
    util.ensure_dirs(config.MODEL_DIR, config.WORK_DIR)
    log(f"[transcribe] loading {cfg.model} on {device} ({compute}) ...")
    t0 = time.time()
    model = WhisperModel(
        cfg.model, device=device, compute_type=compute, download_root=config.MODEL_DIR
    )
    log(f"[transcribe] model ready in {time.time()-t0:.0f}s. Transcribing {wav} ...")

    t0 = time.time()
    segments, info = model.transcribe(
        wav,
        language=cfg.language,
        beam_size=cfg.beam_size,
        word_timestamps=True,
        vad_filter=cfg.vad_filter,
        condition_on_previous_text=cfg.condition_on_previous_text,
    )

    words, segs = [], []
    last_log = time.time()
    for seg in segments:  # generator -> work happens here
        segs.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
        for w in (seg.words or []):
            words.append({"start": w.start, "end": w.end, "word": w.word})
        if time.time() - last_log > 20:
            log(f"[transcribe] ... {util.hhmmss(seg.end)} processed "
                f"({len(words)} words, {time.time()-t0:.0f}s elapsed)")
            last_log = time.time()

    data = {
        "language": info.language,
        "duration": info.duration,
        "model": cfg.model,
        "device": device,
        "words": words,
        "segments": segs,
    }
    util.write_json(out, data)
    log(f"[transcribe] done: {len(words)} words, {len(segs)} segments in "
        f"{time.time()-t0:.0f}s -> {out}")
    return data


# Allow running this stage standalone: python -m pipeline.transcribe <wav> <vod>
if __name__ == "__main__":
    import sys
    cfg = config.load_config()
    wav_arg = sys.argv[1] if len(sys.argv) > 1 else os.path.join(config.WORK_DIR, "stream1_16k.wav")
    vod_arg = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.expanduser("~"), "Videos", "Stream1.mp4")
    transcribe(cfg, wav_arg, vod_arg)
