"""Single-pass ffmpeg render of one vertical clip."""
from __future__ import annotations

from pathlib import Path

from .. import config
from .captions import build_ass, build_srt
from .media import snapshot
from .reframe import crop_x_expr, plan_path, track_faces


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


def render_clip(job_dir: Path, src: Path, meta: dict, clip: dict, words: list[dict],
                opts: dict) -> dict:
    """Render clip -> mp4 + jpg + srt + ass. Returns updated file info."""
    cid = clip["id"]
    start, end = float(clip["start"]), float(clip["end"])
    dur = end - start
    cw = clip_words(words, start, end)

    layout = opts.get("layout", "auto")
    src_w, src_h = meta["width"], meta["height"]
    vertical_src = src_w / src_h < 0.7
    keys = None
    used_layout = layout
    crop_w = int(round(src_h * 9 / 16 / 2) * 2)
    if vertical_src:
        used_layout = "fill"
    elif layout in ("auto", "smart"):
        if clip.get("track") is None:
            samples = track_faces(src, start, end)
            clip["track"] = plan_path(samples, dur, crop_w / src_w)
        keys = clip["track"]
        used_layout = "smart" if keys else ("fit" if layout == "auto" else "center")
    elif layout == "center":
        used_layout = "center"

    # --- video chain
    if used_layout == "smart":
        x = crop_x_expr([tuple(k) for k in keys], src_w, crop_w)
        vchain = (f"[0:v]crop=w={crop_w}:h={src_h}:x='{x}':y=0,"
                  f"scale={config.OUT_W}:{config.OUT_H}:flags=lanczos,setsar=1,fps={config.FPS}[v0]")
    elif used_layout == "center":
        vchain = (f"[0:v]crop=w={crop_w}:h={src_h}:x=(iw-{crop_w})/2:y=0,"
                  f"scale={config.OUT_W}:{config.OUT_H}:flags=lanczos,setsar=1,fps={config.FPS}[v0]")
    elif used_layout == "fill":
        vchain = (f"[0:v]scale={config.OUT_W}:{config.OUT_H}:force_original_aspect_ratio=increase,"
                  f"crop={config.OUT_W}:{config.OUT_H},setsar=1,fps={config.FPS}[v0]")
    else:  # fit: full frame over blurred, zoomed copy of itself
        vchain = (f"[0:v]split[a][b];"
                  f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
                  f"eq=brightness=-0.10:saturation=1.2,scale={config.OUT_W}:{config.OUT_H}[bg];"
                  f"[b]scale={config.OUT_W}:-2:flags=lanczos[fg];"
                  f"[bg][fg]overlay=(W-w)/2:(H-h)/2-{int(config.OUT_H * 0.06)},setsar=1,fps={config.FPS}[v0]")

    keep = keep_intervals(cw, dur) if opts.get("tighten", True) else [(0.0, dur)]
    tightened = len(keep) > 1
    if tightened:
        sel = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep)
        vchain += f";[v0]select='{sel}',setpts=N/({config.FPS}*TB)[v1]"
        out_words = remap(cw, keep)
        out_dur = sum(b - a for a, b in keep)
    else:
        vchain += ";[v0]null[v1]"
        out_words, out_dur = cw, dur

    ass = job_dir / f"{cid}.ass"
    build_ass(out_words, out_dur, ass, style=opts.get("style", "bold"),
              accent=opts.get("accent", "#FFE14D"), position=opts.get("position", "lower"),
              hook=clip.get("hook", "") if opts.get("show_hook", True) else "",
              emphasis=clip.get("emphasis"), progress_bar=opts.get("progress_bar", True),
              watermark=opts.get("watermark", ""))
    build_srt(out_words, job_dir / f"{cid}.srt")
    vchain += f";[v1]ass={ass.name}[vout]"

    graph = vchain
    maps = ["-map", "[vout]"]
    if meta.get("has_audio"):
        achain = "[0:a]"
        if tightened:
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
           "-filter_complex_script", script.name, *maps,
           "-c:v", "libx264", "-preset", config.X264_PRESET, "-crf", config.X264_CRF,
           "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart",
           "-t", f"{out_dur:.3f}", str(tmp.name)]
    import subprocess
    proc = subprocess.run(cmd, cwd=job_dir, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg render failed: " + "\n".join(proc.stderr.splitlines()[-12:]))
    tmp.replace(out)
    snapshot(out, min(1.2, out_dur / 2), job_dir / f"{cid}.jpg", width=360)
    return {"layout_used": used_layout, "duration": round(out_dur, 2),
            "source_duration": round(dur, 2), "tightened_by": round(dur - out_dur, 2)}
