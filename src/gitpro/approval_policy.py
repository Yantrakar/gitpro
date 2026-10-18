"""Operator-supplied, deny-by-default grants for exact persistent change plans."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
from typing import Optional

from .config import SCAN_CONTROL_RE
from .operation_contract import OperationError, decode, digest

MAX_POLICY = 65536
PATHS = {"type": "array", "maxItems": 128, "uniqueItems": True,
         "items": {"type": "string", "minLength": 1}}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["version", "rules"],
    "properties": {
        "version": {"type": "integer", "enum": [1]},
        "expires_at": {"type": "string", "pattern": r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"},
        "rules": {"type": "array", "maxItems": 32, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "kinds", "config_paths", "repo_roots", "write_paths", "write_roots"],
            "properties": {
                "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"},
                "kinds": {"type": "array", "minItems": 1, "maxItems": 4, "uniqueItems": True,
                          "items": {"enum": ["configure", "regenerate", "repo-identity", "undo"]}},
                **{key: PATHS for key in ("config_paths", "repo_roots", "write_paths", "write_roots")},
            }}},
    },
}
AUTHORITY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["kind"],
    "properties": {"kind": {"enum": ["human", "policy"]}, "policy_path": {"type": "string"},
                   "policy_sha256": {"type": "string"}, "policy_fingerprint": {"type": "string"},
                   "rule_id": {"type": "string"}},
}


def _invalid(message: str) -> OperationError:
    return OperationError("invalid_approval_policy", message, "needs_input")


def _path(value: object) -> Path:
    if not isinstance(value, str) or not value or SCAN_CONTROL_RE.search(value):
        raise _invalid("Policy paths must be absolute, normalized paths without control characters.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value or path.resolve() != path:
        raise _invalid("Policy paths must be absolute and canonical; symlink aliases are not supported.")
    return path


def parse(raw: object) -> dict:
    if (not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1 or
            set(raw) - {"version", "expires_at", "rules"}):
        raise _invalid("Use a version-1 operator approval policy with only rules and optional expires_at.")
    rules = raw.get("rules")
    if not isinstance(rules, list) or len(rules) > 32:
        raise _invalid("Supply a rules array containing at most 32 rules.")
    if "expires_at" in raw:
        value = raw["expires_at"]
        if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value) is None:
            raise _invalid("expires_at must be a UTC timestamp, YYYY-MM-DDTHH:MM:SSZ.")
        try:
            datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError:
            raise _invalid("expires_at is not a valid UTC timestamp.") from None
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {
                "id", "kinds", "config_paths", "repo_roots", "write_paths", "write_roots"}:
            raise _invalid("Every rule needs id, kinds, config_paths, repo_roots, write_paths and write_roots.")
        name = rule["id"]
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) is None or name in seen:
            raise _invalid("Rule IDs must be unique labels of at most 64 characters.")
        seen.add(name)
        kinds = rule["kinds"]
        if (not isinstance(kinds, list) or not kinds or len(kinds) > 4 or
                any(not isinstance(k, str) or k not in ("configure", "regenerate", "repo-identity", "undo") for k in kinds) or
                len(set(kinds)) != len(kinds)):
            raise _invalid("Rule kinds must be unique supported change kinds.")
        for key in ("config_paths", "repo_roots", "write_paths", "write_roots"):
            values = rule[key]
            if not isinstance(values, list) or len(values) > 128:
                raise _invalid("Each path scope must be an array containing at most 128 paths.")
            paths = [_path(value) for value in values]
            if len(set(paths)) != len(paths) or any(path == Path(path.anchor) for path in paths):
                raise _invalid("Duplicate paths and filesystem-root scopes are not allowed.")
        if not rule["write_paths"] and not rule["write_roots"]:
            raise _invalid("A rule needs at least one explicit write path or write root.")
    return raw


def _inside(path: Path, roots: list[str]) -> bool:
    return any(path == Path(root) or Path(root) in path.parents for root in roots)


@dataclass(frozen=True)
class Policy:
    path: Path
    rules: list[dict]
    sha256: str
    fingerprint: str
    expires_at: Optional[str]

    def grant(self, document: dict) -> dict:
        if self.expires_at and datetime.now(timezone.utc) >= datetime.fromisoformat(
                self.expires_at[:-1] + "+00:00"):
            raise OperationError("approval_policy_expired", "The operator approval policy expired.", "needs_approval")
        request = document["request"]
        source = request.get("config_path") or document["context"].get("config_path")
        repo = request.get("repo") or document["context"].get("repo")
        paths = [Path(c["path"]) for c in document["changes"]]
        if any(p == self.path or p in self.path.parents for p in paths):
            raise OperationError("approval_scope_denied", "An operator policy cannot authorize its own replacement.",
                                 "needs_approval")
        for rule in self.rules:
            if request["kind"] not in rule["kinds"] or not source or source not in rule["config_paths"]:
                continue
            if repo and not _inside(Path(repo), rule["repo_roots"]):
                continue
            if not all(str(p) in rule["write_paths"] or _inside(p, rule["write_roots"]) for p in paths):
                continue
            return {"kind": "policy", "policy_path": str(self.path), "policy_sha256": self.sha256,
                    "policy_fingerprint": self.fingerprint, "rule_id": rule["id"]}
        raise OperationError("approval_scope_denied", "No single operator rule covers this kind, config, repository "
                             "and every changed file. Obtain human approval or a narrower proposal.", "needs_approval")


def load(path: Path) -> Policy:
    # Share the change planner's strict, bounded file/parent checks rather than interpreting symlinks differently.
    from .changes import _bytes, _fingerprint, _snapshot
    path = _path(str(path))
    for parent in path.parents:
        if parent.exists() and parent.stat().st_uid not in (0, os.getuid()):
            raise _invalid("Policy parent directories must be owned by the operator or root.")
    snapshot = _snapshot(path)
    if snapshot is None:
        raise _invalid("The explicitly selected approval policy does not exist.")
    data = _bytes(snapshot)
    try:
        raw = parse(decode(data.decode("utf-8"), limit=MAX_POLICY))
    except OperationError as exc:
        if exc.code == "invalid_approval_policy":
            raise
        raise _invalid("The operator policy must be bounded JSON without duplicate fields.") from None
    except (UnicodeError, ValueError):
        raise _invalid("The operator policy must be valid UTF-8 JSON.") from None
    return Policy(path, raw["rules"], hashlib.sha256(data).hexdigest(),
                  digest({"path": str(path), "file": _fingerprint(snapshot)}), raw.get("expires_at"))
