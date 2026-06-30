"""Audio feature extraction with pure numpy on the 16 kHz mono WAV.

No librosa / numba / torch. We read the same PCM the transcriber used and derive:
  - a fine-grained loudness (dB) envelope for silence / pause detection
  - per-second loudness + high-frequency ratio (a cheap laughter/excitement proxy)
"""
from __future__ import annotations

import wave
from dataclasses import dataclass

import numpy as np

EPS = 1e-9


def load_pcm(wav_path: str) -> tuple[int, np.ndarray]:
    """Return (sample_rate, mono float32 samples in [-1, 1])."""
    with wave.open(wav_path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"expected 16-bit PCM, got sample width {sw}")
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return sr, x


def frame_rms_db(x: np.ndarray, sr: int, frame_ms: int, hop_ms: int) -> tuple[np.ndarray, np.ndarray]:
    """Fast RMS-in-dB per frame via cumulative sum of squares.

    Returns (frame_center_times_s, rms_db). dB is relative to full scale (<=0).
    """
    frame = max(1, int(sr * frame_ms / 1000))
    hop = max(1, int(sr * hop_ms / 1000))
    if len(x) < frame:
        return np.array([0.0]), np.array([-120.0])
    csum = np.concatenate(([0.0], np.cumsum(x.astype(np.float64) ** 2)))
    starts = np.arange(0, len(x) - frame + 1, hop)
    sumsq = csum[starts + frame] - csum[starts]
    rms = np.sqrt(sumsq / frame)
    db = 20.0 * np.log10(rms + EPS)
    times = (starts + frame / 2.0) / sr
    return times, db.astype(np.float32)


def per_second_features(x: np.ndarray, sr: int, bin_s: float) -> dict:
    """Per-second mean loudness (dB) and high-frequency energy ratio.

    hf_ratio = energy above 2 kHz / total energy; laughter/screams/hype skew high,
    a useful local proxy with no model. One rFFT per bin (cheap).
    """
    step = max(1, int(sr * bin_s))
    n_bins = int(np.ceil(len(x) / step))
    sec_db = np.full(n_bins, -120.0, dtype=np.float32)
    sec_hf = np.zeros(n_bins, dtype=np.float32)
    cutoff = 2000.0
    for i in range(n_bins):
        seg = x[i * step:(i + 1) * step]
        if len(seg) < 8:
            continue
        rms = np.sqrt(np.mean(seg.astype(np.float64) ** 2))
        sec_db[i] = 20.0 * np.log10(rms + EPS)
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        freqs = np.fft.rfftfreq(len(seg), 1.0 / sr)
        total = spec.sum() + EPS
        sec_hf[i] = float(spec[freqs >= cutoff].sum() / total)
    return {"sec_db": sec_db, "sec_hf": sec_hf, "n_bins": n_bins, "bin_s": bin_s}


@dataclass
class SilenceMap:
    """Boolean silence over the fine grid, for boundary snapping."""
    times: np.ndarray      # frame center times (s)
    is_silence: np.ndarray  # bool per frame
    hop_ms: int

    def runs(self, min_ms: int) -> list[tuple[float, float]]:
        """Contiguous silence runs >= min_ms, as (start_s, end_s)."""
        min_frames = max(1, int(min_ms / self.hop_ms))
        out: list[tuple[float, float]] = []
        sil = self.is_silence
        i, n = 0, len(sil)
        while i < n:
            if sil[i]:
                j = i
                while j < n and sil[j]:
                    j += 1
                if (j - i) >= min_frames:
                    out.append((float(self.times[i]), float(self.times[j - 1])))
                i = j
            else:
                i += 1
        return out


def silence_map(times: np.ndarray, rms_db: np.ndarray, drop_db: float, hop_ms: int) -> SilenceMap:
    """Mark frames whose loudness is `drop_db` below the quiet-speech floor.

    The floor is the 25th percentile of voiced (> -50 dB) frames - i.e. quiet talking.
    """
    voiced = rms_db[rms_db > -50.0]
    floor = np.percentile(voiced, 25) if voiced.size else -45.0
    thresh = floor - drop_db
    return SilenceMap(times=times, is_silence=(rms_db < thresh), hop_ms=hop_ms)


def speech_ref_db(rms_db: np.ndarray, pct: int) -> float:
    """Reference 'typical speech' level = `pct` percentile of voiced frames."""
    voiced = rms_db[rms_db > -50.0]
    return float(np.percentile(voiced, pct)) if voiced.size else -35.0
