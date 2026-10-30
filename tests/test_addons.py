from __future__ import annotations

import httpx
import pytest
import respx

from stremio_dl.addons.cache import JsonCache
from stremio_dl.addons.client import Addon, AddonClient, AddonError
from stremio_dl.catalog import search
from stremio_dl.config import Config
from stremio_dl.metadata import fetch_title
from stremio_dl.streams import StreamResolver

from .conftest import load

CINEMETA = "https://v3-cinemeta.strem.io"
TORRENTIO = "https://torrentio.strem.fun/sort=seeders|qualityfilter=cam"


def test_manifest_capabilities():
    cinemeta = Addon(f"{CINEMETA}/manifest.json", load("cinemeta_manifest.json"))
    torrentio = Addon(f"{TORRENTIO}/manifest.json", load("torrentio_manifest.json"))
    assert cinemeta.supports("meta", "series", "tt0108778")
    assert not cinemeta.supports("meta", "series", "kitsu:1")
    assert not cinemeta.supports("stream", "series")
    assert cinemeta.search_catalogs("series") == ["top"]
    assert torrentio.supports("stream", "series", "tt0108778:1:10")
    assert torrentio.base_url == TORRENTIO  # the config path segment is kept
    assert torrentio.resource_url("stream", "series", "tt0108778:1:10") == (
        f"{TORRENTIO}/stream/series/tt0108778:1:10.json"
    )
    assert cinemeta.resource_url("catalog", "series", "top", "search=the%20office") == (
        f"{CINEMETA}/catalog/series/top/search=the%20office.json"
    )


@pytest.fixture
def mocked():
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{CINEMETA}/manifest.json").respond(json=load("cinemeta_manifest.json"))
        router.get(f"{TORRENTIO}/manifest.json").respond(json=load("torrentio_manifest.json"))
        router.get(f"{CINEMETA}/catalog/series/top/search=Friends.json").respond(
            json=load("cinemeta_search_series_friends.json")
        )
        router.get(f"{CINEMETA}/catalog/movie/top/search=Friends.json").respond(
            json=load("cinemeta_search_movie_friends.json")
        )
        router.get(f"{CINEMETA}/meta/series/tt0108778.json").respond(
            json=load("cinemeta_meta_friends.json")
        )
        yield router


async def test_search_and_metadata(mocked, tmp_path):
    cfg = Config(meta_addons=[f"{CINEMETA}/manifest.json"])
    async with AddonClient(cfg, JsonCache(tmp_path)) as client:
        addons = await client.load_all(cfg.meta_addons)
        results = await search(client, addons, "Friends")
        assert results[0].id == "tt0108778" and results[0].type == "series"
        assert {r.type for r in results} == {"series", "movie"}
        title = await fetch_title(client, addons, "series", "tt0108778")
    assert title.name == "Friends" and title.year == 1994
    assert len(title.seasons()) == 10 and len(title.episodes) == 236


async def test_streams_retry_then_succeed_and_404_is_empty(mocked, tmp_path, friends):
    route = mocked.get(f"{TORRENTIO}/stream/series/tt0108778:1:10.json")
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json=load("torrentio_streams_friends_s01e10.json")),
    ]
    mocked.get(f"{TORRENTIO}/stream/series/tt0108778:1:11.json").respond(404)
    cfg = Config(stream_addons=[f"{TORRENTIO}/manifest.json"])
    async with AddonClient(cfg, JsonCache(tmp_path), retry_wait=0.01) as client:
        resolver = StreamResolver(client, await client.load_all(cfg.stream_addons), cfg)
        found = await resolver.all_candidates(friends, [friends.episodes[9].ref,
                                                        friends.episodes[10].ref])  # fmt: skip
    assert route.call_count == 2
    s01e10, s01e11 = found.values()
    assert len(s01e10) == 52  # 58 streams minus 6 foreign-only releases
    assert s01e11 == []


async def test_forbidden_is_reported_without_retrying(tmp_path):
    with respx.mock() as router:
        route = router.get("https://blocked.example/manifest.json").respond(403)
        cfg = Config()
        async with AddonClient(cfg, None, retry_wait=0.01) as client:
            with pytest.raises(AddonError, match="403"):
                await client.load("https://blocked.example/manifest.json")
        assert route.call_count == 1


async def test_cache_serves_repeat_requests(mocked, tmp_path):
    cfg = Config()
    async with AddonClient(cfg, JsonCache(tmp_path)) as client:
        await client.get_json(f"{CINEMETA}/manifest.json", ttl=60)
        await client.get_json(f"{CINEMETA}/manifest.json", ttl=60)
    assert mocked.routes[0].call_count == 1
