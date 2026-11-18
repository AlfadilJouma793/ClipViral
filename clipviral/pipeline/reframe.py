"""Speaker-aware 16:9 -> 9:16 reframing.

Samples frames, finds faces, then plans a *camera-operator* path: the crop
holds still while the speaker stays inside a dead-zone and makes short eased
moves when they leave it (or when the shot cuts to another person). The path is
compiled into an ffmpeg `crop` x-expression so rendering stays a single pass.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

ANALYSIS_W = 960
_detectors: dict = {}


def _faces(frame) -> list[tuple[float, float, float, float]]:
    """Detect faces on a frame already resized to ANALYSIS_W wide.

    Uses OpenCV's YuNet DNN when CLIPVIRAL_FACE_MODEL points at
    face_detection_yunet_2023mar.onnx (much more robust: profiles, small and
    partly-lit faces); otherwise frontal + profile Haar cascades.
    """
    import cv2

    h, w = frame.shape[:2]
    model = os.environ.get("CLIPVIRAL_FACE_MODEL", "")
    if model and Path(model).exists() and not _detectors.get("yunet_failed"):
        try:
            det = _detectors.get("yunet")
            if det is None:
                det = cv2.FaceDetectorYN.create(model, "", (w, h), 0.6, 0.3, 50)
                _detectors["yunet"] = det
            det.setInputSize((w, h))
            _, found = det.detect(frame)
            return [tuple(map(float, f[:4])) for f in (found if found is not None else [])]
        except Exception:
            _detectors["yunet_failed"] = True  # bad/missing model: use dlib from now on
    try:  # dlib HOG: built-in weights, accurate on talking heads, ~50ms/frame at 960px
        import dlib
        det = _detectors.get("hog")
        if det is None:
            det = _detectors["hog"] = dlib.get_frontal_face_detector()
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rects, scores, _ = det.run(rgb, 0, -0.2)
        return [(float(r.left()), float(r.top()), float(r.width()), float(r.height()))
                for r, s in zip(rects, scores)]
    except ImportError:
        pass
    if "front" not in _detectors:
        _detectors["front"] = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        _detectors["profile"] = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    min_side = max(20, int(w * 0.025))
    out = list(_detectors["front"].detectMultiScale(gray, 1.1, 4, minSize=(min_side, min_side)))
    if not out:
        out = list(_detectors["profile"].detectMultiScale(gray, 1.1, 5, minSize=(min_side, min_side)))
        if not out:  # profile cascade only finds left-facing; mirror for right-facing
            flipped = list(_detectors["profile"].detectMultiScale(cv2.flip(gray, 1), 1.1, 5,
                                                                  minSize=(min_side, min_side)))
            out = [(w - x - fw, y, fw, fh) for (x, y, fw, fh) in flipped]
    return [tuple(map(float, f)) for f in out]


def track_faces(video: Path, start: float, end: float, sample_fps: float = 2.5) -> list[tuple[float, float | None]]:
    """Return [(t_rel, cx_norm|None)] samples of the main face's horizontal centre."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []
    samples = []
    prev = None
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    stride = max(1, int(round(fps / sample_fps)))
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)  # one seek, then decode sequentially
    t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 or start
    while t < end:
        ok, frame = cap.read()
        if not ok:
            break
        for _ in range(stride - 1):
            cap.grab()
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (ANALYSIS_W, max(1, int(h * ANALYSIS_W / w))))
        faces = _faces(small)
        cx = None
        if len(faces):
            # Prefer big faces, with stickiness to the face we followed last.
            best, best_score = None, -1.0
            for (x, y, fw, fh) in faces:
                c = (x + fw / 2) / ANALYSIS_W
                score = fw * fh
                if prev is not None:
                    score *= 1.6 - min(1.0, abs(c - prev) * 2)
                if score > best_score:
                    best, best_score = c, score
            cx = float(best)
            prev = cx
        samples.append((t - start, cx))
        t += stride / fps
    cap.release()
    return samples


def plan_path(samples: list[tuple[float, float | None]], duration: float, crop_frac: float,
              dead_zone: float = 0.09, move_time: float = 0.45) -> list[tuple[float, float]] | None:
    """Turn raw face samples into keyframes [(t, cx_norm)] for the crop centre.

    Returns None when too few faces were found (caller falls back to fit+blur).
    """
    if not samples:
        return None
    found = [(t, c) for t, c in samples if c is not None]
    if len(found) < max(2, 0.25 * len(samples)):
        return None
    ts = np.array([t for t, _ in samples])
    cs = np.array([np.nan if c is None else c for _, c in samples], dtype=float)
    idx = np.where(~np.isnan(cs))[0]
    cs = np.interp(np.arange(len(cs)), idx, cs[idx])
    # median filter (k=5) to kill detector flicker
    k = 2
    med = np.array([np.median(cs[max(0, i - k):i + k + 1]) for i in range(len(cs))])

    half = crop_frac / 2
    def clamp(c: float) -> float:
        return float(min(1 - half, max(half, c)))

    keys = [(0.0, clamp(med[0]))]
    cur = keys[0][1]
    i = 1
    while i < len(med):
        target = clamp(med[i])
        if abs(target - cur) > dead_zone * crop_frac * 2.2:
            # require the new position to persist ~0.6s (avoid chasing glances)
            j = i
            while j < len(med) and abs(clamp(med[j]) - cur) > dead_zone * crop_frac * 2.2 and ts[j] - ts[i] < 0.6:
                j += 1
            if j < len(med) and ts[j] - ts[i] < 0.6:
                i = j
                continue
            seg = med[i:min(len(med), i + 4)]
            target = clamp(float(np.median(seg)))
            t0 = float(ts[i])
            big_jump = abs(target - cur) > crop_frac * 0.9  # likely a camera cut: snap
            dt = 0.04 if big_jump else move_time
            keys.append((t0, cur))
            keys.append((min(duration, t0 + dt), target))
            cur = target
        i += 1
    keys.append((duration, cur))
    # drop duplicates / non-increasing times
    clean = [keys[0]]
    for t, c in keys[1:]:
        if t > clean[-1][0] + 1e-3:
            clean.append((t, c))
    return clean[:120]


def crop_x_expr(keys: list[tuple[float, float]], src_w: int, crop_w: int) -> str:
    """Piecewise-linear ffmpeg expression for the crop's left edge."""
    def x_of(c: float) -> float:
        return max(0.0, min(src_w - crop_w, c * src_w - crop_w / 2))

    if len(keys) == 1:
        return f"{x_of(keys[0][1]):.1f}"
    expr = f"{x_of(keys[-1][1]):.1f}"
    for (t0, c0), (t1, c1) in reversed(list(zip(keys[:-1], keys[1:]))):
        x0, x1 = x_of(c0), x_of(c1)
        if abs(x1 - x0) < 0.5:
            seg = f"{x0:.1f}"
        else:
            # smoothstep easing between keyframes
            p = f"((t-{t0:.3f})/{max(1e-3, t1 - t0):.3f})"
            seg = f"({x0:.1f}+({x1 - x0:.1f})*({p}*{p}*(3-2*{p})))"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return expr
