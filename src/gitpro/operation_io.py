"""Bounded child processes and durable, cooperative operation serialization (Linux/macOS)."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional, Sequence
from types import FrameType

from .operation_contract import OperationError, digest

MAX_OUTPUT = 1024 * 1024
TIMEOUT = 30
GRACE = 2


@contextlib.contextmanager
def termination_signals() -> Iterator[None]:
    """Let a CLI/MCP SIGTERM unwind locks and stop the active child, like Ctrl-C."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def terminate(signum: int, frame: Optional[FrameType]) -> None:
        raise KeyboardInterrupt

    old = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


@dataclass(frozen=True)
class Process:
    code: int
    out: str
    err: str
    stopped: Optional[str] = None


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def process(argv: Sequence[str], repo: Path, env: Dict[str, str], *,
            text: Optional[str] = None, timeout: float = TIMEOUT) -> Process:
    """No inherited stdin, shell, or unbounded pipe buffers; output is never logged here."""
    with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        if text is not None:
            inp.write(text.encode("utf-8"))
            inp.seek(0)
        child = subprocess.Popen(list(argv), cwd=repo, env=env, stdin=inp, stdout=out, stderr=err,
                                 start_new_session=True)
        stopped = None
        deadline = time.monotonic() + timeout
        try:
            while child.poll() is None:
                if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > MAX_OUTPUT:
                    stopped = "output_limit"
                    break
                if time.monotonic() >= deadline:
                    stopped = "timeout"
                    break
                time.sleep(0.02)
        except KeyboardInterrupt:
            stopped = "cancelled"
        finally:
            if stopped or child.poll() is None:
                _signal_group(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=GRACE)
                except subprocess.TimeoutExpired:
                    pass
            # A hook/helper may outlive its leader. Never leave it running after this operation.
            _signal_group(child.pid, signal.SIGKILL)
            child.wait()
        if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > MAX_OUTPUT:
            stopped = stopped or "output_limit"
        out.seek(0)
        err.seek(0)
        return Process(child.returncode, out.read(MAX_OUTPUT).decode("utf-8", "replace"),
                       err.read(MAX_OUTPUT).decode("utf-8", "replace"), stopped)


def file_hash(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    if not base.is_absolute():
        raise OperationError("invalid_state_path", "XDG_STATE_HOME must be an absolute path; "
                             "no operation state was opened or created.", "needs_input")
    return base / "gitpro" / "operations"


def sync_path(path: Path, *, directory: bool = False) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if path.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise OperationError("unsafe_state", "The operation state directory must be owned by you and mode 0700.")


@contextlib.contextmanager
def repository_lock(common: Path) -> Iterator[None]:
    with state_lock(digest(str(common.resolve()))):
        yield


@contextlib.contextmanager
def configuration_lock(*, exclusive: bool = False) -> Iterator[None]:
    """Shared during scoped execution; exclusive during approved configuration changes."""
    with state_lock("configuration", shared=not exclusive):
        yield


@contextlib.contextmanager
def state_lock(name: str, *, shared: bool = False) -> Iterator[None]:
    root = state_dir()
    _private_dir(root)
    path = root / (name + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OperationError("operation_busy", "Another gitpro operation holds the required lock.",
                                 "blocked") from None
        yield
    finally:
        os.close(fd)  # Do not unlink: waiting callers must continue to lock the same inode.


@contextlib.contextmanager
def locked_index(index: Path, expected_hash: Optional[str]) -> Iterator[Path]:
    """Freeze the ordinary index; Git and its hooks operate on a private copy."""
    lock = index.with_name(index.name + ".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise OperationError("index_busy", "Git's index is locked; no operation was started.") from None
    st = os.fstat(fd)
    os.close(fd)
    try:
        if file_hash(index) != expected_hash:
            raise OperationError("stale_plan", "The staged index changed; run preflight again.", "blocked")
        with tempfile.TemporaryDirectory(prefix="gitpro-index-") as tmp:
            private = Path(tmp) / "index"
            if index.exists():
                private.write_bytes(index.read_bytes())
            yield private
    finally:
        try:
            current = lock.lstat()
        except FileNotFoundError:
            current = None
        if current and (current.st_dev, current.st_ino) == (st.st_dev, st.st_ino):
            lock.unlink()


class Ledger:
    """A committed 'running' row precedes every write; unfinished rows are never auto-retried."""

    def __init__(self, readonly: bool = False) -> None:
        root = state_dir()
        path = root / "ledger.sqlite3"
        if readonly:
            if not path.exists():
                raise OperationError("unknown_request", "No operation has been recorded.", "needs_input")
            self.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        else:
            _private_dir(root)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(path, timeout=5)
            try:
                self.db.execute("PRAGMA synchronous=FULL")
                with self.db:
                    self.db.execute("CREATE TABLE IF NOT EXISTS operations "
                                    "(key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                                    "state TEXT NOT NULL, result TEXT)")
                    self.db.execute("CREATE TABLE IF NOT EXISTS operation_intents "
                                    "(key TEXT PRIMARY KEY, intent TEXT NOT NULL, checksum TEXT NOT NULL)")
                # Locks may have created this directory chain before the ledger was opened.
                for parent in (root, *root.parents):
                    sync_path(parent, directory=True)
            except (OSError, sqlite3.Error):
                self.db.close()
                raise

    def close(self) -> None:
        self.db.close()

    def get(self, key: str, fingerprint: Optional[str] = None) -> Optional[dict]:
        row = self.db.execute("SELECT fingerprint, state, result FROM operations WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        if fingerprint is not None and row[0] != fingerprint:
            raise OperationError("idempotency_conflict", "This key belongs to a different request or plan.", "blocked")
        if row[1] == "running":
            raise OperationError("outcome_unknown", "This request is running or was interrupted. "
                                 "Inspect its effects; do not retry it under a new key.")
        result = json.loads(row[2])
        return dict(result, replayed=True)

    def claim(self, key: str, fingerprint: str, *, intent: Optional[dict] = None) -> None:
        try:
            with self.db:
                self.db.execute("INSERT INTO operations VALUES (?, ?, 'running', NULL)", (key, fingerprint))
                if intent is not None:
                    self.db.execute("INSERT INTO operation_intents VALUES (?, ?, ?)",
                                    (key, json.dumps(intent), digest(intent)))
        except sqlite3.IntegrityError:
            raise OperationError("operation_busy", "This idempotency key was already claimed; query its status.") from None

    def inspect(self, key: str) -> dict:
        row = self.db.execute("SELECT fingerprint, state, result FROM operations WHERE key=?", (key,)).fetchone()
        if row is None:
            raise OperationError("unknown_request", "No operation has this key.", "needs_input")
        exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='operation_intents'").fetchone()
        saved = self.db.execute("SELECT intent, checksum FROM operation_intents WHERE key=?", (key,)).fetchone() if exists else None
        intent = json.loads(saved[0]) if saved else None
        if saved and (not isinstance(intent, dict) or digest(intent) != saved[1]):
            raise OperationError("journal_corrupt", "The recorded operation intent failed its integrity check.")
        return {"fingerprint": row[0], "state": row[1],
                "result": json.loads(row[2]) if row[2] is not None else None, "intent": intent}

    def finish(self, key: str, result: dict) -> None:
        with self.db:
            self.db.execute("UPDATE operations SET state = ?, result = ? WHERE key = ?",
                            (result["status"], json.dumps(result), key))
