"""Social publishing: OAuth connect, per-platform publish flows (against a fake
HTTP layer that records every request), scheduling, retries and API."""
from __future__ import annotations

import json
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from clipviral import accounts, brands, config, jobs, main, social


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = data if data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._data


class FakeHTTP:
    """Routes (method, url-prefix) -> list of responses (consumed in order)."""

    def __init__(self):
        self.routes: list[tuple[str, str, list]] = []
        self.calls: list[dict] = []

    def add(self, method, prefix, *responses):
        self.routes.append((method, prefix, list(responses)))

    def __call__(self, method, url, **kw):
        body = kw.get("data")
        if hasattr(body, "read"):
            body = body.read()
        self.calls.append({"method": method, "url": url, **{k: v for k, v in kw.items() if k != "data"}, "data": body})
        for m, prefix, resps in self.routes:
            if m == method and url.startswith(prefix):
                if len(resps) > 1:
                    return resps.pop(0)
                return resps[0]
        raise AssertionError(f"unexpected request {method} {url}")


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    monkeypatch.setattr(brands, "BRANDS_DIR", tmp_path / "brands")
    monkeypatch.setattr(accounts, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(social, "_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.delenv("CLIPVIRAL_SECRET_KEY", raising=False)
    monkeypatch.setattr(social.time, "sleep", lambda s: None)
    accounts.init()
    social.init()
    accounts._fails.clear()
    for k, v in {"YOUTUBE_CLIENT_ID": "yt-id", "YOUTUBE_CLIENT_SECRET": "yt-sec", "TIKTOK_CLIENT_KEY": "tt-key",
                 "TIKTOK_CLIENT_SECRET": "tt-sec", "INSTAGRAM_APP_ID": "ig-id", "INSTAGRAM_APP_SECRET": "ig-sec",
                 "X_CLIENT_ID": "x-id", "X_CLIENT_SECRET": "x-sec"}.items():
        monkeypatch.setenv(k, v)
    http = FakeHTTP()
    monkeypatch.setattr(social, "_request", http)
    return http


@pytest.fixture
def ws(env):
    c = TestClient(main.app)
    assert c.post("/api/auth/signup", json={"email": "s@example.com", "password": "password123"}).status_code == 200
    me = c.get("/api/me").json()
    return c, me["workspace"]["id"], me["user"]["id"]


def _clip(tmp_ws, size=1500, duration=30.0):
    job = jobs.create("p", {}, workspace_id=tmp_ws)
    d = jobs.job_dir(job["id"])
    (d / "c01.mp4").write_bytes(b"\x00" * size)
    jobs.update(job["id"], status="done", clips=[{"id": "c01", "status": "ready", "duration": duration,
                                                  "title": "Clip title", "start": 0, "end": duration}])
    return job["id"]


def _connect(env, platform, ws_id, user_id):
    http = env
    url = social.start_connect(platform, ws_id, user_id, f"https://app.example/api/social/callback/{platform}")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return url, q


# ---------------------------------------------------------------- crypto & signing
def test_encrypt_roundtrip_and_media_signing(env):
    blob = social.encrypt({"access_token": "secret-token"})
    assert "secret-token" not in blob and social.decrypt(blob)["access_token"] == "secret-token"
    tok = social.sign_media("job1", "c01")
    assert social.verify_media(tok) == ("job1", "c01")
    assert social.verify_media(tok.replace("c01", "c02")) is None
    expired = social.sign_media("job1", "c01", ttl=-5)
    assert social.verify_media(expired) is None


# ---------------------------------------------------------------- connect flows
def test_connect_urls_and_state(env, ws):
    _, ws_id, uid = ws
    url, q = _connect(env, "x", ws_id, uid)
    assert url.startswith("https://x.com/i/oauth2/authorize?")
    assert q["code_challenge_method"] == ["S256"] and "media.write" in q["scope"][0] and q["client_id"] == ["x-id"]
    url, q = _connect(env, "youtube", ws_id, uid)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["access_type"] == ["offline"] and "youtube.upload" in q["scope"][0]
    url, q = _connect(env, "tiktok", ws_id, uid)
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?") and q["client_key"] == ["tt-key"]
    assert "video.publish" in q["scope"][0]
    url, q = _connect(env, "instagram", ws_id, uid)
    assert url.startswith("https://www.instagram.com/oauth/authorize?")
    assert "instagram_business_content_publish" in q["scope"][0]
    with pytest.raises(social.SocialError):
        social.finish_connect("x", "bogus-state", "code", "https://app.example/cb")


def test_unconfigured_platform(env, ws, monkeypatch):
    monkeypatch.delenv("X_CLIENT_SECRET")
    with pytest.raises(social.SocialError, match="isn't available"):
        social.start_connect("x", ws[1], ws[2], "https://a/cb")


def test_finish_connect_all_platforms_and_tokens_encrypted(env, ws):
    http = env
    _, ws_id, uid = ws
    # X
    _, q = _connect(env, "x", ws_id, uid)
    http.add("POST", "https://api.x.com/2/oauth2/token", Resp(200, {"access_token": "xa", "refresh_token": "xr", "expires_in": 7200}))
    http.add("GET", "https://api.x.com/2/users/me", Resp(200, {"data": {"id": "42", "username": "clipper"}}))
    c = social.finish_connect("x", q["state"][0], "code1", "https://app.example/api/social/callback/x")
    assert c["account_name"] == "@clipper"
    tok_call = [x for x in http.calls if x["url"].endswith("/oauth2/token")][0]
    assert tok_call["auth"] == ("x-id", "x-sec") and tok_call["data"]["code_verifier"]
    # YouTube
    _, q = _connect(env, "youtube", ws_id, uid)
    http.add("POST", "https://oauth2.googleapis.com/token", Resp(200, {"access_token": "ya", "refresh_token": "yr", "expires_in": 3600}))
    http.add("GET", "https://www.googleapis.com/youtube/v3/channels", Resp(200, {"items": [{"id": "UC1", "snippet": {"title": "My Channel"}}]}))
    assert social.finish_connect("youtube", q["state"][0], "c", "https://app.example/cb")["account_name"] == "My Channel"
    # TikTok
    _, q = _connect(env, "tiktok", ws_id, uid)
    http.add("POST", "https://open.tiktokapis.com/v2/oauth/token/", Resp(200, {"access_token": "ta", "refresh_token": "tr",
             "open_id": "open1", "expires_in": 86400}))
    http.add("POST", "https://open.tiktokapis.com/v2/post/publish/creator_info/query/", Resp(200, {
        "data": {"creator_nickname": "Tik Person", "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
                 "max_video_post_duration_sec": 600}, "error": {"code": "ok"}}))
    assert social.finish_connect("tiktok", q["state"][0], "c", "https://app.example/cb")["account_name"] == "Tik Person"
    # Instagram
    _, q = _connect(env, "instagram", ws_id, uid)
    http.add("POST", "https://api.instagram.com/oauth/access_token", Resp(200, {"access_token": "short", "user_id": 77}))
    http.add("GET", "https://graph.instagram.com/access_token", Resp(200, {"access_token": "long", "expires_in": 5184000}))
    http.add("GET", "https://graph.instagram.com/v25.0/me", Resp(200, {"user_id": "1784", "username": "brand.ig"}))
    assert social.finish_connect("instagram", q["state"][0], "c", "https://app.example/cb")["account_name"] == "brand.ig"
    conns = social.connections(ws_id)
    assert {c["platform"] for c in conns} == {"x", "youtube", "tiktok", "instagram"}
    with accounts.db() as con:
        raw = json.dumps([dict(r) for r in con.execute("SELECT * FROM social_connections").fetchall()])
    for secret in ("xa", "xr", "ya", "yr", "ta", "long"):
        assert f'"{secret}"' not in raw
    # state is single-use
    with pytest.raises(social.SocialError):
        social.finish_connect("instagram", q["state"][0], "c", "https://app.example/cb")


def _store_conn(ws_id, platform, account_id="acc", tok=None):
    tok = tok or {"access_token": "AT", "refresh_token": "RT", "expires_at": time.time() + 3600}
    cid = "conn" + platform
    with accounts.db() as con:
        con.execute("INSERT INTO social_connections (id,workspace_id,platform,account_id,account_name,token_enc,expires_at,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)", (cid, ws_id, platform, account_id, platform, social.encrypt(tok),
                                                 tok["expires_at"], time.time()))
    return cid


# ---------------------------------------------------------------- publish flows
def test_publish_youtube(env, ws):
    http = env
    _, ws_id, uid = ws
    accounts.set_plan(ws_id, "starter")
    job = _clip(ws_id)
    cid = _store_conn(ws_id, "youtube", "UC1")
    http.add("POST", "https://www.googleapis.com/upload/youtube/v3/videos", Resp(200, {}, {"Location": "https://upload.example/session1"}))
    http.add("PUT", "https://upload.example/session1", Resp(200, {"id": "vid123"}))
    [p] = social.schedule(ws_id, uid, job, "c01", [{"connection_id": cid, "caption": "Watch this #shorts", "title": "Big title"}], None)
    social.publish_one(p["id"])
    post = social.list_posts(ws_id)[0]
    assert post["status"] == "published" and post["url"] == "https://www.youtube.com/shorts/vid123", post
    init = [c for c in http.calls if c["url"].startswith("https://www.googleapis.com/upload")][0]
    assert init["params"] == {"uploadType": "resumable", "part": "snippet,status"}
    assert init["headers"]["X-Upload-Content-Length"] == "1500" and init["headers"]["X-Upload-Content-Type"] == "video/mp4"
    meta = json.loads(init["data"])
    assert meta["snippet"]["title"] == "Big title" and meta["status"]["privacyStatus"] == "public"
    put = [c for c in http.calls if c["method"] == "PUT"][0]
    assert len(put["data"]) == 1500


def test_publish_tiktok_chunked(env, ws, monkeypatch):
    http = env
    _, ws_id, uid = ws
    accounts.set_plan(ws_id, "creator")
    monkeypatch.setattr(social.TikTok, "CHUNK", 1000)
    job = _clip(ws_id, size=2600)
    cid = _store_conn(ws_id, "tiktok", "open1")
    http.add("POST", "https://open.tiktokapis.com/v2/post/publish/creator_info/query/", Resp(200, {
        "data": {"privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 600}, "error": {"code": "ok"}}))
    http.add("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/", Resp(200, {
        "data": {"publish_id": "pub1", "upload_url": "https://upload.tiktok.example/u"}, "error": {"code": "ok"}}))
    http.add("PUT", "https://upload.tiktok.example/u", Resp(201, {}))
    http.add("POST", "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
             Resp(200, {"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}}),
             Resp(200, {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7311]}, "error": {"code": "ok"}}))
    [p] = social.schedule(ws_id, uid, job, "c01", [{"connection_id": cid, "caption": "hi #fyp", "options": {"privacy": "SELF_ONLY"}}], None)
    social.publish_one(p["id"])
    post = social.list_posts(ws_id)[0]
    assert post["status"] == "published" and post["external_id"] == "7311", post
    init = [c for c in http.calls if c["url"].endswith("/video/init/")][0]["json"]
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": 2600, "chunk_size": 1000, "total_chunk_count": 2}
    assert init["post_info"]["privacy_level"] == "SELF_ONLY"
    puts = [c for c in http.calls if c["method"] == "PUT"]
    assert [c["headers"]["Content-Range"] for c in puts] == ["bytes 0-999/2600", "bytes 1000-2599/2600"]
    assert [len(c["data"]) for c in puts] == [1000, 1600]


def test_tiktok_chunk_plan_rules():
    small = 3 * 1024 * 1024
    assert social.TikTok.chunk_plan(small) == (small, 1)
    big = 95 * 1024 * 1024
    size, count = social.TikTok.chunk_plan(big)
    assert size == social.TikTok.CHUNK and count == 9  # last chunk absorbs the remainder (15 MB)
    assert 5 * 1024 * 1024 <= size <= 64 * 1024 * 1024


def test_publish_tiktok_rejects_disallowed_privacy(env, ws):
    http = env
    _, ws_id, uid = ws
    accounts.set_plan(ws_id, "creator")
    job = _clip(ws_id)
    cid = _store_conn(ws_id, "tiktok")
    http.add("POST", "https://open.tiktokapis.com/v2/post/publish/creator_info/query/", Resp(200, {
        "data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}}))
    [p] = social.schedule(ws_id, uid, job, "c01", [{"connection_id": cid, "caption": "x", "options": {"privacy": "PUBLIC_TO_EVERYONE"}}], None)
    social.publish_one(p["id"])
    post = social.list_posts(ws_id)[0]
    assert post["status"] == "failed" and "isn't allowed" in post["error"]


def test_publish_instagram_reel(env, ws, monkeypatch):
    http = env
    _, ws_id, uid = ws
    accounts.set_plan(ws_id, "pro")
    monkeypatch.setenv("CLIPVIRAL_PUBLIC_URL", "https://clips.example.com")
    job = _clip(ws_id)
    cid = _store_conn(ws_id, "instagram", "1784")
    g = "https://graph.instagram.com/v25.0"
    http.add("POST", f"{g}/1784/media_publish", Resp(200, {"id": "media9"}))
    http.add("POST", f"{g}/1784/media", Resp(200, {"id": "cont1"}))
    http.add("GET", f"{g}/cont1", Resp(200, {"status_code": "IN_PROGRESS"}), Resp(200, {"status_code": "FINISHED"}))
    http.add("GET", f"{g}/media9", Resp(200, {"permalink": "https://www.instagram.com/reel/abc/"}))
    [p] = social.schedule(ws_id, uid, job, "c01", [{"connection_id": cid, "caption": "Reel caption"}], None)
    social.publish_one(p["id"])
    post = social.list_posts(ws_id)[0]
    assert post["status"] == "published" and post["url"] == "https://www.instagram.com/reel/abc/", post
    create = [c for c in http.calls if c["url"] == f"{g}/1784/media"][0]["data"]
    assert create["media_type"] == "REELS" and create["video_url"].startswith("https://clips.example.com/media/")
    token = create["video_url"].rsplit("/", 1)[1][:-4]
    assert social.verify_media(token) == (job, "c01")
    pub = [c for c in http.calls if c["url"].endswith("/media_publish")][0]["data"]
    assert pub["creation_id"] == "cont1"


def test_instagram_requires_public_https(env, ws, monkeypatch):
    _, ws_id, uid = ws
    accounts.set_plan(ws_id, "pro")
    monkeypatch.setenv("CLIPVIRAL_PUBLIC_URL", "http://localhost:8000")
    job = _clip(ws_id)
    cid = _store_conn(ws_id, "instagram", "1784")
    [p] = social.schedule(ws_id, uid, job, "c01", [{"connection_id": cid, "caption": "x"}], None)
    social.publish_one(p["id"])
    assert "public https" in social.list_posts(ws_id)[0]["error"]


def test_publish_x_with_processing(env, ws, monkeypatch):
    http = env
    _, ws_id, uid = ws
    accounts.set_plan(ws_id, "pro")
    monkeypatch.setattr(social.X, "CHUNK", 600)
    job = _clip(ws_id, size=1500, duration=45)
    cid = _store_conn(ws_id, "x", "42")
    http.add("POST", "https://api.x.com/2/media/upload/initialize", Resp(200, {"data": {"id": "m55"}}))
    http.add("POST", "https://api.x.com/2/media/upload/m55/append", Resp(204, {}))
    http.add("POST", "https://api.x.com/2/media/upload/m55/finalize", Resp(200, {"data": {"id": "m55",
             "processing_info": {"state": "pending", "check_after_secs": 1}}}))
    http.add("GET", "https://api.x.com/2/media/upload", Resp(200, {"data": {"processing_info": {"state": "succeeded"}}}))
    http.add("POST", "https://api.x.com/2/tweets", Resp(201, {"data": {"id": "t900", "text": "x"}}))
    [p] = social.schedule(ws_id, uid, job, "c01", [{"connection_id": cid, "caption": "Short post"}], None)
    social.publish_one(p["id"])
    post = social.list_posts(ws_id)[0]
    assert post["status"] == "published" and post["url"] == "https://x.com/i/web/status/t900", post
    init = [c for c in http.calls if c["url"].endswith("/initialize")][0]["json"]
    assert init == {"media_type": "video/mp4", "total_bytes": 1500, "media_category": "tweet_video"}
    appends = [c for c in http.calls if c["url"].endswith("/append")]
    assert [a["data"]["segment_index"] for a in appends] == ["0", "1", "2"]
    tweet = [c for c in http.calls if c["url"].endswith("/2/tweets")][0]["json"]
    assert tweet == {"text": "Short post", "media": {"media_ids": ["m55"]}}


# ---------------------------------------------------------------- scheduling rules, retries, refresh
def test_schedule_validations(env, ws):
    _, ws_id, uid = ws
    accounts.set_plan(ws_id, "pro")
    job = _clip(ws_id, duration=200)
    x = _store_conn(ws_id, "x")
    with pytest.raises(social.SocialError, match="140 seconds"):
        social.schedule(ws_id, uid, job, "c01", [{"connection_id": x, "caption": "a"}], None)
    job2 = _clip(ws_id, duration=30)
    with pytest.raises(social.SocialError, match="280"):
        social.schedule(ws_id, uid, job2, "c01", [{"connection_id": x, "caption": "a" * 281}], None)
    with pytest.raises(social.SocialError, match="not found"):
        social.schedule(ws_id, uid, job2, "c01", [{"connection_id": "nope", "caption": "a"}], None)
    with pytest.raises(social.SocialError, match="6 months"):
        social.schedule(ws_id, uid, job2, "c01", [{"connection_id": x, "caption": "a"}], time.time() + 400 * 86400)


def test_future_posts_wait_and_cancel(env, ws):
    _, ws_id, uid = ws
    accounts.set_plan(ws_id, "pro")
    job = _clip(ws_id)
    x = _store_conn(ws_id, "x")
    [p] = social.schedule(ws_id, uid, job, "c01", [{"connection_id": x, "caption": "later"}], time.time() + 3600)
    assert p["id"] not in social.due_posts()
    assert social.cancel(ws_id, p["id"])
    assert social.list_posts(ws_id)[0]["status"] == "cancelled"
    assert not social.cancel(ws_id, p["id"])


def test_transient_failure_retries_then_fails(env, ws):
    http = env
    _, ws_id, uid = ws
    accounts.set_plan(ws_id, "pro")
    job = _clip(ws_id)
    x = _store_conn(ws_id, "x")
    http.add("POST", "https://api.x.com/2/media/upload/initialize", Resp(503, {"title": "Service Unavailable"}))
    [p] = social.schedule(ws_id, uid, job, "c01", [{"connection_id": x, "caption": "retry me"}], None)
    social.publish_one(p["id"])
    post = social.list_posts(ws_id)[0]
    assert post["status"] == "scheduled" and post["attempts"] == 1 and post["scheduled_at"] > time.time() + 200
    for _ in range(2):
        with accounts.db() as con:
            con.execute("UPDATE social_posts SET scheduled_at=? WHERE id=?", (time.time() - 1, p["id"]))
        social.publish_one(p["id"])
    post = social.list_posts(ws_id)[0]
    assert post["status"] == "failed" and post["attempts"] == 3 and "503" in post["error"]


def test_expiring_token_is_refreshed(env, ws):
    http = env
    _, ws_id, uid = ws
    accounts.set_plan(ws_id, "pro")
    job = _clip(ws_id)
    cid = _store_conn(ws_id, "youtube", "UC1", tok={"access_token": "old", "refresh_token": "RT", "expires_at": time.time() + 10})
    http.add("POST", "https://oauth2.googleapis.com/token", Resp(200, {"access_token": "fresh", "expires_in": 3600}))
    http.add("POST", "https://www.googleapis.com/upload/youtube/v3/videos", Resp(200, {}, {"Location": "https://up/s"}))
    http.add("PUT", "https://up/s", Resp(200, {"id": "v1"}))
    [p] = social.schedule(ws_id, uid, job, "c01", [{"connection_id": cid, "caption": "c"}], None)
    social.publish_one(p["id"])
    assert social.list_posts(ws_id)[0]["status"] == "published"
    refresh = [c for c in http.calls if c["url"] == "https://oauth2.googleapis.com/token"][0]["data"]
    assert refresh["grant_type"] == "refresh_token" and refresh["refresh_token"] == "RT"
    init = [c for c in http.calls if "upload/youtube" in c["url"]][0]
    assert init["headers"]["Authorization"] == "Bearer fresh"
    with accounts.db() as con:
        stored = social.decrypt(con.execute("SELECT token_enc FROM social_connections WHERE id=?", (cid,)).fetchone()[0])
    assert stored["access_token"] == "fresh" and stored["refresh_token"] == "RT"


def test_downgraded_workspace_cannot_publish(env, ws):
    _, ws_id, uid = ws
    accounts.set_plan(ws_id, "pro")
    job = _clip(ws_id)
    x = _store_conn(ws_id, "x")
    [p] = social.schedule(ws_id, uid, job, "c01", [{"connection_id": x, "caption": "a"}], None)
    accounts.set_plan(ws_id, "free")
    social.publish_one(p["id"])
    post = social.list_posts(ws_id)[0]
    assert post["status"] == "failed" and "paid plan" in post["error"]


# ---------------------------------------------------------------- API
def test_social_api_endpoints(env, ws, monkeypatch):
    http = env
    c, ws_id, uid = ws
    o = c.get("/api/social").json()
    assert o["allowed"] is False and {p["key"] for p in o["platforms"]} == {"youtube", "tiktok", "instagram", "x"}
    assert c.post("/api/social/connect/x").status_code == 402  # free plan
    accounts.set_plan(ws_id, "starter")
    r = c.post("/api/social/connect/x")
    assert r.status_code == 200 and r.json()["url"].startswith("https://x.com/i/oauth2/authorize")
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.json()["url"]).query)["state"][0]
    http.add("POST", "https://api.x.com/2/oauth2/token", Resp(200, {"access_token": "a", "refresh_token": "r", "expires_in": 7200}))
    http.add("GET", "https://api.x.com/2/users/me", Resp(200, {"data": {"id": "1", "username": "me"}}))
    r = c.get(f"/api/social/callback/x?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/studio?connected=x"
    r = c.get("/api/social/callback/x?error=access_denied", follow_redirects=False)
    assert r.status_code == 303 and "social_error=" in r.headers["location"]
    conn = c.get("/api/social").json()["connections"][0]
    job = _clip(ws_id)
    r = c.post("/api/posts", json={"job_id": job, "clip_id": "c01", "scheduled_at": time.time() + 3600,
                                   "items": [{"connection_id": conn["id"], "caption": "hello"}]})
    assert r.status_code == 200, r.text
    pid = r.json()["posts"][0]["id"]
    posts = c.get("/api/posts").json()["posts"]
    assert posts[0]["status"] == "scheduled" and posts[0]["account_name"] == "@me"
    assert c.delete(f"/api/posts/{pid}").status_code == 200
    # someone else's project can't be posted
    other = TestClient(main.app)
    other.post("/api/auth/signup", json={"email": "o@example.com", "password": "password123"})
    accounts.set_plan(other.get("/api/me").json()["workspace"]["id"], "pro")
    r = other.post("/api/posts", json={"job_id": job, "clip_id": "c01", "items": [{"connection_id": conn["id"]}]})
    assert r.status_code == 404
    # signed public media link
    tok = social.sign_media(job, "c01")
    assert TestClient(main.app).get(f"/media/{tok}.mp4").status_code == 200
    assert TestClient(main.app).get(f"/media/{tok[:-2]}xx.mp4").status_code == 404
    # disconnect cancels scheduled posts
    c.post("/api/posts", json={"job_id": job, "clip_id": "c01", "scheduled_at": time.time() + 3600,
                               "items": [{"connection_id": conn["id"], "caption": "x"}]})
    assert c.delete(f"/api/social/connections/{conn['id']}").status_code == 200
    assert all(p["status"] == "cancelled" for p in c.get("/api/posts").json()["posts"])
