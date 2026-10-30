"""Turn ranked candidates into torrent jobs: one job per torrent, one target per wanted file."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import quality as q
from .config import Config
from .library import find_existing, target_path
from .models import (
    Candidate,
    FileTarget,
    Key,
    Title,
    TorrentJob,
    key_from_json,
    key_to_json,
)
from .streams import VIDEO_EXTS, choose

PACK_MIN_EPISODES = 3
PACK_SEEDER_RATIO = 0.5


@dataclass
class Plan:
    title: Title
    quality: str
    keys: list[Key]
    choices: dict[Key, list[Candidate]] = field(default_factory=dict)
    used_quality: dict[Key, str] = field(default_factory=dict)
    jobs: list[TorrentJob] = field(default_factory=list)
    missing: dict[Key, str] = field(default_factory=dict)
    existing: dict[Key, Path] = field(default_factory=dict)
    dead: set[str] = field(default_factory=set)

    @property
    def total_size(self) -> int:
        return sum(job.total_size for job in self.jobs)

    @property
    def file_count(self) -> int:
        return sum(len(job.targets) for job in self.jobs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title.to_dict(),
            "quality": self.quality,
            "keys": [key_to_json(k) for k in self.keys],
            "choices": [
                [key_to_json(k), [c.to_dict() for c in v]] for k, v in self.choices.items()
            ],
            "used_quality": [[key_to_json(k), v] for k, v in self.used_quality.items()],
            "missing": [[key_to_json(k), v] for k, v in self.missing.items()],
            "dead": sorted(self.dead),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Plan:
        return cls(
            title=Title.from_dict(d["title"]),
            quality=d["quality"],
            keys=[key_from_json(k) for k in d["keys"]],
            choices={
                key_from_json(k): [Candidate.from_dict(c) for c in v] for k, v in d["choices"]
            },
            used_quality={key_from_json(k): v for k, v in d.get("used_quality", [])},
            missing={key_from_json(k): v for k, v in d.get("missing", [])},
            dead=set(d.get("dead", [])),
        )


def _ext(c: Candidate) -> str:
    ext = PurePosixPath(c.filename.lower()).suffix
    return ext if ext in VIDEO_EXTS else ".mkv"  # corrected from the real file after metadata


def _consolidate_packs(ranked: dict[Key, list[Candidate]]) -> dict[Key, Candidate]:
    """Prefer one well-seeded pack over many separate torrents when it covers 3+ episodes."""
    cover: dict[str, dict[Key, Candidate]] = defaultdict(dict)
    for key, cands in ranked.items():
        floor = cands[0].seeders * PACK_SEEDER_RATIO
        for c in cands:
            if c.seeders >= floor and key not in cover[c.info_hash]:
                cover[c.info_hash][key] = c
    assigned: dict[Key, Candidate] = {}
    order = sorted(cover.values(), key=lambda m: (-len(m), -sum(c.seeders for c in m.values())))
    for members in order:
        free = {k: c for k, c in members.items() if k not in assigned}
        if len(free) >= PACK_MIN_EPISODES:
            assigned.update(free)
    return assigned


def group_jobs(title: Title, cfg: Config, winners: dict[Key, Candidate]) -> list[TorrentJob]:
    by_file: dict[tuple[str, int | None], list[tuple[Key, Candidate]]] = defaultdict(list)
    for key, cand in winners.items():
        slot = (cand.info_hash, cand.file_idx)
        if cand.file_idx is None:
            slot = (cand.info_hash, None if key is None else -(key.season * 10_000 + key.episode))
        by_file[slot].append((key, cand))

    jobs: dict[str, TorrentJob] = {}
    for (info_hash, _), members in by_file.items():
        cand = members[0][1]
        refs = tuple(sorted(k for k, _ in members if k is not None))
        target = FileTarget(
            file_idx=cand.file_idx,
            refs=refs,
            final_path=target_path(cfg, title, refs, _ext(cand)),
            size=cand.size,
            candidate=cand,
        )
        job = jobs.get(info_hash)
        if job is None:
            job = jobs[info_hash] = TorrentJob(info_hash, cand.release, cand.trackers, [])
        job.targets.append(target)
        job.trackers = tuple(dict.fromkeys(job.trackers + cand.trackers))

    for job in jobs.values():
        job.targets.sort(key=lambda t: t.refs)
    return sorted(jobs.values(), key=lambda j: j.targets[0].refs)


def _missing_reason(cands: list[Candidate], requested: str) -> str:
    if not cands:
        return "no streams found"
    available = sorted({c.resolution for c in cands}, key=q.rank, reverse=True)
    return f"no {requested} streams (available: {', '.join(available)})"


def build_plan(
    title: Title,
    keys: list[Key],
    candidates: dict[Key, list[Candidate]],
    cfg: Config,
    requested: str,
    on_missing: str = "skip",
    prefer_packs: bool = False,
    skip_existing: bool = True,
) -> Plan:
    plan = Plan(title, requested, list(keys))
    ranked: dict[Key, list[Candidate]] = {}
    for key in keys:
        refs = () if key is None else (key,)
        if skip_existing:
            found = find_existing(target_path(cfg, title, refs, ".mkv"), refs)
            if found:
                plan.existing[key] = found
                continue
        cands = candidates.get(key, [])
        choices, used = choose(cands, requested, on_missing, limit=len(cands))
        if not choices:
            plan.missing[key] = _missing_reason(cands, requested)
            continue
        ranked[key] = choices
        plan.used_quality[key] = used or requested

    winners = {key: cands[0] for key, cands in ranked.items()}
    if prefer_packs:
        winners.update(_consolidate_packs(ranked))
    limit = max(1, cfg.fallbacks_per_episode)
    for key, cands in ranked.items():
        winner = winners[key]
        rest = [c for c in cands if c is not winner]
        plan.choices[key] = [winner, *rest][:limit]
    plan.jobs = group_jobs(title, cfg, winners)
    return plan


def replan(plan: Plan, cfg: Config, dead_hash: str, keys: list[Key]) -> list[TorrentJob]:
    """Replacement jobs for ``keys`` after the torrent ``dead_hash`` failed."""
    plan.dead.add(dead_hash)
    winners: dict[Key, Candidate] = {}
    for key in keys:
        alive = [c for c in plan.choices.get(key, []) if c.info_hash not in plan.dead]
        plan.choices[key] = alive
        if alive:
            winners[key] = alive[0]
        else:
            plan.missing[key] = "every candidate torrent failed (no peers or no metadata)"
    return group_jobs(plan.title, cfg, winners)
