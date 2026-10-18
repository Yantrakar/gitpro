"""Guard: global chained git hooks that refuse a commit or push with the wrong committer email.

`gitpro guard install` points core.hooksPath (through ~/.gitconfig-gitpro-guard, included from the
generated gitconfig) at ~/.config/gitpro/hooks. Its pre-commit / pre-push / commit-msg scripts run
`gitpro guard check <hook>`, then the repo's own hook, then [guard] chain_to; every other hook name is a
pass-through. Only exit 3 from `guard check` refuses; every failure of gitpro lets the commit through.

Hot path: `guard check`, `guard test` and `guard status` never import cli, view or rich.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import args as _args
from .args import ask as _confirm
from .config import HOOKS_DIR, ConfigError

PROG = "gitpro"
BLOCK = 3                    # the only exit code that refuses; the hook script maps it to 1
GIT_TIMEOUT = 5              # seconds, for every git call made inside a hook
GUARD_GITCONFIG = ".gitconfig-gitpro-guard"   # plan.GUARD_GITCONFIG, relative to home
REPO_HOOKS = ("pre-commit", "pre-push", "commit-msg")   # render.GUARD_HOOKS

# hook name -> "module:function" entries, run in order with one Context (01 §5.6). A missing module or
# function is skipped. An entry returns 0 (pass) or 3 (block); anything else is a warning.
HOOKS: Dict[str, List[str]] = {
    "pre-commit": ["gitpro.guard:identity_commit", "gitpro.policy:hook_commit"],
    "pre-push": ["gitpro.guard:identity_push", "gitpro.policy:hook_push"],
    "commit-msg": ["gitpro.trailers:hook_message", "gitpro.policy:hook_message"],
}

# Problems that explain a wrong committer email, in the order resolve reports them.
_CAUSES = ("url_form", "override", "pin_stale", "global_override", "not_applied", "env_override")
_SHEBANGS = ("#!/bin/sh", "#!/usr/bin/env sh", "#!/bin/bash", "#!/usr/bin/env bash", "#!/usr/bin/bash",
             "#!/bin/zsh", "#!/usr/bin/env zsh")


@dataclass(frozen=True)
class Context:
    hook: str                       # "pre-commit" | "pre-push" | "commit-msg"
    repo: Path                      # the work-tree top level
    args: Tuple[str, ...]           # the hook's arguments from git
    stdin: str                      # pre-push: the ref lines; "" otherwise
    cfg: object                     # config.Config
    report: object                  # resolve.Report for this repo (and remote, for pre-push)
    committer_email: Optional[str]
    author_email: Optional[str]
    say: Callable[[str], None]      # one line to stderr


def _say(line: str) -> None:
    print(line, file=sys.stderr)


_ONCE_HOOKS = ("pre-commit", "commit-msg")


def _warn(hook: str, line: str) -> None:
    """A fail-open warning, printed once per git command: pre-commit notes git's pid in GITPRO_WARNED_FILE
    when it warns, and commit-msg of the same git process (GITPRO_GIT_PID, both set by the hook script)
    stays quiet."""
    f, pid = os.environ.get("GITPRO_WARNED_FILE"), os.environ.get("GITPRO_GIT_PID")
    if f and pid and hook in _ONCE_HOOKS:
        try:
            if hook == "commit-msg":
                if Path(f).read_text(encoding="utf-8", errors="replace").strip() == pid:
                    return
            else:
                Path(f).write_text(pid + "\n", encoding="utf-8")
        except OSError:
            pass
    _say(line)


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").casefold() == (b or "").casefold()


# ---------------------------------------------------------------- git inside a hook

def _hook_git(repo: Optional[Path], *argv: str, env: Optional[Mapping[str, str]] = None,
              ok: Sequence[int] = (0,)) -> subprocess.CompletedProcess:
    """git with the hook timeout; ConfigError on timeout, on a missing git or an unexpected exit code.
    repo=None runs git as the hook sees it (the hook's cwd and its GIT_DIR / GIT_WORK_TREE); with a repo,
    git runs there without them. Output that is not UTF-8 is decoded with replacement characters."""
    if env is None:
        from .resolve import _child_env
        env = os.environ if repo is None else _child_env()
    try:
        r = subprocess.run(["git", *(["-C", str(repo)] if repo is not None else []), *argv], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=dict(env),
                           stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        raise ConfigError("git not found") from None
    except subprocess.TimeoutExpired:
        raise ConfigError(f"git {' '.join(argv[:2])} timed out after {GIT_TIMEOUT}s") from None
    if r.returncode not in ok:
        raise ConfigError(f"git {' '.join(argv[:2])} failed: {r.stderr.strip() or r.returncode}")
    return r


def _ident_email(repo: Optional[Path], which: str, env: Mapping[str, str]) -> Optional[str]:
    """The email in `git var GIT_<which>_IDENT`, or None when git cannot work one out."""
    r = _hook_git(repo, "var", f"GIT_{which}_IDENT", env=env, ok=range(256))
    line = r.stdout.strip()
    if r.returncode or "<" not in line:
        return None
    return line[line.rfind("<") + 1:line.rfind(">")] or None


def _load_cfg(home: Path):
    from . import config
    path, how = config.find(None, home)
    return config.load_all(path, how)


def _entry(spec: str) -> Optional[Callable]:
    mod_name, _, attr = spec.partition(":")
    try:
        mod = importlib.import_module(mod_name)
    except ModuleNotFoundError as e:
        if e.name == mod_name:
            return None
        raise
    fn = getattr(mod, attr, None)
    return fn if callable(fn) else None


# ---------------------------------------------------------------- check

def check(name: str, argv: Sequence[str] = (), stdin: Optional[str] = None, *,
          repo: Optional[Path] = None, hook: bool = True) -> int:
    """Run the entries of HOOKS[name]; 3 when one refuses and [guard] on_mismatch is "block", else 0.
    Never raises: any failure of gitpro prints `⚠ gitpro guard: <msg>; not blocking` and returns 0."""
    try:
        return _check(name, list(argv), stdin, repo, hook)
    except KeyboardInterrupt:
        return 130
    except BaseException as e:   # noqa: BLE001 -- a broken gitpro must never block a commit
        msg = str(e) if isinstance(e, ConfigError) else (f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
        _warn(name, f"⚠ gitpro guard: {msg}; not blocking")
        return 0


def _check(name: str, argv: List[str], stdin: Optional[str], repo: Optional[Path], hook: bool) -> int:
    if os.environ.get("GITPRO_SKIP"):
        return 0
    if name not in HOOKS:
        raise ConfigError(f"unknown hook {name!r}")
    from . import resolve
    resolve.TIMEOUT = GIT_TIMEOUT
    resolve.require_git()
    cfg = _load_cfg(Path.home())
    ident_at = repo   # None in a hook: ask git exactly as the hook sees it
    repo = Path(repo) if repo is not None else (_hook_repo() if hook else Path(os.getcwd()))
    # git exports GIT_AUTHOR_* in every pre-commit/commit-msg run; judging them would flag every --author.
    judged = {k: v for k, v in os.environ.items()
              if not (hook and k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE"))}
    if name == "pre-push" and stdin is None:
        stdin = sys.stdin.read()
    stdin = stdin or ""
    report = committer = author = None
    if name in ("pre-commit", "pre-push"):
        remote = argv[0] if name == "pre-push" and argv else None
        destination = argv[1] if name == "pre-push" and len(argv) > 1 else None
        report = resolve.compare(repo, cfg, remote=remote, remote_url=destination, env=judged)
        repo = report.repo
        if name == "pre-commit":
            committer = _ident_email(ident_at, "COMMITTER", os.environ)
            author = _ident_email(ident_at, "AUTHOR", os.environ)
    ctx = Context(name, repo, tuple(argv), stdin, cfg, report, committer, author, _say)
    refused = False
    for spec in HOOKS[name]:
        try:
            fn = _entry(spec)
            if fn is None:
                continue
            rc = fn(ctx)
        except KeyboardInterrupt:
            raise
        except Exception as e:   # noqa: BLE001
            _record_crash(f"guard {name}", e)
            _warn(name, f"⚠ gitpro {spec}: {e}; not blocking")
            continue
        if rc == BLOCK:
            refused = True
        elif rc not in (0, None):
            _warn(name, f"⚠ gitpro {spec}: returned {rc}; not blocking")
    return BLOCK if refused and cfg.guard.on_mismatch == "block" else 0


_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


def _hook_repo() -> Path:
    """The repo git runs the hook for. `git --git-dir=D --work-tree=W` (a dotfiles repo, say) exports
    GIT_DIR / GIT_WORK_TREE, and W may lie inside another repo or in no repo: judge D, which holds the
    remotes and the config. A bare-style D is judged as itself."""
    if not any(os.environ.get(k) for k in _REDIRECTS):
        return Path(os.getcwd())
    out = _hook_git(None, "rev-parse", "--absolute-git-dir").stdout.strip()
    if not out:
        raise ConfigError("git rev-parse --absolute-git-dir printed nothing")
    return Path(out)


def _belongs(exp) -> str:
    who = f"identity {exp.id}" if exp.id else "[default]"
    return f"{who} <{exp.email}>"


def _where(exp) -> str:
    from .resolve import redact_url
    m = exp.match
    url = redact_url(m.url)
    if m.remote and url:
        return f"{m.remote} ({url})"
    return url or "this repo"


_RECORD = True        # False while `guard test` simulates a hook: nothing was denied


def _mismatch(ctx: Context, head: str, lines: List[str], skip: str, reason: str = "problem") -> int:
    warn = ctx.cfg.guard.on_mismatch != "block"
    if not warn and _RECORD:
        _record_denial(ctx, reason)
    ctx.say(("⚠" if warn else "✗") + f" gitpro: {head}")
    for line in lines:
        ctx.say("  " + line)
    ctx.say(f"  skip once: GITPRO_SKIP=1 git {skip} ...")
    if warn:
        ctx.say('  (not blocking: [guard] on_mismatch = "warn")')
    return BLOCK


def _record_crash(command: str, e: Exception) -> None:
    try:
        from . import incident
        incident.record_crash(command, e)
    except Exception:   # noqa: BLE001
        pass


def _record_denial(ctx: Context, reason: str) -> None:
    try:
        from . import incident
        codes = tuple(p.code for p in getattr(ctx.report, "problems", ()) or ())
        incident.record_denial("guard", None, ctx.hook, reason, codes)
    except Exception:   # noqa: BLE001 - recording never changes the hook's answer
        pass


def _id_of(cfg, email: str) -> str:
    for i in cfg.identities:
        if _same(i.email, email):
            return i.id
    return "default"


def identity_commit(ctx: Context) -> int:
    """pre-commit: the committer email must be the expected identity's; an author email counts only when
    it is one of the user's own (a colleague's --author, cherry-pick or git am never blocks)."""
    from .resolve import _shown_fix, identity_conflicts, own_emails
    rep = ctx.report
    exp = rep.expected
    for p in rep.problems:
        if p.code in ("key_mismatch", "sign_mismatch"):
            ctx.say(f"⚠ gitpro: {p.message}")
    amb = next((p for p in rep.problems if p.code == "ambiguous"), None)
    if amb is not None:
        # git picks one identity for every remote; whichever it is, the work for the others is wrong
        return _mismatch(ctx, f"{amb.message}; this commit would use {exp.email}",
                         [f"fix:   {_shown_fix(amb)}"] if _shown_fix(amb) else [], "commit", "ambiguous")
    if exp.email is None:
        return 0
    bad_committer, bad_author = identity_conflicts(exp.email, own_emails(ctx.cfg),
                                                  ctx.author_email, ctx.committer_email)
    if ctx.committer_email and bad_committer:
        cause = next((p for p in rep.problems if p.code in _CAUSES), None)
        if cause is not None:
            lines = [f"cause: {cause.message}"] + ([f"fix:   {_shown_fix(cause)}"] if _shown_fix(cause) else [])
        elif os.environ.get("GIT_COMMITTER_EMAIL"):
            lines = ["cause: GIT_COMMITTER_EMAIL is set in the environment",
                     "fix:   unset GIT_AUTHOR_EMAIL GIT_COMMITTER_EMAIL"]
        else:
            lines = [f"cause: git resolves user.email to {ctx.committer_email} here",
                     f"fix:   {PROG} why {shlex.quote(str(ctx.repo))}"]
        return _mismatch(ctx, f"this commit would be committed as {ctx.committer_email} but {_where(exp)} "
                              f"belongs to {_belongs(exp)}", lines, "commit", "committer")
    a = ctx.author_email
    if a and bad_author:
        lines = [f"cause: author is your {_id_of(ctx.cfg, a)} email",
                 f"fix:   unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL, or commit with "
                 f"--author={shlex.quote(f'{exp.name} <{exp.email}>')}"]
        return _mismatch(ctx, f"this commit would be authored as {a} but {_where(exp)} belongs to "
                              f"{_belongs(exp)}", lines, "commit", "author")
    return 0


def _outgoing(repo: Path, stdin: str) -> List[Tuple[str, str, str, str]]:
    """(committer email, author email, short sha, subject) of every commit this push sends that no
    remote-tracking ref has, oldest last as git log prints them."""
    seen, out = set(), []
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) != 4 or set(parts[1]) == {"0"}:
            continue
        local, remote = parts[1], parts[3]
        argv = ["log", "--format=%ce%x00%ae%x00%h%x00%s", local]
        if set(remote) != {"0"} and \
                _hook_git(repo, "cat-file", "-e", f"{remote}^{{commit}}", ok=range(256)).returncode == 0:
            argv.append(f"^{remote}")
        for row in _hook_git(repo, *argv, "--not", "--remotes").stdout.splitlines():
            f = row.split("\0")
            if len(f) == 4 and f[2] not in seen:
                seen.add(f[2])
                out.append((f[0], f[1], f[2], f[3]))
    return out


def identity_push(ctx: Context) -> int:
    """pre-push: every outgoing commit whose committer (or author) email is one of the user's own must be
    the remote's identity's. Commits by anyone else are never judged (V-010)."""
    from .resolve import _tilde, own_emails
    exp = ctx.report.expected
    if exp.email is None:
        return 0
    own = {e.casefold() for e in own_emails(ctx.cfg)}

    def wrong(e: str) -> bool:
        return e.casefold() in own and not _same(e, exp.email)
    bad = [(c if wrong(c) else a, h, s) for c, a, h, s in _outgoing(ctx.repo, ctx.stdin) if wrong(c) or wrong(a)]
    if not bad:
        return 0
    emails = ", ".join(dict.fromkeys(e for e, _, _ in bad))
    n = len(bad)
    lines = [f"{h} {e} {s}" for e, h, s in bad[:5]] + ([f"… and {n - 5} more"] if n > 5 else [])
    if _audit_available():
        lines.append(f"fix:   {PROG} audit --rewrite {_tilde(str(ctx.repo))}")
    else:
        lines.append("fix:   amend the listed commits with the right user.email (git rebase -i <base>, "
                     "then git commit --amend --no-edit on each)")
    head = (f"{n} commit{'s' * (n != 1)} to {_where(exp)} {'are' if n != 1 else 'is'} committed as {emails}; "
            f"this remote belongs to {exp.id or '[default]'} <{exp.email}>")
    return _mismatch(ctx, head, lines, "push", "push")


def _audit_available() -> bool:
    try:
        importlib.import_module("gitpro.audit")
        return True
    except Exception:   # noqa: BLE001
        return False


# ---------------------------------------------------------------- status

def _home() -> Path:
    return Path.home()


def _config_entries(home: Path) -> List[Tuple[str, str, str, str]]:
    """(scope, origin, key, value) for core.hookspath and filter.lfs.process, from a non-repo dir."""
    from .resolve import _child_env
    try:
        r = subprocess.run(["git", "config", "-z", "--show-scope", "--show-origin", "--get-regexp",
                            r"^(core\.hookspath|filter\.lfs\.process)$"], cwd=str(home), capture_output=True,
                           text=True, env=_child_env(), stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        raise ConfigError("git not found") from None
    except subprocess.TimeoutExpired:
        raise ConfigError(f"git config timed out after {GIT_TIMEOUT}s") from None
    fields = r.stdout.split("\0")
    out = []
    for n in range(0, len(fields) - 2, 3):
        key, _, value = fields[n + 2].partition("\n")
        # HOME may itself be a repo (dotfiles): its local config is that repo's, not the user's.
        if fields[n] in ("system", "global"):
            out.append((fields[n], fields[n + 1], key, value))
    return out


def chain_advice(value: str) -> str:
    """What to do about someone else's core.hooksPath = value: chain to it by its absolute path."""
    v = os.path.expanduser(value)
    if os.path.isabs(v):
        return f'set [guard] chain_to = "{os.path.normpath(v)}" so gitpro runs those hooks too'
    return (f"it is relative (each repo's own {value}), which chain_to cannot follow; set it per repo instead "
            f"(git config --local core.hooksPath {shlex.quote(value)})")


def _real(p: object) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def _chain_of(script: Path) -> Optional[str]:
    """The chain_to directory baked into an installed hook script, or None."""
    try:
        for line in script.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("chain="):
                value = shlex.split(line[len("chain="):]) or [""]
                return value[0] or None
    except (OSError, ValueError):
        pass
    return None


def _gitpro_on_path(home: Path) -> Optional[str]:
    found = shutil.which(PROG)
    if found:
        return found
    local = home / ".local/bin" / PROG
    return str(local) if os.access(local, os.X_OK) else None


def status(home: Optional[Path] = None) -> dict:
    """Where the guard stands: installed ("global"|"none"), hooks_dir, on_mismatch, chain_to, gitpro_bin,
    hooks {name: bool}, system_hooks_path, plus lfs / foreign_hooks_path. Never raises: on a failure
    installed is "none" and "error" says why. One git call; no network."""
    home = Path(home) if home is not None else _home()
    hooks_dir = home / HOOKS_DIR
    base = {"installed": "none", "hooks_dir": str(hooks_dir), "on_mismatch": "block", "chain_to": None,
            "gitpro_bin": None, "hooks": {}, "system_hooks_path": None}
    try:
        return _status(home, hooks_dir, base)
    except Exception as e:   # noqa: BLE001
        return {**base, "error": str(e) or type(e).__name__}


def _status(home: Path, hooks_dir: Path, out: dict) -> dict:
    from .render import HOOK_NAMES, is_generated
    out = dict(out)
    own = _real(hooks_dir)
    guard_file = str(home / GUARD_GITCONFIG)
    hook_paths = []
    lfs = None
    for scope, origin, key, value in _config_entries(home):
        if key == "core.hookspath":
            hook_paths.append({"scope": scope, "origin": origin, "value": value})
        elif key == "filter.lfs.process" and value:
            lfs = scope
    ours = [h for h in hook_paths if _real(h["value"]) == own]
    foreign = [h for h in hook_paths if _real(h["value"]) != own and h["origin"] != f"file:{guard_file}"]
    effective = hook_paths[-1]["value"] if hook_paths else None
    system = [h["value"] for h in hook_paths if h["scope"] == "system"]

    def present(name: str) -> bool:
        p = hooks_dir / name
        try:
            return os.access(p, os.X_OK) and p.is_file() and is_generated(p.read_text(errors="replace"))
        except OSError:
            return False
    out["hooks"] = {name: present(name) for name in HOOK_NAMES}
    out["installed"] = "global" if ours and effective is not None and _real(effective) == own else "none"
    out["system_hooks_path"] = system[-1] if system else None
    out["foreign_hooks_path"] = foreign
    out["effective_hooks_path"] = effective
    out["lfs"] = lfs
    out["chain_to"] = _chain_of(hooks_dir / "pre-commit")
    out["gitpro_bin"] = _gitpro_on_path(home)
    try:
        out["on_mismatch"] = _load_cfg(home).guard.on_mismatch
    except Exception:   # noqa: BLE001 -- status works without a config
        pass
    return out


def _bypassed(home: Path) -> List[dict]:
    """Repos under the scan roots with a local core.hooksPath (the global guard does not run there)."""
    from . import scan
    from .resolve import _git
    out = []
    for repo in scan.find([home]):
        value = _git(repo, "config", "--local", "--get", "core.hooksPath").stdout.strip()
        if not value:
            continue
        hooks = (repo / value) if not os.path.isabs(value) else Path(value)
        mode = any(_has_block(hooks / n) for n in REPO_HOOKS)
        out.append({"repo": str(repo), "hooks_path": value, "repo_mode": mode})
    return out


def _has_block(path: Path) -> bool:
    from .render import has_block
    try:
        return has_block(path.read_text(errors="replace"))
    except OSError:
        return False


def cmd_status(args) -> int:
    from . import jsonout
    from .render import GUARD_HOOKS, HOOK_NAMES, NOT_FORWARDED
    from .undo import tilde
    st = status()
    if getattr(args, "repos", False):
        st["bypassed_repos"] = _bypassed(_home())
    if getattr(args, "json", False):
        jsonout.emit("guard status", st)
        return 0
    hooks = st["hooks"]
    if st["installed"] == "global":
        print(f"guard: installed (global, {tilde(Path(st['hooks_dir']))})  on_mismatch = {st['on_mismatch']}")
        mark = {True: "✓", False: "✗"}
        extra = (f" (trailers: {'installed' if _entry('gitpro.trailers:hook_message') else 'not installed → no-op'}"
                 f"; policy: {'installed' if _entry('gitpro.policy:hook_message') else 'not installed'})")
        print("  " + "   ".join(f"{n} {mark[hooks.get(n, False)]}" for n in GUARD_HOOKS) + extra)
        stubs = [n for n in HOOK_NAMES if n not in GUARD_HOOKS]
        missing = [n for n in stubs if not hooks.get(n)]
        print(f"  {len(stubs)} pass-through hooks {'✓' if not missing else '✗ missing: ' + ', '.join(missing)}"
              f"   (not forwarded: {', '.join(NOT_FORWARDED)})")
        print(f"  chain_to: {st['chain_to'] or '(none)'}")
        print(f"  gitpro on PATH: {tilde(Path(st['gitpro_bin'])) if st['gitpro_bin'] else '(not found; the hooks do nothing)'}")
        if st.get("lfs"):
            print(f"  git lfs: filter.lfs.process set {'globally' if st['lfs'] == 'global' else 'in ' + st['lfs'] + ' scope'}; "
                  "stubs call 'git lfs <hook>' unless the repo hook already does")
    elif st.get("effective_hooks_path"):
        other = st["effective_hooks_path"]
        print(f"guard: core.hooksPath = {other} (not gitpro) → {chain_advice(other)}, then run {PROG} guard install")
    else:
        print(f"guard: not installed → {PROG} guard install")
    if st.get("error"):
        print(f"⚠ guard status: {st['error']}", file=sys.stderr)
    if st["system_hooks_path"]:
        print(f"⚠ system gitconfig sets core.hooksPath = {st['system_hooks_path']}; the global guard overrides it; "
              "set chain_to to keep it", file=sys.stderr)
    else:
        print("system core.hooksPath: (none)")
    if "bypassed_repos" in st:
        if st["bypassed_repos"]:
            print("repos with their own core.hooksPath (global guard bypassed):")
            for r in st["bypassed_repos"]:
                p = tilde(Path(r["repo"]))
                tail = "repo mode ✓" if r["repo_mode"] else f"→ {PROG} guard install --repo {shlex.quote(p)}"
                print(f"  {p}   {r['hooks_path']}   {tail}")
        else:
            print("repos with their own core.hooksPath: none")
    return 0


def doctor_section(ctx) -> dict:
    """`gitpro doctor`: the guard's state from status(); never raises, no network."""
    st = status(getattr(ctx, "home", None))
    fix_install = f"{PROG} guard install"
    if st.get("error"):
        return {"status": "warning", "summary": f"could not read the guard state: {st['error']}",
                "problems": [{"message": st["error"], "fix": f"{PROG} guard status", "level": "warning"}],
                "details": []}
    cfg = None
    try:
        cfg = getattr(ctx, "cfg", None)
    except Exception:   # noqa: BLE001
        pass
    if st["installed"] != "global":
        other = st.get("effective_hooks_path")
        details = [f"core.hooksPath = {other} (not gitpro) → {chain_advice(other)}"] if other else []
        if cfg is not None and getattr(getattr(cfg, "guard", None), "hooks", False):
            return {"status": "warning", "summary": "not installed", "details": details,
                    "problems": [{"message": "[guard] hooks = true but the hooks are not installed",
                                  "fix": fix_install, "level": "warning"}]}
        return {"status": "skipped", "summary": f"not installed → {fix_install}", "problems": [],
                "details": details}
    problems = []
    try:
        from . import plan
        missing = chain_missing(cfg, plan.Env(Path(getattr(ctx, "home", None) or _home()))) if cfg else None
    except Exception:   # noqa: BLE001
        missing = None
    if missing:
        msg, _, fix = missing.partition(" → ")
        problems.append({"message": msg, "fix": fix, "level": "warning"})
    missing = [n for n, ok in st["hooks"].items() if not ok]
    if missing:
        problems.append({"message": f"hook scripts missing or not gitpro's: {', '.join(missing)}",
                         "fix": fix_install, "level": "error"})
    if not st["gitpro_bin"]:
        problems.append({"message": "gitpro is not on PATH or in ~/.local/bin; the hooks check nothing",
                         "fix": "put gitpro on PATH", "level": "warning"})
    if st["system_hooks_path"]:
        problems.append({"message": f"system gitconfig sets core.hooksPath = {st['system_hooks_path']}; the "
                                    "global guard overrides it",
                         "fix": chain_advice(st["system_hooks_path"]), "level": "warning"})
    level = "error" if any(p["level"] == "error" for p in problems) else ("warning" if problems else "ok")
    return {"status": level, "summary": f"installed (global), on_mismatch = {st['on_mismatch']}",
            "problems": problems,
            "details": [f"hooks: {st['hooks_dir']}", f"chain_to: {st['chain_to'] or '(none)'}"]}


# ---------------------------------------------------------------- test

def cmd_test(args) -> int:
    from . import resolve
    from .undo import tilde
    d = Path(args.dir)
    if resolve.is_repo(d) is None:
        raise ConfigError(f"{d}: not a git repository")
    rc = _check_strict(d)
    if rc == 0:
        rep_cfg = _load_cfg(Path.home())
        exp = resolve.expected(resolve.is_repo(d), rep_cfg)
        if exp.email is None:
            print(f"✓ guard: no identity for {tilde(resolve.is_repo(d))}; commits are not checked")
        else:
            print(f"✓ guard: would allow commits as {exp.email} ({exp.id or 'default'})")
    return rc


def _check_strict(repo: Path) -> int:
    """The pre-commit check as `guard test` runs it: errors are errors (exit 2), not warnings."""
    from . import resolve
    resolve.require_git()
    cfg = _load_cfg(Path.home())
    report = resolve.compare(repo, cfg)
    top = report.repo
    ctx = Context("pre-commit", top, (), "", cfg, report, _ident_email(top, "COMMITTER", os.environ),
                  _ident_email(top, "AUTHOR", os.environ), _say)
    refused = False
    global _RECORD
    _RECORD = False
    try:
        for spec in HOOKS["pre-commit"]:
            fn = _entry(spec)
            if fn is not None and fn(ctx) == BLOCK:
                refused = True
    finally:
        _RECORD = True
    return BLOCK if refused and cfg.guard.on_mismatch == "block" else 0


# ---------------------------------------------------------------- install / uninstall (global)

def _env(args):
    from . import plan
    return plan.Env(Path(getattr(args, "home", None) or Path.home()).expanduser().resolve())


def _need_yes_for_json(args) -> None:
    if getattr(args, "json", False) and getattr(args, "write", False) and not args.yes:
        raise ConfigError("--write --json needs -y (there is no confirmation in JSON mode)")


_WARNED_MISSING: set = set()


def chain_missing(cfg, env) -> Optional[str]:
    """The warning for a [guard] chain_to dir that does not exist (deleted after install), else None."""
    if not cfg.guard.chain_to:
        return None
    p = env.expand(cfg.guard.chain_to)
    if os.path.lexists(p):
        return None
    return (f"[guard] chain_to {cfg.guard.chain_to} does not exist; the hooks run without it → create it, "
            f"or remove chain_to from the config and run {PROG} apply")


def check_chain_to(cfg, env, missing_ok: bool = False) -> Optional[Path]:
    """The [guard] chain_to dir, refused unless it is a directory you own that only you can write. With
    missing_ok (plan/apply), a missing dir is a ⚠ line (once per process) and None: the hooks are rendered
    without the chain, so a deleted chain_to never blocks unrelated changes. `guard install` refuses it."""
    if not cfg.guard.chain_to:
        return None
    p = env.expand(cfg.guard.chain_to)
    shown = cfg.guard.chain_to
    missing = chain_missing(cfg, env)
    if missing and not missing_ok:
        raise ConfigError(f"chain_to {shown} is not a directory (it does not exist); create it, "
                          f"or remove chain_to from the config")
    if missing:
        if str(p) not in _WARNED_MISSING:
            _WARNED_MISSING.add(str(p))
            _say(f"⚠ {missing}")
        return None
    if not p.is_absolute() or not p.is_dir():
        raise ConfigError(f"chain_to {shown} is not a directory; point [guard] chain_to at a hooks directory")
    if _real(p) == _real(env.home / HOOKS_DIR):
        raise ConfigError(f"chain_to {shown} is gitpro's own hooks dir; that would run the hooks in a loop")
    st = p.resolve().stat()
    if st.st_uid != os.getuid():
        raise ConfigError(f"chain_to {shown} is not owned by you; refusing to run its hooks on every commit "
                          f"(fix: chown it, or point [guard] chain_to at a directory you own)")
    if st.st_mode & 0o022:
        raise ConfigError(f"chain_to {shown} is group/world-writable; fix: chmod go-w {shlex.quote(str(p))}")
    return p


def foreign_hooks_path(home: Path) -> List[dict]:
    """core.hooksPath values ({scope, origin, value}) set by anything but gitpro's own guard include."""
    own = _real(home / HOOKS_DIR)
    mine = f"file:{home / GUARD_GITCONFIG}"
    return [{"scope": s, "origin": o, "value": v} for s, o, k, v in _config_entries(home)
            if k == "core.hookspath" and _real(v) != own and o != mine]


def check_foreign(cfg, home: Path) -> None:
    """Refuse to take over someone else's core.hooksPath unless [guard] chain_to keeps running it."""
    if cfg.guard.chain_to:
        return
    for h in foreign_hooks_path(home):
        raise ConfigError(f"core.hooksPath is already {h['value']} ({h['scope']}, {h['origin']}); "
                          f"{chain_advice(h['value'])}, then re-run")


def _check_applied(env) -> None:
    from . import plan, render
    managed = env.home / plan.MANAGED_GITCONFIG
    gitconfig = env.home / ".gitconfig"
    ok = managed.is_file() and gitconfig.is_file() and render.has_block(gitconfig.read_text())
    if not ok:
        raise ConfigError(f"the gitpro config is not applied yet; run {PROG} apply first")


def _backup_lines(changes, state: Path) -> List[str]:
    """Undo lines that put each changed file back: copies of the old contents go into state/<dir>."""
    lines = []
    store = None
    for n, c in enumerate(changes):
        q = shlex.quote(str(c.path))
        if c.old is None:
            lines.append(f"rm -f {q}")
            continue
        if store is None:
            store = state / f"guard-backup-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
            store.mkdir(mode=0o700, parents=True, exist_ok=True)
        keep = store / f"{n:02d}-{c.path.name}"
        keep.write_text(c.old)
        mode = c.path.stat().st_mode & 0o777 if c.path.exists() else 0o600
        os.chmod(keep, mode)
        lines.append(f"mkdir -p {shlex.quote(str(c.path.parent))} && cp -p {shlex.quote(str(keep))} {q}")
    return lines


def _show_changes(changes, home: Path) -> None:
    from .render import GUARD_HOOKS, HOOK_NAMES
    from .undo import tilde
    stubs = [c for c in changes if c.path.parent == home / HOOKS_DIR and c.path.name in HOOK_NAMES
             and c.path.name not in GUARD_HOOKS]
    shown_stub = False
    for c in changes:
        if c in stubs:
            if not shown_stub:
                print(f"  {c.action:7} {tilde(c.path)} … ({len(stubs)} pass-through hooks)")
                shown_stub = True
            continue
        note = ""
        if c.path.name == ".gitconfig-identities":
            note = f"        (+ include ~/{GUARD_GITCONFIG})" if c.action != "remove" and \
                GUARD_GITCONFIG in (c.new or "") else f"        (- include ~/{GUARD_GITCONFIG})"
        print(f"  {c.action:7} {tilde(c.path)}{note}")


def cmd_install(args) -> int:
    _need_yes_for_json(args)
    if getattr(args, "repo", None):
        return _install_repo(args)
    from . import config, files, jsonout, plan, render, resolve, undo
    from .args import preview_only, preview_result
    resolve.require_git()
    env = _env(args)
    path, how = config.find(args.config, env.home)
    cfg = config.load_all(path, how)
    check_chain_to(cfg, env)
    check_foreign(cfg, env.home)
    _check_applied(env)
    managed = env.home / plan.MANAGED_GITCONFIG
    for t in plan.guard_targets(cfg, env):
        if t.path.exists() and not render.is_generated(t.path.read_text(errors="replace")):
            raise ConfigError(f"{t.path} exists and was not written by gitpro; move it away and re-run")
    changes = files.diff(plan.guard_targets(cfg, env) + [plan.Target(managed, render.with_guard_include(
        managed.read_text(), plan.guard_include(env), plan.guard_manifest(env)))], [])
    as_json = getattr(args, "json", False)
    if preview_only(args):
        if not as_json:
            _show_changes(changes, env.home)
        return preview_result(args, "guard install", {
            "changes": [{"action": c.action, "path": str(c.path)} for c in changes], "undo": None})
    if not changes:
        if as_json:
            jsonout.emit("guard install", {"changes": [], "undo": None})
        else:
            print("✓ guard already installed; nothing to change")
        return 0
    if not as_json:
        _show_changes(changes, env.home)
        n_files = sum(1 for c in changes if c.path != managed)
        n_upd = len(changes) - n_files
        if not _confirm(f"install guard hooks ({n_files} files, {n_upd} change{'s' * (n_upd != 1)})?", args.yes):
            from . import view
            view.aborted()
            return 1
    lines = _backup_lines(changes, undo.state_dir())
    lines.append(f"rmdir {shlex.quote(str(env.home / HOOKS_DIR))} 2>/dev/null || true   # only when empty")
    script = undo.write("guard install", lines, what="guard install")
    files.apply(changes)
    if as_json:
        jsonout.emit("guard install", {"changes": [{"action": c.action, "path": str(c.path)} for c in changes],
                                       "undo": str(script)})
        return 0
    from . import view
    view.ok(f"guard installed; test it: {PROG} guard test")
    print(f"↩ undo: {PROG} guard uninstall   (or: sh {undo.tilde(script)})")
    view.step(f"'{PROG} apply' keeps these hooks; {PROG} guard uninstall removes them "
              "(or set [guard] hooks = true to make the TOML the source of truth)")
    return 0


def cmd_uninstall(args) -> int:
    _need_yes_for_json(args)
    if getattr(args, "repo", None):
        return _uninstall_repo(args)
    from . import files, jsonout, plan, render, undo
    from .args import preview_only, preview_result
    env = _env(args)
    managed = env.home / plan.MANAGED_GITCONFIG
    targets = []
    if managed.is_file():
        text = managed.read_text()
        new = render.without_guard_include(text, plan.guard_include(env), plan.guard_manifest(env))
        targets.append(plan.Target(managed, new))
    changes = files.diff(targets, plan.guard_paths(env))
    as_json = getattr(args, "json", False)
    if preview_only(args):
        if not as_json:
            _show_changes(changes, env.home)
        return preview_result(args, "guard uninstall", {
            "changes": [{"action": c.action, "path": str(c.path)} for c in changes], "undo": None})
    if not changes:
        if as_json:
            jsonout.emit("guard uninstall", {"changes": [], "undo": None})
        else:
            print("✓ guard not installed; nothing to remove")
        return 0
    if not as_json:
        _show_changes(changes, env.home)
        if not _confirm(f"remove guard hooks ({len(changes)} change{'s' * (len(changes) != 1)})?", args.yes):
            from . import view
            view.aborted()
            return 1
    script = undo.write("guard uninstall", _backup_lines(changes, undo.state_dir()), what="guard uninstall")
    files.apply(changes)
    plan.prune_hooks_dir(env)
    hooks_on: object = False
    try:
        hooks_on = _load_cfg(env.home).guard.hooks
    except Exception:   # noqa: BLE001
        pass
    if as_json:
        jsonout.emit("guard uninstall", {"changes": [{"action": c.action, "path": str(c.path)} for c in changes],
                                         "undo": str(script)})
        return 0
    from . import view
    view.ok("guard removed; git runs each repo's own hooks again")
    print(f"↩ undo: sh {undo.tilde(script)}")
    if hooks_on:
        view.warn(f"[guard] hooks = true in your config: the next '{PROG} apply' installs them again; set it to false")
    elif hooks_on is None:
        view.step(f"'{PROG} setup' installs them again; to keep them off, set [guard] hooks = false in your config")
    return 0


# ---------------------------------------------------------------- repo mode

def _repo_layout(path: Path) -> Tuple[Path, Path, Path]:
    """(top, common dir, hooks dir) for a repo; hooks dir honours the repo's local core.hooksPath."""
    from . import resolve
    top = resolve.is_repo(path)
    if top is None:
        raise ConfigError(f"{path}: not a git repository")
    out = resolve._git(top, "rev-parse", "--path-format=absolute", "--git-common-dir", "--git-path", "hooks")
    lines = out.stdout.splitlines()
    if out.returncode or len(lines) < 2:
        raise ConfigError(f"{path}: git rev-parse failed")
    common, hooks = Path(lines[0]), Path(lines[1])
    if _real(hooks) == _real(_home() / HOOKS_DIR):
        hooks = common / "hooks"
    elif _real(hooks) != _real(common / "hooks"):
        _refuse_shared_hooks_dir(top, hooks)
    return top, common, hooks


def _refuse_shared_hooks_dir(top: Path, hooks: Path) -> None:
    """Repo mode edits only this repo's hooks: a core.hooksPath from global, system or included config
    names a dir other repos share."""
    from . import resolve
    from .undo import tilde
    r = resolve._git(top, "config", "--show-scope", "--show-origin", "--get", "core.hooksPath")
    scope, origin, _ = (r.stdout.rstrip("\n").split("\t", 2) + ["", "", ""])[:3]
    if scope in ("local", "worktree"):
        return
    where = origin[5:] if origin.startswith("file:") else origin
    raise ConfigError(f"{tilde(hooks)} is core.hooksPath from {scope or 'unknown'} config"
                      f"{' (' + tilde(Path(where)) + ')' if where else ''}; other repos share it, so gitpro "
                      f"does not edit it. Use {PROG} guard install with [guard] chain_to = \"{hooks}\", or add "
                      f"this block to {tilde(hooks / 'pre-commit')} yourself:\n{_block_help('pre-commit')}")


def _block_help(name: str) -> str:
    from .render import guard_block
    return guard_block(name).rstrip("\n")


def _refuse_tool_dirs(top: Path, common: Path, hooks: Path) -> None:
    from . import resolve
    from .undo import tilde
    try:
        rel = hooks.resolve().relative_to(top.resolve())
    except ValueError:
        rel = None
    if rel is not None and "_" in rel.parts:
        user_dir = top / Path(*rel.parts[:rel.parts.index("_")])
        files_ = ", ".join(tilde(user_dir / n) for n in ("pre-commit", "pre-push"))
        raise ConfigError(f"{tilde(hooks)} is managed by husky (regenerated on install); add this block to the "
                          f"hook that tool runs ({files_}):\n{_block_help('pre-commit')}")
    if _real(hooks) != _real(common / "hooks") and rel is not None and \
            resolve._git(top, "check-ignore", "-q", str(rel)).returncode == 0:
        raise ConfigError(f"{tilde(hooks)} is managed by a tool (the dir is ignored by git, so it is regenerated); "
                          f"add this block to the hook that tool runs:\n{_block_help('pre-commit')}")


def _repo_plan(top: Path, common: Path, hooks: Path) -> Tuple[List[Tuple[str, Path, Optional[str], str]], List[Path]]:
    """([(hook, file, old text or None, new text)] for the hooks that need the block, [hook files skipped
    because they are not executable: the user turned them off, and git does not run them])."""
    from . import resolve
    from .render import has_block
    from .undo import tilde
    out, skipped = [], []
    for name in REPO_HOOKS:
        f = hooks / name
        try:
            rel = f.resolve().relative_to(top.resolve())
        except ValueError:
            rel = None
        if rel is not None and resolve._git(top, "ls-files", "--error-unmatch", str(rel)).returncode == 0:
            raise ConfigError(f"{tilde(f)} is tracked by git; gitpro does not edit team files. Add this block "
                              f"yourself:\n{_block_help(name)}")
        block = _block_help(name) + "\n"
        if not f.exists():
            out.append((name, f, None, "#!/bin/sh\n" + block))
            continue
        old = f.read_text()
        if has_block(old):
            continue
        if not os.access(f, os.X_OK):
            skipped.append(f)
            continue
        first, nl, rest = old.partition("\n")
        if first.strip() not in _SHEBANGS:
            raise ConfigError(f"{tilde(f)} is not a shell script; add this block yourself:\n{_block_help(name)}")
        out.append((name, f, old, first + "\n" + block + rest))
    return out, skipped


def _install_repo(args) -> int:
    _need_yes_for_json(args)
    from . import files, jsonout, undo
    from .args import preview_only, preview_result
    top, common, hooks = _repo_layout(Path(args.repo))
    _refuse_tool_dirs(top, common, hooks)
    todo, skipped = _repo_plan(top, common, hooks)
    as_json = getattr(args, "json", False)
    if preview_only(args):
        planned = [{"action": "create" if old is None else "update", "path": str(path)}
                   for _, path, old, _ in todo]
        if not as_json:
            for item in planned:
                print(f"  {item['action']} {item['path']}")
        return preview_result(args, "guard install", {"changes": planned, "undo": None,
                                                     "skipped": [str(p) for p in skipped]})
    if not as_json:
        for f in skipped:
            print(f"· skip {undo.tilde(f)}: not executable, so git does not run it; left as it is "
                  "(chmod +x it and re-run to add the block)")
    if not todo:
        if as_json:
            jsonout.emit("guard install", {"changes": [], "undo": None, "skipped": [str(f) for f in skipped]})
        else:
            print(f"✓ guard block already in every hook in {undo.tilde(hooks)}; nothing to change")
        return 0
    if not as_json:
        for _, f, old, _ in todo:
            print(f"  {'create' if old is None else 'update':7} {undo.tilde(f)}   (+ gitpro block)")
        if not _confirm(f"add the guard block to {len(todo)} hook{'s' * (len(todo) != 1)}?", args.yes):
            from . import view
            view.aborted()
            return 1
    state = undo.state_dir()
    backup = state / f"guard-backup-{time.strftime('%Y%m%d-%H%M%S')}" / \
        hashlib.sha1(str(top).encode()).hexdigest()[:12]
    lines = []
    for name, f, old, _ in todo:
        if old is None:
            lines.append(f"rm -f {shlex.quote(str(f))}")
        else:
            lines.append(f"cp -p {shlex.quote(str(backup / name))} {shlex.quote(str(f))}")
    script = undo.write("guard install", lines, what=f"guard block in {top}")
    for name, f, old, new in todo:
        mode = 0o700
        if old is not None:
            mode = f.stat().st_mode & 0o777
            backup.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(f, backup / name)
        files.atomic_write(f, new, mode)
    if as_json:
        jsonout.emit("guard install", {"changes": [{"action": "create" if o is None else "update", "path": str(f)}
                                                   for _, f, o, _ in todo], "undo": str(script),
                                       "skipped": [str(f) for f in skipped]})
        return 0
    from . import view
    view.ok(f"guard block added to {len(todo)} hook{'s' * (len(todo) != 1)} in {undo.tilde(hooks)}")
    print(f"↩ undo: sh {undo.tilde(script)}")
    return 0


def _uninstall_repo(args) -> int:
    _need_yes_for_json(args)
    from . import files, jsonout, undo
    from .render import has_block, without_block
    from .args import preview_only, preview_result
    top, common, hooks = _repo_layout(Path(args.repo))
    todo = []
    for name in REPO_HOOKS:
        f = hooks / name
        if not f.is_file():
            continue
        old = f.read_text()
        if not has_block(old):
            continue
        new = without_block(old)
        todo.append((name, f, old, None if new.strip() in ("", "#!/bin/sh") else new))
    as_json = getattr(args, "json", False)
    if preview_only(args):
        planned = [{"action": "remove" if new is None else "update", "path": str(path)}
                   for _, path, _, new in todo]
        if not as_json:
            for item in planned:
                print(f"  {item['action']} {item['path']}")
        return preview_result(args, "guard uninstall", {"changes": planned, "undo": None})
    if not todo:
        if as_json:
            jsonout.emit("guard uninstall", {"changes": [], "undo": None})
        else:
            print(f"✓ no guard block in {undo.tilde(hooks)}; nothing to remove")
        return 0
    if not as_json:
        for _, f, _, new in todo:
            print(f"  {'remove' if new is None else 'update':7} {undo.tilde(f)}   (- gitpro block)")
        if not _confirm(f"remove the guard block from {len(todo)} hook{'s' * (len(todo) != 1)}?", args.yes):
            from . import view
            view.aborted()
            return 1
    state = undo.state_dir()
    backup = state / f"guard-backup-{time.strftime('%Y%m%d-%H%M%S')}" / \
        hashlib.sha1(str(top).encode()).hexdigest()[:12]
    backup.mkdir(mode=0o700, parents=True, exist_ok=True)
    lines = []
    for name, f, _, _ in todo:
        shutil.copy2(f, backup / name)
        lines.append(f"cp -p {shlex.quote(str(backup / name))} {shlex.quote(str(f))}")
    script = undo.write("guard uninstall", lines, what=f"guard block in {top}")
    for name, f, _, new in todo:
        if new is None:
            f.unlink()
        else:
            files.atomic_write(f, new, f.stat().st_mode & 0o777)
    if as_json:
        jsonout.emit("guard uninstall", {"changes": [{"action": "remove" if n is None else "update", "path": str(f)}
                                                     for _, f, _, n in todo], "undo": str(script)})
        return 0
    from . import view
    view.ok(f"guard block removed from {len(todo)} hook{'s' * (len(todo) != 1)} in {undo.tilde(hooks)}")
    print(f"↩ undo: sh {undo.tilde(script)}")
    return 0


# ---------------------------------------------------------------- argument parsing

def _cmd_check(args) -> int:
    return check(args.hook, args.hook_args)


def _leaf(gs, name: str, help_: str, **kw) -> argparse.ArgumentParser:
    sp = gs.add_parser(name, help=help_, **kw)
    # Also accepted after the subcommand; SUPPRESS keeps the values given before it.
    sp.add_argument("-c", "--config", type=Path, metavar="PATH", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sp.add_argument("--home", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sp.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sp.add_argument("--write", action="store_true", default=argparse.SUPPRESS,
                    help="execute after confirmation (default: preview only)")
    return sp


def add_arguments(p: argparse.ArgumentParser) -> None:
    gs = p.add_subparsers(dest="guard_command", metavar="SUBCOMMAND")
    for name, help_, func in (("install", "install the guard hooks (global, or --repo for one repo)", cmd_install),
                              ("uninstall", "remove the guard hooks", cmd_uninstall)):
        sp = _leaf(gs, name, help_)
        sp.add_argument("--repo", type=Path, metavar="PATH",
                        help="only this repo: add the guard block to its own hook files")
        sp.add_argument("--json", action="store_true", help="machine-readable output (needs -y)")
        sp.set_defaults(func=func)
    sp = _leaf(gs, "status", "show whether the guard is installed and what it runs")
    sp.add_argument("--repos", action="store_true", help="also list repos with their own core.hooksPath")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_status)
    sp = _leaf(gs, "check", "called by the hook scripts; not for humans")
    sp.add_argument("hook", help="pre-commit | pre-push | commit-msg")
    sp.add_argument("hook_args", nargs=argparse.REMAINDER)
    sp.set_defaults(func=_cmd_check)
    sp = _leaf(gs, "test", "run the pre-commit check on a repo; exits as the hook would")
    sp.add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    sp.set_defaults(func=cmd_test)
    p.set_defaults(func=cmd_status)


def register(sub, common, confirm) -> None:
    add_arguments(sub.add_parser("guard", parents=[common, confirm],
                                 help="git hooks that refuse commits with the wrong identity"))
    _args.capability("guard")


def main(argv: Sequence[str]) -> int:
    """`gitpro guard ...` without cli.py. argv includes "guard". `guard check` never fails a hook: no
    argparse, every error is a ⚠ line and exit 0."""
    rest = list(argv)[1:] if list(argv)[:1] == ["guard"] else list(argv)
    if rest[:1] == ["check"]:
        try:
            if len(rest) < 2:
                raise ConfigError("guard check needs a hook name")
            return check(rest[1], rest[2:])
        except KeyboardInterrupt:
            return 130
        except BaseException as e:   # noqa: BLE001
            _say(f"⚠ gitpro guard: {e}; not blocking")
            return 0
    p = argparse.ArgumentParser(prog=f"{PROG} guard", parents=[_args.common(), _args.confirm()])
    add_arguments(p)
    return _args.guarded(lambda a: a.func(a), p.parse_args(rest))
