"""Command-line entry point: ``sdl``."""

from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Annotated

import tomli_w
import typer

from . import __version__
from .addons.client import AddonError
from .catalog import search
from .config import Config, config_path, data_dir, load_config, log_dir, save_config, set_value
from .home import run_home
from .quality import parse_quality
from .selection import SelectionError, parse_selection, split_spec
from .shortcut import create_shortcuts
from .store import Store
from .ui import console, plan_table, search_table, selection_line, status_table
from .wizard import (
    Cancelled,
    ask_selection,
    confirm_plan,
    download,
    interactive,
    make_plan,
    open_session,
    pick_title,
    resume,
    show_overview,
)

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    help="Download movies and TV shows through Stremio addons. Run without a command for the "
    "home menu.",
)
config_app = typer.Typer(help="Show or change settings.", no_args_is_help=True)
app.add_typer(config_app, name="config")


def _setup_logging(verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir() / "stremio-dl.log", maxBytes=2_000_000,
                                      backupCount=3, encoding="utf-8")  # fmt: skip
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError:
        pass
    if verbose:
        err = logging.StreamHandler(sys.stderr)
        err.setLevel(logging.DEBUG)
        root.addHandler(err)
    for noisy in ("httpx", "httpcore", "rebulk", "guessit"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _run(coro) -> None:
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        console.print("\n[cyan]Paused.[/] Run [bold]sdl resume[/] to continue any downloads.")
        raise typer.Exit(130) from None
    except Cancelled:
        console.print("Cancelled.")
        raise typer.Exit(1) from None
    except (AddonError, SelectionError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None


def _cfg() -> Config:
    try:
        return load_config()
    except Exception as exc:
        console.print(f"[red]Could not read {config_path()}: {exc}[/]")
        raise typer.Exit(1) from None


@app.callback()
def main_callback(
    ctx: typer.Context,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log debug output.")] = False,
    version: Annotated[bool, typer.Option("--version", help="Show the version.")] = False,
) -> None:
    if version:
        console.print(f"stremio-dl {__version__}")
        raise typer.Exit()
    _setup_logging(verbose)
    if ctx.invoked_subcommand is None:
        run_home()


@app.command("search")
def search_cmd(
    query: Annotated[str, typer.Argument(help="Title to look for.")],
    type_: Annotated[str | None, typer.Option("--type", "-t", help="series or movie")] = None,
) -> None:
    """Search the metadata addons."""

    async def go() -> None:
        async with open_session(_cfg()) as sess:
            types = (type_,) if type_ else ("series", "movie")
            results = await search(sess.client, sess.meta_addons, query, types)
            if results:
                console.print(search_table(results))
            else:
                console.print("Nothing found.")

    _run(go())


@app.command("info")
def info_cmd(
    query: Annotated[str, typer.Argument(help="Title or IMDb id (tt…).")],
    type_: Annotated[str | None, typer.Option("--type", "-t", help="series or movie")] = None,
    sample: Annotated[bool, typer.Option(help="Sample available resolutions.")] = True,
) -> None:
    """Show seasons, episode counts and available resolutions."""

    async def go() -> None:
        async with open_session(_cfg()) as sess:
            title = await pick_title(sess, query, type_, first=True, prompt=False)
            if title:
                await show_overview(sess, title, sample=sample)

    _run(go())


@app.command("get")
def get_cmd(
    query: Annotated[str, typer.Argument(help="Title or IMDb id (tt…).")],
    selection: Annotated[
        str | None,
        typer.Argument(help='Episodes and quality, e.g. "s1 eps10 - s2 eps 3 /full hd".'),
    ] = None,
    quality: Annotated[str | None, typer.Option("--quality", "-q", help="e.g. 4k, 1080p")] = None,
    type_: Annotated[str | None, typer.Option("--type", "-t", help="series or movie")] = None,
    first: Annotated[bool, typer.Option("--first", help="Take the top search result.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan only.")] = False,
    prefer_packs: Annotated[
        bool | None, typer.Option("--prefer-packs/--no-prefer-packs", help="Favor season packs.")
    ] = None,
) -> None:
    """Find, plan and download in one go."""
    cfg = _cfg()
    prompt = interactive() and not yes
    if type_ is None and selection and split_spec(selection)[0]:
        type_ = "series"  # "s1e5"-style input only makes sense for a show

    async def go() -> None:
        async with open_session(cfg) as sess:
            title = await pick_title(sess, query, type_, first=first, prompt=prompt)
            if title is None:
                raise typer.Exit(1)
            text = selection
            if text is None:
                if title.type == "series" and not prompt:
                    raise ValueError('Give an episode selection, e.g. "s1e1-5 /1080p".')
                if title.type == "series":
                    await show_overview(sess, title)
                    picked = await ask_selection(sess, title)
                else:
                    picked = parse_selection("", title, cfg.default_quality)
            else:
                picked = parse_selection(text, title, cfg.default_quality)
            if quality:
                picked.quality = parse_quality(quality)
            if title.type == "series":
                console.print(selection_line(picked.refs, picked.quality or cfg.default_quality))
            plan = await make_plan(sess, title, picked, prompt=prompt, prefer_packs=prefer_packs)
            if dry_run:
                console.print(plan_table(plan))
                return
            if not await confirm_plan(sess, plan, prompt=prompt, assume_yes=yes):
                return
        await download(cfg, plan)

    _run(go())


@app.command("status")
def status_cmd(
    active: Annotated[bool, typer.Option("--active", help="Only unfinished jobs.")] = False,
) -> None:
    """List download jobs."""
    store = Store(data_dir() / "state.db")
    try:
        rows = store.jobs(include_finished=not active)
    finally:
        store.close()
    if rows:
        console.print(status_table(rows))
    else:
        console.print("No downloads yet.")


@app.command("resume")
def resume_cmd(
    job_id: Annotated[int | None, typer.Argument(help="Job number (default: all).")] = None,
) -> None:
    """Continue paused or interrupted downloads."""
    _run(resume(_cfg(), job_id))


@app.command("shortcut")
def shortcut_cmd(
    start_menu: Annotated[
        bool, typer.Option("--start-menu/--no-start-menu", help="Also add a Start menu entry.")
    ] = True,
) -> None:
    """Put a stremio-dl icon on the desktop (Windows) that opens the home menu."""
    try:
        paths = create_shortcuts(start_menu=start_menu)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    for path in paths:
        console.print(f"Created [bold]{path}[/]")


@config_app.command("show")
def config_show() -> None:
    """Print the current settings."""
    console.print(f"[dim]{config_path()}[/]")
    console.print(tomli_w.dumps(_cfg().model_dump()), markup=False)


@config_app.command("path")
def config_path_cmd() -> None:
    """Print the config file location."""
    console.print(str(config_path()), markup=False)


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Setting name, e.g. tv_root.")],
    value: Annotated[str, typer.Argument(help="New value. Lists are comma-separated.")],
) -> None:
    """Change one setting."""
    try:
        cfg = set_value(_cfg(), key, value)
    except (KeyError, ValueError) as exc:
        console.print(f"[red]{exc.args[0] if exc.args else exc}[/]")
        raise typer.Exit(1) from None
    save_config(cfg)
    console.print(f"{key} = {getattr(cfg, key)!r}", markup=False)


@config_app.command("reset")
def config_reset() -> None:
    """Restore the default settings."""
    save_config(Config())
    console.print(f"Defaults written to {config_path()}", markup=False)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # emoji, Cyrillic titles
        except (AttributeError, ValueError):
            pass
    app()
