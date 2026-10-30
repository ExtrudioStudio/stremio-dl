"""Core data types shared by the search, selection, planning and download layers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

MediaType = Literal["movie", "series"]


@dataclass(frozen=True, order=True)
class EpisodeRef:
    season: int
    episode: int

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class Episode:
    season: int
    episode: int
    title: str = ""
    released: datetime | None = None

    @property
    def ref(self) -> EpisodeRef:
        return EpisodeRef(self.season, self.episode)


@dataclass
class Title:
    """A movie or a series, with the aired episodes (in order) for series."""

    id: str
    type: MediaType
    name: str
    year: int | None = None
    release_info: str = ""
    episodes: list[Episode] = field(default_factory=list)

    def seasons(self) -> dict[int, list[Episode]]:
        out: dict[int, list[Episode]] = {}
        for ep in self.episodes:
            out.setdefault(ep.season, []).append(ep)
        return out

    def episode(self, ref: EpisodeRef) -> Episode | None:
        for ep in self.episodes:
            if ep.season == ref.season and ep.episode == ref.episode:
                return ep
        return None

    def stream_id(self, ref: EpisodeRef | None) -> str:
        if self.type == "movie" or ref is None:
            return self.id
        return f"{self.id}:{ref.season}:{ref.episode}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "year": self.year,
            "release_info": self.release_info,
            "episodes": [[e.season, e.episode, e.title] for e in self.episodes],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Title:
        return cls(
            id=d["id"],
            type=d["type"],
            name=d["name"],
            year=d.get("year"),
            release_info=d.get("release_info", ""),
            episodes=[Episode(s, e, t) for s, e, t in d.get("episodes", [])],
        )


@dataclass(frozen=True)
class SearchResult:
    id: str
    type: MediaType
    name: str
    release_info: str = ""
    addon: str = ""

    @property
    def label(self) -> str:
        year = f" ({self.release_info})" if self.release_info else ""
        kind = "TV" if self.type == "series" else "Movie"
        return f"{self.name}{year} · {kind}"


@dataclass(frozen=True)
class Candidate:
    """One normalized stream offer from a stream addon."""

    info_hash: str
    file_idx: int | None
    resolution: str  # "2160p" | "1440p" | "1080p" | "720p" | "480p" | "360p" | "unknown"
    seeders: int
    size: int  # bytes, 0 when unknown
    source: str = ""
    languages: tuple[str, ...] = ()
    filename: str = ""
    release: str = ""
    is_pack: bool = False
    hdr: tuple[str, ...] = ()
    trackers: tuple[str, ...] = ()
    label: str = ""  # second line of the addon's "name", e.g. "1080p" or "4k DV | HDR"
    addon: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Candidate:
        data = dict(d)
        for key in ("languages", "hdr", "trackers"):
            data[key] = tuple(data.get(key, ()))
        return cls(**data)


Key = EpisodeRef | None  # None is the key for a movie


def key_to_json(key: Key) -> list[int] | None:
    return None if key is None else [key.season, key.episode]


def key_from_json(value: list[int] | None) -> Key:
    return None if value is None else EpisodeRef(value[0], value[1])


@dataclass
class FileTarget:
    """A file inside a torrent and the library path it should be written to."""

    file_idx: int | None
    refs: tuple[EpisodeRef, ...]
    final_path: Path
    size: int
    candidate: Candidate

    @property
    def label(self) -> str:
        if not self.refs:
            return "Movie"
        if len(self.refs) == 1:
            return self.refs[0].code
        return f"{self.refs[0].code}-E{self.refs[-1].episode:02d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_idx": self.file_idx,
            "refs": [[r.season, r.episode] for r in self.refs],
            "final_path": str(self.final_path),
            "size": self.size,
            "candidate": self.candidate.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FileTarget:
        return cls(
            file_idx=d["file_idx"],
            refs=tuple(EpisodeRef(s, e) for s, e in d["refs"]),
            final_path=Path(d["final_path"]),
            size=d["size"],
            candidate=Candidate.from_dict(d["candidate"]),
        )


@dataclass
class TorrentJob:
    info_hash: str
    release: str
    trackers: tuple[str, ...]
    targets: list[FileTarget]
    peers: list[tuple[str, int]] = field(default_factory=list)  # extra peers to dial directly

    @property
    def total_size(self) -> int:
        return sum(t.size for t in self.targets)

    @property
    def keys(self) -> list[Key]:
        out: list[Key] = []
        for t in self.targets:
            out.extend(t.refs or [None])
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "info_hash": self.info_hash,
            "release": self.release,
            "trackers": list(self.trackers),
            "targets": [t.to_dict() for t in self.targets],
            "peers": [list(p) for p in self.peers],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TorrentJob:
        return cls(
            info_hash=d["info_hash"],
            release=d["release"],
            trackers=tuple(d.get("trackers", ())),
            targets=[FileTarget.from_dict(t) for t in d["targets"]],
            peers=[(h, int(p)) for h, p in d.get("peers", [])],
        )
