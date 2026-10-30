"""Client for the Stremio addon protocol (manifest, catalog, meta and stream resources).

Protocol reference: https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/protocol.md
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..config import Config
from .cache import JsonCache

log = logging.getLogger(__name__)

TTL_MANIFEST = 24 * 3600
TTL_CATALOG = 6 * 3600
TTL_META = 24 * 3600
TTL_STREAMS = 2 * 3600


class AddonError(Exception):
    pass


class _Retryable(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} from {url}")
        self.status = status


@dataclass
class Addon:
    manifest_url: str
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        url = self.manifest_url.strip()
        if url.startswith("stremio://"):
            url = "https://" + url[len("stremio://") :]
        if url.endswith("/manifest.json"):
            url = url[: -len("/manifest.json")]
        return url.rstrip("/")

    @property
    def name(self) -> str:
        return self.manifest.get("name") or self.base_url

    def supports(self, resource: str, type_: str, id_: str | None = None) -> bool:
        """True if the manifest declares ``resource`` for ``type_`` (and the id prefix)."""
        top_types = self.manifest.get("types") or []
        top_prefixes = self.manifest.get("idPrefixes")
        for entry in self.manifest.get("resources") or []:
            if isinstance(entry, str):
                name, types, prefixes = entry, top_types, top_prefixes
            else:
                name = entry.get("name")
                types = entry.get("types") or top_types
                prefixes = entry.get("idPrefixes", top_prefixes)
            if name != resource or type_ not in types:
                continue
            if id_ is None or not prefixes or any(id_.startswith(p) for p in prefixes):
                return True
        return False

    def search_catalogs(self, type_: str) -> list[str]:
        out = []
        for cat in self.manifest.get("catalogs") or []:
            if cat.get("type") != type_:
                continue
            extras = {e.get("name") for e in cat.get("extra") or []}
            extras |= set(cat.get("extraSupported") or [])
            if "search" in extras:
                out.append(cat["id"])
        return out

    def resource_url(self, resource: str, type_: str, id_: str, extra: str = "") -> str:
        url = f"{self.base_url}/{resource}/{type_}/{quote(id_, safe=':')}"
        if extra:
            url += f"/{extra}"
        return url + ".json"


class AddonClient:
    """Async HTTP access to addons, with retries, a concurrency limit and a disk cache."""

    def __init__(
        self,
        cfg: Config,
        cache: JsonCache | None = None,
        http: httpx.AsyncClient | None = None,
        retry_wait: float = 1.0,
    ):
        self.cfg = cfg
        self.cache = cache
        self._own_http = http is None
        self.http = http or httpx.AsyncClient(
            timeout=cfg.http_timeout,
            headers={"User-Agent": cfg.user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(max(1, cfg.http_concurrency))
        self._retry_wait = retry_wait
        self._addons: dict[str, Addon] = {}

    async def __aenter__(self) -> AddonClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._own_http:
            await self.http.aclose()

    async def get_json(self, url: str, ttl: float, missing_ok: bool = False) -> dict[str, Any]:
        if self.cache is not None:
            cached = self.cache.get(url, ttl)
            if cached is not None:
                return cached
        data = await self._fetch(url, missing_ok)
        if self.cache is not None and data:
            self.cache.set(url, data)
        return data

    async def _fetch(self, url: str, missing_ok: bool) -> dict[str, Any]:
        retrying = AsyncRetrying(
            retry=retry_if_exception_type((_Retryable, httpx.TransportError)),
            wait=wait_exponential_jitter(initial=self._retry_wait, max=20 * self._retry_wait),
            stop=stop_after_attempt(4),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    async with self._sem:
                        log.debug("GET %s", url)
                        resp = await self.http.get(url)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise _Retryable(resp.status_code, url)
                    if resp.status_code == 404 and missing_ok:
                        return {}
                    if resp.status_code == 403:
                        raise AddonError(
                            f"{url} refused the request (HTTP 403). The addon may be blocking "
                            "automated clients or your region; try again later or configure a "
                            "different stream addon with `sdl config set stream_addons ...`."
                        )
                    if resp.status_code >= 400:
                        raise AddonError(f"HTTP {resp.status_code} from {url}")
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise AddonError(f"{url} did not return JSON") from exc
        except _Retryable as exc:
            raise AddonError(f"{exc} (gave up after retries)") from exc
        except httpx.TransportError as exc:
            raise AddonError(f"Could not reach {url}: {exc}") from exc
        raise AssertionError("unreachable")

    async def load(self, manifest_url: str) -> Addon:
        if manifest_url not in self._addons:
            addon = Addon(manifest_url)
            addon.manifest = await self.get_json(f"{addon.base_url}/manifest.json", TTL_MANIFEST)
            self._addons[manifest_url] = addon
        return self._addons[manifest_url]

    async def load_all(self, urls: list[str]) -> list[Addon]:
        results = await asyncio.gather(*(self.load(u) for u in urls), return_exceptions=True)
        addons = []
        for url, result in zip(urls, results, strict=True):
            if isinstance(result, Exception):
                log.warning("Skipping addon %s: %s", url, result)
            else:
                addons.append(result)
        if not addons:
            raise AddonError(f"None of these addons could be loaded: {', '.join(urls)}")
        return addons

    async def catalog_search(
        self, addon: Addon, type_: str, catalog_id: str, query: str
    ) -> list[dict[str, Any]]:
        extra = "search=" + quote(query, safe="")
        url = addon.resource_url("catalog", type_, catalog_id, extra)
        data = await self.get_json(url, TTL_CATALOG, missing_ok=True)
        return data.get("metas") or []

    async def meta(self, addon: Addon, type_: str, id_: str) -> dict[str, Any]:
        data = await self.get_json(addon.resource_url("meta", type_, id_), TTL_META)
        meta = data.get("meta")
        if not meta:
            raise AddonError(f"{addon.name} has no metadata for {id_}")
        return meta

    async def streams(self, addon: Addon, type_: str, id_: str) -> list[dict[str, Any]]:
        url = addon.resource_url("stream", type_, id_)
        data = await self.get_json(url, TTL_STREAMS, missing_ok=True)
        return data.get("streams") or []
