"""Shared helpers: ffmpeg/ffprobe discovery, subprocess, time + path formatting."""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
from typing import Sequence

from . import config

# --- Windows: stop ffmpeg / whisper.cpp / ffprobe from each flashing up its own console window ---
# The desktop launcher runs under pythonw (no console), so every child console process would
# otherwise pop a black terminal — dozens of them during a render. Inject CREATE_NO_WINDOW into
# EVERY subprocess this process spawns (run() funnels through Popen), so the app stays clean. GUI
# children (the app-window browser) are unaffected; nothing in ClipForge ever wants a child console.
if sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen_init = subprocess.Popen.__init__

    def _silent_popen_init(self, *a, **kw):  # noqa: ANN001
        if not kw.get("creationflags"):
            kw["creationflags"] = _CREATE_NO_WINDOW
        _orig_popen_init(self, *a, **kw)

    if getattr(subprocess.Popen.__init__, "__name__", "") != "_silent_popen_init":
        subprocess.Popen.__init__ = _silent_popen_init  # type: ignore[method-assign]


def log(msg: str) -> None:
    print(msg, flush=True)


def _winget_ffmpeg(name: str) -> str | None:
    """Newest Gyan.FFmpeg winget portable build's exe, if installed.

    winget installs ffmpeg to a versioned dir and modifies PATH, but a running
    process won't see the new PATH until restarted — so resolve the absolute path
    rather than trusting shutil.which() right after an install. Survives upgrades
    (the literal config.FFMPEG_FALLBACK path is version-pinned and would break)."""
    import glob
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    pats = [os.path.join(base, "Gyan.FFmpeg_*", "ffmpeg-*-full_build", "bin", name),
            os.path.join(base, "Gyan.FFmpeg.Essentials_*", "ffmpeg-*-essentials_build", "bin", name)]
    hits = [h for p in pats for h in glob.glob(p)]
    return max(hits, key=os.path.getmtime) if hits else None


@functools.lru_cache(maxsize=2)
def ffmpeg() -> str:
    return shutil.which("ffmpeg") or _winget_ffmpeg("ffmpeg.exe") or config.FFMPEG_FALLBACK


@functools.lru_cache(maxsize=2)
def ffprobe() -> str:
    return shutil.which("ffprobe") or _winget_ffmpeg("ffprobe.exe") or config.FFPROBE_FALLBACK


def run(cmd: Sequence[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a command. Raises on non-zero when check=True."""
    return subprocess.run(
        list(cmd),
        check=check,
        text=True,
        capture_output=capture,
        encoding="utf-8",
        errors="replace",
    )


def probe_duration(path: str) -> float:
    out = run(
        [ffprobe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture=True,
    ).stdout.strip()
    return float(out)


def ensure_dirs(*paths: str) -> None:
    for p in paths:
        if p:
            os.makedirs(p, exist_ok=True)


def hhmmss(seconds: float) -> str:
    """123.4 -> '02:03'. Hours added only when needed. For filenames/manifests."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def stamp_for_name(seconds: float) -> str:
    """123.4 -> '0h02m03s' for safe filenames."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def ass_time(seconds: float) -> str:
    """ASS timestamp h:mm:ss.cs (centiseconds)."""
    if seconds < 0:
        seconds = 0.0
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ff_escape_path(path: str) -> str:
    """Escape a Windows path for use INSIDE an ffmpeg filtergraph option value.

    'C:\\dir\\f.ass' -> "'C\\:/dir/f.ass'"  (forward slashes, escaped colon,
    wrapped in single quotes). This is the verified-working form for the
    subtitles/fontsdir filters on Windows.
    """
    p = path.replace("\\", "/").replace(":", "\\:")
    return f"'{p}'"


def add_cuda_dll_dirs() -> None:
    """Put the pip-installed nvidia cuBLAS/cuDNN bin dirs on the DLL search path.

    CTranslate2 4.x on Windows needs cudnn_ops64_9.dll / cublas64_12.dll etc.;
    the nvidia-*-cu12 wheels ship them under site-packages/nvidia/**/bin but do
    not add them to PATH. Call this before constructing a CUDA WhisperModel.
    """
    try:
        import nvidia  # noqa: F401
    except Exception:
        return
    # nvidia is a namespace package: __file__ is None, but __path__ lists the dirs.
    bases = list(getattr(nvidia, "__path__", []) or [])
    if not bases and getattr(nvidia, "__file__", None):
        bases = [os.path.dirname(nvidia.__file__)]
    for base in bases:
        for root, _dirs, _files in os.walk(base):
            if os.path.basename(root).lower() == "bin":
                try:
                    os.add_dll_directory(root)
                except Exception:
                    pass
                os.environ["PATH"] = root + os.pathsep + os.environ.get("PATH", "")


def ensure_fonts() -> int:
    """Copy Poppins*.ttf into FONTS_DIR so libass (ffmpeg subtitles) can find them.

    Returns the number of Poppins font files available in FONTS_DIR.
    """
    import glob
    import shutil as _sh
    ensure_dirs(config.FONTS_DIR)
    srcs = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
        r"C:\Windows\Fonts",
    ]
    for d in srcs:
        for f in glob.glob(os.path.join(d, "Poppins*.ttf")):
            dst = os.path.join(config.FONTS_DIR, os.path.basename(f))
            if not os.path.exists(dst):
                try:
                    _sh.copy2(f, dst)
                except Exception:
                    pass
    return len(glob.glob(os.path.join(config.FONTS_DIR, "Poppins*.ttf")))


SECRET_PATH = os.path.join(config.LOCAL_ROOT, "secret.json")


def get_api_key() -> str | None:
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return k
    try:
        with open(SECRET_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh).get("anthropic_key") or None
    except Exception:
        return None


def set_api_key(key: str) -> None:
    key = (key or "").strip()
    if not key:
        raise ValueError("empty API key")  # never clobber a good key with blank
    ensure_dirs(config.LOCAL_ROOT)
    with open(SECRET_PATH, "w", encoding="utf-8") as fh:
        json.dump({"anthropic_key": key}, fh)


def write_json(path: str, obj) -> None:
    # Atomic: write a temp file then os.replace, so a crash mid-write never
    # leaves a truncated project.json (which would lose all edits on next load).
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
