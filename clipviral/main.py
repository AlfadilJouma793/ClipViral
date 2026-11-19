"""ClipViral web server.

Run:  uvicorn clipviral.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, jobs
from .pipeline import fetch
from .pipeline.captions import STYLES
from .pipeline.niches import public_niches

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
LAYOUTS = {"auto": "Auto (follow speaker, else fit)", "smart": "Follow speaker",
           "fit": "Fit + blurred background", "center": "Centre crop"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    jobs.start_workers()
    yield


app = FastAPI(title="ClipViral", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def landing():
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/studio", response_class=HTMLResponse)
def studio():
    return FileResponse(config.WEB_DIR / "studio.html")


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
    return {"niches": public_niches(), "styles": STYLES, "layouts": LAYOUTS,
            "ai": config.ai_available(), "whisper": whisper, "links": links,
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
):
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
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent or ""):
        accent = "#FFE14D"
    num_clips = max(1, min(30, num_clips))
    min_len = max(5, min(170, min_len))
    max_len = max(min_len + 5, min(180, max_len))

    name = Path(file.filename or "video").stem[:80] if has_file else (urlparse(url).hostname or "link")
    job = jobs.create(name, dict(niche=niche, num_clips=num_clips, min_len=min_len, max_len=max_len,
                                 style=style, layout=layout, accent=accent, position=position,
                                 tighten=tighten, progress_bar=progress_bar, show_hook=show_hook,
                                 watermark=watermark, language=language, prompt=prompt))
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
def list_jobs():
    return jobs.list_jobs()


def _public(job: dict) -> dict:
    j = json.loads(json.dumps(job))
    for c in j.get("clips", []):
        c.pop("track", None)
    return j


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.load(_safe(job_id))
    if not job:
        raise HTTPException(404, "Job not found")
    return _public(job)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    jobs.delete(_safe(job_id))
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


@app.patch("/api/jobs/{job_id}/clips/{clip_id}")
def edit_clip(job_id: str, clip_id: str, edit: ClipEdit):
    job = jobs.load(_safe(job_id))
    if not job:
        raise HTTPException(404, "Job not found")
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


@app.get("/api/jobs/{job_id}/clips/{clip_id}/{kind}")
def clip_file(job_id: str, clip_id: str, kind: str, download: int = 0):
    ext = {"video": "mp4", "thumb": "jpg", "srt": "srt"}.get(kind)
    if not ext:
        raise HTTPException(404)
    p = jobs.job_dir(_safe(job_id)) / f"{_safe(clip_id)}.{ext}"
    if not p.exists():
        raise HTTPException(404, "Not rendered yet")
    job = jobs.load(job_id)
    clip = next((c for c in job["clips"] if c["id"] == clip_id), {})
    nice = _slug(clip.get("title") or clip_id)
    headers = {"Cache-Control": "no-cache"}
    if download:
        return FileResponse(p, filename=f"{nice}.{ext}", headers=headers)
    return FileResponse(p, headers=headers)


@app.get("/api/jobs/{job_id}/export.zip")
def export_zip(job_id: str):
    job = jobs.load(_safe(job_id))
    if not job:
        raise HTTPException(404)
    d = jobs.job_dir(job_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        posts = []
        for c in job["clips"]:
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
                         f"X (Twitter):\n{cap.get('x', '')}\n")
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
