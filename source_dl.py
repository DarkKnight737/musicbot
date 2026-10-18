"""Download the original studio version of a track via yt-dlp (YouTube)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

import config
import utils

log = logging.getLogger("source_dl")

# Noise patterns that identify covers/remixes/etc.
_BAD = re.compile(
    r"cover|karaoke|remix|rework|bootleg|nightcore|slowed|sped ?up|reverb|8d\b|"
    r"instrumental|reaction|mashup|medley|full album|1 ?hour|10 ?hours|"
    r"\blive\b|unplugged|acoustic|studio session|fan ?made|tutorial|"
    r"dancer|just ?dance|exercise|workout|line ?dance"
)
_GOOD = ("official audio", "official video", "official music", "official lyric",
         "lyric video", "official", "audio")

# Same noise, but for the CHANNEL/uploader name (e.g. "Sing King Karaoke",
# "Piano Karaoke", sunset-slowed-channels). Titles alone were not enough:
# a clean-titled karaoke upload on a karaoke channel used to outscore
# the real original.
_BAD_CH = re.compile(
    r"\bkaraoke\b|\bplay ?along\b|\bsing ?along\b|\btribute\b|"
    r"\bcover\b|\bcovers\b|\bbacking ?track\b|\binstrumental\b|"
    r"\bnightcore\b|\bslowed\b|\bsped ?up\b|\breverb\b|\b8d\b|"
    r"\bsoundalike\b|\bmade famous\b|\bin the style of\b"
)


def _score(entry: dict, artist: str, title: str, expected_dur: int | None) -> float:
    t_title = (entry.get("title") or "")
    low = t_title.lower()
    s = 0.0
    for w in _GOOD:
        if w in low:
            s += 2.0
            break
    # canonical upload style "Artist - Title (Official Audio)" starts with
    # the artist name — a strong original-version signal.
    if artist and low.startswith(artist.lower()):
        s += 3.0
    if _BAD.search(low):
        s -= 40.0
    if artist and artist.lower() in low:
        s += 5.0
    if title and title.lower() in low:
        s += 5.0
    ch = ((entry.get("channel") or entry.get("uploader") or "") or "").lower()
    if _BAD_CH.search(ch):
        s -= 40.0
    if artist and artist.lower() in ch:
        s += 6.0
    for w in ("topic", "vevo", "official"):
        if w in ch:
            s += 3.0
            break
    d = entry.get("duration") or 0
    if expected_dur and d:
        s -= min(abs(d - expected_dur) * 0.2, 12.0)
    return s


def _find_output(work_dir: Path) -> Path | None:
    """Locate the finished audio file inside a candidate's work dir.

    The ExtractAudio postprocessor always leaves `source.m4a` for an m4a
    request; the alternates cover exotic edge cases (fixup skipped, full
    track "best" download with no postprocessing, etc.).
    """
    for name in ("source.m4a", "source.mp3", "source.mp4",
                 "source.opus", "source.webm", "source.ogg"):
        p = work_dir / name
        if p.is_file():
            return p
    return None


async def fetch_original(
    title: str, artist: str, duration: int | None, dest_dir: Path | str
) -> Path | None:
    """Search YouTube for the original upload and download as native m4a.

    Returns the audio path, or None if nothing suitable is downloadable.

    Speed notes:
      * search uses `extract_flat` — one request instead of a full
        metadata fetch per candidate (saves ~10-25 s on typical queries),
      * the native AAC stream (~128 kbps, ~3.5 MB for a 4-minute track) is
        taken as-is wherever possible: no re-encode, <2 MB of download per
        delivery minute, and the ffmpeg "conversion" degenerates to a
        container fixup that takes well under a second,
      * if the source cannot be m4a (no AAC stream offered), the
        postprocessor still transcoded it — correctness over speed there.
    """
    import yt_dlp  # local import so bot can start even offline

    dest_dir = str(dest_dir)
    started = time.monotonic()
    query = f"ytsearch{config.YOUTUBE_CANDIDATES}:{artist} - {title} audio"
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 15,
        "retries": 2,
        "noplaylist": True,
        "ffmpeg_location": utils.find_ffmpeg(),
        # Flat search: no full page fetch per result => several times faster.
        "extract_flat": "in_playlist",
        "skip_download": True,
        "default_search": "ytsearch",
        "playlist_items": f"1:{config.YOUTUBE_CANDIDATES}",
    }

    def _search():
        with yt_dlp.YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(query, download=False)
        return list(info.get("entries") or [])

    try:
        entries = await asyncio.wait_for(
            asyncio.to_thread(_search), timeout=config.YOUTUBE_SEARCH_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("flat search for %r hit its %ss deadline",
                    f"{artist} - {title}", config.YOUTUBE_SEARCH_TIMEOUT)
        return None
    except Exception as e:
        log.error("yt-dlp search failed: %s", e)
        return None

    entries = [e for e in entries if e]
    entries.sort(key=lambda e: _score(e, artist, title, duration), reverse=True)
    log.info("flat search for %r done in %.1fs (%d candidates)",
             f"{artist} - {title}", time.monotonic() - started, len(entries))

    dl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 15,
        "retries": 2,
        "noplaylist": True,
        "ffmpeg_location": utils.find_ffmpeg(),
        "format": "bestaudio[ext=m4a][abr<=192]/bestaudio[ext=m4a]/bestaudio",
        # outtmpl is set per candidate (each gets its own dir) in _dl below
        "concurrent_fragment_downloads": 8,
        "postprocessors": [
            # Native m4a sources are NOT re-encoded — yt-dlp only repairs the
            # container (near-instant). The re-encode to m4a only happens for
            # the rare upload that offers no AAC stream at all.
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a",
             "preferredquality": "128"},
        ],
    }

    tried = 0
    for e in entries:
        score = _score(e, artist, title, duration)
        if score <= 0 and tried >= 2:
            continue
        if tried >= 3:
            break
        tried += 1
        # Distinct output dir per candidate: a download abandoned on timeout
        # keeps running in the background and must not collide with the next
        # candidate's writes, or the bot could deliver a half-written file.
        work_dir = Path(dest_dir, f"dl{tried}")
        url = e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}"
        try:
            def _dl():
                with yt_dlp.YoutubeDL({**dl_opts, "outtmpl": str(work_dir / "source.%(ext)s")}) as ydl:
                    ydl.download([url])
            # Deadline per candidate: if the thread exceeds it (server stall),
            # abandon the wait and try the next candidate. The detached thread
            # still terminates on its own socket timeouts; nothing is left
            # holding the user's flow hostage.
            await asyncio.wait_for(asyncio.to_thread(_dl),
                                   timeout=config.YOUTUBE_DOWNLOAD_TIMEOUT)
            mp3 = _find_output(work_dir)
            if mp3 and mp3.stat().st_size > 100_000:
                log.info("downloaded %s (score %.1f) in %.1fs",
                         url, score, time.monotonic() - started)
                return mp3
        except asyncio.TimeoutError:
            log.warning("download of %s exceeded its deadline; trying next candidate", url)
            continue
        except Exception as e:
            log.warning("download failed for %s: %s", url, e)
            continue
    return None
