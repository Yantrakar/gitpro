"""Copy a repo's identity into its own .git directory, for containers that don't see ~/.gitconfig's includes.

VS Code Dev Containers copy ~/.gitconfig into the container but not the files it includes, so the
includeIf identities are lost there. The repo's .git directory is mounted with the workspace, so a
plain [user] file included from .git/config carries the name and email along.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import render
from .config import ConfigError, check

FILE = "git-identities"  # in the repo's common git dir; .git/config includes it by this relative path


def git(repo: str, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode not in ok:
        raise ConfigError(r.stderr.strip() or f"git {' '.join(args)} failed ({r.returncode})")
    return r.stdout.strip()


def _pin_file(repo: str) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / FILE


def _resolved(repo: str, key: str) -> str:
    """key's value, if it comes from a file we generated (not, say, a hand-written ~/.gitconfig in a container)."""
    origin, _, value = git(repo, "config", "--show-origin", "--get", key, ok=(0, 1)).partition("\t")
    check(value, f"git resolves no {key} for this repo; nothing to pin")
    src = Path(origin[len("file:"):]) if origin.startswith("file:") else None
    check(src is not None and src.is_file() and render.is_generated(src.read_text()),
          f"{key} comes from {origin}, not from gitpro; run 'gitpro apply', "
          "and run pin on the host, not inside the container")
    return value


def unpin(repo: str) -> bool:
    """Remove the pin; return whether there was one."""
    path = _pin_file(repo)
    # --get-all exits 1 when the value is absent: not pinned.
    had = git(repo, "config", "--local", "--fixed-value", "--get-all", "include.path", FILE, ok=(0, 1)) != ""
    if had:
        git(repo, "config", "--local", "--fixed-value", "--unset-all", "include.path", FILE)
        if not git(repo, "config", "--local", "--get-regexp", r"^include\.", ok=(0, 1)):
            # Older git leaves the emptied [include] header behind; newer git already dropped it (exit 128).
            git(repo, "config", "--local", "--remove-section", "include", ok=(0, 128))
    if path.is_file() and render.is_generated(path.read_text()):
        path.unlink()
        had = True
    return had


def pin(repo: str) -> tuple[str, str, Path]:
    """Write the identity git resolves for repo now (name, email) into the repo; return them."""
    path = _pin_file(repo)
    check(not path.exists() or render.is_generated(path.read_text()), f"{path} exists and was not written by us")
    unpin(repo)  # resolve without a previous pin
    name, email = (_resolved(repo, key) for key in ("user.name", "user.email"))
    path.write_text(f"{render.HEADER}\n# Re-run 'gitpro pin' after changing the config.\n"
                    f"[user]\n{render.INDENT}name = {render.gval(name)}\n{render.INDENT}email = {render.gval(email)}\n")
    git(repo, "config", "--local", "--add", "include.path", FILE)
    return name, email, path
