"""Versioned, bounded persistent-change requests. Approval is deliberately not a request field."""
from __future__ import annotations

import os
import json
import re
from pathlib import Path

from . import config
from .approval_policy import AUTHORITY_SCHEMA
from .operation_contract import DECISIONS, MAX_REQUEST, OperationError

KINDS = {
    "configure": ("config_path", "toml"),
    "regenerate": ("config_path",),
    "repo-identity": ("config_path", "repo"),
    "undo": ("change_id",),
}
REQUIRED = {"configure": ("toml",), "regenerate": (), "repo-identity": ("repo",), "undo": ("change_id",)}
STRING = {"type": "string", "minLength": 1, "maxLength": MAX_REQUEST}
REQUEST_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["version", "kind"],
    "properties": {
        "version": {"type": "integer", "enum": [1]}, "kind": {"type": "string", "enum": list(KINDS)},
        "config_path": {**STRING, "description": "Absolute gitpro TOML path inside your home directory."},
        "repo": {**STRING, "description": "Absolute worktree root; repo-identity only."},
        "toml": {**STRING, "description": "Complete proposed configuration; values must be explicitly supplied."},
        "change_id": {**STRING, "pattern": "^[0-9a-f]{64}$"},
    },
    "oneOf": [
        {"properties": {"kind": {"const": kind}}, "required": list(REQUIRED[kind]),
         "not": {"anyOf": [{"required": [k]} for k in
                          sorted(set(sum(KINDS.values(), ())) - set(fields))]}}
        for kind, fields in KINDS.items()
    ],
}
RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["version", "command", "ok", "decision", "problems"],
    "properties": {
        "version": {"const": 1}, "command": {"enum": ["change propose", "change approve", "change apply",
                                                      "change status", "change recover"]},
        "ok": {"type": "boolean"}, "decision": {"enum": list(DECISIONS)},
        "problems": {"type": "array", "items": {
            "type": "object", "required": ["code", "message", "decision"]}},
        "plan_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "status": {"enum": ["proposed", "no_changes", "approved", "applying", "applied", "rolled_back",
                              "indeterminate", "rejected", "stale"]},
        "request": REQUEST_SCHEMA,
        "changes": {"type": "array", "items": {
            "type": "object", "required": ["path", "action", "before_sha256", "after_sha256", "diff"],
            "properties": {"path": STRING, "action": {"enum": ["create", "update", "chmod", "remove"]},
                           "before_sha256": {"type": ["string", "null"]},
                           "after_sha256": {"type": ["string", "null"]},
                           "diff": {"type": "string"}, "before_mode": {"type": ["integer", "null"]},
                           "after_mode": {"type": ["integer", "null"]}}}},
        "replayed": {"type": "boolean"}, "backup_store": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "approval": {"type": "string"}, "verification": {"type": "string"},
        "authority": AUTHORITY_SCHEMA,
    },
}


def plan_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise OperationError("invalid_plan", "Supply the complete 64-character plan ID.", "needs_input")
    return value


def parse(raw: object) -> dict:
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise OperationError("invalid_request", "Supply a version-1 change request.", "needs_input")
    try:
        size = len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        raise OperationError("invalid_request", "Change requests must contain JSON values.", "needs_input") from None
    if size > MAX_REQUEST:
        raise OperationError("invalid_request", "Change requests are limited to 128 KiB.", "needs_input")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise OperationError("invalid_request", "Use configure, regenerate, repo-identity or undo.", "needs_input")
    if set(raw) - {"version", "kind", *KINDS[kind]}:
        raise OperationError("invalid_request", "Unknown or inapplicable change-request field.", "needs_input")
    for key in REQUIRED[kind]:
        if key not in raw:
            raise OperationError("missing_input", f"Supply {key}; gitpro will not invent its value.", "needs_input")
    out = dict(raw)
    for key, value in raw.items():
        if key in ("version", "kind"):
            continue
        if not isinstance(value, str) or not value.strip() or "\0" in value or len(value.encode()) > MAX_REQUEST:
            raise OperationError("invalid_request", f"Invalid {key}.", "needs_input")
    if kind == "undo":
        out["change_id"] = plan_id(raw["change_id"])
        return out
    home = Path.home().resolve()
    source = Path(raw.get("config_path") or os.environ.get(config.ENV_VAR) or home / config.USER_CONFIG)
    if not source.is_absolute() or source.suffix != ".toml" or home not in source.parents:
        raise OperationError("invalid_config_path", "Use an absolute .toml configuration path inside your home.",
                             "needs_input")
    if ".." in source.parts or config.SCAN_CONTROL_RE.search(str(source)):
        raise OperationError("invalid_config_path", "Use a normalized configuration path without control characters.",
                             "needs_input")
    out["config_path"] = str(source)
    if kind == "repo-identity":
        repo = Path(raw["repo"])
        if not repo.is_absolute() or config.SCAN_CONTROL_RE.search(str(repo)):
            raise OperationError("invalid_repo", "Supply an absolute worktree root.", "needs_input")
        out["repo"] = str(repo.resolve())
    return out


def error(command: str, exc: OperationError) -> dict:
    return {"version": 1, "command": "change " + command, "ok": False, "decision": exc.decision,
            "problems": [exc.problem()]}
