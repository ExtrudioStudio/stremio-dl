from __future__ import annotations

from pathlib import Path

from stremio_dl.models import Candidate, EpisodeRef, Title
from stremio_dl.planner import Plan, build_plan, replan

PACK = "b" * 40


def cand(hash_char: str, seeders: int, file_idx: int | None = 0, res: str = "1080p",
         filename: str = "x.mkv", size: int = 1000) -> Candidate:  # fmt: skip
    return Candidate(hash_char * 40 if len(hash_char) == 1 else hash_char, file_idx, res,
                     seeders, size, filename=filename, release=f"rel-{hash_char}")  # fmt: skip


def e(s: int, n: int) -> EpisodeRef:
    return EpisodeRef(s, n)


def test_episodes_from_one_pack_become_one_torrent(friends: Title, cfg):
    keys = [e(1, 1), e(1, 2)]
    cands = {
        e(1, 1): [cand(PACK, 100, 0, filename="Friends.S01E01.mkv"), cand("c", 5)],
        e(1, 2): [cand(PACK, 100, 1, filename="Friends.S01E02.mp4"), cand("d", 5)],
    }
    plan = build_plan(friends, keys, cands, cfg, "1080p")
    assert len(plan.jobs) == 1
    job = plan.jobs[0]
    assert job.info_hash == PACK
    assert [t.refs for t in job.targets] == [(e(1, 1),), (e(1, 2),)]
    first = job.targets[0].final_path
    assert first == Path(cfg.tv_root) / "Friends" / "Season 01" / (
        "Friends - S01E01 - The One Where Monica Gets a Roommate.mkv"
    )
    assert job.targets[1].final_path.suffix == ".mp4"


def test_double_episode_file_gets_one_target(friends, cfg):
    keys = [e(1, 16), e(1, 17)]
    shared = cand("a", 40, 7)
    plan = build_plan(friends, keys, {e(1, 16): [shared], e(1, 17): [shared]}, cfg, "1080p")
    (job,) = plan.jobs
    (target,) = job.targets
    assert target.refs == (e(1, 16), e(1, 17))
    assert "S01E16-E17" in target.final_path.name


def test_highest_seeders_win_unless_packs_are_preferred(friends, cfg):
    keys = [e(2, i) for i in range(1, 5)]
    cands = {
        k: [cand(chr(ord("c") + i), 100), cand(PACK, 60, i)] for i, k in enumerate(keys)
    }  # each episode has its own best torrent; the pack has 60% of the seeders
    strict = build_plan(friends, keys, cands, cfg, "1080p")
    assert len(strict.jobs) == 4 and PACK not in {j.info_hash for j in strict.jobs}

    packed = build_plan(friends, keys, cands, cfg, "1080p", prefer_packs=True)
    assert [j.info_hash for j in packed.jobs] == [PACK]
    assert len(packed.jobs[0].targets) == 4


def test_missing_and_existing_episodes(friends, cfg):
    existing = Path(cfg.tv_root) / "Friends" / "Season 01" / "Friends.S01E03.720p.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"x")
    keys = [e(1, 1), e(1, 2), e(1, 3)]
    cands = {e(1, 1): [cand("a", 9)], e(1, 2): [cand("c", 9, res="720p")], e(1, 3): []}
    plan = build_plan(friends, keys, cands, cfg, "1080p")
    assert plan.missing == {e(1, 2): "no 1080p streams (available: 720p)"}
    assert plan.existing == {e(1, 3): existing}
    assert len(plan.jobs) == 1

    lower = build_plan(friends, keys, cands, cfg, "1080p", on_missing="lower")
    assert lower.used_quality[e(1, 2)] == "720p" and not lower.missing


def test_replan_moves_to_the_next_alive_candidate(friends, cfg):
    keys = [e(1, 1)]
    plan = build_plan(friends, keys, {e(1, 1): [cand("a", 9), cand("c", 5)]}, cfg, "1080p")
    assert plan.jobs[0].info_hash == "a" * 40
    (job,) = replan(plan, cfg, "a" * 40, keys)
    assert job.info_hash == "c" * 40
    assert replan(plan, cfg, "c" * 40, keys) == []
    assert "every candidate" in plan.missing[e(1, 1)]


def test_movie_plan_and_round_trip(cfg):
    movie = Title("tt1375666", "movie", "Inception: Part 1?", 2010)
    plan = build_plan(movie, [None], {None: [cand("a", 9, None, filename="i.mp4")]}, cfg, "1080p")
    target = plan.jobs[0].targets[0]
    assert target.final_path == Path(cfg.movie_root) / "Inception - Part 1 (2010)" / (
        "Inception - Part 1 (2010).mp4"
    )
    again = Plan.from_dict(plan.to_dict())
    assert again.choices == plan.choices and again.title.name == movie.name
