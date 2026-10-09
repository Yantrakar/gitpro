"""`gitpro mcp`: read-only by default, with operator-opted-in agent operation execution.

The server speaks newline-delimited JSON-RPC 2.0 on stdin/stdout, by hand with the stdlib. Every tool
runs with stdout redirected to stderr, so a stray print never corrupts the stream; responses go to the
saved stdout handle. Preflight may explicitly opt into network reads. Execution tools require operator
opt-in; persistent changes additionally require human approval or an operator-selected scoped policy.
No tool records human approval or returns credential values.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import inspect
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, IO, List, Optional, Tuple

from . import __version__
from .config import ConfigError
from .operation_contract import REQUEST_SCHEMA
from .change_contract import REQUEST_SCHEMA as CHANGE_REQUEST_SCHEMA

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")  # newest last
STRUCTURED_SINCE = "2025-06-18"   # structuredContent only for clients at this version or newer
MAX_ROOTS = 20
CLI_TIMEOUT = 30                  # seconds for `claude mcp add|remove`

INSTRUCTIONS = ("gitpro's tools are read-only: they tell you which git identity (name, email, SSH key, forge "
                "account) a repository uses, why, and whether a commit would be blocked by gitpro's guard. They "
                "never change anything. To change something, show the user the fix command a tool returns and "
                "let them run it.")
ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

PARSE_ERROR, INVALID_REQUEST, NO_METHOD, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603


class ToolError(Exception):
    """A gitpro error a tool reports as a result with isError (the server carries on)."""


class RpcError(Exception):
    """A JSON-RPC error response."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---------------------------------------------------------------- tool helpers

class Ctx:
    """What a tool gets besides its arguments: -c and the config, loaded on first use (per tools/call)."""

    def __init__(self, cfg_path: Optional[Path] = None, allow_execution: bool = False,
                 approval_policy: Optional[Path] = None):
        self.cfg_path = Path(cfg_path) if cfg_path else None
        self.home = Path.home().resolve()
        self._cfg = None
        self.allow_execution = allow_execution
        self.approval_policy = approval_policy

    def config(self):
        """The config; ConfigError when there is none or it is invalid."""
        from . import config
        if self._cfg is None:
            path, how = config.find(self.cfg_path, self.home)
            self._cfg = config.load_all(path, how)
        return self._cfg

    def config_or_none(self):
        try:
            return self.config()
        except ConfigError:
            return None


def _absolute(raw: str) -> Path:
    p = Path(raw).expanduser()
    return (p if p.is_absolute() else Path.cwd() / p).resolve()


def _readable_dir(p: Path) -> bool:
    return p.is_dir() and os.access(p, os.R_OK | os.X_OK)


def _dir(raw: str) -> Path:
    """A client path that must be a directory the user can read."""
    p = _absolute(raw)
    if not _readable_dir(p):
        raise ToolError(f"{raw}: not a directory")
    return p


def _repo(raw: str) -> Path:
    """A client path that must be (inside) a git repository."""
    from . import resolve
    p = _absolute(raw)
    if not _readable_dir(p) or resolve.is_repo(p) is None:
        raise ToolError(f"{raw}: not a git repository")
    return p


def _envelope(command: str, obj: dict) -> dict:
    """{"ok", "command", **obj}; an "ok" inside obj wins (as jsonout.emit)."""
    out = {"ok": obj.get("ok", True), "command": command}
    out.update((k, v) for k, v in obj.items() if k not in ("ok", "command"))
    return out


def _cli(command: str, argv: List[str], cfg_path: Optional[Path] = None) -> dict:
    """Run `gitpro <command> <argv>` in-process (the parser is built per call, so extensions and the
    current HOME apply) and return its --json object. An error object becomes a ToolError."""
    from . import cli
    from .args import guarded
    parser, subs = cli.build_parser()
    if cfg_path and "-c" in subs[command]._option_string_actions:
        argv = ["-c", str(cfg_path), *argv]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            args = parser.parse_args([command, *argv])
            rc = guarded(args.func, args)
        except SystemExit as e:
            raise ToolError(f"gitpro {command}: invalid arguments (exit {e.code})") from None
    text = buf.getvalue()
    if not text.strip():
        raise ToolError(f"gitpro {command} printed nothing (exit {rc})")
    obj = json.loads(text)
    if obj.get("ok") is False and obj.get("error"):
        raise ToolError(str(obj["error"]))
    return obj


# ---------------------------------------------------------------- tools

def tool_which(args: dict, ctx: Ctx) -> dict:
    return _cli("which", ["--json", "--", str(_repo(args["path"]))])


def tool_why(args: dict, ctx: Ctx) -> dict:
    from . import resolve
    resolve.require_git()
    repo = _repo(args["path"])
    cfg = ctx.config()
    rep = resolve.compare(repo, cfg, remote=args.get("remote"), home=ctx.home)
    data = rep.to_json()
    data.pop("ok")
    return {"ok": rep.ok(), "command": "why", "repo": data.pop("repo"),
            "config": {"path": str(cfg.path), "how": cfg.how}, **data}


GUARD_CODES = ("override", "pin_stale", "no_identity", "env_override")   # 10 §2 step 5
CAUSE_CODES = ("url_form", "override", "pin_stale", "global_override", "not_applied", "env_override")


def _problem(code: str, message: str, fix: Optional[str]) -> dict:
    return {"code": code, "message": message, "fix": fix}


def _who(rep) -> str:
    e = rep.expected
    return f"identity {e.id or '[default]'} <{e.email}>"


def _where(rep) -> str:
    m = rep.expected.match
    return f"{m.remote} ({m.url})" if m.remote and m.url else str(rep.repo)


def _identity_problems(rep, cfg, args: dict) -> List[dict]:
    """The guard's committer/author judgement (06 §2): committer must be the expected email; the author
    only counts when it is one of the user's own emails."""
    from . import resolve
    expected = rep.expected.email
    committer = args.get("committer_email") or rep.effective.email.value or ""
    author = args.get("author_email") or committer
    if expected is None:
        return []
    out = []
    bad_committer, bad_author = resolve.identity_conflicts(expected, resolve.own_emails(cfg), author, committer)
    if bad_committer:
        cause = next((p for p in rep.problems if p.code in CAUSE_CODES), None)
        msg = (f"this commit would be committed as {committer or '(no email)'} but {_where(rep)} belongs to "
               f"{_who(rep)}" + (f". cause: {cause.message}" if cause else ""))
        fix = resolve._shown_fix(cause) if cause else f"gitpro why {shlex.quote(str(rep.repo))}"
        out.append(_problem("committer", msg, fix))
    a = author.casefold()
    if bad_author and a != committer.casefold():   # same as the committer: reported above
        who = f"{rep.expected.name} <{expected}>"
        out.append(_problem("author", f"the author would be {author}, your own other email; {_where(rep)} belongs "
                                      f"to {_who(rep)}", f"git commit --author={shlex.quote(who)} ..."))
    return out


def _trailer_problems(repo: Path, rep, cfg, message: str, warnings: List[str]) -> List[dict]:
    """Co-Authored-By policy for the message (08's trailers module); a warning when it cannot be checked."""
    try:
        from . import trailers
    except ImportError:
        warnings.append("message not checked: trailer rules need gitpro's audit package")
        return []
    try:
        r = subprocess.run(["git", "-C", str(repo), "interpret-trailers", "--parse"], input=message,
                           capture_output=True, text=True, timeout=10)
        pairs = [tuple(s.strip() for s in ln.split(":", 1)) for ln in r.stdout.splitlines() if ":" in ln]
        # 08 §11: trailers.policy_for(identity, default), trailers.check(trailers, policy) -> list[str]
        policy = _call_spec(trailers.policy_for, [("identity", rep.expected.match.identity),
                                                  ("default", cfg.default)])
        if hasattr(trailers, "violations"):   # 08: (message, fix) pairs, so a require violation gets its own fix
            found = [(str(m), str(f)) for m, f in trailers.violations(pairs, policy)]
        else:
            found = [(str(m), "remove the trailer from the message")
                     for m in _call_spec(trailers.check, [("trailers", pairs), ("policy", policy)]) or []]
    except Exception as e:  # noqa: BLE001 - another package's checker must not break the tool
        warnings.append(f"message not checked: {type(e).__name__}")
        return []
    return [_problem("trailer", m, f) for m, f in found]


def tool_check_commit(args: dict, ctx: Ctx) -> dict:
    from . import resolve
    resolve.require_git()
    repo = _repo(args["path"])
    cfg = ctx.config()
    rep = resolve.compare(repo, cfg, remote=args.get("remote"), home=ctx.home)
    probs = _identity_problems(rep, cfg, args)
    probs += [_problem(p.code, p.message, resolve._shown_fix(p) or None) for p in rep.problems
              if p.code in GUARD_CODES]
    warnings: List[str] = []
    if args.get("message") is not None:
        probs += _trailer_problems(rep.repo, rep, cfg, args["message"], warnings)
    mode = cfg.agents.hook
    out = {"ok": not probs, "command": "check_commit", "would_block": bool(probs) and mode == "block",
           "mode": mode, "problems": probs, "reason": probs[0]["message"] if probs else None,
           "fix": probs[0]["fix"] if probs else None}
    if warnings:
        out["warnings"] = warnings
    return out


def _capped(values) -> List[str]:
    values = list(values or ())
    if len(values) > MAX_ROOTS:
        raise RpcError(INVALID_PARAMS, f"at most {MAX_ROOTS} entries")
    return values


def tool_scan(args: dict, ctx: Ctx) -> dict:
    roots = [str(_dir(r)) for r in _capped(args.get("roots"))]
    argv = ["--json"]
    for r in _capped(args.get("repos")):
        argv += ["--repo", str(_dir(r))]
    if roots:
        argv += ["--", *roots]
    return _cli("scan", argv, ctx.cfg_path)


def tool_env(args: dict, ctx: Ctx) -> dict:
    repo = _repo(args["path"])
    return _cli("env", ["--json", "--shell", args.get("shell", "sh"), "--", str(repo)], ctx.cfg_path)


def tool_preflight(args: dict, ctx: Ctx) -> dict:
    from .operations import preflight
    return preflight(args["request"], ctx.cfg_path, online=args.get("online", False))


def tool_run_operation(args: dict, ctx: Ctx) -> dict:
    from .operations import execute
    return execute(args["request"], args["plan_token"], args["idempotency_key"], ctx.cfg_path,
                   online=args.get("online", False), authorized=ctx.allow_execution)


def tool_operation_status(args: dict, ctx: Ctx) -> dict:
    from .operations import status
    return status(args["idempotency_key"])


def tool_propose_change(args: dict, ctx: Ctx) -> dict:
    from .changes import propose
    return propose(args["request"], ctx.cfg_path, policy_path=ctx.approval_policy)


def tool_change_status(args: dict, ctx: Ctx) -> dict:
    from .changes import status
    return status(args["plan_id"])


def tool_apply_change(args: dict, ctx: Ctx) -> dict:
    from .changes import apply
    return apply(args["plan_id"], authorized=ctx.allow_execution, raw=args.get("request"),
                 policy_path=ctx.approval_policy, cfg_path=ctx.cfg_path)


def tool_recover_change(args: dict, ctx: Ctx) -> dict:
    from .changes import apply
    return apply(args["plan_id"], authorized=ctx.allow_execution, recover=True)


def tool_reconcile_operation(args: dict, ctx: Ctx) -> dict:
    from .operation_reconcile import reconcile
    return reconcile(args["idempotency_key"], online=args.get("online", False))


LOST_KINDS = ("unpushed", "merged_gone", "stash", "dirty", "no_remote", "duplicate", "stale", "empty")  # offline


def _scan_config(ctx: Ctx):
    """(full config or None, its [scan] table or None), as `gitpro lost` reads them: a config whose other
    tables are broken still gives its [scan]."""
    from . import config
    cfg = ctx.config_or_none()
    if cfg is not None:
        return cfg, getattr(cfg, "scan", None)
    load_scan = getattr(config, "load_scan", None)
    if load_scan is None:
        return None, None
    try:
        path, _how = config.find(ctx.cfg_path, ctx.home)
        return None, load_scan(path)
    except ConfigError:
        return None, None


def _lost_roots(dirs: List[Path], scan_cfg) -> Tuple[List[Path], List[str], str]:
    """(roots, exclude, roots_source) as `gitpro lost` picks them (03 §2.1, 02's scan.effective_roots)."""
    try:
        from .scan import effective_roots  # type: ignore[attr-defined]  (added by 02)
    except ImportError:
        effective_roots = None
    if effective_roots is not None:
        roots, exclude, source = effective_roots([str(d) for d in dirs], scan_cfg)
        return [Path(r) for r in roots], list(exclude), str(source)
    if dirs:
        raw, source = [str(d) for d in dirs], "args"
    elif scan_cfg is not None and getattr(scan_cfg, "roots", None):
        raw, source = list(scan_cfg.roots), "config"
    else:
        raw, source = ["~"], "default"
    out: List[Path] = []
    for r in raw:
        p = Path(r).expanduser().resolve()
        if p not in out:
            out.append(p)
    return out, list(getattr(scan_cfg, "exclude", None) or ()), source


def tool_lost(args: dict, ctx: Ctx) -> dict:
    from . import lost  # type: ignore[attr-defined]
    dirs = [_dir(r) for r in _capped(args.get("roots"))]
    cfg, scan_cfg = _scan_config(ctx)
    roots, exclude, source = _lost_roots(dirs, scan_cfg)
    offline = getattr(lost, "OFFLINE_KINDS", LOST_KINDS)
    kinds = set(args.get("only") or offline)      # a set: 03 tests membership and intersects it
    stale = getattr(scan_cfg, "stale_days", None)
    rep = lost.collect(roots, exclude, cfg, online=False, kinds=kinds,
                       stale_days=int(stale) if stale else 180, roots_source=source)
    return _envelope("lost", rep)


def _call_spec(fn: Callable, values: List[Tuple[str, object]]):
    """fn called with values, which follow the spec's positional order (name, value). Tolerant of how the
    other package declares them: keyword-only parameters get their value by name, and positional ones by
    name when every name matches, else by position."""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return fn(*[v for _, v in values])
    named = dict(values)
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    kwargs = {p.name: named[p.name] for p in params if p.kind == p.KEYWORD_ONLY and p.name in named}
    if all(p.name in named for p in positional):
        return fn(*[named[p.name] for p in positional], **kwargs)
    rest = [v for k, v in values if k not in kwargs]
    return fn(*rest[:len(positional)], **kwargs)


def tool_audit(args: dict, ctx: Ctx) -> dict:
    from . import audit  # type: ignore[attr-defined]
    repo = _repo(args["path"])
    rng = args.get("range")
    if rng is not None and rng.startswith("-"):
        raise ToolError("range must not start with '-'")
    # 08 §11: audit.collect(repo, cfg, range, all) -> dict
    rep = _call_spec(audit.collect, [("repo", repo), ("cfg", ctx.config()), ("range", rng),
                                     ("all", bool(args.get("all")))])
    return _envelope("audit", rep)


def tool_identities(args: dict, ctx: Ctx) -> dict:
    from . import config
    cfg = ctx.config()
    d = cfg.default
    return {"ok": True, "command": "identities", "config": str(cfg.path),
            "default": {"name": d.name, "email": d.email, "gh_account": d.gh_account,
                        "glab_account": d.glab_account, "clone_dir": d.clone_dir, "sign": d.sign},
            "identities": [{"id": i.id, "name": i.name, "email": i.email, "host": i.host, "owners": list(i.owners),
                            "ssh_alias": i.ssh_alias, "gh_account": i.gh_account, "glab_account": i.glab_account,
                            "clone_dir": i.clone_dir, "sign": config.signs(i, d)} for i in cfg.identities]}


# ---------------------------------------------------------------- the registry

def _schema(props: dict, required: Tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _string(desc: str, **extra) -> dict:
    return {"type": "string", "minLength": 1, "description": desc, **extra}


def _strings(desc: str, **items) -> dict:
    return {"type": "array", "items": {"type": "string", "minLength": 1, **items}, "maxItems": MAX_ROOTS,
            "description": desc}


PATH = _string("a directory inside the git repository (absolute, or relative to the server's directory)")


class Tool:
    """One MCP tool; `fn(args, ctx) -> dict` is looked up per call, so tests can replace it."""

    def __init__(self, name: str, description: str, schema: dict, fn: Callable[[dict, Ctx], dict],
                 module: Optional[str] = None, execution: bool = False):
        self.name, self.description, self.schema, self.fn, self.module = name, description, schema, fn, module
        self.execution = execution

    def available(self) -> bool:
        """False when the module behind the tool is not installed (or blocked with sys.modules[...] = None)."""
        if self.module is None:
            return True
        try:
            importlib.import_module(self.module)
        except ImportError:
            return False
        return True

    def listing(self) -> dict:
        return {"name": self.name, "description": self.description, "inputSchema": self.schema,
                "annotations": ({**ANNOTATIONS, "readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}
                                if self.execution else {**ANNOTATIONS, "openWorldHint":
                                                        self.name in ("preflight", "reconcile_operation")})}


TOOLS: Dict[str, Tool] = {t.name: t for t in (
    Tool("which_identity", "The git identity settings (user.name, user.email, signing, core.sshCommand) a repo "
         "uses right now and where each comes from; same as `gitpro which --json`.",
         _schema({"path": PATH}, ("path",)), tool_which),
    Tool("why", "Which gitpro identity a repo should use, which it really uses, why, and the fix command for "
         "every problem; same as `gitpro why --json`.",
         _schema({"path": PATH, "remote": _string("judge against this remote (name or url) instead of origin")},
                 ("path",)), tool_why),
    Tool("check_commit", "Would gitpro's guard block this commit? Judges the committer (default: the repo's "
         "effective email) and author emails against the repo's identity and returns the reason and fix.",
         _schema({"path": PATH, "author_email": _string("the commit's author email (default: the committer)"),
                  "committer_email": _string("the committer email (default: what git would use in the repo)"),
                  "remote": _string("judge against this remote (name or url) instead of origin"),
                  "message": {"type": "string", "description": "the commit message, for the Co-Authored-By rules"}},
                 ("path",)), tool_check_commit),
    Tool("scan", "List repositories under the given roots (or only the given repos) and the identity git uses "
         "in each; never fixes anything. Same as `gitpro scan --json`.",
         _schema({"roots": _strings("directories to search (default: the configured roots)"),
                  "repos": _strings("only these repositories, not the repos under them")}), tool_scan),
    Tool("env_for", "Shell lines that set GH_TOKEN etc. for the repo's identity: the commands that fetch the "
         "tokens, never their values. Same as `gitpro env --json`.",
         _schema({"path": PATH, "shell": {"type": "string", "enum": ["sh", "fish", "pwsh"],
                                          "description": "shell syntax (default: sh)"}}, ("path",)),
         tool_env, "gitpro.envcmd"),
    Tool("lost", "Unpushed branches, stashes, dirty trees, repos without a remote and duplicate clones under "
         "the roots (offline checks only). Same as `gitpro lost --json`.",
         _schema({"roots": _strings("directories to search (default: the configured roots)"),
                  "only": _strings("only these kinds", enum=list(LOST_KINDS))}), tool_lost, "gitpro.lost"),
    Tool("audit", "Commits whose author or committer is not the repo's identity, and agent trailers; never "
         "rewrites. Same as `gitpro audit --json`.",
         _schema({"path": PATH, "range": _string("a git revision range (default: the unpushed commits)"),
                  "all": {"type": "boolean", "description": "every commit on the branch"}}, ("path",)),
         tool_audit, "gitpro.audit"),
    Tool("identities", "The configured identities (name, email, host, owners, forge accounts, signing); no key "
         "contents, no extra git settings.", _schema({}), tool_identities),
    Tool("preflight", "Versioned operation decision and state token for commit, push or GitHub PR creation. "
         "Read-only; online=true explicitly permits account/destination reads. Ready is not authorization.",
         _schema({"request": REQUEST_SCHEMA, "online": {"type": "boolean"}}, ("request",)), tool_preflight),
    Tool("operation_status", "Read the durable result of an idempotency key. Never executes or retries.",
         _schema({"idempotency_key": _string("the original operation key")}, ("idempotency_key",)),
         tool_operation_status),
    Tool("reconcile_operation", "Read-only inspection of recorded commit/push/PR intent versus current state. "
         "Remote reads require online=true. Does not retry writes, change the ledger outcome or prove attribution.",
         _schema({"idempotency_key": _string("the original operation key"), "online": {"type": "boolean"}},
                 ("idempotency_key",)), tool_reconcile_operation),
    Tool("propose_change", "Read-only exact configuration/repair proposal. Does not record approval or change "
         "target files. Reports coverage by the operator's startup policy; otherwise ask a human to review "
         "its JSON with gitpro change approve --write in their terminal.",
         _schema({"request": CHANGE_REQUEST_SCHEMA}, ("request",)), tool_propose_change),
    Tool("change_status", "Read the approval/apply/recovery status of a persistent change. Does not execute.",
         _schema({"plan_id": _string("the complete change plan ID")}, ("plan_id",)), tool_change_status),
    Tool("apply_change", "Apply an exact persistent change with human approval or a covering operator policy. "
         "Provide request only to obtain a scoped grant from the server's startup policy. Host authorization "
         "is still required; clients cannot choose policies or approve themselves.",
         _schema({"plan_id": _string("the complete plan ID"), "request": CHANGE_REQUEST_SCHEMA}, ("plan_id",)),
         tool_apply_change, execution=True),
    Tool("recover_change", "Recover an interrupted approved change by restoring its recorded backups only "
         "where current files match the planned before/after contents and modes. Never retries forward writes.",
         _schema({"plan_id": _string("the interrupted approved plan ID")}, ("plan_id",)),
         tool_recover_change, execution=True),
    Tool("run_operation", "EXECUTES a reviewed commit, push or GitHub PR operation; requires host/user approval. "
         "Never edits identity configuration. Reuse the exact request, plan_token and idempotency_key for retries. "
         "Unknown outcomes must be inspected, not retried under a new key.",
         _schema({"request": REQUEST_SCHEMA, "plan_token": _string("the reviewed ready preflight token"),
                  "idempotency_key": _string("stable unique operation key"), "online": {"type": "boolean"}},
                 ("request", "plan_token", "idempotency_key")), tool_run_operation, execution=True),
)}


def _check_value(name: str, spec: dict, value: object) -> None:
    kind = spec["type"]
    if kind == "string":
        good = isinstance(value, str) and "\0" not in value and len(value) >= spec.get("minLength", 0)
    elif kind == "boolean":
        good = isinstance(value, bool)
    elif kind == "integer":
        good = type(value) is int
    elif kind == "object":
        good = isinstance(value, dict)
        if good:
            validate(spec, value)
    elif kind == "array":
        if isinstance(value, list) and len(value) > spec.get("maxItems", len(value)):
            raise RpcError(INVALID_PARAMS, f"'{name}': at most {spec['maxItems']} entries")
        good = isinstance(value, list)
        for v in value if good else ():
            _check_value(name, spec["items"], v)
    else:
        good = False
    if not good:
        article = "an" if kind[0] in "aeiou" else "a"
        raise RpcError(INVALID_PARAMS, f"'{name}' must be {article} {kind}"
                       + (" (non-empty)" if kind == "string" else ""))
    if kind == "string" and len(value) > spec.get("maxLength", len(value)):
        raise RpcError(INVALID_PARAMS, f"'{name}' is too long")
    if "enum" in spec and value not in spec["enum"]:
        raise RpcError(INVALID_PARAMS, f"'{name}' must be one of {', '.join(map(str, spec['enum']))}")


def validate(schema: dict, args: object) -> None:
    """The subset of JSON Schema the tools use; RpcError -32602 when args do not fit."""
    if not isinstance(args, dict):
        raise RpcError(INVALID_PARAMS, "arguments must be an object")
    for key in schema["required"]:
        if key not in args:
            raise RpcError(INVALID_PARAMS, f"missing argument '{key}'")
    for key, value in args.items():
        if key not in schema["properties"]:
            raise RpcError(INVALID_PARAMS, f"unknown argument '{key}'")
        _check_value(key, schema["properties"][key], value)


# ---------------------------------------------------------------- the server

def redact(value: object) -> object:
    """value with a password or token in any URL userinfo replaced by *** (resolve.redact_url), in every
    string and key, however deep: the backstop that keeps credentials in remote URLs from the model."""
    from .resolve import redact_text
    if isinstance(value, str):
        return redact_text(value) if "@" in value else value
    if isinstance(value, dict):
        return {redact(k): redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def _open_log(path: Path) -> IO[str]:
    fd = os.open(str(Path(path).expanduser()), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


class Server:
    """One client session: the negotiated version, the output handle, the optional log."""

    def __init__(self, out: IO[str], cfg_path: Optional[Path], log: Optional[IO[str]] = None,
                 allow_execution: bool = False, approval_policy: Optional[Path] = None):
        self.out, self.cfg_path, self.log = out, cfg_path, log
        self.allow_execution = allow_execution
        self.approval_policy = approval_policy
        self.version = PROTOCOL_VERSIONS[-1]

    def note(self, arrow: str, text: str) -> None:
        if not self.log:
            return
        try:
            self.log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {arrow} {text.rstrip()}\n")
            self.log.flush()
        except (OSError, ValueError) as e:   # disk full, file gone: the session goes on without the log
            self.log = None
            print(f"⚠ gitpro mcp: --log stopped ({type(e).__name__}: {e})", file=sys.stderr)

    def send(self, msg: dict) -> None:
        text = json.dumps(msg)   # ASCII escapes: the stream never depends on an encoding
        self.note("→", text)
        self.out.write(text + "\n")
        self.out.flush()

    def handle(self, line: str) -> Optional[dict]:
        """The response to one line, or None (notifications, responses sent to us)."""
        self.note("←", line)
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):      # RecursionError: nesting deeper than the parser allows
            return _error(None, PARSE_ERROR, "parse error")
        if not isinstance(msg, dict):
            return _error(None, INVALID_REQUEST, "invalid request: not an object")
        method = msg.get("method")
        if not isinstance(method, str) and ("result" in msg or "error" in msg):
            return None                            # a response to a request we never send
        is_request = "id" in msg
        mid = msg.get("id")
        if is_request and (mid is None or isinstance(mid, bool) or not isinstance(mid, (str, int))):
            return _error(None, INVALID_REQUEST, "invalid request: id must be a string or an integer")
        if msg.get("jsonrpc") != "2.0":
            return _error(mid, INVALID_REQUEST, 'invalid request: "jsonrpc" must be "2.0"') if is_request else None
        if not isinstance(method, str):
            return _error(mid, INVALID_REQUEST, "invalid request: no method")
        if not is_request:
            return None
        try:
            return {"jsonrpc": "2.0", "id": mid, "result": self.dispatch(method, msg.get("params"))}
        except RpcError as e:
            return _error(mid, e.code, e.message)

    def dispatch(self, method: str, params: object) -> dict:
        params = {} if params is None else params
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
            return {"protocolVersion": self.version, "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "gitpro", "version": __version__},
                    "instructions": (INSTRUCTIONS if not self.allow_execution else
                                     "Execution tools are enabled. Obtain host/user approval before each write. "
                                     "A preflight token checks state, not permission. apply_change and "
                                     "recover_change additionally require recorded approval. apply_change can "
                                     "use a covering operator startup policy, never a client-selected policy. "
                                     "There is no approval tool. Other tools remain read-only.")}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t.listing() for t in TOOLS.values()
                              if t.available() and (not t.execution or self.allow_execution)]}
        if method == "tools/call":
            return self.call(params)
        empty = {"resources/list": "resources", "resources/templates/list": "resourceTemplates",
                 "prompts/list": "prompts"}
        if method in empty:
            return {empty[method]: []}
        raise RpcError(NO_METHOD, f"method not found: {method}")

    def call(self, params: dict) -> dict:
        name = params.get("name")
        tool = TOOLS.get(name) if isinstance(name, str) else None
        if tool is None or not tool.available() or (tool.execution and not self.allow_execution):
            raise RpcError(INVALID_PARAMS, f"unknown tool: {name}")
        args = params.get("arguments")
        args = {} if args is None else args
        validate(tool.schema, args)
        print(f"gitpro mcp: tools/call {tool.name}", file=sys.stderr)   # the tool only, never its arguments
        try:
            with contextlib.redirect_stdout(sys.stderr):
                obj = tool.fn(args, Ctx(self.cfg_path, self.allow_execution, self.approval_policy))
            return self.result(obj)
        except RpcError:
            raise
        except (SystemExit, KeyboardInterrupt) as e:   # one request never ends the session
            code = getattr(e, "code", None)
            return self.failed(f"internal error: the tool stopped ({type(e).__name__}"
                               + (f" {code}" if code not in (None, 0) else "") + ")")
        except (ToolError, ConfigError, OSError, UnicodeError) as e:
            return self.failed(str(e))
        except subprocess.CalledProcessError as e:
            cmd = e.cmd if isinstance(e.cmd, str) else shlex.join(str(c) for c in e.cmd)
            return self.failed(f"{cmd} failed ({e.returncode})")
        except Exception as e:  # noqa: BLE001 - a tool must never kill the server
            tb = traceback.format_exc()
            sys.stderr.write(tb)
            self.note("!", tb)
            return self.failed(f"internal error: {type(e).__name__}")

    def result(self, obj: dict) -> dict:
        """The tools/call result; TypeError/ValueError when obj is not plain JSON (call() makes that isError)."""
        if not isinstance(obj, dict):
            raise TypeError(f"tool returned {type(obj).__name__}, not an object")
        obj = redact(obj)
        out = {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False, allow_nan=False)}],
               "isError": False}
        if self.version >= STRUCTURED_SINCE:
            out["structuredContent"] = obj
        return out

    @staticmethod
    def failed(message: str) -> dict:
        return {"content": [{"type": "text", "text": f"✗ {message}"}], "isError": True}


def _error(mid: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(stdin: IO[str], stdout: IO[str], cfg_path: Optional[Path] = None, log: Optional[Path] = None,
          allow_execution: bool = False, approval_policy: Optional[Path] = None) -> int:
    """Answer JSON-RPC lines from stdin on stdout until EOF; returns 0."""
    logf = _open_log(log) if log else None
    old = os.environ.get("GIT_TERMINAL_PROMPT")
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    server = Server(stdout, cfg_path, logf, allow_execution, approval_policy)
    try:
        while True:
            line = stdin.readline()
            if not line:
                return 0
            if line.strip():
                try:
                    reply = server.handle(line)
                except Exception as e:  # noqa: BLE001 - one bad request never ends the session
                    sys.stderr.write(traceback.format_exc())
                    reply = _error(None, INTERNAL_ERROR, f"internal error: {type(e).__name__}")
                if reply is not None:
                    server.send(reply)
    finally:
        if old is None:
            os.environ.pop("GIT_TERMINAL_PROMPT", None)
        else:
            os.environ["GIT_TERMINAL_PROMPT"] = old
        if logf:
            logf.close()


def _private_streams() -> Tuple[IO[str], IO[str]]:
    """The protocol streams on private copies of fds 0 and 1. fd 0 becomes /dev/null and fd 1 a copy of
    stderr, so no child process and no stray write can read or corrupt the stream."""
    sys.stdout.flush()
    try:
        rfd, wfd = os.dup(0), os.dup(1)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
        os.dup2(2, 1)
    except OSError:
        return sys.stdin, sys.stdout
    return (open(rfd, "r", encoding="utf-8", errors="replace"),
            open(wfd, "w", encoding="utf-8", newline="\n"))


def cmd_mcp(args: argparse.Namespace) -> int:
    from .operation_io import termination_signals
    stdin, stdout = _private_streams()
    with termination_signals():
        return serve(stdin, stdout, args.config, args.log, args.allow_execution, args.approval_policy)


# ---------------------------------------------------------------- install / uninstall

AGENTS = ("claude", "claude-desktop", "cursor", "gemini", "opencode")
ENTRY = {"type": "stdio", "command": "gitpro", "args": ["mcp"]}
PLAIN_ENTRY = {"command": "gitpro", "args": ["mcp"]}               # formats that document no "type"
OPENCODE_ENTRY = {"type": "local", "command": ["gitpro", "mcp"]}
PROJECT_FILES = {"claude": ".mcp.json", "cursor": ".cursor/mcp.json"}
CLAUDE_ADD = ["claude", "mcp", "add", "--scope", "user", "--transport", "stdio", "gitpro", "--", "gitpro", "mcp"]
CLAUDE_REMOVE = ["claude", "mcp", "remove", "--scope", "user", "gitpro"]


def _user_target(agent: str, home: Path) -> Tuple[Path, str, dict]:
    """(file, servers key, entry) for an agent's user-level config."""
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    if agent == "claude":
        return home / ".claude.json", "mcpServers", ENTRY
    if agent == "claude-desktop":
        base = home / "Library/Application Support/Claude" if sys.platform == "darwin" else xdg / "Claude"
        return base / "claude_desktop_config.json", "mcpServers", PLAIN_ENTRY
    if agent == "cursor":
        return home / ".cursor/mcp.json", "mcpServers", ENTRY
    if agent == "gemini":
        return home / ".gemini/settings.json", "mcpServers", PLAIN_ENTRY
    return xdg / "opencode/opencode.json", "mcp", OPENCODE_ENTRY


def _target(args: argparse.Namespace) -> Tuple[Path, str, dict]:
    if args.scope == "user":
        return _user_target(args.agent, Path(args.home).expanduser())
    from . import resolve
    rel = PROJECT_FILES.get(args.agent)
    if rel is None:
        raise ConfigError(f"{args.agent} has no project-level config; use --scope user")
    if not args.write:
        raise ConfigError(f"{rel} is shared with your team; re-run with --write to edit it (backup + undo), "
                          "or use --scope user")
    top = resolve.is_repo(Path.cwd())
    if top is None:
        raise ConfigError(f"{Path.cwd()}: not a git repository (--scope project edits the repo's {rel})")
    return top / rel, "mcpServers", ENTRY


def _ours(value: object, entry: dict) -> bool:
    """True when value is gitpro's own server entry (extra keys such as "env" are allowed)."""
    return isinstance(value, dict) and value.get("command") == entry["command"] and value.get("args") == entry.get("args")


def _precheck(path: Path, key: str, snippet: str) -> Optional[dict]:
    """The file's JSON (None when missing); ConfigError for anything gitpro must not edit."""
    from . import jsonfile, undo
    shown = undo.tilde(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        if jsonfile.strip_jsonc(text)[1]:
            raise ConfigError(f"{shown} has comments; gitpro cannot edit it without losing them"
                              f" — add this by hand: {snippet}") from None
        raise ConfigError(f"{shown} is not valid JSON; fix it by hand (nothing written)") from None
    if not isinstance(obj, dict):
        raise ConfigError(f"{shown} is not a JSON object; fix it by hand (nothing written)")
    if not isinstance(obj.get(key, {}), dict):
        raise ConfigError(f"{shown}: {key} is not an object; fix it by hand (nothing written)")
    return obj


def _confirm_with(args: argparse.Namespace, question: str) -> Callable[[str], bool]:
    """A confirm callback that asks at most once: after a yes (e.g. to `claude mcp add`, before the
    fallback edits the file) later calls show their diff and go ahead."""
    said_yes = []

    def confirm(diff: str) -> bool:
        if args.yes:
            return True
        if not said_yes and not sys.stdin.isatty():
            raise ConfigError("no terminal to confirm on; re-run with --yes")
        from . import view
        if diff:
            print(diff, end="" if diff.endswith("\n") else "\n")
        if said_yes:
            return True
        ok = view.confirm(question)
        if ok:
            said_yes.append(True)
        return ok
    return confirm


def _foreign(path: Path, current: object) -> str:
    cmd = current.get("command") if isinstance(current, dict) else None
    return f"{_tilde_path(path)} already has a 'gitpro' server that is not gitpro's own" + \
        (f" (command {json.dumps(cmd)})" if cmd is not None else "")


def _tilde_path(path: Path) -> str:
    from . import undo
    return undo.tilde(path)


def _claude_cli(args: argparse.Namespace, removing: bool, confirm: Callable[[str], bool]) -> Optional[dict]:
    """Register/unregister through `claude mcp add|remove` (the CLI owns ~/.claude.json). The outcome, or
    None when the caller should edit the file itself (the CLI failed)."""
    from . import undo, view
    argv, back = (CLAUDE_REMOVE, CLAUDE_ADD) if removing else (CLAUDE_ADD, CLAUDE_REMOVE)
    if not confirm(f"run: {shlex.join(argv)}\n"):
        return {"changed": False, "aborted": True}
    command = "mcp uninstall" if removing else "mcp install"
    script = undo.write(command, [shlex.join(back)], what=f"ran {shlex.join(argv[:3])}")
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=CLI_TIMEOUT)
        rc: object = r.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        rc = "timeout" if isinstance(e, subprocess.TimeoutExpired) else type(e).__name__
    if rc == 0:
        return {"changed": True, "backup": None, "undo": script, "via": "claude-cli"}
    script.unlink()
    view.warn(f"{shlex.join(argv[:3])} failed ({rc}); editing ~/.claude.json directly")
    return None


def _file_edit(args, path: Path, key: str, entry: dict, removing: bool, confirm, snippet: str) -> dict:
    from . import jsonfile, view

    def mutate(obj):
        if not isinstance(obj, dict) or not isinstance(obj.get(key, {}), dict):
            raise ConfigError(f"{_tilde_path(path)}: {key} is not an object; fix it by hand (nothing written)")
        servers = obj.setdefault(key, {})
        current = servers.get("gitpro")
        # checked again on the content edit_json re-read: the claude CLI may have written it meanwhile
        if current is not None and not _ours(current, entry) and not getattr(args, "force", False):
            raise ConfigError(_foreign(path, current) + ("; left alone" if removing else
                                                          "; re-run with --force to replace it"))
        if removing:
            servers.pop("gitpro", None)
            if not servers and args.scope == "project":
                del obj[key]            # the team file keeps no empty "mcpServers": {}
        else:
            servers["gitpro"] = entry
        return obj
    if args.agent == "claude" and args.scope == "user":
        # R-036: a running Claude Code rewrites ~/.claude.json constantly
        view.warn("close Claude Code first: it rewrites ~/.claude.json while it runs and could undo this edit")
    res = jsonfile.edit_json(path, mutate, command="mcp uninstall" if removing else "mcp install", jsonc=False,
                             create={}, confirm=confirm, mkdir=True, snippet=snippet)
    return {"changed": res.changed, "aborted": not res.changed, "backup": res.backup, "undo": res.undo,
            "via": "file"}


def _report(args, path: Path, removing: bool, outcome: dict) -> int:
    from . import jsonout, undo, view
    command = "mcp uninstall" if removing else "mcp install"
    if args.json:
        jsonout.emit(command, {"file": str(path), "changed": outcome["changed"],
                               "backup": str(outcome["backup"]) if outcome.get("backup") else None,
                               "undo": str(outcome["undo"]) if outcome.get("undo") else None,
                               "via": outcome.get("via", "file")}, ok=not outcome.get("aborted"))
        return 1 if outcome.get("aborted") else 0
    if outcome.get("aborted"):
        view.aborted()
        return 1
    if not outcome["changed"]:
        view.ok(f"{'not registered' if removing else 'already registered'} in {undo.tilde(path)}, nothing to do")
        return 0
    how = "with claude mcp " + ("remove" if removing else "add") if outcome["via"] == "claude-cli" \
        else f"in {undo.tilde(path)}"
    view.ok(f"{'removed' if removing else 'registered'} the gitpro MCP server {how}")
    restores = (f"restores the whole file from {undo.tilde(outcome['backup'])}; changes made to it later, "
                "e.g. by Claude Code, are lost") if outcome.get("backup") else \
        ("re-registers it" if removing else "removes it")
    print(f"↩ undo: sh {undo.tilde(outcome['undo'])}   ({restores})")
    return 0


def _edit(args: argparse.Namespace, removing: bool) -> int:
    from .args import fail, preview_only, preview_result
    from . import view
    path, key, entry = _target(args)
    if args.agent == "claude" and not removing and not args.json:
        view.step("plugin users already have the server (the gitpro plugin ships .mcp.json)")
    snippet = json.dumps({key: {"gitpro": entry}})
    obj = _precheck(path, key, snippet)
    current = (obj or {}).get(key, {}).get("gitpro")
    if removing and current is not None and not _ours(current, entry):
        view.warn(f"{_foreign(path, current)}; left alone")
        return 1
    if not removing and current is not None and not _ours(current, entry) and not getattr(args, "force", False):
        return fail(f"{_foreign(path, current)}; re-run with --force to replace it", args)
    if (current is None) if removing else _ours(current, entry):
        return _report(args, path, removing, {"changed": False, "via": "file"})
    if preview_only(args):
        if not args.json:
            print(f"Would {'remove' if removing else 'register'} gitpro in {path}:\n{snippet}")
        return preview_result(args, "mcp uninstall" if removing else "mcp install",
                              {"file": str(path), "changed": False, "entry": entry,
                               "action": "remove" if removing else "install", "backup": None, "undo": None})
    confirm = _confirm_with(args, "Remove it?" if removing else "Register it?")
    outcome = None
    if args.agent == "claude" and args.scope == "user" and shutil.which("claude"):
        outcome = _claude_cli(args, removing, confirm)
    if outcome is None:
        outcome = _file_edit(args, path, key, entry, removing, confirm, snippet)
    return _report(args, path, removing, outcome)


def cmd_install(args: argparse.Namespace) -> int:
    return _edit(args, removing=False)


def cmd_uninstall(args: argparse.Namespace) -> int:
    return _edit(args, removing=True)


# ---------------------------------------------------------------- doctor

def _shown(path: Path, home: Path) -> str:
    p, h = str(path), str(home)
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def status_data(home: Path) -> dict:
    """Which agents' user configs register the gitpro server; reads files only (never runs `claude`)."""
    from . import jsonfile
    home = Path(home)
    found, problems = [], []
    for agent in AGENTS:
        path, key, entry = _user_target(agent, home)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as e:
            problems.append({"message": f"cannot read {_shown(path, home)} ({e.strerror})", "fix": "",
                             "level": "warning"})
            continue
        try:
            obj = json.loads(jsonfile.strip_jsonc(text)[0])
        except ValueError:
            problems.append({"message": f"{_shown(path, home)} is not valid JSON; {agent} cannot load its MCP "
                                        "servers", "fix": f"fix {_shown(path, home)} by hand", "level": "warning"})
            continue
        servers = obj.get(key) if isinstance(obj, dict) else None
        if isinstance(servers, dict) and _ours(servers.get("gitpro"), entry):
            found.append({"agent": agent, "file": str(path)})
    summary = ("mcp registered: " + ", ".join(f"{f['agent']} ({_shown(Path(f['file']), home)})" for f in found)
               if found else "mcp not registered (optional: gitpro mcp install)")
    return {"status": "warning" if problems else "ok", "summary": summary, "registered": found,
            "problems": problems, "details": [f"mcp: {f['agent']} → {_shown(Path(f['file']), home)}" for f in found]}


def doctor_section(ctx) -> dict:
    data = status_data(ctx.home)
    return {k: data[k] for k in ("status", "summary", "problems", "details")}


# ---------------------------------------------------------------- CLI

def register(sub, common, confirm) -> None:
    from .args import capability
    p = sub.add_parser("mcp", parents=[common],
                       help="run the read-only MCP server for AI assistants (stdio); install registers it")
    p.add_argument("--log", type=Path, metavar="FILE", help="append every request and response to FILE (0600)")
    p.add_argument("--allow-execution", action="store_true",
                   help="expose operation and approved-change execution; the host must authorize each write")
    p.add_argument("--approval-policy", type=Path, help="operator-selected scoped grants; never a client argument")
    p.set_defaults(func=cmd_mcp)
    actions = p.add_subparsers(dest="mcp_action", metavar="ACTION")
    for name, func, text in (("install", cmd_install, "register the server with an AI assistant (backup + undo)"),
                             ("uninstall", cmd_uninstall, "remove the registration (backup + undo)")):
        a = actions.add_parser(name, parents=[confirm], help=text)
        a.add_argument("--agent", choices=AGENTS, default="claude", help="which assistant (default: claude)")
        a.add_argument("--scope", choices=("user", "project"), default="user",
                       help="user config (default) or the repo's tracked file (claude, cursor; needs --write)")
        a.add_argument("--json", action="store_true", help="machine-readable output (needs -y)")
        if name == "install":
            a.add_argument("--force", action="store_true",
                           help="replace a 'gitpro' server entry that is not gitpro's own")
        a.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
        a.set_defaults(func=func, command=f"mcp {name}")
    capability("mcp", "mcp.install")
