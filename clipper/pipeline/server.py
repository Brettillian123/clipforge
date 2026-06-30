"""Local dashboard server (stdlib only).

Serves the dashboard UI, streams clip mp4s with HTTP range support (so the
browser can scrub), and exposes a small JSON API to edit specs, trigger renders,
run AI edits, grab chat/frame previews, and store the API key.

Thread-safety: ThreadingHTTPServer runs each request (and each render) on its own
thread, so every read-modify-write of the shared project goes through STATE.lock,
and project.json is written atomically (util.write_json) from a snapshot.
"""
from __future__ import annotations

import copy
import hashlib
import html as _html
import json
import math
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import branding, camdetect, config, longform, project as proj, render, transcribe, util
from .util import log

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITABLE = {"approved", "captions_enabled", "caption_style", "caption_words",
            "cam", "cam_mode", "chat", "metadata", "segments", "notes", "elements"}


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _no_nan(_v):
    raise ValueError("NaN/Infinity not allowed")


# the fields that define the rendered/exported video (used for staleness + reset snapshots)
EXPORT_FIELDS = ("segments", "captions_enabled", "caption_style", "caption_words",
                 "chat", "elements", "cam", "cam_mode")


def _export_sig(c) -> str:
    """Signature of everything that affects the burned-in export, to detect staleness."""
    keys = {k: c.get(k) for k in EXPORT_FIELDS}
    return hashlib.md5(json.dumps(keys, sort_keys=True, default=str).encode()).hexdigest()[:16]


class State:
    _VIDEO_EXT = (".mp4", ".mkv", ".mov", ".flv", ".ts", ".webm")

    def __init__(self, cfg: config.Config, library_root: str, active_dir: str | None = None):
        self.cfg = cfg
        self.library_root = library_root      # folder the Home picker lists VODs + jobs from
        self.active = False                   # is a clips project currently loaded?
        self.out_dir = None
        self.project = None
        self.vod = None
        self.vod_duration = 0.0
        self.lf_dir = None
        self.transcript = {"words": [], "segments": []}
        self.watermark = branding.render_watermark(cfg)
        self.jobs: dict = {}
        self.job_seq = 0
        self.pending_post: dict = {}          # cid -> {platforms,when,privacy}: post once its export finishes
        self.cancels: dict = {}               # jid -> threading.Event (set => cancel that render)
        self.latest: dict = {}                # (cid, kind) -> jid of the most recent render request
        self.lock = threading.RLock()         # guards self.project + jobs
        self.render_lock = threading.Lock()   # serializes ffmpeg renders
        if active_dir:
            self.activate(active_dir)

    def activate(self, out_dir: str) -> None:
        """Load a clips folder as the active project (recovering from .bak if its VOD moved)."""
        project = proj.load_project(out_dir)
        vod = project["vod"]
        if not os.path.exists(vod):
            bak = proj.project_path(out_dir) + ".bak"
            if os.path.exists(bak):
                try:
                    alt = util.read_json(bak)
                except Exception:  # noqa: BLE001
                    alt = None
                if alt and os.path.exists(alt.get("vod", "")):
                    log(f"[server] project.json VOD missing ({vod}); recovered from backup ({alt['vod']})")
                    project, vod = alt, alt["vod"]
                    proj.save_project(out_dir, alt)
            if not os.path.exists(vod):
                raise RuntimeError(f"VOD not found: {vod}\n  Re-make clips for this VOD to regenerate it.")
        tp = transcribe.transcript_path(self.cfg, vod)
        with self.lock:
            self.out_dir, self.project, self.vod = out_dir, project, vod
            self.vod_duration = util.probe_duration(vod)
            from . import jobs as _jobs
            self.lf_dir = _jobs.longform_dir_for(vod)        # '<stem> - longform' (separate from clips)
            # the full transcript is only a caption fallback (clip_words re-transcribes slices), so
            # tolerate it being absent — the dashboard still opens after a cache cleanup
            self.transcript = util.read_json(tp) if os.path.exists(tp) else {"words": [], "segments": []}
            self.active = True
        log(f"[dashboard] active project: {os.path.basename(out_dir)} ({len(project['clips'])} clips)")

    # ---- Home: library + new jobs ----
    def library(self) -> dict:
        """VODs available to clip + existing clip batches, for the Home picker."""
        root, vods, jobs = self.library_root, [], []
        try:
            for name in os.listdir(root):
                p = os.path.join(root, name)
                if os.path.isfile(p) and name.lower().endswith(self._VIDEO_EXT) and os.path.getsize(p) > 1_000_000:
                    st = os.stat(p)
                    vods.append({"path": p, "name": name, "size": st.st_size, "mtime": st.st_mtime})
                elif os.path.isdir(p) and os.path.exists(os.path.join(p, "project.json")):
                    jobs.append(self._job_summary(p))
        except OSError:
            pass
        vods.sort(key=lambda v: v["mtime"], reverse=True)
        jobs.sort(key=lambda j: j["mtime"], reverse=True)
        return {"vods": vods, "jobs": jobs, "active": self.active, "active_dir": self.out_dir}

    def _job_summary(self, d: str) -> dict:
        n, vod = 0, ""
        try:
            pj = proj.load_project(d)
            n, vod = len(pj.get("clips", [])), pj.get("vod", "")
        except Exception:  # noqa: BLE001
            pass
        return {"dir": d, "name": os.path.basename(d), "vod": os.path.basename(vod),
                "clips": n, "mtime": os.path.getmtime(os.path.join(d, "project.json"))}

    def start_new_job(self, vod: str, count=None) -> str:
        """Run the clip pipeline on `vod` in a background thread; activates the result when done."""
        from . import jobs as jobsmod
        with self.lock:
            self.job_seq += 1
            jid = f"p{self.job_seq}"
            self.jobs[jid] = {"kind": "pipeline", "status": "running", "phase": "Starting…",
                              "progress": 0, "vod": os.path.basename(vod), "error": ""}
        out_dir = jobsmod.clips_dir_for(vod)

        def run():
            def pcb(phase, frac=None):
                j = self.jobs.get(jid)
                if j:
                    j["phase"] = phase
                    if frac is not None:
                        j["progress"] = int(frac * 100)
            try:
                use_ai = bool(util.get_api_key())     # AI picks the best clips + writes content-relevant
                project = jobsmod.make_clips(self.cfg, vod, out_dir, count=count, ai=use_ai, progress_cb=pcb)
                #                                  title/caption/hashtags from the transcript (falls back to local if it fails)
                if project is None:
                    self.jobs[jid].update(status="error", error="No clips found (the VOD looks mostly silent).")
                    return
                self.activate(out_dir)
                self.jobs[jid].update(status="done", out_dir=out_dir, progress=100, phase="Done")
            except Exception as e:  # noqa: BLE001
                self.jobs[jid].update(status="error", error=str(e))
                log(f"[server] pipeline job failed for {vod}: {e}")
        threading.Thread(target=run, daemon=True).start()
        return jid

    def start_longform_job(self, vod: str) -> str:
        """Run the LONG-FORM pipeline on `vod` (transcribe -> plan -> render 16:9 videos) in a
        background thread, into '<stem> - longform'. Independent of clips; opens the folder when done."""
        from . import jobs as jobsmod
        with self.lock:
            self.job_seq += 1
            jid = f"lfj{self.job_seq}"
            self.jobs[jid] = {"kind": "longform_job", "status": "running", "phase": "Starting…",
                              "progress": 0, "vod": os.path.basename(vod), "error": ""}
        out_dir = jobsmod.longform_dir_for(vod)

        def run():
            def pcb(phase, frac=None):
                j = self.jobs.get(jid)
                if j:
                    j["phase"] = phase
                    if frac is not None:
                        j["progress"] = int(frac * 100)
            try:
                segs = jobsmod.make_longform(self.cfg, vod, out_dir, progress_cb=pcb)
                if not segs:
                    self.jobs[jid].update(status="error",
                                          error="No long-form segments found (VOD too short or mostly silent).")
                    return
                self.jobs[jid].update(status="done", out_dir=out_dir, count=len(segs), progress=100, phase="Done")
                try:
                    os.startfile(out_dir)                 # show the finished long-form videos
                except Exception:  # noqa: BLE001
                    pass
            except Exception as e:  # noqa: BLE001
                self.jobs[jid].update(status="error", error=str(e))
                log(f"[server] longform job failed for {vod}: {e}")
        threading.Thread(target=run, daemon=True).start()
        return jid

    def _under_library(self, p: str) -> bool:
        try:
            return os.path.commonpath([os.path.abspath(self.library_root), os.path.abspath(p)]) \
                == os.path.abspath(self.library_root)
        except ValueError:
            return False

    def review_vod(self, p: str) -> str | None:
        """A library VOD path, validated for the Home 'review' player (inside library_root, a video
        file) — so we never stream an arbitrary path off disk."""
        p = os.path.abspath(p or "")
        if self._under_library(p) and os.path.isfile(p) and p.lower().endswith(self._VIDEO_EXT):
            return p
        return None

    def delete_job(self, d: str) -> bool:
        """Delete a clip set (its '<stem> - clips' folder). Safety: only a directory that lives inside
        library_root AND actually holds a project.json — never a VOD or an unrelated folder. Deactivates
        it first if it's the open project."""
        import shutil
        d = os.path.abspath(d or "")
        if not (self._under_library(d) and d != os.path.abspath(self.library_root)
                and os.path.isdir(d) and os.path.exists(os.path.join(d, "project.json"))):
            return False
        with self.lock:
            if self.active and self.out_dir and os.path.abspath(self.out_dir) == d:
                self.active = False
                self.out_dir = self.project = self.vod = None
        try:
            shutil.rmtree(d)
            log(f"[server] deleted clip set: {d}")
            return True
        except OSError as e:  # noqa: BLE001
            log(f"[server] could not delete {d}: {e}")
            return False

    def delete_vod(self, p: str) -> bool:
        """Send a library VOD to the Recycle Bin (recoverable). Safety: only a video FILE that lives
        inside library_root — never a directory, never an arbitrary path off disk. Recordings are
        valuable + irreplaceable, so we trash (recoverable) rather than hard-delete; clip/longform
        folders made from it are left alone (delete those separately)."""
        p = os.path.abspath(p or "")
        if not (self._under_library(p) and os.path.isfile(p) and p.lower().endswith(self._VIDEO_EXT)):
            return False
        try:
            try:
                from send2trash import send2trash
                send2trash(p)                          # -> OS Recycle Bin (restorable)
                log(f"[server] VOD moved to Recycle Bin: {p}")
            except ImportError:
                os.remove(p)                           # fallback: permanent (send2trash not installed)
                log(f"[server] VOD permanently deleted (send2trash unavailable): {p}")
            return True
        except OSError as e:  # noqa: BLE001
            log(f"[server] could not delete VOD {p}: {e}")
            return False

    # ---- batch finish: post approved / delete rejected ----
    def _post_items(self, cid: str, platforms, when: int, privacy: str) -> list:
        """Build upload items (one per platform) for a clip from its finished file + metadata.
        Returns [] if the clip isn't downloaded yet (no 'Ready to post' file)."""
        from . import jobs as jobsmod
        with self.lock:
            c = self.find(cid)
            if not c:
                return []
            meta = copy.deepcopy(c.get("metadata") or {})
        mp4 = os.path.join(jobsmod.ready_dir(self.out_dir), f"{cid}.mp4")
        cover = os.path.join(jobsmod.ready_dir(self.out_dir), f"{cid}.jpg")
        if not os.path.exists(mp4):
            return []
        items = []
        for plat in platforms:
            if plat == "youtube":
                m = meta.get("shorts") or {}
                items.append({"platform": "youtube", "clip": cid, "mp4": mp4, "cover": cover,
                              "title": (m.get("title") or "")[:100], "description": m.get("caption") or "",
                              "hashtags": m.get("hashtags") or [], "privacy": privacy or "public", "when": when})
            elif plat == "tiktok":
                m = meta.get("tiktok") or {}
                items.append({"platform": "tiktok", "clip": cid, "mp4": mp4, "cover": cover,
                              "title": (m.get("title") or "")[:100], "caption": m.get("caption") or "",
                              "hashtags": m.get("hashtags") or [], "when": when})
        return items

    def finish_post_approved(self, platforms, when: int, privacy: str) -> dict:
        """Post every APPROVED clip to the chosen platforms. Clips already downloaded are queued now;
        approved clips whose export is missing/stale are rendered first, then auto-posted on completion."""
        from . import jobs as jobsmod, posting
        platforms = [p for p in (platforms or []) if p in ("youtube", "tiktok")]
        if not platforms:
            return {"error": "Pick at least one platform."}
        with self.lock:
            approved = [c["id"] for c in self.project["clips"] if c.get("approved") is True]
            ready_ids, render_ids = [], []
            for cid in approved:
                c = self.find(cid)
                fresh = (_export_sig(c) == c.get("export_sig")
                         and os.path.exists(os.path.join(jobsmod.ready_dir(self.out_dir), f"{cid}.mp4")))
                (ready_ids if fresh else render_ids).append(cid)
            for cid in render_ids:                         # post these as soon as their export finishes
                self.pending_post[cid] = {"platforms": platforms, "when": when, "privacy": privacy}
        P = posting.init(self.cfg)
        posted = 0
        for cid in ready_ids:
            items = self._post_items(cid, platforms, when, privacy)
            if items:
                P.enqueue(items)
                posted += 1
        for cid in render_ids:
            self.start_render(cid, "export")
        return {"approved": len(approved), "posted": posted, "rendering": len(render_ids)}

    def _fire_pending_post(self, cid: str) -> None:
        """After an export render finishes, post the clip if a batch-post was queued for it."""
        with self.lock:
            pp = self.pending_post.pop(cid, None)
        if not pp:
            return
        try:
            items = self._post_items(cid, pp["platforms"], pp["when"], pp["privacy"])
            if items:
                from . import posting
                posting.init(self.cfg).enqueue(items)
                log(f"[server] auto-posted {cid} after export ({len(items)} item(s)).")
        except Exception as e:  # noqa: BLE001
            log(f"[server] auto-post after export failed for {cid}: {e}")

    def delete_rejected(self) -> dict:
        """Remove every REJECTED clip from the active batch: delete its rendered files (base, export,
        and the 'Ready to post' copy + cover) and drop it from project.json. Generated files only —
        the source VOD is never touched, and clips can be re-made anytime."""
        from . import jobs as jobsmod
        ready = jobsmod.ready_dir(self.out_dir)
        removed = 0
        with self.lock:
            keep, drop = [], []
            for c in self.project["clips"]:
                (drop if c.get("approved") is False else keep).append(c)
            for c in drop:
                cid = c["id"]
                self.pending_post.pop(cid, None)
                for path in (os.path.join(self.out_dir, c.get("file") or f"{cid}.mp4"),
                             os.path.join(self.out_dir, c.get("export_file") or f"{cid}.export.mp4"),
                             os.path.join(ready, f"{cid}.mp4"),
                             os.path.join(ready, f"{cid}.jpg")):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass
                removed += 1
            self.project["clips"] = keep
            self.save_locked()
        log(f"[server] deleted {removed} rejected clip(s) from {os.path.basename(self.out_dir)}")
        return {"deleted": removed}

    def open_ready_folder(self) -> bool:
        """Open the 'Ready to post' folder (the clean finished clips) in Explorer."""
        if not self.active:
            return False
        from . import jobs as jobsmod
        d = jobsmod.ready_dir(self.out_dir)
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)                       # Windows: open the folder in Explorer
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---- project helpers (call under self.lock) ----
    def find(self, cid: str):
        for c in self.project["clips"]:
            if c["id"] == cid:
                return c
        return None

    def save_locked(self):
        snapshot = copy.deepcopy(self.project)
        proj.save_project(self.out_dir, snapshot)

    def public_project(self) -> dict:
        with self.lock:
            clips = []
            for c in self.project["clips"]:
                d = copy.deepcopy(c)
                fpath = os.path.join(self.out_dir, c.get("file") or "")
                exists = bool(c.get("file") and os.path.exists(fpath))
                d["version"] = int(os.path.getmtime(fpath) * 1000) if exists else 0
                d["exists"] = exists
                efile = c.get("export_file") or ""
                epath = os.path.join(self.out_dir, efile)
                eexists = bool(efile and os.path.exists(epath))
                d["export_exists"] = eexists
                d["export_version"] = int(os.path.getmtime(epath) * 1000) if eexists else 0
                d["export_stale"] = (not eexists) or (c.get("export_sig") != _export_sig(c))
                d["can_reset"] = bool(c.get("export_spec"))
                clips.append(d)
            return {"active": True, "vod": os.path.basename(self.vod), "vod_duration": self.vod_duration,
                    "out_dir": self.out_dir, "ai_available": bool(util.get_api_key()), "clips": clips}

    def normalize_after_segments(self, c):
        """Keep element/chat seg_index valid after segments change; drop stale
        single-segment word fixes once a clip becomes multi-part."""
        n = max(1, len(c.get("segments", [])))
        for el in (c.get("elements") or []):
            el["seg_index"] = max(0, min(int(el.get("seg_index", 0) or 0), n - 1))
        if c.get("chat"):
            c["chat"]["seg_index"] = max(0, min(int(c["chat"].get("seg_index", 0) or 0), n - 1))
        if n > 1 and c.get("caption_words"):
            c["caption_words"] = []

    def validate_segments(self, segs):
        if not isinstance(segs, list) or not (1 <= len(segs) <= 16):
            return None
        clean = []
        for s in segs:
            if not isinstance(s, dict) or not (_finite(s.get("start")) and _finite(s.get("end"))):
                return None
            a, b = float(s["start"]), float(s["end"])
            if not (0 <= a < b <= self.vod_duration + 0.5) or (b - a) > 900:
                return None
            seg = {"start": round(a, 2), "end": round(b, 2)}
            if s.get("mute"):
                seg["mute"] = True              # per-part audio off (renders silent)
            clean.append(seg)
        return clean

    # ---- render jobs (serialized) ----
    def start_render(self, cid: str, kind: str = "base") -> str:
        with self.lock:
            # supersede any in-flight/queued render of the SAME clip+kind: cancel it so
            # only the most recent request runs (no waiting on a stale render).
            key = (cid, kind)
            prev = self.latest.get(key)
            if prev and self.jobs.get(prev, {}).get("status") in ("queued", "rendering"):
                ev = self.cancels.get(prev)
                if ev:
                    ev.set()
                self.jobs[prev]["superseded"] = True
            self.job_seq += 1
            jid = f"j{self.job_seq}"
            self.cancels[jid] = threading.Event()
            self.latest[key] = jid
            self.jobs[jid] = {"clip": cid, "kind": kind, "status": "queued", "error": "", "progress": 0}
            c = self.find(cid)
            if c:
                c["export_status" if kind == "export" else "render_status"] = "queued"
        threading.Thread(target=self._run_render, args=(jid, cid, kind), daemon=True).start()
        return jid

    def _run_render(self, jid: str, cid: str, kind: str = "base"):
        export = (kind == "export")
        statusf = "export_status" if export else "render_status"
        errf = "export_error" if export else "render_error"
        with self.lock:
            cancel = self.cancels.get(jid)
        def _should_cancel():
            return cancel is not None and cancel.is_set()
        with self.render_lock:
            # superseded while waiting in the queue -> don't render at all
            if _should_cancel():
                with self.lock:
                    self.jobs[jid].update(status="cancelled", error="")
                    self.cancels.pop(jid, None)
                return
            with self.lock:
                c = self.find(cid)
                if not c:
                    self.jobs[jid].update(status="error", error="clip not found")
                    return
                c[statusf] = "rendering"
                spec = proj.spec_from_dict(c, self.cfg)
                sig = _export_sig(c)
                # snapshot the spec at render START so edits made DURING the render
                # don't corrupt the "downloaded version" we can later reset to
                snap = {k: copy.deepcopy(c.get(k)) for k in EXPORT_FIELDS} if export else None
                self.save_locked()
                self.jobs[jid].update(status="rendering", progress=0)
            fname = f"{spec.id}.export.mp4" if export else f"{spec.id}.mp4"
            out = os.path.join(self.out_dir, fname)
            # render to a temp file and swap it in atomically only on success, so a killed /
            # cancelled render can never leave a partial file where the dashboard would serve it
            tmp = os.path.join(self.out_dir, f"{spec.id}{'.export' if export else ''}.rendering.mp4")
            _last = [-1]
            def _pcb(frac):                       # progress is best-effort; no lock (never stalls ffmpeg's stdout)
                p = max(0, min(99, int(frac * 100)))
                if p == _last[0]:
                    return
                _last[0] = p
                j = self.jobs.get(jid)
                if j:
                    j["progress"] = p
            try:
                render.render_spec(self.cfg, self.vod, spec, self.transcript, self.watermark,
                                   tmp, include_overlays=export, progress_cb=_pcb,
                                   should_cancel=_should_cancel)
                for _attempt in range(12):        # atomic swap (retry: the player may briefly hold the file)
                    try:
                        os.replace(tmp, out)
                        break
                    except OSError:
                        if _attempt == 11:
                            raise
                        threading.Event().wait(0.25)
                status, err = "done", ""
                if export:                        # replace the clean 'Ready to post' copy in place
                    from . import jobs as jobsmod
                    jobsmod.publish_clip(self.out_dir, cid, out)
            except render.RenderCancelled:
                status, err = "cancelled", ""
                log(f"[server] {kind} render cancelled (superseded) for {cid}")
            except Exception as e:  # noqa: BLE001
                status, err = "error", str(e)
                log(f"[server] {kind} render error {cid}: {e}")
            if status != "done":
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
            with self.lock:
                c = self.find(cid)
                if c:
                    c[statusf] = status
                    # a cancelled render leaves file/needs_render untouched (a newer render is taking over)
                    if status != "cancelled":
                        c[errf] = err
                        if status == "done":
                            if export:
                                c["export_file"] = fname
                                c["export_sig"] = sig
                                c["export_spec"] = snap   # captured at render start (the version downloaded)
                            else:
                                c["file"] = fname
                                c["needs_render"] = False
                    self.save_locked()
                self.jobs[jid].update(status=status, error=err, progress=100 if status == "done" else _last[0])
                self.cancels.pop(jid, None)
                if self.latest.get((cid, kind)) == jid:
                    self.latest.pop((cid, kind), None)
            if export and status == "done":                # batch-finish: post this clip now its export is ready
                self._fire_pending_post(cid)

    # ---- long-form (isolated from the clip pipeline; reuses the job/cancel infra) ----
    def lf_manifest(self) -> dict:
        p = os.path.join(self.lf_dir, "longform.json")
        try:
            return util.read_json(p)
        except Exception:  # noqa: BLE001
            return {"vod": os.path.basename(self.vod), "segments": []}

    def plan_longform(self) -> list:
        wav = transcribe.wav_path(self.vod)
        segs = longform.plan(self.cfg, self.transcript, wav, self.vod_duration)   # heavy; outside the lock
        with self.lock:                                                           # serialize manifest writes
            util.ensure_dirs(self.lf_dir)
            for s in segs:
                longform.write_package(self.cfg, self.transcript, s, self.lf_dir)
            longform.write_manifest(self.lf_dir, self.vod, segs)
            longform.write_md(self.lf_dir, self.vod, segs)
        return [s.to_dict() for s in segs]

    def start_longform_render(self, idx: int) -> str:
        with self.lock:
            key = ("lf", idx)
            prev = self.latest.get(key)
            if prev and self.jobs.get(prev, {}).get("status") in ("queued", "rendering"):
                ev = self.cancels.get(prev)
                if ev:
                    ev.set()
                self.jobs[prev]["superseded"] = True
            self.job_seq += 1
            jid = f"j{self.job_seq}"
            self.cancels[jid] = threading.Event()
            self.latest[key] = jid
            self.jobs[jid] = {"clip": f"lf{idx}", "kind": "longform", "status": "queued",
                              "error": "", "progress": 0}
        threading.Thread(target=self._run_longform, args=(jid, idx), daemon=True).start()
        return jid

    def _run_longform(self, jid: str, idx: int):
        with self.lock:
            cancel = self.cancels.get(jid)
        def _sc():
            return cancel is not None and cancel.is_set()
        with self.render_lock:
            if _sc():
                with self.lock:
                    self.jobs[jid].update(status="cancelled", error="")
                    self.cancels.pop(jid, None)
                return
            with self.lock:
                seg_d = next((s for s in self.lf_manifest().get("segments", []) if s["idx"] == idx), None)
            if not seg_d:
                with self.lock:
                    self.jobs[jid].update(status="error", error="no such long-form segment")
                return
            with self.lock:
                self.jobs[jid].update(status="rendering", progress=0)
            seg = longform.LongSeg(
                idx=seg_d["idx"], start=seg_d["start"], end=seg_d["end"], file=seg_d["file"],
                score=seg_d.get("score", 0), title=seg_d.get("title", ""),
                description=seg_d.get("description", ""), tags=seg_d.get("tags", []),
                game=seg_d.get("game"), keywords=seg_d.get("keywords", []),
                chapters=seg_d.get("chapters", []))
            util.ensure_dirs(self.lf_dir)
            out = os.path.join(self.lf_dir, seg.file)
            tmp = os.path.join(self.lf_dir, os.path.splitext(seg.file)[0] + ".rendering.mp4")
            _last = [-1]
            def _pcb(frac):
                p = max(0, min(99, int(frac * 100)))
                if p == _last[0]:
                    return
                _last[0] = p
                j = self.jobs.get(jid)
                if j:
                    j["progress"] = p
            wm = self.watermark if self.cfg.lf_watermark else ""
            try:
                longform.render_segment(self.cfg, self.vod, seg, wm, tmp, progress_cb=_pcb, should_cancel=_sc)
                for _attempt in range(4):          # atomic swap (retry: the player may briefly hold the file)
                    try:
                        os.replace(tmp, out)
                        break
                    except OSError:
                        if _attempt == 3:
                            raise
                        threading.Event().wait(0.2)
                with self.lock:
                    longform.write_package(self.cfg, self.transcript, seg, self.lf_dir)
                status, err = "done", ""
            except render.RenderCancelled:
                status, err = "cancelled", ""
                log(f"[server] longform render cancelled (superseded) seg{idx}")
            except Exception as e:  # noqa: BLE001
                status, err = "error", str(e)
                log(f"[server] longform render error seg{idx}: {e}")
            if status != "done":
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
            with self.lock:
                self.jobs[jid].update(status=status, error=err, progress=100 if status == "done" else _last[0])
                self.cancels.pop(jid, None)
                if self.latest.get(("lf", idx)) == jid:
                    self.latest.pop(("lf", idx), None)


STATE: State | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        self._started = False

    # ---------- response helpers ----------
    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._started = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self._safe_write(body)

    def _safe_write(self, data):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return {}
        if not n:
            return {}
        raw = self.rfile.read(n)
        return json.loads(raw, parse_constant=_no_nan)

    # ---------- routing ----------
    def do_GET(self):
        self._started = False
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                return self._file(os.path.join(CODE_DIR, "dashboard.html"), "text/html; charset=utf-8")
            if p in ("/icon.png", "/apple-touch-icon.png"):
                return self._file(os.path.join(config.LOCAL_ROOT, "ClipForge.png"), "image/png")
            if p == "/favicon.ico":
                return self._file(os.path.join(config.LOCAL_ROOT, "ClipForge.ico"), "image/x-icon")
            if p == "/api/project":
                return self._json(STATE.public_project() if STATE.active else {"active": False})
            if p == "/api/library":
                return self._json(STATE.library())
            if p.startswith("/api/job/"):
                with STATE.lock:
                    return self._json(STATE.jobs.get(p.rsplit("/", 1)[-1], {"status": "unknown"}))
            if p.startswith("/api/clip/") and p.endswith("/words"):
                return self._clip_words(p.split("/")[3])
            if p.startswith("/clips/"):
                return self._serve_video(os.path.join(STATE.out_dir, os.path.basename(p)))
            if p == "/vod":
                return self._serve_video(STATE.vod)
            if p == "/api/review":                       # preview any library VOD before clipping it
                vp = STATE.review_vod((q.get("path") or [""])[0])
                return self._serve_video(vp) if vp else self._send(404, "not found", "text/plain")
            if p == "/api/review_strip":                  # filmstrip 'scene bar' across the whole VOD
                vp = STATE.review_vod((q.get("path") or [""])[0])
                if not vp:
                    return self._send(404, "not found", "text/plain")
                from . import jobs as jobsmod
                sp = jobsmod.review_strip(STATE.cfg, vp)
                return self._file(sp, "image/jpeg") if sp else self._send(404, "no strip", "text/plain")
            if p == "/longform":
                return self._file(os.path.join(CODE_DIR, "longform.html"), "text/html; charset=utf-8")
            if p == "/api/longform":
                return self._json(self._lf_public())
            if p.startswith("/lf/"):
                return self._serve_lf(os.path.basename(p))
            if p.startswith("/emoji/"):
                return self._file(os.path.join(config.LOCAL_ROOT, "emoji", os.path.basename(p)), "image/png")
            if p == "/api/preview/chat":
                return self._preview_chat(q)
            if p == "/api/frame":
                return self._frame(q)
            if p == "/api/base_frame":
                return self._base_frame(q)
            if p == "/api/thumb":
                return self._thumb(q)
            if p == "/api/vod_frame":
                return self._vod_frame(q)
            if p == "/api/element_preview":
                return self._element_preview(q)
            if p == "/api/posting/state":
                from . import posting
                return self._json(posting.init(STATE.cfg).state())
            if p == "/oauth2/youtube/callback":
                return self._oauth_cb("youtube", q)
            if p == "/oauth2/tiktok/callback":
                return self._oauth_cb("tiktok", q)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        self._started = False
        u = urlparse(self.path)
        p = u.path
        try:
            body = self._body()
            if p == "/api/key":
                k = (body.get("key") or "").strip()
                if not k:
                    return self._json({"ok": False, "error": "empty key"}, 400)
                util.set_api_key(k)
                return self._json({"ok": True, "ai_available": bool(util.get_api_key())})
            if p == "/api/new_job":
                vod = body.get("vod") or ""
                if not (vod and os.path.exists(vod)):
                    return self._json({"error": "VOD not found"}, 400)
                return self._json({"job": STATE.start_new_job(vod, body.get("count"))})
            if p == "/api/new_longform":
                vod = body.get("vod") or ""
                if not (vod and os.path.exists(vod)):
                    return self._json({"error": "VOD not found"}, 400)
                return self._json({"job": STATE.start_longform_job(vod)})
            if p == "/api/open_job":
                d = body.get("dir") or ""
                if not os.path.exists(os.path.join(d, "project.json")):
                    return self._json({"error": "no clip batch there"}, 400)
                STATE.activate(d)
                return self._json({"ok": True})
            if p == "/api/delete_job":
                return self._json({"ok": STATE.delete_job(body.get("dir") or "")})
            if p == "/api/delete_vod":
                return self._json({"ok": STATE.delete_vod(body.get("path") or "")})
            if p == "/api/shutdown":
                def _stop():
                    threading.Event().wait(0.3)
                    if _HTTPD is not None:
                        _HTTPD.shutdown()         # returns serve_forever() -> process exits cleanly
                threading.Thread(target=_stop, daemon=True).start()
                return self._json({"ok": True})
            if p == "/api/open_folder":
                return self._json({"ok": STATE.open_ready_folder()})
            if p == "/api/posting/creds":
                from . import posting
                P = posting.init(STATE.cfg)
                P.save_creds(body.get("platform"), body)
                return self._json({"ok": True, "state": P.state()})
            if p == "/api/posting/connect":
                from . import posting
                P = posting.init(STATE.cfg)
                plat = body.get("platform")
                url = P.youtube_auth_url() if plat == "youtube" else P.tiktok_auth_url()
                _open_browser(url)                     # consent opens in the user's real browser
                return self._json({"ok": True, "auth_url": url})
            if p == "/api/posting/disconnect":
                from . import posting
                posting.init(STATE.cfg).disconnect(body.get("platform"))
                return self._json({"ok": True, "state": posting.init(STATE.cfg).state()})
            if p == "/api/posting/enqueue":
                return self._enqueue_posts(body)
            if p == "/api/posting/cancel":
                from . import posting
                return self._json({"ok": posting.init(STATE.cfg).cancel(body.get("id"))})
            if p == "/api/render_approved":
                return self._render_approved()
            if p == "/api/finish/post_approved":
                if not STATE.active:
                    return self._json({"error": "Open a clip batch first."}, 400)
                try:
                    when = int(float(body.get("when") or 0))
                except (TypeError, ValueError):
                    when = 0
                r = STATE.finish_post_approved(body.get("platforms"), when, body.get("privacy") or "public")
                return self._json(r, 400 if r.get("error") else 200)
            if p == "/api/finish/delete_rejected":
                if not STATE.active:
                    return self._json({"error": "Open a clip batch first."}, 400)
                return self._json(STATE.delete_rejected())
            if p == "/api/longform/plan":
                return self._json({"segments": STATE.plan_longform()})
            if p.startswith("/api/longform/") and p.endswith("/render"):
                part = p.split("/")[3]
                if not part.isdigit():
                    return self._json({"error": "bad segment id"}, 400)
                return self._json({"job": STATE.start_longform_render(int(part))})
            if p == "/api/ai_titles":
                return self._ai_titles()
            if p.startswith("/api/clip/") and p.endswith("/render"):
                return self._json({"job": STATE.start_render(p.split("/")[3], "base")})
            if p.startswith("/api/clip/") and p.endswith("/export"):
                return self._json({"job": STATE.start_render(p.split("/")[3], "export")})
            if p.startswith("/api/clip/") and p.endswith("/reset"):
                return self._reset_clip(p.split("/")[3])
            if p.startswith("/api/clip/") and p.endswith("/clean_captions"):
                return self._clean_captions(p.split("/")[3])
            if p.startswith("/api/clip/") and p.endswith("/ai"):
                return self._ai(p.split("/")[3], body)
            if p.startswith("/api/clip/"):
                return self._update(p.split("/")[3], body)
        except json.JSONDecodeError:
            return self._json({"error": "bad JSON body"}, 400)
        except Exception as e:  # noqa: BLE001
            return self._fail(e)
        return self._send(404, "not found", "text/plain")

    def _fail(self, e):
        log(f"[server] {type(e).__name__}: {e}")
        if not self._started:
            self._json({"error": str(e)}, 500)

    # ---------- API handlers ----------
    def _update(self, cid, body):
        with STATE.lock:
            c = STATE.find(cid)
            if not c:
                return self._json({"error": "no such clip"}, 404)
            old_segs = c.get("segments")
            if "segments" in body:
                clean = STATE.validate_segments(body["segments"])
                if clean is None:
                    return self._json({"error": "invalid segments"}, 400)
                body["segments"] = clean
            if "chat" in body and isinstance(body["chat"], dict):
                body["chat"] = {**(c.get("chat") or proj.chat_default(STATE.cfg)), **body["chat"]}
            for k, v in body.items():
                if k in EDITABLE:
                    c[k] = v
            # timing changed -> fixed caption words are timestamped to the OLD window and now
            # mismatch; drop them so captions regenerate from a fresh per-clip transcription
            if "segments" in body and body["segments"] != old_segs and not body.get("caption_words"):
                c["caption_words"] = []
            if "segments" in body or "elements" in body or "chat" in body:
                STATE.normalize_after_segments(c)
            if EDITABLE & set(body.keys()) - {"approved", "metadata", "notes"}:
                c["needs_render"] = True
            STATE.save_locked()
            snapshot = copy.deepcopy(c)
        return self._json({"ok": True, "clip": snapshot})

    def _clean_captions(self, cid):
        from . import ai_edit
        with STATE.lock:
            c = STATE.find(cid)
            if not c:
                return self._json({"error": "no clip"}, 404)
            segs = c.get("segments", [])
            if len(segs) > 1:
                return self._json({"error": "Caption cleanup works on single-part clips only."}, 400)
            existing = c.get("caption_words")
            seg0 = (float(segs[0]["start"]), float(segs[0]["end"]))
        words = existing or transcribe.clip_words(STATE.cfg, transcribe.wav_path(STATE.vod), *seg0)
        cleaned = ai_edit.clean_captions(STATE.cfg, words)   # slow AI call, outside the lock
        if not cleaned:
            return self._json({"error": "no captions to clean"}, 400)
        with STATE.lock:
            c = STATE.find(cid)
            if c:
                c["caption_words"] = cleaned
                c["captions_enabled"] = True
                c["needs_render"] = True
                STATE.save_locked()
        return self._json({"ok": True, "count": len(cleaned), "words": cleaned})

    def _ai(self, cid, body):
        from . import ai_edit
        with STATE.lock:
            c = STATE.find(cid)
            if not c:
                return self._json({"error": "no such clip"}, 404)
            spec = proj.spec_from_dict(c, STATE.cfg)
        changes = ai_edit.edit(STATE.cfg, STATE.vod_duration, spec, STATE.transcript,
                               body.get("instruction", ""))
        with STATE.lock:
            c = STATE.find(cid)
            if "segments" in changes:
                clean = STATE.validate_segments(changes["segments"])
                if clean:
                    c["segments"] = clean
                changes.pop("segments", None)
            for k, v in changes.items():
                if k == "metadata" and isinstance(v, dict):
                    for plat, md in v.items():
                        c.setdefault("metadata", {}).setdefault(plat, {}).update(md or {})
                elif k == "chat" and isinstance(v, dict):
                    c["chat"] = {**proj.chat_default(STATE.cfg), **(c.get("chat") or {}), **v}
                elif k in EDITABLE:
                    c[k] = v
            c["notes"] = (c.get("notes", "") + "\nAI: " + str(changes.get("note", ""))).strip()
            STATE.normalize_after_segments(c)
            c["needs_render"] = True
            STATE.save_locked()
            snapshot = copy.deepcopy(c)
        job = STATE.start_render(cid)
        return self._json({"ok": True, "changes": changes, "clip": snapshot, "job": job})

    def _ai_titles(self):
        from . import meta as _meta, rerank
        with STATE.lock:
            snap = [{"id": c["id"], "keywords": c.get("keywords", []),
                     "profanity": c.get("profanity", False), "transcript": c.get("transcript", "")}
                    for c in STATE.project["clips"]]
        base = rerank.write_titles(STATE.cfg, snap)
        with STATE.lock:
            n = 0
            for c in STATE.project["clips"]:
                b = base.get(c["id"])
                if not b:
                    continue
                tags = _meta.coerce_hashtags(b.get("hashtags"))
                short_tags = (["#Shorts"] + [t for t in tags if t.lower() != "#shorts"])[:5]
                c["metadata"] = {
                    "tiktok": {"title": b["title"], "caption": b["caption"], "hashtags": tags[:4]},
                    "shorts": {"title": b["title"], "caption": b["caption"], "hashtags": short_tags},
                }
                n += 1
            STATE.save_locked()
        return self._json({"ok": True, "updated": n})

    # ---------- posting (YouTube Shorts + TikTok) ----------
    def _oauth_cb(self, platform, q):
        """OAuth redirect target: capture the code, finish the token exchange, show a tidy page."""
        from . import posting
        P = posting.init(STATE.cfg)
        try:
            err = (q.get("error") or [None])[0]
            if err:
                raise RuntimeError(err)
            code = (q.get("code") or [None])[0]
            state = (q.get("state") or [None])[0]
            if platform == "youtube":
                P.youtube_callback(code, state)
            else:
                P.tiktok_callback(code, state)
            head, msg = "✓ Connected", "You can close this tab and return to ClipForge."
        except Exception as e:  # noqa: BLE001
            head, msg = "Connection failed", str(e)
        page = ("<!doctype html><meta charset=utf-8><title>ClipForge</title>"
                "<body style='font-family:Segoe UI,system-ui,sans-serif;background:#0d0b14;color:#f6f3ec;"
                "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                f"<div style='text-align:center;max-width:420px;padding:20px'><h2 style='color:#E7C58A;margin:0 0 8px'>{head}</h2>"
                f"<p style='opacity:.8;line-height:1.5'>{_html.escape(msg)}</p></div>")
        return self._send(200, page, "text/html; charset=utf-8")

    def _enqueue_posts(self, body):
        """Resolve a clip's finished file + per-platform metadata and queue it for upload now/later."""
        from . import posting
        if not STATE.active:
            return self._json({"error": "Open a clip batch first."}, 400)
        cid = body.get("clip") or ""
        platforms = [p for p in (body.get("platforms") or []) if p in ("youtube", "tiktok")]
        if not platforms:
            return self._json({"error": "Pick at least one platform."}, 400)
        try:
            when = int(float(body.get("when") or 0))
        except (TypeError, ValueError):
            when = 0
        with STATE.lock:
            if not STATE.find(cid):
                return self._json({"error": "no such clip"}, 404)
        items = STATE._post_items(cid, platforms, when, body.get("privacy") or "public")
        if not items:
            return self._json({"error": "This clip isn't downloaded yet — click ⬇ Download first."}, 400)
        P = posting.init(STATE.cfg)
        ids = P.enqueue(items)
        return self._json({"ok": True, "ids": ids, "state": P.state()})

    def _render_approved(self):
        jobs = []
        with STATE.lock:
            ids = [c["id"] for c in STATE.project["clips"]
                   if c.get("approved") is True and
                   (_export_sig(c) != c.get("export_sig") or
                    not (c.get("export_file") and
                         os.path.exists(os.path.join(STATE.out_dir, c["export_file"]))))]
        for cid in ids:
            jobs.append(STATE.start_render(cid, "export"))
        return self._json({"queued": len(jobs), "jobs": jobs, "ids": ids})

    # ---------- previews ----------
    def _clamp_t(self, q, default):
        t = q.get("t", [None])[0]
        t = float(t) if _finite(t) else default
        return max(0.0, min(t, STATE.vod_duration))

    def _preview_chat(self, q):
        from . import elements
        cid = q.get("clip", [""])[0]
        with STATE.lock:
            c = STATE.find(cid)
            ch = dict((c.get("chat") if c else None) or {})
            seg0 = (c["segments"][0]["start"] if c and c.get("segments") else 0.0)
        # preview goes through the SAME inset (rounded + gold border + shadow) as the render
        if ch.get("src") and ch.get("src") != "auto" and ch.get("image") and os.path.exists(ch["image"]):
            out = os.path.join(config.WORK_DIR, "preview", f"chatimg_{cid}.png")
            elements.chat_inset_png(STATE.cfg, ch["image"], out)
            return self._file(out, "image/png")
        rect = render._clamp_rect(ch.get("rect", list(STATE.cfg.chat_rect)))
        t = self._clamp_t(q, seg0)
        raw = os.path.join(config.WORK_DIR, "preview", f"chatraw_{cid}_{int(t)}.png")
        camdetect.grab_region(STATE.cfg, STATE.vod, t, rect, raw)
        out = os.path.join(config.WORK_DIR, "preview", f"chat_{cid}_{int(t)}.png")
        elements.chat_inset_png(STATE.cfg, raw, out)
        return self._file(out, "image/png")

    def _frame(self, q):
        t = self._clamp_t(q, 0.0)
        w = q.get("w", ["480"])[0]
        w = max(120, min(int(w), 1080)) if str(w).isdigit() else 480
        png = os.path.join(config.WORK_DIR, "preview", f"frame_{int(t)}_{w}.jpg")
        util.ensure_dirs(os.path.dirname(png))
        r = subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                            "-ss", f"{t:.3f}", "-i", STATE.vod, "-frames:v", "1",
                            "-vf", f"scale={w}:-1", "-q:v", "4", png],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("frame grab failed: " + r.stderr.strip()[-200:])
        return self._file(png, "image/jpeg")

    def _clip_words(self, cid):
        with STATE.lock:
            c = STATE.find(cid)
            if not c:
                return self._json({"error": "no clip"}, 404)
            segs = c.get("segments", [])
            if len(segs) > 1:
                return self._json({"words": [], "multi": True})  # word-fix is single-part only
            if c.get("caption_words"):
                return self._json({"words": c["caption_words"]})
            seg0 = (float(segs[0]["start"]), float(segs[0]["end"]))
        words = transcribe.clip_words(STATE.cfg, transcribe.wav_path(STATE.vod), *seg0)
        return self._json({"words": words})

    def _base_frame(self, q):
        cid = q.get("clip", [""])[0]
        with STATE.lock:
            c = STATE.find(cid)
            spec = proj.spec_from_dict(c, STATE.cfg) if c else None
        if not spec:
            return self._json({"error": "no such clip"}, 404)
        t = q.get("t", ["0"])[0]
        t = float(t) if _finite(t) else 0.0
        out = os.path.join(config.WORK_DIR, "preview", f"base_{cid}_{int(t)}.jpg")
        render.base_frame(STATE.cfg, STATE.vod, spec, max(0.0, t), STATE.watermark, out)
        return self._file(out, "image/jpeg")

    def _thumb(self, q):
        cid = q.get("clip", [""])[0]
        with STATE.lock:
            c = STATE.find(cid)
            spec = proj.spec_from_dict(c, STATE.cfg) if c else None
            f = c.get("file") if c else None
        path = os.path.join(STATE.out_dir, f) if f else ""
        if not f or not os.path.exists(path):
            return self._send(404, "no clip", "text/plain")
        out = os.path.join(config.WORK_DIR, "preview", f"thumb_{cid}.jpg")
        util.ensure_dirs(os.path.dirname(out))
        # elements that sit at the very start (the "hook") -> show the START frame and composite
        # them onto it, so an element added at 0s actually appears on the clip's thumbnail.
        seg0 = [el for el in (spec.elements or []) if el.get("visible", True)
                and int(el.get("seg_index", 0) or 0) == 0]
        start_els = [el for el in seg0 if float((el.get("timing") or {}).get("start", 0) or 0) <= 0.2]
        t = 0.3 if start_els else 1.5
        frame = os.path.join(config.WORK_DIR, "preview", f"thumbframe_{cid}.png")
        subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", frame], capture_output=True, text=True)
        composited = False
        if start_els and os.path.exists(frame):
            try:
                from PIL import Image
                from . import elements as _el
                img = Image.open(frame).convert("RGBA")
                eldir = os.path.join(config.WORK_DIR, "preview", "thumbel")
                util.ensure_dirs(eldir)
                for el in start_els:
                    en = (el.get("timing") or {}).get("end")
                    if en is not None and float(en) <= t:        # element already gone by t
                        continue
                    res = _el.render_element_png(STATE.cfg, el, os.path.join(eldir, f"{cid}_{el.get('id', 'x')}.png"))
                    if not res:
                        continue
                    p, _w, _h = res
                    g = el.get("geom", {})
                    img.alpha_composite(Image.open(p).convert("RGBA"), (int(g.get("x", 0)), int(g.get("y", 0))))
                W, H = img.size
                img.convert("RGB").resize((200, max(1, round(200 * H / W)))).save(out, quality=82)
                composited = True
            except Exception as e:  # noqa: BLE001
                log(f"[thumb] element composite failed: {e}")
        if not composited:
            subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                            "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", "-vf", "scale=200:-1",
                            "-q:v", "5", out], capture_output=True, text=True)
        return self._file(out, "image/jpeg") if os.path.exists(out) else self._send(404, "x", "text/plain")

    def _reset_clip(self, cid):
        """Restore a clip's editable spec to the snapshot taken at its last download/export."""
        with STATE.lock:
            c = STATE.find(cid)
            if not c:
                return self._json({"error": "no such clip"}, 404)
            snap = c.get("export_spec")
            if not snap:
                return self._json({"error": "no downloaded version to reset to"}, 400)
            for k in EXPORT_FIELDS:
                if k in snap:
                    c[k] = copy.deepcopy(snap[k])
            STATE.normalize_after_segments(c)
            c["needs_render"] = True
            STATE.save_locked()
        return self._json({"ok": True})

    def _lf_public(self):
        man = STATE.lf_manifest()
        for s in man.get("segments", []):
            fp = os.path.join(STATE.lf_dir, s.get("file", "") or "")
            ex = bool(s.get("file") and os.path.exists(fp))
            s["exists"] = ex
            s["version"] = int(os.path.getmtime(fp) * 1000) if ex else 0
        man["vod_duration"] = STATE.vod_duration
        man["planned"] = os.path.exists(os.path.join(STATE.lf_dir, "longform.json"))
        return man

    def _serve_lf(self, name):
        path = os.path.normpath(os.path.join(STATE.lf_dir, os.path.basename(name)))
        root = os.path.normpath(STATE.lf_dir)
        if not (path == root or path.startswith(root + os.sep)) or not os.path.exists(path):
            return self._send(404, "not found", "text/plain")
        if path.lower().endswith(".mp4"):
            return self._serve_video(path)
        return self._file(path, "text/plain; charset=utf-8")

    def _vod_frame(self, q):
        """A fast raw-VOD thumbnail at an absolute VOD time, for live trim-bar scrubbing."""
        t = q.get("t", ["0"])[0]
        t = float(t) if _finite(t) else 0.0
        t = max(0.0, min(t, STATE.vod_duration))
        key = int(round(t * 2))                       # 0.5s cache buckets
        out = os.path.join(config.WORK_DIR, "preview", f"vodframe_{key}.jpg")
        util.ensure_dirs(os.path.dirname(out))
        if not os.path.exists(out):
            subprocess.run([util.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                            "-ss", f"{t:.3f}", "-i", STATE.vod, "-frames:v", "1",
                            "-vf", "scale=256:-1", "-q:v", "5", out],
                           capture_output=True, text=True)
        return self._file(out, "image/jpeg") if os.path.exists(out) else self._send(404, "x", "text/plain")

    def _element_preview(self, q):
        """Render a single element to a transparent PNG for the editor stage (hashed cache)."""
        import hashlib
        from . import elements
        raw = q.get("el", ["{}"])[0]
        try:
            el = json.loads(raw)
        except json.JSONDecodeError:
            return self._json({"error": "bad element"}, 400)
        h = hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]
        png = os.path.join(config.WORK_DIR, "preview", f"el_{h}.png")
        force = el.get("type") == "image"   # source file may change under a stable JSON
        if force or not os.path.exists(png):
            res = elements.render_element_png(STATE.cfg, el, png)
            if not res:
                return self._send(204, b"", "text/plain")
        return self._file(png, "image/png")

    # ---------- static + video ----------
    def _file(self, path, ctype):
        if not os.path.exists(path):
            return self._send(404, "not found", "text/plain")
        with open(path, "rb") as fh:
            data = fh.read()
        return self._send(200, data, ctype, {"Cache-Control": "no-cache"})

    def _serve_video(self, path):
        if not os.path.exists(path):
            return self._send(404, "not found", "text/plain")
        size = os.path.getsize(path)
        start, end = 0, size - 1
        is_range = False
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[6:].partition("-")
                start = int(s) if s else 0
                end = int(e) if e else size - 1
                is_range = True
            except ValueError:
                is_range = False
        if is_range and start >= size:
            self._started = True
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start = max(0, start)
        end = min(end, size - 1)
        # Cap each response so we never read a huge file (e.g. the full VOD) into memory, AND so the
        # file handle is held only for the brief disk read below — NOT during the slow network write.
        # Holding it during the write keeps clipNN.mp4 open while the <video> streams it, which makes a
        # preview re-render's os.replace() fail on Windows ("access is denied"). The browser just
        # requests the next range. (Confirmed root cause of "Preview render failed" on trim edits.)
        _MAX = 32 * 1024 * 1024
        end = min(end, start + _MAX - 1)
        partial = is_range or start > 0 or end < size - 1
        length = end - start + 1
        self._started = True
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if self.command == "HEAD":
            return
        try:                                    # read the range, then CLOSE before the socket write
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read(length)
        except OSError:
            return
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return


class _QuietHTTPServer(ThreadingHTTPServer):
    """A browser (esp. an app window fetching many frames/video ranges) routinely cancels in-flight
    requests, which surfaces as ConnectionReset/BrokenPipe from the stdlib's request read. Those are
    normal — swallow them so the log stays clean; report anything genuinely unexpected."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


_HTTPD = None   # the running server, so /api/shutdown can stop it cleanly


def _open_browser(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


def _find_chromium():
    """Path to Edge or Chrome (for a chrome-less --app window), or None."""
    import shutil
    cands = [shutil.which("msedge"), shutil.which("chrome"),
             os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
             os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
             os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
             os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
             os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def app_window(url: str):
    """Open ClipForge as a chrome-less desktop window (Edge/Chrome --app). Returns the Popen
    (so the launcher can quit when the window closes) or None if no Chromium browser is found.
    A dedicated user-data-dir makes the process independently monitorable + isolated from browsing."""
    exe = _find_chromium()
    if not exe:
        return None
    udd = os.path.join(config.LOCAL_ROOT, "appwindow")
    try:
        return subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={udd}",
                                 "--window-size=1320,900", "--no-first-run", "--no-default-browser-check"])
    except OSError:
        return None


def _set_app_id():
    """Give the process its own Windows taskbar identity (icon + grouping) so ClipForge shows up as
    ClipForge — not grouped under python.exe or Edge."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClipForge.Studio")
        except Exception:  # noqa: BLE001
            pass


def _run_webview(url: str) -> bool:
    """Open ClipForge in a true NATIVE window (pywebview over the Windows WebView2 runtime): no
    browser chrome, no Edge identity in the taskbar, just a ClipForge-branded desktop window. Blocks
    until the user closes it. Returns True if it ran, False if pywebview/WebView2 isn't available (so
    the caller can fall back)."""
    try:
        import webview
    except Exception:  # noqa: BLE001  (pywebview not installed)
        return False
    _set_app_id()
    icon = os.path.join(config.LOCAL_ROOT, "ClipForge.ico")
    try:
        webview.create_window("ClipForge Studio", url, width=1320, height=900, min_size=(900, 640))
        webview.start(icon=icon if os.path.exists(icon) else None)
        return True
    except Exception as e:  # noqa: BLE001  (no WebView2 runtime, GUI init failure, ...)
        log(f"[dashboard] native window unavailable ({e}); falling back.")
        return False


def stop():
    """Stop the running server (called when the app window closes)."""
    if _HTTPD is not None:
        try:
            _HTTPD.shutdown()
        except Exception:  # noqa: BLE001
            pass


def _instance_running(port: int) -> bool:
    """True if something is already listening on the dashboard port (single-instance check).
    (Windows SO_REUSEADDR lets a 2nd bind succeed, so we connect-test instead of catching bind errors.)"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def serve(cfg: config.Config, library_root: str, active_dir: str | None = None, open_browser: bool = True,
          app_mode: bool = False):
    """Run the dashboard. app_mode=True opens it as a real NATIVE desktop window (pywebview/WebView2)
    — no browser chrome, ClipForge's own taskbar icon — and quits the server when that window closes.
    It degrades gracefully: native window -> chrome-less Edge/Chrome --app -> a normal browser tab."""
    global STATE, _HTTPD
    util.ensure_fonts()
    url = f"http://127.0.0.1:{cfg.dash_port}/"
    if _instance_running(cfg.dash_port):
        # already running -> single-instance: surface the existing instance and exit
        log(f"[dashboard] already running -> {url}")
        if app_mode:
            return _run_webview(url) or (app_window(url) is not None) or _open_browser(url)
        if open_browser:
            _open_browser(url)
        return
    try:
        httpd = _QuietHTTPServer(("127.0.0.1", cfg.dash_port), Handler)
    except OSError:
        log(f"[dashboard] port busy -> {url}")
        if open_browser:
            _open_browser(url)
        return
    STATE = State(cfg, library_root, active_dir=active_dir)
    _HTTPD = httpd
    try:
        from . import posting               # start the upload scheduler (fires due posts in the background)
        posting.init(cfg)
    except Exception as e:  # noqa: BLE001  (posting is optional; never block the dashboard)
        log(f"[posting] scheduler not started: {e}")
    where = f"{len(STATE.project['clips'])} clips" if STATE.active else f"home — pick a VOD from {library_root}"
    log(f"[dashboard] {where}  ->  {url}")
    if app_mode:
        return _serve_native(httpd, url)
    if open_browser:
        _open_browser(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log("[dashboard] stopped.")
        httpd.server_close()
        _HTTPD = None


def _shutdown(httpd):
    global _HTTPD
    log("[dashboard] window closed -> stopping.")
    try:
        httpd.shutdown()
    except Exception:  # noqa: BLE001
        pass
    httpd.server_close()
    _HTTPD = None


def _serve_native(httpd, url):
    """Serve in a background thread, then open the dashboard as a desktop window; when the window
    closes the process exits (no orphaned server). Tries a native window first, then a chrome-less
    Edge/Chrome --app window, then a plain browser tab — whichever the machine supports."""
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()                                   # the ThreadingHTTPServer socket is already listening
    if _run_webview(url):                        # 1) native WebView2 window (blocks until closed)
        return _shutdown(httpd)
    proc = app_window(url)                        # 2) chrome-less Edge/Chrome --app window
    if proc is not None:
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        return _shutdown(httpd)
    log("[dashboard] no native window or Edge/Chrome; opening in your browser instead.")
    _open_browser(url)                            # 3) last resort: a normal browser tab
    try:
        t.join()
    except KeyboardInterrupt:
        pass
    _shutdown(httpd)
