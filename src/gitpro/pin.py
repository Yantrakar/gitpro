"""Copy a repo's identity into its own .git directory, for containers that don't see ~/.gitconfig's includes.

VS Code Dev Containers copy ~/.gitconfig into the container but not the files it includes, so the
includeIf identities are lost there. The repo's .git directory is mounted with the workspace, so a
plain [user] file included from .git/config carries the name and email along.
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from . import files, render, resolve
from .config import ConfigError, check

FILE = "git-identities"  # in the repo's common git dir; .git/config includes it by this relative path


def git(repo: str, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode not in ok:
        raise ConfigError(r.stderr.strip() or f"git {' '.join(args)} failed ({r.returncode})")
    return r.stdout.strip()


def _pin_file(repo: str) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / FILE


def _resolved(repo: str, key: str, pin_path: Path, base: Path) -> str:
    """key's value, if it comes from a file we generated (not, say, a hand-written ~/.gitconfig in a container)."""
    entries = git(repo, "config", "-z", "--show-origin", "--get-all", key, ok=(0, 1)).split("\0")
    origin, value, src = "", "", None
    for found_origin, found_value in zip(entries[::2], entries[1::2]):
        found_src = (base / found_origin[len("file:"):]).resolve() if found_origin.startswith("file:") else None
        if found_src == pin_path.resolve():
            continue
        origin, value, src = found_origin, found_value, found_src
    check(value, f"git resolves no {key} for this repo; nothing to pin")
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
    check(not path.is_symlink(), f"{path}: pin file must not be a symlink; replace it with a regular file")
    previous = path.read_bytes() if path.exists() else None
    check(previous is None or render.is_generated(previous.decode()), f"{path} exists and was not written by us")
    base = resolve._need_layout(Path(repo)).base
    name, email = (_resolved(repo, key, path, base) for key in ("user.name", "user.email"))
    included = git(repo, "config", "--local", "--fixed-value", "--get-all", "include.path", FILE, ok=(0, 1)) != ""
    previous_mode = stat.S_IMODE(path.stat().st_mode) if previous is not None else 0o600
    mode = previous_mode & 0o755
    text = (f"{render.HEADER}\n# Re-run 'gitpro pin' after changing the config.\n"
            f"[user]\n{render.INDENT}name = {render.gval(name)}\n{render.INDENT}email = {render.gval(email)}\n")
    files.atomic_write(path, text, mode)
    if not included:
        try:
            git(repo, "config", "--local", "--add", "include.path", FILE)
        except BaseException:
            if previous is None:
                path.unlink()
            else:
                files.atomic_write(path, previous, previous_mode)
            raise
    return name, email, path
