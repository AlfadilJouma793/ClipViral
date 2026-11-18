"""ffprobe / ffmpeg helpers and audio signal analysis."""
from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import numpy as np


class MediaError(RuntimeError):
    pass


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise MediaError(f"{cmd[0]} failed ({proc.returncode}):\n{tail}")
    return proc


def probe(path: Path) -> dict:
    proc = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                "-show_streams", str(path)])
    info = json.loads(proc.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError("No video stream found in the upload.")
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise MediaError("Could not read the video duration.")
    w, h = int(video["width"]), int(video["height"])
    # Respect phone rotation metadata.
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = abs(int(sd["rotation"])) % 180
    if rotation == 90:
        w, h = h, w
    num, _, den = (video.get("avg_frame_rate") or "30/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    return {"duration": duration, "width": w, "height": h, "fps": fps or 30.0,
            "has_audio": audio is not None}


def extract_audio(src: Path, dst: Path) -> None:
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(dst)])


def loudness_curve(wav_path: Path, hop: float = 0.25) -> np.ndarray:
    """RMS loudness in dBFS per `hop` seconds."""
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    n = max(1, int(sr * hop))
    usable = len(samples) // n * n
    if usable == 0:
        return np.array([-90.0])
    blocks = samples[:usable].reshape(-1, n)
    rms = np.sqrt(np.mean(blocks**2, axis=1) + 1e-10)
    return 20 * np.log10(rms)


def window_energy(curve: np.ndarray, start: float, end: float, hop: float = 0.25) -> dict:
    a, b = int(start / hop), max(int(start / hop) + 1, int(end / hop))
    seg = curve[a:b]
    if seg.size == 0:
        return {"mean": -90.0, "peak": -90.0, "var": 0.0}
    return {"mean": float(seg.mean()), "peak": float(np.percentile(seg, 95)),
            "var": float(seg.std())}


def snapshot(src: Path, t: float, dst: Path, width: int = 540) -> None:
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
         "-vf", f"scale={width}:-2", "-q:v", "3", str(dst)])
