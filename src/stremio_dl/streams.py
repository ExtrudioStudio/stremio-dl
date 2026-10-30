"""Fetch streams from the stream addons, normalize them into :class:`Candidate`, filter and rank.

Torrentio stream example (abridged)::

    name:  "Torrentio\\n1080p"                     (line 2 is usually, but not always, a resolution)
    title: "Friends S01 1080p BluRay x265-GROUP\\n"  (release name)
           "Friends.S01E10.1080p.mkv\\n"            (file inside a pack; absent for single files)
           "👤 21 💾 6.55 GB ⚙️ Rutracker\\n"        (seeders, size, source site)
           "🇬🇧 / 🇷🇺"                              (audio languages; absent for English-only)
    infoHash, fileIdx, behaviorHints.filename, sources ["tracker:udp://…", "dht:<hash>"]
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

from . import quality as q
from .addons.client import Addon, AddonClient
from .config import Config
from .models import Candidate, EpisodeRef, Key, Title

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm", ".mov", ".wmv", ".mpg", ".mpeg"}
BLOCKED_EXTS = {
    ".exe", ".lnk", ".scr", ".bat", ".cmd", ".com", ".msi", ".pif", ".vbs", ".js", ".ps1",
    ".jar", ".apk", ".zip", ".rar", ".7z", ".iso", ".url", ".hta",
}  # fmt: skip

_SEEDERS = re.compile(r"👤\s*(\d+)")
_SIZE = re.compile(r"💾\s*([\d.,]+)\s*([KMGT]?B)", re.I)
_SOURCE = re.compile(r"⚙️?\s*(.+?)\s*$")
_FLAG = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
_LABEL_RES = [
    (re.compile(r"\b(4k|2160p|uhd)\b", re.I), "2160p"),
    (re.compile(r"\b1440p\b", re.I), "1440p"),
    (re.compile(r"\b1080[pi]\b", re.I), "1080p"),
    (re.compile(r"\b720p\b", re.I), "720p"),
    (re.compile(r"\b(480p|576p|sd)\b", re.I), "480p"),
    (re.compile(r"\b360p\b", re.I), "360p"),
]
_CAM = re.compile(
    r"(?:^|[\s.\-_\[(])(cam|camrip|hdcam|hd-?ts|telesync|ts-?rip|tc|telecine|scr|screener|"
    r"dvdscr|bdscr|hdtc)(?:$|[\s.\-_\])])",
    re.I,
)
_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

# Country flag → audio language. Unlisted flags fall back to the lowercase country code.
_COUNTRY_LANG = {
    "GB": "en", "US": "en", "AU": "en", "CA": "en", "IE": "en", "NZ": "en",
    "RU": "ru", "UA": "uk", "FR": "fr", "IT": "it", "ES": "es", "MX": "es", "AR": "es",
    "DE": "de", "AT": "de", "PT": "pt", "BR": "pt", "NL": "nl", "PL": "pl", "CZ": "cs",
    "SK": "sk", "HU": "hu", "RO": "ro", "BG": "bg", "GR": "el", "TR": "tr", "IL": "he",
    "SA": "ar", "AE": "ar", "EG": "ar", "IR": "fa", "IN": "hi", "JP": "ja", "KR": "ko",
    "CN": "zh", "TW": "zh", "HK": "zh", "TH": "th", "VN": "vi", "ID": "id", "MY": "ms",
    "PH": "tl", "SE": "sv", "NO": "no", "DK": "da", "FI": "fi", "EE": "et", "LV": "lv",
    "LT": "lt", "RS": "sr", "HR": "hr", "SI": "sl", "LA": "es",
}  # fmt: skip


def _flag_to_country(flag: str) -> str:
    return "".join(chr(ord(ch) - 0x1F1E6 + ord("A")) for ch in flag)


def parse_languages(text: str) -> tuple[str, ...]:
    langs: list[str] = []
    for flag in _FLAG.findall(text):
        country = _flag_to_country(flag)
        lang = _COUNTRY_LANG.get(country, country.lower())
        if lang not in langs:
            langs.append(lang)
    if "🌐" in text and "multi" not in langs:
        langs.append("multi")
    return tuple(langs)


def parse_size(text: str) -> int:
    m = _SIZE.search(text)
    if not m:
        return 0
    number = float(m.group(1).replace(",", ""))
    return int(number * _UNITS.get(m.group(2).upper(), 1))


@lru_cache(maxsize=4096)
def _guess_resolution(name: str) -> str | None:
    if not name:
        return None
    try:
        from guessit import guessit  # imported lazily: it's slow to import

        size = guessit(name).get("screen_size")
    except Exception:  # guessit can raise on odd input; treat it as "no information"
        return None
    if not isinstance(size, str):
        return None
    for pattern, res in _LABEL_RES:
        if pattern.search(size):
            return res
    return None


def detect_resolution(label: str, filename: str, release: str) -> str:
    for pattern, res in _LABEL_RES:
        if pattern.search(label):
            return res
    return _guess_resolution(filename) or _guess_resolution(release) or q.UNKNOWN


def normalize_stream(stream: dict[str, Any], addon: str = "") -> Candidate | None:
    info_hash = (stream.get("infoHash") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", info_hash):
        return None  # HTTP/debrid or malformed streams aren't handled by the torrent engine
    name = stream.get("name") or ""
    label = name.split("\n", 1)[1].strip() if "\n" in name else ""
    title = stream.get("title") or stream.get("description") or ""
    lines = [line.strip() for line in title.split("\n") if line.strip()]

    stats_idx = next((i for i, line in enumerate(lines) if "👤" in line or "💾" in line), None)
    head = lines[:stats_idx] if stats_idx is not None else lines
    tail = lines[stats_idx + 1 :] if stats_idx is not None else []
    stats = lines[stats_idx] if stats_idx is not None else ""

    release = head[0] if head else ""
    inner_path = head[1] if len(head) > 1 else ""
    hints = stream.get("behaviorHints") or {}
    filename = hints.get("filename") or PurePosixPath(inner_path).name or release

    seeders_m = _SEEDERS.search(stats)
    source_m = _SOURCE.search(stats)
    raw_idx = stream.get("fileIdx")
    file_idx = int(raw_idx) if str(raw_idx).isdigit() else None
    hdr = tuple(t for t in re.split(r"[\s|]+", label) if t.upper() in {"DV", "HDR", "HDR10+"})

    return Candidate(
        info_hash=info_hash,
        file_idx=file_idx,
        resolution=detect_resolution(label, filename, release),
        seeders=int(seeders_m.group(1)) if seeders_m else 0,
        size=parse_size(stats),
        source=source_m.group(1) if source_m else "",
        languages=parse_languages(" ".join(tail)),
        filename=filename,
        release=release,
        is_pack=bool(inner_path),
        hdr=hdr,
        trackers=tuple(
            s.split(":", 1)[1] for s in stream.get("sources") or [] if s.startswith("tracker:")
        ),
        label=label,
        addon=addon,
    )


def rejection_reason(c: Candidate, cfg: Config) -> str | None:
    """Why a candidate is unusable regardless of resolution, or None if it's fine."""
    ext = PurePosixPath(c.filename.lower()).suffix
    if ext in BLOCKED_EXTS:
        return f"not a video file ({ext})"
    if c.filename and c.filename != c.release and ext not in VIDEO_EXTS:
        return f"not a video file ({ext or 'no extension'})"
    stem = c.release + " " + PurePosixPath(c.filename).stem
    if _CAM.search(stem) or _CAM.search(c.label):
        return "cam/screener release"
    wanted = {lang.lower() for lang in cfg.languages}
    if c.languages and "any" not in wanted and "multi" not in c.languages:
        if not wanted.intersection(c.languages):
            return "audio language " + "/".join(c.languages)
    if cfg.exclude_dv_only and "DV" in c.hdr and "HDR" not in c.hdr:
        return "Dolby Vision only"
    return None


def usable(cands: list[Candidate], cfg: Config) -> list[Candidate]:
    return [c for c in cands if rejection_reason(c, cfg) is None]


def rank(cands: list[Candidate], requested: str) -> list[Candidate]:
    """Filter to the requested resolution; most seeders first (size breaks ties)."""
    pool = [c for c in cands if q.matches(requested, c.resolution)]
    if requested == q.BEST:
        pool = [c for c in pool if c.seeders > 0] or pool
        return sorted(pool, key=lambda c: (-q.rank(c.resolution), -c.seeders, -c.size))
    return sorted(pool, key=lambda c: (-c.seeders, -c.size))


def merge(cands: list[Candidate]) -> list[Candidate]:
    """De-duplicate offers of the same file from several addons, keeping the best-seeded one."""
    best: dict[tuple[str, int | None], Candidate] = {}
    for c in cands:
        key = (c.info_hash, c.file_idx)
        if key not in best or c.seeders > best[key].seeders:
            best[key] = c
    return list(best.values())


class StreamResolver:
    def __init__(self, client: AddonClient, addons: list[Addon], cfg: Config):
        self.client = client
        self.cfg = cfg
        self.addons = addons

    def _addons_for(self, title: Title, stream_id: str) -> list[Addon]:
        return [a for a in self.addons if a.supports("stream", title.type, stream_id)]

    async def candidates(self, title: Title, ref: EpisodeRef | None) -> list[Candidate]:
        """All usable candidates for one episode (or the movie), any resolution."""
        stream_id = title.stream_id(ref)
        addons = self._addons_for(title, stream_id)
        results = await asyncio.gather(
            *(self.client.streams(a, title.type, stream_id) for a in addons),
            return_exceptions=True,
        )
        found: list[Candidate] = []
        for addon, streams in zip(addons, results, strict=True):
            if isinstance(streams, Exception):
                log.warning("Streams failed on %s for %s: %s", addon.name, stream_id, streams)
                continue
            for stream in streams:
                cand = normalize_stream(stream, addon.name)
                if cand is not None:
                    found.append(cand)
        return usable(merge(found), self.cfg)

    async def all_candidates(
        self, title: Title, keys: list[Key], on_progress: Any = None
    ) -> dict[Key, list[Candidate]]:
        out: dict[Key, list[Candidate]] = {}

        async def one(key: Key) -> None:
            out[key] = await self.candidates(title, key)
            if on_progress:
                on_progress(key)

        await asyncio.gather(*(one(k) for k in keys))
        return {k: out[k] for k in keys}

    async def resolution_matrix(self, title: Title, max_seasons: int = 40) -> dict[int, Counter]:
        """Release counts per resolution, sampled from episode 1 of each season."""
        firsts = [eps[0].ref for _, eps in sorted(title.seasons().items())][:max_seasons]
        cands = await self.all_candidates(title, list(firsts))
        return {ref.season: Counter(c.resolution for c in cands[ref]) for ref in firsts}


def choose(
    cands: list[Candidate], requested: str, on_missing: str, limit: int
) -> tuple[list[Candidate], str | None]:
    """Ranked choices for one episode and the resolution actually used.

    With ``on_missing="lower"``, falls back to the next lower resolution that has streams.
    Returns ``([], None)`` when nothing fits.
    """
    ranked = rank(cands, requested)
    if ranked:
        return ranked[:limit], requested
    if on_missing == "lower":
        for res in q.lower_than(requested):
            ranked = rank(cands, res)
            if ranked:
                return ranked[:limit], res
    return [], None
