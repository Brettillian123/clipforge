#!/usr/bin/env python
"""Launch the ClipForge Studio.

  python dashboard.py                      # open the Home picker (choose a VOD to clip)
  python dashboard.py "C:\\...\\some.mp4"     # open straight into that VOD's clips (if any)
  python dashboard.py "C:\\...\\clips"        # open a clips folder directly
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import config, jobs, server  # noqa: E402

# where the Home picker looks for VODs + clip batches. Defaults to your Videos folder;
# override with the CLIPFORGE_LIBRARY env var (e.g. a dedicated recordings drive).
LIBRARY = os.environ.get("CLIPFORGE_LIBRARY") or os.path.join(os.path.expanduser("~"), "Videos")


def _setup_logging():
    """Launched without a console (pythonw / the desktop shortcut), stdout/stderr are None (or a dead
    handle) and the server's print()-logging would crash. Redirect logs to a file so it runs silently
    + debuggably. (When launched from a real console, leave logging on the console.)"""
    need = sys.stdout is None or sys.stderr is None
    if not need:
        try:
            sys.stderr.write(""); sys.stderr.flush()        # probe: a dead pythonw handle raises here
        except Exception:  # noqa: BLE001
            need = True
    if not need:
        return
    try:
        os.makedirs(config.LOCAL_ROOT, exist_ok=True)
        f = open(os.path.join(config.LOCAL_ROOT, "clipforge.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
    except OSError:
        import io
        sys.stdout = sys.stderr = io.StringIO()


def main() -> int:
    _setup_logging()
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    library_root, active_dir = LIBRARY, None
    if arg:
        if os.path.isdir(arg):                                  # a clips folder
            active_dir = os.path.abspath(arg)
            library_root = os.path.dirname(active_dir) or LIBRARY
        elif arg.lower().endswith(".json"):                    # a project.json
            active_dir = os.path.dirname(os.path.abspath(arg))
            library_root = os.path.dirname(active_dir) or LIBRARY
        else:                                                  # a VOD path
            library_root = os.path.dirname(os.path.abspath(arg)) or LIBRARY
            for cand in (jobs.clips_dir_for(arg),                       # new per-VOD folder
                         os.path.join(library_root, "clips")):         # legacy folder
                if os.path.exists(os.path.join(cand, "project.json")):
                    active_dir = cand
                    break
    server.serve(config.load_config(), library_root, active_dir=active_dir, app_mode=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
