# Reelquarry

**One long video in. A week of shorts out.**

Reelquarry turns a 30-minute, 1-hour or 2-hour video (podcast, webinar, property tour, course lesson) into ranked, ready-to-post vertical clips for TikTok, Instagram Reels and YouTube Shorts.

Upload → transcribe every word → find the best moments → reframe to 9:16 around the speaker → animated captions + hook → download.

## What makes it different

| | |
|---|---|
| **Clips cut on complete thoughts** | Claude picks *sentence ranges*, not raw timestamps, so no clip starts mid-word or stops before the payoff. |
| **"Why this works"** | Every clip gets a 0–100 score with a hook / flow / value / trend breakdown and a plain-English reason. |
| **Camera-operator reframing** | Face tracking with a dead-zone: the crop holds steady, eases when the speaker moves, and snaps on camera cuts. No faces (slides, screen share)? It fits the frame over a blurred background instead. |
| **Dead-air removal** | Long pauses inside a clip are cut and the captions re-timed. |
| **Niche presets** | Podcasters, YouTubers, **UK property agents** (stamp duty, EPC, chains; compliance-safe hooks), coaches, agencies. You can also tell the AI what to look for. |
| **5 caption styles** | Bold Pop, Karaoke, Boxed, Minimal, Neon. Word-by-word highlighting, keyword emphasis, brand colour, @handle watermark, progress bar. |
| **Post copy included** | A TikTok caption, Instagram caption and YouTube Shorts title with hashtags for each clip. SRT per clip, and a ZIP export with everything. |
| **Platform audio** | Loudness normalised to −14 LUFS. |
| **Bring your own transcript** | Upload an SRT/VTT to skip transcription or fix names. |
| **Works without AI** | With no API key, a built-in scorer ranks moments on hook words, vocal energy, pace, payoff and niche keywords. |

## Run locally

Requirements: Python 3.11+, `ffmpeg` with libass (standard on most distros), and the Inter font for the best-looking captions (`apt install fonts-inter`).

```bash
python -m venv .venv && source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env   # add your ANTHROPIC_API_KEY
```

```bash
set -a && source .env && set +a && uvicorn reelquarry.main:app --reload --port 8000
```

Open http://localhost:8000 for the landing page and http://localhost:8000/studio for the app.

The first transcription downloads the Whisper model (~150 MB for `base`) from huggingface.co.

### Docker

```bash
docker build -t reelquarry .
```

```bash
docker run -p 8000:8000 -v reelquarry-data:/data -e ANTHROPIC_API_KEY=sk-ant-... reelquarry
```

## How it works

```
reelquarry/
  main.py               FastAPI: upload, jobs, clip edits, downloads, ZIP export
  jobs.py               job store (one folder per project) + background worker queue
  pipeline/
    media.py            ffprobe, audio extraction, loudness curve
    transcribe.py       faster-whisper word timestamps, SRT/VTT import, sentence splitting
    highlights.py       Claude highlight picker (structured output) + offline signal scorer
    niches.py           audience presets
    reframe.py          face tracking -> steady camera path -> ffmpeg crop expression
    captions.py         animated ASS captions, hook card, watermark, progress bar
    render.py           single-pass ffmpeg render (crop, de-silence, loudnorm, burn-in)
web/                    landing page + studio (vanilla HTML/CSS/JS, no build step)
scripts/make_demo_video.py   builds an offline test video + SRT
tests/                  pytest suite (includes a real ffmpeg render)
```

**Highlight detection with Claude** (`highlights.py`): the transcript is sent as numbered sentences with timestamps and vocal-energy tags. Claude returns a validated `ClipPlan` (Pydantic structured output) with sentence ranges, hook, scores, reasoning, emphasis words, hashtags and per-platform captions. Ranges are then fitted to the requested length, and overlapping picks are dropped. A refusal or API failure falls back to the offline scorer, so a job never fails because of the AI step.

**Face tracking** (`reframe.py`): uses YuNet (set `REELQUARRY_FACE_MODEL`, baked into the Docker image), else dlib's HOG detector, else OpenCV Haar cascades.

## Tests

```bash
pip install -r requirements-dev.txt && python -m pytest -q
```

To try the full pipeline offline, build the demo video, then upload `demo/demo.mp4` with `demo/demo.srt` as the transcript:

```bash
python scripts/make_demo_video.py demo
```

## Not built yet (needed before charging customers)

- User accounts and authentication. The API is currently open to anyone who can reach the server.
- Stripe billing and enforcing plan limits (upload minutes per month, max video length).
- Object storage (S3/R2) and a CDN for uploads and renders, plus auto-deleting old projects.
- A GPU worker pool for faster transcription at scale (set `REELQUARRY_WHISPER_DEVICE=cuda`, `REELQUARRY_WORKERS`).
- Importing from a YouTube/Drive link, and direct publishing to TikTok, Instagram and YouTube.
- Resumable or chunked uploads for multi-GB files on poor connections.
