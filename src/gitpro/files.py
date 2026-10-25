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
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # plan and render load lazily, so modules that only need atomic_write stay cheap
    from .plan import Target


def is_generated(text: str) -> bool:
    from .render import is_generated as _is_generated
    return _is_generated(text)


def has_block(text: str) -> bool:
    from .render import has_block as _has_block
    return _has_block(text)


def without_block(text: str) -> str:
    from .render import without_block as _without_block
    return _without_block(text)


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


def _backup(path: Path, data: bytes | None = None, into: Path | None = None) -> Path:
    """A copy of path (O_EXCL, 0600) named <name>.bak.<stamp>, beside it or in the folder into."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if data is None:
        data = path.read_bytes()
    for n in range(100):
        name = f"{path.name}.bak.{stamp}" + (f".{n}" if n else "")
        bak = into / name if into is not None else path.with_name(name)
        try:
            fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return bak
    raise OSError(f"no free backup name for {path}")


def atomic_write(path: Path, text: str | bytes, mode: int, *,
                 follow_symlinks: bool = True, overwrite: bool = True) -> None:
    """Replace path's content atomically: temp file in the same directory, fsync, rename, fsync of the directory.
    A str is written as UTF-8.
    By default symlinks are followed. Callers can replace the entry itself instead,
    or require exclusive creation without overwriting any existing entry."""
    path = Path(os.path.realpath(path)) if overwrite and follow_symlinks and Path(path).is_symlink() else Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with (os.fdopen(fd, "wb") if isinstance(text, bytes) else os.fdopen(fd, "w", encoding="utf-8")) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if overwrite:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)
            os.unlink(tmp)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):   # so the rename survives a power loss; not every platform can fsync a directory
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


_atomic_write = atomic_write  # the pre-0.2 name; kept for one release


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
            atomic_write(c.path, c.new, c.mode)
