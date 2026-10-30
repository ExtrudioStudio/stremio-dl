from __future__ import annotations

from stremio_dl.models import Candidate, EpisodeRef
from stremio_dl.planner import build_plan, replan
from stremio_dl.runner import DownloadRunner
from stremio_dl.store import Store


def _plan(friends, cfg):
    keys = [EpisodeRef(1, 1), EpisodeRef(1, 2)]
    a = Candidate("a" * 40, 0, "1080p", 9, 100, release="A")
    b = Candidate("b" * 40, 0, "1080p", 5, 100, release="B")
    return build_plan(friends, keys, {keys[0]: [a, b], keys[1]: [a.__class__(
        **{**a.to_dict(), "file_idx": 1})]}, cfg, "1080p")  # fmt: skip


def test_job_lifecycle(tmp_path, friends, cfg):
    store = Store(tmp_path / "state.db")
    plan = _plan(friends, cfg)
    job_id = store.create_job(plan)
    (row,) = store.jobs()
    assert row.status == "running" and row.counts == {"queued": 2}

    runner = DownloadRunner(cfg, store, job_id, store.load_plan(job_id))
    job = store.open_torrents(job_id)[0][0]
    runner.on_metadata(job)
    runner.on_resume_data(job.info_hash, b"resume-bytes")
    runner.on_target_done(job, job.targets[0])
    assert store.open_torrents(job_id)[0][1] == b"resume-bytes"

    # The torrent dies with S01E02 unfinished: S01E02 has no other candidate, so it fails.
    replacements = runner.on_job_dead(job, "stalled", [job.targets[1]])
    assert replacements == []
    assert store.open_torrents(job_id) == []
    assert store.finish_job(job_id) == "partial"
    assert store.jobs()[0].counts == {"done": 1, "failed": 1}
    store.close()


def test_replacement_torrents_are_recorded(tmp_path, friends, cfg):
    store = Store(tmp_path / "state.db")
    plan = _plan(friends, cfg)
    job_id = store.create_job(plan)
    runner = DownloadRunner(cfg, store, job_id, store.load_plan(job_id))
    job = store.open_torrents(job_id)[0][0]
    (new_job,) = runner.on_job_dead(job, "no metadata", [job.targets[0]])
    assert new_job.info_hash == "b" * 40
    assert {j.info_hash for j, _ in store.open_torrents(job_id)} == {"b" * 40}
    assert "a" * 40 in store.load_plan(job_id).dead
    assert replan(store.load_plan(job_id), cfg, "b" * 40, [EpisodeRef(1, 1)]) == []
    store.close()
