"""Connect social accounts and publish / schedule clips.

Platforms: YouTube (Shorts), TikTok, Instagram (Reels) and X. Each needs a
developer app (client id + secret) configured in the environment; platforms
without credentials are simply shown as "not configured".

Tokens are encrypted at rest (Fernet, key from CLIPVIRAL_SECRET_KEY or an
auto-generated DATA_DIR/secret.key). A background thread publishes posts when
their time comes and retries transient failures.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

from . import accounts, config, jobs

log = logging.getLogger("clipviral.social")
_KEY_FILE = config.DATA_DIR / "secret.key"
MAX_ATTEMPTS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS social_connections (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, platform TEXT NOT NULL, account_id TEXT,
  account_name TEXT, token_enc TEXT NOT NULL, expires_at REAL, created_at REAL, created_by TEXT);
CREATE TABLE IF NOT EXISTS social_posts (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, connection_id TEXT NOT NULL, platform TEXT NOT NULL,
  job_id TEXT NOT NULL, clip_id TEXT NOT NULL, caption TEXT, title TEXT, options TEXT,
  scheduled_at REAL NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  external_id TEXT, url TEXT, error TEXT, created_by TEXT, created_at REAL, published_at REAL);
CREATE INDEX IF NOT EXISTS posts_due ON social_posts (status, scheduled_at);
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, user_id TEXT, platform TEXT NOT NULL,
  verifier TEXT, created_at REAL);
"""


class SocialError(Exception):
    """A failure to show the user. `retry` marks transient errors."""

    def __init__(self, msg: str, retry: bool = False):
        super().__init__(msg)
        self.retry = retry


def init() -> None:
    with accounts.db() as con:
        con.executescript(SCHEMA)


# --------------------------------------------------------------------------- secrets
def _secret() -> bytes:
    env = os.environ.get("CLIPVIRAL_SECRET_KEY", "").strip()
    if env:
        return base64.urlsafe_b64decode(env.encode())
    if not _KEY_FILE.exists():
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KEY_FILE.write_bytes(base64.urlsafe_b64encode(secrets.token_bytes(32)))
        os.chmod(_KEY_FILE, 0o600)
    return base64.urlsafe_b64decode(_KEY_FILE.read_bytes())


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(_secret()))


def encrypt(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt(blob: str) -> dict:
    return json.loads(_fernet().decrypt(blob.encode()).decode())


def sign_media(job_id: str, clip_id: str, ttl: int = 3600) -> str:
    """Short-lived public token for platforms that fetch the video by URL (Instagram)."""
    exp = int(time.time()) + ttl
    msg = f"{job_id}.{clip_id}.{exp}"
    sig = hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{msg}.{sig}"


def verify_media(token: str) -> tuple[str, str] | None:
    try:
        job_id, clip_id, exp, sig = token.split(".")
        good = hmac.new(_secret(), f"{job_id}.{clip_id}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(sig, good) and int(exp) > time.time():
            return job_id, clip_id
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- HTTP
def _request(method: str, url: str, **kw):
    """Single choke point for outbound HTTP (patched in tests)."""
    import requests
    kw.setdefault("timeout", 60)
    try:
        return requests.request(method, url, **kw)
    except requests.RequestException as exc:
        raise SocialError(f"Network error talking to the platform: {exc}", retry=True)


def _json(resp, what: str) -> dict:
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code >= 400:
        detail = ""
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or err.get("code") or ""
            elif err:
                detail = str(data.get("error_description") or err)
            detail = detail or data.get("detail") or data.get("title") or ""
        retry = resp.status_code == 429 or resp.status_code >= 500
        raise SocialError(f"{what} failed ({resp.status_code}) {detail}".strip(), retry=retry)
    return data if isinstance(data, dict) else {}


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


# --------------------------------------------------------------------------- providers
class Provider:
    key = ""
    label = ""
    env_id = ""
    env_secret = ""
    max_seconds: float | None = None

    def client_id(self) -> str:
        return os.environ.get(self.env_id, "").strip()

    def client_secret(self) -> str:
        return os.environ.get(self.env_secret, "").strip()

    def configured(self) -> bool:
        return bool(self.client_id() and self.client_secret())

    def authorize_url(self, state: str, redirect_uri: str, challenge: str) -> str:
        raise NotImplementedError

    def exchange(self, code: str, redirect_uri: str, verifier: str) -> tuple[dict, str, str]:
        """-> (token dict incl. expires_at, account_id, account_name)"""
        raise NotImplementedError

    def refresh(self, tok: dict) -> dict:
        raise NotImplementedError

    def publish(self, tok: dict, account_id: str, video: Path, post: dict) -> tuple[str, str]:
        """-> (external_id, url)"""
        raise NotImplementedError


class YouTube(Provider):
    key, label = "youtube", "YouTube"
    env_id, env_secret = "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"
    AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN = "https://oauth2.googleapis.com/token"
    SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"

    def authorize_url(self, state, redirect_uri, challenge):
        return self.AUTH + "?" + urllib.parse.urlencode({
            "client_id": self.client_id(), "redirect_uri": redirect_uri, "response_type": "code",
            "scope": self.SCOPES, "access_type": "offline", "prompt": "consent", "state": state,
            "include_granted_scopes": "true", "code_challenge": challenge, "code_challenge_method": "S256"})

    def _token(self, form: dict) -> dict:
        d = _json(_request("POST", self.TOKEN, data={**form, "client_id": self.client_id(),
                                                      "client_secret": self.client_secret()}), "Google sign-in")
        return {"access_token": d["access_token"], "refresh_token": d.get("refresh_token"),
                "expires_at": time.time() + float(d.get("expires_in", 3600))}

    def exchange(self, code, redirect_uri, verifier):
        tok = self._token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                           "code_verifier": verifier})
        d = _json(_request("GET", "https://www.googleapis.com/youtube/v3/channels",
                           params={"part": "snippet", "mine": "true"},
                           headers={"Authorization": f"Bearer {tok['access_token']}"}), "YouTube channel lookup")
        items = d.get("items") or []
        if not items:
            raise SocialError("This Google account has no YouTube channel.")
        return tok, items[0]["id"], items[0]["snippet"]["title"]

    def refresh(self, tok):
        if not tok.get("refresh_token"):
            raise SocialError("YouTube access expired. Reconnect the account.")
        new = self._token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        new["refresh_token"] = new.get("refresh_token") or tok["refresh_token"]
        return new

    def publish(self, tok, account_id, video, post):
        title = (post.get("title") or post["caption"] or "New video").replace("<", "").replace(">", "")[:100]
        meta = {"snippet": {"title": title, "description": post["caption"][:5000], "categoryId": "22"},
                "status": {"privacyStatus": post.get("options", {}).get("privacy", "public"),
                           "selfDeclaredMadeForKids": False}}
        size = video.stat().st_size
        init = _request("POST", "https://www.googleapis.com/upload/youtube/v3/videos",
                        params={"uploadType": "resumable", "part": "snippet,status"},
                        headers={"Authorization": f"Bearer {tok['access_token']}",
                                 "Content-Type": "application/json; charset=UTF-8",
                                 "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
                        data=json.dumps(meta))
        if init.status_code >= 400:
            _json(init, "YouTube upload start")
        upload_url = init.headers.get("Location")
        if not upload_url:
            raise SocialError("YouTube didn't return an upload URL.", retry=True)
        with video.open("rb") as f:
            d = _json(_request("PUT", upload_url, data=f, timeout=600,
                               headers={"Content-Type": "video/mp4", "Content-Length": str(size)}), "YouTube upload")
        vid = d.get("id")
        if not vid:
            raise SocialError("YouTube upload finished without a video id.", retry=True)
        return vid, f"https://www.youtube.com/shorts/{vid}"


class TikTok(Provider):
    key, label = "tiktok", "TikTok"
    env_id, env_secret = "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"
    AUTH = "https://www.tiktok.com/v2/auth/authorize/"
    TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
    API = "https://open.tiktokapis.com/v2"
    MIN_CHUNK = 5 * 1024 * 1024
    CHUNK = 10 * 1024 * 1024

    def authorize_url(self, state, redirect_uri, challenge):
        return self.AUTH + "?" + urllib.parse.urlencode({
            "client_key": self.client_id(), "scope": "user.info.basic,video.publish,video.upload",
            "response_type": "code", "redirect_uri": redirect_uri, "state": state})

    def _token(self, form):
        d = _json(_request("POST", self.TOKEN, data={**form, "client_key": self.client_id(),
                                                      "client_secret": self.client_secret()},
                           headers={"Content-Type": "application/x-www-form-urlencoded"}), "TikTok sign-in")
        if "access_token" not in d:
            raise SocialError(f"TikTok sign-in failed: {d.get('error_description') or d.get('error') or d}")
        return {"access_token": d["access_token"], "refresh_token": d.get("refresh_token"),
                "open_id": d.get("open_id"), "expires_at": time.time() + float(d.get("expires_in", 86400))}

    def _api(self, tok, path, body, what):
        d = _json(_request("POST", f"{self.API}{path}", json=body,
                           headers={"Authorization": f"Bearer {tok['access_token']}",
                                    "Content-Type": "application/json; charset=UTF-8"}), what)
        err = d.get("error") or {}
        if err.get("code") not in (None, "ok"):
            raise SocialError(f"{what} failed: {err.get('message') or err.get('code')}",
                              retry=err.get("code") in ("rate_limit_exceeded", "internal_error"))
        return d.get("data") or {}

    def exchange(self, code, redirect_uri, verifier):
        tok = self._token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri})
        info = self.creator_info(tok)
        return tok, tok.get("open_id") or "", info.get("creator_nickname") or info.get("creator_username") or "TikTok"

    def refresh(self, tok):
        new = self._token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        return {**tok, **{k: v for k, v in new.items() if v}}

    def creator_info(self, tok) -> dict:
        return self._api(tok, "/post/publish/creator_info/query/", {}, "TikTok creator info")

    @classmethod
    def chunk_plan(cls, size: int) -> tuple[int, int]:
        """(chunk_size, total_chunk_count): small files go in one piece; otherwise
        fixed chunks with the remainder folded into the last one."""
        if size <= cls.CHUNK:
            return size, 1
        count = size // cls.CHUNK
        return cls.CHUNK, count

    def publish(self, tok, account_id, video, post):
        opts = post.get("options") or {}
        info = self.creator_info(tok)
        allowed = info.get("privacy_level_options") or []
        privacy = opts.get("privacy") or (allowed[0] if allowed else "SELF_ONLY")
        if allowed and privacy not in allowed:
            raise SocialError(f"TikTok privacy '{privacy}' isn't allowed for this account ({', '.join(allowed)}).")
        max_s = info.get("max_video_post_duration_sec")
        if max_s and post.get("duration") and post["duration"] > max_s:
            raise SocialError(f"This account can post TikToks up to {max_s} seconds.")
        size = video.stat().st_size
        chunk, count = self.chunk_plan(size)
        data = self._api(tok, "/post/publish/video/init/", {
            "post_info": {"title": post["caption"][:2200], "privacy_level": privacy,
                          "disable_duet": bool(opts.get("disable_duet", False)),
                          "disable_comment": bool(opts.get("disable_comment", False)),
                          "disable_stitch": bool(opts.get("disable_stitch", False)),
                          "video_cover_timestamp_ms": 1000},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                            "total_chunk_count": count}}, "TikTok upload start")
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise SocialError("TikTok didn't return an upload URL.", retry=True)
        with video.open("rb") as f:
            for i in range(count):
                start = i * chunk
                end = size - 1 if i == count - 1 else start + chunk - 1
                f.seek(start)
                body = f.read(end - start + 1)
                r = _request("PUT", upload_url, data=body, timeout=600, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {start}-{end}/{size}"})
                if r.status_code >= 400:
                    _json(r, "TikTok upload")
        for _ in range(60):  # wait for processing (up to ~5 minutes)
            st = self._api(tok, "/post/publish/status/fetch/", {"publish_id": publish_id}, "TikTok status")
            status = st.get("status")
            if status == "PUBLISH_COMPLETE":
                ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
                pid = str(ids[0]) if ids else publish_id
                return pid, (f"https://www.tiktok.com/video/{ids[0]}" if ids else "")
            if status == "FAILED":
                raise SocialError(f"TikTok rejected the video: {st.get('fail_reason') or 'unknown reason'}")
            time.sleep(5)
        return publish_id, ""  # still processing on TikTok's side; it will appear when done


class Instagram(Provider):
    key, label = "instagram", "Instagram"
    env_id, env_secret = "INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"
    max_seconds = 15 * 60

    @property
    def graph(self) -> str:
        return f"https://graph.instagram.com/{os.environ.get('INSTAGRAM_GRAPH_VERSION', 'v25.0')}"

    def authorize_url(self, state, redirect_uri, challenge):
        return "https://www.instagram.com/oauth/authorize?" + urllib.parse.urlencode({
            "client_id": self.client_id(), "redirect_uri": redirect_uri, "response_type": "code",
            "scope": "instagram_business_basic,instagram_business_content_publish", "state": state})

    def exchange(self, code, redirect_uri, verifier):
        d = _json(_request("POST", "https://api.instagram.com/oauth/access_token", data={
            "client_id": self.client_id(), "client_secret": self.client_secret(),
            "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code}), "Instagram sign-in")
        if isinstance(d.get("data"), list) and d["data"]:
            d = d["data"][0]
        short = d.get("access_token")
        if not short:
            raise SocialError("Instagram sign-in returned no token.")
        ll = _json(_request("GET", "https://graph.instagram.com/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": self.client_secret(), "access_token": short}),
            "Instagram long-lived token")
        tok = {"access_token": ll["access_token"], "expires_at": time.time() + float(ll.get("expires_in", 5184000))}
        me = _json(_request("GET", f"{self.graph}/me", params={"fields": "user_id,username",
                                                               "access_token": tok["access_token"]}), "Instagram profile")
        return tok, str(me.get("user_id") or me.get("id") or d.get("user_id")), me.get("username") or "Instagram"

    def refresh(self, tok):
        d = _json(_request("GET", "https://graph.instagram.com/refresh_access_token", params={
            "grant_type": "ig_refresh_token", "access_token": tok["access_token"]}), "Instagram token refresh")
        return {"access_token": d["access_token"], "expires_at": time.time() + float(d.get("expires_in", 5184000))}

    def publish(self, tok, account_id, video, post):
        video_url = post.get("public_video_url")
        if not video_url:
            raise SocialError("Instagram needs the site on a public https address (set CLIPVIRAL_PUBLIC_URL).")
        c = _json(_request("POST", f"{self.graph}/{account_id}/media", data={
            "media_type": "REELS", "video_url": video_url, "caption": post["caption"][:2200],
            "share_to_feed": "true", "access_token": tok["access_token"]}), "Instagram upload start")
        cid = c.get("id")
        if not cid:
            raise SocialError("Instagram didn't create a media container.", retry=True)
        for _ in range(60):  # Instagram fetches + processes the video
            s = _json(_request("GET", f"{self.graph}/{cid}", params={"fields": "status_code,status",
                                                                       "access_token": tok["access_token"]}),
                      "Instagram processing status")
            code = s.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise SocialError(f"Instagram couldn't process the video: {s.get('status') or code}")
            time.sleep(5)
        else:
            raise SocialError("Instagram is still processing the video. We'll try again.", retry=True)
        p = _json(_request("POST", f"{self.graph}/{account_id}/media_publish", data={
            "creation_id": cid, "access_token": tok["access_token"]}), "Instagram publish")
        mid = p.get("id")
        link = ""
        try:
            link = _json(_request("GET", f"{self.graph}/{mid}", params={"fields": "permalink",
                                                                       "access_token": tok["access_token"]}),
                         "Instagram permalink").get("permalink", "")
        except SocialError:
            pass
        return str(mid), link


class X(Provider):
    key, label = "x", "X"
    env_id, env_secret = "X_CLIENT_ID", "X_CLIENT_SECRET"
    max_seconds = 140
    API = "https://api.x.com/2"
    CHUNK = 4 * 1024 * 1024

    def authorize_url(self, state, redirect_uri, challenge):
        return "https://x.com/i/oauth2/authorize?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": self.client_id(), "redirect_uri": redirect_uri,
            "scope": "tweet.read tweet.write users.read media.write offline.access", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    def _token(self, form):
        d = _json(_request("POST", f"{self.API}/oauth2/token", data={**form, "client_id": self.client_id()},
                           auth=(self.client_id(), self.client_secret()),
                           headers={"Content-Type": "application/x-www-form-urlencoded"}), "X sign-in")
        return {"access_token": d["access_token"], "refresh_token": d.get("refresh_token"),
                "expires_at": time.time() + float(d.get("expires_in", 7200))}

    def _auth(self, tok):
        return {"Authorization": f"Bearer {tok['access_token']}"}

    def exchange(self, code, redirect_uri, verifier):
        tok = self._token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                           "code_verifier": verifier})
        me = _json(_request("GET", f"{self.API}/users/me", headers=self._auth(tok)), "X profile").get("data") or {}
        return tok, me.get("id", ""), "@" + me.get("username", "x")

    def refresh(self, tok):
        if not tok.get("refresh_token"):
            raise SocialError("X access expired. Reconnect the account.")
        new = self._token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        new["refresh_token"] = new.get("refresh_token") or tok["refresh_token"]
        return new

    @staticmethod
    def _media(d: dict) -> dict:
        return d.get("data") if isinstance(d.get("data"), dict) else d

    def publish(self, tok, account_id, video, post):
        size = video.stat().st_size
        init = self._media(_json(_request("POST", f"{self.API}/media/upload/initialize", headers=self._auth(tok), json={
            "media_type": "video/mp4", "total_bytes": size, "media_category": "tweet_video"}), "X upload start"))
        mid = str(init.get("id") or init.get("media_id_string") or init.get("media_id") or "")
        if not mid:
            raise SocialError("X didn't return a media id.", retry=True)
        with video.open("rb") as f:
            seg = 0
            while chunk := f.read(self.CHUNK):
                r = _request("POST", f"{self.API}/media/upload/{mid}/append", headers=self._auth(tok),
                             data={"segment_index": str(seg)}, files={"media": ("clip.mp4", chunk, "video/mp4")},
                             timeout=300)
                if r.status_code >= 400:
                    _json(r, "X upload")
                seg += 1
        fin = self._media(_json(_request("POST", f"{self.API}/media/upload/{mid}/finalize", headers=self._auth(tok)),
                                "X upload finish"))
        info = fin.get("processing_info") or {}
        for _ in range(60):
            state = info.get("state")
            if not state or state == "succeeded":
                break
            if state == "failed":
                raise SocialError(f"X couldn't process the video: {(info.get('error') or {}).get('message', '')}")
            time.sleep(max(1, min(30, int(info.get("check_after_secs", 5)))))
            st = self._media(_json(_request("GET", f"{self.API}/media/upload", headers=self._auth(tok),
                                            params={"command": "STATUS", "media_id": mid}), "X processing status"))
            info = st.get("processing_info") or {}
        d = _json(_request("POST", f"{self.API}/tweets", headers=self._auth(tok),
                           json={"text": post["caption"][:280], "media": {"media_ids": [mid]}}), "X post")
        tid = (d.get("data") or {}).get("id")
        if not tid:
            raise SocialError("X didn't return a post id.", retry=True)
        return tid, f"https://x.com/i/web/status/{tid}"


PROVIDERS: dict[str, Provider] = {p.key: p for p in (YouTube(), TikTok(), Instagram(), X())}


# --------------------------------------------------------------------------- connections
def start_connect(platform: str, workspace_id: str, user_id: str, redirect_uri: str) -> str:
    p = PROVIDERS.get(platform)
    if not p:
        raise SocialError("Unknown platform.")
    if not p.configured():
        raise SocialError(f"Posting to {p.label} isn't available yet.")
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce()
    with accounts.db() as con:
        con.execute("DELETE FROM oauth_states WHERE created_at < ?", (time.time() - 900,))
        con.execute("INSERT INTO oauth_states (state,workspace_id,user_id,platform,verifier,created_at) VALUES (?,?,?,?,?,?)",
                    (state, workspace_id, user_id, platform, verifier, time.time()))
    return p.authorize_url(state, redirect_uri, challenge)


def finish_connect(platform: str, state: str, code: str, redirect_uri: str) -> dict:
    with accounts.db() as con:
        row = con.execute("SELECT * FROM oauth_states WHERE state=? AND platform=?", (state, platform)).fetchone()
        con.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    if not row or time.time() - row["created_at"] > 900:
        raise SocialError("That sign-in link expired. Please try connecting again.")
    p = PROVIDERS[platform]
    tok, account_id, name = p.exchange(code, redirect_uri, row["verifier"])
    with accounts.db() as con:
        existing = con.execute("SELECT id FROM social_connections WHERE workspace_id=? AND platform=? AND account_id=?",
                               (row["workspace_id"], platform, account_id)).fetchone()
        cid = existing["id"] if existing else secrets.token_hex(6)
        if existing:
            con.execute("UPDATE social_connections SET token_enc=?, expires_at=?, account_name=? WHERE id=?",
                        (encrypt(tok), tok.get("expires_at"), name, cid))
        else:
            con.execute("INSERT INTO social_connections (id,workspace_id,platform,account_id,account_name,token_enc,"
                        "expires_at,created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                        (cid, row["workspace_id"], platform, account_id, name, encrypt(tok),
                         tok.get("expires_at"), time.time(), row["user_id"]))
    return {"id": cid, "platform": platform, "account_name": name, "workspace_id": row["workspace_id"]}


def connections(workspace_id: str) -> list[dict]:
    with accounts.db() as con:
        rows = con.execute("SELECT id, platform, account_id, account_name, created_at FROM social_connections "
                           "WHERE workspace_id=? ORDER BY created_at", (workspace_id,)).fetchall()
    return [dict(r) for r in rows]


def disconnect(workspace_id: str, cid: str) -> None:
    with accounts.db() as con:
        con.execute("DELETE FROM social_connections WHERE workspace_id=? AND id=?", (workspace_id, cid))
        con.execute("UPDATE social_posts SET status='cancelled', error='Account disconnected' "
                    "WHERE connection_id=? AND status='scheduled'", (cid,))


def _token_for(conn_row) -> dict:
    tok = decrypt(conn_row["token_enc"])
    if tok.get("expires_at") and tok["expires_at"] < time.time() + 300:
        tok = PROVIDERS[conn_row["platform"]].refresh(tok)
        with accounts.db() as con:
            con.execute("UPDATE social_connections SET token_enc=?, expires_at=? WHERE id=?",
                        (encrypt(tok), tok.get("expires_at"), conn_row["id"]))
    return tok


def tiktok_options(workspace_id: str, cid: str) -> dict:
    with accounts.db() as con:
        row = con.execute("SELECT * FROM social_connections WHERE id=? AND workspace_id=? AND platform='tiktok'",
                          (cid, workspace_id)).fetchone()
    if not row:
        raise SocialError("TikTok account not found.")
    info = PROVIDERS["tiktok"].creator_info(_token_for(row))
    return {"privacy_level_options": info.get("privacy_level_options") or [],
            "max_video_post_duration_sec": info.get("max_video_post_duration_sec"),
            "comment_disabled": info.get("comment_disabled"), "duet_disabled": info.get("duet_disabled"),
            "stitch_disabled": info.get("stitch_disabled"), "nickname": info.get("creator_nickname")}


# --------------------------------------------------------------------------- posts
def schedule(workspace_id: str, user_id: str | None, job_id: str, clip_id: str, items: list[dict],
             when: float | None) -> list[dict]:
    """items: [{connection_id, caption, title?, options?}]"""
    job = jobs.load(job_id)
    if not job or job.get("workspace_id") != workspace_id:
        raise SocialError("Project not found.")
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if not clip or clip.get("status") != "ready":
        raise SocialError("Render the clip before posting it.")
    when = time.time() if when is None else float(when)
    if when > time.time() + 180 * 86400:
        raise SocialError("Posts can be scheduled up to 6 months ahead.")
    conns = {c["id"]: c for c in connections(workspace_id)}
    out = []
    with accounts.db() as con:
        for it in items:
            conn = conns.get(it.get("connection_id"))
            if not conn:
                raise SocialError("Social account not found.")
            p = PROVIDERS[conn["platform"]]
            if p.max_seconds and (clip.get("duration") or 0) > p.max_seconds:
                raise SocialError(f"{p.label} accepts videos up to {int(p.max_seconds)} seconds; this clip is "
                                  f"{clip['duration']:.0f}s. Trim it first.")
            caption = (it.get("caption") or "").strip()
            if conn["platform"] == "x" and len(caption) > 280:
                raise SocialError("X posts can be at most 280 characters.")
            pid = secrets.token_hex(6)
            con.execute("INSERT INTO social_posts (id,workspace_id,connection_id,platform,job_id,clip_id,caption,title,"
                        "options,scheduled_at,status,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, workspace_id, conn["id"], conn["platform"], job_id, clip_id, caption,
                         (it.get("title") or "")[:100], json.dumps(it.get("options") or {}), when, "scheduled",
                         user_id, time.time()))
            out.append({"id": pid, "platform": conn["platform"], "scheduled_at": when})
    _wake.set()
    return out


def list_posts(workspace_id: str, limit: int = 200) -> list[dict]:
    with accounts.db() as con:
        rows = con.execute("SELECT p.*, c.account_name FROM social_posts p LEFT JOIN social_connections c "
                           "ON c.id=p.connection_id WHERE p.workspace_id=? ORDER BY p.scheduled_at DESC LIMIT ?",
                           (workspace_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["options"] = json.loads(d.get("options") or "{}")
        out.append(d)
    return out


def cancel(workspace_id: str, post_id: str) -> bool:
    with accounts.db() as con:
        cur = con.execute("UPDATE social_posts SET status='cancelled' WHERE id=? AND workspace_id=? AND status='scheduled'",
                          (post_id, workspace_id))
        return cur.rowcount > 0


def _public_video_url(job_id: str, clip_id: str) -> str | None:
    base = os.environ.get("CLIPVIRAL_PUBLIC_URL", "").rstrip("/")
    if not base.startswith("https://"):
        return None
    return f"{base}/media/{sign_media(job_id, clip_id)}.mp4"


def publish_one(post_id: str) -> None:
    with accounts.db() as con:
        cur = con.execute("UPDATE social_posts SET status='publishing', attempts=attempts+1 "
                          "WHERE id=? AND status='scheduled'", (post_id,))
        if cur.rowcount == 0:
            return  # already taken / cancelled
        post = dict(con.execute("SELECT * FROM social_posts WHERE id=?", (post_id,)).fetchone())
        conn = con.execute("SELECT * FROM social_connections WHERE id=?", (post["connection_id"],)).fetchone()
    try:
        if not conn:
            raise SocialError("The social account was disconnected.")
        if not accounts.plan_of(post["workspace_id"])["scheduler"]:
            raise SocialError("Publishing needs a paid plan.")
        job = jobs.load(post["job_id"])
        clip = next((c for c in (job or {}).get("clips", []) if c["id"] == post["clip_id"]), None)
        video = jobs.job_dir(post["job_id"]) / f"{post['clip_id']}.mp4"
        if not clip or not video.exists():
            raise SocialError("The clip no longer exists.")
        payload = {"caption": post["caption"] or "", "title": post["title"] or clip.get("title", ""),
                   "options": json.loads(post["options"] or "{}"), "duration": clip.get("duration"),
                   "public_video_url": _public_video_url(post["job_id"], post["clip_id"])}
        tok = _token_for(conn)
        ext_id, url = PROVIDERS[post["platform"]].publish(tok, conn["account_id"], video, payload)
        with accounts.db() as con:
            con.execute("UPDATE social_posts SET status='published', external_id=?, url=?, error=NULL, published_at=? "
                        "WHERE id=?", (ext_id, url, time.time(), post_id))
    except Exception as exc:
        retry = isinstance(exc, SocialError) and exc.retry and post["attempts"] < MAX_ATTEMPTS
        msg = str(exc) if isinstance(exc, SocialError) else f"Unexpected error: {exc}"
        log.warning("post %s failed (retry=%s): %s", post_id, retry, msg)
        with accounts.db() as con:
            if retry:
                con.execute("UPDATE social_posts SET status='scheduled', scheduled_at=?, error=? WHERE id=?",
                            (time.time() + 300 * post["attempts"], msg, post_id))
            else:
                con.execute("UPDATE social_posts SET status='failed', error=? WHERE id=?", (msg, post_id))


_wake = threading.Event()


def due_posts() -> list[str]:
    with accounts.db() as con:
        rows = con.execute("SELECT id FROM social_posts WHERE status='scheduled' AND scheduled_at <= ? "
                           "ORDER BY scheduled_at LIMIT 20", (time.time(),)).fetchall()
    return [r["id"] for r in rows]


def _loop() -> None:
    while True:
        try:
            for pid in due_posts():
                publish_one(pid)
        except Exception:
            log.exception("scheduler loop error")
        _wake.wait(20)
        _wake.clear()


def start_scheduler() -> None:
    init()
    with accounts.db() as con:  # posts interrupted by a restart go back to the queue
        con.execute("UPDATE social_posts SET status='scheduled' WHERE status='publishing'")
    threading.Thread(target=_loop, daemon=True).start()
