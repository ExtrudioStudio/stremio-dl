from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stremio_dl.config import Config
from stremio_dl.metadata import parse_meta
from stremio_dl.models import Title

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep config, cache and state for each test inside its temp folder."""
    monkeypatch.setenv("SDL_HOME", str(tmp_path / "home"))


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(tv_root=str(tmp_path / "TV"), movie_root=str(tmp_path / "Movies"))


@pytest.fixture
def friends() -> Title:
    meta = load("cinemeta_meta_friends.json")["meta"]
    return parse_meta(meta, "series", now=datetime(2026, 10, 30, tzinfo=UTC))


@pytest.fixture
def friends_streams() -> list[dict]:
    return load("torrentio_streams_friends_s01e10.json")["streams"]
