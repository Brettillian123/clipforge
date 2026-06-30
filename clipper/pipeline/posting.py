"""Post finished clips straight to YouTube Shorts + TikTok from inside ClipForge.

FREE / local-only design:
  * OAuth uses the app's OWN local server as the loopback redirect
    (http://127.0.0.1:<dash_port>/oauth2/<platform>/callback), so NO public website is
    needed for sign-in. The consent page opens in the user's real browser (platforms block
    embedded webviews), then redirects back to ClipForge, which captures the code.
  * Client credentials, tokens, and the schedule queue all live in posting.json under
    LOCAL_ROOT (same place as the API key — local only, never synced, treated like a password).
  * A daemon scheduler uploads queued clips at their chosen time while ClipForge is running.

Reality (see the research write-up that preceded this build): without passing each platform's
public-posting AUDIT (which needs a public website + privacy policy), posting is SEMI-automated:
  * YouTube  — an UNVERIFIED API project force-locks uploads to PRIVATE regardless of the
               privacyStatus we send. So clips upload to your channel and you flip them to
               Public (or schedule) in YouTube Studio. (An audited project honors the privacy
               we pass, making it fully hands-off.)
  * TikTok   — the free path uploads to your TikTok DRAFTS (the "inbox" endpoint). You open the
               TikTok app, add the caption, and tap Post (you can make it public there).
               Direct API publishing needs the audit.

Everything here degrades gracefully: if the optional Google/requests libraries aren't installed,
the rest of ClipForge is unaffected and the posting actions surface a friendly "pip install" error.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse

from . import config
from .util import log

# Loopback redirect is plain http://127.0.0.1 — tell oauthlib that's fine (it is, for loopback),
# and relax scope-equality (Google may reorder/extend the granted scope list).
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

STORE_PATH = os.path.join(config.LOCAL_ROOT, "posting.json")
_LOCK = threading.RLock()

YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube.readonly"]   # readonly => show which channel
YT_TOKEN_URI = "https://oauth2.googleapis.com/token"
YT_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"

TT_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TT_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
TT_INBOX_INIT = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
TT_STATUS = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
TT_USERINFO = "https://open.tiktokapis.com/v2/user/info/"
TT_SCOPES = "user.info.basic,video.upload"
TT_MAX_BYTES = 64 * 1024 * 1024          # single-chunk FILE_UPLOAD ceiling for the inbox endpoint


# ----------------------------------------------------------------------------- store
def _load() -> dict:
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            d = {}
    except Exception:  # noqa: BLE001  (missing/corrupt -> fresh)
        d = {}
    d.setdefault("youtube", {})
    d.setdefault("tiktok", {})
    d.setdefault("queue", [])
    d.setdefault("seq", 0)
    return d


def _save(d: dict) -> None:
    os.makedirs(config.LOCAL_ROOT, exist_ok=True)
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_PATH)


# ----------------------------------------------------------------------------- Poster
class Poster:
    """Owns credentials, the OAuth handshakes, the uploaders, and the schedule loop."""

    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self._pending_yt: dict = {}        # state -> google flow (in-flight connect)
        self._pending_tt: dict = {}        # state -> pkce code_verifier
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        # recover anything that was mid-upload when the app last closed -> retry it
        with _LOCK:
            d = _load()
            stuck = [it for it in d["queue"] if it.get("status") == "uploading"]
            for it in stuck:
                it["status"] = "queued"
            if stuck:
                _save(d)
                log(f"[posting] requeued {len(stuck)} interrupted upload(s).")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="clipforge-scheduler")
        self._thread.start()
        log("[posting] scheduler started.")

    def _redirect(self, platform: str) -> str:
        return f"http://127.0.0.1:{self.cfg.dash_port}/oauth2/{platform}/callback"

    def _creds(self, platform: str) -> dict:
        with _LOCK:
            d = _load()
            return dict(d.get(platform) or {})

    # -- public state for the UI -------------------------------------------
    def state(self) -> dict:
        with _LOCK:
            d = _load()
        yt, tt = d["youtube"], d["tiktok"]
        q = [{k: it.get(k) for k in ("id", "platform", "clip", "title", "when",
                                     "status", "progress", "error", "result")}
             for it in d["queue"][-60:]]
        q.reverse()                         # newest first
        return {
            "youtube": {"configured": bool(yt.get("client_id") and yt.get("client_secret")),
                        "connected": bool(yt.get("refresh_token")),
                        "channel": yt.get("channel", "")},
            "tiktok": {"configured": bool(tt.get("client_key") and tt.get("client_secret")),
                       "connected": bool(tt.get("access_token")),
                       "handle": tt.get("handle", "")},
            "queue": q,
            "redirect": {"youtube": self._redirect("youtube"), "tiktok": self._redirect("tiktok")},
        }

    def save_creds(self, platform: str, body: dict) -> None:
        with _LOCK:
            d = _load()
            if platform == "youtube":
                d["youtube"]["client_id"] = (body.get("client_id") or "").strip()
                d["youtube"]["client_secret"] = (body.get("client_secret") or "").strip()
            elif platform == "tiktok":
                d["tiktok"]["client_key"] = (body.get("client_key") or "").strip()
                d["tiktok"]["client_secret"] = (body.get("client_secret") or "").strip()
            else:
                raise RuntimeError("unknown platform")
            _save(d)

    def disconnect(self, platform: str) -> None:
        """Forget tokens (keep the client id/secret so reconnecting is one click)."""
        keep = ("client_id", "client_secret") if platform == "youtube" else ("client_key", "client_secret")
        with _LOCK:
            d = _load()
            cur = d.get(platform) or {}
            d[platform] = {k: cur[k] for k in keep if k in cur}
            _save(d)

    # =======================================================================
    # YouTube
    # =======================================================================
    def youtube_auth_url(self) -> str:
        cr = self._creds("youtube")
        if not (cr.get("client_id") and cr.get("client_secret")):
            raise RuntimeError("Add your Google client ID + secret first (Connections ▸ YouTube).")
        try:
            from google_auth_oauthlib.flow import Flow
        except ImportError as e:
            raise RuntimeError("Google libraries missing. Run: pip install -r requirements.txt") from e
        redirect = self._redirect("youtube")
        flow = Flow.from_client_config(
            {"web": {"client_id": cr["client_id"], "client_secret": cr["client_secret"],
                     "auth_uri": YT_AUTH_URI, "token_uri": YT_TOKEN_URI,
                     "redirect_uris": [redirect]}},
            scopes=YT_SCOPES, redirect_uri=redirect)
        url, state = flow.authorization_url(access_type="offline", prompt="consent")
        self._pending_yt[state] = flow
        return url

    def youtube_callback(self, code: str, state: str) -> None:
        flow = self._pending_yt.pop(state, None)
        if not flow:
            raise RuntimeError("Sign-in expired or came back out of order — click Connect again.")
        if not code:
            raise RuntimeError("No authorization code returned.")
        flow.fetch_token(code=code)
        creds = flow.credentials
        if not creds.refresh_token:
            raise RuntimeError("Google didn't return a refresh token. Disconnect this app at "
                               "myaccount.google.com/permissions, then Connect again.")
        channel = self._yt_channel_title(creds)
        with _LOCK:
            d = _load()
            d["youtube"].update({
                "refresh_token": creds.refresh_token,
                "token": creds.token,
                "expiry": creds.expiry.isoformat() if getattr(creds, "expiry", None) else None,
                "scopes": list(creds.scopes or YT_SCOPES),
                "channel": channel,
            })
            _save(d)
        log(f"[posting] YouTube connected: {channel or '(channel name unavailable)'}")

    def _yt_channel_title(self, creds) -> str:
        try:
            from googleapiclient.discovery import build
            svc = build("youtube", "v3", credentials=creds, cache_discovery=False)
            r = svc.channels().list(part="snippet", mine=True).execute()
            items = r.get("items", [])
            return items[0]["snippet"]["title"] if items else ""
        except Exception as e:  # noqa: BLE001
            log(f"[posting] yt channel lookup failed: {e}")
            return ""

    def _yt_credentials(self):
        from google.oauth2.credentials import Credentials
        with _LOCK:
            yt = dict(_load()["youtube"])
        if not yt.get("refresh_token"):
            raise RuntimeError("YouTube isn't connected yet.")
        return Credentials(
            token=yt.get("token"), refresh_token=yt["refresh_token"],
            token_uri=YT_TOKEN_URI, client_id=yt.get("client_id"),
            client_secret=yt.get("client_secret"), scopes=yt.get("scopes") or YT_SCOPES)

    def _yt_persist_token(self, creds) -> None:
        with _LOCK:
            d = _load()
            d["youtube"]["token"] = creds.token
            try:
                d["youtube"]["expiry"] = creds.expiry.isoformat() if creds.expiry else None
            except Exception:  # noqa: BLE001
                pass
            _save(d)

    def youtube_upload(self, item: dict, progress_cb=None) -> dict:
        try:
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
        except ImportError as e:
            raise RuntimeError("Google libraries missing. Run: pip install -r requirements.txt") from e
        creds = self._yt_credentials()
        svc = build("youtube", "v3", credentials=creds, cache_discovery=False)

        tags = [str(t).lstrip("#") for t in (item.get("hashtags") or []) if str(t).strip()]
        desc = (item.get("description") or "").strip()
        if tags:
            desc = (desc + "\n\n" + " ".join("#" + t for t in tags)).strip()
        if "#shorts" not in desc.lower():               # nudge YouTube to classify it as a Short
            desc = (desc + "\n\n#Shorts").strip()
        body = {
            "snippet": {"title": (item.get("title") or "Clip").strip()[:100],
                        "description": desc[:4900], "tags": tags[:15], "categoryId": "20"},  # 20 = Gaming
            "status": {"privacyStatus": item.get("privacy") or "private",
                       "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(item["mp4"], chunksize=4 * 1024 * 1024, resumable=True, mimetype="video/mp4")
        req = svc.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            status, resp = req.next_chunk()
            if status and progress_cb:
                progress_cb(status.progress())
        vid = resp["id"]
        actual_privacy = (resp.get("status") or {}).get("privacyStatus", body["status"]["privacyStatus"])

        cover = item.get("cover")
        if cover and os.path.exists(cover):
            try:
                svc.thumbnails().set(videoId=vid,
                                     media_body=MediaFileUpload(cover, mimetype="image/jpeg")).execute()
            except Exception as e:  # noqa: BLE001  (custom thumbnails need a verified channel)
                log(f"[posting] yt thumbnail skipped: {e}")
        self._yt_persist_token(creds)
        return {"video_id": vid, "url": f"https://youtu.be/{vid}", "privacy": actual_privacy}

    # =======================================================================
    # TikTok
    # =======================================================================
    def tiktok_auth_url(self) -> str:
        cr = self._creds("tiktok")
        if not (cr.get("client_key") and cr.get("client_secret")):
            raise RuntimeError("Add your TikTok client key + secret first (Connections ▸ TikTok).")
        verifier = secrets.token_hex(48)                # 96 chars, in PKCE's 43..128 range
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        self._pending_tt[state] = verifier
        q = {"client_key": cr["client_key"], "scope": TT_SCOPES, "response_type": "code",
             "redirect_uri": self._redirect("tiktok"), "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        return TT_AUTH + "?" + urllib.parse.urlencode(q)

    def tiktok_callback(self, code: str, state: str) -> None:
        import requests
        verifier = self._pending_tt.pop(state, None)
        if not verifier:
            raise RuntimeError("Sign-in expired or came back out of order — click Connect again.")
        if not code:
            raise RuntimeError("No authorization code returned.")
        cr = self._creds("tiktok")
        r = requests.post(TT_TOKEN, timeout=30,
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          data={"client_key": cr["client_key"], "client_secret": cr["client_secret"],
                                "code": code, "grant_type": "authorization_code",
                                "redirect_uri": self._redirect("tiktok"), "code_verifier": verifier})
        j = r.json()
        if "access_token" not in j:
            raise RuntimeError(f"TikTok token error: {j.get('error_description') or j}")
        now = int(time.time())
        with _LOCK:
            d = _load()
            d["tiktok"].update({
                "access_token": j["access_token"], "refresh_token": j.get("refresh_token"),
                "open_id": j.get("open_id"), "scope": j.get("scope"),
                "expires_at": now + int(j.get("expires_in", 0) or 0),
                "refresh_expires_at": now + int(j.get("refresh_expires_in", 0) or 0),
            })
            _save(d)
        self._tiktok_fetch_handle()
        log("[posting] TikTok connected.")

    def _tiktok_token(self) -> str:
        import requests
        with _LOCK:
            tt = dict(_load()["tiktok"])
        if not tt.get("access_token"):
            raise RuntimeError("TikTok isn't connected yet.")
        if int(tt.get("expires_at", 0)) - 60 > int(time.time()):
            return tt["access_token"]
        cr = self._creds("tiktok")
        r = requests.post(TT_TOKEN, timeout=30,
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          data={"client_key": cr["client_key"], "client_secret": cr["client_secret"],
                                "grant_type": "refresh_token", "refresh_token": tt.get("refresh_token")})
        j = r.json()
        if "access_token" not in j:
            raise RuntimeError(f"TikTok session expired and refresh failed — reconnect TikTok. ({j.get('error_description') or j})")
        now = int(time.time())
        with _LOCK:
            d = _load()
            d["tiktok"].update({
                "access_token": j["access_token"],
                "refresh_token": j.get("refresh_token", tt.get("refresh_token")),
                "expires_at": now + int(j.get("expires_in", 0) or 0),
            })
            _save(d)
        return j["access_token"]

    def _tiktok_fetch_handle(self) -> None:
        try:
            import requests
            tok = self._tiktok_token()
            r = requests.get(TT_USERINFO, params={"fields": "display_name"}, timeout=20,
                             headers={"Authorization": f"Bearer {tok}"})
            user = ((r.json() or {}).get("data") or {}).get("user") or {}
            handle = user.get("display_name") or ""
            if handle:
                with _LOCK:
                    d = _load()
                    d["tiktok"]["handle"] = handle
                    _save(d)
        except Exception as e:  # noqa: BLE001
            log(f"[posting] tiktok handle lookup failed: {e}")

    def tiktok_upload(self, item: dict, progress_cb=None) -> dict:
        import requests
        tok = self._tiktok_token()
        path = item["mp4"]
        size = os.path.getsize(path)
        if size > TT_MAX_BYTES:
            raise RuntimeError(f"Clip is {size // (1024 * 1024)} MB — over TikTok's {TT_MAX_BYTES // (1024 * 1024)} MB "
                               "single-upload limit. Trim it shorter.")
        init = requests.post(TT_INBOX_INIT, timeout=60,
                             headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                             json={"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                                   "chunk_size": size, "total_chunk_count": 1}})
        j = init.json()
        err = (j.get("error") or {})
        if err and err.get("code") not in (None, "ok"):
            raise RuntimeError(f"TikTok init error: {err.get('message') or err}")
        data = j.get("data") or {}
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not upload_url:
            raise RuntimeError(f"TikTok init returned no upload URL: {j}")
        with open(path, "rb") as fh:
            blob = fh.read()
        put = requests.put(upload_url, data=blob, timeout=900,
                           headers={"Content-Type": "video/mp4", "Content-Length": str(size),
                                    "Content-Range": f"bytes 0-{size - 1}/{size}"})
        if put.status_code not in (200, 201, 204):
            raise RuntimeError(f"TikTok upload failed ({put.status_code}): {put.text[:200]}")
        if progress_cb:
            progress_cb(1.0)
        return {"publish_id": publish_id, "where": "TikTok drafts"}

    # =======================================================================
    # Queue + scheduler
    # =======================================================================
    def enqueue(self, items: list) -> list:
        """Add fully-resolved upload items (self-contained: abs mp4 path + metadata captured now,
        so a scheduled post doesn't depend on which project is open when it fires)."""
        out = []
        now = int(time.time())
        with _LOCK:
            d = _load()
            for it in items:
                d["seq"] += 1
                jid = f"up{d['seq']}"
                rec = {"id": jid, "status": "queued", "progress": 0, "error": "", "result": None,
                       "created": now, "updated": now, **it}
                d["queue"].append(rec)
                out.append(jid)
            # keep the journal from growing forever: drop the oldest finished items past 100
            if len(d["queue"]) > 100:
                done = [x for x in d["queue"] if x.get("status") in ("uploaded", "error")]
                drop = set(x["id"] for x in done[:len(d["queue"]) - 100])
                d["queue"] = [x for x in d["queue"] if x["id"] not in drop]
            _save(d)
        self._wake.set()
        return out

    def cancel(self, jid: str) -> bool:
        """Remove a queued/finished item (never one mid-upload)."""
        with _LOCK:
            d = _load()
            before = len(d["queue"])
            d["queue"] = [x for x in d["queue"]
                          if not (x["id"] == jid and x.get("status") in ("queued", "error", "uploaded"))]
            changed = len(d["queue"]) < before
            if changed:
                _save(d)
        return changed

    def _set(self, jid: str, **kw) -> None:
        with _LOCK:
            d = _load()
            for it in d["queue"]:
                if it["id"] == jid:
                    it.update(kw)
                    it["updated"] = int(time.time())
                    break
            _save(d)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                log(f"[posting] scheduler tick error: {e}")
            self._wake.wait(20)
            self._wake.clear()

    def _tick(self) -> None:
        now = time.time()
        with _LOCK:
            d = _load()
            due = [it for it in d["queue"]
                   if it.get("status") == "queued" and (it.get("when") or 0) <= now]
            ids = [it["id"] for it in due]
            for it in due:
                it["status"] = "uploading"
                it["updated"] = int(now)
            if due:
                _save(d)
        for jid in ids:                       # serialize: one upload at a time
            self._run_one(jid)

    def _run_one(self, jid: str) -> None:
        with _LOCK:
            d = _load()
            it = next((x for x in d["queue"] if x["id"] == jid), None)
            it = dict(it) if it else None
        if not it:
            return
        plat = it.get("platform")

        def pcb(frac):
            self._set(jid, progress=max(0, min(100, int(frac * 100))))

        try:
            if not os.path.exists(it.get("mp4") or ""):
                raise RuntimeError("the clip file is gone — re-download it, then post again.")
            res = self.youtube_upload(it, pcb) if plat == "youtube" else self.tiktok_upload(it, pcb)
            self._set(jid, status="uploaded", result=res, error="", progress=100)
            log(f"[posting] {plat} upload done for {it.get('clip')} -> {res}")
        except Exception as e:  # noqa: BLE001
            self._set(jid, status="error", error=str(e))
            log(f"[posting] {plat} upload {jid} failed: {e}")


# ----------------------------------------------------------------------------- module singleton
POSTER: Poster | None = None


def init(cfg: config.Config) -> Poster:
    """Get (creating + starting the scheduler on first call) the process-wide Poster."""
    global POSTER
    if POSTER is None:
        POSTER = Poster(cfg)
        POSTER.start()
    return POSTER
