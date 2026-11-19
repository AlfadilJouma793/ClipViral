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
    assert info["layout_used"] == "fit"  # no faces in a test pattern
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
