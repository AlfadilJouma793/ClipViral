"""Run: python -m pytest -q"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from clipviral import config
from clipviral.pipeline import captions, highlights, reframe, render, transcribe

SRT = """1
00:00:01,000 --> 00:00:04,000
Here is the biggest mistake first-time buyers make.

2
00:00:04,200 --> 00:00:08,000
They never check the EPC rating before the viewing.

3
00:00:10,500 --> 00:00:14,000
An E rated home can cost two thousand pounds more a year.

4
00:00:14,200 --> 00:00:18,000
So the lesson is simple. Always ask for the EPC.

5
00:00:18,300 --> 00:00:23,000
Why do so many offers fall through? The chain is the real problem.
"""


@pytest.fixture
def words():
    return transcribe.parse_subtitles(SRT)["words"]


def test_parse_subtitles_times_words_inside_cues(words):
    assert words[0]["w"] == "Here"
    assert words[0]["s"] == pytest.approx(1.0)
    assert all(w["e"] > w["s"] for w in words)
    assert all(a["s"] <= b["s"] for a, b in zip(words, words[1:]))
    assert words[-1]["e"] <= 23.0


def test_sentences_split_on_punctuation(words):
    sents = transcribe.sentences(words)
    assert sents[0]["text"].endswith("make.")
    assert any(s["text"].endswith("?") for s in sents)


def test_heuristic_plan_returns_non_overlapping_sentence_aligned_clips(words):
    sents = transcribe.sentences(words)
    curve = np.full(200, -20.0)
    clips = highlights.heuristic_plan(sents, words, curve, "property_uk", 3, 5, 12)
    assert clips
    starts = {s["s"] for s in sents}
    ends = {s["e"] for s in sents}
    for c in clips:
        assert c["start"] in starts and c["end"] in ends
        assert 5 <= c["end"] - c["start"] <= 12
        assert 0 <= c["virality"] <= 100
        assert c["hook"] and c["captions"]["youtube"].endswith("#shorts")
    spans = sorted((c["start"], c["end"]) for c in clips)
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        assert b0 >= a1 - 0.35 * min(a1 - a0, b1 - b0)


def test_claude_plan_maps_sentence_ranges_to_times(monkeypatch, words):
    sents = transcribe.sentences(words)
    pick = highlights.ClipPick(
        start_sentence=0, end_sentence=1, title="EPC mistake", hook="The EPC mistake", virality=91,
        scores=highlights.Scores(hook=90, flow=85, value=88, trend=70), why="Clear mistake + payoff.",
        emphasis_words=["EPC"], hashtags=["#ukproperty", "epc"],
        captions=highlights.PlatformCaptions(tiktok="t", instagram="i", youtube="y #shorts", x="x post"))
    captured = {}

    class FakeMessages:
        def parse(self, **kw):
            captured.update(kw)
            return SimpleNamespace(stop_reason="end_turn",
                                   parsed_output=highlights.ClipPlan(clips=[pick, pick]))

    class FakeClient:
        def __init__(self):
            self.beta = SimpleNamespace(messages=FakeMessages())

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    clips = highlights.claude_plan(sents, np.full(200, -20.0), "property_uk", 3, 5, 12)
    assert len(clips) == 1  # duplicate overlapping pick dropped
    c = clips[0]
    assert c["start"] == sents[0]["s"] and c["end"] == sents[1]["e"]
    assert c["hashtags"] == ["ukproperty", "epc"]
    assert captured["model"] == config.CLAUDE_MODEL
    assert captured["output_format"] is highlights.ClipPlan
    assert "[0]" in captured["messages"][0]["content"]


def test_find_highlights_falls_back_when_claude_fails(monkeypatch, words):
    monkeypatch.setattr(config, "ai_available", lambda: True)
    monkeypatch.setattr(highlights, "claude_plan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    notes = []
    clips = highlights.find_highlights(transcribe.sentences(words), words, np.full(200, -20.0),
                                       "general", 2, 5, 12, on_note=notes.append)
    assert clips and clips[0]["engine"] == "signals" and notes


def test_keep_intervals_and_remap_remove_dead_air():
    ws = [{"w": "a", "s": 0.0, "e": 0.5}, {"w": "b", "s": 0.6, "e": 1.0},
          {"w": "c", "s": 3.0, "e": 3.5}]
    keep = render.keep_intervals(ws, 4.0)
    assert len(keep) == 2
    total = sum(b - a for a, b in keep)
    assert total < 4.0 - 1.0
    out = render.remap(ws, keep)
    assert out[2]["s"] < 2.0  # pulled forward
    assert out[-1]["e"] <= total + 1e-6


def test_plan_path_holds_steady_and_snaps_on_cut():
    samples = [(i * 0.4, 0.25 + 0.01 * np.sin(i)) for i in range(20)] + \
              [(8 + i * 0.4, 0.75) for i in range(20)]
    keys = reframe.plan_path(samples, 16.0, 0.32)
    assert keys is not None
    xs = [c for _, c in keys]
    assert min(xs) < 0.3 and max(xs) > 0.7
    assert len(keys) <= 6  # no jitter
    assert reframe.plan_path([(0, None)] * 10, 4, 0.3) is None


def test_build_ass_contains_hook_progress_and_word_events(tmp_path, words):
    p = tmp_path / "x.ass"
    rel = [{**w, "s": w["s"] - 1, "e": w["e"] - 1} for w in words[:12]]
    for style in captions.STYLES:
        captions.build_ass(rel, 10, p, style=style, hook="Big {hook}", watermark="@me", emphasis=["EPC"])
        text = p.read_text()
        assert "Big (hook)" in text and "@me" in text and "\\clip(" in text
        assert text.count("Dialogue:") > 5


def test_render_clip_end_to_end(tmp_path, words):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=44100", "-t", "24",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    meta = {"width": 1280, "height": 720, "duration": 24, "has_audio": True, "fps": 25}
    clip = {"id": "t1", "start": 1.0, "end": 14.0, "hook": "Test hook", "emphasis": ["EPC"], "track": None}
    info = render.render_clip(tmp_path, src, meta, clip, words,
                              {"layout": "auto", "style": "bold", "tighten": True, "watermark": "@x"})
    out = tmp_path / "t1.mp4"
    assert out.exists() and (tmp_path / "t1.jpg").exists() and (tmp_path / "t1.srt").exists()
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    assert probe.stdout.strip() == f"{config.OUT_W},{config.OUT_H}"
    assert info["layout_used"] in ("smart", "fit")  # no faces: follows motion, else fits
    assert info["tightened_by"] > 1.0  # the 2.5s gap in the SRT was tightened


# ---------------------------------------------------------------- link import
def test_validate_url_rejects_bad_and_private_links(monkeypatch):
    from clipviral.pipeline import fetch
    monkeypatch.delenv("CLIPVIRAL_ALLOW_PRIVATE_URLS", raising=False)
    for bad in ("", "ftp://example.com/a.mp4", "youtube.com/watch?v=x", "http://127.0.0.1:8000/x.mp4",
                "http://localhost/x.mp4", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/v.mp4"):
        with pytest.raises(fetch.LinkError):
            fetch.validate_url(bad)


def test_download_from_link_end_to_end(tmp_path, monkeypatch):
    """Serve a real video over HTTP and import it through yt-dlp's generic extractor."""
    import functools
    import http.server
    import threading

    from clipviral.pipeline import fetch
    srv_dir = tmp_path / "srv"
    srv_dir.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=300", "-t", "6", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(srv_dir / "talk.mp4")], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(srv_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("CLIPVIRAL_ALLOW_PRIVATE_URLS", "1")
        out = tmp_path / "job"
        out.mkdir()
        seen = []
        got = fetch.download(f"http://127.0.0.1:{httpd.server_port}/talk.mp4", out, on_progress=seen.append)
        assert got["path"].exists() and got["path"].stat().st_size > 10_000
        assert got["path"].name.startswith("source.")

        # too long -> friendly error, nothing left behind
        monkeypatch.setattr(config, "MAX_VIDEO_SECONDS", 3)
        out2 = tmp_path / "job2"
        out2.mkdir()
        with pytest.raises(fetch.LinkError, match="limit"):
            fetch.download(f"http://127.0.0.1:{httpd.server_port}/talk.mp4", out2)

        with pytest.raises(fetch.LinkError):
            fetch.download(f"http://127.0.0.1:{httpd.server_port}/missing.mp4", tmp_path)
    finally:
        httpd.shutdown()


def test_api_link_job_requires_rights_and_valid_link(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from clipviral import jobs, main
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: None)
    monkeypatch.delenv("CLIPVIRAL_ALLOW_PRIVATE_URLS", raising=False)
    c = TestClient(main.app)
    r = c.post("/api/jobs", data={"url": "https://example.com/v.mp4"})
    assert r.status_code == 400 and "permission" in r.json()["detail"]
    r = c.post("/api/jobs", data={"url": "http://127.0.0.1/v.mp4", "rights": "true"})
    assert r.status_code == 400 and "private" in r.json()["detail"]
    r = c.post("/api/jobs", data={})
    assert r.status_code == 400
    monkeypatch.setattr(main.fetch, "validate_url", lambda u: u)
    r = c.post("/api/jobs", data={"url": "https://www.youtube.com/watch?v=abc", "rights": "true", "niche": "x"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["url"].endswith("v=abc") and j["step"] == "download" and j["options"]["niche"] == "x"
    assert j["name"] == "www.youtube.com"


def test_new_platform_presets_exist():
    from clipviral.pipeline.niches import NICHES, public_niches
    keys = {n["key"] for n in public_niches()}
    assert {"tiktok", "instagram", "x"} <= keys
    assert NICHES["x"]["lengths"] == [30, 90]


# ---------------------------------------------------------------- editing & render features
def _talk_video(path, seconds=24, size="1280x720"):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=44100", "-t", str(seconds),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


def _dims(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    import json as _j
    d = _j.loads(out)
    return d["streams"][0]["width"], d["streams"][0]["height"], float(d["format"]["duration"])


def test_subtract_and_filler_spans():
    assert render.subtract([(0, 10)], [(2, 3), (5, 6)]) == [(0, 2), (3, 5), (6, 10)]
    assert render.subtract([(0, 4), (6, 9)], [(3, 7)]) == [(0, 3), (7, 9)]
    ws = [{"w": "so", "s": 0, "e": 0.3}, {"w": "um,", "s": 0.4, "e": 0.8}, {"w": "yes", "s": 0.9, "e": 1.2}]
    spans = render.filler_spans(ws)
    assert len(spans) == 1 and spans[0][0] < 0.4 and spans[0][1] > 0.8


def test_emoji_events_detected(tmp_path):
    ws = [{"w": "This", "s": 0.0, "e": 0.3}, {"w": "house", "s": 0.3, "e": 0.7},
          {"w": "costs", "s": 0.7, "e": 1.0}, {"w": "money.", "s": 1.0, "e": 1.4},
          {"w": "Huge", "s": 3.5, "e": 3.8}, {"w": "mortgage", "s": 3.8, "e": 4.3}, {"w": "rates.", "s": 4.3, "e": 4.8}]
    ev = captions.build_ass(ws, 6, tmp_path / "e.ass", style="bold", emojis=True)
    assert ev and all((render.ASSETS / "emoji" / f"{c}.png").exists() for _, _, c in ev)
    assert ev[0][2] == "1f3e0"  # house
    assert captions.build_ass(ws, 6, tmp_path / "f.ass", emojis=False) == []


@pytest.mark.parametrize("aspect", ["1:1", "4:5", "16:9"])
def test_render_other_aspects(tmp_path, words, aspect):
    src = tmp_path / "src.mp4"
    _talk_video(src, 16)
    meta = {"width": 1280, "height": 720, "duration": 16, "has_audio": True, "fps": 25}
    clip = {"id": "a1", "start": 1.0, "end": 9.0, "hook": "Shape test", "aspect": aspect, "track": None}
    info = render.render_clip(tmp_path, src, meta, clip, words, {"layout": "auto", "style": "boxed"})
    w, h, _ = _dims(tmp_path / "a1.mp4")
    assert (w, h) == render.ASPECTS[aspect] and info["size"] == [w, h]


def test_render_cuts_fillers_manual_crop_emoji_logo_font(tmp_path):
    from clipviral import brands
    src = tmp_path / "src.mp4"
    _talk_video(src, 14)
    ws = [{"w": "Buying", "s": 1.0, "e": 1.4}, {"w": "a", "s": 1.4, "e": 1.5}, {"w": "house", "s": 1.5, "e": 2.0},
          {"w": "um", "s": 2.1, "e": 2.6}, {"w": "costs", "s": 2.7, "e": 3.1}, {"w": "money.", "s": 3.1, "e": 3.6},
          {"w": "Delete", "s": 3.8, "e": 4.2}, {"w": "these", "s": 4.2, "e": 4.6}, {"w": "words", "s": 4.6, "e": 5.0},
          {"w": "please.", "s": 5.0, "e": 5.4}, {"w": "Mortgage", "s": 5.6, "e": 6.2}, {"w": "rates", "s": 6.2, "e": 6.6},
          {"w": "matter.", "s": 6.6, "e": 7.2}]
    # brand with logo + custom font
    b = brands.create({"name": "Client A", "accent": "#00C2FF"})
    import cv2
    import numpy as np
    logo = np.zeros((80, 200, 4), np.uint8)
    logo[:, :, 1] = 200
    logo[:, :, 3] = 255
    b = brands.set_logo(b["id"], cv2.imencode(".png", logo)[1].tobytes())
    font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf")
    if font.exists():
        b = brands.set_font(b["id"], font.read_bytes(), "DejaVuSerif-Bold.ttf")
        assert b["font_family"]
    opts = {"layout": "auto", "style": "bold", "tighten": False, "cut_fillers": True, "emojis": True,
            **brands.render_opts(b["id"])}
    assert "logo" in opts
    clip = {"id": "m1", "start": 0.5, "end": 8.0, "hook": "Edit test", "layout": "manual", "crop_x": 0.2,
            "cuts": [[3.8, 5.45]], "track": None}
    info = render.render_clip(tmp_path, src, {"width": 1280, "height": 720, "duration": 14, "has_audio": True},
                              clip, ws, opts)
    _, _, d = _dims(tmp_path / "m1.mp4")
    # 7.5s clip minus "um" (~0.56s) minus the deleted words (1.65s)
    assert 4.9 < d < 5.6, d
    assert info["layout_used"] == "manual" and info["emoji_count"] >= 1
    srt = (tmp_path / "m1.srt").read_text()
    assert "Delete" not in srt and "um" not in srt.split()
    if font.exists():
        assert "fontsdir=" in (tmp_path / "m1.filter").read_text()
        assert b["font_family"] in (tmp_path / "m1.ass").read_text()
    brands.delete(b["id"])


def test_motion_tracking_follows_moving_subject(tmp_path):
    # a white square moving from left to right on black
    src = tmp_path / "move.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=25:d=8",
                    "-f", "lavfi", "-i", "color=c=white:s=120x120:r=25:d=8",
                    "-filter_complex", "[0][1]overlay=x='min(1100,t*150)':y=300",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    samples = reframe.track_motion(src, 0, 8)
    xs = [c for _, c in samples if c is not None]
    assert len(xs) > 10 and xs[0] < 0.25 and xs[-1] > 0.7
    keys = reframe.plan_path(samples, 8, 405 / 1280)
    assert keys and keys[-1][1] > keys[0][1]


def test_api_brands_words_and_clip_edits(monkeypatch, tmp_path):
    import json as _j
    from fastapi.testclient import TestClient
    from clipviral import brands, jobs, main
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    monkeypatch.setattr(brands, "BRANDS_DIR", tmp_path / "brands")
    queued = []
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: queued.append(a))
    c = TestClient(main.app)

    # brands
    r = c.post("/api/brands", json={"name": "Client", "accent": "#112233", "aspect": "4:5", "emojis": True})
    assert r.status_code == 200, r.text
    bid = r.json()["id"]
    assert c.post("/api/brands", json={"name": "Bad", "accent": "red"}).status_code == 400
    assert c.post("/api/brands", json={"name": "Bad", "aspect": "3:2"}).status_code == 400
    assert c.post(f"/api/brands/{bid}/logo", files={"file": ("x.png", b"not an image")}).status_code == 400
    assert c.post(f"/api/brands/{bid}/font", files={"file": ("x.txt", b"x")}).status_code == 400
    import cv2
    import numpy as np
    png = cv2.imencode(".png", np.full((20, 40, 3), 200, np.uint8))[1].tobytes()
    r = c.post(f"/api/brands/{bid}/logo", files={"file": ("l.png", png)})
    assert r.status_code == 200 and r.json()["logo_url"]
    assert c.get(r.json()["logo_url"]).status_code == 200
    assert [b["name"] for b in c.get("/api/brands").json()] == ["Client"]

    # a finished job with one clip
    job = jobs.create("demo", {"niche": "general", "aspect": "4:5", "brand_id": bid})
    assert job["options"]["aspect"] == "4:5" and job["options"]["brand_id"] == bid
    words = transcribe.parse_subtitles(SRT)["words"]
    d = jobs.job_dir(job["id"])
    (d / "transcript.json").write_text(_j.dumps({"words": words}))
    jobs.update(job["id"], status="done", meta={"duration": 30, "width": 1280, "height": 720, "has_audio": True},
                source="source.mp4", clips=[{"id": "c01", "start": 4.2, "end": 14.0, "hook": "h", "style": "bold",
                                             "layout": "auto", "accent": "#FFE14D", "position": "lower",
                                             "aspect": "4:5", "cuts": [], "status": "ready", "track": [[0, 0.5]]}])
    r = c.get(f"/api/jobs/{job['id']}/clips/c01/words")
    assert r.status_code == 200
    ws = r.json()["words"]
    inside = [w for w in ws if w["in"]]
    assert inside and any(not w["in"] for w in ws)  # context words included

    cut = [inside[1]["s"], inside[2]["e"]]
    r = c.patch(f"/api/jobs/{job['id']}/clips/c01",
                json={"cuts": [cut], "aspect": "1:1", "crop_x": 0.3, "emojis": True})
    assert r.status_code == 200, r.text
    clip = jobs.load(job["id"])["clips"][0]
    assert clip["cuts"] == [[round(cut[0], 3), round(cut[1], 3)]] and clip["aspect"] == "1:1"
    assert clip["layout"] == "manual" and clip["crop_x"] == 0.3 and clip["track"] is None and clip["emojis"] is True
    assert queued[-1] == (job["id"], "render", "c01")
    assert c.get(f"/api/jobs/{job['id']}/clips/c01/words").json()["words"][[w["s"] for w in ws].index(inside[1]["s"])]["cut"]
    assert c.patch(f"/api/jobs/{job['id']}/clips/c01", json={"aspect": "2:3"}).status_code == 400
    # end beyond the video is capped to the video length (30s here)
    assert c.patch(f"/api/jobs/{job['id']}/clips/c01", json={"start": 0, "end": 1000}).status_code == 200
    assert jobs.load(job["id"])["clips"][0]["end"] == 30
    # on a long video, clips longer than 15 minutes are refused
    jobs.update(job["id"], meta={"duration": 3600, "width": 1280, "height": 720, "has_audio": True})
    r = c.patch(f"/api/jobs/{job['id']}/clips/c01", json={"start": 0, "end": 901})
    assert r.status_code == 400 and "15 minutes" in r.json()["detail"]
    assert c.patch(f"/api/jobs/{job['id']}/clips/c01", json={"start": 0, "end": 900}).status_code == 200
    r = c.patch(f"/api/jobs/{job['id']}/clips/c01", json={"start": 1, "end": 2})
    assert r.status_code == 400  # < 3 seconds


# ---------------------------------------------------------------- visual (no-speech) clipping
def _action_video(path, with_audio=True):
    """60 s: calm static scene, with a burst of motion + noise between 30 s and 42 s."""
    vf = ("[0][1]overlay=x='if(between(t,30,42),mod(t*900,1100),100)':"
          "y='if(between(t,30,42),300+200*sin(t*9),300)'")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x223344:s=960x540:r=25:d=60",
           "-f", "lavfi", "-i", "color=c=orange:s=140x140:r=25:d=60"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "anoisesrc=d=60:c=pink:a=0.02",
                "-f", "lavfi", "-i", "anoisesrc=d=12:c=white:a=0.6"]
        vf += "[v];[3]adelay=30000|30000[n];[2][n]amix=inputs=2:duration=first[a]"
        cmd += ["-filter_complex", vf, "-map", "[v]", "-map", "[a]", "-c:a", "aac"]
    else:
        cmd += ["-filter_complex", vf]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "60", str(path)]
    subprocess.run(cmd, check=True)


def test_visual_candidates_find_the_action(tmp_path):
    from clipviral.pipeline import media, visual
    src = tmp_path / "act.mp4"
    _action_video(src)
    meta = media.probe(src)
    media.extract_audio(src, tmp_path / "a.wav")
    curve = media.loudness_curve(tmp_path / "a.wav")
    sig = visual.frame_signals(src, meta["duration"])
    assert sig["motion"].size > 100
    cands = visual.candidates(sig, curve, 3, 8, 15)
    best = cands[0]
    overlap = min(best["end"], 42) - max(best["start"], 30)
    assert overlap > 6, best
    picks = visual.heuristic_writeup(cands, "general", 2)
    assert picks[0]["engine"] == "visual-signals" and picks[0]["captions"]["x"]


def test_process_silent_video_end_to_end(monkeypatch, tmp_path):
    from clipviral import jobs
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(config, "AI_DISABLED", True)
    job = jobs.create("silent", {"niche": "general", "num_clips": 2, "min_len": 8, "max_len": 15})
    d = jobs.job_dir(job["id"])
    _action_video(d / "source.mp4", with_audio=False)
    jobs.update(job["id"], source="source.mp4")
    jobs.process(job["id"])
    j = jobs.load(job["id"])
    assert j["status"] == "done" and j["clips"], j.get("error")
    assert all(c["status"] == "ready" for c in j["clips"])
    assert any("Little speech" in n for n in j["notes"])
    assert any(min(c["end"], 42) - max(c["start"], 30) > 5 for c in j["clips"])


def test_claude_vision_request_shape(monkeypatch, tmp_path):
    from clipviral.pipeline import highlights, visual
    src = tmp_path / "act.mp4"
    _action_video(src, with_audio=False)
    cands = [{"start": 30.0, "end": 40.0, "signal": 1.0}, {"start": 5.0, "end": 15.0, "signal": 0.1}]
    sent = {}

    class Msgs:
        def parse(self, **kw):
            sent.update(kw)
            pick = visual.VisualPick(candidate=0, title="Orange box chaos", hook="Watch it fly", virality=88,
                                     scores=highlights.Scores(hook=90, flow=80, value=70, trend=75),
                                     why="Fast motion.", hashtags=["#satisfying"],
                                     captions=highlights.PlatformCaptions(tiktok="t", instagram="i", youtube="y", x="x"))
            return SimpleNamespace(stop_reason="end_turn", parsed_output=visual.VisualPlan(clips=[pick, pick]))

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda: SimpleNamespace(beta=SimpleNamespace(messages=Msgs())))
    out = visual.claude_rank(src, tmp_path, cands, [], "general", 2)
    assert len(out) == 1 and out[0]["start"] == 30.0 and out[0]["hashtags"] == ["satisfying"]
    imgs = [b for b in sent["messages"][0]["content"] if b["type"] == "image"]
    assert len(imgs) == 2 and imgs[0]["source"]["media_type"] == "image/jpeg" and len(imgs[0]["source"]["data"]) > 1000
    assert sent["output_format"] is visual.VisualPlan
