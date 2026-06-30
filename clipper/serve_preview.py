"""Preview server for the Studio (no auto-opened browser).

No arg -> Home picker (library = Videos). Or pass a clips folder / VOD / project.json
to open straight into it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import config, jobs, server  # noqa: E402

LIBRARY = os.environ.get("CLIPFORGE_LIBRARY") or os.path.join(os.path.expanduser("~"), "Videos")

arg = sys.argv[1] if len(sys.argv) > 1 else None
library_root, active_dir = LIBRARY, None
if arg:
    if os.path.isdir(arg):
        active_dir = os.path.abspath(arg)
        library_root = os.path.dirname(active_dir) or LIBRARY
    elif arg.lower().endswith(".json"):
        active_dir = os.path.dirname(os.path.abspath(arg))
        library_root = os.path.dirname(active_dir) or LIBRARY
    else:
        library_root = os.path.dirname(os.path.abspath(arg)) or LIBRARY
        for cand in (jobs.clips_dir_for(arg), os.path.join(library_root, "clips")):
            if os.path.exists(os.path.join(cand, "project.json")):
                active_dir = cand
                break

server.serve(config.load_config(), library_root, active_dir=active_dir, open_browser=False)
