"""Undo scripts: one shell script per writing run, so `sh <script>` puts back what the run changed."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import List, Optional

from .util import tilde as _tilde

PROG = "gitpro"


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def state_dir() -> Path:
    """$XDG_STATE_HOME/gitpro or ~/.local/state/gitpro, created 0700."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    path = Path(base) / PROG
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def slug(command: str) -> str:
    """The file-name part of a command: its first word, [A-Za-z0-9-] only ("scan --fix" -> "scan")."""
    word = (command.split() or ["gitpro"])[0]
    return re.sub(r"[^A-Za-z0-9-]", "-", word) or "gitpro"


def tilde(path: Path) -> str:
    """path with the home directory shown as ~ (display only)."""
    return _tilde(Path(path))


def write(command: str, lines: List[str], *, what: str, state: Optional[Path] = None) -> Path:
    """Create <state>/<slug>-undo-<YYYYmmdd-HHMMSS>[-n].sh (O_EXCL, 0700) holding `lines`, which the
    caller has shell-quoted. No `set -e`: every line runs even when one fails. Returns the path."""
    state = state or state_dir()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = _stamp()
    text = (f"#!/bin/sh\n# puts back what '{PROG} {command}' changed on {time.strftime('%Y-%m-%d %H:%M')}"
            f"   ({what})\n" + "".join(line + "\n" for line in lines))
    for n in range(1000):
        path = state / f"{slug(command)}-undo-{stamp}{f'-{n}' if n else ''}.sh"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(path, 0o700)  # the umask may have narrowed it
        return path
    raise OSError(f"no free undo script name in {state}")
