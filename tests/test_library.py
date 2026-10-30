from __future__ import annotations

from pathlib import Path

import pytest

from stremio_dl.library import MAX_PATH, episode_code, find_existing, sanitize, tv_path
from stremio_dl.models import Episode, EpisodeRef, Title


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("CON", "_CON"),
        ("con.mkv", "_con.mkv"),
        ("Star Wars: Andor", "Star Wars - Andor"),
        ('What? "Really" <yes>|no*', "What Really yesno"),
        ("Trailing dots...", "Trailing dots"),
        ("  spaced   out  ", "spaced out"),
        ("", "_"),
        ("Amélie", "Amélie"),
    ],
)
def test_sanitize(raw, clean):
    assert sanitize(raw) == clean


def test_episode_codes():
    assert episode_code((EpisodeRef(1, 5),)) == "S01E05"
    assert episode_code((EpisodeRef(1, 16), EpisodeRef(1, 17))) == "S01E16-E17"
    assert episode_code((EpisodeRef(1, 24), EpisodeRef(2, 1))) == "S01E24-S02E01"


def test_long_titles_are_truncated_to_fit_max_path(cfg):
    show = Title("tt1", "series", "Show", episodes=[Episode(1, 1, "x" * 400)])
    path = tv_path(cfg, show, (EpisodeRef(1, 1),), ".mkv")
    assert len(str(path)) <= MAX_PATH
    assert path.name.startswith("Show - S01E01 - xxx") and path.suffix == ".mkv"


def test_missing_episode_title_leaves_no_dangling_separator(cfg):
    show = Title("tt1", "series", "Show", episodes=[Episode(2, 3, "")])
    path = tv_path(cfg, show, (EpisodeRef(2, 3),), ".mp4")
    assert path == Path(cfg.tv_root) / "Show" / "Season 02" / "Show - S02E03.mp4"


def test_find_existing_matches_episode_codes(tmp_path):
    folder = tmp_path / "Show" / "Season 01"
    folder.mkdir(parents=True)
    (folder / "show.s01e05.720p.mkv").write_bytes(b"x")
    (folder / "Show - S01E06 - Part.mkv.part").write_bytes(b"x")
    target = folder / "Show - S01E05 - Title.mkv"
    assert find_existing(target, (EpisodeRef(1, 5),)) == folder / "show.s01e05.720p.mkv"
    assert find_existing(folder / "x.mkv", (EpisodeRef(1, 6),)) is None  # .part isn't finished
    assert find_existing(folder / "x.mkv", (EpisodeRef(1, 50),)) is None
