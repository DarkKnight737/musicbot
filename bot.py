"""Telegram music finder bot.

Text search -> candidates -> pick -> full original track via yt-dlp,
tagged with the real album cover + metadata from the iTunes catalog.
Voice/audio/video -> Shazam recognition -> same delivery path.

UX contract:
 * status/results messages persist in the chat (never auto-deleted);
   each one carries a red 🗑 Delete button;
 * picking a candidate leaves the candidate list intact — delivery runs
   on its own status message, so other options stay tappable;
 * every failure shows ❌ once and waits for the user to press the
   🔄 Retry button (calls are never retried automatically).

Speed:
 * repeat deliveries re-send by Telegram audio file_id — no bytes travel
   and the track arrives in one API round trip;
 * fresh deliveries stream the file once; same track previously sent is
   also kept on local disk (already tagged) for instant full re-uploads.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import shutil
import sys
import threading
from pathlib import Path

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

import audio_recognition
import cache as track_cache
import catalog
import config
import source_dl
import tagger
from models import TrackMeta
from utils import find_ffmpeg_dir, new_task_dir, fmt_seconds

log = logging.getLogger("bot")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

_locks: dict[int, asyncio.Lock] = {}
_choices: dict[int, list[TrackMeta]] = {}
_options: dict[int, list[list[InlineKeyboardButton]]] = {}
_retry_states: dict[int, dict] = {}

MEDIA_LABELS = {"voice": "voice message", "audio": "audio file",
                "video": "video", "video_note": "video message"}

NOMATCH_TEXT = (
    "🤔 Couldn't identify that clip.\n"
    "Try a longer/cleaner snippet, or type the artist / some lyrics as text."
)
# Telegram doesn't allow styling inline-button colors — the red dot
# emoji below is how the destructive button is marked "red".


class Status:
    """Simple editable status message (never deleted on success)."""

    def __init__(self, msg: Message):
        self.msg = msg

    async def edit(self, text: str, markup: InlineKeyboardMarkup | None = None):
        kwargs = {"parse_mode": ParseMode.HTML, "reply_markup": markup} if markup else {
            "parse_mode": ParseMode.HTML}
        try:
            await self.msg.edit_text(text, **kwargs)
        except telegram.error.BadRequest as e:
            # Re-editing identical content raises "message is not modified" —
            # benign; anything else is a real mistake.
            if "not modified" not in str(e).lower():
                raise
        except telegram.error.TelegramError as e:
            # Status text is cosmetic: a failed edit must not break delivery.
            # No automatic retries — user-driven retries happen via buttons.
            log.warning("status edit skipped: %s", e)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎵 <b>Music Finder</b>\n\n"
        "Send me any of these and I'll identify the song, then reply with the "
        "<b>original studio version</b> as an audio file — including the real album "
        "cover and complete metadata:\n\n"
        "✍️ <b>Text</b> — artist - song, or a few lyrics\n"
        "🎤 <b>Voice / audio</b> — a clip of the music\n"
        "🎬 <b>Video</b> — I'll recognize its soundtrack\n\n"
        "<i>Tip: text works best as “artist - song”.</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "1. Send text, voice, audio, or a video containing music.\n"
        "2. I identify the song (Shazam fingerprint / catalog search).\n"
        "3. I fetch the original recording, embed the album cover + tags "
        "(title, artist, album, year, genre, track number) and send it as an audio file.\n\n"
        "If a text query is ambiguous you'll get a numbered candidate list —\n"
        "those messages stay in chat so you can reuse or delete them anytime\n"
        "(🗑️ button). If something fails you get a 🔄 Retry button."
    )


def _get_lock(user_id: int) -> asyncio.Lock:
    return _locks.setdefault(user_id, asyncio.Lock())


# ---------------------------------------------------------------- keyboards
DEL = InlineKeyboardButton("🔴 🗑️ Delete This Message", callback_data="trash")
RETRY = InlineKeyboardButton("🔄 Retry", callback_data="retry")

# Keyboard for standalone status messages (fetch progress/completion).
# Delivery never mutates the results message, so the candidate list the
# user picked from remains available for further picks.
DEL_KB = InlineKeyboardMarkup([[DEL]])


def _option_rows(results: list[TrackMeta]) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for i, m in enumerate(results[:6]):
        label = f"{i + 1}. {m.title} — {m.artist}"
        if m.album:
            label += f" · {m.album}"
        if m.duration:
            label += f" · {fmt_seconds(m.duration)}"
        rows.append([InlineKeyboardButton(label[:64], callback_data=f"pick|{i}")])
    return rows


def _base_kb(uid: int) -> InlineKeyboardMarkup:
    """Current option rows (if any) + the red delete button."""
    rows = list(_options.get(uid, [])) + [[DEL]]
    return InlineKeyboardMarkup(rows)


def _fail_kb(uid: int) -> InlineKeyboardMarkup:
    """Failure state on a status message: manual 🔄 retry + delete.

    Deliberately free of option rows: option buttons live on the results
    message, which stays intact for further picks.
    """
    return InlineKeyboardMarkup([[RETRY], [DEL]])


def _store_retry(uid: int, kind: str, **data) -> None:
    _retry_states[uid] = {"kind": kind, **data}


# ---------------------------------------------------------------- text flow
async def _text_flow(update: Update, status: Status, query: str):
    """Search the catalog and either deliver the single hit or offer options.

    Kept separate from on_text so that 🔄 Retry can re-run it with a query
    restored from the retry state (callback updates carry no user text).
    """
    uid = update.effective_user.id
    try:
        results = await catalog.text_search(query)
    except Exception:
        log.exception("text search failed")
        _store_retry(uid, "text", query=query)
        await status.edit("⚠️ The catalog search failed — try again shortly.", markup=_fail_kb(uid))
        return
    if not results:
        await status.edit("Nothing found. Try “artist - song”, or send a clip of the song.")
        return

    _options[uid] = _option_rows(results)
    _choices[uid] = results
    top = results[0]
    # A precise "artist - title" query whose top hit matches exactly needs
    # no manual pick — fetch it right away (results are ordered
    # originals-first, so the top hit is the studio cut, not a karaoke).
    if len(results) == 1 or (top is not None and catalog.exact_text_match(query, top)):
        m = top
        await status.edit(
            f"⏬ Fetching <b>{html.escape(m.title)} — {html.escape(m.artist)}</b>…",
            markup=_base_kb(uid))
        await _run_delivery(update, top, status=status)
        return
    await status.edit("🎧 I found several matches — tap one:",
                      markup=_base_kb(uid))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.effective_message.text.strip()
    if len(query) < 3:
        await update.effective_message.reply_text("Give me a bit more to search on 🙂")
        return
    uid = update.effective_user.id
    status = Status(await update.effective_message.reply_text(
        f"🔎 Searching the catalog for “{html.escape(query)}”…", parse_mode=ParseMode.HTML))
    await _text_flow(update, status, query)


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q.data.startswith("pick|"):
        await q.answer("Unknown.")
        return
    try:
        idx = int(q.data.split("|")[1])
    except (IndexError, ValueError):
        await q.answer("Bad selection.")
        return
    results = _choices.get(q.from_user.id)
    if not results or idx >= len(results):
        await q.answer("Stale list — press 🔄 Retry or search again.", show_alert=True)
        return
    await q.answer("Fetching…")
    meta = results[idx]
    # Delivery runs on its own status message; the candidate list stays
    # exactly as posted so any other option can still be picked from it.
    status = Status(await update.effective_message.reply_text(
        f"⏬ Fetching <b>{html.escape(meta.title)} — {html.escape(meta.artist)}</b>…",
        parse_mode=ParseMode.HTML, reply_markup=DEL_KB))
    await _run_delivery(update, meta, status=status)


async def on_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    state = _retry_states.get(q.from_user.id)
    if not state:
        await q.answer("Nothing to retry — send the request again.", show_alert=True)
        return
    await q.answer("Retrying…")
    status = Status(update.effective_message)
    if state["kind"] == "meta":
        await _run_delivery(update, state["meta"], status=status)
    elif state["kind"] == "text":
        await _text_flow(update, status, state["query"])
    else:
        await _media_flow(update, context, status, state["descriptor"])


async def on_trash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message is None:
        await q.answer("The original message is gone.", show_alert=True)
        return
    try:
        await q.message.delete()
        await q.answer("Deleted ✅")
    except telegram.error.Forbidden:
        await q.answer("I no longer have permission to delete it.", show_alert=True)
    except telegram.error.BadRequest:
        await q.answer("Message can't be deleted (too old or missing).", show_alert=True)


# ---------------------------------------------------------------- media flow
def _media_info(message):
    m = message
    if m.voice:
        return m.voice, "oga", "voice"
    if m.audio:
        return m.audio, None, "audio"
    if m.video:
        return m.video, "mp4", "video"
    if m.video_note:
        return m.video_note, "mp4", "video_note"
    return None, None, None


def _descriptor(att, kind: str, ext: str | None) -> dict:
    """Everything needed to re-download the media later (for 🔄 Retry)."""
    return {"file_id": att.file_id, "kind": kind, "ext": ext,
            "size": att.file_size or 0, "name": getattr(att, "file_name", None)}


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    att, ext, kind = _media_info(msg)
    if att is None:
        await msg.reply_text("Send voice, audio or video please.")
        return
    status = Status(await msg.reply_text(f"📥 Received your {MEDIA_LABELS[kind]}…"))
    await _media_flow(update, context, status, _descriptor(att, kind, ext))


async def _download_media(desc: dict, context, task_dir: Path) -> str:
    size = desc["size"]
    if size > config.MAX_TELEGRAM_FILE_BYTES:
        raise ValueError(f"{(desc['kind'] or 'file').split()[0]} is too big "
                         f"({size // (1024*1024)} MB) — Telegram limits bot "
                         "downloads to ~20 MB.")
    ext, name = desc["ext"], desc["name"]
    p = task_dir / (f"clip.{ext}" if ext else (name or "audio"))
    # Single attempt — a failure here lands on ❌ + 🔄 Retry for the user
    # to press deliberately.
    tg_file = await context.bot.get_file(desc["file_id"],
                                         connect_timeout=10, read_timeout=30)
    await tg_file.download_to_drive(custom_path=str(p),
                                    read_timeout=config.DOWNLOAD_TIMEOUT)
    return str(p)


async def _media_flow(update: Update, context, status: Status, descriptor: dict):
    uid = update.effective_user.id
    async with _get_lock(uid):
        task_dir = new_task_dir()
        recognized = False
        match: TrackMeta | None = None
        try:
            source_path = await _download_media(descriptor, context, task_dir)
            await status.edit("🎧 Listening — fingerprinting the audio…",
                              markup=_base_kb(uid))
            sample = await audio_recognition.prepare_for_recognition(
                source_path, find_ffmpeg_dir(), config.SHAZAM_SEGMENT_SECONDS)
            match = await audio_recognition.recognize_file(sample)
            recognized = True
            if not match:
                _store_retry(uid, "media", descriptor=descriptor)
                await status.edit(NOMATCH_TEXT, markup=_fail_kb(uid))
                return

            await status.edit(
                f"🎯 Identified: <b>{html.escape(match.artist_title)}</b>\n"
                "⬇️ Fetching the original version…", markup=_base_kb(uid))
            await _finish_delivery(update, status, match, task_dir=task_dir,
                                   progress_markup=_base_kb(uid))
        except Exception as e:
            log.exception("media pipeline failed")
            _store_retry(uid, "meta" if recognized else "media",
                         **({"meta": match} if recognized
                            else {"descriptor": descriptor}))
            await status.edit(f"❌ Failed: {html.escape(str(e))}", markup=_fail_kb(uid))
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)


# ---------------------------------------------------------------- delivery
async def _run_delivery(update: Update, meta: TrackMeta, status: Status,
                        progress_markup: InlineKeyboardMarkup = DEL_KB):
    uid = update.effective_user.id
    async with _get_lock(uid):
        task_dir = new_task_dir()
        try:
            await _finish_delivery(update, status, meta, task_dir=task_dir,
                                   progress_markup=progress_markup)
        except Exception as e:
            log.exception("delivery failed")
            _store_retry(uid, "meta", meta=meta)
            await status.edit(f"❌ Couldn't complete delivery: {html.escape(str(e))}",
                              markup=_fail_kb(uid))
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)


async def _finish_delivery(update: Update, status: Status, meta: TrackMeta,
                           task_dir: Path | None = None,
                           progress_markup: InlineKeyboardMarkup | None = None):
    uid = update.effective_user.id
    task_dir = task_dir or new_task_dir()
    kb = progress_markup if progress_markup is not None else DEL_KB

    # ---- fast path: same track delivered before -> instant upload
    cached = track_cache.get(meta.artist, meta.title)
    if cached:
        mp3_path, thumb_path, staged = cached
        for attr in ("album", "year", "genre", "duration", "track_no",
                     "track_total", "apple_url", "artwork_url", "tg_file_id"):
            if not getattr(meta, attr):
                setattr(meta, attr, getattr(staged, attr))
        await status.edit("📤 Uploading from cache…", markup=kb)
        await _upload(update, status, meta, mp3_path, thumb_path, uid, kb=kb)
        if meta.tg_file_id and meta.tg_file_id != getattr(staged, "tg_file_id", ""):
            # file_id refreshed by a fresh upload — keep the cache current
            try:
                await asyncio.to_thread(track_cache.put, meta.artist, meta.title,
                                        mp3_path, thumb_path, meta)
            except Exception:
                log.exception("cache write failed")
        return

    # ---- enrichment (missing fields), download, cover art: all in parallel
    need_meta = not (meta.album and meta.artwork_url and meta.year and meta.genre)

    async def _enrich():
        return (await catalog.best_itunes_match(meta.artist, meta.title)) if need_meta else None

    enrich_task = asyncio.ensure_future(_enrich())

    await status.edit(f"⬇️ Downloading the original version by {html.escape(meta.artist)}…",
                      markup=kb)
    cover_task = None
    if meta.artwork_url:
        cover_task = asyncio.ensure_future(catalog.download_bytes(meta.artwork_url))
    try:
        audio_path = await source_dl.fetch_original(
            meta.title, meta.artist, meta.duration, task_dir)
        if enrich_task is not None:
            try:
                info = await enrich_task
            except Exception:
                log.exception("itunes enrichment failed")
                info = None
            if info:
                meta.album = meta.album or info.album
                meta.year = meta.year or info.year
                meta.genre = meta.genre or info.genre
                meta.duration = meta.duration or info.duration
                meta.track_no = meta.track_no or info.track_no
                meta.track_total = info.track_total
                meta.apple_url = info.apple_url
                if not meta.artwork_url and info.artwork_url and not cover_task:
                    meta.artwork_url = info.artwork_url
                    cover_task = asyncio.ensure_future(catalog.download_bytes(meta.artwork_url))
        cover_bytes = b""
        if cover_task is not None:
            try:
                cover_bytes = await cover_task
            except Exception:
                log.exception("cover download failed")
    finally:
        for t in (enrich_task, cover_task):
            if t is not None and not t.done():
                t.cancel()
    if audio_path is None:
        raise RuntimeError(
            "couldn't download the original version — YouTube download is unavailable right now")

    meta.duration = (await asyncio.to_thread(tagger.read_duration, audio_path)) or meta.duration

    # ---- tags (fast, pure-Python) — thumbnail needs no ffmpeg anymore.
    # Tag writing rewrites the whole audio file, so it runs in a worker
    # thread to keep the event loop responsive for other chats. On the m4a
    # fast path the file is only ~3-4 MB, so this is well under a second.
    await asyncio.to_thread(tagger.embed_all, audio_path, meta, cover_bytes or None)

    thumb_path: Path | None = None
    if cover_bytes:
        cover_path = task_dir / "cover.jpg"
        cover_path.write_bytes(cover_bytes)
        thumb_path = task_dir / "thumb.jpg"
        try:
            await asyncio.to_thread(tagger.make_thumbnail, cover_path, thumb_path)
        except Exception:
            log.exception("thumbnail failed")
            thumb_path = None

    await _upload(update, status, meta, Path(audio_path), thumb_path, uid, kb=kb)

    # ---- stash for repeat requests (instant re-delivery later)
    try:
        await asyncio.to_thread(track_cache.put, meta.artist, meta.title, audio_path,
                                thumb_path, meta)
    except Exception:
        log.exception("cache write failed")


async def _upload(update: Update, status: Status, meta: TrackMeta,
                  audio_path: Path, thumb_path: Path | None, uid: int,
                  kb: InlineKeyboardMarkup = DEL_KB):
    file_name = (f"{meta.artist} - {meta.title}{audio_path.suffix.lower() or '.m4a'}"
                 .replace("/", "_").replace("\\", "_"))
    caption = (
        f"🎵 <b>{html.escape(meta.title)}</b>\n"
        f"🎤 {html.escape(meta.artist)}\n"
        f"💿 {html.escape(meta.album or '—')}"
        + (f" ({html.escape(meta.year)})" if meta.year else "")
        + (f"  ·  {html.escape(meta.genre)}" if meta.genre else "")
        + f"\n⏱ {fmt_seconds(meta.duration)}"
        + (f"\n🍎 <a href=\"{html.escape(meta.apple_url, quote=True)}\">Apple Music</a>"
           if meta.apple_url else "")
        + "\n✅ Original studio version · cover embedded"
    )
    success = f"✅ <b>{html.escape(meta.title)} — {html.escape(meta.artist)}</b> delivered."

    # ---- instant path: Telegram already holds these exact bytes, so
    # re-sending by file_id moves no data across the wire.
    if meta.tg_file_id:
        try:
            _ = await update.effective_message.reply_audio(
                audio=meta.tg_file_id,
                title=meta.title, performer=meta.artist, duration=meta.duration,
                caption=caption, parse_mode=ParseMode.HTML,
                connect_timeout=10, read_timeout=30)
            await status.edit(success, markup=kb)
            return
        except telegram.error.BadRequest as e:
            log.warning("cached file_id rejected (%s) — re-uploading the local copy", e)
        # fall through to the regular byte upload; the fresh file_id below
        # replaces the stale one in the cache afterwards.

    await status.edit("📨 Uploading to chat…", markup=kb)
    thumb = thumb_path.open("rb") if thumb_path and Path(thumb_path).exists() else None
    audio_handle = audio_path.open("rb")
    try:
        # Single attempt — no automatic retries. Any failure lands as
        # ❌ + 🔄 Retry, which the user presses deliberately.
        sent = await update.effective_message.reply_audio(
            audio=audio_handle, thumbnail=thumb, title=meta.title,
            performer=meta.artist, duration=meta.duration,
            caption=caption, parse_mode=ParseMode.HTML,
            filename=file_name,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=config.UPLOAD_TIMEOUT,
        )
        # Remember the upload so future deliveries skip the byte transfer.
        if sent is not None and sent.audio is not None:
            meta.tg_file_id = sent.audio.file_id
        await status.edit(success, markup=kb)
    finally:
        audio_handle.close()
        if thumb:
            thumb.close()


# ---------------------------------------------------------------- main
# ---------------------------------------------------------------- main
def _fake_watcher(port: int):
    """Minimal HTTP responder for HF Spaces port probing.

    HF Spaces routes its health check to the container's exposed port.
    This bot is a Telegram poller with no real web presence, so this
    server answers with 200 to any request and keeps the platform from
    declaring the container dead. Runs only when HF_PORT is set.
    """
    import socketserver
    from http.server import BaseHTTPRequestHandler

    class _Resp(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"music bot is running"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence the request log
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", port), _Resp) as srv:
        srv.serve_forever()


def main():
    if not config.BOT_TOKEN:
        print("Set BOT_TOKEN in .env first.")
        sys.exit(1)

    # HF Spaces (or any container host) probes this port for liveness.
    if os.environ.get("HF_PORT"):
        threading.Thread(target=_fake_watcher, args=(int(os.environ["HF_PORT"]),),
                         daemon=True).start()
        log.info("health endpoint listening on port %s", os.environ["HF_PORT"])

    if not find_ffmpeg_dir():
        log.warning("FFmpeg not found — install it or add to PATH; recognition will fail "
                    "(text search + delivery still work).")

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        # PTB's defaults (5 s reads, 1 s pool wait) are too tight for this bot.
        # These are plain API calls: fast-fail so a retry can kick in quickly.
        .connect_timeout(10)
        .read_timeout(15)
        .write_timeout(30)
        .pool_timeout(10)
        # CRITICAL: requests that carry files (our 5-15 MB track uploads) use
        # media_write_timeout — NOT the write_timeout above (whose 300 s was
        # the silent cause of upload TimedOuts). Applies to reply_audio etc.
        .media_write_timeout(config.UPLOAD_TIMEOUT)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pick\|\d+$"))
    app.add_handler(CallbackQueryHandler(on_retry, pattern=r"^retry$"))
    app.add_handler(CallbackQueryHandler(on_trash, pattern=r"^trash$"))
    app.add_handler(MessageHandler(
        filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot starting (polling)…")
    # Queued updates are delivered on reconnect: while the fresh install
    # sleeps or hands off between restarts, messages from users during the
    # pause are not lost — Telegram holds them until we log in again.
    app.run_polling(allowed_updates=["message", "callback_query"],
                    drop_pending_updates=False)


if __name__ == "__main__":
    main()
