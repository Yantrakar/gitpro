"""Deprecated commands, flags and MCP tools (spec 22 §10): they keep their 0.3 behavior through 0.4.x,
are hidden from help, and say once what replaces them. GITPRO_NO_DEPRECATION=1 turns the notice off."""
from __future__ import annotations

import os
import sys
from typing import Optional

REMOVED_IN = "0.5.0"


def silenced() -> bool:
    return os.environ.get("GITPRO_NO_DEPRECATION", "") not in ("", "0")


def field(use: Optional[str]) -> dict:
    """The `deprecated` member of a JSON result; `use` is the replacement, None when nothing replaces it."""
    return {"removed_in": REMOVED_IN, "use": use}


def notice(old: str, use: Optional[str]) -> str:
    rest = f"use: {use}" if use else "nothing replaces it"
    return f"{old} is deprecated and will be removed in {REMOVED_IN}; {rest}"


def warn(old: str, use: Optional[str]) -> None:
    """The stderr line, unless silenced."""
    if not silenced():
        print(f"⚠ {notice(old, use)}", file=sys.stderr)


def describe(use: Optional[str], description: str) -> str:
    """A deprecated MCP tool's description."""
    head = f"Deprecated, removed in {REMOVED_IN}; use {use}." if use else \
        f"Deprecated, removed in {REMOVED_IN}; nothing replaces it."
    return f"{head} {description}"
