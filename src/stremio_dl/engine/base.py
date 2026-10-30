"""The downloader contract, so a debrid/HTTP backend can sit next to the torrent engine later."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..models import FileTarget, TorrentJob


@dataclass
class TargetView:
    label: str
    name: str
    size: int
    done: int
    state: str  # queued | metadata | downloading | done | failed


@dataclass
class JobView:
    info_hash: str
    release: str
    state: str  # queued | metadata | downloading | seeding | done | dead
    down_rate: int = 0
    up_rate: int = 0
    peers: int = 0
    seeds: int = 0
    targets: list[TargetView] = field(default_factory=list)
    error: str = ""


class EngineListener(Protocol):
    def on_metadata(self, job: TorrentJob) -> None: ...

    def on_target_done(self, job: TorrentJob, target: FileTarget) -> None: ...

    def on_job_done(self, job: TorrentJob) -> None: ...

    def on_job_dead(
        self, job: TorrentJob, reason: str, remaining: list[FileTarget]
    ) -> list[TorrentJob]:
        """Called when a torrent is abandoned; returns replacement jobs to try next."""
        ...

    def on_resume_data(self, info_hash: str, data: bytes) -> None: ...


class Downloader(Protocol):
    def add_job(self, job: TorrentJob, resume_data: bytes | None = None) -> None: ...

    async def run(self, on_tick: Callable[[list[JobView]], None] | None = None) -> None: ...

    def shutdown(self, save_resume: bool = True) -> None: ...

    def snapshot(self) -> list[JobView]: ...
