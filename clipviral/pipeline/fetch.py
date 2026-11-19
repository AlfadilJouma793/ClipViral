"""Import a video from a link (YouTube, Vimeo, TikTok, Instagram, X, Facebook,
Dropbox/Drive direct links, plain .mp4 URLs, ...) using yt-dlp."""
from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .. import config


class LinkError(ValueError):
    """A problem the customer can fix (bad link, private video, too long...)."""


def validate_url(url: str) -> str:
    url = (url or "").strip()
    if len(url) > 2000:
        raise LinkError("That link is too long.")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise LinkError("Paste a full link starting with https://")
    if os.environ.get("CLIPVIRAL_ALLOW_PRIVATE_URLS") == "1":
        return url
    # Block links to the server's own network (SSRF protection).
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        raise LinkError("We couldn't find that website. Check the link and try again.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise LinkError("That link points to a private network address.")
    return url


def _friendly(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "private" in low or "sign in" in low or "login" in low or "members-only" in low:
        return "This video is private or needs a login. Make it public/unlisted, or download it and upload the file."
    if "unsupported url" in low:
        return "We can't import from that website. Download the video and upload the file instead."
    if "too long" in low or "duration" in low:
        return msg
    if "unavailable" in low or "removed" in low or "404" in low:
        return "That video is unavailable (removed, region-locked or the link is wrong)."
    if "max-filesize" in low or "larger than max" in low:
        return "That video file is larger than the upload limit."
    return "Couldn't download the video: " + msg.replace("ERROR: ", "")[:300]


def _js_runtimes() -> dict:
    """YouTube needs a JavaScript runtime; prefer the pip-installed Deno."""
    import shutil
    runtimes = {}
    try:
        import deno
        runtimes["deno"] = {"path": deno.find_deno_bin()}
    except Exception:
        if shutil.which("deno"):
            runtimes["deno"] = {}
    if shutil.which("node"):
        runtimes["node"] = {}
    return runtimes


def download(url: str, dest_dir: Path, on_progress: Callable[[float], None] | None = None) -> dict:
    """Download to dest_dir/source.<ext>. Returns {"path", "title", "duration", "extractor"}."""
    import yt_dlp

    url = validate_url(url)
    max_secs = config.MAX_VIDEO_SECONDS

    def too_long(info, *, incomplete=False):
        d = info.get("duration")
        if d and d > max_secs:
            return f"This video is {d / 60:.0f} minutes long; the limit is {max_secs / 60:.0f} minutes."
        return None

    def hook(d):
        if on_progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                on_progress(min(1.0, d.get("downloaded_bytes", 0) / total))

    opts = {
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        # best quality up to 1080p; prefer mp4/m4a so merging is a fast remux
        "format": ("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/"
                   "b[height<=1080]/b"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlist_items": "1",
        "match_filter": too_long,
        "max_filesize": config.MAX_UPLOAD_BYTES,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "restrictfilenames": True,
    }
    js = _js_runtimes()
    if js:
        opts["js_runtimes"] = js
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise LinkError("Nothing to download at that link.")
            if info.get("_type") == "playlist":
                entries = [e for e in info.get("entries") or [] if e]
                if not entries:
                    raise LinkError("That playlist is empty.")
                info = entries[0]
    except LinkError:
        raise
    except yt_dlp.utils.DownloadError as exc:
        raise LinkError(_friendly(exc)) from exc

    files = [p for p in dest_dir.glob("source.*") if p.suffix not in (".part", ".ytdl")]
    if not files:
        d = info.get("duration")
        if d and d > max_secs:
            raise LinkError(too_long(info))
        raise LinkError("The download didn't produce a video file. Try uploading the file instead.")
    path = max(files, key=lambda p: p.stat().st_size)
    # Direct file links don't report a duration up front: check the real file.
    from .media import MediaError, probe
    try:
        real = probe(path)["duration"]
    except MediaError:
        path.unlink(missing_ok=True)
        raise LinkError("That link didn't contain a playable video.")
    if real > max_secs:
        path.unlink(missing_ok=True)
        raise LinkError(f"This video is {real / 60:.0f} minutes long; the limit is {max_secs / 60:.0f} minutes.")
    return {"path": path, "title": (info.get("title") or "").strip()[:80],
            "duration": info.get("duration"), "extractor": info.get("extractor_key") or ""}
