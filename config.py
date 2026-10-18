"""Bot configuration loaded from .env"""
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Max size (bytes) Telegram bots can download via getFile: 20 MB (margin)
MAX_TELEGRAM_FILE_BYTES = 19 * 1024 * 1024

# Recognition/audio processing
SHAZAM_SEGMENT_SECONDS = 12      # audio length sampled from each clip
CLIP_KEEP_SECONDS = 45           # trim long uploads before recognition
YOUTUBE_CANDIDATES = 6           # yt-dlp flat search depth (fast)

ROOT = Path(__file__).resolve().parent

# Hard deadlines so a stalled network call can never wedge a user's flow.
# Tight caps are deliberate: a mid-list search normally answers in 3-5 s and
# an audio download in well under a minute; letting a stalled attempt hold
# the user hostage is far worse than surfacing a ❌ + 🔄 Retry quickly.
# Note: for requests carrying files, python-telegram-bot applies
# media_write_timeout (NOT write_timeout) — see bot.Application setup.
UPLOAD_TIMEOUT = 120             # one full-track upload to Telegram
DOWNLOAD_TIMEOUT = 90            # Telegram media download (getFile), ≤19 MB
SHAZAM_TIMEOUT = 45              # fingerprint round-trip (normally 3-8 s)
YOUTUBE_SEARCH_TIMEOUT = 12     # yt-dlp flat search deadline (normally 2-4 s)
YOUTUBE_DOWNLOAD_TIMEOUT = 120  # per-candidate stall safety net only — the
                                 # native m4a download normally takes 3-8 s

# Local disk cache of already fetched+tagged tracks (repeat deliveries are instant)
CACHE_DIR = Path(tempfile.gettempdir()) / "music_bot_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_MAX_BYTES = 400 * 1024 * 1024
