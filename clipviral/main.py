"""ClipViral web server.

Run:  uvicorn clipviral.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import accounts, billing, brands, config, jobs, social
from .pipeline import broll, fetch, render
from .pipeline.captions import STYLES
from .pipeline.niches import public_niches

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
LAYOUTS = {"auto": "Auto (follow speaker or subject)", "smart": "Follow speaker or subject",
           "fit": "Fit + blurred background", "center": "Centre crop", "manual": "Manual position"}
ASPECT_LABELS = {"9:16": "9:16 vertical (TikTok, Reels, Shorts)", "1:1": "1:1 square",
                 "4:5": "4:5 portrait (Instagram feed)", "16:9": "16:9 landscape (YouTube, X)"}


COOKIE = "cv_session"
accounts.init()
social.init()


@asynccontextmanager
async def lifespan(_: FastAPI):
    accounts.init()
    jobs.start_workers()
    social.start_scheduler()
    yield


app = FastAPI(title="ClipViral", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def landing():
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/studio", response_class=HTMLResponse)
def studio():
    return FileResponse(config.WEB_DIR / "studio.html")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return FileResponse(config.WEB_DIR / "login.html")


# --------------------------------------------------------------------------- auth helpers
def ctx(request: Request) -> dict:
    """Current user + workspace from the session cookie or an API key (Authorization: Bearer cv_live_...)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        c = accounts.api_key_context(auth[7:].strip())
        if not c:
            raise HTTPException(401, "Invalid or revoked API key.")
        return c
    c = accounts.session_context(request.cookies.get(COOKIE))
    if not c:
        raise HTTPException(401, "Please log in.")
    return c


def user_ctx(c: dict = Depends(ctx)) -> dict:
    if c["via"] != "session":
        raise HTTPException(403, "This action needs a logged-in user, not an API key.")
    return c


def owner_ctx(c: dict = Depends(user_ctx)) -> dict:
    if c["role"] != "owner":
        raise HTTPException(403, "Only the workspace owner can do this.")
    return c


def _ws(c: dict) -> str:
    return c["workspace"]["id"]


def _job(job_id: str, c: dict) -> dict:
    job = jobs.load(_safe(job_id))
    if not job or job.get("workspace_id") != _ws(c):
        raise HTTPException(404, "Project not found")
    return job


def _brand(brand_id: str, c: dict) -> dict:
    b = brands.get(brand_id)
    if not b or b.get("workspace_id") != _ws(c):
        raise HTTPException(404, "Brand template not found")
    return b


def _set_cookie(resp: Response, token: str, request: Request) -> None:
    secure = os.environ.get("CLIPVIRAL_SECURE_COOKIES") == "1" or request.url.scheme == "https"
    resp.set_cookie(COOKIE, token, max_age=accounts.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=secure, path="/")


# --------------------------------------------------------------------------- accounts
class SignupIn(BaseModel):
    email: str
    password: str
    name: Optional[str] = ""
    invite: Optional[str] = None


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
def signup(body: SignupIn, request: Request, response: Response):
    try:
        user, token = accounts.signup(body.email, body.password, body.name or "", body.invite)
    except accounts.AuthError as exc:
        raise HTTPException(400, str(exc))
    _set_cookie(response, token, request)
    return {"user": user}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    try:
        user, token = accounts.login(body.email, body.password)
    except accounts.AuthError as exc:
        raise HTTPException(400, str(exc))
    _set_cookie(response, token, request)
    return {"user": user}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    tok = request.cookies.get(COOKIE)
    if tok:
        accounts.logout(tok)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(c: dict = Depends(user_ctx)):
    ws = c["workspace"]
    return {"user": c["user"], "role": c["role"],
            "workspace": {"id": ws["id"], "name": ws["name"], "plan_status": ws.get("plan_status"),
                          "has_billing": bool(ws.get("stripe_customer_id"))},
            "usage": accounts.usage_summary(ws["id"]),
            "workspaces": [{"id": w["id"], "name": w["name"], "role": w["role"]}
                           for w in accounts.workspaces_for(c["user"]["id"])]}


@app.post("/api/workspaces/{ws_id}/switch")
def switch_ws(ws_id: str, request: Request, c: dict = Depends(user_ctx)):
    try:
        accounts.switch_workspace(request.cookies.get(COOKIE), c["user"]["id"], ws_id)
    except accounts.AuthError as exc:
        raise HTTPException(403, str(exc))
    return {"ok": True}


class WorkspaceIn(BaseModel):
    name: str


@app.patch("/api/workspace")
def rename_ws(body: WorkspaceIn, c: dict = Depends(owner_ctx)):
    try:
        accounts.rename_workspace(_ws(c), body.name)
    except accounts.AuthError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.get("/api/plans")
def plans():
    return {"plans": accounts.PLANS, "billing": billing.enabled()}


# --------------------------------------------------------------------------- team
class InviteIn(BaseModel):
    email: str


@app.get("/api/team")
def team(c: dict = Depends(user_ctx)):
    return {"members": accounts.members(_ws(c)), "invites": accounts.pending_invites(_ws(c)),
            "seats": accounts.plan_of(_ws(c))["seats"], "owner_id": c["workspace"]["owner_id"]}


@app.post("/api/team/invites")
def invite(body: InviteIn, request: Request, c: dict = Depends(owner_ctx)):
    try:
        token = accounts.create_invite(_ws(c), c["user"]["id"], body.email)
    except accounts.AuthError as exc:
        raise HTTPException(400, str(exc))
    except accounts.QuotaError as exc:
        raise HTTPException(402, str(exc))
    base = billing.public_url(str(request.base_url))
    return {"link": f"{base}/login?invite={token}"}


@app.delete("/api/team/invites/{invite_id}")
def cancel_invite(invite_id: str, c: dict = Depends(owner_ctx)):
    accounts.revoke_invite(_ws(c), invite_id)
    return {"ok": True}


@app.delete("/api/team/members/{user_id}")
def remove_member(user_id: str, c: dict = Depends(owner_ctx)):
    try:
        accounts.remove_member(_ws(c), user_id)
    except accounts.AuthError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.get("/api/invites/{token}")
def invite_info(token: str):
    info = accounts.invite_info(token)
    if not info:
        raise HTTPException(404, "This invite link is invalid or was already used.")
    return info


@app.post("/api/invites/{token}/accept")
def accept(token: str, request: Request, c: dict = Depends(user_ctx)):
    try:
        ws_id = accounts.accept_invite(token, c["user"]["id"])
        accounts.switch_workspace(request.cookies.get(COOKIE), c["user"]["id"], ws_id)
    except accounts.AuthError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


# --------------------------------------------------------------------------- billing
class CheckoutIn(BaseModel):
    plan: str


@app.get("/api/billing")
def billing_info(c: dict = Depends(user_ctx)):
    return {"enabled": billing.enabled(), "usage": accounts.usage_summary(_ws(c)), "plans": accounts.PLANS,
            "has_subscription": bool(c["workspace"].get("stripe_subscription_id"))}


@app.post("/api/billing/checkout")
def checkout(body: CheckoutIn, request: Request, c: dict = Depends(owner_ctx)):
    try:
        return {"url": billing.checkout_url(c["workspace"], c["user"], body.plan, str(request.base_url))}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))


@app.post("/api/billing/portal")
def portal(request: Request, c: dict = Depends(owner_ctx)):
    try:
        return {"url": billing.portal_url(c["workspace"], str(request.base_url))}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/billing/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    try:
        etype = billing.handle_webhook(payload, request.headers.get("stripe-signature"))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:  # bad signature / malformed payload
        logging.getLogger("clipviral.billing").warning("webhook rejected: %s", exc)
        raise HTTPException(400, "Invalid webhook")
    return {"received": etype}


# --------------------------------------------------------------------------- social publishing
def _need_scheduler(c: dict) -> None:
    if not accounts.plan_of(_ws(c))["scheduler"]:
        raise HTTPException(402, "Publishing and scheduling are included in the Starter plan and above.")


@app.get("/api/social")
def social_overview(c: dict = Depends(ctx)):
    return {"allowed": accounts.plan_of(_ws(c))["scheduler"],
            "platforms": [{"key": p.key, "label": p.label, "configured": p.configured(), "max_seconds": p.max_seconds}
                          for p in social.PROVIDERS.values()],
            "connections": social.connections(_ws(c))}


def _redirect_uri(request: Request, platform: str) -> str:
    return f"{billing.public_url(str(request.base_url))}/api/social/callback/{platform}"


@app.post("/api/social/connect/{platform}")
def social_connect(platform: str, request: Request, c: dict = Depends(user_ctx)):
    _need_scheduler(c)
    try:
        return {"url": social.start_connect(platform, _ws(c), c["user"]["id"], _redirect_uri(request, platform))}
    except social.SocialError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/social/callback/{platform}")
def social_callback(platform: str, request: Request, code: str = "", state: str = "", error: str = "",
                    error_description: str = ""):
    from urllib.parse import quote
    if platform not in social.PROVIDERS:
        raise HTTPException(404)
    if error or not code:
        msg = error_description or error or "Sign-in was cancelled."
        return RedirectResponse(f"/studio?social_error={quote(msg[:200])}", status_code=303)
    try:
        social.finish_connect(platform, state, code, _redirect_uri(request, platform))
    except social.SocialError as exc:
        return RedirectResponse(f"/studio?social_error={quote(str(exc)[:200])}", status_code=303)
    return RedirectResponse(f"/studio?connected={platform}", status_code=303)


@app.delete("/api/social/connections/{cid}")
def social_disconnect(cid: str, c: dict = Depends(user_ctx)):
    social.disconnect(_ws(c), cid)
    return {"ok": True}


@app.get("/api/social/connections/{cid}/tiktok")
def social_tiktok_options(cid: str, c: dict = Depends(ctx)):
    try:
        return social.tiktok_options(_ws(c), cid)
    except social.SocialError as exc:
        raise HTTPException(400, str(exc))


class PostItem(BaseModel):
    connection_id: str
    caption: str = ""
    title: Optional[str] = None
    options: Optional[dict] = None


class PostIn(BaseModel):
    job_id: str
    clip_id: str
    items: List[PostItem]
    scheduled_at: Optional[float] = None  # unix seconds (UTC); omit to post now


@app.post("/api/posts")
def create_posts(body: PostIn, c: dict = Depends(ctx)):
    _need_scheduler(c)
    _job(body.job_id, c)
    if not body.items:
        raise HTTPException(400, "Choose at least one account.")
    try:
        posts = social.schedule(_ws(c), (c.get("user") or {}).get("id"), body.job_id, _safe(body.clip_id),
                                [i.model_dump() for i in body.items], body.scheduled_at)
    except social.SocialError as exc:
        raise HTTPException(400, str(exc))
    return {"posts": posts}


@app.get("/api/posts")
def get_posts(c: dict = Depends(ctx)):
    return {"posts": social.list_posts(_ws(c))}


@app.delete("/api/posts/{post_id}")
def cancel_post(post_id: str, c: dict = Depends(ctx)):
    if not social.cancel(_ws(c), post_id):
        raise HTTPException(400, "Only scheduled posts that haven't started can be cancelled.")
    return {"ok": True}


@app.get("/media/{token}.mp4")
def public_media(token: str):
    """Short-lived signed link so platforms like Instagram can fetch a clip."""
    got = social.verify_media(token)
    if not got:
        raise HTTPException(404)
    job_id, clip_id = got
    p = jobs.job_dir(_safe(job_id)) / f"{_safe(clip_id)}.mp4"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="video/mp4")


# --------------------------------------------------------------------------- API keys
class KeyIn(BaseModel):
    name: str = "API key"


@app.get("/api/keys")
def keys(c: dict = Depends(owner_ctx)):
    return {"keys": accounts.list_api_keys(_ws(c)), "allowed": accounts.plan_of(_ws(c))["api"]}


@app.post("/api/keys")
def new_key(body: KeyIn, c: dict = Depends(owner_ctx)):
    try:
        info, key = accounts.create_api_key(_ws(c), body.name)
    except accounts.QuotaError as exc:
        raise HTTPException(402, str(exc))
    return {**info, "key": key}


@app.delete("/api/keys/{key_id}")
def del_key(key_id: str, c: dict = Depends(owner_ctx)):
    accounts.revoke_api_key(_ws(c), key_id)
    return {"ok": True}


@app.get("/api/config")
def get_config():
    try:
        import faster_whisper  # noqa: F401
        whisper = True
    except Exception:
        whisper = False
    try:
        import yt_dlp  # noqa: F401
        links = True
    except Exception:
        links = False
    return {"niches": public_niches(), "styles": STYLES, "layouts": LAYOUTS, "aspects": ASPECT_LABELS,
            "max_clip_seconds": render.MAX_CLIP_SECONDS,
            "ai": config.ai_available(), "whisper": whisper, "links": links, "broll": broll.available(),
            "max_video_minutes": config.MAX_VIDEO_SECONDS // 60,
            "whisper_model": config.WHISPER_MODEL,
            "max_upload_gb": round(config.MAX_UPLOAD_BYTES / 1024**3, 1)}


@app.post("/api/jobs")
async def create_job(
    file: Optional[UploadFile] = File(None),
    url: str = Form(""),
    rights: bool = Form(False),
    transcript: Optional[UploadFile] = File(None),
    niche: str = Form("general"),
    num_clips: int = Form(8),
    min_len: float = Form(15),
    max_len: float = Form(60),
    style: str = Form(""),
    layout: str = Form(""),
    accent: str = Form("#FFE14D"),
    position: str = Form("lower"),
    tighten: bool = Form(True),
    progress_bar: bool = Form(True),
    show_hook: bool = Form(True),
    watermark: str = Form(""),
    language: str = Form(""),
    prompt: str = Form(""),
    aspect: str = Form("9:16"),
    emojis: bool = Form(False),
    cut_fillers: bool = Form(True),
    brand_id: str = Form(""),
    broll: bool = Form(False),
    c: dict = Depends(ctx),
):
    usage = accounts.usage_summary(_ws(c))
    if usage["minutes_left"] <= 0:
        raise HTTPException(402, f"You've used all {usage['minutes_limit']} minutes on your "
                                 f"{usage['plan']['label']} plan this month. Upgrade to keep clipping.")
    has_file = file is not None and bool(file.filename)
    url = url.strip()
    if has_file == bool(url):
        raise HTTPException(400, "Upload a video file or paste a link (one of the two).")
    if has_file:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in VIDEO_EXT:
            raise HTTPException(400, f"Unsupported file type {ext or '?'}. Upload MP4, MOV, MKV, WEBM or AVI.")
    else:
        if not rights:
            raise HTTPException(400, "Please confirm you own this video or have permission to use it.")
        try:
            url = await run_in_threadpool(fetch.validate_url, url)
        except fetch.LinkError as exc:
            raise HTTPException(400, str(exc))
    if style and style not in STYLES:
        raise HTTPException(400, "Unknown caption style")
    if layout and layout not in LAYOUTS:
        raise HTTPException(400, "Unknown layout")
    if aspect not in render.ASPECTS:
        raise HTTPException(400, "Unknown aspect ratio")
    if brand_id:
        b = brands.get(brand_id)
        if not b or b.get("workspace_id") != _ws(c):
            raise HTTPException(400, "Brand template not found")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent or ""):
        accent = "#FFE14D"
    num_clips = max(1, min(30, num_clips))
    min_len = max(5, min(render.MAX_CLIP_SECONDS - 10, min_len))
    max_len = max(min_len + 5, min(render.MAX_CLIP_SECONDS, max_len))

    name = Path(file.filename or "video").stem[:80] if has_file else (urlparse(url).hostname or "link")
    job = jobs.create(name, dict(niche=niche, num_clips=num_clips, min_len=min_len, max_len=max_len,
                                 style=style, layout=layout, accent=accent, position=position,
                                 tighten=tighten, progress_bar=progress_bar, show_hook=show_hook,
                                 watermark=watermark, language=language, prompt=prompt,
                                 aspect=aspect, emojis=emojis, cut_fillers=cut_fillers,
                                 brand_id=brand_id or None, broll=broll), workspace_id=_ws(c))
    d = jobs.job_dir(job["id"])
    if not has_file:
        if transcript is not None and transcript.filename:
            text = (await transcript.read())[:20_000_000]
            (d / f"sidecar{Path(transcript.filename).suffix.lower() or '.srt'}").write_bytes(text)
        jobs.update(job["id"], url=url, source=None, status="queued", step="download", progress=0)
        jobs.enqueue(job["id"])
        return jobs.load(job["id"])
    dst = d / f"source{ext}"
    size = 0
    try:
        with dst.open("wb") as out:
            while chunk := await file.read(8 * 1024 * 1024):
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "File is larger than the upload limit.")
                out.write(chunk)
        if transcript is not None and transcript.filename:
            text = (await transcript.read())[:20_000_000]
            (d / f"sidecar{Path(transcript.filename).suffix.lower() or '.srt'}").write_bytes(text)
    except HTTPException:
        jobs.delete(job["id"])
        raise
    jobs.update(job["id"], source=dst.name, status="queued", step="probe", progress=1,
                size_bytes=size)
    jobs.enqueue(job["id"])
    return jobs.load(job["id"])


@app.get("/api/jobs")
def list_jobs(c: dict = Depends(ctx)):
    return jobs.list_jobs(_ws(c))


def _public(job: dict) -> dict:
    j = json.loads(json.dumps(job))
    for c in j.get("clips", []):
        c.pop("track", None)
        for b in c.get("broll") or []:
            b.pop("path", None)  # never expose server paths
    return j


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, c: dict = Depends(ctx)):
    return _public(_job(job_id, c))


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, c: dict = Depends(ctx)):
    _job(job_id, c)
    jobs.delete(job_id)
    return {"ok": True}


class ClipEdit(BaseModel):
    hook: Optional[str] = None
    title: Optional[str] = None
    style: Optional[str] = None
    layout: Optional[str] = None
    accent: Optional[str] = None
    position: Optional[str] = None
    start: Optional[float] = None
    end: Optional[float] = None
    aspect: Optional[str] = None
    emojis: Optional[bool] = None
    crop_x: Optional[float] = None
    cuts: Optional[List[Tuple[float, float]]] = None
    use_broll: Optional[bool] = None


@app.patch("/api/jobs/{job_id}/clips/{clip_id}")
def edit_clip(job_id: str, clip_id: str, edit: ClipEdit, c: dict = Depends(ctx)):
    _job(job_id, c)
    if edit.style and edit.style not in STYLES:
        raise HTTPException(400, "Unknown caption style")
    if edit.layout and edit.layout not in LAYOUTS:
        raise HTTPException(400, "Unknown layout")
    if edit.accent and not re.fullmatch(r"#[0-9a-fA-F]{6}", edit.accent):
        raise HTTPException(400, "Accent must be a hex colour like #FFE14D")
    try:
        clip = jobs.edit_clip(job_id, _safe(clip_id), edit.model_dump(exclude_none=True))
    except StopIteration:
        raise HTTPException(404, "Clip not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    clip.pop("track", None)
    return clip


@app.get("/api/jobs/{job_id}/clips/{clip_id}/words")
def clip_words(job_id: str, clip_id: str, c: dict = Depends(ctx)):
    """Words of a clip (source time) for the text-based editor, with cut flags."""
    job = _job(job_id, c)
    clip = next((x for x in job["clips"] if x["id"] == _safe(clip_id)), None)
    if not clip:
        raise HTTPException(404, "Clip not found")
    words = json.loads((jobs.job_dir(job_id) / "transcript.json").read_text())["words"]
    pad = 20.0  # include context so the user can extend the clip by clicking earlier/later words
    cuts = clip.get("cuts") or []
    out = []
    for w in words:
        if clip["start"] - pad <= w["s"] < clip["end"] + pad:
            mid = (w["s"] + w["e"]) / 2
            out.append({"w": w["w"], "s": w["s"], "e": w["e"],
                        "in": clip["start"] - 0.05 <= w["s"] < clip["end"],
                        "cut": any(a <= mid <= b for a, b in cuts)})
    return {"start": clip["start"], "end": clip["end"], "words": out}


@app.get("/api/jobs/{job_id}/clips/{clip_id}/frame")
def clip_frame(job_id: str, clip_id: str, c: dict = Depends(ctx)):
    """A full-width source frame from the clip, for drag-to-reframe."""
    job = _job(job_id, c)
    if not job.get("source"):
        raise HTTPException(404)
    clip = next((x for x in job["clips"] if x["id"] == _safe(clip_id)), None)
    if not clip:
        raise HTTPException(404, "Clip not found")
    d = jobs.job_dir(job_id)
    out = d / f"{clip_id}.{int(clip['start'] * 10)}.frame.jpg"
    if not out.exists():
        from .pipeline.media import snapshot
        snapshot(d / job["source"], min(clip["end"] - 0.1, clip["start"] + 1.0), out, width=720)
    return FileResponse(out, headers={"Cache-Control": "no-cache"})


@app.get("/api/jobs/{job_id}/clips/{clip_id}/{kind}")
def clip_file(job_id: str, clip_id: str, kind: str, download: int = 0, c: dict = Depends(ctx)):
    ext = {"video": "mp4", "thumb": "jpg", "srt": "srt"}.get(kind)
    if not ext:
        raise HTTPException(404)
    job = _job(job_id, c)
    p = jobs.job_dir(job_id) / f"{_safe(clip_id)}.{ext}"
    if not p.exists():
        raise HTTPException(404, "Not rendered yet")
    clip = next((x for x in job["clips"] if x["id"] == clip_id), {})
    nice = _slug(clip.get("title") or clip_id)
    headers = {"Cache-Control": "no-cache"}
    if download:
        return FileResponse(p, filename=f"{nice}.{ext}", headers=headers)
    return FileResponse(p, headers=headers)


# --------------------------------------------------------------------------- brand templates
class BrandIn(BaseModel):
    name: Optional[str] = None
    accent: Optional[str] = None
    style: Optional[str] = None
    position: Optional[str] = None
    watermark: Optional[str] = None
    aspect: Optional[str] = None
    emojis: Optional[bool] = None
    layout: Optional[str] = None


def _brand_check(b: BrandIn) -> dict:
    data = b.model_dump(exclude_none=True)
    if data.get("style") and data["style"] not in STYLES:
        raise HTTPException(400, "Unknown caption style")
    if data.get("aspect") and data["aspect"] not in render.ASPECTS:
        raise HTTPException(400, "Unknown aspect ratio")
    if data.get("layout") and data["layout"] not in LAYOUTS:
        raise HTTPException(400, "Unknown layout")
    if data.get("position") and data["position"] not in ("lower", "middle", "bottom"):
        raise HTTPException(400, "Unknown caption position")
    return data


def _brand_public(b: dict) -> dict:
    return {**b, "logo_url": f"/api/brands/{b['id']}/logo?v={int(b.get('updated_at', 0))}" if b.get("logo") else None}


@app.get("/api/brands")
def list_brands(c: dict = Depends(ctx)):
    return [_brand_public(b) for b in brands.list_brands(_ws(c))]


@app.post("/api/brands")
def create_brand(b: BrandIn, c: dict = Depends(ctx)):
    limit = accounts.plan_of(_ws(c))["brands"]
    if len(brands.list_brands(_ws(c))) >= limit:
        raise HTTPException(402, f"Your plan includes {limit} brand template(s). Upgrade for more.")
    try:
        return _brand_public(brands.create(_brand_check(b), workspace_id=_ws(c)))
    except brands.BrandError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/brands/{brand_id}")
def update_brand(brand_id: str, b: BrandIn, c: dict = Depends(ctx)):
    _brand(brand_id, c)
    try:
        return _brand_public(brands.update(brand_id, _brand_check(b)))
    except brands.BrandError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/brands/{brand_id}")
def delete_brand(brand_id: str, c: dict = Depends(ctx)):
    _brand(brand_id, c)
    try:
        brands.delete(brand_id)
    except brands.BrandError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/brands/{brand_id}/{kind}")
async def upload_brand_asset(brand_id: str, kind: str, file: UploadFile = File(...), c: dict = Depends(ctx)):
    _brand(brand_id, c)
    data = await file.read(brands.MAX_FONT_BYTES + 1)
    try:
        if kind == "logo":
            b = brands.set_logo(brand_id, data)
        elif kind == "font":
            b = brands.set_font(brand_id, data, file.filename or "")
        else:
            raise HTTPException(404)
    except brands.BrandError as exc:
        raise HTTPException(400, str(exc))
    return _brand_public(b)


@app.delete("/api/brands/{brand_id}/{kind}")
def clear_brand_asset(brand_id: str, kind: str, c: dict = Depends(ctx)):
    _brand(brand_id, c)
    if kind not in ("logo", "font"):
        raise HTTPException(404)
    try:
        return _brand_public(brands.clear_asset(brand_id, kind))
    except brands.BrandError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/brands/{brand_id}/logo")
def brand_logo(brand_id: str, c: dict = Depends(ctx)):
    b = _brand(brand_id, c)
    if not b.get("logo"):
        raise HTTPException(404)
    return FileResponse(brands.BRANDS_DIR / brand_id / "logo.png")


@app.get("/api/jobs/{job_id}/export.zip")
def export_zip(job_id: str, c: dict = Depends(ctx)):
    job = _job(job_id, c)
    d = jobs.job_dir(job_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        posts = []
        for c in job["clips"]:  # noqa: B020 (shadows the auth ctx only inside this loop)
            if c.get("status") != "ready":
                continue
            base = f"{c['rank']:02d}-{_slug(c['title'])}"
            z.write(d / f"{c['id']}.mp4", f"{base}.mp4")
            z.write(d / f"{c['id']}.srt", f"{base}.srt")
            cap = c.get("captions", {})
            posts.append(f"## {c['rank']}. {c['title']}  (score {c['virality']})\n"
                         f"Hook: {c['hook']}\nWhy: {c['why']}\n\n"
                         f"TikTok:\n{cap.get('tiktok', '')}\n\nInstagram:\n{cap.get('instagram', '')}\n\n"
                         f"YouTube Shorts:\n{cap.get('youtube', '')}\n\n"
                         f"X (Twitter):\n{cap.get('x', '')}\n"
                         + ("\nB-roll (Pexels): " + ", ".join(
                             f"{b.get('credit', {}).get('name', 'Pexels')} {b.get('url') or ''}".strip()
                             for b in c.get("broll") or []) + "\n" if c.get("use_broll") and c.get("broll") else ""))
        z.writestr("POST-CAPTIONS.md", f"# {job['name']}\n\n" + "\n---\n\n".join(posts))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{_slug(job["name"])}-clips.zip"'})


def _safe(s: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", s):
        raise HTTPException(400, "Bad id")
    return s


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50] or "clip"
