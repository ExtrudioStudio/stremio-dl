<img src="src/stremio_dl/assets/stremio-dl.png" width="96" align="right" alt="">

# stremio-dl

A terminal app that finds movies and TV shows through Stremio addons (Cinemeta for
metadata, Torrentio for streams), picks the best-seeded torrent at the resolution you ask
for, downloads only the files you selected, and writes them straight into your library:

```
TV Shows\Friends\Season 01\Friends - S01E10 - The One with the Monkey.mkv
Movies\Inception (2010)\Inception (2010).mkv
```

## Install

Requires [uv](https://docs.astral.sh/uv/). From this folder:

```powershell
uv sync                 # creates .venv with Python 3.13 and libtorrent
uv run sdl shortcut     # adds a stremio-dl icon to the desktop and the Start menu
```

After that, double-click the **stremio-dl** icon to start. You can also run `uv run sdl`
from this folder, or `uv tool install .` for a global `sdl` command. The shortcut points
at the environment you ran `sdl shortcut` from, so run it again if you move the project or
switch to the global install.

The first run creates `%APPDATA%\stremio-dl\config.toml`. Windows Firewall may ask
whether to allow incoming connections; allowing them gives you more peers, but downloads
work either way.

## Use

The icon (or `sdl` with no command) opens a home menu:

```
? What would you like to do?
 » Download a movie or TV show
   Resume paused downloads (1)
   Download history
   Open TV Shows folder
   Open Movies folder
   Settings
   Quit
```

**Download a movie or TV show** walks you through search → pick → overview → selection →
plan → download. When it finishes, or when you press Ctrl+C to pause, you're back at the
menu; the window only closes when you pick **Quit**. **Settings** opens the config file in
Notepad.

One-shot:

```powershell
sdl get "Friends" "s1 eps10 - s2 eps 3 /full hd"        # asks before starting
sdl get "Friends" "s3 /720p" --yes                       # no confirmation
sdl get "Friends" "s1-s2 /4k" --dry-run                  # show the plan only
sdl get tt1375666 "4k" --type movie                      # a movie, by IMDb id
```

Other commands:

| Command | What it does |
|---|---|
| `sdl search "the office"` | List matching titles |
| `sdl info tt0108778` | Seasons, episode counts and sampled resolutions |
| `sdl status` | Download jobs and their progress |
| `sdl resume [JOB]` | Continue paused/interrupted jobs (Ctrl+C pauses) |
| `sdl shortcut` | Create the desktop (and Start menu) icon |
| `sdl config show` / `set KEY VALUE` / `reset` / `path` | Settings |

### Selection syntax

| You type | You get |
|---|---|
| `s1 eps10 - s2 eps 3 /full hd` | S01E10 through S02E03 (crosses seasons), 1080p |
| `s3` | All of season 3 at `default_quality` |
| `s1-s3 /4k` or `s1-3 /4k` | Seasons 1–3 in 2160p |
| `s2e1-5, s4e2 /720` | S02E01–05 and S04E02 in 720p |
| `1x05`, `season 1 episode 5` | S01E05 |
| `all /best` | Every aired episode, highest resolution available per episode |

Quality words: `4k`/`uhd`, `1440p`/`2k`, `1080p`/`full hd`/`fhd`, `720p`/`hd`, `480p`/`sd`,
`best`. Separate them with `/` or `@`, or just put them at the end (`s1 1080p`).

In Git Bash, an argument that *starts* with `/` (like `"/4k"`) gets rewritten into a
Windows path; use `@4k`, `-q 4k` or plain `4k` there. PowerShell and cmd are fine.

## How streams are chosen

1. Every selected episode is looked up on each stream addon.
2. Unusable offers are dropped:
   - not a video file (`.exe`, `.lnk`, archives and so on)
   - cam/screener releases
   - releases whose audio is only in languages outside `languages` (default `["en"]`)
   - optionally, Dolby-Vision-only releases (`exclude_dv_only`)
3. The rest are filtered to the requested resolution and sorted by seeders. The top one
   wins, and up to `fallbacks_per_episode` more are kept as fallbacks.
4. Episodes whose winners share a torrent (a season pack) are downloaded as one torrent.
   `--prefer-packs` goes further: it consolidates onto a pack that has at least half the
   best seeder count.
5. If a torrent never delivers metadata (`metadata_timeout`) or stalls (`stall_timeout`),
   it is dropped and its episodes move to their next candidate.
6. When an episode has no stream at the requested resolution, `on_missing` decides what
   happens: `ask` (the default), `lower` or `skip`.

Only the wanted files inside a torrent are downloaded. They are written directly to their
library path as `name.mkv.part` and renamed when complete. Episodes already in the library
(matched by `SxxEyy`) are skipped.

## Settings

`sdl config show` lists everything. The ones you're most likely to change:

| Key | Default | |
|---|---|---|
| `tv_root`, `movie_root` | `Videos\TV Shows`, `Videos\Movies` | Library roots |
| `tv_template` | `{show}/Season {season:02}/{show} - {code} - {title}{ext}` | Also `{year}`, `{episode}` |
| `default_quality` | `1080p` | Used when the selection has no quality |
| `languages` | `en` | Comma-separated; `any` disables the filter |
| `stream_addons` | Torrentio | Paste a *configured* manifest URL to keep its options |
| `max_active_torrents` | `3` | |
| `seed_ratio` | `0` | Seed after finishing until this upload ratio |
| `outgoing_interfaces` | (empty) | Bind to a VPN adapter so traffic can't leak |

## Development

```powershell
uv run pytest                      # 90 tests, offline (recorded responses + a local seeder)
uv run ruff check src tests
uv run python tools/make_icon.py   # redraw the icon (standard library only)
```

Layout: `addons/` (protocol client and cache), `catalog.py`, `metadata.py`, `selection.py`,
`quality.py`, `streams.py`, `planner.py`, `library.py`, `engine/torrent.py` (libtorrent),
`store.py` (SQLite at `%LOCALAPPDATA%\stremio-dl\state.db`), `runner.py`, `wizard.py`,
`home.py` (the menu), `shortcut.py`, `cli.py`, `ui.py`.

## A word of caution

Torrent peers can see your IP address, and in many countries downloading copyrighted
material without permission is illegal. Use a VPN if that matters to you, and only
download content you have the right to.
