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
from . import brands
from .pipeline import broll, fetch, highlights, media, render, transcribe, visual
from .pipeline.niches import get_niche

log = logging.getLogger("clipviral.jobs")
_lock = threading.RLock()
_q: "queue.Queue[tuple[str, str, str | None]]" = queue.Queue()

STEPS = [
    ("upload", "Uploaded"),
    ("download", "Downloading video"),
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
        "aspect": options.get("aspect") if options.get("aspect") in render.ASPECTS else "9:16",
        "emojis": bool(options.get("emojis", False)),
        "cut_fillers": bool(options.get("cut_fillers", True)),
        "brand_id": options.get("brand_id") or None,
        "broll": bool(options.get("broll", False)),
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

    if not job.get("source"):  # imported from a link
        _set(job_id, "download", 1)
        for stale in d.glob("source.*"):
            stale.unlink()
        got = fetch.download(job["url"], d, on_progress=lambda f: _set(job_id, "download", 1 + 14 * f))
        job = update(job_id, source=got["path"].name, name=got["title"] or job["name"],
                     size_bytes=got["path"].stat().st_size, platform=got["extractor"])
    src = d / job["source"]

    _set(job_id, "probe", 16 if job.get("url") else 3)
    meta = media.probe(src)
    update(job_id, meta=meta, duration=meta["duration"])

    # transcript
    tpath = d / "transcript.json"
    if not tpath.exists():
        t0 = 17 if job.get("url") else 5
        _set(job_id, "transcribe", t0)
        sidecar = next(iter(d.glob("sidecar.*")), None)
        if sidecar is not None:
            tr = transcribe.parse_subtitles(sidecar.read_text(errors="ignore"))
        elif meta["has_audio"]:
            wav = d / "audio.wav"
            media.extract_audio(src, wav)
            tr = transcribe.whisper_transcribe(
                wav, meta["duration"], opts.get("language"),
                on_progress=lambda f: _set(job_id, "transcribe", t0 + (50 - t0) * f))
        else:
            tr = {"language": None, "words": [], "source": "no-audio"}  # silent video: visual mode
        tpath.write_text(json.dumps(tr))
    tr = json.loads(tpath.read_text())
    words = tr["words"]

    # analysis
    _set(job_id, "analyze", 52)
    curve = None
    if meta["has_audio"]:
        wav = d / "audio.wav"
        if not wav.exists():
            media.extract_audio(src, wav)
        curve = media.loudness_curve(wav)
    notes = []
    minutes = max(meta["duration"] / 60, 0.1)
    visual_mode = len(words) < 8 or len(words) / minutes < 25  # little or no speech
    if visual_mode:
        notes.append("Little speech detected: moments were picked from on-screen action and sound.")
        picks = visual.find_visual_moments(src, d, meta["duration"], curve, words, opts["niche"],
                                           opts["num_clips"], opts["min_len"], opts["max_len"],
                                           opts.get("prompt", ""), on_note=notes.append)
    else:
        sents = transcribe.sentences(words)
        if curve is None:  # transcript supplied for a silent video: flat loudness
            import numpy as np
            curve = np.full(int(meta["duration"] / 0.25) + 1, -30.0)
        picks = highlights.find_highlights(sents, words, curve, opts["niche"], opts["num_clips"],
                                           opts["min_len"], opts["max_len"], opts.get("prompt", ""),
                                           on_note=notes.append)
    if not picks:
        raise RuntimeError("No suitable moments found. Try a shorter minimum clip length.")
    clips = []
    for i, p in enumerate(picks):
        clips.append({**p, "id": f"c{i + 1:02d}", "status": "queued", "rank": i + 1,
                      "transcript": " ".join(w["w"] for w in words if p["start"] - 0.05 <= w["s"] < p["end"]),
                      "style": opts["style"], "layout": opts["layout"], "accent": opts["accent"],
                      "position": opts["position"], "aspect": opts.get("aspect", "9:16"),
                      "emojis": opts.get("emojis", False), "cuts": [], "track": None,
                      "use_broll": bool(opts.get("broll")), "broll": None})
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
    opts.update(brands.render_opts(opts.get("brand_id")))
    try:
        if clip.get("use_broll") and clip.get("broll") is None:
            if broll.available():
                rel = render.clip_words(words, clip["start"], clip["end"])
                try:
                    clip["broll"] = broll.build(rel, clip["start"], opts.get("niche", "general"),
                                                render.output_size(clip.get("aspect")))
                except Exception as exc:  # never fail a render because of stock footage
                    log.warning("B-roll failed: %s", exc)
                    clip["broll"] = []
            else:
                clip["broll"] = []
            _patch_clip(job_id, clip_id, broll=clip["broll"])
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
    allowed = {"hook", "title", "style", "layout", "accent", "position", "start", "end", "emphasis",
               "aspect", "emojis", "crop_x", "cuts", "use_broll"}
    fields = {k: v for k, v in changes.items() if k in allowed and v is not None}
    if "aspect" in fields:
        if fields["aspect"] not in render.ASPECTS:
            raise ValueError("Unknown aspect ratio.")
        if fields["aspect"] != clip.get("aspect", "9:16"):
            fields["track"] = None  # crop width changes with the shape
            if clip.get("use_broll"):
                fields["broll"] = None  # stock footage orientation changes too
    if "crop_x" in fields:
        fields["crop_x"] = round(min(1.0, max(0.0, float(fields["crop_x"]))), 4)
        fields.setdefault("layout", "manual")
    if "emojis" in fields:
        fields["emojis"] = bool(fields["emojis"])
    if "use_broll" in fields:
        fields["use_broll"] = bool(fields["use_broll"])
        if fields["use_broll"] and not clip.get("broll"):
            fields["broll"] = None  # (re)plan cutaways on the next render
    if ("start" in changes or "end" in changes) and clip.get("use_broll"):
        fields["broll"] = None  # the words changed: plan new cutaways
    if "cuts" in fields:
        cuts = []
        for pair in list(fields["cuts"])[:500]:
            a, b = float(pair[0]), float(pair[1])
            if b > a:
                cuts.append([round(a, 3), round(b, 3)])
        fields["cuts"] = sorted(cuts)
    if "start" in fields or "end" in fields:
        s = max(0.0, float(fields.get("start", clip["start"])))
        e = min(float(job["meta"]["duration"]), float(fields.get("end", clip["end"])))
        if e - s < 3:
            raise ValueError("Clips must be at least 3 seconds long.")
        if e - s > render.MAX_CLIP_SECONDS:
            raise ValueError("Clips can be at most 15 minutes long.")
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
