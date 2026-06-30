"""All tunable numbers for the clip pipeline live here.

Every value was chosen from researched platform/editing specs (2026) and
verified against real frames of Stream1.mp4. Edit numbers here; the rest of the
code reads them. CLI flags in clip.py override the most common ones at runtime.

Color note:
  ffmpeg drawbox/overlay want 0xRRGGBB.
  ASS subtitle colors are &HAABBGGRR (bytes reversed!) - use hex_to_ass().
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace


# --------------------------------------------------------------------------- #
# Paths. Code lives in OneDrive (syncs to the Desktop). Heavy/intermediate
# files (venv, wav, model cache, rendered clips) stay LOCAL to avoid OneDrive
# churn. Output clips default to a folder next to the VOD (set at runtime).
# --------------------------------------------------------------------------- #
LOCAL_ROOT = r"C:\Users\Brett\clipforge"
WORK_DIR = os.path.join(LOCAL_ROOT, "work")        # wav, transcript cache, frames, per-clip ass
FONTS_DIR = os.path.join(LOCAL_ROOT, "fonts")      # Poppins TTFs copied here for libass
MODEL_DIR = os.path.join(LOCAL_ROOT, "models")     # faster-whisper / HF + ggml model cache
WCPP_DIR = os.path.join(LOCAL_ROOT, "whispercpp")   # prebuilt whisper.cpp (Vulkan) binary dir (AMD GPU)
ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")  # watermark.png

# ffmpeg/ffprobe: resolved in util.py (PATH first, then this WinGet fallback).
FFMPEG_FALLBACK = (
    r"C:\Users\Brett\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"
)
FFPROBE_FALLBACK = FFMPEG_FALLBACK.replace("ffmpeg.exe", "ffprobe.exe")


# --------------------------------------------------------------------------- #
# Brand colors (hex RGB). See memory/brand-kit.md.
# --------------------------------------------------------------------------- #
CREAM = "F4F1EA"
GOLD = "E7C58A"      # peachy gold (the accent we settled on)
LAVENDER = "C3BBE6"
DARK = "0B0912"
WHITE = "FFFFFF"
BLACK = "000000"
TWITCH_PURPLE = "9146FF"


def hex_to_ffmpeg(h: str) -> str:
    """'E7C58A' -> '0xE7C58A'."""
    return "0x" + h.lstrip("#").upper()


def hex_to_ass(h: str, alpha: int = 0) -> str:
    """'E7C58A' -> '&H008AC5E7' (ASS is AABBGGRR; alpha 0=opaque, 255=clear)."""
    h = h.lstrip("#").upper()
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}"


@dataclass
class Config:
    # ---- transcription (faster-whisper) ----
    model: str = "large-v3"
    device: str = "auto"                 # auto -> cuda if available else cpu
    compute_type_gpu: str = "float16"    # ~3GB VRAM, fits the RTX 4060 8GB
    compute_type_cpu: str = "int8"       # validation-only fallback (hours)
    language: str = "en"
    beam_size: int = 5
    vad_filter: bool = False             # OFF: VAD mis-maps word timestamps (captions drift early)
    condition_on_previous_text: bool = False  # off -> fewer hallucination loops on long noisy audio

    # ---- transcription backend selection ----
    # auto: whisper.cpp (Vulkan) when its binary+device are present (AMD desktop), else
    # faster-whisper (CUDA/CPU, the NVIDIA laptop). Force with 'whispercpp' / 'faster_whisper'.
    transcribe_backend: str = "auto"     # auto | whispercpp | faster_whisper
    # whisper.cpp (Vulkan, AMD GPU). large-v3-turbo: ~28x realtime here with DTW word timing
    # whose accuracy matches large-v3 once the lead-correction below cancels its ~0.4s lag.
    wcpp_model: str = "large-v3-turbo"   # ggml model id (see transcribe_whispercpp._MODEL_MAP)
    # whisper.cpp DTW token onsets run a consistent ~0.4s LATE vs faster-whisper; shift word
    # starts earlier by this to center karaoke sync (measured: median err 0.32->0.22s). 0 = off.
    wcpp_word_lead_s: float = 0.40

    # ---- output canvas (9:16) ----
    out_w: int = 1080
    out_h: int = 1920
    fps: int = 60
    seam_y: int = 768                    # 40% facecam / 60% gameplay split

    # ---- facecam crop FROM the 1920x1080 source (default; autodetect may override) ----
    # 380x270 (1.407) scales to the 1080x768 top panel (1.406) edge-to-edge.
    cam_x: int = 90
    cam_y: int = 360
    cam_w: int = 380
    cam_h: int = 270
    cam_panel_h: int = 768               # = seam_y
    autodetect_cam: bool = True          # find the peachy-gold cam border per clip
    # source cam box bounds (the OBS Gameplay scene cam lives here) - used to clamp autodetect
    cam_src_xmin: int = 30
    cam_src_xmax: int = 540
    cam_src_ymin: int = 330
    cam_src_ymax: int = 660
    # facecam framing: tighten the detected/default cam onto head+shoulders (the webcam is
    # framed wide, so the full cam wastes the top panel on empty room). Fractions of the cam box.
    # Tuned to the current cam setup; retune if you reframe your webcam, or use the Studio's
    # manual cam mode for a per-clip override. Set cam_face_crop=False to use the full cam.
    cam_face_crop: bool = True
    cam_face_x: float = 0.26             # left inset as a fraction of cam width
    cam_face_y: float = 0.07             # top inset as a fraction of cam height
    cam_face_w: float = 0.67             # kept width as a fraction of cam width
    cam_face_h: float = 0.85             # kept height as a fraction of cam height

    # ---- gameplay crop FROM source (default; derived from cam box when autodetecting) ----
    # 1012x1080 (0.937) scales to the 1080x1152 bottom panel (0.9375). x>=520 clears the cam.
    gp_x: int = 686
    gp_y: int = 0
    gp_w: int = 1012
    gp_h: int = 1080
    gp_panel_h: int = 1152               # = out_h - seam_y

    # ---- divider at the seam ----
    div_gold_y: int = 765
    div_gold_h: int = 6
    div_lav_y: int = 771
    div_lav_h: int = 2

    # ---- captions (animated word-by-word karaoke, burned via ASS/libass) ----
    cap_font: str = "Poppins ExtraBold"
    cap_size: int = 80                   # short-form spec is 80-120px at 1080 wide
    cap_uppercase: bool = True
    cap_fill: str = WHITE                # inactive words
    cap_highlight: str = GOLD            # the currently-spoken word
    cap_outline_color: str = BLACK
    cap_outline_w: int = 10              # thick stroke reads over busy gameplay at 1080w
    cap_shadow: int = 3
    cap_max_words: int = 4               # per on-screen line
    cap_break_pause_s: float = 0.6       # also break a line on a pause longer than this
    cap_margin_v: int = 520              # lifts baseline to ~y1330-1380 (above the 400px bottom dead zone)
    cap_margin_lr: int = 60
    cap_alignment: int = 2               # 2 = bottom-center
    cap_pop: bool = False                # scale-pop the active word (off by default to avoid line re-centering jitter)
    cap_pop_scale: int = 110

    # ---- watermark (persistent Twitch follow tag) ----
    # Upper-RIGHT by default: TikTok's top-left/center has the search + tab UI; the right rail
    # (like/comment/share) is lower, so the upper-right corner stays clear. wm_align "right" makes
    # wm_x the margin from the RIGHT edge; "left" makes it the left margin. wm_y is the top inset.
    wm_handle: str = "theenchantingchicken"
    wm_cta: str = "FOLLOW ON TWITCH"
    wm_align: str = "right"              # "right" | "left"
    wm_x: int = 36                       # margin from the aligned edge
    wm_y: int = 250                      # top inset — below the TikTok top bar
    wm_opacity: float = 0.80
    wm_text_px: int = 34
    wm_font: str = "Poppins SemiBold"

    # ---- titlecard: a hook headline burned over the FIRST few seconds of every clip ----
    # The short-form convention now — survives the ~3s retention checkpoint and lands on the auto-cover.
    # Seeded per clip as a normal text element, so it's fully editable / movable / removable in the studio.
    titlecard: bool = True
    titlecard_secs: float = 3.0
    tc_size: int = 74
    tc_font: str = "Poppins ExtraBold"
    tc_color: str = WHITE
    tc_outline: int = 8
    tc_bg: str = "#0B0912"
    tc_bg_alpha: float = 0.52
    tc_y: int = 410                      # top inset: below the watermark, over the facecam
    tc_margin_lr: int = 70

    # ---- detection (local scoring on numpy PCM + transcript) ----
    score_bin_s: float = 1.0             # 1-second scoring bins
    smooth_bins: int = 5                 # moving-average window over bins
    frame_ms: int = 25                   # fine grid for silence/pauses
    hop_ms: int = 10
    win_primary_s: int = 30              # main scan window
    win_punchy_s: int = 15               # parallel short scan
    win_hop_s: int = 3
    clip_min_s: int = 16
    clip_max_s: int = 60
    seed_back_s: int = 8                 # rough start = peak - this. Tight pre-roll front-loads the
                                         # hook for retention (was 20); start still snaps to a clean
                                         # word/pause boundary so nothing is cut. Raise for more setup.
    seed_fwd_s: int = 16                 # rough end   = peak + this (let the payoff breathe)
    dedupe_gap_s: int = 90               # min spacing between picked clip centers
    clip_count: int = 12                 # how many clips to render
    keep_for_rerank: int = 30            # candidates handed to the optional LLM
    # robust normalization references
    speech_pct: int = 60                 # percentile of voiced frames = "typical speech" ref
    loud_db: float = 6.0                 # +dB over ref = "got loud"
    freakout_db: float = 11.0            # +dB over ref = "freakout"
    pause_sentence_ms: int = 300         # silence >= this = sentence boundary (snap point)
    pause_micro_ms: int = 150            # silence >= this = micro snap point
    pause_drop_db: float = 6.0           # RMS this far below quiet-speech floor = pause
    # per-window score weights (sum to 1.0). Tuned to favour REACTIONS (a sudden freakout, a laugh,
    # dynamic audio) over talking density — plain talk-rate peaks during lobby/menu socialising, which
    # was pulling clips off the actual gameplay. TXT down, SP/LAU/VAR up.
    w_energy: float = 0.18               # E  sustained loudness vs baseline
    w_spike: float = 0.28                # SP sudden +dB rise (the freakout) — the #1 highlight signal
    w_sustain: float = 0.11              # SUS fraction of window in hype range
    w_text: float = 0.15                 # TXT lexicon + speech-rate + repetition + questions (was 0.28: lobby bias)
    w_laugh: float = 0.16                # LAU laugh tokens + hf-energy proxy
    w_var: float = 0.12                  # VAR loudness dynamics (gameplay swings; lobby chatter is flat)

    # ---- platform presets (length targets + safe zones). One 1080x1920 master
    # serves both; these drive metadata + which platforms a clip best fits. ----
    platforms: dict = field(default_factory=lambda: {
        "tiktok": dict(target_s=26, band=(18, 34), soft_max_s=60,
                       safe_top=150, safe_bottom=320, safe_left=40, safe_right=160,
                       hashtags=4),
        "shorts": dict(target_s=32, band=(24, 42), soft_max_s=180,
                       safe_top=180, safe_bottom=400, safe_left=50, safe_right=120,
                       hashtags=4),
    })

    # ---- chat auto-grab (opt-in per clip) ----
    chat_rect: tuple = (30, 640, 510, 430)   # source crop of the on-stream chat panel (gameplay scene)
    chat_default_scale: float = 0.45         # inset width as a fraction of out_w
    chat_default_pos: str = "bottom-left"    # bottom-left|bottom-right|top-left|top-right|center
    chat_default_dur: float = 4.0            # seconds the chat inset stays on screen

    # ---- dashboard ----
    dash_port: int = 8765

    # ---- LLM re-rank (optional, --ai) ----
    ai_enabled: bool = False
    ai_model: str = "claude-sonnet-4-6"  # strong + cost-effective for titles/edits
    ai_blend: float = 0.5                # final = ai_blend*llm + (1-ai_blend)*local
    ai_max_trim_s: float = 3.0           # LLM may nudge boundaries this much (re-validated vs silence map)

    # ---- encode ----
    use_nvenc: bool = True               # h264_nvenc -preset p5 -cq 23; falls back to libx264 on error
    x264_crf: int = 20
    audio_bitrate: str = "192k"

    # ---- long-form (20-90 min 16:9 YouTube session videos; see LONGFORM_PLAN.md) ----
    lf_min_min: float = 20.0             # shortest long-form segment (minutes)
    lf_max_min: float = 90.0             # longest before we split it
    lf_target_min: float = 45.0          # preferred length when splitting a long block
    lf_break_min: float = 2.5            # >= this many consecutive DEAD minutes = a stream break (split point)
    lf_dead_frac: float = 0.12           # a minute is "dead" if < this fraction of its seconds are active
    lf_merge_gap_min: float = 4.0        # merge an under-length block into a neighbor if the gap is <= this
    lf_chapter_min: float = 6.0          # aim one chapter roughly every this many minutes
    lf_chapter_label_words: int = 3      # keywords per chapter label
    lf_height: int = 1080                # output height (16:9 -> width auto); 720 for smaller files
    lf_watermark: bool = True            # corner Twitch watermark on long-form too
    lf_count: int = 6                    # max long-form videos to keep from one VOD
    lf_srt_max_words: int = 9            # words per subtitle line in the sidecar .srt

    def with_overrides(self, **kw) -> "Config":
        return replace(self, **kw)


def load_config() -> Config:
    """Return defaults. (Hook: merge a config.json here later if desired.)"""
    return Config()
