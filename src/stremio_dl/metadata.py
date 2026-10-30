"""Turn addon metadata into a :class:`Title` with the aired episodes in order."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from .addons.client import Addon, AddonClient, AddonError
from .models import Episode, MediaType, Title

_YEAR = re.compile(r"(\d{4})")


def _parse_date(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_meta(
    meta: dict[str, Any],
    type_: MediaType,
    include_specials: bool = False,
    now: datetime | None = None,
) -> Title:
    now = now or datetime.now(UTC)
    release_info = str(meta.get("releaseInfo") or meta.get("year") or "")
    year_match = _YEAR.search(release_info) or _YEAR.search(str(meta.get("released") or ""))
    title = Title(
        id=meta.get("imdb_id") or meta["id"],
        type=type_,
        name=meta.get("name") or meta["id"],
        year=int(year_match.group(1)) if year_match else None,
        release_info=release_info,
    )
    if type_ != "series":
        return title

    episodes: dict[tuple[int, int], Episode] = {}
    for video in meta.get("videos") or []:
        season = video.get("season")
        number = video.get("episode", video.get("number"))
        if season is None or number is None:
            continue
        season, number = int(season), int(number)
        if season == 0 and not include_specials:
            continue
        released = _parse_date(video.get("released") or video.get("firstAired"))
        if released and released > now:
            continue  # not aired yet: there won't be streams for it
        name = (video.get("name") or video.get("title") or "").strip()
        episodes.setdefault((season, number), Episode(season, number, name, released))
    title.episodes = [episodes[k] for k in sorted(episodes)]
    return title


def _meta_addon(addons: list[Addon], type_: str, id_: str) -> Addon:
    for addon in addons:
        if addon.supports("meta", type_, id_):
            return addon
    raise AddonError(f"No configured metadata addon provides {type_} metadata for {id_}")


async def fetch_title(
    client: AddonClient,
    addons: list[Addon],
    type_: MediaType,
    id_: str,
    include_specials: bool = False,
) -> Title:
    addon = _meta_addon(addons, type_, id_)
    meta = await client.meta(addon, type_, id_)
    return parse_meta(meta, type_, include_specials=include_specials)
