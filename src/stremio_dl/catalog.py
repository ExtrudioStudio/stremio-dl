"""Title search across the configured metadata addons."""

from __future__ import annotations

import asyncio
import logging

from .addons.client import Addon, AddonClient, AddonError
from .models import MediaType, SearchResult

log = logging.getLogger(__name__)


def _rank(results: list[SearchResult], query: str) -> list[SearchResult]:
    q = query.casefold().strip()

    def score(item: tuple[int, SearchResult]) -> tuple[int, int]:
        idx, r = item
        name = r.name.casefold()
        if name == q:
            tier = 0
        elif name.startswith(q):
            tier = 1
        elif q in name:
            tier = 2
        else:
            tier = 3
        return tier, idx  # keep the addon's own relevance order within a tier

    return [r for _, r in sorted(enumerate(results), key=score)]


async def search(
    client: AddonClient,
    addons: list[Addon],
    query: str,
    types: tuple[MediaType, ...] = ("series", "movie"),
    limit: int = 20,
) -> list[SearchResult]:
    calls = []
    for addon in addons:
        for type_ in types:
            for catalog_id in addon.search_catalogs(type_):
                calls.append((addon, type_, client.catalog_search(addon, type_, catalog_id, query)))
    results = await asyncio.gather(*(c for _, _, c in calls), return_exceptions=True)

    seen: set[str] = set()
    found: list[SearchResult] = []
    for (addon, type_, _), metas in zip(calls, results, strict=True):
        if isinstance(metas, Exception):
            log.warning("Search failed on %s (%s): %s", addon.name, type_, metas)
            continue
        for meta in metas:
            mid = meta.get("id") or meta.get("imdb_id")
            if not mid or mid in seen or not meta.get("name"):
                continue
            seen.add(mid)
            found.append(
                SearchResult(
                    id=mid,
                    type=meta.get("type", type_),
                    name=meta["name"],
                    release_info=str(meta.get("releaseInfo") or meta.get("year") or ""),
                    addon=addon.name,
                )
            )
    if not found and all(isinstance(r, Exception) for r in results) and results:
        raise AddonError(f"Search failed: {results[0]}")
    return _rank(found, query)[:limit]
