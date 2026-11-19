"""Single-pass ffmpeg render of one clip (any aspect ratio)."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .. import config
from .captions import _FILLER, build_ass, build_srt, caption_margin
from .media import snapshot
from .reframe import crop_x_expr, plan_path, track_subject

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# Output shapes: name -> (width, height)
ASPECTS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),   # TikTok, Reels, Shorts
    "1:1": (1080, 1080),    # feed posts
    "4:5": (1080, 1350),    # Instagram / Facebook feed
    "16:9": (1920, 1080),   # YouTube, X, LinkedIn
}
MAX_CLIP_SECONDS = 15 * 60


def output_size(aspect: str | None) -> tuple[int, int]:
    return ASPECTS.get(aspect or "9:16", ASPECTS["9:16"])


# --------------------------------------------------------------------------- timeline helpers
def keep_intervals(words: list[dict], duration: float, max_gap: float = 0.55,
                   pad: float = 0.12) -> list[tuple[float, float]]:
    """Segments of the clip to keep when tightening dead air (clip-relative)."""
    if not words:
        return [(0.0, duration)]
    spans: list[list[float]] = []
    cur = [0.0, min(duration, words[0]["e"] + pad)]
    for w in words[1:]:
        s, e = max(0.0, w["s"] - pad), min(duration, w["e"] + pad)
        if s - cur[1] > max_gap:
            spans.append(cur)
            cur = [s, e]
        else:
            cur[1] = max(cur[1], e)
    cur[1] = duration
    spans.append(cur)
    return [(round(a, 3), round(b, 3)) for a, b in spans if b - a > 0.05]


def subtract(keep: list[tuple[float, float]], removed: list[tuple[float, float]],
             min_len: float = 0.05) -> list[tuple[float, float]]:
    """keep minus removed (both lists of [start, end) intervals)."""
    out = []
    for a, b in keep:
        segs = [(a, b)]
        for ra, rb in removed:
            nxt = []
            for s, e in segs:
                if rb <= s or ra >= e:
                    nxt.append((s, e))
                    continue
                if ra > s:
                    nxt.append((s, ra))
                if rb < e:
                    nxt.append((rb, e))
            segs = nxt
        out.extend((round(s, 3), round(e, 3)) for s, e in segs if e - s >= min_len)
    return out


def filler_spans(words: list[dict]) -> list[tuple[float, float]]:
    """Time ranges of filler words ("um", "uh"...) to cut from audio and video."""
    return [(max(0.0, w["s"] - 0.02), w["e"] + 0.04) for w in words if _FILLER.match(w["w"])]


def remap(words: list[dict], keep: list[tuple[float, float]]) -> list[dict]:
    out, offset_list, acc = [], [], 0.0
    for a, b in keep:
        offset_list.append((a, b, acc))
        acc += b - a

    def m(t: float) -> float:
        for a, b, base in offset_list:
            if t < a:
                return base
            if t <= b:
                return base + (t - a)
        return acc

    for w in words:
        s, e = m(w["s"]), m(w["e"])
        if e - s > 0.02:
            out.append({**w, "s": round(s, 3), "e": round(e, 3)})
    return out


def clip_words(all_words: list[dict], start: float, end: float) -> list[dict]:
    return [{"w": w["w"], "s": round(w["s"] - start, 3), "e": round(min(w["e"], end) - start, 3)}
            for w in all_words if w["s"] >= start - 0.05 and w["s"] < end]


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


# --------------------------------------------------------------------------- render
def render_clip(job_dir: Path, src: Path, meta: dict, clip: dict, words: list[dict],
                opts: dict) -> dict:
    """Render clip -> mp4 + jpg + srt + ass. Returns updated file info."""
    cid = clip["id"]
    start, end = float(clip["start"]), float(clip["end"])
    dur = end - start
    cw = clip_words(words, start, end)
    W, H = output_size(clip.get("aspect") or opts.get("aspect"))
    fps = config.FPS

    # ---------------------------------------------------------------- framing
    layout = clip.get("layout") or opts.get("layout", "auto")
    src_w, src_h = meta["width"], meta["height"]
    target = W / H
    src_ratio = src_w / src_h
    crop_w, crop_h = src_w, src_h
    if src_ratio > target * 1.01:
        crop_w = min(src_w, _even(src_h * target))
    elif src_ratio < target / 1.01:
        crop_h = min(src_h, _even(src_w / target))
    needs_pan = crop_w < src_w
    keys = None
    used = layout
    if layout == "fit":
        used = "fit"
    elif not needs_pan:
        # source is the same shape or taller than the output: crop height (upper-middle keeps heads)
        used = "fill" if crop_h < src_h else "scale"
    elif layout == "manual":
        used = "manual"
    elif layout in ("auto", "smart"):
        if clip.get("track") is None:
            clip["track"] = plan_path(track_subject(src, start, end), dur, crop_w / src_w)
        keys = clip["track"]
        used = "smart" if keys else ("fit" if layout == "auto" else "center")
    else:
        used = "center"

    scale = f"scale={W}:{H}:flags=lanczos,setsar=1,fps={fps}"
    if used == "smart":
        x = crop_x_expr([tuple(k) for k in keys], src_w, crop_w)
        vchain = f"[0:v]crop=w={crop_w}:h={src_h}:x='{x}':y=0,{scale}[v0]"
    elif used == "manual":
        cx = min(1.0, max(0.0, float(clip.get("crop_x", 0.5))))
        x = max(0, min(src_w - crop_w, int(cx * src_w - crop_w / 2)))
        vchain = f"[0:v]crop=w={crop_w}:h={src_h}:x={x}:y=0,{scale}[v0]"
    elif used == "center":
        vchain = f"[0:v]crop=w={crop_w}:h={src_h}:x=(iw-{crop_w})/2:y=0,{scale}[v0]"
    elif used == "fill":
        vchain = f"[0:v]crop=w={src_w}:h={crop_h}:x=0:y=(ih-{crop_h})*0.35,{scale}[v0]"
    elif used == "scale":
        vchain = f"[0:v]{scale}[v0]"
    else:  # fit: full frame over a blurred, zoomed copy of itself
        bw, bh = _even(W / 4), _even(H / 4)
        lift = int(H * 0.06) if H > W else 0
        vchain = (f"[0:v]split[a][b];"
                  f"[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=10:2,"
                  f"eq=brightness=-0.10:saturation=1.2,scale={W}:{H}[bg];"
                  f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
                  f"[bg][fg]overlay=(W-w)/2:(H-h)/2-{lift},setsar=1,fps={fps}[v0]")

    # ---------------------------------------------------------------- timeline edits
    keep = keep_intervals(cw, dur) if opts.get("tighten", True) else [(0.0, dur)]
    removed: list[tuple[float, float]] = []
    if opts.get("cut_fillers", True):
        removed += filler_spans(cw)
    for a, b in clip.get("cuts") or []:  # text-based edits, stored in source time
        ra, rb = max(0.0, float(a) - start), min(dur, float(b) - start)
        if rb > ra:
            removed.append((ra, rb))
    if removed:
        keep = subtract(keep, sorted(removed))
    if not keep:
        raise RuntimeError("Every part of this clip was cut. Restore some words and try again.")
    edited = keep != [(0.0, round(dur, 3))]
    if edited:
        sel = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep)
        vchain += f";[v0]select='{sel}',setpts=N/({fps}*TB)[v1]"
        out_words = remap(cw, keep)
        out_dur = sum(b - a for a, b in keep)
    else:
        vchain += ";[v0]null[v1]"
        out_words, out_dur = cw, dur

    # ---------------------------------------------------------------- captions + overlays
    ass = job_dir / f"{cid}.ass"
    position = clip.get("position") or opts.get("position", "lower")
    emoji_events = build_ass(
        out_words, out_dur, ass, style=clip.get("style") or opts.get("style", "bold"),
        accent=clip.get("accent") or opts.get("accent", "#FFE14D"), position=position,
        hook=clip.get("hook", "") if opts.get("show_hook", True) else "",
        emphasis=clip.get("emphasis"), progress_bar=opts.get("progress_bar", True),
        watermark=opts.get("watermark", ""), size=(W, H), font=opts.get("font", ""),
        emojis=bool(clip.get("emojis", opts.get("emojis", False))))
    build_srt(out_words, job_dir / f"{cid}.srt")

    ass_opts = ass.name
    fontsdir = opts.get("fontsdir")
    if fontsdir and Path(fontsdir).is_dir():
        rel = os.path.relpath(fontsdir, job_dir).replace("\\", "/")
        if re.fullmatch(r"[\w./-]+", rel):
            ass_opts += f":fontsdir={rel}"
    vchain += f";[v1]ass={ass_opts}[v2]"

    inputs: list[str] = []
    last = "v2"
    n_in = 1
    # colour emoji pop-ups above the captions
    size = int(min(W, H) * 0.13)
    ey = max(10, H - caption_margin(position, H) - int(min(W, H) * 0.11) - size)
    for i, (a, b, code) in enumerate(emoji_events):
        png = ASSETS / "emoji" / f"{code}.png"
        if not png.exists():
            continue
        inputs += ["-i", str(png)]
        vchain += (f";[{n_in}:v]scale={size}:{size},format=rgba[e{i}];"
                   f"[{last}][e{i}]overlay=x=(W-w)/2:y={ey}:enable='between(t,{a:.3f},{b:.3f})'[o{i}]")
        last = f"o{i}"
        n_in += 1
    # brand logo (top-left; text watermark sits top-right)
    logo = opts.get("logo")
    if logo and Path(logo).exists():
        lw = _even(W * 0.16)
        inputs += ["-i", str(logo)]
        vchain += (f";[{n_in}:v]scale={lw}:-2,format=rgba,colorchannelmixer=aa=0.92[logo];"
                   f"[{last}][logo]overlay=x=44:y=44[olog]")
        last = "olog"
        n_in += 1
    vchain += f";[{last}]null[vout]"

    graph = vchain
    maps = ["-map", "[vout]"]
    if meta.get("has_audio"):
        achain = "[0:a]"
        if edited:
            asel = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep)
            achain += f"aselect='{asel}',asetpts=N/SR/TB,"
        achain += "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]"
        graph += ";" + achain
        maps += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]

    out = job_dir / f"{cid}.mp4"
    tmp = job_dir / f"{cid}.tmp.mp4"
    script = job_dir / f"{cid}.filter"
    script.write_text(graph, encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
           *inputs, "-filter_complex_script", script.name, *maps,
           "-c:v", "libx264", "-preset", config.X264_PRESET, "-crf", config.X264_CRF,
           "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart",
           "-t", f"{out_dur:.3f}", str(tmp.name)]
    proc = subprocess.run(cmd, cwd=job_dir, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg render failed: " + "\n".join(proc.stderr.splitlines()[-12:]))
    tmp.replace(out)
    snapshot(out, min(1.2, out_dur / 2), job_dir / f"{cid}.jpg", width=360)
    return {"layout_used": used, "duration": round(out_dur, 2), "size": [W, H],
            "source_duration": round(dur, 2), "tightened_by": round(dur - out_dur, 2),
            "emoji_count": len(emoji_events)}
