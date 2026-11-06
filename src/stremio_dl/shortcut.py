"""Windows desktop and Start menu shortcuts that open the stremio-dl home menu."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from .config import data_dir

SHORTCUT_NAME = "stremio-dl"
ICON_NAME = "stremio-dl.ico"

# Values come in through environment variables so paths never need PowerShell quoting.
_POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$shell = New-Object -ComObject WScript.Shell
$folders = @([Environment]::GetFolderPath('Desktop'))
if ($env:SDL_START_MENU -eq '1') { $folders += [Environment]::GetFolderPath('Programs') }
foreach ($folder in $folders) {
    $path = Join-Path $folder ($env:SDL_NAME + '.lnk')
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $env:SDL_TARGET
    $link.WorkingDirectory = $env:SDL_WORKDIR
    $link.IconLocation = $env:SDL_ICON + ',0'
    $link.Description = 'Download movies and TV shows through Stremio addons'
    $link.Save()
    Write-Output $path
}
"""


def launcher() -> Path:
    """The ``sdl.exe`` of the environment this code is running from."""
    exe = Path(sys.executable).with_name("sdl.exe")
    if exe.exists():
        return exe
    found = shutil.which("sdl")
    if found:
        return Path(found)
    raise RuntimeError("Can't find sdl.exe; run this from an installed stremio-dl.")


def install_icon() -> Path:
    """Copy the icon next to the app data so the shortcut survives reinstalls."""
    dest = data_dir() / ICON_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes((resources.files("stremio_dl") / "assets" / ICON_NAME).read_bytes())
    return dest


def create_shortcuts(
    start_menu: bool = True, run: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> list[Path]:
    if sys.platform != "win32":
        raise RuntimeError("Shortcuts are only supported on Windows.")
    env = {
        **os.environ,
        "SDL_NAME": SHORTCUT_NAME,
        "SDL_TARGET": str(launcher()),
        "SDL_WORKDIR": str(Path.home()),
        "SDL_ICON": str(install_icon()),
        "SDL_START_MENU": "1" if start_menu else "0",
    }
    result = run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "PowerShell could not create the shortcut.")
    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
