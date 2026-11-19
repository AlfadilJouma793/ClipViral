"""Visual moment finding for videos with little or no speech (sports, gaming,
vlogs, music, product demos, B-roll-heavy edits).

Signals: frame-to-frame motion, scene cuts and audio loudness. Candidate
windows are scored on those, snapped to scene cuts for clean edits, and then
(when an Anthropic credential is configured) Claude looks at a strip of frames
from each candidate and picks/writes up the best ones.
"""
from __future__ import annotations

import base64
import logging
import subprocess
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from .. import config
from .highlights import PlatformCaptions, Scores
from .niches import get_niche

log = logging.getLogger("clipviral.visual")

FPS = 2.0
SIG_W, SIG_H = 160, 90


def frame_signals(src: Path, duration: float) -> dict:
    """Motion per sample (0-255 mean abs diff) and scene-cut times, sampled at 2 fps."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(src), "-an",
           "-vf", f"fps={FPS},scale={SIG_W}:{SIG_H},format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = SIG_W * SIG_H
    motion, cuts = [], []
    prev = None
    i = 0
    assert proc.stdout is not None
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        f = np.frombuffer(buf, np.uint8).astype(np.int16)
        if prev is None:
            motion.append(0.0)
        else:
            d = float(np.abs(f - prev).mean())
            motion.append(d)
            # scene cut: large change of the whole picture, not just movement
            hist_a = np.bincount(f // 16, minlength=16) / size
            hist_b = np.bincount(prev // 16, minlength=16) / size
            if d > 25 and float(np.abs(hist_a - hist_b).sum()) > 0.5:
                cuts.append(round(i / FPS, 2))
        prev = f
        i += 1
    proc.wait()
    m = np.array(motion, dtype=np.float64)
    # scene cuts produce huge one-frame spikes; don't count them as action
    for t in cuts:
        k = int(t * FPS)
        if k < m.size:
            m[k] = m[max(0, k - 1)]
    return {"fps": FPS, "motion": m, "cuts": cuts, "duration": duration}


def _z(a: np.ndarray) -> np.ndarray:
    if a.size == 0:
        return a
    sd = float(a.std()) or 1.0
    return (a - float(a.mean())) / sd


def _resample(curve: np.ndarray, src_hop: float, n: int) -> np.ndarray:
    """Loudness curve (hop seconds) -> n samples at FPS."""
    if curve.size == 0 or n == 0:
        return np.zeros(n)
    t = np.arange(n) / FPS
    idx = np.clip((t / src_hop).astype(int), 0, curve.size - 1)
    return curve[idx]


def candidates(sig: dict, curve: np.ndarray | None, n: int, min_len: float, max_len: float,
               hop: float = 0.25) -> list[dict]:
    """Best non-overlapping windows by motion + loudness + cut rhythm."""
    m = sig["motion"]
    N = m.size
    if N < 4:
        return []
    duration = sig["duration"]
    loud = _resample(curve, hop, N) if curve is not None and curve.size else np.zeros(N)
    cut_density = np.zeros(N)
    for t in sig["cuts"]:
        k = int(t * FPS)
        if k < N:
            cut_density[max(0, k - 4):k + 4] += 1  # +/- 2 s
    # per-sample excitement score
    smooth = np.convolve(m, np.ones(3) / 3, mode="same")
    s = 0.55 * np.tanh(_z(smooth)) + 0.35 * np.tanh(_z(loud)) + 0.10 * np.tanh(_z(cut_density))
    prefix = np.concatenate([[0.0], np.cumsum(s)])
    peak_prefix = smooth

    target = max(min_len, min(max_len, (min_len + max_len) / 2))
    lengths = sorted({min_len, target, max_len})
    out = []
    step = max(1, int(FPS))  # 1 s steps
    for L in lengths:
        w = int(L * FPS)
        if w >= N:
            w = N
        for a in range(0, max(1, N - w + 1), step):
            b = a + w
            mean = (prefix[b] - prefix[a]) / max(1, b - a)
            # strong opening matters: weight the first 3 s
            head = (prefix[min(b, a + 6)] - prefix[a]) / max(1, min(b, a + 6) - a)
            peak = float(peak_prefix[a:b].max())
            score = mean + 0.35 * head + 0.05 * np.tanh(peak / 20)
            out.append((float(score), a / FPS, min(duration, b / FPS)))
    out.sort(reverse=True)
    picked: list[dict] = []
    for score, a, b in out:
        if any(min(b, p["end"]) - max(a, p["start"]) > 0.25 * min(b - a, p["end"] - p["start"]) for p in picked):
            continue
        a2, b2 = _snap(a, b, sig["cuts"], min_len, max_len, duration)
        picked.append({"start": round(a2, 2), "end": round(b2, 2), "signal": round(score, 3)})
        if len(picked) >= n:
            break
    return picked


def _snap(a: float, b: float, cuts: list[float], min_len: float, max_len: float, duration: float) -> tuple[float, float]:
    """Move boundaries onto nearby scene cuts (within 2 s) so clips start/end cleanly."""
    def near(t):
        c = [x for x in cuts if abs(x - t) <= 2.0]
        return min(c, key=lambda x: abs(x - t)) if c else t
    a2, b2 = near(a), near(b)
    if not (min_len <= b2 - a2 <= max_len):
        a2, b2 = a, b
    return max(0.0, a2), min(duration, b2)


def contact_sheet(src: Path, start: float, end: float, dst: Path, frames: int = 4) -> Path:
    """A horizontal strip of frames across the window (JPEG, ~1280 px wide)."""
    span = max(0.5, end - start)
    rate = frames / span
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{span:.2f}", "-i", str(src),
                    "-vf", f"fps={rate:.5f},scale=320:-2,tile={frames}x1", "-frames:v", "1", "-q:v", "4", str(dst)],
                   check=True, capture_output=True)
    return dst


# --------------------------------------------------------------------------- Claude vision
class VisualPick(BaseModel):
    candidate: int = Field(description="index of the candidate moment")
    title: str = Field(description="short internal title, <=60 chars")
    hook: str = Field(description="on-screen hook headline, <=8 words, accurate to what is shown")
    virality: int = Field(description="0-100 predicted performance")
    scores: Scores
    why: str = Field(description="one or two sentences: why this moment will perform")
    hashtags: list[str] = Field(description="4-6 hashtags without #")
    captions: PlatformCaptions


class VisualPlan(BaseModel):
    clips: list[VisualPick]


VISION_SYSTEM = """You are ClipViral's short-form video editor. The video has little or no \
speech, so moments were found from motion, scene changes and loudness. For each candidate \
you get a strip of frames (left to right in time) and any words spoken. Choose the candidates \
that will work best as standalone short clips (action, reveals, skill, emotion, humour, \
satisfying or beautiful moments) and write accurate hooks and captions based only on what \
is visible or said. Never invent names, scores or facts you cannot see. Order best first."""


def claude_rank(src: Path, work: Path, cands: list[dict], words: list[dict], niche_key: str,
                n: int, extra_prompt: str = "") -> list[dict]:
    import anthropic

    niche = get_niche(niche_key)
    content: list[dict] = [{"type": "text", "text":
                            f"Audience preset: {niche['label']}. {niche['guidance']}\n"
                            + (f"Creator's instructions: {extra_prompt}\n" if extra_prompt else "")
                            + f"Pick the best {n} of these {len(cands)} candidates."}]
    for i, c in enumerate(cands):
        sheet = contact_sheet(src, c["start"], c["end"], work / f"cand{i}.jpg")
        said = " ".join(w["w"] for w in words if c["start"] <= w["s"] < c["end"])[:400]
        content.append({"type": "text", "text": f"Candidate {i}: {c['end'] - c['start']:.0f}s"
                        + (f", speech: \"{said}\"" if said else ", no speech")})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(sheet.read_bytes()).decode()}})
    client = anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        system=VISION_SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium"},
        output_format=VisualPlan,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to analyse these frames")
    if response.parsed_output is None:
        raise RuntimeError(f"Claude returned no usable plan (stop_reason={response.stop_reason})")
    out, seen = [], set()
    for p in response.parsed_output.clips:
        if p.candidate in seen or not 0 <= p.candidate < len(cands):
            continue
        seen.add(p.candidate)
        c = cands[p.candidate]
        out.append({"start": c["start"], "end": c["end"], "title": p.title.strip(), "hook": p.hook.strip(),
                    "virality": int(max(0, min(100, p.virality))), "scores": p.scores.model_dump(),
                    "why": p.why.strip(), "emphasis": [], "engine": "claude-vision",
                    "hashtags": [h.lstrip("#").strip() for h in p.hashtags if h.strip()],
                    "captions": p.captions.model_dump()})
        if len(out) >= n:
            break
    return out


def heuristic_writeup(cands: list[dict], niche_key: str, n: int) -> list[dict]:
    niche = get_niche(niche_key)
    tags = niche["hashtags"][:5]
    hs = " ".join("#" + t for t in tags)
    hooks = ["Wait for it…", "Watch this moment", "You have to see this", "This part 👀", "Don't scroll past this"]
    if not cands:
        return []
    best = max(c["signal"] for c in cands) or 1.0
    out = []
    for i, c in enumerate(cands[:n]):
        rel = c["signal"] / best if best > 0 else 0.5
        hook = hooks[i % len(hooks)].replace(" 👀", "")
        mm, ss = divmod(int(c["start"]), 60)
        out.append({"start": c["start"], "end": c["end"], "title": f"Moment at {mm}:{ss:02d}", "hook": hook,
                    "virality": int(max(50, min(95, 60 + 35 * rel))),
                    "scores": {"hook": int(60 + 30 * rel), "flow": 75, "value": int(55 + 30 * rel),
                               "trend": int(60 + 25 * rel)},
                    "why": "High on-screen action and audio energy compared with the rest of the video.",
                    "emphasis": [], "engine": "visual-signals", "hashtags": tags,
                    "captions": {"tiktok": f"{hook} {hs}"[:150],
                                 "instagram": f"{hook}\n\nSave this and share it with a friend.\n\n{hs}",
                                 "youtube": f"{hook} #shorts", "x": f"{hook} {' '.join('#' + t for t in tags[:2])}"}})
    return out


def find_visual_moments(src: Path, work: Path, duration: float, curve: np.ndarray | None,
                        words: list[dict], niche_key: str, n: int, min_len: float, max_len: float,
                        extra_prompt: str = "", on_note=None) -> list[dict]:
    sig = frame_signals(src, duration)
    pool = candidates(sig, curve, max(n * 2, n + 3), min_len, max_len)
    if not pool:
        # very short video: one clip of the whole thing
        return heuristic_writeup([{"start": 0.0, "end": round(duration, 2), "signal": 1.0}], niche_key, 1)
    if config.ai_available():
        try:
            picks = claude_rank(src, work, pool, words, niche_key, n, extra_prompt)
            if picks:
                return picks
        except Exception as exc:
            log.warning("Claude vision ranking failed, using signals: %s", exc)
            if on_note:
                on_note(f"AI vision unavailable ({type(exc).__name__}); used motion & sound signals.")
    return heuristic_writeup(pool, niche_key, n)
