"""Job persistence (one folder + job.json per upload) and a background worker
queue that runs the pipeline."""
from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import config
from .pipeline import highlights, media, render, transcribe
from .pipeline.niches import get_niche

log = logging.getLogger("clipviral.jobs")
_lock = threading.RLock()
_q: "queue.Queue[tuple[str, str, str | None]]" = queue.Queue()

STEPS = [
    ("upload", "Uploaded"),
    ("probe", "Reading video"),
    ("transcribe", "Transcribing every word"),
    ("analyze", "Finding viral moments"),
    ("render", "Reframing, captioning & rendering"),
    ("done", "Ready"),
]


def job_dir(job_id: str) -> Path:
    return config.JOBS_DIR / job_id


def load(job_id: str) -> dict | None:
    p = job_dir(job_id) / "job.json"
    if not p.exists():
        return None
    with _lock:
        return json.loads(p.read_text())


def save(job: dict) -> None:
    with _lock:
        job["updated_at"] = time.time()
        p = job_dir(job["id"]) / "job.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, indent=1))
        tmp.replace(p)


def update(job_id: str, **fields) -> dict:
    with _lock:
        job = load(job_id)
        job.update(fields)
        save(job)
        return job


def list_jobs() -> list[dict]:
    out = []
    for p in config.JOBS_DIR.glob("*/job.json"):
        try:
            j = json.loads(p.read_text())
        except Exception:
            continue
        out.append({k: j.get(k) for k in ("id", "name", "status", "step", "progress",
                                         "created_at", "niche", "duration", "error")}
                   | {"clip_count": len(j.get("clips", [])),
                      "thumb": next((c["id"] for c in j.get("clips", []) if c.get("status") == "ready"), None)})
    return sorted(out, key=lambda j: j.get("created_at") or 0, reverse=True)


def delete(job_id: str) -> None:
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def create(name: str, options: dict) -> dict:
    job_id = uuid.uuid4().hex[:12]
    job_dir(job_id).mkdir(parents=True)
    niche = get_niche(options.get("niche"))
    opts = {
        "niche": options.get("niche") or "general",
        "num_clips": int(options.get("num_clips") or 8),
        "min_len": float(options.get("min_len") or 15),
        "max_len": float(options.get("max_len") or 60),
        "style": options.get("style") or niche["style"],
        "layout": options.get("layout") or niche["layout"],
        "accent": options.get("accent") or "#FFE14D",
        "position": options.get("position") or "lower",
        "tighten": bool(options.get("tighten", True)),
        "progress_bar": bool(options.get("progress_bar", True)),
        "show_hook": bool(options.get("show_hook", True)),
        "watermark": (options.get("watermark") or "")[:40],
        "language": options.get("language") or None,
        "prompt": (options.get("prompt") or "")[:500],
    }
    opts["max_len"] = max(opts["max_len"], opts["min_len"] + 5)
    job = {"id": job_id, "name": name, "status": "uploading", "step": "upload", "progress": 0,
           "created_at": time.time(), "niche": opts["niche"], "options": opts, "clips": [],
           "notes": [], "error": None}
    save(job)
    return job


def enqueue(job_id: str, task: str = "process", clip_id: str | None = None) -> None:
    _q.put((job_id, task, clip_id))


# --------------------------------------------------------------------------- pipeline
def _set(job_id: str, step: str, progress: float, status: str = "processing") -> None:
    update(job_id, step=step, progress=round(progress, 1), status=status)


def process(job_id: str) -> None:
    d = job_dir(job_id)
    job = load(job_id)
    opts = job["options"]
    src = d / job["source"]

    _set(job_id, "probe", 3)
    meta = media.probe(src)
    update(job_id, meta=meta, duration=meta["duration"])

    # transcript
    tpath = d / "transcript.json"
    if not tpath.exists():
        _set(job_id, "transcribe", 5)
        sidecar = next(iter(d.glob("sidecar.*")), None)
        if sidecar is not None:
            tr = transcribe.parse_subtitles(sidecar.read_text(errors="ignore"))
        elif meta["has_audio"]:
            wav = d / "audio.wav"
            media.extract_audio(src, wav)
            tr = transcribe.whisper_transcribe(
                wav, meta["duration"], opts.get("language"),
                on_progress=lambda f: _set(job_id, "transcribe", 5 + 45 * f))
        else:
            raise RuntimeError("This video has no audio track, so there is nothing to transcribe.")
        tpath.write_text(json.dumps(tr))
    tr = json.loads(tpath.read_text())
    words = tr["words"]
    if len(words) < 8:
        raise RuntimeError("We couldn't hear enough speech in this video to find highlights.")

    # analysis
    _set(job_id, "analyze", 52)
    wav = d / "audio.wav"
    if not wav.exists():
        media.extract_audio(src, wav)
    curve = media.loudness_curve(wav)
    sents = transcribe.sentences(words)
    notes = []
    picks = highlights.find_highlights(sents, words, curve, opts["niche"], opts["num_clips"],
                                       opts["min_len"], opts["max_len"], opts.get("prompt", ""),
                                       on_note=notes.append)
    if not picks:
        raise RuntimeError("No suitable moments found. Try a shorter minimum clip length.")
    clips = []
    for i, p in enumerate(picks):
        clips.append({**p, "id": f"c{i + 1:02d}", "status": "queued", "rank": i + 1,
                      "transcript": " ".join(s["text"] for s in sents[p["s_idx"]:p["e_idx"] + 1]),
                      "style": opts["style"], "layout": opts["layout"], "accent": opts["accent"],
                      "position": opts["position"], "track": None})
    update(job_id, clips=clips, notes=job.get("notes", []) + notes,
           engine=picks[0].get("engine"), transcript_source=tr.get("source"))

    # render
    for i, c in enumerate(clips):
        _set(job_id, "render", 55 + 44 * i / len(clips))
        _render_one(job_id, c["id"], words, meta)
    final = load(job_id)
    errors = [c for c in final["clips"] if c.get("status") == "error"]
    if errors and len(errors) == len(final["clips"]):
        raise RuntimeError("Rendering failed for every clip: " + (errors[0].get("error") or "")[-300:])
    _set(job_id, "done", 100, status="done")


def _render_one(job_id: str, clip_id: str, words: list[dict] | None = None, meta: dict | None = None) -> None:
    d = job_dir(job_id)
    job = load(job_id)
    if words is None:
        words = json.loads((d / "transcript.json").read_text())["words"]
    meta = meta or job["meta"]
    clip = next(c for c in job["clips"] if c["id"] == clip_id)
    _patch_clip(job_id, clip_id, status="rendering")
    opts = {**job["options"], "style": clip["style"], "layout": clip["layout"],
            "accent": clip["accent"], "position": clip["position"]}
    try:
        info = render.render_clip(d, d / job["source"], meta, clip, words, opts)
        _patch_clip(job_id, clip_id, status="ready", track=clip.get("track"),
                    version=int(clip.get("version", 0)) + 1, error=None, **info)
    except Exception as exc:
        log.exception("render failed")
        _patch_clip(job_id, clip_id, status="error", error=str(exc)[-400:])


def _patch_clip(job_id: str, clip_id: str, **fields) -> dict:
    with _lock:
        job = load(job_id)
        for c in job["clips"]:
            if c["id"] == clip_id:
                c.update(fields)
                save(job)
                return c
    raise KeyError(clip_id)


def edit_clip(job_id: str, clip_id: str, changes: dict) -> dict:
    """Apply user edits; re-render if anything visual changed."""
    job = load(job_id)
    clip = next(c for c in job["clips"] if c["id"] == clip_id)
    allowed = {"hook", "title", "style", "layout", "accent", "position", "start", "end", "emphasis"}
    fields = {k: v for k, v in changes.items() if k in allowed and v is not None}
    if "start" in fields or "end" in fields:
        s = max(0.0, float(fields.get("start", clip["start"])))
        e = min(float(job["meta"]["duration"]), float(fields.get("end", clip["end"])))
        if e - s < 3:
            raise ValueError("Clips must be at least 3 seconds long.")
        if e - s > 180:
            raise ValueError("Clips can be at most 3 minutes long.")
        fields["start"], fields["end"] = round(s, 3), round(e, 3)
        fields["track"] = None  # re-plan the camera path for the new range
        words = json.loads((job_dir(job_id) / "transcript.json").read_text())["words"]
        fields["transcript"] = " ".join(w["w"] for w in words if s <= w["s"] < e)
    if "hook" in fields:
        fields["hook"] = str(fields["hook"])[:80]
    clip = _patch_clip(job_id, clip_id, **fields, status="queued")
    enqueue(job_id, "render", clip_id)
    return clip


def _worker() -> None:
    while True:
        job_id, task, clip_id = _q.get()
        try:
            if load(job_id) is None:
                continue
            if task == "process":
                process(job_id)
            elif task == "render" and clip_id:
                _render_one(job_id, clip_id)
        except Exception as exc:
            log.error("job %s failed: %s\n%s", job_id, exc, traceback.format_exc())
            try:
                update(job_id, status="error", error=str(exc)[-600:])
            except Exception:
                pass
        finally:
            _q.task_done()


def start_workers() -> None:
    for _ in range(max(1, config.WORKERS)):
        threading.Thread(target=_worker, daemon=True).start()
    # resume anything interrupted by a restart
    for j in list_jobs():
        if j["status"] in ("queued", "processing"):
            enqueue(j["id"])
