"""Incident records for `gitpro report` (spec 19, section 8): the last crash and the last denials.

Stdlib only, so the hot hooks can import it. Best effort: a record that cannot be written is dropped
silently, because a failing hook must behave exactly as it did before. A record holds fixed fields
only: never an exception message, a local variable, an argument or a path outside the gitpro package.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__

PROG = "gitpro"
CRASH = "last-crash.json"
DENIALS = "denials.jsonl"
KEEP = 20            # denials kept
MAX_FRAMES = 30
REASONS = ("committer", "author", "no_identity", "pushed_to", "problem", "push")
SOURCES = ("guard", "agent-check")
_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,80}$")
_FUNC_RE = re.compile(r"^[A-Za-z_<][A-Za-z0-9_<>]{0,80}$")
_COMMAND_RE = re.compile(r"^[a-z][a-z0-9 _.-]{0,60}$")
_WORD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_CODE_RE = re.compile(r"^[a-z_]{1,32}$")


def state_dir() -> Path:
    """The same directory as undo.state_dir(): $XDG_STATE_HOME/gitpro or ~/.local/state/gitpro, 0700."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    path = Path(base) / PROG
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())


def _write(path: Path, text: str) -> None:
    """Atomically replace path with text, mode 0600."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _frames(tb: Any) -> "tuple[List[list], int]":
    """(frames inside the gitpro package as [gitpro/<relpath>, line, func], count of the others)."""
    import traceback
    pkg = Path(__file__).resolve().parent
    inside: List[list] = []
    outside = 0
    for frame, line in traceback.walk_tb(tb):
        try:
            rel = Path(frame.f_code.co_filename).resolve().relative_to(pkg)
        except (ValueError, OSError):
            outside += 1
            continue
        name = frame.f_code.co_name
        inside.append([f"{PROG}/{rel.as_posix()}", int(line), name if _FUNC_RE.match(name) else "?"])
    return inside[-MAX_FRAMES:], outside


def record_crash(command: str, exc: BaseException) -> None:
    """Write last-crash.json for an unexpected exception raised while running `command`."""
    try:
        frames, outside = _frames(exc.__traceback__)
        name = type(exc).__qualname__
        rec = {"time": _now(), "version": __version__,
               "command": command if _COMMAND_RE.match(command or "") else "unknown",
               "type": name if _TYPE_RE.match(name) else "Exception",
               "frames": frames, "outside": outside}
        _write(state_dir() / CRASH, json.dumps(rec) + "\n")
    except Exception:   # noqa: BLE001 - recording must never change what the caller does
        pass


def record_denial(source: str, agent: Optional[str], action: str, reason: str,
                  codes: Sequence[str] = ()) -> None:
    """Append one denial to denials.jsonl, keeping the last KEEP."""
    try:
        if source not in SOURCES:
            return
        rec = {"time": _now(), "version": __version__, "source": source,
               "agent": agent if agent and _WORD_RE.match(agent) else None,
               "action": action if _WORD_RE.match(action or "") else "other",
               "reason": reason if reason in REASONS else "problem",
               "codes": [c for c in codes if isinstance(c, str) and _CODE_RE.match(c)]}
        path = state_dir() / DENIALS
        old = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        lines = [ln for ln in old if ln.strip()][-(KEEP - 1):] + [json.dumps(rec)]
        _write(path, "\n".join(lines) + "\n")
    except Exception:   # noqa: BLE001
        pass


def read_crash() -> Optional[Dict[str, Any]]:
    """The parsed last-crash.json, or None. Fields are not validated here; the reader must."""
    try:
        data = json.loads((state_dir() / CRASH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_denials() -> List[Dict[str, Any]]:
    """The parsed records of denials.jsonl, oldest first (unparsable lines skipped)."""
    out: List[Dict[str, Any]] = []
    try:
        text = (state_dir() / DENIALS).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
