"""The --json envelope every command shares: {"ok": ..., "command": ..., ...}."""
from __future__ import annotations

import json
import sys

EXTRA: dict = {}   # keys every envelope of this run gains (cli: "deprecated"); main() clears it


def dumps(obj: dict) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def emit(command: str, obj: dict, *, ok: bool = True) -> None:
    """Print {"ok", "command", **obj} on stdout; an "ok" inside obj wins over the argument."""
    out = {"ok": obj.get("ok", ok), "command": command}
    out.update((k, v) for k, v in obj.items() if k not in ("ok", "command"))
    out.update(EXTRA)
    sys.stdout.write(dumps(out) + "\n")
    sys.stdout.flush()


def error(command: str, message: str) -> None:
    """The error object, on stdout; nothing goes to stderr."""
    emit(command, {"error": message}, ok=False)


def wants_json(args: object) -> bool:
    return bool(getattr(args, "json", False))
