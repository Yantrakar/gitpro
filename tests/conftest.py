from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from gitpro import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway $HOME; git subprocesses see it too."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in ("GITPRO_CONFIG", "GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a terminal
    return h.resolve()


def write_config(home: Path, text: str, mode: int = 0o600) -> Path:
    path = home / ".config/gitpro.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def run(*argv: str) -> int:
    return cli.main(list(argv))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout.strip()


def repo_with_remote(tmp_path: Path, name: str, url: str | None) -> Path:
    repo = tmp_path / name
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if url:
        git(repo, "remote", "add", "origin", url)
    return repo
