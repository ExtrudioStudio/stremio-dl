"""User configuration and app directories.

Config lives in ``%APPDATA%\\stremio-dl\\config.toml``; state, cache and logs in
``%LOCALAPPDATA%\\stremio-dl``. Set ``SDL_HOME`` to put everything under one folder
(used by the tests).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

import platformdirs
import tomli_w
from pydantic import BaseModel, ValidationError

APP_NAME = "stremio-dl"
CINEMETA = "https://v3-cinemeta.strem.io/manifest.json"
TORRENTIO = "https://torrentio.strem.fun/manifest.json"


def _home_override() -> Path | None:
    value = os.environ.get("SDL_HOME")
    return Path(value) if value else None


def config_dir() -> Path:
    if home := _home_override():
        return home / "config"
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True))


def data_dir() -> Path:
    if home := _home_override():
        return home / "data"
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def cache_dir() -> Path:
    if home := _home_override():
        return home / "cache"
    return Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False))


def log_dir() -> Path:
    if home := _home_override():
        return home / "logs"
    return Path(platformdirs.user_log_dir(APP_NAME, appauthor=False))


def config_path() -> Path:
    return config_dir() / "config.toml"


def _videos_dir() -> Path:
    return Path(platformdirs.user_videos_dir())


class Config(BaseModel):
    # Addons: paste a *configured* manifest URL to keep its options (e.g. a Torrentio
    # URL with "sort=seeders|qualityfilter=cam" in the path).
    meta_addons: list[str] = [CINEMETA]
    stream_addons: list[str] = [TORRENTIO]

    # Library layout
    tv_root: str = str(_videos_dir() / "TV Shows")
    movie_root: str = str(_videos_dir() / "Movies")
    tv_template: str = "{show}/Season {season:02}/{show} - {code} - {title}{ext}"
    movie_template: str = "{title} ({year})/{title} ({year}){ext}"

    # Stream selection
    default_quality: str = "1080p"
    languages: list[str] = ["en"]  # audio languages to accept; ["any"] disables the filter
    on_missing: Literal["ask", "lower", "skip"] = "ask"
    exclude_dv_only: bool = False
    include_specials: bool = False
    prefer_packs: bool = False
    fallbacks_per_episode: int = 5

    # BitTorrent
    max_active_torrents: int = 3
    listen_interfaces: str = "0.0.0.0:6881,[::]:6881"
    outgoing_interfaces: str = ""  # e.g. the VPN adapter name, to keep traffic on the VPN
    seed_ratio: float = 0.0  # 0 = stop as soon as the wanted files are complete
    metadata_timeout: int = 120
    stall_timeout: int = 180

    # HTTP
    http_concurrency: int = 4
    http_timeout: float = 20.0
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) stremio-dl/0.1"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        cfg = Config()
        save_config(cfg, path)
        return cfg
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return Config.model_validate(data)


def save_config(cfg: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(cfg.model_dump()), encoding="utf-8")
    return path


def _coerce(field: str, raw: str) -> Any:
    """Turn a command-line string into the type the field expects."""
    annotation = Config.model_fields[field].annotation
    if annotation == list[str]:
        return [part.strip() for part in raw.split(",") if part.strip()]
    if annotation is bool:
        if raw.lower() in {"1", "true", "yes", "on"}:
            return True
        if raw.lower() in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{field} expects true/false, got {raw!r}")
    return raw


def set_value(cfg: Config, field: str, raw: str) -> Config:
    if field not in Config.model_fields:
        known = ", ".join(Config.model_fields)
        raise KeyError(f"Unknown setting {field!r}. Known settings: {known}")
    data = cfg.model_dump()
    data[field] = _coerce(field, raw)
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ValueError(exc.errors()[0]["msg"]) from exc
