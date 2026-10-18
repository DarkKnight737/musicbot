"""Embed album cover + full metadata (ID3 for mp3, MP4 atoms for m4a),
and build a Telegram thumb.

The thumbnail is made with Pillow (pure compute, ~50 ms) instead of an
ffmpeg subprocess, so delivering a track no longer needs ffmpeg present
— the audio-recognition path is the only remaining ffmpeg user.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from mutagen.id3 import (
    APIC, ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPOS, TPUB, TRCK, TPE1, TPE2,
)
from mutagen.mp4 import MP4, MP4Cover

log = logging.getLogger("tagger")


def embed_tags(mp3: str | Path, meta) -> None:
    """Write title/artist/album/year/genre/track + cover art (mutagen)."""
    tags = ID3()
    tags.add(TIT2(encoding=3, text=meta.title))
    tags.add(TPE1(encoding=3, text=meta.artist))
    if meta.album:
        tags.add(TALB(encoding=3, text=meta.album))
        tags.add(TPE2(encoding=3, text=meta.artist))
    if meta.year:
        tags.add(TDRC(encoding=3, text=meta.year[:4]))
    if meta.genre:
        tags.add(TCON(encoding=3, text=meta.genre))
    if meta.track_no:
        txt = f"{meta.track_no}/{meta.track_total}" if meta.track_total else str(meta.track_no)
        tags.add(TRCK(encoding=3, text=txt))
        tags.add(TPOS(encoding=3, text="1"))
    tags.save(mp3, v1=2, v2_version=3)


def embed_all(mp3: str | Path, meta, cover: bytes | None = None) -> None:
    """Single-pass tag write: all text tags + artwork in one save.

    Dispatches by file type: m4a (the fast delivery path) gets MP4 atoms,
    mp3 keeps ID3v2.3. Replaces the old embed_tags + embed_cover pair
    (which rewrote the file twice) — saves ~0.5-1 s per delivery for a
    10 MB mp3.
    """
    try:
        if Path(mp3).suffix.lower() in (".m4a", ".mp4", ".aac"):
            _embed_m4a(mp3, meta, cover)
            log.info("tags written to %s", mp3)
            return
        try:
            audio = ID3(mp3)
        except ID3NoHeaderError:
            audio = ID3()
        audio.setall("APIC", [])
        _add_text_tags(audio, meta)
        if cover:
            data, mime = _normalize_cover(cover)
            audio.add(APIC(encoding=0, mime=mime,
                           type=3, desc="Album cover", data=data))
        audio.save(mp3, v1=2, v2_version=3)
        log.info("tags written to %s", mp3)
    except Exception:
        log.exception("tag embedding failed")


def _embed_m4a(path: str | Path, meta, cover: bytes | None = None) -> None:
    """Write MP4 atoms + cover into an m4a container (mutagen MP4)."""
    audio = MP4(str(path))
    audio["\xa9nam"] = [meta.title]
    audio["\xa9ART"] = [meta.artist]
    if meta.album:
        audio["\xa9alb"] = [meta.album]
        audio["aART"] = [meta.artist]
    if meta.year:
        audio["\xa9day"] = [meta.year[:4]]
    if meta.genre:
        audio["\xa9gen"] = [meta.genre]
    if meta.track_no:
        total = meta.track_total if meta.track_total else 0
        audio["trkn"] = [(int(meta.track_no), int(total))]
    if cover:
        data, mime = _normalize_cover(cover)
        img_format = (MP4Cover.FORMAT_PNG if mime == "image/png"
                      else MP4Cover.FORMAT_JPEG)
        audio["covr"] = [MP4Cover(data, imageformat=img_format)]
    audio.save()


def _normalize_cover(cover: bytes) -> tuple[bytes, str]:
    """Ensure the artwork is something APIC handles (jpeg/png); convert others."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(cover))
        fmt = img.format
        if fmt == "JPEG":
            img.close()
            return cover, "image/jpeg"
        if fmt == "PNG":
            img.close()
            return cover, "image/png"
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=88)
        img.close()
        return buf.getvalue(), "image/jpeg"
    except Exception:
        log.exception("cover normalizing failed, embedding raw bytes")
        return cover, "image/jpeg"


def read_duration(mp3: str | Path) -> int:
    try:
        if Path(mp3).suffix.lower() in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4
            a = MP4(str(mp3))
            if getattr(a, "info", None):
                return int(round(a.info.length))
        from mutagen.mp3 import MP3
        a = MP3(str(mp3))
        if getattr(a, "info", None):
            return int(round(a.info.length))
    except Exception:
        # may be a non-mp3 header; fall back to a generic mutagen file
        try:
            from mutagen import File
            a = File(str(mp3))
            if a is not None and getattr(a, "info", None):
                return int(round(a.info.length))
        except Exception:
            log.exception("read duration failed")
    return 0


def make_thumbnail(cover_path: str | Path, thumb_path: str | Path) -> None:
    """Square ≤320 px jpeg for send_audio (Pillow; run in a worker thread)."""
    from PIL import Image

    img = Image.open(cover_path).convert("RGB")
    img.thumbnail((320, 320))
    w, h = img.size
    side = max(w, h, 320)
    canvas = Image.new("RGB", (side, side), (0, 0, 0))
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    # keep under Telegram's 200 kB thumbnail budget regardless of source quality
    canvas.save(thumb_path, "JPEG", quality=85)
