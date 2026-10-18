"""Canonical track metadata + cover art via the iTunes Search API (keyless).

Text-search candidates are ordered originals-first: the karaoke / tribute /
cover / instrumental releases that iTunes regularly mixes into plain text
searches are pushed below the real studio recordings. The same penalty
defends the voice pipeline's metadata match (`best_itunes_match`).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urlencode

import httpx

from models import TrackMeta

log = logging.getLogger("catalog")

ITUNES = "https://itunes.apple.com/search?"

_HEADERS = {"User-Agent": "TelegramMusicFinder/1.0"}

# One shared client = TCP/TLS connections are reused instead of a fresh
# handshake for every request (saves ~0.3-0.5 s per call on cold start).
# iTunes is usually sub-second; 12 s fails a stalled call fast so the bot
# can surface the failure for a manual 🔄 Retry instead of hanging.
_CLIENT_TIMEOUT = httpx.Timeout(12.0, connect=6.0, pool=5.0)
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:            # never create two clients on a race
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=_CLIENT_TIMEOUT,
                follow_redirects=True,
                headers=_HEADERS,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
    return _client


async def close() -> None:
    global _client
    async with _client_lock:
        if _client is not None:
            await _client.aclose()
            _client = None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


# Titles like "Karaoke Version", "originally performed by…" or the genre
# "Karaoke" mark re-recordings. iTunes text search freely mixes those in —
# sometimes even above the studio original — so they are ranked LAST
# regardless of how well their text matched the query.
_KARAOKE = re.compile(
    r"\bkaraoke\b|\bplay ?along\b|\bsing ?along\b|"
    r"\bpiano version\b|\bstring version\b|\bvocal version\b|"
    r"\bvocal cover\b|\bcover version\b|\bcover band\b|"
    r"\btribute\b|\bmade famous by\b|\boriginally performed by\b|"
    r"\bin the style of\b|\bsoundalike\b|\binterpretation\b"
)

# Badges that mark the CANONICAL studio release (as iTunes names it):
# explicit "Album Version"/"Single Version" track titles and the
# "... - Single" album suffix that the store gives to singles.
_ORIG_MARK = re.compile(
    r"\b(album|single) version\b|\boriginal (album|studio|version)\b|"
    r"\bremaster(ed|ings?)\b|\bsingle\b"
)


def _is_karaokeish(m: TrackMeta) -> bool:
    nt, nal = _norm(m.title), _norm(m.album)
    if _norm(m.genre) == "karaoke":
        return True
    if _KARAOKE.search(nt + " " + nal):
        # A song TITLE that is literally just one of those words
        # ("Tribute" by Tenacious D, "Covers"…) is still a studio cut —
        # only context marks a re-recording.
        return nt not in ("tribute", "cover", "covers", "karaoke",
                          "play along", "sing along")
    return False


def high_res_artwork(url: str) -> str:
    if not url:
        return ""
    # artworkUrl100 -> 1000x1000 version
    return re.sub(r"/[\d]+x[\d]+bb\.jpg", "/1000x1000bb.jpg", url)


# ------------------------------------------------------------ result caches
# Shazam often recognizes the same clip if the user retries; repeated
# identical queries (candidates picking an item then re-searching) are common.
_META_TTL = 300          # canonical "artist + title" metadata: 5 min
_COVER_TTL = 3600        # artwork bytes: 1 h
_META_CACHE: dict[str, tuple[float, TrackMeta | None]] = {}
_COVER_CACHE: dict[str, tuple[float, bytes]] = {}
_CACHE_LOCK = asyncio.Lock()


def _cached(store: dict, key: str, ttl: float):
    hit = store.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    return None


async def lookup_itunes(title_artist: str, limit: int = 5) -> list[TrackMeta]:
    """Search the iTunes catalog: title, artist, album, year, genre, track no, cover.

    Raises on any failure — the bot shows the user a 🔄 Retry button instead
    of retrying automatically.
    """
    url = ITUNES + urlencode([
        ("term", title_artist),
        ("media", "music"),
        ("entity", "song"),
        ("limit", limit),
    ])
    client = await _get_client()

    def _parse(data: dict) -> list[TrackMeta]:
        out: list[TrackMeta] = []
        for it in data.get("results", []):
            if it.get("wrapperType") != "track":
                continue
            meta = TrackMeta(
                title=it.get("trackName") or "",
                artist=it.get("artistName") or "",
                album=it.get("collectionName") or "",
                year=(it.get("releaseDate") or "")[:4],
                genre=it.get("primaryGenreName") or "",
                duration=it.get("trackTimeMillis", 0) // 1000,
                artwork_url=high_res_artwork(it.get("artworkUrl100") or ""),
                track_no=it.get("trackNumber") or 0,
                track_total=it.get("trackCount") or 0,
                source="itunes",
                apple_url=it.get("trackViewUrl") or "",
            )
            out.append(meta)
        return out

    try:
        r = await client.get(url)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        log.warning("itunes lookup failed (%s): %s", e.response.status_code, url)
        raise
    except (httpx.TimeoutException, httpx.TransportError) as e:
        log.warning("itunes lookup unreachable: %s", e)
        raise

    return _parse(r.json())


async def best_itunes_match(artist: str, title: str) -> TrackMeta | None:
    """Fetch canonical catalog metadata for a recognized track (or text query)."""
    key = _norm(artist) + "|" + _norm(title)
    hit = _cached(_META_CACHE, key, _META_TTL)
    if hit is not None:
        return hit

    candidates = await lookup_itunes(f"{artist} {title}", limit=8)
    best = _pick_best(candidates, artist, title) if candidates else None

    async with _CACHE_LOCK:
        _META_CACHE[key] = (time.monotonic(), best)
        if len(_META_CACHE) > 256:
            _prune(_META_CACHE, _META_TTL)
    return best


def exact_text_match(query: str, m: TrackMeta) -> bool:
    """True when the top hit is exactly what the user asked for.

    "artist - title" queries match artist and title separately; single-term
    queries (usually bare titles) match the title or the artist+title combo.
    Call only on results already underscored originals-first by text_search,
    and only as a shortcut ("- Song (Official Video)" releases don't match,
    so a strict equality test is safe).
    """
    if " - " in query:
        a, _, t = query.partition(" - ")
        return (_norm(m.artist) == _norm(a) and _norm(m.title) == _norm(t.strip()))
    q = _norm(query)
    if not q:
        return False
    return q in (_norm(m.artist), _norm(m.title),
                 " ".join((_norm(m.artist), _norm(m.title))).strip())


def _pick_best(candidates: list[TrackMeta], artist: str, title: str) -> TrackMeta | None:
    """Best canonical match for a KNOWN artist/title (voice path).

    Returns the top-ranked candidate; karaoke/tribute/cover releases rank
    below any studio release no matter how well their text matched, so a
    proper original is preferred whenever one exists in the results.
    """
    if not candidates:
        return None
    return _rank_metadata(candidates, artist, title)[0]


def _rank_metadata(candidates: list[TrackMeta], artist: str = "",
                   title: str = "") -> list[TrackMeta]:
    """Order candidates: studio originals first, karaoke/tribute last.

    Scoring layers (each is an intentional ranking tier):
      * text similarity — how well artist+title match the query;
      * karaoke/tribute/cover penalty — non-original releases sink,
        but stay in the list (a user typing "X karaoke" might want one);
      * release-type bonus — "Album Version", "Single", remasters etc.
        are strong original-release markers.
    """
    na, nt = _norm(artist), _norm(title)
    cand_key = (na + " " + nt).strip()

    def score(m: TrackMeta) -> tuple[float, float]:
        key = _norm(m.artist) + " " + _norm(m.title)
        s = 0.0
        if nt and nt in key:
            s += 3.0
        if na and na in key:
            s += 3.0
        if cand_key and key == cand_key:
            s += 6.0
        text = s
        # karaoke / tribute / cover sinks below any studio release,
        # and among originals the closest text match wins first
        if _is_karaokeish(m):
            s -= 100.0
        elif _ORIG_MARK.search(_norm(m.title) + " " + _norm(m.album)):
            s += 1.5
        return (s, text)

    return sorted(candidates, key=score, reverse=True)


async def download_bytes(url: str, timeout: float = 25.0) -> bytes:
    hit = _cached(_COVER_CACHE, url, _COVER_TTL)
    if hit is not None:
        return hit
    client = await _get_client()
    # Single attempt; cover failures are logged and the track is still
    # delivered (tagged without art) — no automatic retries.
    r = await client.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.content
    async with _CACHE_LOCK:
        _COVER_CACHE[url] = (time.monotonic(), data)
        if len(_COVER_CACHE) > 64:
            _prune(_COVER_CACHE, _COVER_TTL)
    return data


def _prune(store: dict, ttl: float) -> None:
    now = time.monotonic()
    stale = [k for k, (t, _) in store.items() if now - t >= ttl]
    for k in stale or list(store)[: len(store) // 4]:
        store.pop(k, None)


async def text_search(query: str, limit: int = 6) -> list[TrackMeta]:
    """Vector 1: user's text query -> catalog candidates, originals first.

    iTunes orders results by its own relevance, which is why karaoke /
    tribute versions regularly ended up at the top. Everything that looks
    like a re-recording sinks below the studio cuts (stable sort, so the
    original relevance order is preserved within each tier); a couple of
    extra candidates are fetched so the visible list is still `limit` long
    after karaoke entries sink.
    """
    results = await lookup_itunes(query, limit=max(limit + 2, 8))
    return _orig_first(results)[:limit]


def _orig_first(results: list[TrackMeta]) -> list[TrackMeta]:
    """Stable move of karaoke/tribute/cover candidates to the end."""
    originals = [m for m in results if not _is_karaokeish(m)]
    karaoke = [m for m in results if _is_karaokeish(m)]
    return originals + karaoke
