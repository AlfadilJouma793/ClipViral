"""Finding the moments worth posting.

Two engines share one output format:

* Claude (when an Anthropic credential is configured) reads the full,
  sentence-numbered transcript plus per-sentence audio-energy hints and returns
  clips as *sentence ranges* — so every clip starts and ends on a complete
  thought instead of mid-word.
* A signal-based scorer (always available, offline) that rates every
  candidate window on hook strength, vocal energy, pace, payoff and niche
  relevance.
"""
from __future__ import annotations

import logging
import re
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from .. import config
from .media import window_energy
from .niches import get_niche

log = logging.getLogger("reelquarry.highlights")


# --------------------------------------------------------------------------- schema
class PlatformCaptions(BaseModel):
    tiktok: str = Field(description="TikTok caption, <=150 chars, conversational, 3-5 hashtags at end")
    instagram: str = Field(description="Instagram Reels caption: 1-3 short lines + call to action + hashtags")
    youtube: str = Field(description="YouTube Shorts title, <=90 chars, include #shorts")


class Scores(BaseModel):
    hook: int = Field(description="0-100: how strongly the first 3 seconds stop the scroll")
    flow: int = Field(description="0-100: is it self-contained and easy to follow cold")
    value: int = Field(description="0-100: insight, emotion, humour or usefulness")
    trend: int = Field(description="0-100: shareability / relevance to what performs on short-form now")


class ClipPick(BaseModel):
    start_sentence: int = Field(description="index of the first sentence of the clip")
    end_sentence: int = Field(description="index of the last sentence (inclusive)")
    title: str = Field(description="short internal title for the clip, <=60 chars")
    hook: str = Field(description="on-screen hook headline for the first seconds, <=8 words, punchy, accurate")
    virality: int = Field(description="0-100 overall predicted performance")
    scores: Scores
    why: str = Field(description="one or two sentences: why this moment will perform")
    emphasis_words: list[str] = Field(description="3-8 words from the clip to colour-highlight in captions")
    hashtags: list[str] = Field(description="4-6 hashtags without #")
    captions: PlatformCaptions


class ClipPlan(BaseModel):
    clips: list[ClipPick]


# --------------------------------------------------------------------------- helpers
def _fmt(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def _energy_tags(sents: list[dict], curve: np.ndarray) -> list[str]:
    means = np.array([window_energy(curve, s["s"], s["e"])["mean"] for s in sents])
    if means.size == 0:
        return []
    hi, lo = np.percentile(means, 85), np.percentile(means, 15)
    return ["LOUD" if m >= hi else ("quiet" if m <= lo else "") for m in means]


def _fit_range(sents: list[dict], a: int, b: int, min_len: float, max_len: float) -> tuple[int, int]:
    a = max(0, min(a, len(sents) - 1))
    b = max(a, min(b, len(sents) - 1))
    # trim from the end until it fits
    while b > a and sents[b]["e"] - sents[a]["s"] > max_len:
        b -= 1
    # extend forward, then backward, until long enough
    while sents[b]["e"] - sents[a]["s"] < min_len and b + 1 < len(sents) \
            and sents[b + 1]["e"] - sents[a]["s"] <= max_len:
        b += 1
    while sents[b]["e"] - sents[a]["s"] < min_len and a > 0 \
            and sents[b]["e"] - sents[a - 1]["s"] <= max_len:
        a -= 1
    return a, b


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return min(a[1], b[1]) - max(a[0], b[0]) > 0.35 * min(a[1] - a[0], b[1] - b[0])


# --------------------------------------------------------------------------- Claude engine
SYSTEM = """You are Reelquarry's senior short-form video editor. You have cut thousands of \
viral TikToks, Reels and Shorts from long-form podcasts, interviews, webinars and vlogs.

You receive a transcript split into numbered sentences with timestamps. Sentences tagged \
LOUD had noticeably higher vocal energy (often laughter, emphasis or emotion).

Choose the moments that will perform best as standalone vertical clips:
- The first sentence must hook: a bold claim, question, conflict, number, or curiosity gap. \
Prefer starting where the hook is, not at the build-up.
- The clip must be self-contained: no dangling references ("as I said earlier") and it must \
end on a payoff, punchline or complete thought — never mid-explanation.
- Clips must not overlap and should cover different topics.
- Stay within the requested duration range (use the timestamps).
- Hooks and captions must be truthful to what is said. No fake claims, no invented numbers.
- Write in the same language as the transcript.
Return clips ordered from best to worst."""


def claude_plan(sents: list[dict], curve: np.ndarray, niche_key: str, n: int,
                min_len: float, max_len: float, extra_prompt: str = "") -> list[dict]:
    import anthropic

    niche = get_niche(niche_key)
    tags = _energy_tags(sents, curve)
    lines = [f"[{i}] {_fmt(s['s'])}-{_fmt(s['e'])}{' ' + tags[i] if tags and tags[i] else ''} | {s['text']}"
             for i, s in enumerate(sents)]
    user = (
        f"Audience preset: {niche['label']}.\n{niche['guidance']}\n"
        + (f"Creator's extra instructions: {extra_prompt}\n" if extra_prompt else "")
        + f"\nFind the {n} best clips, each between {min_len:.0f} and {max_len:.0f} seconds long.\n\n"
        "<transcript>\n" + "\n".join(lines) + "\n</transcript>"
    )
    client = anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "medium"},
        output_format=ClipPlan,
        # Server-side fallback: if the primary model declines, the API retries
        # on a suitable fallback model inside the same call.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to analyse this transcript")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"Claude returned no usable plan (stop_reason={response.stop_reason})")

    out: list[dict] = []
    taken: list[tuple[float, float]] = []
    for pick in response.parsed_output.clips:
        a, b = _fit_range(sents, pick.start_sentence, pick.end_sentence, min_len, max_len)
        span = (sents[a]["s"], sents[b]["e"])
        if any(_overlaps(span, t) for t in taken):
            continue
        taken.append(span)
        out.append({
            "s_idx": a, "e_idx": b, "start": span[0], "end": span[1],
            "title": pick.title.strip(), "hook": pick.hook.strip(),
            "virality": int(max(0, min(100, pick.virality))),
            "scores": pick.scores.model_dump(), "why": pick.why.strip(),
            "emphasis": [w.strip() for w in pick.emphasis_words if w.strip()],
            "hashtags": [h.lstrip("#").strip() for h in pick.hashtags if h.strip()],
            "captions": pick.captions.model_dump(), "engine": "claude",
        })
        if len(out) >= n:
            break
    return out


# --------------------------------------------------------------------------- offline engine
_HOOK_WORDS = {"never", "always", "secret", "mistake", "truth", "why", "how", "stop", "nobody",
               "everyone", "biggest", "worst", "best", "crazy", "insane", "actually", "wrong",
               "most", "only", "real", "warning", "don't", "dont", "need", "must", "free"}
_PAYOFF = {"so", "that's", "thats", "which", "means", "lesson", "point", "bottom", "result",
           "and", "because", "why", "today", "finally"}
_FILLER = re.compile(r"^(um+|uh+|erm+|hmm+|ah+|like,?|you know,?)$", re.I)
_NUM = re.compile(r"(\d|£|\$|%|percent|thousand|million|half|double|twice)", re.I)


def _norm(w: str) -> str:
    return re.sub(r"[^\w£$%']", "", w.lower())


def _hook_score(text: str, niche_kw: list[str]) -> tuple[float, list[str]]:
    words = [_norm(w) for w in text.split()]
    reasons, s = [], 0.0
    if text.rstrip().endswith("?"):
        s += 0.35; reasons.append("opens with a question")
    if _NUM.search(text):
        s += 0.25; reasons.append("leads with a concrete number")
    hw = [w for w in words[:14] if w in _HOOK_WORDS]
    if hw:
        s += min(0.35, 0.15 * len(hw)); reasons.append(f"strong hook word “{hw[0]}”")
    if any(w in ("you", "your", "you're", "youre") for w in words[:10]):
        s += 0.15; reasons.append("speaks directly to the viewer")
    low = text.lower()
    if any(k in low for k in niche_kw):
        s += 0.2
    if 5 <= len(words) <= 22:
        s += 0.1
    return min(1.0, s), reasons


def heuristic_plan(sents: list[dict], words: list[dict], curve: np.ndarray, niche_key: str,
                   n: int, min_len: float, max_len: float) -> list[dict]:
    niche = get_niche(niche_key)
    kw = [k.lower() for k in niche["keywords"]]
    if not sents:
        return []
    sent_energy = np.array([window_energy(curve, s["s"], s["e"])["mean"] for s in sents])
    e_mu, e_sd = float(sent_energy.mean()), float(sent_energy.std() or 1.0)
    hooks = [_hook_score(s["text"], kw) for s in sents]

    cands = []
    for a in range(len(sents)):
        for b in range(a, len(sents)):
            dur = sents[b]["e"] - sents[a]["s"]
            if dur > max_len:
                break
            if dur < min_len:
                continue
            text = " ".join(s["text"] for s in sents[a:b + 1])
            low = text.lower()
            toks = [_norm(w) for w in text.split()]
            hook, hook_reasons = hooks[a]
            energy_z = float((sent_energy[a:b + 1].mean() - e_mu) / e_sd)
            peak_z = float((sent_energy[a:b + 1].max() - e_mu) / e_sd)
            wpm = len(toks) / dur * 60
            pace = 1.0 - min(1.0, abs(wpm - 170) / 120)
            kw_hits = sum(low.count(k) for k in kw)
            kw_score = min(1.0, kw_hits / max(1.0, dur / 12))
            fillers = sum(1 for t in text.split() if _FILLER.match(t))
            filler_pen = min(0.3, fillers / max(1, len(toks)) * 3)
            last = sents[b]["text"].strip()
            payoff = 0.6 if last.endswith((".", "!")) else 0.2
            if any(_norm(w) in _PAYOFF for w in last.split()[:3]):
                payoff += 0.25
            if last.endswith("!"):
                payoff += 0.15
            dangling = 0.25 if re.match(r"^(and|but|so|because|which|it|that|this|he|she|they)\b",
                                        sents[a]["text"].strip(), re.I) else 0.0
            # long silences inside a clip usually mean the topic changed
            topic_breaks = sum(1 for k in range(a, b) if sents[k + 1]["s"] - sents[k]["e"] > 1.1)
            score = (0.34 * hook + 0.16 * np.tanh(energy_z) + 0.08 * np.tanh(peak_z)
                     + 0.12 * pace + 0.15 * kw_score + 0.15 * min(1.0, payoff)
                     - filler_pen - dangling - 0.12 * topic_breaks)
            cands.append((score, a, b, hook, hook_reasons, energy_z, peak_z, pace, kw_score, payoff, dangling))

    cands.sort(key=lambda c: c[0], reverse=True)
    out, taken = [], []
    if not cands:  # video shorter than min_len: just take everything
        cands = [(0.5, 0, len(sents) - 1, hooks[0][0], hooks[0][1], 0, 0, 0.5, 0, 0.5, 0)]
    best = cands[0][0] or 1.0
    for score, a, b, hook, hr, ez, pz, pace, kws, payoff, dangling in cands:
        span = (sents[a]["s"], sents[b]["e"])
        if any(_overlaps(span, t) for t in taken):
            continue
        taken.append(span)
        first = sents[a]["text"].strip()
        reasons = list(hr)
        if pz > 1.0:
            peak_t = sents[a + int(np.argmax(sent_energy[a:b + 1]))]["s"] - span[0]
            reasons.append(f"energy spike at {peak_t:.0f}s")
        if kws > 0.4:
            reasons.append(f"on-topic for {niche['label'].lower()}")
        if payoff >= 0.8:
            reasons.append("ends on a clear payoff")
        why = ("Strong candidate: " + ", ".join(reasons) + ".") if reasons else \
            "Self-contained moment with steady delivery."
        rel = max(0.0, score / best)
        virality = int(round(55 + 40 * rel * min(1.0, 0.6 + score)))
        text = " ".join(s["text"] for s in sents[a:b + 1])
        emph = _pick_emphasis(text, kw)
        topic_tags = [re.sub(r"[^a-z0-9]", "", k) for k in kw if k in text.lower() and len(k) > 3]
        tags = list(dict.fromkeys(niche["hashtags"] + [t for t in topic_tags if t]))[:6]
        hook_text = _make_hook(first)
        out.append({
            "s_idx": a, "e_idx": b, "start": span[0], "end": span[1],
            "title": _title(first), "hook": hook_text, "virality": min(97, virality),
            "scores": {"hook": int(hook * 100), "flow": int(max(0, 1 - dangling * 3) * 85),
                       "value": int(max(0.0, min(1.0, 0.3 + 0.35 * kws + 0.15 * np.tanh(ez) + 0.2 * min(1.0, payoff))) * 100),
                       "trend": int(min(1.0, 0.35 + pace * 0.4 + max(0.0, np.tanh(pz)) * 0.3) * 100)},
            "why": why, "emphasis": emph, "hashtags": tags,
            "captions": _captions(hook_text, first, tags), "engine": "signals",
        })
        if len(out) >= n:
            break
    return out


def _title(first: str) -> str:
    t = first.strip().rstrip(".!?,")
    return (t[:57] + "…") if len(t) > 58 else t


def _make_hook(first: str) -> str:
    words = first.strip().split()
    words = [w for w in words if not _FILLER.match(w)]
    if len(words) <= 8:
        return " ".join(words).rstrip(",")
    cut = words[:8]
    # prefer to stop at a natural break
    for i in range(7, 3, -1):
        if cut[i - 1].endswith((",", ".", "?", "!", ":")):
            cut = cut[:i]
            break
    return " ".join(cut).rstrip(",.:") + "…"


def _pick_emphasis(text: str, kw: list[str]) -> list[str]:
    seen, out = set(), []
    for raw in text.split():
        w = _norm(raw)
        if not w or w in seen:
            continue
        if _NUM.search(w) or w in _HOOK_WORDS or any(w == k for k in kw):
            seen.add(w); out.append(raw.strip(".,!?\"'"))
        if len(out) >= 6:
            break
    return out


def _captions(hook: str, first: str, tags: list[str]) -> dict:
    hs = " ".join("#" + t for t in tags[:5])
    clean = hook.rstrip("…")
    return {
        "tiktok": f"{clean} 👀 {hs}"[:150],
        "instagram": f"{clean}\n\nSave this for later and share it with someone who needs it.\n\n{hs}",
        "youtube": f"{clean[:78]} #shorts",
    }


# --------------------------------------------------------------------------- entry
def find_highlights(sents, words, curve, niche, n, min_len, max_len, extra_prompt="",
                    on_note=None) -> list[dict]:
    if config.ai_available():
        try:
            clips = claude_plan(sents, curve, niche, n, min_len, max_len, extra_prompt)
            if clips:
                return clips
        except Exception as exc:  # network, auth, refusal... never fail the job for it
            log.warning("Claude highlight detection failed, using signal scorer: %s", exc)
            if on_note:
                on_note(f"AI analysis unavailable ({type(exc).__name__}); used signal scorer.")
    return heuristic_plan(sents, words, curve, niche, n, min_len, max_len)
