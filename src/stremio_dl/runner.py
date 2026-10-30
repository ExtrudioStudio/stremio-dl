"""Glue between a plan, the torrent engine, the state store and the live progress display."""

from __future__ import annotations

import asyncio
import logging

from rich.live import Live

from .config import Config, data_dir
from .engine.base import JobView
from .engine.torrent import NOT_IN_TORRENT, TorrentEngine
from .models import FileTarget, TorrentJob
from .planner import Plan, replan
from .store import Store
from .ui import console, progress_table

log = logging.getLogger(__name__)


class DownloadRunner:
    """Runs one stored job to completion; implements :class:`EngineListener`."""

    def __init__(self, cfg: Config, store: Store, job_id: int, plan: Plan):
        self.cfg = cfg
        self.store = store
        self.job_id = job_id
        self.plan = plan
        self.engine = TorrentEngine(cfg, data_dir() / "staging", listener=self)

    # -- EngineListener --------------------------------------------------------------------

    def on_metadata(self, job: TorrentJob) -> None:
        self.store.update_torrent(self.job_id, job.info_hash, status="active", job=job)
        for target in job.targets:
            for key in target.refs or (None,):
                self.store.set_item(self.job_id, key, "downloading", info_hash=job.info_hash,
                                    path=str(target.final_path))  # fmt: skip

    def on_target_done(self, job: TorrentJob, target: FileTarget) -> None:
        for key in target.refs or (None,):
            self.store.set_item(self.job_id, key, "done", path=str(target.final_path))
        self.store.update_torrent(self.job_id, job.info_hash, job=job)

    def on_job_done(self, job: TorrentJob) -> None:
        self.store.update_torrent(self.job_id, job.info_hash, status="done", job=job)

    def on_job_dead(
        self, job: TorrentJob, reason: str, remaining: list[FileTarget]
    ) -> list[TorrentJob]:
        if reason != NOT_IN_TORRENT:  # otherwise the torrent keeps serving its other files
            self.store.update_torrent(self.job_id, job.info_hash, status="dead", error=reason)
        keys = [key for t in remaining for key in (t.refs or (None,))]
        replacements = replan(self.plan, self.cfg, job.info_hash, keys)
        for key in keys:
            if key in self.plan.missing:
                self.store.set_item(self.job_id, key, "failed", error=self.plan.missing[key])
        for new_job in replacements:
            self.store.add_torrent(self.job_id, new_job)
        self.store.save_plan(self.job_id, self.plan)
        return replacements

    def on_resume_data(self, info_hash: str, data: bytes) -> None:
        self.store.update_torrent(self.job_id, info_hash, resume=data)

    # -- running ---------------------------------------------------------------------------

    async def run(self, torrents: list[tuple[TorrentJob, bytes | None]]) -> str:
        for job, resume in torrents:
            self.engine.add_job(job, resume)
        with Live(progress_table([]), console=console, refresh_per_second=4) as live:

            def tick(views: list[JobView]) -> None:
                live.update(progress_table(views))

            try:
                await self.engine.run(on_tick=tick)
            except asyncio.CancelledError:
                self.store.pause_open(self.job_id)
                self.store.finish_job(self.job_id)
                raise
            finally:
                self.engine.shutdown(save_resume=True)  # no-op when run() already shut down
        return self.store.finish_job(self.job_id)
