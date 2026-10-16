"""Read and edit JSON / JSONC settings files with a preview, a backup and an undo script (10, 11, 13)."""
from __future__ import annotations

import copy
import difflib
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple, Union

from . import files, undo
from .config import ConfigError


@dataclass(frozen=True)
class Result:
    path: Path
    changed: bool
    diff: str
    backup: Optional[Path]
    undo: Optional[Path]
    text: str  # what is (or would be) on disk after the edit


def strip_jsonc(text: str) -> Tuple[str, bool]:
    """(text without // and /* */ comments outside strings and without trailing commas, had comments)."""
    out, i, n, comments = [], 0, len(text), False
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            comments = True
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            comments = True
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return _drop_trailing_commas("".join(out)), comments


def _drop_trailing_commas(text: str) -> str:
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if c == ",":
            m = re.compile(r"\s*[}\]]").match(text, i + 1)
            if m:
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def read(path: Path, *, jsonc: bool = False) -> Tuple[object, str]:
    """(parsed object, original text, line endings kept). Raises ConfigError("<path>: cannot parse (<error>)")."""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    return _parse(path, text, jsonc)[0], text


def _parse(path: Path, text: str, jsonc: bool) -> Tuple[object, bool]:
    src, comments = strip_jsonc(text) if jsonc else (text, False)
    try:
        return json.loads(src), comments
    except ValueError as e:
        raise ConfigError(f"{path}: cannot parse ({e})") from None


def detect_indent(text: str) -> Union[int, str]:
    """The file's own indent when it is 2 or 4 spaces or a tab, else 2."""
    m = re.search(r"[{\[][ \t]*\r?\n([ \t]+)\S", text)
    if m:
        ws = m.group(1)
        if ws == "\t":
            return "\t"
        if ws in ("  ", "    "):
            return len(ws)
    return 2


def _mode_for(path: Path, old_mode: Optional[int]) -> int:
    home = Path.home()
    real = Path(os.path.realpath(path))
    for private in (home / ".ssh", home / ".config/gh"):
        if private in real.parents or private in Path(path).parents:
            return 0o600
    return old_mode if old_mode is not None else 0o644


def _backup(path: Path, text: str) -> Path:
    """A 0600 copy in <state>/backup-<ts>/ (never next to the file: it may be in a work tree)."""
    d = undo.state_dir() / f"backup-{undo._stamp()}"
    d.mkdir(mode=0o700, exist_ok=True)
    for n in range(1000):
        bak = d / (path.name + (f".{n}" if n else ""))
        try:
            fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)  # the original bytes: CRLF stays CRLF, so the undo restores the file exactly
        return bak
    raise OSError(f"no free backup name in {d}")


def _current(path: Path) -> Optional[str]:
    """The file's text with its own line endings (newline=""), None when it does not exist."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _discard(*paths: Optional[Path]) -> None:
    """Remove a backup and undo script whose write failed (and the backup's directory once empty)."""
    for p in paths:
        if p is None:
            continue
        try:
            p.unlink()
            if p.parent.name.startswith("backup-"):
                p.parent.rmdir()
        except OSError:
            pass


def edit_json(path: Path, mutate: Callable[[object], object], *, command: str, jsonc: bool = False,
              create: Optional[object] = None, confirm: Callable[[str], bool], indent: int = 2,
              mkdir: bool = False, snippet: Optional[str] = None) -> Result:
    """Apply mutate to the file's JSON; show the diff to confirm; back up, write an undo script, then
    replace the file atomically. `indent` is used for new files; existing files keep their own indent.
    `snippet` is what the caller would have the user add by hand when a JSONC file has comments."""
    path = Path(path)
    old = _current(path)
    if old is None:
        if create is None:
            raise ConfigError(f"{path}: not found")
        if not path.parent.is_dir() and not mkdir:
            raise ConfigError(f"{path.parent}: no such directory")
        obj, comments, width, old_mode = create, False, indent, None
    else:
        obj, comments = _parse(path, old, jsonc)
        width = detect_indent(old)
        old_mode = path.stat().st_mode & 0o777
    new_obj = mutate(copy.deepcopy(obj))
    text = json.dumps(new_obj, indent=width, ensure_ascii=False) + "\n"
    diff = "".join(difflib.unified_diff((old or "").replace("\r\n", "\n").splitlines(True), text.splitlines(True),
                                        f"a{path}", f"b{path}"))
    if old is not None and "\r\n" in old:
        text = text.replace("\n", "\r\n")  # keep a CRLF file CRLF (json.dumps escapes newlines in strings)
    if old is not None and new_obj == obj:
        return Result(path, False, "", None, None, old)
    if comments:
        raise ConfigError(f"{path} has comments; gitpro cannot edit it without losing them"
                          f" — add this by hand: {snippet or diff}")
    if not confirm(diff):
        return Result(path, False, diff, None, None, text)
    if _current(path) != old:
        raise ConfigError(f"{path} changed while gitpro was running; nothing written")
    backup = _backup(path, old) if old is not None else None
    made = None
    target = shlex.quote(str(path))
    if backup:
        # The backup is 0600; the undo restores the file's own mode, not the backup's.
        lines = [f"cp {shlex.quote(str(backup))} {target}", f"chmod {old_mode:04o} {target}"]
    else:
        # A file gitpro created is removed only while it holds what gitpro wrote: another step (e.g. the
        # Gemini MCP entry after its hook) or the user may have added to it since.
        made = _backup(path, text)
        lines = [f'if [ ! -e {target} ] || [ "$(cat {shlex.quote(str(made))})" = "$(cat {target})" ]; then '
                 f"rm -f {target}; else "
                 + shlex.quote(f"gitpro: kept {undo.tilde(path)}: it changed after gitpro created it; "
                               "remove gitpro's entry by hand").join(["echo ", " >&2"]) + "; fi"]
    script = undo.write(command, lines, what=f"edited {undo.tilde(path)}")
    if old is None and mkdir:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        files.atomic_write(path, text, _mode_for(path, old_mode))
    except BaseException:
        _discard(backup, made, script)  # nothing changed: leave no backup or undo script behind
        raise
    return Result(path, True, diff, backup, script, text)
