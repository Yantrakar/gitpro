"""Small display and path helpers shared by several modules. Standard library only, so any module (the
hooks included) can import it without a cost or a cycle."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Optional


def plural(n: int, word: str, many: str = "") -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def first_line(text: Optional[str]) -> str:
    return next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")


def tilde(path: object, home: Optional[object] = None) -> str:
    """path with the home directory (the real one unless given) shown as ~ (display only)."""
    p, h = str(path), str(Path.home() if home is None else home)
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def sh_tilde(path: str) -> str:
    """Quote a displayed path for the shell, leaving a leading ~ unquoted so the shell still expands it."""
    return path if path == "~" else "~/" + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)


def expand_home(p: str, home: Path) -> Path:
    return home / p[2:] if p.startswith("~/") else Path(p)


def pub_path(key: Path) -> Path:
    return key.with_name(key.name + ".pub")
