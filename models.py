"""Shared data structures."""
from dataclasses import dataclass, field


@dataclass
class TrackMeta:
    """Canonical song info used for search, tagging and delivery."""
    title: str
    artist: str
    album: str = ""
    year: str = ""
    genre: str = ""
    duration: int = 0            # seconds, if known
    artwork_url: str = ""        # highest-res cover available
    track_no: int = 0
    track_total: int = 0
    source: str = ""             # where recognition happened: shazam/itunes
    apple_url: str = ""
    tg_file_id: str = ""         # Telegram audio file_id from a prior upload (instant re-delivery)

    extra: dict = field(default_factory=dict)

    @property
    def artist_title(self) -> str:
        return f"{self.artist} - {self.title}"
