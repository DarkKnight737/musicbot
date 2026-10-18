"""Local track cache: reuse already fetched & tagged audio files.

Keyed by (artist, title). Cache hits skip the yt-dlp fetch, the cover
download and tagging entirely — they deliver in one upload. The audio
is stored under its own extension (track.m4a / track.mp3).
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

import config
from models import TrackMeta

log = logging.getLogger("cache")

_META_FIELDS = ("title", "artist", "album", "year", "genre", "duration",
                "track_no", "track_total", "apple_url", "artwork_url",
                "tg_file_id")


def _dir(artist: str, title: str) -> Path:
    raw = f"{artist.strip().lower()}|{title.strip().lower()}".encode("utf-8")
    return config.CACHE_DIR / hashlib.sha1(raw).hexdigest()[:16]


def _audio_file(d: Path) -> Path | None:
    """Find the cached audio regardless of extension (track.*)."""
    for name in ("track.m4a", "track.mp3", "track.mp4"):
        p = d / name
        if p.is_file():
            return p
    return None


def get(artist: str, title: str):
    """Return (audio_path, thumb_path|None, TrackMeta) or None if not cached."""
    d = _dir(artist, title)
    mp3 = _audio_file(d)
    meta_json = d / "meta.json"
    if not (mp3 is not None and meta_json.is_file()):
        return None
    try:
        data = json.loads(meta_json.read_text("utf-8"))
        defaults = TrackMeta(title="", artist="")
        meta = TrackMeta(**{k: data.get(k, getattr(defaults, k)) for k in _META_FIELDS})
    except Exception:
        log.exception("corrupt cache entry in %s", d)
        return None
    thumb = d / "thumb.jpg"
    log.info("cache hit for %s — %s", artist, title)
    return mp3, (thumb if thumb.is_file() else None), meta


def put(artist: str, title: str, mp3_path: str | Path,
        thumb_path: str | Path | None, meta: TrackMeta) -> None:
    """Copy the finished artifacts into the cache (call in a worker thread)."""
    d = _dir(artist, title)
    tmp = d.with_name(d.name + ".part")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(str(mp3_path), tmp / f"track{Path(mp3_path).suffix.lower() or '.m4a'}")
        if thumb_path and Path(thumb_path).is_file():
            shutil.copy2(str(thumb_path), tmp / "thumb.jpg")
        (tmp / "meta.json").write_text(
            json.dumps({k: getattr(meta, k) for k in _META_FIELDS}, ensure_ascii=False),
            encoding="utf-8")
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        tmp.rename(d)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    _prune()


def _prune() -> None:
    """Drop oldest entries when the cache exceeds its byte budget."""
    entries = []
    for d in config.CACHE_DIR.iterdir():
        if not d.is_dir():
            continue
        mp3 = _audio_file(d)
        if mp3 is None:
            continue
        size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
        entries.append((mp3.stat().st_mtime, d))
    total = sum((sum(f.stat().st_size for f in d.iterdir() if f.is_file())
                 for _, d in entries), start=0)
    if total <= config.CACHE_MAX_BYTES:
        return
    entries.sort()
    for _, d in entries:
        if total <= config.CACHE_MAX_BYTES:
            break
        size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
        shutil.rmtree(d, ignore_errors=True)
        total -= size
