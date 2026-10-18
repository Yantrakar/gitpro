"""Exact, approved persistent changes with durable backups and bounded recovery."""
from __future__ import annotations

import argparse
import base64
import contextlib
import difflib
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional

from . import change_contract as contract
from . import approval_policy
from . import config, files, jsonout, resolve
from .operation_contract import MAX_REQUEST, OperationError, decode, digest, exit_code, read_text
from .operation_io import Ledger, configuration_lock, process, repository_lock, state_dir, termination_signals
from .operation_io import sync_path as _sync

MAX_FILE = 1024 * 1024
MAX_PLAN = 4 * MAX_FILE
MAX_FILES = 128


def _safe_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise OperationError("unsafe_path", "Change paths must be absolute and normalized.", "blocked")
    for parent in (path, *path.parents):
        try:
            st = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            raise OperationError("unsafe_path", f"Symlinks are not supported in change paths: {parent}", "blocked")
        if parent != path and not stat.S_ISDIR(st.st_mode):
            raise OperationError("unsafe_path", f"A change parent is not a directory: {parent}", "blocked")
        if parent != path and st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
            raise OperationError("unsafe_path", f"A change parent is writable by others: {parent}", "blocked")


def _protect_git_metadata(path: Path) -> None:
    _safe_path(path)
    for parent in (path, *path.parents):
        # Bare/separate Git dirs need not be named .git; linked dirs carry commondir.
        if parent.name.casefold() == ".git" or ((parent / "HEAD").is_file() and (
                (parent / "objects").is_dir() or (parent / "refs").is_dir() or
                (parent / "commondir").is_file())):
            raise OperationError("unsafe_target", f"Managed configuration cannot replace Git metadata: {path}. "
                                 "Use repo-identity for a supported local identity repair.", "blocked")


def _snapshot(path: Path) -> Optional[dict]:
    _safe_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022:
            raise OperationError("unsafe_file", f"Expected a trusted, non-writable-by-others regular file: {path}",
                                 "blocked")
        data = stream.read(MAX_FILE + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_FILE:
        raise OperationError("file_too_large", f"Change inputs are limited to 1 MiB per file: {path}", "blocked")
    if (st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise OperationError("stale_plan", "A file changed while it was being read.", "blocked")
    return {**_content(data, stat.S_IMODE(st.st_mode)), "uid": st.st_uid,
            "stamp": [st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns]}


def _content(data: bytes, mode: int) -> dict:
    return {"data": base64.b64encode(data).decode("ascii"), "sha256": hashlib.sha256(data).hexdigest(), "mode": mode}


def _bytes(value: Optional[dict]) -> bytes:
    return base64.b64decode(value["data"], validate=True) if value else b""


def _fingerprint(value: Optional[dict]) -> Optional[dict]:
    return {k: v for k, v in value.items() if k != "data"} if value else None


def _same_content(left: Optional[dict], right: Optional[dict]) -> bool:
    return ((left is None and right is None) or
            (left is not None and right is not None and
             (left["sha256"], left["mode"]) == (right["sha256"], right["mode"])))


class Store(Ledger):
    """Shares the private, FULL-synchronous operation database; old bytes are the undo backup."""

    def __init__(self, readonly: bool = False) -> None:
        try:
            super().__init__(readonly)
        except OperationError as exc:
            if readonly and exc.code == "unknown_request":
                raise OperationError("approval_required", "No persistent change approval is recorded.",
                                     "needs_approval") from None
            raise
        if not readonly:
            try:
                with self.db:
                    self.db.execute("CREATE TABLE IF NOT EXISTS changes "
                                    "(id TEXT PRIMARY KEY, document TEXT NOT NULL, state TEXT NOT NULL, "
                                    "approved_at REAL NOT NULL, result TEXT)")
                    self.db.execute("CREATE TABLE IF NOT EXISTS change_locks "
                                    "(id TEXT, path TEXT, device INTEGER, inode INTEGER, ctime INTEGER, "
                                    "PRIMARY KEY(id, path))")
                    self.db.execute("CREATE TABLE IF NOT EXISTS change_writes "
                                    "(id TEXT, path TEXT, PRIMARY KEY(id, path))")
                    self.db.execute("CREATE TABLE IF NOT EXISTS change_authorizations "
                                    "(id TEXT PRIMARY KEY, authority TEXT NOT NULL)")
                    columns = {row[1] for row in self.db.execute("PRAGMA table_info(changes)")}
                    if "authority_kind" not in columns:
                        self.db.execute("ALTER TABLE changes ADD COLUMN authority_kind TEXT NOT NULL DEFAULT 'human'")
            except (OSError, sqlite3.Error):
                self.close()
                raise

    def lookup(self, identifier: str) -> tuple[dict, str, Optional[dict]]:
        exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='changes'").fetchone()
        row = (self.db.execute("SELECT document, state, result FROM changes WHERE id=?", (identifier,)).fetchone()
               if exists else None)
        if row is None:
            raise OperationError("approval_required", "No approval is recorded for this exact plan.", "needs_approval")
        document = json.loads(row[0])
        result = json.loads(row[2]) if row[2] is not None else None
        if (not isinstance(document, dict) or digest(document) != identifier or
                row[1] not in ("approved", "applying", "applied", "rolled_back", "indeterminate") or
                (row[1] in ("applied", "rolled_back", "indeterminate") and not isinstance(result, dict))):
            raise OperationError("journal_corrupt", "The recorded change does not match its ID.", "indeterminate")
        return document, row[1], result

    def approve(self, document: dict, authority: Optional[dict] = None) -> None:
        authority = authority or {"kind": "human"}
        identifier = digest(document)
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO changes "
                                       "(id, document, state, approved_at, result, authority_kind) "
                                       "VALUES (?, ?, 'approved', ?, NULL, ?)",
                                       (identifier, json.dumps(document), time.time(), authority["kind"])).rowcount
            state = self.db.execute("SELECT state FROM changes WHERE id=?", (identifier,)).fetchone()[0]
            if inserted or (state == "approved" and (
                    authority["kind"] == "human" or self.authority(identifier)["kind"] == "policy")):
                self.db.execute("INSERT OR REPLACE INTO change_authorizations VALUES (?, ?)",
                                (identifier, json.dumps(authority)))
                self.db.execute("UPDATE changes SET authority_kind=?, approved_at=? WHERE id=?",
                                (authority["kind"], time.time(), identifier))

    def authority(self, identifier: str) -> dict:
        table = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='change_authorizations'").fetchone()
        row = (self.db.execute("SELECT authority FROM change_authorizations WHERE id=?", (identifier,)).fetchone()
               if table else None)
        columns = {col[1] for col in self.db.execute("PRAGMA table_info(changes)")}
        kind = (self.db.execute("SELECT authority_kind FROM changes WHERE id=?", (identifier,)).fetchone()[0]
                if "authority_kind" in columns else "human")
        # Records created before scoped policies existed were exclusively terminal-approved.
        value = json.loads(row[0]) if row else {"kind": "human"}
        if not isinstance(value, dict) or value.get("kind") not in ("human", "policy") or value["kind"] != kind:
            raise OperationError("journal_corrupt", "Invalid approval authority record.", "indeterminate")
        return value

    def transition(self, identifier: str, state: str, result: Optional[dict] = None) -> None:
        with self.db:
            self.db.execute("UPDATE changes SET state=?, result=? WHERE id=?",
                            (state, json.dumps(result) if result is not None else None, identifier))

    def record_lock(self, identifier: str, path: Path, st: os.stat_result) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO change_locks VALUES (?, ?, ?, ?, ?)",
                            (identifier, str(path), st.st_dev, st.st_ino, st.st_ctime_ns))

    def record_write(self, identifier: str, path: Path) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO change_writes VALUES (?, ?)", (identifier, str(path)))

    def written(self, identifier: str) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT path FROM change_writes WHERE id=?", (identifier,))}

    def release_orphan_locks(self, document: dict) -> None:
        allowed = {str(Path(c["path"]).with_name(Path(c["path"]).name + ".lock")) for c in document["changes"]}
        for name, device, inode, ctime in self.db.execute(
                "SELECT path, device, inode, ctime FROM change_locks WHERE id=?", (digest(document),)):
            if name not in allowed:
                raise OperationError("journal_corrupt", "Unexpected target lock in the change journal.", "indeterminate")
            path = Path(name)
            _safe_path(path)
            try:
                current = path.lstat()
            except FileNotFoundError:
                continue
            if (current.st_dev, current.st_ino, current.st_ctime_ns) == (device, inode, ctime):
                path.unlink()
                _sync(path.parent, directory=True)


class Builder:
    def __init__(self, request: dict):
        self.request = request
        self.home = Path.home().resolve()
        self.inputs: dict[str, Optional[dict]] = {}
        self.changes: list[dict] = []
        self.context: dict = {}

    def read(self, path: Path) -> Optional[dict]:
        key = str(path)
        if key not in self.inputs:
            if len(self.inputs) >= MAX_FILES:
                raise OperationError("plan_too_large", "A change is limited to 128 input files.", "blocked")
            self.inputs[key] = _snapshot(path)
        return self.inputs[key]

    def target(self, path: Path, after: Optional[dict]) -> None:
        before = self.read(path)
        if before is not None and before["uid"] != os.getuid():
            raise OperationError("unsafe_file", f"Change targets must be owned by you: {path}", "blocked")
        if not _same_content(before, after):
            self.changes.append({"path": str(path), "before": before, "after": after})

    def document(self) -> dict:
        parents = {}
        for name, before in self.inputs.items():
            path = Path(name)
            if _snapshot(path) != before:
                raise OperationError("stale_plan", "Change inputs moved while planning; propose again.", "blocked")
            for parent in path.parents:
                if str(parent) in parents:
                    continue
                try:
                    st = parent.lstat()
                    parents[str(parent)] = [st.st_dev, st.st_ino, st.st_uid, stat.S_IMODE(st.st_mode)]
                except FileNotFoundError:
                    parents[str(parent)] = None
        document = {"version": 1, "home": str(self.home), "request": self.request, "context": self.context,
                    "inputs": {p: _fingerprint(v) for p, v in sorted(self.inputs.items())},
                    "parents": parents, "changes": sorted(self.changes, key=lambda c: c["path"])}
        if len(json.dumps(document).encode()) > MAX_PLAN:
            raise OperationError("plan_too_large", "A change and its backups are limited to 4 MiB.", "blocked")
        return document


def _managed(builder: Builder) -> config.Config:
    from . import plan, render
    source = Path(builder.request["config_path"])
    _protect_git_metadata(source)
    current = builder.read(source)
    if builder.request["kind"] == "configure":
        text = builder.request["toml"]
    elif current is not None:
        text = _bytes(current).decode("utf-8")
    else:
        raise OperationError("missing_config", "No configuration exists; supply a configure proposal.",
                             "needs_input")
    cfg = config.loads(text, source, "--config")
    env = plan.Env(builder.home)
    paths = [builder.home / n for n in (plan.MANAGED_GITCONFIG, ".gitconfig", plan.ALLOWED_SIGNERS,
                                       ".ssh/config", ".ssh/" + plan.MANAGED_SSH)]
    paths += plan.guard_paths(env) + [env.expand(i.file) for i in cfg.identities]
    private_keys = {env.expand(person.ssh_key) for person in (cfg.default, *cfg.identities) if person.ssh_key}
    # Check paths before the renderer follows or reads them, including its old manifest.
    managed = builder.read(builder.home / plan.MANAGED_GITCONFIG)
    paths += [env.expand(p) for p in render.manifest_of(_bytes(managed).decode("utf-8"))]
    for path in (source, *paths):
        _protect_git_metadata(path)
        journal = state_dir().resolve()
        if path == journal or journal in path.parents or path in journal.parents:
            raise OperationError("unsafe_target", "Managed files cannot replace the operation journal or its locks.",
                                 "blocked")
        if builder.home not in path.parents or path in private_keys:
            raise OperationError("unsafe_target", "Managed changes must stay inside home and cannot replace keys.",
                                 "blocked")
        if builder.home / ".ssh" in path.parents and path not in (
                builder.home / ".ssh/config", builder.home / ".ssh" / plan.MANAGED_SSH):
            raise OperationError("unsafe_target", "Only gitpro's two managed SSH configuration files may change.",
                                 "blocked")
        builder.read(path)
    for key in private_keys:
        builder.read(render.pub_path(key))
    from .setup import _validate
    _validate(text, source, builder.home)
    targets, stale = plan.build(cfg, env)
    for change in files.diff(targets, stale):
        before = builder.read(change.path)
        after = _content(_preserve_lines(_bytes(before), change.new), change.mode) if change.new is not None else None
        builder.target(change.path, after)
    if builder.request["kind"] == "configure":
        builder.target(source, _content(text.encode("utf-8"), 0o600))
    return cfg


def _preserve_lines(before: bytes, text: str) -> bytes:
    """Keep unchanged hand-written bytes, including CRLF; use their line ending for inserted lines."""
    old = before.decode("utf-8")
    raw = old.splitlines(True)
    normalized = old.replace("\r\n", "\n").replace("\r", "\n").splitlines(True)
    new = text.splitlines(True)
    ending = "\r\n" if old.count("\r\n") > old.count("\n") - old.count("\r\n") else "\n"
    out = []
    for tag, a, b, c, d in difflib.SequenceMatcher(a=normalized, b=new, autojunk=False).get_opcodes():
        out.extend(raw[a:b] if tag == "equal" else [line.replace("\n", ending) for line in new[c:d]])
    return "".join(out).encode("utf-8")


def _repo_identity(builder: Builder, cfg: config.Config) -> None:
    if builder.changes:
        raise OperationError("not_applied", "Approve a regenerate change before repairing repository overrides.",
                             "needs_input")
    from .operations import child_environment
    env = child_environment()
    repo = Path(builder.request["repo"])
    layout = resolve._need_layout(repo, env)
    if layout.top != repo:
        raise OperationError("invalid_repo", "Supply a worktree root, not a bare repository or subdirectory.",
                             "needs_input")
    builder.context["common_dir"] = str(layout.common_dir)
    local = layout.common_dir / "config"
    if (repo / ".git").is_file():
        builder.read(repo / ".git")
    builder.read(layout.git_dir / "config.worktree")
    before = builder.read(local)
    if before is None:
        raise OperationError("invalid_repo", "The repository has no local configuration file.", "blocked")
    report = resolve.compare(repo, cfg, git_env=env, extra_checks=False)
    output = process(["git", "config", "-z", "--show-scope", "--show-origin", "--list"], repo, env)
    if output.code != 0 or output.stopped:
        raise OperationError("config_read_failed", "Could not read effective repository configuration.", "blocked")
    parts = output.out.split("\0")
    if (len(parts) - 1) % 3 or parts[-1]:
        raise OperationError("config_read_failed", "Malformed effective repository configuration.", "blocked")
    beneath = {}
    for n in range(0, len(parts) - 1, 3):
        scope, origin, item = parts[n:n + 3]
        origin = resolve._absolute(origin, layout)
        key, _, value = item.partition("\n")
        if origin.startswith("file:"):
            builder.read(Path(origin[5:]))
        if key.lower() in ("user.name", "user.email") and origin != "file:" + str(local):
            beneath[key.lower()] = value
        if scope in ("local", "worktree") and (
                key.lower().startswith(("include.", "includeif.")) or scope == "worktree" or
                key.lower() == "extensions.worktreeconfig"):
            raise OperationError("unsupported_repair", "Pins, local includes and worktree-specific configuration "
                                 "need separate human repair.", "needs_input")
    keys = []
    for field in ("name", "email"):
        setting = getattr(report.effective, field)
        expected = getattr(report.expected, field)
        if setting.value != expected:
            if (not expected or setting.scope != "repo" or setting.origin != "file:" + str(local) or
                    beneath.get("user." + field) != expected):
                raise OperationError("unsupported_repair", "Removing direct local overrides would not restore "
                                     "the configured identity.", "needs_input")
            keys.append("user." + field)
    if any(p.code != "override" for p in report.problems):
        raise OperationError("unsupported_repair", "This repository also needs repairs beyond direct name/email "
                             "overrides; inspect gitpro why.", "needs_input")
    builder.context.update(effective_digest=digest(output.out), identity={"name": report.expected.name,
                                                                         "email": report.expected.email})
    if not keys:
        return
    with tempfile.TemporaryDirectory(prefix="gitpro-change-") as tmp:
        copy = Path(tmp) / "config"
        files.atomic_write(copy, _bytes(before), 0o600, overwrite=False)
        for key in keys:
            result = process(["git", "config", "--file", str(copy), "--unset-all", key], repo, env)
            if result.code != 0 or result.stopped:
                raise OperationError("repair_failed", "Could not prepare the repository identity repair.", "blocked")
        builder.target(local, _content(copy.read_bytes(), before["mode"]))


def _request(raw: object, cfg_path: Optional[Path] = None) -> dict:
    if isinstance(raw, dict) and cfg_path is not None and raw.get("kind") != "undo" and "config_path" not in raw:
        raw = dict(raw, config_path=str(cfg_path))
    return contract.parse(raw)


def build(raw: object, cfg_path: Optional[Path] = None) -> dict:
    request = _request(raw, cfg_path)
    builder = Builder(request)
    if request["kind"] == "undo":
        with contextlib.closing(Store(readonly=True)) as store:
            original, state, _ = store.lookup(request["change_id"])
        if state != "applied" or original["home"] != str(builder.home):
            raise OperationError("undo_unavailable", "Only a successfully applied change for this home can be undone.",
                                 "blocked")
        builder.context = {"undo_of": request["change_id"], **{
            k: v for k, v in original["context"].items() if k == "common_dir"}}
        for key in ("config_path", "repo"):
            value = original["request"].get(key) or original["context"].get(key)
            if value:
                builder.context[key] = value
        for change in original["changes"]:
            path = Path(change["path"])
            if not _same_content(builder.read(path), change["after"]):
                raise OperationError("stale_plan", "An applied file changed; undo will not overwrite it.", "blocked")
            builder.target(path, change["before"])
    else:
        cfg = _managed(builder)
        if request["kind"] == "repo-identity":
            _repo_identity(builder, cfg)
    document = builder.document()
    review = json.dumps({"request": document["request"], "changes": _display(document)}, ensure_ascii=False)
    if resolve.redact_text(review) != review:
        raise OperationError("sensitive_configuration", "The proposed request/diff contains credential-bearing URLs. "
                             "Use credential helpers rather than literal credentials in configuration.", "needs_input")
    return document


def _display(document: dict) -> list[dict]:
    changes = []
    for change in document["changes"]:
        before, after = change["before"], change["after"]
        action = "create" if before is None else "remove" if after is None else (
            "chmod" if before["sha256"] == after["sha256"] else "update")
        # No context: unrelated hand-written credentials must not leak into a proposal.
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                       for line in difflib.unified_diff(
            _bytes(before).decode("utf-8").splitlines(True), _bytes(after).decode("utf-8").splitlines(True),
            "before", "after", n=0))
        changes.append({"path": change["path"], "action": action,
                        "before_sha256": before["sha256"] if before else None,
                        "after_sha256": after["sha256"] if after else None,
                        "before_mode": before["mode"] if before else None,
                        "after_mode": after["mode"] if after else None, "diff": diff})
    return changes


def _result(command: str, document: dict, status: str) -> dict:
    return {"version": 1, "command": "change " + command, "ok": True,
            "decision": "needs_approval" if status == "proposed" else "ready",
            "problems": [], "plan_id": digest(document), "status": status}


def propose(raw: object, cfg_path: Optional[Path] = None, *, policy_path: Optional[Path] = None) -> dict:
    try:
        document = build(raw, cfg_path)
        result = {**_result("propose", document, "proposed" if document["changes"] else "no_changes"),
                  "request": document["request"], "changes": _display(document), "authority": {"kind": "human"},
                  "approval": "Human review or a covering operator policy is required; a plan ID is not authorization."}
        if policy_path is not None:
            selected = approval_policy.load(policy_path)
            try:
                result["authority"] = selected.grant(document)
                result["decision"] = "ready"
                result["approval"] = "Covered by operator policy; apply still requires host authorization and revalidation."
            except OperationError as exc:
                if exc.decision != "needs_approval":
                    raise
                result["problems"].append(exc.problem())
        return result
    except OperationError as exc:
        return contract.error("propose", exc)
    except config.ConfigError as exc:
        return contract.error("propose", OperationError("invalid_config", "Invalid configuration: " +
                                                       resolve.redact_text(str(exc)), "needs_input"))
    except (OSError, ValueError, sqlite3.Error):
        return contract.error("propose", OperationError("planning_failed", "Could not read or render change inputs.",
                                                       "blocked"))


def approve(proposal: object) -> dict:
    """Record human approval through a terminal; operator policy grants use a separate path."""
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise OperationError("terminal_required", "Approval requires a human-controlled interactive terminal.",
                                 "needs_approval")
        if not sys.stderr.isatty():
            raise OperationError("terminal_required", "Approval review must also be visible on the terminal (stderr).",
                                 "needs_approval")
        if not isinstance(proposal, dict) or proposal.get("command") != "change propose":
            raise OperationError("invalid_plan", "Supply the JSON result of change propose.", "needs_input")
        identifier = contract.plan_id(proposal.get("plan_id"))
        document = build(proposal.get("request"))
        if digest(document) != identifier:
            raise OperationError("stale_plan", "The proposal no longer matches disk; propose and review again.", "blocked")
        from rich.console import Console
        from rich.text import Text
        from rich.prompt import Confirm
        out = Console(stderr=True, highlight=False)
        out.print(Text(f"Approve persistent gitpro change {identifier}\n"))
        for item in _display(document):
            out.print(Text(f"{item['action']} {item['path']} "
                           f"(mode {item['before_mode']} -> {item['after_mode']})\n{item['diff']}"))
        out.print("This approval covers these exact files and conflict-safe rollback on failure.\n"
                  "Parent directories may be created privately. It does not authorize future changed plans.")
        if not document["changes"]:
            return _result("approve", document, "no_changes")
        if not Confirm.ask("Approve this exact change?", default=False, console=out):
            return dict(contract.error("approve", OperationError("approval_declined", "No approval recorded.",
                                                                 "needs_approval")), status="rejected")
        with configuration_lock(exclusive=True):
            if digest(build(document["request"])) != identifier:
                raise OperationError("stale_plan", "Inputs changed during review; no approval recorded.", "blocked")
            with contextlib.closing(Store()) as store:
                store.approve(document)
                _, state, result = store.lookup(identifier)
                return dict(result or _result("approve", document, state), command="change approve")
    except OperationError as exc:
        return contract.error("approve", exc)
    except (OSError, ValueError, config.ConfigError, sqlite3.Error, KeyboardInterrupt):
        return contract.error("approve", OperationError("approval_failed", "Approval was cancelled or could not "
                              "be durably recorded. Inspect change status.", "indeterminate"))


@contextlib.contextmanager
def _target_lock(path: Path, store: Store, identifier: str) -> Iterator[None]:
    lock = path.with_name(path.name + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise OperationError("target_busy", f"A file is locked; no write was made to {path}.", "blocked") from None
    st = os.fstat(fd)
    os.close(fd)
    try:
        store.record_lock(identifier, lock, st)
        yield
    finally:
        try:
            current = lock.lstat()
        except FileNotFoundError:
            current = None
        if current and (st.st_dev, st.st_ino, st.st_ctime_ns) == (
                current.st_dev, current.st_ino, current.st_ctime_ns):
            lock.unlink()
            _sync(lock.parent, directory=True)


def _replace(path: Path, expected: Optional[dict], wanted: Optional[dict], store: Store, identifier: str) -> None:
    _safe_path(path)
    missing = [p for p in path.parents if not p.exists()]
    for parent in reversed(missing):
        parent.mkdir(mode=0o700)
        _sync(parent.parent, directory=True)
    with _target_lock(path, store, identifier):
        if _snapshot(path) != expected:
            raise OperationError("stale_plan", f"A file changed before replacement: {path}", "blocked")
        store.record_write(identifier, path)
        if wanted is None:
            path.unlink()
        else:
            files.atomic_write(path, _bytes(wanted), wanted["mode"], follow_symlinks=False,
                               overwrite=expected is not None)
            _sync(path)
        _sync(path.parent, directory=True)
        if not _same_content(_snapshot(path), wanted):
            raise OperationError("verification_failed", f"Replacement could not be verified: {path}", "indeterminate")


def _rollback(document: dict, store: Store) -> list[str]:
    conflicts = []
    identifier = digest(document)
    written = store.written(identifier)
    for change in reversed(document["changes"]):
        path = Path(change["path"])
        if str(path) not in written:
            continue
        try:
            current = _snapshot(path)
            if _same_content(current, change["before"]):
                if current is not None:
                    _sync(path)
                _sync(path.parent, directory=True)
                continue
            if not _same_content(current, change["after"]):
                conflicts.append(str(path))
                continue
            _replace(path, current, change["before"], store, identifier)
        except (OSError, OperationError, sqlite3.Error):
            conflicts.append(str(path))
    return conflicts


def _verify(document: dict) -> None:
    changed = {c["path"] for c in document["changes"]}
    for name, before in document["inputs"].items():
        if name not in changed and _fingerprint(_snapshot(Path(name))) != before:
            raise OperationError("verification_failed", "An unchanged input moved while applying the change.",
                                 "indeterminate")
    for change in document["changes"]:
        if not _same_content(_snapshot(Path(change["path"])), change["after"]):
            raise OperationError("verification_failed", "The applied files do not match their reviewed contents.",
                                 "indeterminate")
    if document["request"]["kind"] == "repo-identity":
        from .operations import child_environment
        cfg = config.load_all(Path(document["request"]["config_path"]))
        report = resolve.compare(Path(document["request"]["repo"]), cfg, git_env=child_environment(),
                                 extra_checks=False)
        if not report.ok():
            raise OperationError("verification_failed", "The repository identity is not repaired.", "indeterminate")


def _replay(document: dict, state: str, previous: Optional[dict], command: str) -> Optional[dict]:
    if document["home"] != str(Path.home().resolve()):
        raise OperationError("wrong_home", "This approval belongs to another home directory.", "blocked")
    if state not in ("applied", "rolled_back") or previous is None:
        return None
    if command == "apply" and state == "rolled_back" and previous["ok"]:
        return dict(contract.error(command, OperationError("change_rolled_back", "This change was recovered, not "
                    "applied. A new attempt needs a fresh proposal and approval.", "blocked")),
                    plan_id=digest(document), status=state, replayed=True)
    return dict(previous, command="change " + command, replayed=True)


def _authorize(identifier: str, raw: object, policy_path: Optional[Path], cfg_path: Optional[Path]) -> None:
    if policy_path is None:
        raise OperationError("approval_required", "Unattended authorization requires an operator-selected policy.",
                             "needs_approval")
    with configuration_lock(exclusive=True):
        document = build(raw, cfg_path)
        if digest(document) != identifier:
            raise OperationError("stale_plan", "The supplied request no longer matches the reviewed plan.", "blocked")
        authority = approval_policy.load(policy_path).grant(document)
        with contextlib.closing(Store()) as store:
            store.approve(document, authority)


def _apply(identifier: str, recover: bool, policy_path: Optional[Path] = None) -> dict:
    command = "recover" if recover else "apply"
    with contextlib.closing(Store(readonly=True)) as store:
        completed = _replay(*store.lookup(identifier), command)
        if completed is not None:
            return completed
    with configuration_lock(exclusive=True), contextlib.closing(Store()) as store:
        document, state, previous = store.lookup(identifier)
        completed = _replay(document, state, previous, command)
        if completed is not None:
            return completed
        if state not in ("approved", "applying", "indeterminate"):
            raise OperationError("journal_corrupt", "Unknown change journal state.", "indeterminate")
        if state != "approved" and not recover:
            raise OperationError("outcome_unknown", "This change was interrupted. Inspect status, then request "
                                 "change recover; apply will not repeat its writes.", "indeterminate")
        if recover and state == "approved":
            raise OperationError("not_started", "This approved change has not started; there is nothing to recover.",
                                 "blocked")
        common = document["context"].get("common_dir")
        with repository_lock(Path(common)) if common else contextlib.nullcontext():
            if not recover:
                if digest(build(document["request"])) != identifier:
                    raise OperationError("stale_plan", "The approved inputs changed; propose and approve again.", "blocked")
                authority = store.authority(identifier)
                if authority["kind"] == "policy":
                    if policy_path is None:
                        raise OperationError("approval_required", "This plan needs its operator policy at execution.",
                                             "needs_approval")
                    current = approval_policy.load(policy_path).grant(document)
                    if current != authority:
                        raise OperationError("approval_policy_changed", "The granting policy changed; explicitly "
                                             "reauthorize the exact plan or obtain human approval.", "needs_approval")
                store.transition(identifier, "applying")
            try:
                if recover:
                    store.release_orphan_locks(document)
                    conflicts = _rollback(document, store)
                    if conflicts:
                        raise OperationError("recovery_conflict", "Recovery preserved changed or locked files: " +
                                             ", ".join(conflicts), "indeterminate")
                    result = _result(command, document, "rolled_back")
                else:
                    for change in document["changes"]:
                        _replace(Path(change["path"]), change["before"], change["after"], store, identifier)
                    _verify(document)
                    result = _result(command, document, "applied")
            except (OSError, OperationError, config.ConfigError, ValueError, KeyboardInterrupt) as exc:
                conflicts = _rollback(document, store) if not recover else ["recovery incomplete"]
                problem = exc if isinstance(exc, OperationError) else OperationError(
                    "apply_failed", "The change failed or was cancelled.", "blocked")
                result = contract.error(command, problem)
                result.update(plan_id=identifier, status="indeterminate" if conflicts else "rolled_back")
                result["decision"] = "indeterminate" if conflicts else "blocked"
                if conflicts:
                    result["problems"].append(OperationError("recovery_required", "Could not restore: " +
                                              ", ".join(conflicts) + ". Backups are retained; inspect the files "
                                              "and use change recover, not apply.", "indeterminate").problem())
            result.update(replayed=False, backup_store=str(state_dir() / "ledger.sqlite3"),
                          authority=store.authority(identifier),
                          verification="exact file contents and modes; run preflight for operation readiness")
            try:
                store.transition(identifier, result["status"], result)
            except sqlite3.Error:
                return dict(contract.error(command, OperationError("journal_failed", "The files may have changed, "
                            "but their outcome could not be recorded. Inspect status and recover; do not reapply.",
                            "indeterminate")), plan_id=identifier, status="indeterminate")
            return result


def apply(identifier: object, *, authorized: bool = False, recover: bool = False,
          raw: object = None, policy_path: Optional[Path] = None, cfg_path: Optional[Path] = None) -> dict:
    command = "recover" if recover else "apply"
    try:
        identifier = contract.plan_id(identifier)
        if not authorized:
            raise OperationError("authorization_required", "Applying or recovering needs explicit host/user "
                                 "authorization as well as recorded approval.", "needs_approval")
        if raw is not None:
            if recover:
                raise OperationError("invalid_request", "Recovery uses the original recorded rollback permission.",
                                     "needs_input")
            # Completed or uncertain attempts must never be reauthorized into a fresh forward execution.
            needs_grant = True
            normalized = _request(raw, cfg_path)
            try:
                with contextlib.closing(Store(readonly=True)) as store:
                    document, state, previous = store.lookup(identifier)
                    if normalized != document["request"]:
                        raise OperationError("idempotency_conflict", "The request differs from this recorded plan.",
                                             "blocked")
                    completed = _replay(document, state, previous, command)
                    if completed is not None:
                        return completed
                    if state != "approved":
                        raise OperationError("outcome_unknown", "Inspect and recover this interrupted attempt.",
                                             "indeterminate")
                    needs_grant = store.authority(identifier)["kind"] != "human"
            except OperationError as exc:
                if exc.code != "approval_required":
                    raise
            if needs_grant:
                _authorize(identifier, raw, policy_path, cfg_path)
        return _apply(identifier, recover, policy_path)
    except OperationError as exc:
        return contract.error(command, exc)
    except (OSError, ValueError, config.ConfigError, sqlite3.Error, KeyboardInterrupt):
        return contract.error(command, OperationError("change_failed", "Could not complete the change. Inspect "
                              "change status before any retry.", "indeterminate"))


def status(identifier: object) -> dict:
    try:
        identifier = contract.plan_id(identifier)
        with contextlib.closing(Store(readonly=True)) as store:
            document, state, result = store.lookup(identifier)
            authority = store.authority(identifier)
        details = {"files": [c["path"] for c in document["changes"]],
                   "authority": authority,
                   "backup_store": str(state_dir() / "ledger.sqlite3")}
        if result is not None:
            return dict(result, **details, command="change status", replayed=True)
        if state != "approved":
            return dict(contract.error("status", OperationError("outcome_unknown", "Apply was interrupted or is "
                        "active. Do not reapply; inspect and request recovery.", "indeterminate")),
                        **details, plan_id=identifier, status=state)
        return {**_result("status", document, state), **details}
    except OperationError as exc:
        return contract.error("status", exc)
    except (OSError, ValueError, sqlite3.Error):
        return contract.error("status", OperationError("journal_failed", "Could not read the change journal.",
                                                       "indeterminate"))


def command(args: argparse.Namespace) -> int:
    with termination_signals():
        action = args.change_command
        if getattr(args, "schema", False):
            jsonout.emit("change-schema", {"version": 1, "request": contract.REQUEST_SCHEMA,
                                           "result": contract.RESULT_SCHEMA, "approval_policy": approval_policy.SCHEMA})
            return 0
        try:
            if action in ("propose", "approve"):
                source = args.request if action == "propose" else args.proposal
                if not source:
                    raise OperationError("missing_input", "Supply the request/proposal JSON file.", "needs_input")
                limit = MAX_REQUEST if action == "propose" else MAX_PLAN
                text = read_text(source, limit)
                if action == "propose":
                    result = propose(decode(text), args.config, policy_path=args.approval_policy)
                else:
                    if len(text.encode()) > limit:
                        raise OperationError("invalid_plan", "Proposal exceeds 4 MiB.", "needs_input")
                    proposal = decode(text, limit=limit)
                    if args.write:
                        result = approve(proposal)
                    else:
                        if not isinstance(proposal, dict):
                            raise OperationError("invalid_plan", "Supply a change proposal.", "needs_input")
                        result = propose(proposal.get("request"))
                        if result["ok"] and result["plan_id"] != proposal.get("plan_id"):
                            raise OperationError("stale_plan", "The proposal changed; propose again.", "blocked")
            elif action == "status":
                result = status(args.plan_id)
            else:
                raw = None
                if action == "apply" and args.proposal:
                    with open(args.proposal, encoding="utf-8") as stream:
                        proposal = decode(stream.read(MAX_PLAN + 1), limit=MAX_PLAN)
                    if not isinstance(proposal, dict) or proposal.get("plan_id") != args.plan_id:
                        raise OperationError("invalid_plan", "Proposal and --plan-id must match.", "needs_input")
                    raw = proposal.get("request")
                    if raw is None:
                        raise OperationError("invalid_request", "Proposal has no request.", "needs_input")
                if not (args.yes or args.write):
                    if action == "recover":
                        result = status(args.plan_id)
                    else:
                        if raw is None:
                            with contextlib.closing(Store(readonly=True)) as store:
                                document, _, _ = store.lookup(contract.plan_id(args.plan_id))
                            raw = document["request"]
                        result = propose(raw, policy_path=getattr(args, "approval_policy", None))
                        if result["ok"] and result["plan_id"] != args.plan_id:
                            raise OperationError("stale_plan", "The inputs changed; propose again.", "blocked")
                else:
                    result = apply(args.plan_id, authorized=True, recover=action == "recover", raw=raw,
                                   policy_path=getattr(args, "approval_policy", None))
        except OperationError as exc:
            result = contract.error(action, exc)
        except (OSError, ValueError):
            result = contract.error(action, OperationError("invalid_request", "Could not read the JSON input.",
                                                           "needs_input"))
        jsonout.emit(result["command"], result)
        return exit_code(result)


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser,
             confirm: argparse.ArgumentParser) -> None:
    from .args import capability
    parent = sub.add_parser("change", help="propose, approve and apply exact persistent identity changes")
    commands = parent.add_subparsers(dest="change_command", required=True)
    for name in ("propose", "approve", "apply", "status", "recover"):
        parents = [common] if name == "propose" else [confirm] if name in ("apply", "recover") else []
        parser = commands.add_parser(name, parents=parents, help=name + " a persistent change")
        parser.add_argument("--json", action="store_true", help="JSON is always emitted (approval also needs a terminal)")
        if name == "propose":
            parser.add_argument("--request", metavar="FILE", help="versioned change JSON (- reads stdin)")
            parser.add_argument("--schema", action="store_true", help="print request and result JSON Schemas")
        elif name == "approve":
            parser.add_argument("--proposal", required=True, metavar="FILE", help="saved change propose JSON")
            parser.add_argument("--write", action="store_true", help="record human approval after terminal review")
        else:
            parser.add_argument("--plan-id", required=True, help="the complete approved plan ID")
        if name in ("propose", "apply"):
            parser.add_argument("--approval-policy", type=Path, help="operator-owned scoped authorization JSON")
        if name == "apply":
            parser.add_argument("--proposal", metavar="FILE", help="exact proposal to authorize under --approval-policy")
        parser.set_defaults(func=command)
    capability("change-propose.v1", "change-approve.v1", "change-apply.v1", "change-status.v1", "change-recover.v1")
    capability("approval-policy.v1")
