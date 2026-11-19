"""Brand templates: saved looks (colour, caption style, position, watermark,
logo, custom font, default shape) that agencies reuse per client."""
from __future__ import annotations

import io
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from . import config

BRANDS_DIR = config.DATA_DIR / "brands"
FIELDS = {"name", "accent", "style", "position", "watermark", "aspect", "emojis", "layout"}
MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_FONT_BYTES = 10 * 1024 * 1024


class BrandError(ValueError):
    pass


def _dir(brand_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", brand_id or ""):
        raise BrandError("Bad brand id")
    return BRANDS_DIR / brand_id


def _clean(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k not in FIELDS or v is None:
            continue
        if k == "accent":
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(v)):
                raise BrandError("Accent must be a hex colour like #FFE14D")
            v = str(v)
        elif k == "emojis":
            v = bool(v)
        else:
            v = str(v).strip()[:60]
        out[k] = v
    return out


def get(brand_id: str | None) -> dict | None:
    if not brand_id:
        return None
    try:
        p = _dir(brand_id) / "brand.json"
    except BrandError:
        return None
    return json.loads(p.read_text()) if p.exists() else None


def list_brands(workspace_id: str | None = None) -> list[dict]:
    out = []
    if not BRANDS_DIR.exists():
        return out
    for p in BRANDS_DIR.glob("*/brand.json"):
        b = json.loads(p.read_text())
        if workspace_id is None or b.get("workspace_id") == workspace_id:
            out.append(b)
    return sorted(out, key=lambda b: b.get("created_at", 0))


def _save(b: dict) -> dict:
    d = _dir(b["id"])
    d.mkdir(parents=True, exist_ok=True)
    b["updated_at"] = time.time()
    (d / "brand.json").write_text(json.dumps(b, indent=1))
    return b


def create(fields: dict, workspace_id: str | None = None) -> dict:
    data = _clean(fields)
    if not data.get("name"):
        raise BrandError("Give the brand template a name.")
    b = {"id": uuid.uuid4().hex[:12], "workspace_id": workspace_id, "created_at": time.time(),
         "accent": "#FFE14D", "style": "bold", "position": "lower", "watermark": "", "aspect": "9:16",
         "emojis": False, "layout": "auto", "logo": None, "font_file": None, "font_family": None, **data}
    return _save(b)


def update(brand_id: str, fields: dict) -> dict:
    b = get(brand_id)
    if not b:
        raise BrandError("Brand template not found.")
    b.update(_clean(fields))
    return _save(b)


def delete(brand_id: str) -> None:
    shutil.rmtree(_dir(brand_id), ignore_errors=True)


def set_logo(brand_id: str, data: bytes) -> dict:
    """Validate an uploaded image and store it as PNG (keeps transparency)."""
    import cv2
    import numpy as np

    b = get(brand_id)
    if not b:
        raise BrandError("Brand template not found.")
    if len(data) > MAX_LOGO_BYTES:
        raise BrandError("Logo must be under 5 MB.")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None or img.ndim < 2:
        raise BrandError("That logo isn't a readable image (use PNG or JPG).")
    h, w = img.shape[:2]
    if max(h, w) > 2000:
        f = 2000 / max(h, w)
        img = cv2.resize(img, (max(1, int(w * f)), max(1, int(h * f))), interpolation=cv2.INTER_AREA)
    ok, png = cv2.imencode(".png", img)
    if not ok:
        raise BrandError("Couldn't process that logo.")
    (_dir(brand_id) / "logo.png").write_bytes(png.tobytes())
    b["logo"] = "logo.png"
    return _save(b)


def set_font(brand_id: str, data: bytes, filename: str) -> dict:
    """Store a TTF/OTF font and read its real family name (what libass matches on)."""
    from fontTools.ttLib import TTFont

    b = get(brand_id)
    if not b:
        raise BrandError("Brand template not found.")
    ext = Path(filename or "").suffix.lower()
    if ext not in (".ttf", ".otf"):
        raise BrandError("Upload a .ttf or .otf font file.")
    if len(data) > MAX_FONT_BYTES:
        raise BrandError("Font must be under 10 MB.")
    try:
        font = TTFont(io.BytesIO(data))
        names = font["name"]
        family = (names.getDebugName(16) or names.getDebugName(1) or "").strip()
        full = (names.getDebugName(4) or family).strip()
    except Exception:
        raise BrandError("That file isn't a valid font.")
    if not family:
        raise BrandError("That font has no family name.")
    fonts = _dir(brand_id) / "fonts"
    shutil.rmtree(fonts, ignore_errors=True)
    fonts.mkdir(parents=True)
    (fonts / f"brand{ext}").write_bytes(data)
    # libass matches the full name (e.g. "Montserrat Black") or the family name
    b["font_file"] = f"fonts/brand{ext}"
    b["font_family"] = full or family
    return _save(b)


def clear_asset(brand_id: str, kind: str) -> dict:
    b = get(brand_id)
    if not b:
        raise BrandError("Brand template not found.")
    d = _dir(brand_id)
    if kind == "logo":
        (d / "logo.png").unlink(missing_ok=True)
        b["logo"] = None
    elif kind == "font":
        shutil.rmtree(d / "fonts", ignore_errors=True)
        b["font_file"] = b["font_family"] = None
    return _save(b)


def render_opts(brand_id: str | None) -> dict:
    """Extra render options contributed by a brand template (logo + font)."""
    b = get(brand_id)
    if not b:
        return {}
    d = _dir(b["id"])
    out = {}
    if b.get("logo") and (d / "logo.png").exists():
        out["logo"] = str(d / "logo.png")
    if b.get("font_file") and (d / b["font_file"]).exists():
        out["font"] = b["font_family"]
        out["fontsdir"] = str(d / "fonts")
    return out
