"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("REELQUARRY_DATA", ROOT / "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
WEB_DIR = ROOT / "web"

# Speech-to-text (faster-whisper). "base" is a good CPU default; use
# "small"/"medium"/"large-v3" on a GPU worker for better accuracy.
WHISPER_MODEL = os.environ.get("REELQUARRY_WHISPER_MODEL", "base")
WHISPER_DEVICE = os.environ.get("REELQUARRY_WHISPER_DEVICE", "auto")

# Highlight detection with Claude. Without credentials the app falls back to
# the built-in signal-based scorer, so it still works offline.
CLAUDE_MODEL = os.environ.get("REELQUARRY_CLAUDE_MODEL", "claude-opus-5-5")
AI_DISABLED = os.environ.get("REELQUARRY_DISABLE_AI", "") == "1"

# Rendering
OUT_W, OUT_H = 1080, 1920
FPS = int(os.environ.get("REELQUARRY_FPS", "30"))
X264_PRESET = os.environ.get("REELQUARRY_X264_PRESET", "veryfast")
X264_CRF = os.environ.get("REELQUARRY_X264_CRF", "20")
WORKERS = int(os.environ.get("REELQUARRY_WORKERS", "1"))

MAX_UPLOAD_BYTES = int(float(os.environ.get("REELQUARRY_MAX_UPLOAD_GB", "8")) * 1024**3)

JOBS_DIR.mkdir(parents=True, exist_ok=True)


def ai_available() -> bool:
    if AI_DISABLED:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
