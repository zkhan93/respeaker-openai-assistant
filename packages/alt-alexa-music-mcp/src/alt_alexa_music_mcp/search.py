"""Query normalization + fuzzy scoring against Navidrome candidates.

We deliberately don't pre-index the whole library. Navidrome's `search3`
is already decent and cheap; we just need to (a) tame messy candidates
into a clean comparable string and (b) score each candidate against the
user's spoken query so we can decide between "stream from library" and
"fall back to YouTube".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from .navidrome import Song

# Strip leading track numbers, bracketed segments, common quality tags,
# trailing parenthesized notes. Tuned for Indian-music filename conventions
# but harmless on Western libraries.
_NOISE_PATTERNS = [
    re.compile(r"^\s*\d+[\.\-\s]+"),
    re.compile(r"\[[^\]]*\]"),
    re.compile(r"\([^)]*\)"),
    re.compile(r"\b(?:official|video|audio|hd|hq|lyrics?|full|song)\b", re.I),
    re.compile(r"\b(?:mp3|m4a|flac|opus|ogg)\b", re.I),
    re.compile(r"[_\-]+"),
]


def normalize(text: str) -> str:
    """Lowercase, strip diacritics, drop common noise, collapse whitespace."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = ascii_text.lower()
    for pattern in _NOISE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


@dataclass(frozen=True)
class ScoredSong:
    song: Song
    score: float

    @property
    def display(self) -> str:
        if self.song.artist:
            return f"{self.song.title} — {self.song.artist}"
        return self.song.title


def _song_haystack(song: Song) -> str:
    parts = [song.title or "", song.artist or "", song.album or ""]
    return normalize(" ".join(p for p in parts if p))


def score_candidates(query: str, songs: list[Song]) -> list[ScoredSong]:
    """Return candidates sorted by descending fuzzy score against `query`."""
    needle = normalize(query)
    if not needle or not songs:
        return []
    scored = [ScoredSong(song=s, score=fuzz.WRatio(needle, _song_haystack(s))) for s in songs]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def best_match(query: str, songs: list[Song], threshold: int) -> ScoredSong | None:
    """Return the top candidate iff it clears the threshold, else None."""
    scored = score_candidates(query, songs)
    if not scored:
        return None
    top = scored[0]
    return top if top.score >= threshold else None
