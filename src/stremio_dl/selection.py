"""Parser for episode selections such as ``s1 eps10 - s2 eps 3 /full hd``.

Grammar, after normalization (see :func:`normalize`)::

    selection := spec [ ("/" | "@") quality ]
    spec      := term ("," term)*
    term      := point | point "-" point
    point     := "all" | "s" N | "s" N "e" M | "e" M | N

Ranges follow the show's aired order, so ``s1e10-s2e3`` crosses the season boundary.
A bare ``N`` on the right of a range inherits the kind of the left side:
``s1-3`` is seasons 1 to 3, ``s2e1-5`` is episodes 1 to 5 of season 2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import EpisodeRef, Title
from .quality import parse_quality


class SelectionError(ValueError):
    def __init__(self, message: str, token: str | None = None):
        super().__init__(message)
        self.token = token


@dataclass
class Selection:
    refs: list[EpisodeRef]
    quality: str | None  # canonical resolution or "best"; None when not given


_DASHES = re.compile(r"[‐-―−~]")
_TRAILING_QUALITY = re.compile(
    r"(?:^|\s)(4k|uhd|2160p|1440p|2k|qhd|full\s*hd|fhd|1080[pi]|720p|hd|sd|480p|best)\s*$"
)
_POINT = re.compile(r"^(?:s(\d+))?(?:e(\d+))?$")


def normalize(text: str) -> str:
    t = text.lower().strip()
    t = _DASHES.sub("-", t)
    t = re.sub(r"\b(?:to|through|thru)\b|\.\.", "-", t)
    t = re.sub(r"\band\b|[;&+]", ",", t)
    t = re.sub(r"(\d+)\s*x\s*(\d+)", r"s\1e\2", t)
    t = re.sub(r"seasons?(?=\s*\d)", "s", t)
    t = re.sub(r"(?:episodes?|eps|ep)(?=\s*\d)", "e", t)
    t = re.sub(r"\b([se])\s+(?=\d)", r"\1", t)
    t = re.sub(r"(s\d+)\s*(e\d+)", r"\1\2", t)
    t = re.sub(r"\s*-\s*", "-", t)
    t = re.sub(r"\s*,\s*", ",", t)
    t = re.sub(r"\s+", ",", t)  # "s1 s3" lists two terms
    return t.strip(",")


def split_spec(text: str) -> tuple[str, str | None]:
    """Split user input into (episode spec, quality text or None)."""
    for sep in ("/", "@"):
        if sep in text:
            spec, _, quality = text.partition(sep)
            return spec.strip(), quality.strip() or None
    match = _TRAILING_QUALITY.search(text.lower())
    if match:
        return text[: match.start()].strip(), match.group(1)
    return text.strip(), None


@dataclass
class _Point:
    raw: str
    season: int | None = None
    episode: int | None = None
    bare: int | None = None
    all: bool = False


def _parse_point(raw: str) -> _Point:
    if raw == "all":
        return _Point(raw, all=True)
    if raw.isdigit():
        return _Point(raw, bare=int(raw))
    m = _POINT.match(raw)
    if not m or (m.group(1) is None and m.group(2) is None):
        raise SelectionError(
            f"Can't understand {raw!r}. Use forms like s1, s1e5, s1e10-s2e3 or all.", raw
        )
    season = int(m.group(1)) if m.group(1) else None
    episode = int(m.group(2)) if m.group(2) else None
    return _Point(raw, season, episode)


class _Expander:
    def __init__(self, title: Title):
        self.title = title
        self.seasons = title.seasons()
        self.order = {ep.ref: i for i, ep in enumerate(title.episodes)}
        self.current_season: int | None = None
        self.last_kind = "season"

    def _season_list(self) -> str:
        nums = sorted(self.seasons)
        if not nums:
            return "no aired episodes"
        if nums == list(range(nums[0], nums[-1] + 1)):
            return f"seasons {nums[0]}–{nums[-1]}" if len(nums) > 1 else f"season {nums[0]}"
        return "seasons " + ", ".join(map(str, nums))

    def _check_season(self, season: int, token: str) -> list[int]:
        if season not in self.seasons:
            raise SelectionError(
                f"Season {season} doesn't exist: {self.title.name} has {self._season_list()}.",
                token,
            )
        return [self.order[ep.ref] for ep in self.seasons[season]]

    def _check_episode(self, ref: EpisodeRef, token: str) -> int:
        self._check_season(ref.season, token)
        if ref not in self.order:
            count = len(self.seasons[ref.season])
            raise SelectionError(
                f"{ref.code} doesn't exist: season {ref.season} has {count} aired episodes.",
                token,
            )
        return self.order[ref]

    def _resolve(self, p: _Point, inherit: str | None = None) -> tuple[str, int, int]:
        """Return (kind, first index, last index) covered by a point."""
        if p.bare is not None:
            kind = inherit or self.last_kind
            if kind == "season":
                p = _Point(p.raw, season=p.bare)
            else:
                p = _Point(p.raw, episode=p.bare)
        if p.episode is not None and p.season is None:
            if self.current_season is None:
                raise SelectionError(f"{p.raw!r} needs a season, e.g. s1{p.raw}.", p.raw)
            p = _Point(p.raw, season=self.current_season, episode=p.episode)
        assert p.season is not None
        self.current_season = p.season
        if p.episode is None:
            idx = self._check_season(p.season, p.raw)
            self.last_kind = "season"
            return "season", idx[0], idx[-1]
        idx = self._check_episode(EpisodeRef(p.season, p.episode), p.raw)
        self.last_kind = "episode"
        return "episode", idx, idx

    def term(self, raw: str) -> list[int]:
        parts = raw.split("-")
        if len(parts) > 2 or any(not part for part in parts):
            raise SelectionError(f"Can't understand the range {raw!r}.", raw)
        points = [_parse_point(part) for part in parts]
        if any(p.all for p in points):
            if len(points) > 1:
                raise SelectionError("'all' can't be part of a range.", raw)
            return list(range(len(self.title.episodes)))
        if len(points) == 1:
            _, first, last = self._resolve(points[0])
            return list(range(first, last + 1))
        kind, first, _ = self._resolve(points[0])
        _, _, last = self._resolve(points[1], inherit=kind)
        if last < first:
            raise SelectionError(f"The range {raw!r} runs backwards.", raw)
        return list(range(first, last + 1))


def parse_selection(text: str, title: Title, default_quality: str | None = None) -> Selection:
    spec_text, quality_text = split_spec(text)
    quality = parse_quality(quality_text) if quality_text else default_quality

    if title.type == "movie":
        if spec_text and not quality_text:
            quality = parse_quality(spec_text)  # for movies the whole input is the quality
        return Selection([], quality)

    spec = normalize(spec_text)
    if not spec:
        raise SelectionError("Say which episodes to download, e.g. s1, s1e5 or s1e10-s2e3.")
    expander = _Expander(title)
    picked: set[int] = set()
    for term in spec.split(","):
        picked.update(expander.term(term))
    refs = [title.episodes[i].ref for i in sorted(picked)]
    return Selection(refs, quality)


def summarize(refs: list[EpisodeRef]) -> str:
    """Compact description: ``S01E10–S01E24, S02E01–S02E03``."""
    if not refs:
        return ""
    groups: list[list[EpisodeRef]] = []
    for ref in sorted(refs):
        last = groups[-1][-1] if groups else None
        if last and last.season == ref.season and ref.episode == last.episode + 1:
            groups[-1].append(ref)
        else:
            groups.append([ref])
    parts = [g[0].code if len(g) == 1 else f"{g[0].code}–{g[-1].code}" for g in groups]
    return ", ".join(parts)
