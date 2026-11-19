# ClipViral

**One long video in. A week of shorts out.**

ClipViral turns a 30-minute, 1-hour or 2-hour video (podcast, webinar, property tour, course lesson, gameplay) into ranked, ready-to-post clips for TikTok, Instagram Reels, YouTube Shorts and X, and can publish or schedule them for you.

Upload or paste a link → transcribe every word → find the best moments → reframe around the speaker or subject → animated captions, emojis, B-roll and a hook → download, post or schedule.

## What makes it different

| | |
|---|---|
| **Clips cut on complete thoughts** | Claude picks *sentence ranges*, not raw timestamps, so no clip starts mid-word or stops before the payoff. |
| **"Why this works"** | Every clip gets a 0–100 score with a hook / flow / value / trend breakdown and a plain-English reason. |
| **Camera-operator reframing** | Face tracking with a dead-zone: the crop holds steady, eases when the speaker moves, and snaps on camera cuts. No faces (slides, screen share)? It fits the frame over a blurred background instead. |
| **Dead-air removal** | Long pauses inside a clip are cut and the captions re-timed. |
| **Niche presets** | Podcasters, YouTubers, **UK property agents** (stamp duty, EPC, chains; compliance-safe hooks), coaches, agencies. You can also tell the AI what to look for. |
| **5 caption styles** | Bold Pop, Karaoke, Boxed, Minimal, Neon. Word-by-word highlighting, keyword emphasis, brand colour, @handle watermark, progress bar. |
| **Post copy included** | A TikTok caption, Instagram caption, YouTube Shorts title and X post for each clip. SRT per clip, and a ZIP export with everything. |
| **Platform audio** | Loudness normalised to −14 LUFS. |
| **Paste a link** | Import from YouTube, Vimeo, TikTok, Instagram, X, Facebook, Twitch, Dropbox or any direct video URL (yt-dlp, up to 1080p, length-capped, private-network links blocked, rights confirmation required). |
| **Platform presets** | TikTok, Instagram Reels and X modes tune the AI's picks, clip length, caption style and post copy to each platform. |
| **Visual AI clipping** | Videos with little or no speech (sports, gaming, vlogs, demos) are clipped from motion, scene cuts and sound; with an API key Claude also looks at frame strips of each candidate. |
| **Any-subject reframing** | Follows faces, or the main moving subject when there is no face. Or drag the crop box yourself. |
| **Every shape, up to 15 minutes** | 9:16, 1:1, 4:5 and 16:9 outputs; clips from 15 seconds to 15 minutes. |
| **Text-based editing** | Click words in the transcript to cut them; set start and end by clicking words. Filler words are cut from the audio and video. |
| **Emoji captions** | Colour emoji pop-ups matched to what is said (Twemoji artwork, see NOTICE). |
| **AI B-roll** | Stock cutaways from Pexels at the right moments, with credits. |
| **Brand templates** | Saved colours, caption style, logo, custom font, watermark and default shape per client. |
| **Publish & schedule** | Connect YouTube, TikTok, Instagram and X; post now or schedule, with retries. |
| **Accounts, teams & billing** | Free plan plus £10 / £15 / £29 plans via Stripe, monthly minute metering, team seats, Pro API keys. |
| **Bring your own transcript** | Upload an SRT/VTT to skip transcription or fix names. |
| **Works without AI** | With no API key, a built-in scorer ranks moments on hook words, vocal energy, pace, payoff and niche keywords. |

## Run locally

### In the browser with GitHub Codespaces (nothing to install)

1. Optional: add your Anthropic key at GitHub **Settings → Codespaces → Secrets → New secret**, named `ANTHROPIC_API_KEY`, with access to this repo.
2. On the repo page, click **Code → Codespaces → Create codespace on main**.
3. Wait for setup (about 3–5 minutes the first time). The app starts by itself and opens in a new tab. If it doesn't, open the **Ports** tab and click the globe icon next to port 8000, then add `/studio` to the address.
4. When you're finished, stop the codespace (**Code → Codespaces → … → Stop**) so it doesn't use your free hours.

### Mac: one click

1. Download the project (green **Code** button on GitHub, then **Download ZIP**) and unzip it.
2. In Finder, right-click **`start-mac.command`**, choose **Open**, then **Open** again. (macOS asks this the first time because the file was downloaded.)
3. Wait. The first run installs Homebrew, Python 3.12, ffmpeg and the app's packages, which takes 5–15 minutes. It asks once for an optional Anthropic API key.
4. Your browser opens the studio at http://localhost:8000/studio. Close the Terminal window to stop the app.

Next time, just double-click `start-mac.command`. It starts in a few seconds.

### Manual setup

The app needs Python 3.10 or newer (3.12 recommended). On macOS use Homebrew's `python@3.12`, not the system Python; a few packages have no Mac build for 3.13 yet.

Requirements: `ffmpeg` with libass (standard on most distros) and the Inter font for the best-looking captions (`apt install fonts-inter`).

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
uvicorn clipviral.main:app --reload --port 8000
```

Settings in `.env` are read automatically. Open http://localhost:8000, click **Start free** and create an account (the first account also takes ownership of any projects made before accounts existed).

The first transcription downloads the Whisper model (~150 MB for `base`) from huggingface.co.

### Docker

```bash
docker build -t clipviral .
```

```bash
docker run -p 8000:8000 -v clipviral-data:/data -e ANTHROPIC_API_KEY=sk-ant-... clipviral
```

## Going live

Everything below is optional; features without keys are hidden or disabled in the studio.

| Feature | What to set | Notes |
|---|---|---|
| AI clip picking, hooks, captions | `ANTHROPIC_API_KEY` | Without it the offline scorer is used. |
| Payments | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_STARTER/CREATOR/PRO`, `CLIPVIRAL_PUBLIC_URL` | In Stripe create three monthly GBP prices (£10, £15, £29). Add a webhook endpoint `https://YOUR-DOMAIN/api/billing/webhook` for `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`. Enable the Customer Portal. |
| B-roll | `PEXELS_API_KEY` | Free key from pexels.com/api. |
| YouTube | `YOUTUBE_CLIENT_ID/SECRET` | Google Cloud project with YouTube Data API v3, OAuth web client, redirect `https://YOUR-DOMAIN/api/social/callback/youtube`. Uploads from unverified projects may be restricted to private until Google's audit is passed. Uploads use a large share of the default daily API quota, so request a quota increase in the Google console before launch. |
| TikTok | `TIKTOK_CLIENT_KEY/SECRET` | TikTok developer app with Login Kit + Content Posting API (Direct Post), redirect `.../api/social/callback/tiktok`. Until TikTok audits the app, posts are limited to private ("Only me"). |
| Instagram | `INSTAGRAM_APP_ID/SECRET` | Meta app with "Instagram API with Instagram Login", permissions `instagram_business_basic` and `instagram_business_content_publish`, redirect `.../api/social/callback/instagram`. Needs a professional (business/creator) account, Meta app review for public use, and the site on a public **https** address because Instagram fetches the video by URL. |
| X | `X_CLIENT_ID/SECRET` | X developer app with OAuth 2.0 (confidential client), redirect `.../api/social/callback/x`, scopes incl. `media.write`. Posting video requires a paid X API tier. Clips over 140 s can't be posted to X. |
| Security | `CLIPVIRAL_SECURE_COOKIES=1`, `CLIPVIRAL_SECRET_KEY` | Serve over HTTPS. The secret key encrypts stored social tokens; keep it safe and stable. |

The API reference is served at `/docs`. API keys (Pro) are sent as `Authorization: Bearer cv_live_...`.

## How it works

```
clipviral/
  main.py               FastAPI: auth, projects, clip edits, brands, billing, social, downloads
  accounts.py           users, sessions, workspaces/teams, invites, plans, usage, API keys (SQLite)
  billing.py            Stripe Checkout, Customer Portal, webhook
  brands.py             brand templates (logo, font, colours)
  social.py             YouTube / TikTok / Instagram / X connect + publish, scheduler
  jobs.py               job store (one folder per project) + background worker queue
  pipeline/
    media.py            ffprobe, audio extraction, loudness curve
    transcribe.py       faster-whisper word timestamps, SRT/VTT import, sentence splitting
    highlights.py       Claude highlight picker (structured output) + offline signal scorer
    niches.py           audience presets
    reframe.py          face tracking -> steady camera path -> ffmpeg crop expression
    captions.py         animated ASS captions, hook card, watermark, progress bar
    render.py           single-pass ffmpeg render (shapes, cuts, B-roll, emojis, logo, loudnorm, captions)
    visual.py           motion / scene / sound moment finding + Claude vision ranking
    broll.py            B-roll planning and Pexels stock footage
    fetch.py            link import (yt-dlp)
    emoji_map.py        spoken keyword -> emoji
web/                    landing page + studio (vanilla HTML/CSS/JS, no build step)
scripts/make_demo_video.py   builds an offline test video + SRT
tests/                  pytest suite (includes a real ffmpeg render)
```

**Highlight detection with Claude** (`highlights.py`): the transcript is sent as numbered sentences with timestamps and vocal-energy tags. Claude returns a validated `ClipPlan` (Pydantic structured output) with sentence ranges, hook, scores, reasoning, emphasis words, hashtags and per-platform captions. Ranges are then fitted to the requested length, and overlapping picks are dropped. A refusal or API failure falls back to the offline scorer, so a job never fails because of the AI step.

**Face tracking** (`reframe.py`): uses YuNet (set `CLIPVIRAL_FACE_MODEL`, baked into the Docker image), else dlib's HOG detector, else OpenCV Haar cascades.

## Tests

```bash
pip install -r requirements-dev.txt && python -m pytest -q
```

To try the full pipeline offline, build the demo video, then upload `demo/demo.mp4` with `demo/demo.srt` as the transcript:

```bash
python scripts/make_demo_video.py demo
```

## Not built yet

- Emails: password reset, email verification and sending invite emails (invite links are copied by hand).
- Object storage (S3/R2) and a CDN for uploads and renders, plus auto-deleting old projects.
- A GPU worker pool for faster transcription at scale (set `CLIPVIRAL_WHISPER_DEVICE=cuda`, `CLIPVIRAL_WORKERS`).
- Resumable or chunked uploads for multi-GB files on poor connections.
- AI-generated (not stock) B-roll visuals.
- Live trend data for the "trend" score (it is the AI's judgement today).

Emoji artwork: Twemoji, CC-BY 4.0 (see NOTICE).
