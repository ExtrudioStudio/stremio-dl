"""Resolution names, aliases and ordering."""

from __future__ import annotations

import re

BEST = "best"
UNKNOWN = "unknown"
# Highest first. "unknown" (e.g. a DVDRip with no resolution in its name) ranks as SD.
RESOLUTIONS = ["2160p", "1440p", "1080p", "720p", "480p", "360p", UNKNOWN]

_ALIASES = {
    "2160p": {"4k", "uhd", "2160", "2160p", "4kuhd", "ultrahd"},
    "1440p": {"2k", "qhd", "1440", "1440p"},
    "1080p": {"fullhd", "fhd", "1080", "1080p", "1080i"},
    "720p": {"hd", "720", "720p", "hdready"},
    "480p": {"sd", "480", "480p", "576", "576p", "dvd"},
    "360p": {"360", "360p"},
    BEST: {"best", "max", "highest", "any"},
}
_LOOKUP = {alias: canon for canon, aliases in _ALIASES.items() for alias in aliases}

LABELS = {"2160p": "4K", "1440p": "1440p", "1080p": "1080p", "720p": "720p", "480p": "SD"}


def parse_quality(text: str) -> str:
    """Map user text such as "full hd", "4K" or "720" to a canonical resolution or "best"."""
    key = re.sub(r"[\s_\-]+", "", text.strip().lower())
    if key in _LOOKUP:
        return _LOOKUP[key]
    known = "4k, 1440p, 1080p/full hd, 720p/hd, 480p/sd, best"
    raise ValueError(f"Unknown quality {text.strip()!r} (try: {known})")


def rank(resolution: str) -> int:
    """Higher is better; unknown resolutions sort below everything else."""
    try:
        return len(RESOLUTIONS) - RESOLUTIONS.index(resolution)
    except ValueError:
        return 0


def matches(requested: str, resolution: str) -> bool:
    if requested == BEST:
        return True
    if requested == "480p":
        return resolution in {"480p", UNKNOWN}
    return resolution == requested


def lower_than(requested: str) -> list[str]:
    """Resolutions to fall back to, best first, when ``requested`` isn't available."""
    if requested not in RESOLUTIONS:
        return []
    idx = RESOLUTIONS.index(requested)
    return [r for r in RESOLUTIONS[idx + 1 :] if r != UNKNOWN]
