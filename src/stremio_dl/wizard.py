"""The search → overview → select → plan → download flow, shared by the wizard and `sdl get`."""

from __future__ import annotations

import sys
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import questionary
from rich.status import Status

from . import quality as q
from .addons.cache import JsonCache
from .addons.client import Addon, AddonClient
from .catalog import search
from .config import Config, cache_dir, data_dir
from .library import free_space
from .metadata import fetch_title
from .models import Key, MediaType, Title
from .planner import Plan, build_plan
from .runner import DownloadRunner
from .selection import Selection, SelectionError, parse_selection
from .store import Store
from .streams import StreamResolver
from .ui import (
    console,
    human_size,
    movie_resolutions,
    plan_table,
    search_table,
    selection_line,
    title_overview,
)

SYNTAX_HINT = (
    "Examples: [cyan]s1[/]  ·  [cyan]s1e5[/]  ·  [cyan]s1 eps10 - s2 eps 3[/]  ·  "
    "[cyan]s2e1-5, s4e2[/]  ·  [cyan]all[/]   then optionally [cyan]/4k[/], [cyan]/full hd[/], "
    "[cyan]/720p[/], [cyan]/best[/]"
)


SEARCH_AGAIN = "search-again"


class Cancelled(Exception):
    """The user backed out of a prompt."""


@dataclass
class Session:
    cfg: Config
    client: AddonClient
    meta_addons: list[Addon]
    resolver: StreamResolver


@asynccontextmanager
async def open_session(cfg: Config):
    async with AddonClient(cfg, JsonCache(cache_dir() / "http")) as client:
        with Status("Loading addons…", console=console):
            meta_addons = await client.load_all(cfg.meta_addons)
            stream_addons = await client.load_all(cfg.stream_addons)
        yield Session(cfg, client, meta_addons, StreamResolver(client, stream_addons, cfg))


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


async def _ask(question: questionary.Question):
    answer = await question.ask_async()
    if answer is None:
        raise Cancelled
    return answer


# -- step 1: find the title ------------------------------------------------------------------


async def pick_title(
    sess: Session,
    query: str,
    type_: MediaType | None = None,
    first: bool = False,
    prompt: bool = True,
) -> Title | None:
    if query.startswith("tt") and query[2:].isdigit():
        types: list[MediaType] = [type_] if type_ else ["series", "movie"]
        for t in types:
            try:
                return await fetch_title(sess.client, sess.meta_addons, t, query,
                                         sess.cfg.include_specials)  # fmt: skip
            except Exception:
                continue
        console.print(f"[red]No metadata found for {query}.[/]")
        return None

    with Status(f"Searching for “{query}”…", console=console):
        results = await search(sess.client, sess.meta_addons, query,
                               (type_,) if type_ else ("series", "movie"))  # fmt: skip
    if not results:
        console.print(f"[yellow]Nothing found for “{query}”.[/]")
        return None
    chosen = results[0]
    if not first and prompt and len(results) > 1:
        choices = [questionary.Choice(r.label, value=r) for r in results]
        choices.append(questionary.Choice("↩  search again", value=SEARCH_AGAIN))
        chosen = await _ask(questionary.select("Which one?", choices=choices))
        if chosen == SEARCH_AGAIN:
            return None
    elif not prompt and not first and len(results) > 1:
        exact = [r for r in results if r.name.casefold() == query.casefold()]
        if len(exact) != 1:
            console.print(search_table(results[:10]))
            console.print("[yellow]Several matches; pass --first or an IMDb id (tt…).[/]")
            return None
        chosen = exact[0]

    with Status(f"Loading {chosen.name}…", console=console):
        return await fetch_title(sess.client, sess.meta_addons, chosen.type, chosen.id,
                                 sess.cfg.include_specials)  # fmt: skip


# -- step 2: overview ------------------------------------------------------------------------


async def show_overview(sess: Session, title: Title, sample: bool = True) -> None:
    if title.type == "movie":
        console.print(title_overview(title))
        with Status("Checking available streams…", console=console):
            cands = await sess.resolver.candidates(title, None)
        console.print(movie_resolutions(Counter(c.resolution for c in cands)))
        return
    if not title.episodes:
        console.print(title_overview(title))
        console.print("[yellow]No aired episodes listed for this show.[/]")
        return
    matrix = None
    if sample:
        seasons = len(title.seasons())
        with Status(f"Sampling available resolutions across {seasons} seasons…", console=console):
            matrix = await sess.resolver.resolution_matrix(title)
    console.print(title_overview(title, matrix))


# -- step 3: selection -----------------------------------------------------------------------


async def ask_selection(sess: Session, title: Title) -> Selection:
    if title.type == "movie":
        text = await _ask(questionary.text(
            "Quality?", default=sess.cfg.default_quality,
            instruction="(4k, 1080p/full hd, 720p, sd, best)",
        ))  # fmt: skip
        return parse_selection(text, title, sess.cfg.default_quality)
    console.print(SYNTAX_HINT)
    while True:
        text = await _ask(questionary.text("What do you want?"))
        try:
            selection = parse_selection(text, title, sess.cfg.default_quality)
        except (SelectionError, ValueError) as exc:
            console.print(f"[red]{exc}[/]")
            continue
        console.print(selection_line(selection.refs, selection.quality or ""))
        return selection


# -- step 4: streams and plan ----------------------------------------------------------------


async def make_plan(
    sess: Session,
    title: Title,
    selection: Selection,
    prompt: bool = True,
    prefer_packs: bool | None = None,
) -> Plan:
    cfg = sess.cfg
    requested = selection.quality or cfg.default_quality
    keys: list[Key] = list(selection.refs) if title.type == "series" else [None]
    done = 0
    with Status("Finding streams…", console=console) as status:

        def progress(_key: Key) -> None:
            nonlocal done
            done += 1
            status.update(f"Finding streams… {done}/{len(keys)}")

        candidates = await sess.resolver.all_candidates(title, keys, on_progress=progress)

    packs = cfg.prefer_packs if prefer_packs is None else prefer_packs
    on_missing = "skip" if cfg.on_missing == "ask" else cfg.on_missing
    plan = build_plan(title, keys, candidates, cfg, requested, on_missing, packs)

    lower_available = [
        k for k, reason in plan.missing.items() if reason.startswith(f"no {requested} streams")
    ]
    if cfg.on_missing == "ask" and lower_available and requested != q.BEST:
        n = len(lower_available)
        what = "This title has" if title.type == "movie" else f"{n} episode{'s' if n > 1 else ''}"
        if prompt:
            use_lower = await _ask(questionary.select(
                f"{what} no {requested} streams. What should I do?",
                choices=[
                    questionary.Choice("Use the next lower quality for them", value=True),
                    questionary.Choice("Skip them", value=False),
                ],
            ))  # fmt: skip
        else:
            use_lower = False
            console.print(
                f"[yellow]{what} no {requested} streams; skipping them "
                '(set on_missing = "lower" to fall back automatically).[/]'
            )
        if use_lower:
            plan = build_plan(title, keys, candidates, cfg, requested, "lower", packs)
    return plan


def _library_root(cfg: Config, title: Title) -> Path:
    return Path(cfg.movie_root if title.type == "movie" else cfg.tv_root)


async def confirm_plan(sess: Session, plan: Plan, prompt: bool, assume_yes: bool) -> bool:
    console.print(plan_table(plan))
    if not plan.jobs:
        console.print("[yellow]Nothing to download.[/]")
        return False
    root = _library_root(sess.cfg, plan.title)
    needed = int(plan.total_size * 1.05)
    free = free_space(root)
    console.print(f"[dim]Library: {root}  ·  free space {human_size(free)}[/]")
    if needed > free:
        console.print(f"[red]Not enough disk space: need about {human_size(needed)}.[/]")
        if not prompt:
            return assume_yes
    if assume_yes:
        return True
    if not prompt:
        console.print("[yellow]Pass --yes to start without confirmation.[/]")
        return False
    return await _ask(questionary.confirm(
        f"Download {plan.file_count} files ({human_size(plan.total_size)})?", default=True
    ))  # fmt: skip


# -- step 5/6: download ----------------------------------------------------------------------


async def download(cfg: Config, plan: Plan) -> str:
    store = Store(data_dir() / "state.db")
    try:
        job_id = store.create_job(plan)
        console.print(f"[dim]Job #{job_id} · Ctrl+C pauses; `sdl resume` continues.[/]")
        runner = DownloadRunner(cfg, store, job_id, plan)
        status = await runner.run(store.open_torrents(job_id))
        report(store, job_id, status)
        return status
    finally:
        store.close()


async def resume(cfg: Config, job_id: int | None = None) -> None:
    store = Store(data_dir() / "state.db")
    try:
        rows = store.jobs(include_finished=False)
        if job_id is not None:
            rows = [r for r in rows if r.id == job_id]
        if not rows:
            console.print("Nothing to resume.")
            return
        for row in reversed(rows):
            torrents = store.open_torrents(row.id)
            if not torrents:
                store.finish_job(row.id)
                continue
            console.print(f"[bold]Resuming job #{row.id}: {row.title}[/]")
            store.set_job_status(row.id, "running")
            runner = DownloadRunner(cfg, store, row.id, store.load_plan(row.id))
            status = await runner.run(torrents)
            report(store, row.id, status)
    finally:
        store.close()


def report(store: Store, job_id: int, status: str) -> None:
    row = next((r for r in store.jobs() if r.id == job_id), None)
    if row is None:
        return
    done = row.counts.get("done", 0)
    failed = row.counts.get("failed", 0)
    color = {"done": "green", "partial": "yellow", "failed": "red"}.get(status, "cyan")
    console.print(f"[{color}]Job #{job_id} {status}[/]: {done} downloaded, {failed} failed.")


# -- the interactive wizard ------------------------------------------------------------------


async def wizard(cfg: Config, query: str | None = None) -> None:
    async with open_session(cfg) as sess:
        title = None
        while title is None:
            if not query:
                query = await _ask(questionary.text("What do you want to download?"))
                if not query.strip():
                    query = None
                    continue
            title = await pick_title(sess, query.strip())
            query = None
        await show_overview(sess, title)
        selection = await ask_selection(sess, title)
        plan = await make_plan(sess, title, selection)
        if not await confirm_plan(sess, plan, prompt=True, assume_yes=False):
            return
    await download(cfg, plan)
