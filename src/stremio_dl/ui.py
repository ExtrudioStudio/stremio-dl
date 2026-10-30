"""Rich renderables for search results, title overviews, plans and live progress."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from . import quality as q
from .engine.base import JobView
from .models import SearchResult, Title
from .planner import Plan
from .selection import summarize
from .store import JobRow

console = Console(highlight=False)

_COLUMNS = ["4K", "1440p", "1080p", "720p", "SD"]
_COLUMN_OF = {"2160p": "4K", "1440p": "1440p", "1080p": "1080p", "720p": "720p",
              "480p": "SD", "360p": "SD", q.UNKNOWN: "SD"}  # fmt: skip


def human_size(n: float) -> str:
    if n <= 0:
        return "–"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def human_rate(n: float) -> str:
    return "" if n <= 0 else f"{human_size(n)}/s"


def human_eta(seconds: float) -> str:
    if seconds <= 0 or seconds > 30 * 86400:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def search_table(results: list[SearchResult]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title")
    table.add_column("Years")
    table.add_column("Type")
    table.add_column("IMDb", style="dim")
    for i, r in enumerate(results, 1):
        kind = "TV" if r.type == "series" else "Movie"
        table.add_row(str(i), r.name, r.release_info, kind, r.id)
    return table


def _bucket(counts: Counter) -> Counter:
    out: Counter = Counter()
    for res, n in counts.items():
        out[_COLUMN_OF.get(res, "SD")] += n
    return out


def title_overview(title: Title, matrix: dict[int, Counter] | None = None) -> Group:
    seasons = title.seasons()
    header = Text()
    header.append(title.name, style="bold")
    if title.release_info:
        header.append(f"  ({title.release_info})", style="dim")
    if title.type == "movie":
        return Group(header)
    header.append(f"\n{len(seasons)} seasons · {len(title.episodes)} aired episodes", style="cyan")

    table = Table(box=box.SIMPLE_HEAD, show_edge=False, title_justify="left")
    table.add_column("Season", justify="right")
    table.add_column("Episodes", justify="right")
    buckets = {s: _bucket(c) for s, c in (matrix or {}).items()}
    present = [c for c in _COLUMNS if any(b.get(c) for b in buckets.values())]
    for col in present:
        table.add_column(col, justify="right")
    for season, eps in sorted(seasons.items()):
        cells = []
        for col in present:
            n = buckets.get(season, Counter()).get(col, 0)
            cells.append(f"[green]{n}[/]" if n else "[dim]–[/]")
        table.add_row(str(season), str(len(eps)), *cells)
    parts: list = [header, table]
    if matrix is not None:
        parts.append(Text("Resolution columns: releases found for episode 1 of each season "
                          "(after language/cam filters).", style="dim"))  # fmt: skip
    return Group(*parts)


def movie_resolutions(counts: Counter) -> Text:
    buckets = _bucket(counts)
    text = Text("Available: ")
    shown = [f"{col} ({buckets[col]})" for col in _COLUMNS if buckets.get(col)]
    text.append(
        ", ".join(shown) if shown else "no streams found", style="green" if shown else "red"
    )
    return text


def plan_table(plan: Plan) -> Group:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False)
    table.add_column("Episode" if plan.title.type == "series" else "")
    table.add_column("Release", overflow="ellipsis", no_wrap=True, max_width=58)
    table.add_column("Res")
    table.add_column("Seeds", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Source")
    table.add_column("Note", style="yellow")

    rows = []
    for job in plan.jobs:
        for t in job.targets:
            c = t.candidate
            notes = []
            if c.is_pack:
                notes.append("pack")
            key = t.refs[0] if t.refs else None
            used = plan.used_quality.get(key, plan.quality)
            if used != plan.quality and plan.quality != q.BEST:
                notes.append(f"fallback {used}")
            if c.languages:
                notes.append("/".join(c.languages))
            if c.hdr:
                notes.append(" ".join(c.hdr))
            sort_key = t.refs[0] if t.refs else None
            rows.append((sort_key, (t.label, c.release, c.resolution, str(c.seeders),
                                    human_size(t.size), c.source, ", ".join(notes))))  # fmt: skip
    for key, reason in plan.missing.items():
        rows.append((key, ("movie" if key is None else key.code, f"[red]{reason}[/]",
                           "", "", "", "", "")))  # fmt: skip
    for key, path in plan.existing.items():
        rows.append(
            (
                key,
                (
                    "movie" if key is None else key.code,
                    f"[dim]already in library: {Path(path).name}[/]",
                    "",
                    "",
                    "",
                    "",
                    "",
                ),
            )
        )
    for _, row in sorted(rows, key=lambda r: (r[0] is not None, r[0] or (0, 0))):
        table.add_row(*row)

    summary = Text()
    summary.append(f"{plan.file_count} files", style="bold")
    summary.append(f" from {len(plan.jobs)} torrent{'s' if len(plan.jobs) != 1 else ''}")
    summary.append(f" · {human_size(plan.total_size)}")
    if plan.missing:
        summary.append(f" · {len(plan.missing)} unavailable", style="red")
    if plan.existing:
        summary.append(f" · {len(plan.existing)} already downloaded", style="dim")
    return Group(table, summary)


def selection_line(refs: list, quality: str) -> Text:
    text = Text()
    text.append(f"{len(refs)} episode{'s' if len(refs) != 1 else ''}: ", style="bold")
    text.append(summarize(refs))
    text.append(f" @ {quality}", style="cyan")
    return text


def progress_table(views: list[JobView]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=False)
    table.add_column("")
    table.add_column("File", overflow="ellipsis", no_wrap=True, max_width=52)
    table.add_column("Progress", width=22)
    table.add_column("%", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Speed", justify="right")
    table.add_column("Peers", justify="right")
    table.add_column("State")

    total = done = rate = 0
    styles = {"done": "green", "failed": "red", "queued": "dim", "metadata": "yellow"}
    for view in views:
        for i, t in enumerate(view.targets):
            total += t.size
            done += min(t.done, t.size)
            pct = (t.done / t.size * 100) if t.size else 0.0
            state = t.state if view.state != "seeding" else "seeding"
            if t.state == "metadata":
                state = "finding peers"
            first = i == 0
            table.add_row(
                t.label,
                t.name,
                ProgressBar(total=max(t.size, 1), completed=min(t.done, t.size), width=20),
                f"{pct:.0f}%" if t.size else "",
                human_size(t.size),
                human_rate(view.down_rate) if first else "",
                f"{view.seeds}/{view.peers}" if first and view.state not in ("done", "dead",
                                                                             "queued") else "",
                Text(state, style=styles.get(t.state, "")),
            )  # fmt: skip
        if view.state == "dead" and view.error:
            table.add_row("", Text(f"↳ dropped: {view.error}", style="red"), "", "", "", "", "", "")
        if view.state in ("downloading", "metadata", "seeding"):
            rate += view.down_rate
    eta = human_eta((total - done) / rate) if rate else ""
    pct = done / total * 100 if total else 0
    table.add_row(
        Text("Total", style="bold"), "", ProgressBar(total=max(total, 1), completed=done, width=20),
        f"{pct:.0f}%", human_size(total), human_rate(rate), "", eta,
    )  # fmt: skip
    return table


def status_table(rows: list[JobRow]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False)
    table.add_column("Job", justify="right")
    table.add_column("Title")
    table.add_column("Quality")
    table.add_column("Done", justify="right")
    table.add_column("Failed", justify="right")
    table.add_column("Status")
    table.add_column("Updated", style="dim")
    colors = {"done": "green", "partial": "yellow", "failed": "red", "paused": "cyan",
              "running": "cyan"}  # fmt: skip
    for r in rows:
        total = sum(r.counts.values())
        finished = r.counts.get("done", 0) + r.counts.get("skipped", 0)
        table.add_row(
            str(r.id), r.title, r.quality, f"{finished}/{total}", str(r.counts.get("failed", 0)),
            Text(r.status, style=colors.get(r.status, "")), r.updated.replace("T", " ")[:16],
        )  # fmt: skip
    return table
