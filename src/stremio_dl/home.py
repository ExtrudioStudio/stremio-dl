"""The home menu: what `sdl` (and the desktop shortcut) opens when run without a command.

It loops until the user picks Quit, so a window opened from the shortcut never closes on
its own after a download, an error or a Ctrl+C.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import questionary
import typer
from rich.rule import Rule

from . import __version__
from .addons.client import AddonError
from .config import Config, config_path, data_dir, load_config, log_dir, save_config
from .selection import SelectionError
from .store import Store
from .ui import console, status_table
from .wizard import Cancelled, resume, wizard

log = logging.getLogger(__name__)


def _load_config() -> Config:
    try:
        return load_config()
    except Exception as exc:
        console.print(f"[red]Could not read {config_path()}: {exc}[/]")
        console.print("[yellow]Using default settings; fix the file under Settings.[/]")
        return Config()


def _paused_jobs() -> int:
    store = Store(data_dir() / "state.db")
    try:
        return len(store.jobs(include_finished=False))
    finally:
        store.close()


def _open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # opens Explorer
    else:
        typer.launch(str(path))


def _edit_settings() -> None:
    path = config_path()
    if not path.exists():
        save_config(Config())
    if sys.platform == "win32":
        subprocess.Popen(["notepad.exe", str(path)])
    else:
        typer.launch(str(path))
    console.print(f"Opened [bold]{path}[/]. Changes apply from your next pick in this menu.")


def _menu(paused: int) -> questionary.Question:
    choices = [
        questionary.Choice("Download a movie or TV show", "download"),
        questionary.Choice(
            f"Resume paused downloads ({paused})",
            "resume",
            disabled=None if paused else "nothing paused",
        ),
        questionary.Choice("Download history", "history"),
        questionary.Choice("Open TV Shows folder", "tv"),
        questionary.Choice("Open Movies folder", "movies"),
        questionary.Choice("Settings", "settings"),
        questionary.Choice("Quit", "quit"),
    ]
    return questionary.select("What would you like to do?", choices=choices)


def _act(action: str, cfg: Config) -> None:
    if action == "download":
        asyncio.run(wizard(cfg))
    elif action == "resume":
        asyncio.run(resume(cfg))
    elif action == "history":
        store = Store(data_dir() / "state.db")
        try:
            rows = store.jobs()
        finally:
            store.close()
        console.print(status_table(rows) if rows else "No downloads yet.")
    elif action == "tv":
        _open_folder(Path(cfg.tv_root))
    elif action == "movies":
        _open_folder(Path(cfg.movie_root))
    elif action == "settings":
        _edit_settings()


def run_home() -> None:
    console.set_window_title("stremio-dl")
    console.print(Rule(f"[bold]stremio-dl[/] {__version__} · movies and TV through Stremio addons"))
    while True:
        cfg = _load_config()
        action = _menu(_paused_jobs()).ask()  # None on Ctrl+C
        if action in (None, "quit"):
            return
        try:
            _act(action, cfg)
        except KeyboardInterrupt:
            console.print("\n[cyan]Paused.[/] Pick “Resume paused downloads” to continue.")
        except Cancelled:
            console.print("Cancelled.")
        except (AddonError, SelectionError, ValueError) as exc:
            console.print(f"[red]{exc}[/]")
        except Exception as exc:
            log.exception("Unexpected error in the home menu")
            console.print(f"[red]Something went wrong: {exc}[/]")
            console.print(f"[dim]Details are in the log: {log_dir() / 'stremio-dl.log'}[/]")
        console.print()
