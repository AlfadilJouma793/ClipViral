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
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import brands, config, jobs
from .pipeline import fetch, render
from .pipeline.captions import STYLES
from .pipeline.niches import public_niches

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
LAYOUTS = {"auto": "Auto (follow speaker or subject)", "smart": "Follow speaker or subject",
           "fit": "Fit + blurred background", "center": "Centre crop", "manual": "Manual position"}
ASPECT_LABELS = {"9:16": "9:16 vertical (TikTok, Reels, Shorts)", "1:1": "1:1 square",
                 "4:5": "4:5 portrait (Instagram feed)", "16:9": "16:9 landscape (YouTube, X)"}


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
    return {"niches": public_niches(), "styles": STYLES, "layouts": LAYOUTS, "aspects": ASPECT_LABELS,
            "max_clip_seconds": render.MAX_CLIP_SECONDS,
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
    aspect: str = Form("9:16"),
    emojis: bool = Form(False),
    cut_fillers: bool = Form(True),
    brand_id: str = Form(""),
    broll: bool = Form(False),
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
    if aspect not in render.ASPECTS:
        raise HTTPException(400, "Unknown aspect ratio")
    if brand_id and not brands.get(brand_id):
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
                                 brand_id=brand_id or None, broll=broll))
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
    aspect: Optional[str] = None
    emojis: Optional[bool] = None
    crop_x: Optional[float] = None
    cuts: Optional[List[Tuple[float, float]]] = None


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


@app.get("/api/jobs/{job_id}/clips/{clip_id}/words")
def clip_words(job_id: str, clip_id: str):
    """Words of a clip (source time) for the text-based editor, with cut flags."""
    job = jobs.load(_safe(job_id))
    if not job:
        raise HTTPException(404)
    clip = next((c for c in job["clips"] if c["id"] == _safe(clip_id)), None)
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
def clip_frame(job_id: str, clip_id: str):
    """A full-width source frame from the clip, for drag-to-reframe."""
    job = jobs.load(_safe(job_id))
    if not job or not job.get("source"):
        raise HTTPException(404)
    clip = next((c for c in job["clips"] if c["id"] == _safe(clip_id)), None)
    if not clip:
        raise HTTPException(404, "Clip not found")
    d = jobs.job_dir(job_id)
    out = d / f"{clip_id}.{int(clip['start'] * 10)}.frame.jpg"
    if not out.exists():
        from .pipeline.media import snapshot
        snapshot(d / job["source"], min(clip["end"] - 0.1, clip["start"] + 1.0), out, width=720)
    return FileResponse(out, headers={"Cache-Control": "no-cache"})


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
def list_brands():
    return [_brand_public(b) for b in brands.list_brands()]


@app.post("/api/brands")
def create_brand(b: BrandIn):
    try:
        return _brand_public(brands.create(_brand_check(b)))
    except brands.BrandError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/brands/{brand_id}")
def update_brand(brand_id: str, b: BrandIn):
    try:
        return _brand_public(brands.update(brand_id, _brand_check(b)))
    except brands.BrandError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/brands/{brand_id}")
def delete_brand(brand_id: str):
    try:
        brands.delete(brand_id)
    except brands.BrandError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/brands/{brand_id}/{kind}")
async def upload_brand_asset(brand_id: str, kind: str, file: UploadFile = File(...)):
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
def clear_brand_asset(brand_id: str, kind: str):
    if kind not in ("logo", "font"):
        raise HTTPException(404)
    try:
        return _brand_public(brands.clear_asset(brand_id, kind))
    except brands.BrandError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/brands/{brand_id}/logo")
def brand_logo(brand_id: str):
    b = brands.get(brand_id)
    if not b or not b.get("logo"):
        raise HTTPException(404)
    return FileResponse(brands.BRANDS_DIR / brand_id / "logo.png")


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
