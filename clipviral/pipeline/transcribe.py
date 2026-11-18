"""Word-level transcription.

Primary engine: faster-whisper (runs locally, CPU or GPU, word timestamps).
Alternative: the user uploads an existing SRT/VTT transcript (podcasters often
already have one) — words are then timed proportionally inside each cue.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .. import config

Word = dict  # {"w": str, "s": float, "e": float}

_model_cache: dict = {}


def whisper_transcribe(audio: Path, duration: float, language: str | None = None,
                       on_progress: Callable[[float], None] | None = None) -> dict:
    from faster_whisper import WhisperModel  # heavy import, keep lazy

    key = (config.WHISPER_MODEL, config.WHISPER_DEVICE)
    if key not in _model_cache:
        try:
            _model_cache[key] = WhisperModel(config.WHISPER_MODEL, device=config.WHISPER_DEVICE,
                                             compute_type="int8")
        except Exception as exc:
            raise RuntimeError(
                f"The speech-to-text model '{config.WHISPER_MODEL}' could not be loaded ({exc}). "
                "The server needs to download it once from huggingface.co, or you can upload an "
                ".srt/.vtt transcript with the video.") from exc
    model = _model_cache[key]
    segments, info = model.transcribe(str(audio), language=language or None,
                                      word_timestamps=True, vad_filter=True,
                                      condition_on_previous_text=False)
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"w": text, "s": round(w.start, 3), "e": round(w.end, 3)})
        if on_progress and duration:
            on_progress(min(1.0, seg.end / duration))
    return {"language": info.language, "words": words, "source": f"whisper-{config.WHISPER_MODEL}"}


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _ts(s: str) -> float:
    m = _TS.search(s)
    if not m:
        raise ValueError(f"bad timestamp {s!r}")
    h, mnt, sec, ms = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(sec) + int(ms.ljust(3, "0")) / 1000


def parse_subtitles(text: str) -> dict:
    """Parse SRT or WebVTT into word-timed transcript."""
    words: list[Word] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r", "").strip())
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        a, _, b = lines[idx].partition("-->")
        start, end = _ts(a), _ts(b)
        cue = " ".join(lines[idx + 1:])
        cue = re.sub(r"<[^>]+>", "", cue)  # strip vtt/html tags
        tokens = cue.split()
        if not tokens or end <= start:
            continue
        weights = [len(t) + 2 for t in tokens]
        total, t = sum(weights), start
        for tok, wgt in zip(tokens, weights):
            dur = (end - start) * wgt / total
            words.append({"w": tok, "s": round(t, 3), "e": round(t + dur * 0.92, 3)})
            t += dur
    if not words:
        raise ValueError("No cues found in the transcript file.")
    return {"language": None, "words": words, "source": "uploaded-transcript"}


_END = re.compile(r"[.!?…]['\")\]]?$")


def sentences(words: list[Word], max_words: int = 40, pause: float = 0.9) -> list[dict]:
    """Group words into sentence-like units used as clip boundaries."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(i)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["s"] - w["e"]) if nxt else 99
        if _END.search(w["w"]) or gap > pause or len(cur) >= max_words or nxt is None:
            out.append({"i0": cur[0], "i1": cur[-1], "s": words[cur[0]]["s"],
                        "e": words[cur[-1]]["e"],
                        "text": " ".join(words[j]["w"] for j in cur)})
            cur = []
    return out
