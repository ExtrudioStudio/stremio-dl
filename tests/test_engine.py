"""End-to-end engine tests against a local libtorrent seeder (no internet needed)."""

from __future__ import annotations

import asyncio
import os
import socket
import time
from pathlib import Path

import libtorrent as lt
import pytest

from stremio_dl.config import Config
from stremio_dl.engine.torrent import TorrentEngine
from stremio_dl.models import Candidate, EpisodeRef, FileTarget, TorrentJob

pytestmark = pytest.mark.torrent


def _free_port() -> int:
    """A port free for both TCP and UDP (libtorrent binds uTP on the same number)."""
    for _ in range(50):
        with socket.socket() as tcp, socket.socket(type=socket.SOCK_DGRAM) as udp:
            tcp.bind(("127.0.0.1", 0))
            port = tcp.getsockname()[1]
            try:
                udp.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port")


def _local(port: int | None = None) -> dict:
    return {**LOCAL, "listen_interfaces": f"127.0.0.1:{port or _free_port()}"}


LOCAL = {
    "listen_interfaces": "127.0.0.1:0",
    "enable_dht": False,
    "enable_lsd": False,
    "enable_upnp": False,
    "enable_natpmp": False,
}
FILES = {
    "Show.S01E01.1080p.WEB.mkv": 300_000,
    "Show.S01E02.1080p.WEB.mkv": 520_000,
    "Sample/sample.mkv": 20_000,
    "readme.txt": 100,
}


@pytest.fixture
def seeder(tmp_path):
    content = tmp_path / "seed" / "Show.S01.1080p.WEB"
    for name, size in FILES.items():
        path = content / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(size))
    ct = lt.create_torrent(lt.list_files(str(content)), 16384, flags=lt.create_torrent.v1_only)
    lt.set_piece_hashes(ct, str(content.parent))
    info = lt.torrent_info(lt.bencode(ct.generate()))

    ses = lt.session({**_local(), "alert_mask": 0})
    atp = lt.add_torrent_params()
    atp.ti = info
    atp.save_path = str(content.parent)
    atp.flags |= lt.torrent_flags.seed_mode
    ses.add_torrent(atp)
    deadline = time.time() + 10
    while ses.listen_port() == 0 and time.time() < deadline:
        time.sleep(0.05)
    layout = info.layout()
    index = {layout.file_path(i).replace("\\", "/").split("/", 1)[1]: i
             for i in range(info.num_files())}  # fmt: skip
    yield {
        "session": ses,
        "info_hash": str(info.info_hashes().v1),
        "port": ses.listen_port(),
        "index": index,
        "content": content,
    }
    del ses


class Recorder:
    def __init__(self, replacements=None):
        self.metadata, self.done, self.jobs_done, self.dead = [], [], [], []
        self.resume: dict[str, bytes] = {}
        self.replacements = list(replacements or [])

    def on_metadata(self, job):
        self.metadata.append(job.info_hash)

    def on_target_done(self, job, target):
        self.done.append(target.label)

    def on_job_done(self, job):
        self.jobs_done.append(job.info_hash)

    def on_job_dead(self, job, reason, remaining):
        self.dead.append((job.info_hash, reason))
        out, self.replacements = self.replacements, []
        return out

    def on_resume_data(self, info_hash, data):
        self.resume[info_hash] = data


def _job(seed, library: Path, episode: int, file_idx: int | None, filename: str = "") -> TorrentJob:
    ref = EpisodeRef(1, episode)
    cand = Candidate(seed["info_hash"], file_idx, "1080p", 10, 0, filename=filename,
                     release="Show.S01.1080p.WEB", is_pack=True)  # fmt: skip
    target = FileTarget(
        file_idx=file_idx,
        refs=(ref,),
        final_path=library / "Show" / "Season 01" / f"Show - {ref.code} - Pilot.mkv",
        size=0,
        candidate=cand,
    )
    return TorrentJob(seed["info_hash"], cand.release, (), [target], [("127.0.0.1", seed["port"])])


def _engine(tmp_path: Path, listener, **cfg) -> TorrentEngine:
    config = Config(**{"metadata_timeout": 20, "stall_timeout": 30, **cfg})
    return TorrentEngine(config, tmp_path / "staging", listener, poll_interval=0.1,
                         settings=_local())  # fmt: skip


async def test_downloads_only_the_selected_file_to_its_library_path(tmp_path, seeder):
    library = tmp_path / "library"
    idx = seeder["index"]["Show.S01E02.1080p.WEB.mkv"]
    rec = Recorder()
    engine = _engine(tmp_path, rec)
    engine.add_job(_job(seeder, library, 2, idx, "Show.S01E02.1080p.WEB.mkv"))
    await asyncio.wait_for(engine.run(), 60)
    engine.shutdown()

    final = library / "Show" / "Season 01" / "Show - S01E02 - Pilot.mkv"
    assert final.read_bytes() == (seeder["content"] / "Show.S01E02.1080p.WEB.mkv").read_bytes()
    assert rec.done == ["S01E02"] and rec.jobs_done == [seeder["info_hash"]]
    library_files = sorted(p.name for p in library.rglob("*") if p.is_file())
    assert library_files == ["Show - S01E02 - Pilot.mkv"]  # no .part, no other episodes
    assert not (tmp_path / "staging" / seeder["info_hash"]).exists()


async def test_wrong_file_index_is_corrected_by_episode_code(tmp_path, seeder):
    library = tmp_path / "library"
    wrong = seeder["index"]["readme.txt"]
    rec = Recorder()
    engine = _engine(tmp_path, rec)
    engine.add_job(_job(seeder, library, 1, wrong))
    await asyncio.wait_for(engine.run(), 60)
    engine.shutdown()

    final = library / "Show" / "Season 01" / "Show - S01E01 - Pilot.mkv"
    assert final.read_bytes() == (seeder["content"] / "Show.S01E01.1080p.WEB.mkv").read_bytes()


async def test_dead_torrent_falls_back_to_the_next_candidate(tmp_path, seeder):
    library = tmp_path / "library"
    good = _job(seeder, library, 2, seeder["index"]["Show.S01E02.1080p.WEB.mkv"])
    bogus = _job(seeder, library, 2, 0)
    bogus.info_hash = "ab" * 20
    bogus.peers = []
    rec = Recorder(replacements=[good])
    engine = _engine(tmp_path, rec, metadata_timeout=2)
    engine.add_job(bogus)
    await asyncio.wait_for(engine.run(), 60)
    engine.shutdown()

    assert rec.dead and rec.dead[0][0] == "ab" * 20 and "metadata" in rec.dead[0][1]
    assert (library / "Show" / "Season 01" / "Show - S01E02 - Pilot.mkv").exists()


async def test_shutdown_saves_resume_data_and_resume_finishes(tmp_path, seeder):
    library = tmp_path / "library"
    idx = seeder["index"]["Show.S01E02.1080p.WEB.mkv"]
    rec = Recorder()
    engine = _engine(tmp_path, rec)
    engine.add_job(_job(seeder, library, 2, idx, "Show.S01E02.1080p.WEB.mkv"))

    async def stop_after_metadata():
        while not rec.metadata:
            await asyncio.sleep(0.05)

    task = asyncio.create_task(engine.run())
    await asyncio.wait_for(stop_after_metadata(), 30)
    task.cancel()  # what Ctrl+C does
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seeder["info_hash"] in rec.resume

    rec2 = Recorder()
    engine2 = _engine(tmp_path, rec2)
    engine2.add_job(_job(seeder, library, 2, idx, "Show.S01E02.1080p.WEB.mkv"),
                    rec.resume[seeder["info_hash"]])  # fmt: skip
    for a in engine2.pending:
        a[0].peers = [("127.0.0.1", seeder["port"])]
    await asyncio.wait_for(engine2.run(), 60)
    engine2.shutdown()
    assert (library / "Show" / "Season 01" / "Show - S01E02 - Pilot.mkv").exists()
