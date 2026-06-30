"""Diagnose word-timestamp accuracy: transcribe a clip slice with vs without VAD."""
import os
import subprocess
import sys

sys.path.insert(0, r"C:\Users\Brett\OneDrive\Documents\StreamingProject\clipper")
from pipeline import config, util
from pipeline.transcribe import _resolve_device

cfg = config.load_config()
sl = r"C:\Users\Brett\clipforge\work\diag_slice.wav"
subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-ss", "11171", "-t", "40",
                "-i", r"C:\Users\Brett\clipforge\work\stream1_16k.wav", "-c", "copy", sl], check=True)

util.add_cuda_dll_dirs()
try:
    from faster_whisper import WhisperModel
except ImportError:
    raise SystemExit("diag_timing.py is a faster-whisper-only diagnostic; the AMD desktop's "
                     "pipeline uses whisper.cpp. Install faster-whisper to run this, or use the "
                     "whisper.cpp A/B in work/fw_ab.py instead.")
dev, ct = _resolve_device(cfg)
m = WhisperModel(cfg.model, device=dev, compute_type=ct, download_root=config.MODEL_DIR)
KEY = {"what", "was", "that", "where", "are", "you", "wait", "there", "no", "way", "that's"}
for label, vad in (("VAD", True), ("noVAD", False)):
    segs, _ = m.transcribe(sl, language="en", beam_size=5, word_timestamps=True,
                           vad_filter=vad, condition_on_previous_text=False)
    print(label, "->")
    for s in segs:
        for w in (s.words or []):
            if w.word.strip().lower().strip(".,?!") in KEY:
                print(f"   {w.start:6.2f}-{w.end:6.2f} {w.word.strip()}")
