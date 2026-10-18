# Music Finder Telegram Bot 🎵

Identifies songs from **text**, **voice/audio clips**, or **video**, then delivers the
**original studio version** as an **m4a** audio file with the **real album cover** and
**complete metadata** (title, artist, album, year, genre, track number) embedded.

## How it works
| Mode | Pipeline |
|---|---|
| Text | iTunes Search catalog → candidate list → yt-dlp → tag → send |
| Voice / audio / video | ffmpeg prep → Shazam fingerprint (keyless) → iTunes metadata → yt-dlp → tag → send |

- Recognition: `shazamio` (no API key needed)
- Metadata & cover: iTunes Search API (1000×1000 cover)
- **Originals-first ordering**: karaoke / tribute / "made famous by" / "in the style of" releases are pushed below the studio recordings in every text search, and a text query like "artist - title" fetches immediately when the top hit matches it exactly (no manual pick step)
- Download: `yt-dlp` (YouTube) with fast flat search and cover/remix/live filtering by duration + channel + title scoring (karaoke-style uploaders are penalized by channel name too). Delivers the **native AAC stream (~128 kbps)** as m4a — no re-encode, so a fresh delivery normally completes well inside 20 s
- Tagging: `mutagen` (MP4 atoms for m4a, ID3v2.3 + embedded APIC cover for mp3) + 320×320 chat thumbnail (Pillow — ffmpeg not needed for this path)

## Reliability & timeouts
- **No automatic retries**: an operation failing once surfaces ❌ with a 🔄 Retry button; the user decides whether to retry.
- Uploads: `media_write_timeout=120 s` (PTB ignores `write_timeout` for file uploads), per-call connect/read caps so a stalled call fails fast.
- **Repeat deliveries are near-instant**: the audio `file_id` from the first send is remembered (local track cache) and re-sent by Telegram itself — no bytes travel.
- Hard deadlines (in `config.py`): Shazam recognition 45 s, yt-dlp flat search 12 s, yt-dlp download 120 s per candidate (stall safety net only — the native m4a download normally takes 3-8 s), Telegram media download 90 s. Tight on purpose — a stalled attempt fails within seconds/minutes instead of holding the user for 5+ min, and surfaces the 🔄 Retry button.
- iTunes Search: 12 s per call; failures surface the manual retry button.
- Status edits are cosmetic — they never fail a delivery.

## UX notes
- Candidate lists persist after a pick: delivery runs on its own status message (delete button), and the list below stays fully tappable.

## Run
- Foreground: `start.bat` (or `python bot.py`)
- 24/7: `deploy/HF_SPACES.md` (card-free, email-only) · `deploy/DEPLOY.md` (VPS + systemd)
- Token lives in `.env` (`BOT_TOKEN=`)
- Stop: Ctrl+C / close the window. Restart: run `start.bat` again.

Token lives in `.env` (`BOT_TOKEN=`)
- Stop: Ctrl+C / close the window. Restart: run `start.bat` again.

## Files
`bot.py` (Telegram handlers & delivery) · `audio_recognition.py` (Shazam) ·
`catalog.py` (iTunes metadata/cover) · `source_dl.py` (yt-dlp fetch) ·
`tagger.py` (ID3 embedding) · `utils.py` · `config.py` · `.env`

> Note: `HF_PORT` is set only in the hosted Dockerfile; it starts a small
> liveness responder so container hosts (HF Spaces) keep the Space awake.
> Fetching recordings via yt-dlp is for **personal use**; support artists
> via the Apple Music link included in every caption.
