"""BitTorrent engine on top of libtorrent.

Each job is added as a magnet. Once metadata arrives, only the wanted files get a non-zero
priority and each one is renamed to its absolute library path with a ``.part`` suffix, so the
data is written straight into ``Show/Season NN/``. Finished files lose the suffix. Pieces that
straddle unwanted files go to libtorrent's part file in a per-torrent staging folder.

Torrents are never removed with ``delete_files``: their files are the user's library.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import libtorrent as lt

from ..config import Config
from ..models import FileTarget, TorrentJob
from ..streams import VIDEO_EXTS
from .base import EngineListener, JobView, TargetView

log = logging.getLogger(__name__)

DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.dler.org:6969/announce",
)
DHT_BOOTSTRAP = (
    "dht.libtorrent.org:25401,router.bittorrent.com:6881,"
    "router.utorrent.com:6881,dht.transmissionbt.com:6881"
)
PRIORITY_WANTED = 4
RESUME_INTERVAL = 60.0
RENAME_RETRY_DELAY = 2.0
RENAME_ATTEMPTS = 8
NOT_IN_TORRENT = "file not in torrent"


def _ih(handle: Any) -> str:
    try:
        return str(handle.info_hashes().v1)
    except AttributeError:  # older bindings
        return str(handle.info_hash())


def _layout(info: Any) -> Any:
    """The torrent's files as published (not our renames). ``layout()`` is the 2.1 name."""
    return info.layout() if hasattr(info, "layout") else info.orig_files()


def _basename(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).name


def _is_video(path: str) -> bool:
    return PurePosixPath(path.replace("\\", "/")).suffix.lower() in VIDEO_EXTS


def _matches_refs(name: str, target: FileTarget) -> bool:
    for ref in target.refs:
        s, e = ref.season, ref.episode
        pattern = rf"(?:s0*{s}[\s._-]*e0*{e}|(?<!\d){s}x0*{e})(?!\d)"
        if not re.search(pattern, name, re.I):
            return False
    return bool(target.refs)


@dataclass
class _Active:
    job: TorrentJob
    handle: Any
    added_at: float
    last_progress_at: float
    state: str = "metadata"
    mapping: dict[int, FileTarget] = field(default_factory=dict)  # file index -> target
    done: set[int] = field(default_factory=set)
    renaming: dict[int, tuple[int, float]] = field(default_factory=dict)  # idx -> (tries, next)
    unmapped: list[FileTarget] = field(default_factory=list)
    last_done: int = -1
    progress: list[int] = field(default_factory=list)
    status: Any = None
    error: str = ""

    @property
    def remaining(self) -> list[FileTarget]:
        open_ = [t for i, t in self.mapping.items() if i not in self.done]
        mapped = {id(t) for t in self.mapping.values()}
        return open_ + [t for t in self.job.targets if id(t) not in mapped]


class TorrentEngine:
    def __init__(
        self,
        cfg: Config,
        staging_root: Path,
        listener: EngineListener | None = None,
        poll_interval: float = 0.5,
        settings: dict[str, Any] | None = None,
    ):
        self.cfg = cfg
        self.staging_root = staging_root
        self.listener = listener
        self.poll_interval = poll_interval
        self._settings_override = settings or {}
        self._ses: Any = None
        self.pending: deque[tuple[TorrentJob, bytes | None]] = deque()
        self.active: dict[str, _Active] = {}
        self.finished: list[JobView] = []
        self._garbage: list[tuple[Path, int]] = []
        self._last_resume = time.monotonic()

    # -- session ---------------------------------------------------------------------------

    def _settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = {
            "user_agent": f"stremio-dl/0.1 libtorrent/{lt.__version__}",
            "listen_interfaces": self.cfg.listen_interfaces,
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "dht_bootstrap_nodes": DHT_BOOTSTRAP,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "alert_mask": lt.alert_category.error
            | lt.alert_category.status
            | lt.alert_category.storage,
        }
        if self.cfg.outgoing_interfaces:
            settings["outgoing_interfaces"] = self.cfg.outgoing_interfaces
        settings.update(self._settings_override)
        return settings

    @property
    def session(self) -> Any:
        if self._ses is None:
            self._ses = lt.session(self._settings())
        return self._ses

    def magnet(self, job: TorrentJob) -> str:
        trackers = dict.fromkeys((*job.trackers, *DEFAULT_TRACKERS))
        uri = f"magnet:?xt=urn:btih:{job.info_hash}&dn={quote(job.release or job.info_hash)}"
        return uri + "".join(f"&tr={quote(t, safe='')}" for t in trackers)

    # -- adding ----------------------------------------------------------------------------

    def add_job(self, job: TorrentJob, resume_data: bytes | None = None) -> None:
        active = self.active.get(job.info_hash)
        if active is not None:
            active.job.targets.extend(job.targets)
            if active.state != "metadata":
                self._map_targets(active, job.targets)
                self._apply_priorities(active)
            return
        for queued, _ in self.pending:
            if queued.info_hash == job.info_hash:
                queued.targets.extend(job.targets)
                return
        self.pending.append((job, resume_data))

    def _start(self, job: TorrentJob, resume_data: bytes | None) -> None:
        if resume_data:
            atp = lt.read_resume_data(resume_data)
        else:
            atp = lt.parse_magnet_uri(self.magnet(job))
            atp.save_path = str(self.staging_root / job.info_hash)
        Path(atp.save_path).mkdir(parents=True, exist_ok=True)
        atp.flags &= ~lt.torrent_flags.auto_managed
        atp.flags &= ~lt.torrent_flags.paused
        handle = self.session.add_torrent(atp)
        now = time.monotonic()
        self.active[job.info_hash] = _Active(job, handle, now, now)
        for host, port in job.peers:
            handle.connect_peer((host, port))
        log.info("Added %s (%s)", job.info_hash, job.release)

    def _start_pending(self) -> None:
        while self.pending and len(self.active) < max(1, self.cfg.max_active_torrents):
            job, resume = self.pending.popleft()
            try:
                self._start(job, resume)
            except Exception as exc:  # bad resume data, invalid magnet, disk errors
                log.exception("Could not add %s", job.info_hash)
                self._report_dead(job, f"could not add torrent: {exc}", list(job.targets))

    # -- metadata --------------------------------------------------------------------------

    def _locate(self, target: FileTarget, paths: list[str], sizes: list[int],
                usable: list[int], taken: set[int]) -> int | None:  # fmt: skip
        free = [i for i in usable if i not in taken]
        want = _basename(target.candidate.filename).lower() if target.candidate.filename else ""
        idx = target.file_idx
        if idx is not None and idx in free:
            name = _basename(paths[idx])
            if not want or name.lower() == want or _matches_refs(name, target):
                return idx
        if want:
            for i in free:
                if _basename(paths[i]).lower() == want:
                    return i
        if target.refs:
            hits = [i for i in free if _matches_refs(_basename(paths[i]), target)]
            return max(hits, key=lambda i: sizes[i]) if hits else None
        return max(free, key=lambda i: sizes[i]) if free else None  # a movie: biggest video

    def _map_targets(self, a: _Active, targets: list[FileTarget]) -> None:
        files = _layout(a.handle.torrent_file())  # original names matter on resume
        count = files.num_files()
        paths = [files.file_path(i) for i in range(count)]
        sizes = [files.file_size(i) for i in range(count)]
        pad_flag = lt.file_storage.flag_pad_file
        usable = [
            i for i in range(count) if not files.file_flags(i) & pad_flag and _is_video(paths[i])
        ]
        for target in targets:
            idx = self._locate(target, paths, sizes, usable, set(a.mapping))
            if idx is None:
                a.unmapped.append(target)
                log.warning(
                    "%s: no file for %s in %s", a.job.info_hash, target.label, a.job.release
                )
                continue
            ext = PurePosixPath(paths[idx].replace("\\", "/")).suffix.lower()
            target.final_path = target.final_path.with_suffix(ext)
            target.file_idx = idx
            target.size = sizes[idx]
            a.mapping[idx] = target
            target.final_path.parent.mkdir(parents=True, exist_ok=True)
            if target.final_path.exists() and target.final_path.stat().st_size == sizes[idx]:
                a.handle.rename_file(idx, str(target.final_path))  # finished on a previous run
                a.done.add(idx)
            else:
                a.handle.rename_file(idx, str(target.final_path) + ".part")

    def _apply_priorities(self, a: _Active) -> None:
        count = a.handle.torrent_file().num_files()
        priorities = [0] * count
        for idx in a.mapping:
            priorities[idx] = PRIORITY_WANTED
        a.handle.prioritize_files(priorities)

    def _on_metadata(self, a: _Active, now: float) -> None:
        self._map_targets(a, list(a.job.targets))
        if not a.mapping:
            self._kill(a, "none of the wanted files are in this torrent")
            return
        self._apply_priorities(a)
        a.state = "downloading"
        a.last_progress_at = now
        if a.unmapped:
            # Re-plan the episodes this torrent turned out not to contain.
            missing, a.unmapped = a.unmapped, []
            a.job.targets = [t for t in a.job.targets if all(t is not m for m in missing)]
            orphan = TorrentJob(a.job.info_hash, a.job.release, a.job.trackers, missing)
            self._report_dead(orphan, NOT_IN_TORRENT, missing)
        if self.listener:
            self.listener.on_metadata(a.job)

    # -- per-tick work ---------------------------------------------------------------------

    def _tick(self, a: _Active, now: float) -> None:
        if not a.handle.is_valid():
            self._kill(a, "torrent handle became invalid")
            return
        a.status = a.handle.status()
        if a.state == "metadata":
            if a.status.has_metadata:
                self._on_metadata(a, now)
            elif now - a.added_at > self.cfg.metadata_timeout:
                self._kill(a, f"no metadata after {self.cfg.metadata_timeout}s (dead swarm?)")
            return

        a.progress = list(a.handle.file_progress(lt.torrent_handle.piece_granularity))
        done_bytes = sum(min(a.progress[i], t.size) for i, t in a.mapping.items())
        if done_bytes > a.last_done:
            a.last_done = done_bytes
            a.last_progress_at = now
        elif a.state == "downloading" and now - a.last_progress_at > self.cfg.stall_timeout:
            self._kill(a, f"no progress for {self.cfg.stall_timeout}s")
            return

        for idx, target in a.mapping.items():
            if idx in a.done:
                continue
            tries, next_at = a.renaming.get(idx, (0, 0.0))
            if a.progress[idx] >= target.size and now >= next_at and (tries == 0 or next_at):
                if tries >= RENAME_ATTEMPTS:
                    continue
                a.handle.rename_file(idx, str(target.final_path))
                a.renaming[idx] = (tries + 1, 0.0)  # next_at 0 = waiting for the alert

        if len(a.done) == len(a.mapping) and not a.renaming:
            if self.cfg.seed_ratio > 0 and a.state != "seeding":
                a.state = "seeding"
            if a.state == "seeding":
                wanted = max(1, sum(t.size for t in a.mapping.values()))
                if a.status.all_time_upload / wanted < self.cfg.seed_ratio:
                    return
            self._finish(a)

    def _on_renamed(self, info_hash: str, idx: int, ok: bool, error: str = "") -> None:
        a = self.active.get(info_hash)
        if a is None or idx not in a.renaming:
            return  # the initial ".part" rename, or a torrent we already dropped
        tries, _ = a.renaming.pop(idx)
        target = a.mapping[idx]
        if ok:
            a.done.add(idx)
            log.info("Finished %s -> %s", target.label, target.final_path)
            if self.listener:
                self.listener.on_target_done(a.job, target)
        elif tries < RENAME_ATTEMPTS:
            log.warning("Rename of %s failed (%s); retrying", target.final_path, error)
            a.renaming[idx] = (tries, time.monotonic() + RENAME_RETRY_DELAY)
        else:
            a.error = f"could not rename {target.final_path.name}.part: {error}"
            a.done.add(idx)  # leave the complete .part file in place rather than loop forever

    def _process_alerts(self) -> None:
        if self._ses is None:
            return
        for alert in self._ses.pop_alerts():
            if isinstance(alert, lt.file_renamed_alert):
                new_name = getattr(alert, "new_name", "")
                new_name = new_name() if callable(new_name) else new_name
                if not str(new_name).endswith(".part"):  # ignore the initial ".part" rename
                    self._on_renamed(_ih(alert.handle), alert.index, True)
            elif isinstance(alert, lt.file_rename_failed_alert):
                self._on_renamed(_ih(alert.handle), alert.index, False, alert.message())
            elif isinstance(alert, lt.save_resume_data_alert):
                if self.listener:
                    data = lt.write_resume_data_buf(alert.params)
                    self.listener.on_resume_data(_ih(alert.handle), data)
            elif isinstance(alert, lt.torrent_error_alert):
                log.error("Torrent error: %s", alert.message())
                a = self.active.get(_ih(alert.handle))
                if a:
                    a.error = alert.message()
            elif isinstance(alert, lt.listen_failed_alert):
                log.error("Listen failed: %s", alert.message())

    def _request_resume_data(self) -> int:
        flags = getattr(lt.torrent_handle, "save_info_dict", 0)
        count = 0
        for a in self.active.values():
            if a.handle.is_valid() and a.state != "metadata":
                a.handle.save_resume_data(flags)
                count += 1
        return count

    # -- endings ---------------------------------------------------------------------------

    def _drop(self, a: _Active) -> None:
        self.active.pop(a.job.info_hash, None)
        if a.handle.is_valid():
            self.session.remove_torrent(a.handle)  # never delete_files: those are library files
        self._garbage.append((self.staging_root / a.job.info_hash, 0))

    def _finish(self, a: _Active) -> None:
        self._drop(a)
        self.finished.append(self._view(a, "done"))
        if self.listener:
            self.listener.on_job_done(a.job)

    def _kill(self, a: _Active, reason: str) -> None:
        log.warning("Giving up on %s (%s): %s", a.job.info_hash, a.job.release, reason)
        remaining = a.remaining
        self._drop(a)
        for target in remaining:
            self._garbage.append((Path(str(target.final_path) + ".part"), 0))
        a.error = reason
        self.finished.append(self._view(a, "dead"))
        self._report_dead(a.job, reason, remaining)

    def _report_dead(self, job: TorrentJob, reason: str, remaining: list[FileTarget]) -> None:
        if self.listener and remaining:
            for replacement in self.listener.on_job_dead(job, reason, remaining):
                self.add_job(replacement)

    def _collect_garbage(self, final: bool = False) -> None:
        keep = []
        for path, tries in self._garbage:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
                    for parent in (path.parent, path.parent.parent):
                        try:
                            parent.rmdir()  # only succeeds for folders left empty
                        except OSError:
                            break
            except OSError:
                if tries < 20 and not final:
                    keep.append((path, tries + 1))  # libtorrent may still hold the handle
        self._garbage = keep

    # -- loop ------------------------------------------------------------------------------

    async def run(self, on_tick: Callable[[list[JobView]], None] | None = None) -> None:
        try:
            while self.pending or self.active:
                self._process_alerts()
                self._start_pending()
                now = time.monotonic()
                for a in list(self.active.values()):
                    self._tick(a, now)
                self._collect_garbage()
                if now - self._last_resume > RESUME_INTERVAL:
                    self._request_resume_data()
                    self._last_resume = now
                if on_tick:
                    on_tick(self.snapshot())
                await asyncio.sleep(self.poll_interval)
            self._process_alerts()
            self._collect_garbage()
            if on_tick:
                on_tick(self.snapshot())
        except asyncio.CancelledError:
            self.shutdown(save_resume=True)
            raise

    def shutdown(self, save_resume: bool = True) -> None:
        """Pause everything, save resume data for unfinished torrents and close the session."""
        if self._ses is None:
            return
        self._ses.pause()
        outstanding = self._request_resume_data() if save_resume else 0
        deadline = time.monotonic() + 10
        while outstanding > 0 and time.monotonic() < deadline:
            if self._ses.wait_for_alert(500) is None:
                continue
            for alert in self._ses.pop_alerts():
                if isinstance(alert, lt.save_resume_data_alert):
                    outstanding -= 1
                    if self.listener:
                        data = lt.write_resume_data_buf(alert.params)
                        self.listener.on_resume_data(_ih(alert.handle), data)
                elif isinstance(alert, lt.save_resume_data_failed_alert):
                    outstanding -= 1
        self._ses = None  # the session closes when the last reference goes away
        self._collect_garbage(final=True)

    # -- views -----------------------------------------------------------------------------

    def _view(self, a: _Active, state: str | None = None) -> JobView:
        st = a.status
        view = JobView(
            info_hash=a.job.info_hash,
            release=a.job.release,
            state=state or a.state,
            down_rate=st.download_payload_rate if st else 0,
            up_rate=st.upload_payload_rate if st else 0,
            peers=st.num_peers if st else 0,
            seeds=st.num_seeds if st else 0,
            error=a.error,
        )
        by_target = {id(t): i for i, t in a.mapping.items()}
        for target in a.job.targets:
            idx = by_target.get(id(target))
            if idx is None:
                tstate = "metadata" if a.state == "metadata" else "failed"
                if state == "dead":
                    tstate = "failed"
                view.targets.append(TargetView(target.label, target.final_path.name,
                                               target.size, 0, tstate))  # fmt: skip
                continue
            got = a.progress[idx] if idx < len(a.progress) else 0
            if idx in a.done:
                tstate, got = "done", target.size
            elif state == "dead":
                tstate = "failed"
            else:
                tstate = "downloading"
            view.targets.append(
                TargetView(target.label, target.final_path.name, target.size, got, tstate)
            )
        return view

    def snapshot(self) -> list[JobView]:
        views = list(self.finished)
        views += [self._view(a) for a in self.active.values()]
        for job, _ in self.pending:
            targets = [TargetView(t.label, t.final_path.name, t.size, 0, "queued")
                       for t in job.targets]  # fmt: skip
            views.append(JobView(job.info_hash, job.release, "queued", targets=targets))
        return views
