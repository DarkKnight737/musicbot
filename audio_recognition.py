"""Song recognition (keyless Shazam fingerprint API)."""
from __future__ import annotations

import asyncio
import logging

import config
from models import TrackMeta
from utils import find_ffmpeg, run_cmd

log = logging.getLogger("recognize")

_shazam = None


def _pick_image(track: dict) -> str:
    imgs = track.get("images") or {}
    for k in ("coverart_hq", "coverart", "coverarthq", "background"):
        v = imgs.get(k)
        if v:
            return v
    share = (track.get("share") or {}).get("images") or {}
    for k in ("coverart", "background"):
        if share.get(k):
            return share[k]
    return ""


def _parse_shazam_result(out: dict, source: str = "shazam") -> TrackMeta | None:
    data = out.get("track") or {}
    if not data:
        return None
    title = (data.get("title") or "").strip()
    artist = (data.get("subtitle") or "").split(" - ")[0].split(",")[0].strip()
    if not title:
        return None
    meta = TrackMeta(
        title=title,
        artist=artist,
        artwork_url=_pick_image(data),
        source=source,
    )
    for section in data.get("sections") or []:
        rows = section.get("metadata") or []
        for row in rows:
            k = (row.get("title") or "").lower()
            v = (row.get("text") or "").strip()
            if not v:
                continue
            if k == "album":
                meta.album = v
            elif k in ("released", "release date"):
                meta.year = v[:4]
            elif k == "genre":
                meta.genre = v
            elif "music.apple.com" in v:
                meta.apple_url = v
    return meta


async def recognize_file(path: str) -> TrackMeta | None:
    """Fingerprint an audio file through Shazam's public endpoint (keyless).

    A single Shazam client is reused across calls (one TLS handshake for
    the whole bot lifetime instead of one per clip). The overall wait is
    bounded: if the service stalls, we fail before retry buttons go stale
    instead of waiting into aiohttp's multi-minute connection timeout.
    """
    global _shazam
    try:
        from shazamio import Shazam
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("shazamio is not installed; run: pip install shazamio") from e

    if _shazam is None:
        _shazam = Shazam()
    try:
        out = await asyncio.wait_for(_shazam.recognize(str(path)),
                                     timeout=config.SHAZAM_TIMEOUT)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"recognition timed out after {config.SHAZAM_TIMEOUT}s — try again") from None
    return _parse_shazam_result(out)


async def prepare_for_recognition(src: str, ffmpeg_dir: str | None, seconds: int) -> str:
    """Normalize any media to mono 16 kHz WAV, bounded at ~1 minute.

    shazamio's Rust core takes a centered 12 s segment from the WAV itself,
    so we just cap decode length to keep memory reasonable.
    """
    src_path = src
    out = str(src) + ".sample.wav"
    args = ["-y", "-loglevel", "error", "-thread_queue_size", "128",
            "-i", src_path, "-vn", "-ac", "1", "-ar", "16000"]
    keep = min(config.CLIP_KEEP_SECONDS, max(config.SHAZAM_SEGMENT_SECONDS * 2, 30))
    args += ["-t", str(keep)]
    args += [out]
    try:
        await run_cmd([find_ffmpeg() or "ffmpeg"] + args, timeout=90)
    except Exception:
        log.exception("ffmpeg normalization failed, using raw file")
        return src_path
    return out
