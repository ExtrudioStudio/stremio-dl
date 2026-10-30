"""Library paths: naming templates, Windows-safe file names and "already have it" checks."""

from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path

from .config import Config
from .models import EpisodeRef, Title
from .streams import VIDEO_EXTS

MAX_PATH = 240  # stay under Windows' 260-character MAX_PATH with room for ".part"
_INVALID = re.compile(r'[<>"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}  # fmt: skip


def sanitize(name: str, max_len: int = 150) -> str:
    """Make one path component safe on Windows (and everywhere else)."""
    name = unicodedata.normalize("NFC", name)
    name = name.replace(": ", " - ").replace(":", "-")
    name = _INVALID.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:max_len].rstrip(". ")
    if not name:
        return "_"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name


def episode_code(refs: tuple[EpisodeRef, ...]) -> str:
    if not refs:
        return ""
    first, last = refs[0], refs[-1]
    if len(refs) == 1:
        return first.code
    if first.season == last.season:
        return f"{first.code}-E{last.episode:02d}"
    return f"{first.code}-{last.code}"


def _render(template: str, root: Path, fields: dict, ext: str) -> Path:
    parts = [p for p in template.replace("\\", "/").split("/") if p]
    rendered = [p.format(**fields) for p in parts]
    dirs = [sanitize(p) for p in rendered[:-1]]
    filename = rendered[-1]
    stem = filename[: -len(ext)] if ext and filename.endswith(ext) else filename
    stem = re.sub(r"[\s\-–]+$", "", stem)  # "Show - S01E01 - " when the title is empty
    stem = sanitize(stem, max_len=200)

    path = root.joinpath(*dirs, stem + ext)
    overflow = len(str(path)) - MAX_PATH
    if overflow > 0:
        stem = sanitize(stem[: max(20, len(stem) - overflow)])
        path = root.joinpath(*dirs, stem + ext)
    return path


def tv_path(cfg: Config, title: Title, refs: tuple[EpisodeRef, ...], ext: str) -> Path:
    names = []
    for ref in refs:
        ep = title.episode(ref)
        if ep and ep.title and ep.title not in names:
            names.append(ep.title)
    fields = {
        "show": title.name,
        "year": title.year or "",
        "season": refs[0].season,
        "episode": refs[0].episode,
        "code": episode_code(refs),
        "title": " & ".join(names),
        "ext": ext,
    }
    return _render(cfg.tv_template, Path(cfg.tv_root), fields, ext)


def movie_path(cfg: Config, title: Title, ext: str) -> Path:
    fields = {"title": title.name, "year": title.year or "", "ext": ext}
    template = cfg.movie_template if title.year else cfg.movie_template.replace(" ({year})", "")
    return _render(template, Path(cfg.movie_root), fields, ext)


def target_path(cfg: Config, title: Title, refs: tuple[EpisodeRef, ...], ext: str) -> Path:
    return movie_path(cfg, title, ext) if title.type == "movie" else tv_path(cfg, title, refs, ext)


def find_existing(path: Path, refs: tuple[EpisodeRef, ...]) -> Path | None:
    """A finished video already in the target folder for these episodes (or this movie)."""
    if path.exists():
        return path
    folder = path.parent
    if not folder.is_dir():
        return None
    patterns = [
        re.compile(rf"s0*{r.season}\s*e0*{r.episode}(?!\d)", re.I) for r in refs
    ]  # matches S01E05, s1e5, "S01 E05"
    for entry in folder.iterdir():
        if not entry.is_file() or entry.suffix.lower() not in VIDEO_EXTS:
            continue
        if not refs:
            return entry  # a movie folder that already holds a video
        if all(p.search(entry.name) for p in patterns):
            return entry
    return None


def free_space(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free
