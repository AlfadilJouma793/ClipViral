"""Animated word-by-word captions rendered as ASS subtitles (burned by libass).

Styles
  bold     – 2-3 UPPERCASE words, active word pops and turns accent colour
  karaoke  – full phrase, words light up as they are spoken
  boxed    – clean words on a solid rounded card (great for property / B2B)
  minimal  – sentence-case, soft shadow, no motion
  neon     – glowing outline, active word glows in accent colour
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import config
from .emoji_map import WORD_TO_EMOJI

STYLES = {
    "bold": "Bold Pop",
    "karaoke": "Karaoke",
    "boxed": "Boxed",
    "minimal": "Minimal",
    "neon": "Neon",
}

_FILLER = re.compile(r"^(um+|uh+|erm+|hmm+|mm+|ah+)[,.]?$", re.I)


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFE14D"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _t(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def _chunks(words: list[dict], max_words: int, max_chars: int, max_gap: float = 0.6) -> list[list[dict]]:
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words
                    or sum(len(x["w"]) + 1 for x in cur) + len(w["w"]) > max_chars
                    or w["s"] - cur[-1]["e"] > max_gap
                    or re.search(r"[.!?]$", cur[-1]["w"])):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def caption_margin(position: str, H: int) -> int:
    """Distance from the bottom edge to the caption baseline area."""
    return {"middle": int(H * 0.40), "lower": int(H * 0.27), "bottom": int(H * 0.12)}.get(position, int(H * 0.27))


def build_ass(words: list[dict], duration: float, path: Path, *, style: str = "bold",
              accent: str = "#FFE14D", position: str = "lower", hook: str = "",
              hook_seconds: float = 3.2, emphasis: list[str] | None = None,
              progress_bar: bool = True, watermark: str = "", size: tuple[int, int] | None = None,
              font: str = "", emojis: bool = False) -> list[tuple[float, float, str]]:
    """Write the ASS file. `words` are clip-relative (0 = clip start).

    Returns emoji events [(start, end, codepoint)] for the renderer to overlay
    (libass can't draw colour emoji, so they are composited as PNGs).
    """
    W, H = size or (config.OUT_W, config.OUT_H)
    acc = _ass_color(accent)
    white, black = "&H00FFFFFF", "&H00000000"
    emph = {re.sub(r"[^\w£$%']", "", e.lower()) for e in (emphasis or []) if e}
    margin_v = caption_margin(position, H)

    if style == "bold":
        base = f"Style: Cap,Inter Black,88,{white},{white},{black},&H90000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,{margin_v},1"
    elif style == "karaoke":
        base = f"Style: Cap,Inter ExtraBold,70,{white},{white},{black},&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,70,70,{margin_v},1"
    elif style == "boxed":
        base = f"Style: Cap,Inter ExtraBold,66,&H00141414,{white},&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,2,90,90,{margin_v},1"
    elif style == "neon":
        base = f"Style: Cap,Inter Black,80,{white},{white},{acc},&H00000000,-1,0,0,0,100,100,0,0,1,4,0,2,60,60,{margin_v},1"
    else:  # minimal
        base = f"Style: Cap,Inter SemiBold,62,{white},{white},&H60000000,&H80000000,0,0,0,0,100,100,0,0,1,2,4,2,90,90,{margin_v},1"

    hook_style = f"Style: Hook,Inter Black,74,&H00111111,{white},{white},&H00000000,-1,0,0,0,100,100,0,0,3,22,0,8,80,80,{int(H * 0.11)},1"
    wm_style = f"Style: Mark,Inter SemiBold,40,&H50FFFFFF,{white},&H90000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,9,50,50,60,1"
    bar_style = f"Style: Bar,Inter,10,{acc},{acc},{acc},{acc},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"
    if font:  # brand font replaces Inter for captions and the hook
        safe = re.sub(r"[,\r\n]", " ", font).strip()[:60]
        for weight in ("Inter Black", "Inter ExtraBold", "Inter SemiBold"):
            base = base.replace(weight, safe)
            hook_style = hook_style.replace(weight, safe)

    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        base, hook_style, wm_style, bar_style, "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    words = [w for w in words if not _FILLER.match(w["w"])]
    upper = style in ("bold", "neon")
    if style == "bold":
        groups = _chunks(words, 3, 16)
    elif style == "neon":
        groups = _chunks(words, 2, 14)
    elif style == "minimal":
        groups = _chunks(words, 7, 34)
    else:
        groups = _chunks(words, 5, 26)

    def is_emph(w: str) -> bool:
        return re.sub(r"[^\w£$%']", "", w.lower()) in emph

    emoji_events: list[tuple[float, float, str]] = []
    for gi, g in enumerate(groups):
        g_start = g[0]["s"]
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else duration
        g_end = min(nxt, g[-1]["e"] + 0.5) if nxt - g[-1]["e"] < 0.8 else g[-1]["e"] + 0.25
        g_end = min(g_end, duration)
        if emojis:
            code = next((WORD_TO_EMOJI[k] for k in (re.sub(r"[^\w£$%]", "", x["w"].lower()) for x in g)
                         if k in WORD_TO_EMOJI), None)
            last = emoji_events[-1] if emoji_events else None
            # at most one emoji per caption group, spaced out, never the same twice in a row
            if code and g_end - g_start >= 0.4 and (last is None or (g_start - last[1] >= 1.5 and code != last[2])):
                emoji_events.append((round(g_start, 3), round(min(g_end, g_start + 2.2), 3), code))
        if style == "minimal":
            txt = " ".join(_esc(w["w"]) for w in g)
            lines.append(f"Dialogue: 1,{_t(g_start)},{_t(g_end)},Cap,,0,0,0,,{{\\fad(80,60)}}{txt}")
            continue
        for k, w in enumerate(g):
            s = w["s"] if k else g_start
            e = g[k + 1]["s"] if k + 1 < len(g) else g_end
            if e <= s:
                continue
            parts = []
            for j, x in enumerate(g):
                word = _esc(x["w"].upper() if upper else x["w"])
                if style == "boxed":
                    if j == k:
                        parts.append(f"{{\\3c{acc}\\4c{acc}}}{word}{{\\r}}")
                    elif j < k:
                        parts.append(word)
                    else:
                        parts.append(f"{{\\1a&H70&}}{word}{{\\r}}")
                    continue
                if j == k:
                    tag = f"\\c{acc}"
                    if style == "bold":
                        tag += "\\fscx112\\fscy112\\t(0,90,\\fscx104\\fscy104)"
                    elif style == "neon":
                        tag += f"\\3c{acc}\\blur6\\bord6"
                    parts.append(f"{{{tag}}}{word}{{\\r}}")
                elif is_emph(x["w"]):
                    parts.append(f"{{\\c{acc}}}{word}{{\\r}}")
                elif style == "karaoke" and j > k:
                    parts.append(f"{{\\alpha&H70&}}{word}{{\\r}}")
                else:
                    parts.append(word)
            intro = "{\\fscx92\\fscy92\\t(0,70,\\fscx100\\fscy100)}" if (k == 0 and style == "bold") else ""
            lines.append(f"Dialogue: 1,{_t(s)},{_t(e)},Cap,,0,0,0,,{intro}{' '.join(parts)}")

    if hook.strip():
        hs = min(hook_seconds, duration)
        lines.append(f"Dialogue: 3,{_t(0)},{_t(hs)},Hook,,0,0,0,,"
                     f"{{\\fad(120,220)\\fscx85\\fscy85\\t(0,140,\\fscx100\\fscy100)}}{_esc(hook.strip())}")

    if watermark.strip():
        lines.append(f"Dialogue: 2,{_t(0)},{_t(duration)},Mark,,0,0,0,,{_esc(watermark.strip())}")

    if progress_bar and duration > 1:
        bar_h = 10
        draw = f"m 0 0 l {W} 0 l {W} {bar_h} l 0 {bar_h}"
        lines.append(
            f"Dialogue: 4,{_t(0)},{_t(duration)},Bar,,0,0,0,,"
            f"{{\\pos(0,{H - bar_h})\\clip(0,0,0,{H})\\t(0,{int(duration * 1000)},\\clip(0,0,{W},{H}))\\p1}}{draw}{{\\p0}}"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return emoji_events


def build_srt(words: list[dict], path: Path) -> None:
    words = [w for w in words if not _FILLER.match(w["w"])]
    out = []
    for i, g in enumerate(_chunks(words, 7, 38), 1):
        def ts(x: float) -> str:
            ms = int(round(x * 1000))
            return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
        out.append(f"{i}\n{ts(g[0]['s'])} --> {ts(g[-1]['e'])}\n{' '.join(w['w'] for w in g)}\n")
    path.write_text("\n".join(out), encoding="utf-8")
