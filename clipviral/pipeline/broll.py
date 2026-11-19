"""AI B-roll: short stock-footage cutaways that illustrate what's being said.

1. Plan: pick 1-3 moments in the clip and a stock search query for each
   (Claude when available, otherwise concrete keywords from the transcript).
2. Fetch: search Pexels (free API key: https://www.pexels.com/api/) and cache
   the best-fitting MP4.
3. Render: the renderer overlays each cutaway full-frame for ~2.5 s while the
   original audio and captions continue.

B-roll is best-effort: any failure leaves the clip without cutaways.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pydantic import BaseModel, Field

from .. import config
from .emoji_map import WORD_TO_EMOJI
from .niches import get_niche

log = logging.getLogger("clipviral.broll")

CACHE = config.DATA_DIR / "broll_cache"
SEARCH_URLS = ["https://api.pexels.com/v1/videos/search", "https://api.pexels.com/videos/search"]
CUT_SECONDS = 2.5
MAX_CUTS = 3


def api_key() -> str:
    return os.environ.get("PEXELS_API_KEY", "").strip()


def available() -> bool:
    return bool(api_key())


# --------------------------------------------------------------------------- planning
class BrollItem(BaseModel):
    word_index: int = Field(description="index of the word where the cutaway should start")
    query: str = Field(description="2-4 word stock-footage search query, concrete and visual")


class BrollPlan(BaseModel):
    items: list[BrollItem]


_STOP = {"this", "that", "they", "them", "there", "their", "what", "when", "where", "which", "about", "would",
         "could", "should", "really", "because", "people", "thing", "things", "going", "think", "know", "just",
         "like", "your", "have", "with", "from", "were", "been", "into", "than", "then", "also", "very", "much"}


def _eligible(words: list[dict], start_offset: float = 3.0, end_margin: float = 2.0) -> list[int]:
    """Word indexes where a cutaway may start: after the hook and not at the very end."""
    if not words:
        return []
    end = words[-1]["e"]
    return [i for i, w in enumerate(words) if w["s"] >= start_offset and w["s"] + CUT_SECONDS <= end - end_margin]


def _spread(cands: list[tuple[int, str]], words: list[dict]) -> list[tuple[int, str]]:
    """Keep cutaways at least 5 s apart and at most MAX_CUTS."""
    out: list[tuple[int, str]] = []
    for i, q in cands:
        if all(abs(words[i]["s"] - words[j]["s"]) >= 5.0 for j, _ in out):
            out.append((i, q))
        if len(out) >= MAX_CUTS:
            break
    return sorted(out)


def plan_offline(words: list[dict], niche_key: str) -> list[tuple[int, str]]:
    niche = get_niche(niche_key)
    kw = {k.lower() for k in niche["keywords"]}
    ok = set(_eligible(words))
    scored = []
    for i, w in enumerate(words):
        if i not in ok:
            continue
        t = re.sub(r"[^a-z]", "", w["w"].lower())
        if len(t) < 4 or t in _STOP:
            continue
        # concrete, visual words: the emoji vocabulary + niche keywords
        score = (2 if t in WORD_TO_EMOJI else 0) + (1 if t in kw else 0)
        if score:
            scored.append((score, i, t))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return _spread([(i, t) for _, i, t in scored], words)


def plan_claude(words: list[dict], niche_key: str) -> list[tuple[int, str]]:
    import anthropic

    ok = _eligible(words)
    if not ok:
        return []
    numbered = " ".join(f"[{i}]{w['w']}" for i, w in enumerate(words))
    niche = get_niche(niche_key)
    client = anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=4000,
        system=("You add B-roll cutaways to short social clips. Pick up to 3 moments where a short "
                "stock-footage shot would illustrate what is being said (a concrete object, place or "
                "action), and write a concrete visual search query for each. Never pick the first "
                "3 seconds. Prefer fewer, clearly relevant shots over many."),
        messages=[{"role": "user", "content": f"Audience: {niche['label']}.\nAllowed word indexes: "
                   f"{ok[0]}-{ok[-1]}.\nClip words:\n{numbered}"}],
        output_config={"effort": "low"},
        output_format=BrollPlan,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return []
    allowed = set(ok)
    items = [(it.word_index, it.query.strip()[:60]) for it in response.parsed_output.items
             if it.word_index in allowed and it.query.strip()]
    return _spread(items, words)


def plan(words: list[dict], niche_key: str) -> list[tuple[int, str]]:
    if config.ai_available():
        try:
            items = plan_claude(words, niche_key)
            if items:
                return items
        except Exception as exc:
            log.warning("Claude B-roll planning failed, using keywords: %s", exc)
    return plan_offline(words, niche_key)


# --------------------------------------------------------------------------- fetching
def _orientation(size: tuple[int, int]) -> str:
    w, h = size
    return "portrait" if h > w * 1.05 else ("landscape" if w > h * 1.05 else "square")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": api_key(), "User-Agent": "ClipViral/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def search(query: str, size: tuple[int, int]) -> list[dict]:
    params = urllib.parse.urlencode({"query": query, "orientation": _orientation(size), "size": "medium",
                                     "per_page": 8})
    last_exc: Exception | None = None
    for base in SEARCH_URLS:
        try:
            return list(_get_json(f"{base}?{params}").get("videos") or [])
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code != 404:  # only fall back to the older path if the new one is missing
                break
        except Exception as exc:
            last_exc = exc
            break
    raise RuntimeError(f"Pexels search failed: {last_exc}")


def pick_file(video: dict, size: tuple[int, int]) -> dict | None:
    """Smallest MP4 that still covers the output frame; else the largest available."""
    W, H = size
    files = [f for f in video.get("video_files") or []
             if f.get("link") and "mp4" in str(f.get("file_type") or "video/mp4") and f.get("width") and f.get("height")]
    if not files:
        return None
    covering = [f for f in files if f["width"] >= W * 0.66 and f["height"] >= H * 0.66]
    if covering:
        return min(covering, key=lambda f: f["width"] * f["height"])
    return max(files, key=lambda f: f["width"] * f["height"])


def fetch(query: str, size: tuple[int, int], used: set[int]) -> dict | None:
    """Search + download (cached). Returns {"path", "query", "credit", "url", "pexels_id"}."""
    for v in search(query, size):
        vid = v.get("id")
        if vid in used or (v.get("duration") or 0) < 2:
            continue
        f = pick_file(v, size)
        if not f:
            continue
        CACHE.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(str(f["link"]).encode()).hexdigest()[:20]
        path = CACHE / f"{key}.mp4"
        if not path.exists():
            req = urllib.request.Request(f["link"], headers={"User-Agent": "ClipViral/1.0"})
            tmp = path.with_suffix(".part")
            with urllib.request.urlopen(req, timeout=60) as r, tmp.open("wb") as out:
                total = 0
                while chunk := r.read(1 << 20):
                    total += len(chunk)
                    if total > 200 * 1024 * 1024:
                        raise RuntimeError("stock clip too large")
                    out.write(chunk)
            tmp.replace(path)
        used.add(vid)
        user = v.get("user") or {}
        return {"path": str(path), "query": query, "pexels_id": vid, "url": v.get("url"),
                "credit": {"name": user.get("name") or "Pexels", "url": user.get("url") or v.get("url")}}
    return None


def build(words_rel: list[dict], clip_start: float, niche_key: str, size: tuple[int, int]) -> list[dict]:
    """Plan + fetch cutaways for one clip. Times are stored in *source* seconds."""
    items = plan(words_rel, niche_key)
    used: set[int] = set()
    out = []
    for i, query in items:
        try:
            got = fetch(query, size, used)
        except Exception as exc:
            log.warning("B-roll fetch failed for %r: %s", query, exc)
            continue
        if got:
            s = clip_start + words_rel[i]["s"]
            out.append({**got, "s": round(s, 3), "e": round(s + CUT_SECONDS, 3)})
    return out
