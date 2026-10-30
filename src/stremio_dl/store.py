"""SQLite state: download jobs, per-episode items and per-torrent resume data."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import Key, TorrentJob
from .planner import Plan

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id TEXT NOT NULL,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    quality TEXT NOT NULL,
    status TEXT NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    plan_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    job_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    status TEXT NOT NULL,
    info_hash TEXT,
    final_path TEXT,
    error TEXT,
    updated TEXT NOT NULL,
    PRIMARY KEY (job_id, key)
);
CREATE TABLE IF NOT EXISTS torrents (
    job_id INTEGER NOT NULL,
    info_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    job_json TEXT NOT NULL,
    resume BLOB,
    error TEXT,
    updated TEXT NOT NULL,
    PRIMARY KEY (job_id, info_hash)
);
"""

OPEN_TORRENT = ("queued", "active", "paused")


def key_name(key: Key) -> str:
    return "movie" if key is None else key.code


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class JobRow:
    id: int
    title: str
    type: str
    quality: str
    status: str
    created: str
    updated: str
    counts: dict[str, int]


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- jobs ------------------------------------------------------------------------------

    def create_job(self, plan: Plan) -> int:
        now = _now()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO jobs (title_id, title, type, quality, status, created, updated, "
                "plan_json) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
                (
                    plan.title.id,
                    plan.title.name,
                    plan.title.type,
                    plan.quality,
                    now,
                    now,
                    json.dumps(plan.to_dict()),
                ),  # fmt: skip
            )
            job_id = int(cur.lastrowid)
            for key in plan.keys:
                if key in plan.existing:
                    self.set_item(job_id, key, "skipped", path=str(plan.existing[key]))
                elif key in plan.missing:
                    self.set_item(job_id, key, "failed", error=plan.missing[key])
            for job in plan.jobs:
                self.add_torrent(job_id, job)
        return job_id

    def save_plan(self, job_id: int, plan: Plan) -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET plan_json = ?, updated = ? WHERE id = ?",
                (json.dumps(plan.to_dict()), _now(), job_id),
            )

    def load_plan(self, job_id: int) -> Plan:
        row = self.db.execute("SELECT plan_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"No download job #{job_id}")
        return Plan.from_dict(json.loads(row["plan_json"]))

    def set_job_status(self, job_id: int, status: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status = ?, updated = ? WHERE id = ?", (status, _now(), job_id)
            )

    def jobs(self, include_finished: bool = True, limit: int = 30) -> list[JobRow]:
        sql = "SELECT * FROM jobs"
        if not include_finished:
            sql += " WHERE status IN ('running', 'paused')"
        rows = self.db.execute(sql + " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            counts = dict(
                self.db.execute(
                    "SELECT status, COUNT(*) FROM items WHERE job_id = ? GROUP BY status",
                    (r["id"],),
                ).fetchall()
            )
            out.append(JobRow(r["id"], r["title"], r["type"], r["quality"], r["status"],
                              r["created"], r["updated"], counts))  # fmt: skip
        return out

    def finish_job(self, job_id: int) -> str:
        """Set the final status from the items' states and return it."""
        open_ = self.db.execute(
            "SELECT COUNT(*) FROM torrents WHERE job_id = ? AND status IN (?, ?, ?)",
            (job_id, *OPEN_TORRENT),
        ).fetchone()[0]
        counts = dict(
            self.db.execute(
                "SELECT status, COUNT(*) FROM items WHERE job_id = ? GROUP BY status", (job_id,)
            ).fetchall()
        )
        if open_:
            status = "paused"
        elif counts.get("failed"):
            status = "partial" if counts.get("done") else "failed"
        else:
            status = "done"
        self.set_job_status(job_id, status)
        return status

    # -- items -----------------------------------------------------------------------------

    def set_item(
        self,
        job_id: int,
        key: Key,
        status: str,
        info_hash: str | None = None,
        path: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO items (job_id, key, status, info_hash, final_path, error, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (job_id, key) DO UPDATE SET "
                "status = excluded.status, info_hash = COALESCE(excluded.info_hash, info_hash), "
                "final_path = COALESCE(excluded.final_path, final_path), error = excluded.error, "
                "updated = excluded.updated",
                (job_id, key_name(key), status, info_hash, path, error, _now()),
            )

    # -- torrents --------------------------------------------------------------------------

    def add_torrent(self, job_id: int, job: TorrentJob) -> None:
        """Insert a torrent, or add new targets to one that's already recorded."""
        row = self.db.execute(
            "SELECT job_json FROM torrents WHERE job_id = ? AND info_hash = ?",
            (job_id, job.info_hash),
        ).fetchone()
        if row is not None:
            stored = TorrentJob.from_dict(json.loads(row["job_json"]))
            known = {str(t.final_path) for t in stored.targets}
            stored.targets += [t for t in job.targets if str(t.final_path) not in known]
            job = stored
        with self.db:
            self.db.execute(
                "INSERT INTO torrents (job_id, info_hash, status, job_json, updated) "
                "VALUES (?, ?, 'queued', ?, ?) ON CONFLICT (job_id, info_hash) DO UPDATE SET "
                "status = 'queued', job_json = excluded.job_json, updated = excluded.updated",
                (job_id, job.info_hash, json.dumps(job.to_dict()), _now()),
            )
        for target in job.targets:
            for key in target.refs or (None,):
                self.set_item(job_id, key, "queued", info_hash=job.info_hash,
                              path=str(target.final_path))  # fmt: skip

    def update_torrent(
        self,
        job_id: int,
        info_hash: str,
        status: str | None = None,
        job: TorrentJob | None = None,
        resume: bytes | None = None,
        error: str | None = None,
    ) -> None:
        sets, args = ["updated = ?"], [_now()]
        if status is not None:
            sets.append("status = ?")
            args.append(status)
        if job is not None:
            sets.append("job_json = ?")
            args.append(json.dumps(job.to_dict()))
        if resume is not None:
            sets.append("resume = ?")
            args.append(resume)
        if error is not None:
            sets.append("error = ?")
            args.append(error)
        with self.db:
            self.db.execute(
                f"UPDATE torrents SET {', '.join(sets)} WHERE job_id = ? AND info_hash = ?",
                (*args, job_id, info_hash),
            )

    def open_torrents(self, job_id: int) -> list[tuple[TorrentJob, bytes | None]]:
        rows = self.db.execute(
            "SELECT job_json, resume FROM torrents WHERE job_id = ? AND status IN (?, ?, ?)",
            (job_id, *OPEN_TORRENT),
        ).fetchall()
        return [(TorrentJob.from_dict(json.loads(r["job_json"])), r["resume"]) for r in rows]

    def pause_open(self, job_id: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE torrents SET status = 'paused', updated = ? "
                "WHERE job_id = ? AND status IN ('queued', 'active')",
                (_now(), job_id),
            )
