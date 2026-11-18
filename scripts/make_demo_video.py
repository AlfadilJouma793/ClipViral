"""Build a synthetic ~3 minute 16:9 'podcast' for testing the pipeline offline.

* Speech: espeak-ng, one sentence at a time, so exact cue timings are known
  and written to demo.srt (lets you test without a Whisper model).
* Picture: a real face photo (scikit-image's astronaut) that sits on the left
  for the first half and on the right for the second half, simulating a
  two-camera cut — exercises the speaker-tracking reframe.

Usage: python scripts/make_demo_video.py OUT_DIR
Needs: espeak-ng, ffmpeg, scikit-image, opencv-python-headless.
"""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import cv2
import numpy as np

SCRIPT = [
    "Welcome back to the show. Today we are talking about buying your first home in the UK.",
    "Before we start, thanks to everyone who left a review this week.",
    "Here is the biggest mistake first-time buyers make.",
    "They fall in love with the kitchen and never check the EPC rating.",
    "An E rated home can cost you two thousand pounds more a year to heat.",
    "That is money you could be putting into your mortgage instead.",
    "So the lesson is simple. Always ask for the EPC before the viewing.",
    "Anyway, let me just check the time. Okay, we are fine.",
    "Now, why do so many offers fall through?",
    "Nobody tells you this, but the chain is usually the real problem, not the price.",
    "If your seller has not found their next home, your purchase is at risk.",
    "My rule is to ask one question at every viewing. Where are you moving to?",
    "That single question has saved my buyers months of stress.",
    "Let us talk about stamp duty for a moment.",
    "First-time buyers pay no stamp duty on the first part of the price.",
    "But above the threshold you pay five percent, and it adds up fast.",
    "Use a calculator before you make an offer, not after.",
    "Right, a quick story from last week.",
    "A couple offered the full asking price on a flat in Leeds.",
    "The survey came back and found damp in every bedroom.",
    "They renegotiated and saved eleven thousand pounds. Never skip the survey!",
    "That is the truth about surveys. They pay for themselves.",
    "Okay, landlords, this one is for you.",
    "Rental yields in the north are still beating the south by almost two percent.",
    "If you want cash flow, look at the numbers, not the postcode.",
    "Thanks for listening, and see you next week.",
]


def tts(text: str, path: Path, voice: str) -> float:
    subprocess.run(["espeak-ng", "-v", voice, "-s", "165", "-w", str(path), text], check=True)
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_tts"
    tmp.mkdir(exist_ok=True)
    cues, parts, t = [], [], 0.6
    for i, s in enumerate(SCRIPT):
        p = tmp / f"{i:03d}.wav"
        voice = "en-gb" if i % 5 else "en-gb+m3"
        d = tts(s, p, voice)
        cues.append((t, t + d, s))
        parts.append((p, t))
        t += d + (0.35 if i % 4 else 1.3)  # some longer pauses to test tightening
    total = t + 0.8

    # mix speech into one track
    sr = 22050
    track = np.zeros(int(total * sr), dtype=np.float32)
    for p, start in parts:
        with wave.open(str(p)) as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
            sr = w.getframerate()
        i0 = int(start * sr)
        track[i0:i0 + len(a)] += a[: len(track) - i0]
    audio = out_dir / "_speech.wav"
    with wave.open(str(audio), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(np.clip(track, -32768, 32767).astype(np.int16).tobytes())

    # picture
    from skimage import data
    face = cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)
    face = cv2.resize(face, (640, 640))
    W, H, fps = 1920, 1080, 25
    frames = int(total * fps)
    vw = cv2.VideoWriter(str(out_dir / "_video.avi"), cv2.VideoWriter_fourcc(*"MJPG"), fps, (W, H))
    bg = np.zeros((H, W, 3), np.uint8)
    for y in range(H):
        bg[y, :] = (40 + y // 20, 30 + y // 30, 60 + y // 25)
    for f in range(frames):
        frame = bg.copy()
        sec = f / fps
        x = 180 if sec < total / 2 else W - 640 - 180
        x += int(18 * np.sin(sec * 0.9))  # gentle sway
        frame[220:860, x:x + 640] = face
        cv2.putText(frame, "THE PROPERTY HOUR", (60, 90), cv2.FONT_HERSHEY_DUPLEX, 1.6, (230, 230, 230), 2)
        vw.write(frame)
    vw.release()

    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(out_dir / "_video.avi"), "-i", str(audio),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(out_dir / "demo.mp4")], check=True)

    def ts(x: float) -> str:
        ms = int(x * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    (out_dir / "demo.srt").write_text("\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{s}\n"
                                                for i, (a, b, s) in enumerate(cues, 1)))
    for p in list(tmp.iterdir()):
        p.unlink()
    tmp.rmdir()
    (out_dir / "_video.avi").unlink()
    audio.unlink()
    print(f"wrote {out_dir/'demo.mp4'} ({total:.1f}s) and demo.srt")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "demo"))
