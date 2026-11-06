"""The desktop "app" pieces: the icon, the shortcut command and the home menu loop."""

from __future__ import annotations

import struct
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from stremio_dl import home, shortcut


def test_icon_has_every_size():
    data = (resources.files("stremio_dl") / "assets" / shortcut.ICON_NAME).read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind) == (0, 1)
    sizes = [data[6 + 16 * i] or 256 for i in range(count)]
    assert sizes == [16, 24, 32, 48, 64, 128, 256]
    first_offset = struct.unpack_from("<I", data, 6 + 12)[0]
    assert data[first_offset : first_offset + 8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_create_shortcuts_passes_paths_to_powershell(tmp_path, monkeypatch):
    seen = {}

    def fake_run(args, env, **kwargs):
        seen.update(args=args, env=env)
        out = f"{tmp_path}\\Desktop\\stremio-dl.lnk\r\n{tmp_path}\\Programs\\stremio-dl.lnk\r\n"
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(shortcut, "launcher", lambda: tmp_path / "sdl.exe")
    paths = shortcut.create_shortcuts(run=fake_run)

    assert seen["args"][0] == "powershell.exe" and "-Command" in seen["args"]
    env = seen["env"]
    assert env["SDL_TARGET"] == str(tmp_path / "sdl.exe")
    assert env["SDL_START_MENU"] == "1"
    icon = env["SDL_ICON"]
    assert icon.endswith("stremio-dl.ico") and Path(icon).read_bytes()[:4] == b"\0\0\1\0"
    assert [p.name for p in paths] == ["stremio-dl.lnk", "stremio-dl.lnk"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_powershell_errors_are_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(shortcut, "launcher", lambda: tmp_path / "sdl.exe")

    def failing_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Access denied")

    with pytest.raises(RuntimeError, match="Access denied"):
        shortcut.create_shortcuts(run=failing_run)


class _Scripted:
    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, paused):
        answer = self.answers.pop(0)
        return type("Q", (), {"ask": lambda self: answer})()


def test_home_menu_survives_errors_and_interrupts(monkeypatch):
    menu = _Scripted(["download", "resume", "history", "download", "quit"])
    outcomes = iter([KeyboardInterrupt(), RuntimeError("boom"), None, home.Cancelled()])
    acted = []

    def fake_act(action, cfg):
        acted.append(action)
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(home, "_menu", menu)
    monkeypatch.setattr(home, "_act", fake_act)
    home.run_home()
    assert acted == ["download", "resume", "history", "download"]
    assert menu.answers == []


def test_ctrl_c_at_the_menu_quits(monkeypatch):
    monkeypatch.setattr(home, "_menu", _Scripted([None]))
    monkeypatch.setattr(home, "_act", lambda *a: pytest.fail("nothing should run"))
    home.run_home()


def test_history_and_folders(monkeypatch, cfg, capsys):
    opened = []
    monkeypatch.setattr(home, "_open_folder", lambda p: opened.append(p))
    home._act("history", cfg)
    home._act("tv", cfg)
    home._act("movies", cfg)
    assert [p.name for p in opened] == ["TV", "Movies"]
    assert "No downloads yet." in capsys.readouterr().out
