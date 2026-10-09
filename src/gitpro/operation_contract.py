"""Version 1 of the agent operation contract; no shell commands or arbitrary environment input."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

VERSION = 1
MAX_REQUEST = 131072
DECISIONS = ("ready", "blocked", "needs_input", "needs_approval", "indeterminate")
FIELDS = {
    "commit": ("message", "author_name", "author_email"),
    "push": ("remote", "branch"),
    "pr-create": ("remote", "base", "head", "title", "body", "draft"),
}
REQUIRED = {
    "commit": ("message",),
    "push": ("remote", "branch"),
    "pr-create": ("remote", "base", "head", "title", "body"),
}
TEXT = {"type": "string", "minLength": 1, "maxLength": 65536}
REQUEST_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["version", "operation", "repo"],
    "properties": {
        "version": {"type": "integer", "enum": [VERSION]},
        "operation": {"type": "string", "enum": list(FIELDS)},
        "repo": {**TEXT, "description": "Absolute path to a worktree; never inferred from the server cwd."},
        **{key: dict(TEXT) for key in sorted(set(sum(FIELDS.values(), ()))) if key not in ("draft", "body")},
        "body": {"type": "string", "maxLength": 65536}, "draft": {"type": "boolean"},
    },
    "oneOf": [
        {"properties": {"operation": {"const": op}},
         "required": list(REQUIRED[op]),
         "not": {"anyOf": [{"required": [key]} for key in sorted(set(sum(FIELDS.values(), ())) - set(fields))]}}
        for op, fields in FIELDS.items()
    ],
    "dependentRequired": {"author_name": ["author_email"], "author_email": ["author_name"]},
}
RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["version", "command", "ok", "decision", "problems"],
    "properties": {
        "version": {"const": VERSION}, "command": {"enum": ["preflight", "run", "operation-status"]},
        "ok": {"type": "boolean"}, "decision": {"enum": list(DECISIONS)},
        "problems": {"type": "array", "items": {
            "type": "object", "required": ["code", "message", "decision"],
            "properties": {"code": TEXT, "message": TEXT, "decision": {"enum": list(DECISIONS)},
                           "action": {"type": "object", "required": ["kind", "argv"],
                                      "properties": {"kind": {"enum": ["inspect", "request_approval"]},
                                                     "argv": {"type": ["array", "null"],
                                                              "items": {"type": "string"}}}}},
        }},
        "operation": {"enum": [*FIELDS, None]}, "repo": {"type": ["string", "null"]},
        "plan_token": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
        "status": {"enum": ["succeeded", "failed", "blocked", "indeterminate"]},
        "replayed": {"type": "boolean"}, "idempotency_key": {"type": "string"},
        "outcome": {"type": "object", "additionalProperties": False,
                    "properties": {"commit": TEXT, "tree": TEXT, "remote_oid": TEXT, "pr_url": TEXT,
                                   "already_present": {"type": "boolean"}}},
        "checks": {"type": "array", "items": {
            "type": "object", "required": ["code", "status", "message"],
            "properties": {"code": TEXT, "status": {"enum": ["pass", "fail", "unknown", "skip"]},
                           "message": TEXT}}},
        "identity": {"type": "object", "required": ["id", "name", "email", "sign", "ssh_key", "account"],
                     "properties": {"id": TEXT, "name": {"type": ["string", "null"]},
                                    "email": {"type": ["string", "null"]}, "sign": {"type": "boolean"},
                                    "ssh_key": {"type": ["string", "null"]},
                                    "account": {"type": ["object", "null"],
                                                "required": ["host", "cli", "login"],
                                                "properties": {"host": TEXT, "cli": {"const": "gh"},
                                                               "login": TEXT}}}},
        "destination": {"type": ["string", "null"]}, "head": {"type": ["string", "null"]},
        "effects": {"type": "object", "required": ["persistent_configuration", "network", "child_identity_isolated"],
                    "properties": {"persistent_configuration": {"const": False}, "network": {"type": "boolean"},
                                   "child_identity_isolated": {"const": True}}},
        "authorization": TEXT,
    },
}


class OperationError(Exception):
    def __init__(self, code: str, message: str, decision: str = "indeterminate") -> None:
        super().__init__(message)
        self.code, self.decision = code, decision

    def problem(self) -> dict:
        return {"code": self.code, "message": str(self), "decision": self.decision}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, default=str).encode()).hexdigest()


def unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise OperationError("invalid_request", "Duplicate JSON member.", "needs_input")
        out[key] = value
    return out


def decode(text: str, *, limit: int = MAX_REQUEST) -> object:
    if len(text.encode("utf-8")) > limit:
        raise OperationError("invalid_request", f"Request exceeds {limit // 1024} KiB.", "needs_input")
    try:
        return json.loads(text, object_pairs_hook=unique_members)
    except (ValueError, RecursionError):
        raise OperationError("invalid_request", "Request must be a JSON object.", "needs_input") from None


@dataclass(frozen=True)
class Request:
    operation: str
    repo: Path
    values: dict

    @classmethod
    def parse(cls, raw: object) -> Request:
        if not isinstance(raw, dict):
            raise OperationError("invalid_request", "Request must be an object.", "needs_input")
        if type(raw.get("version")) is not int or raw["version"] != VERSION:
            raise OperationError("unsupported_version", "Request version must be 1.", "needs_input")
        op = raw.get("operation")
        if not isinstance(op, str) or op not in FIELDS:
            raise OperationError("unsupported_operation", "Use commit, push or pr-create.", "needs_input")
        if set(raw) - {"version", "operation", "repo", *FIELDS[op]}:
            raise OperationError("invalid_request", "Unknown or inapplicable request field.", "needs_input")
        for key in ("repo", *REQUIRED[op]):
            if key not in raw:
                raise OperationError("missing_input", f"Supply {key}.", "needs_input")
        for key, value in raw.items():
            if key in ("version", "operation"):
                continue
            if key == "draft":
                if type(value) is not bool:
                    raise OperationError("invalid_request", "draft must be boolean.", "needs_input")
            elif not isinstance(value, str) or "\0" in value or len(value) > 65536 or (not value.strip() and key != "body"):
                raise OperationError("invalid_request", f"Invalid {key}.", "needs_input")
        if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST:
            raise OperationError("invalid_request", "Request exceeds 128 KiB.", "needs_input")
        if ("author_name" in raw) != ("author_email" in raw):
            raise OperationError("missing_input", "Supply both author_name and author_email.", "needs_input")
        for key in ("author_name", "author_email", "remote", "branch", "base", "head", "title"):
            if key in raw and (any(ord(c) < 32 or ord(c) == 127 for c in raw[key]) or
                               (key != "title" and raw[key].startswith("-"))):
                raise OperationError("invalid_request", f"Invalid {key}.", "needs_input")
        repo = Path(raw["repo"])
        if not repo.is_absolute():
            raise OperationError("invalid_request", "repo must be an absolute path.", "needs_input")
        values = dict(raw, repo=str(repo.resolve()))
        if op == "pr-create":
            values.setdefault("draft", False)
        return cls(op, repo.resolve(), values)

    @property
    def fingerprint(self) -> str:
        return digest(self.values)


def request_key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise OperationError("invalid_key", "Use a 1-128 character alphanumeric idempotency key (._:- allowed).",
                             "needs_input")
    return value


def failure(command: str, error: OperationError, request: Optional[Request] = None) -> dict:
    return {"version": VERSION, "command": command, "ok": False, "decision": error.decision,
            "operation": request.operation if request else None, "repo": str(request.repo) if request else None,
            "problems": [error.problem()],
            **({"status": "indeterminate" if error.decision == "indeterminate" else "blocked"}
               if command == "run" else {})}
