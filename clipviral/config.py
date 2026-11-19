"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from .env (without overriding real env vars)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.environ.get("CLIPVIRAL_DATA", ROOT / "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
WEB_DIR = ROOT / "web"

# Speech-to-text (faster-whisper). "base" is a good CPU default; use
# "small"/"medium"/"large-v3" on a GPU worker for better accuracy.
WHISPER_MODEL = os.environ.get("CLIPVIRAL_WHISPER_MODEL", "base")
WHISPER_DEVICE = os.environ.get("CLIPVIRAL_WHISPER_DEVICE", "auto")

# Highlight detection with Claude. Without credentials the app falls back to
# the built-in signal-based scorer, so it still works offline.
CLAUDE_MODEL = os.environ.get("CLIPVIRAL_CLAUDE_MODEL", "claude-opus-5-5")
AI_DISABLED = os.environ.get("CLIPVIRAL_DISABLE_AI", "") == "1"

# Rendering
OUT_W, OUT_H = 1080, 1920
FPS = int(os.environ.get("CLIPVIRAL_FPS", "30"))
X264_PRESET = os.environ.get("CLIPVIRAL_X264_PRESET", "veryfast")
X264_CRF = os.environ.get("CLIPVIRAL_X264_CRF", "20")
WORKERS = int(os.environ.get("CLIPVIRAL_WORKERS", "1"))

MAX_UPLOAD_BYTES = int(float(os.environ.get("CLIPVIRAL_MAX_UPLOAD_GB", "8")) * 1024**3)

JOBS_DIR.mkdir(parents=True, exist_ok=True)


def ai_available() -> bool:
    if AI_DISABLED:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
