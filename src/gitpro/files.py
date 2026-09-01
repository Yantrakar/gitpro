"""Compare targets with the disk, show the difference, apply it. The only module that writes."""
from __future__ import annotations

import contextlib
import difflib
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .plan import Target
from .render import has_block, is_generated, without_block


@dataclass(frozen=True)
class Change:
    action: str  # "create" | "update" | "chmod" | "remove"
    path: Path
    old: str | None
    new: str | None
    mode: int


def diff(targets: list[Target], stale: list[Path]) -> list[Change]:
    changes = []
    for t in targets:
        path = t.path.resolve() if t.path.is_symlink() else t.path
        old = path.read_text() if path.exists() else None
        cur = path.stat().st_mode & 0o777 if old is not None else None
        mode = t.mode if t.strict or cur is None else cur & 0o755
        if t.text is None:
            if old is not None:
                changes.append(Change("remove", path, old, None, 0))
        elif old != t.text:
            changes.append(Change("create" if old is None else "update", path, old, t.text, mode))
        elif cur != mode:
            changes.append(Change("chmod", path, old, old, mode))
    for path in dict.fromkeys(stale):
        if path.is_file() and not path.is_symlink():
            old = path.read_text()
            if is_generated(old):
                changes.append(Change("remove", path, old, None, 0))
    return changes


def show(changes: list[Change], diffs: bool) -> None:
    if not changes:
        print("up to date: nothing to change")
        return
    for c in changes:
        print(f"  {c.action:7} {c.path}" + (f" -> {c.mode:o}" if c.action == "chmod" else ""))
        if diffs and c.action != "chmod":
            sys.stdout.writelines(difflib.unified_diff(
                (c.old or "").splitlines(True), (c.new or "").splitlines(True), f"a{c.path}", f"b{c.path}"))


def _backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    data = path.read_bytes()
    for n in range(100):
        bak = path.with_name(f"{path.name}.bak.{stamp}" + (f".{n}" if n else ""))
        try:
            fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return bak
    raise OSError(f"no free backup name for {path}")


def _atomic_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _hand_written(text: str) -> list[str]:
    return [ln for ln in without_block(text).splitlines() if ln.strip()]


def _needs_backup(c: Change) -> bool:
    if c.old is None or is_generated(c.old):
        return False
    # Already managed and only our block changed: nothing hand-written is at risk.
    return not (has_block(c.old) and _hand_written(c.old) == _hand_written(c.new or ""))


def apply(changes: list[Change]) -> None:
    for c in changes:
        if c.action == "remove":
            c.path.unlink()
        elif c.action == "chmod":
            os.chmod(c.path, c.mode)
        else:
            if _needs_backup(c):
                print(f"  backup  {_backup(c.path)}")
            _atomic_write(c.path, c.new, c.mode)
