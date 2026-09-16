"""lost: unpushed branches, gone upstreams, stashes, dirty trees, repos with no remote, gone remotes,
duplicate clones, stale and empty repos across the scan roots (spec 03).

Read-only by design: every git call runs with --no-optional-locks (so `status` never refreshes the
index), gc/maintenance/fsmonitor off, every transport protocol disallowed and GIT_NO_LAZY_FETCH set, so
nothing fetches, not even a partial clone's lazy fetch; status skips rename detection and LFS filters.
What git can still run: a clean filter of another driver (.gitattributes filter=<x>) when status must
re-hash a file whose stat data changed. A suggested fix is only printed, and never a destructive one.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import jsonout
from .args import capability, fail, guarded
from .config import ConfigError

KINDS = ("unpushed", "merged_gone", "stash", "dirty", "no_remote", "remote_gone", "duplicate", "stale", "empty")
OFFLINE_KINDS = tuple(k for k in KINDS if k != "remote_gone")
KEY = {"stash": "stashes", "duplicate": "duplicates"}           # kind -> JSON key (the rest are the same)
COUNTED = ("unpushed", "merged_gone", "stashes", "dirty", "no_remote", "remote_gone", "duplicates")
GIT_TIMEOUT = 10
WORKERS = 4
SENDS = "owner/repo names only"
_REDIRECT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR",
                  "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE")
_SAFE = ["--no-optional-locks", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "core.fsmonitor=false",
         # No transport at all: a lazy fetch (partial clone, git < 2.44 ignores GIT_NO_LAZY_FETCH) dies before it
         # connects. -c reaches the `git fetch` child through GIT_CONFIG_PARAMETERS.
         "-c", "protocol.allow=never",
         *[x for p in ("file", "git", "ssh", "http", "https", "ext") for x in ("-c", f"protocol.{p}.allow=never")]]
# status only: no split-index write, no LFS process/clean filter.
_STATUS = ["-c", "core.splitIndex=false", "-c", "filter.lfs.process=", "-c", "filter.lfs.clean=",
           "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false"]
DUP_DETAILS = 10
_NOTHING = "; nothing to check"
_AHEAD = re.compile(r"\bahead (\d+)")


def key(kind: str) -> str:
    return KEY.get(kind, kind)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


# ---------------------------------------------------------------- git

class _Timeout(Exception):
    pass


def _env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REDIRECT_VARS}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", LC_ALL="C", LANGUAGE="C")
    env.update(extra or {})
    return env


def _first(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)     # git and whatever it started (hooks, filters, a fetch)
    except (AttributeError, OSError):
        p.kill()


def _run(cmd: List[str], env: Dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    """cmd in a session of its own, output captured as text; on timeout its whole process group is killed
    (no orphaned children) and TimeoutExpired raised."""
    with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors="replace", env=env, start_new_session=True) as p:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(p)
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.communicate(timeout=5)
            raise
        return subprocess.CompletedProcess(cmd, p.returncode, out, err)


class _Git:
    """git -C <repo> calls for one repo; a failure becomes a warning and None, a timeout raises _Timeout."""

    def __init__(self, repo: str, warnings: List[str]):
        self.repo, self.warnings = repo, warnings

    def raw(self, *args: str, extra: Optional[Dict[str, str]] = None, pre: Sequence[str] = ()
            ) -> subprocess.CompletedProcess:
        cmd = ["git", *_SAFE, *pre, "-C", self.repo, *args]
        try:
            return _run(cmd, _env(extra), GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise _Timeout(args[0]) from None

    def __call__(self, *args: str, ok: Sequence[int] = (0,), pre: Sequence[str] = ()) -> Optional[str]:
        r = self.raw(*args, pre=pre)
        if r.returncode not in ok:
            self.warnings.append(f"{self.repo}: git {args[0]} failed: {_first(r.stderr) or f'exit {r.returncode}'}")
            return None
        return r.stdout

    def count(self, *args: str) -> Optional[int]:
        out = self("rev-list", "--count", *args)
        try:
            return int(out.strip()) if out is not None else None
        except ValueError:
            return None


# ---------------------------------------------------------------- layout

def _layout(path: Path) -> dict:
    """{"path", "common", "bare"} for a found directory, or {"path", "skip": (reason, detail)}."""
    from .scan import skip_reason
    g = _Git(str(path), [])
    try:
        r = g.raw("rev-parse", "--path-format=absolute", "--git-common-dir", "--is-bare-repository")
        if r.returncode != 0:
            return {"path": path, "skip": skip_reason(path, _first(r.stderr) or "git failed")}
        common, bare = r.stdout.split("\n")[:2]
        if bare.strip() == "true":
            return {"path": path, "bare": True, "common": os.path.normpath(common.strip())}
        top = g.raw("rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return {"path": path, "skip": skip_reason(path, _first(top.stderr) or "git failed")}
    except _Timeout:
        return {"path": path, "skip": ("timed out", "git rev-parse timed out")}
    return {"path": Path(top.stdout.strip()), "common": os.path.normpath(common.strip())}


def _worktrees(g: _Git) -> List[str]:
    """Paths of the repo's non-bare worktrees that exist, the main one first; [] when git can't list them."""
    out = g("worktree", "list", "--porcelain", "-z")
    if out is None:
        return []
    paths = []
    for entry in out.split("\0\0"):
        fields = dict(f.split(" ", 1) if " " in f else (f, "") for f in entry.split("\0") if f)
        if "worktree" in fields and "bare" not in fields and os.path.isdir(fields["worktree"]):
            paths.append(fields["worktree"])
    return paths


# ---------------------------------------------------------------- one repo

def _dirty(g: _Git) -> Optional[Tuple[int, int]]:
    out = g("status", "--porcelain=v2", "-z", "-unormal", "--no-renames", pre=_STATUS)
    if out is None:
        return None
    modified = untracked = 0
    toks = iter(out.split("\0"))
    for t in toks:
        if t.startswith(("1 ", "u ")):
            modified += 1
        elif t.startswith("2 "):
            modified += 1
            next(toks, None)    # the original path of a rename/copy
        elif t.startswith("? "):
            untracked += 1
    return modified, untracked


def _detached(g: _Git, path: str) -> Optional[dict]:
    if g.raw("symbolic-ref", "-q", "HEAD").returncode == 0:
        return None
    if g.raw("rev-parse", "-q", "--verify", "HEAD").returncode != 0:
        return None
    n = g.count("HEAD", "--not", "--branches", "--remotes")
    if not n:
        return None
    return {"repo": path, "branch": None, "commits": n, "upstream": None, "ahead": None, "detached": True,
            "fix": None}


def _push_fix(repo: str, remote: str, branch: str, ref: Optional[str] = None) -> str:
    return f"git -C {shlex.quote(repo)} push -u {shlex.quote(remote)} {shlex.quote(ref or branch)}"


_HEADS = "refs/heads/"


def _branches(g: _Git, repo: str, remotes: List[str], out: dict) -> None:
    # %(refname), not :short: a tag of the same name makes the short name "heads/<branch>".
    listing = g("for-each-ref", "--format=%(refname)%00%(upstream:short)%00%(upstream:track)", "refs/heads")
    for line in (listing or "").splitlines():
        parts = line.split("\0")
        if len(parts) != 3 or not parts[0].startswith(_HEADS):
            continue
        ref, upstream, track = parts
        branch = ref[len(_HEADS):]
        if track == "[gone]":
            n = g.count(ref, "--not", "--remotes")
            if n is not None:
                out["merged_gone"].append({"repo": repo, "branch": branch, "commits": n})
            continue
        if not remotes:
            continue                      # no_remote covers it: there is nothing to push to
        n = g.count(ref, "--not", "--remotes")
        if not n:
            continue
        m = _AHEAD.search(track)
        fix = None
        if not upstream and len(remotes) == 1:
            tag = g.raw("show-ref", "--verify", "-q", f"refs/tags/{branch}").returncode == 0
            fix = _push_fix(repo, remotes[0], branch, ref if tag else None)   # `push origin x` is ambiguous
        out["unpushed"].append({"repo": repo, "branch": branch, "commits": n, "upstream": upstream or None,
                                "ahead": int(m[1]) if m else None, "detached": False, "fix": fix})


def _stashes(g: _Git, repo: str) -> List[dict]:
    listing = g("stash", "list", "--format=%gd%x00%cI%x00%gs")
    found = []
    for line in (listing or "").splitlines():
        parts = line.split("\0")
        if len(parts) == 3:
            found.append({"repo": repo, "ref": parts[0], "date": parts[1], "subject": parts[2]})
    return found


def _urls(g: _Git) -> List[Tuple[str, str]]:
    """[(remote, raw url)] from the repo's config, in config order."""
    out = g("config", "--get-regexp", r"^remote\..*\.url$", ok=(0, 1)) or ""
    pairs = []
    for line in out.splitlines():
        name, _, url = line.partition(" ")
        pairs.append((name[len("remote."):-len(".url")], url))
    return pairs


def _probe(repo: str, common: str, kinds: Set[str], stale_days: int, now: float) -> dict:
    """Everything one repo (grouped by common dir; checked at its main worktree) contributes."""
    warnings: List[str] = []
    out: dict = {k: [] for k in ("unpushed", "merged_gone", "stashes", "dirty", "no_remote", "stale", "empty")}
    out.update(warnings=warnings, worktrees=[], skipped=None, urls=[], head=None, common=common)
    g = _Git(repo, warnings)
    try:
        trees = _worktrees(g)
        if trees and os.path.normpath(trees[0]) == os.path.normpath(common):
            # A submodule lists its gitdir (super/.git/modules/x) as the main tree; repo is its
            # rev-parse --show-toplevel (from _layout).
            trees = [repo] + [t for t in trees[1:] if t != repo]
        main = trees[0] if trees else repo
        out["repo"] = main
        g.repo = main
        out["worktrees"] = [t for t in trees if t != main]
        newest = g("for-each-ref", "--sort=-committerdate", "--count=1", "--format=%(committerdate:unix)")
        has_refs = None if newest is None else bool(newest.strip())
        remote_out = g("remote")
        remotes = remote_out.split() if remote_out is not None else None
        out["urls"] = _urls(g)
        head = g.raw("rev-parse", "-q", "--verify", "HEAD")
        out["head"] = head.stdout.strip() if head.returncode == 0 else None
        if remotes is not None and kinds & {"unpushed", "merged_gone"}:
            _branches(g, main, remotes, out)
        if "stash" in kinds:
            out["stashes"] = _stashes(g, main)
        clean = True
        for tree in [main] + out["worktrees"]:
            t = _Git(tree, warnings)
            d = _dirty(t) if kinds & {"dirty", "empty"} else (0, 0)
            if d is None or any(d):
                clean = False
            if d and any(d):
                out["dirty"].append({"repo": tree, "modified": d[0], "untracked": d[1]})
            det = _detached(t, tree) if "unpushed" in kinds else None
            if det:
                out["unpushed"].append(det)
        if remotes == [] and has_refs:
            n = g.count("--all")
            if n is not None:
                out["no_remote"].append({"repo": main, "commits": n})
        if has_refs:
            ts = int(newest.strip())
            days = int((now - ts) // 86400)
            if days > stale_days:
                last = datetime.fromtimestamp(ts, timezone.utc).isoformat()
                out["stale"].append({"repo": main, "last_commit": last, "days": days})
        elif has_refs is False and clean:
            out["empty"].append({"repo": main})
    except _Timeout as e:
        warnings.append(f"{g.repo}: timed out; skipped")
        out = {k: ([] if isinstance(v, list) else v) for k, v in out.items()}
        out.update(repo=g.repo, warnings=warnings, worktrees=[],
                   skipped={"path": g.repo, "reason": "timed out", "error": f"git {e.args[0]} timed out"})
    return out


# ---------------------------------------------------------------- duplicates

def _local_key(url: str, repo: str) -> str:
    path = url[len("file://"):] if url.lower().startswith("file://") else url
    p = Path(path).expanduser()
    return os.path.normpath(str(p if p.is_absolute() else Path(repo) / p))


def _real_host(host: str, cfg) -> str:
    host = host.lower()
    for i in getattr(cfg, "identities", None) or ():
        if i.ssh_alias and i.ssh_alias.lower() == host:
            return i.host.lower()
    return host


def dup_key(url: str, repo: str, cfg) -> str:
    """host/owner/repo of a remote url, lowercased, ssh alias mapped to its host; a local path otherwise."""
    from .resolve import parse_url
    host, owner, name = parse_url(url)
    if not host or not name:
        return _local_key(url, repo)
    return "/".join(x for x in (_real_host(host, cfg), (owner or "").lower(), name.lower()) if x)


def _ahead(a: dict, b: dict) -> Optional[int]:
    """Commits of b's HEAD that a's HEAD lacks, reading b's objects as alternates; None when git can't tell.

    --missing=print turns off rev-list's lazy fetch on every git version (fetch_if_missing = 0), so a
    partial clone never asks its promisor remote for an object neither repo has (git < 2.44 ignores
    GIT_NO_LAZY_FETCH); a missing commit fails the walk or prints "?<oid>", and either means "can't tell".
    Only commits are walked, so a blob:none or tree:0 clone still compares."""
    g = _Git(a["repo"], [])
    try:
        r = g.raw("rev-list", "--missing=print", f"HEAD..{b['head']}",
                  extra={"GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(b["common"], "objects")})
    except _Timeout:
        return None
    lines = r.stdout.split()
    if r.returncode != 0 or any(ln.startswith("?") for ln in lines):
        return None
    return len(lines)


def _duplicates(results: List[dict], cfg) -> List[dict]:
    groups: Dict[str, List[dict]] = {}
    for r in results:
        origin = dict(r["urls"]).get("origin")
        if origin and not r["skipped"]:
            groups.setdefault(dup_key(origin, r["repo"], cfg), []).append(r)
    found = []
    for k, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        members.sort(key=lambda r: r["repo"])
        first, diverged, details = members[0], False, []
        for other in members[1:]:
            if other["head"] == first["head"]:
                continue
            if not (other["head"] and first["head"]):
                diverged = None if diverged is False else diverged
                continue
            theirs, ours = _ahead(first, other), _ahead(other, first)
            if theirs is None or ours is None:
                diverged = None if diverged is False else diverged
                continue
            for n, has, lacks in ((theirs, other, first), (ours, first, other)):
                if n:
                    diverged = True
                    details.append(f"{has['repo']} has {plural(n, 'commit')} not in {lacks['repo']}")
        if len(details) > DUP_DETAILS:
            details = details[:DUP_DETAILS] + [f"and {len(details) - DUP_DETAILS} more"]
        detail = "; ".join(details) if diverged else ("HEADs differ; could not compare" if diverged is None else None)
        found.append({"key": k, "repos": [m["repo"] for m in members], "diverged": diverged, "detail": detail})
    return found


# ---------------------------------------------------------------- remote_gone (online)

_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_|github_pat_|glpat-)[A-Za-z0-9_\-]+")
_USERINFO_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*)://([^/@\s]+)@")


def redact(text: Optional[str]) -> Optional[str]:
    """text with GitHub/GitLab tokens and URL credentials replaced by [REDACTED] (an ssh:// user name stays)."""
    if not text:
        return text
    text = _TOKEN_RE.sub("[REDACTED]", text)

    def userinfo(m: "re.Match") -> str:
        keep = m[1].lower() in ("ssh", "git+ssh", "ssh+git") and ":" not in m[2]
        return m[0] if keep else f"{m[1]}://[REDACTED]@"
    return _USERINFO_RE.sub(userinfo, text)


def _cli_guess(host: str, accounts) -> Optional[str]:
    """gh or glab for a host: the CLI of a configured account there, else by name; None when unsupported."""
    for a in accounts:
        if a.host == host:
            return a.cli
    return "glab" if "gitlab" in host else "gh" if "github" in host else None


def _account(url: str, host: str, cfg, accounts) -> Tuple[Optional[object], str]:
    """(account, "") to ask about a remote, or (None, reason). The identity that owns the url (resolve's
    match) lends its own login on its own host, else [default] lends its github.com/gitlab.com login;
    another identity's login is never borrowed."""
    from .forge import GITHUB, GITLAB, Account
    from .resolve import match_url
    ident = match_url(url, cfg)[0] if cfg is not None and hasattr(cfg, "identities") else None
    if ident is not None and ident.host.lower() == host:
        for cli in ("gh", "glab"):
            login = getattr(ident, f"{cli}_account", None)
            if login:
                return Account(ident.host, cli, login), ""
    default = getattr(cfg, "default", None)
    if host == GITHUB and getattr(default, "gh_account", None):
        return Account(GITHUB, "gh", default.gh_account), ""
    if host == GITLAB and getattr(default, "glab_account", None):
        return Account(GITLAB, "glab", default.glab_account), ""
    cli = _cli_guess(host, accounts)
    if cli is None:
        return None, f"remote check not supported for {host}"
    if ident is not None:
        return None, f"identity {ident.id} has no {cli}_account"
    return None, f"no {cli}_account in config for {host}"


def _renamed_url(url: str, owner: str, name: str, new: str) -> Optional[str]:
    m = re.search(re.escape(f"{owner}/{name}") + r"(\.git)?/?$", url, re.I)
    return url[:m.start()] + new + (m[1] or "") if m else None


def remote_gone(results: List[dict], cfg, warnings: List[str], *, announce_to=None) -> List[dict]:
    """Ask gh/glab whether each distinct host/owner/repo still exists; one entry per remote that does not
    (status missing, renamed or unknown). One warning per host sums up its unknown answers."""
    from . import forge
    from .resolve import parse_url
    accounts = forge.accounts(cfg) if cfg is not None and hasattr(cfg, "identities") else []
    remotes = []      # (repo, remote, url, (host, owner, name))
    for r in results:
        if r["skipped"]:
            continue
        for remote, url in r["urls"]:
            host, owner, name = parse_url(url)
            if host and owner and name:
                remotes.append((r["repo"], remote, url, (_real_host(host, cfg), owner, name)))
    asks: Dict[Tuple[str, str, str], Tuple[Optional[object], str]] = {}
    for _, _, url, ident in remotes:
        if ident not in asks:
            asks[ident] = _account(url, ident[0], cfg, accounts)
    used = list(dict.fromkeys(a for a, _ in asks.values() if a is not None))
    status: Dict[Tuple[str, str, str], Tuple[str, Optional[str], Optional[str]]] = {}
    broken: Dict[object, str] = {}    # account -> why gh/glab can't act as it (no login, CLI missing)
    if used:
        n = sum(a is not None for a, _ in asks.values())
        with contextlib.redirect_stdout(announce_to or sys.stdout):
            forge.announce(f"whether {plural(n, 'repo')} exist" if n != 1 else "whether 1 repo exists", used, SENDS)
    for ident, (acc, why) in sorted(asks.items(), key=lambda kv: tuple(x.lower() for x in kv[0])):
        if acc is None:
            status[ident] = ("unknown", why, None)
            continue
        if acc in broken:
            status[ident] = ("unknown", broken[acc], acc.cli)
            continue
        try:
            st, detail = forge.repo_exists(acc, ident[1], ident[2])
        except ConfigError as e:
            broken[acc] = redact(str(e)) or f"no {acc.cli} login"
            status[ident] = ("unknown", broken[acc], acc.cli)
            continue
        detail = redact(detail)
        if st == "unknown" and detail and (detail.endswith("command not found") or " timed out after " in detail):
            broken[acc] = detail          # don't wait on a missing or hanging CLI once per repo
        status[ident] = (st, detail, acc.cli)
    found = []
    unknown: Dict[str, Tuple[Set[str], List[str]]] = {}     # host -> (repos, reasons)
    for repo, remote, url, ident in remotes:
        st, detail, via = status[ident]
        if st == "ok":
            continue
        if st == "unknown":
            repos, reasons = unknown.setdefault(ident[0], (set(), []))
            repos.add(repo)
            if detail and detail not in reasons:
                reasons.append(detail)
        shown = redact(url)
        new = detail if st == "renamed" else None
        fixed = _renamed_url(url, ident[1], ident[2], new) if new and shown == url else None
        found.append({"repo": repo, "remote": remote, "url": shown, "status": st, "renamed_to": new,
                      "detail": detail if st == "unknown" else None,
                      "fix": (f"git -C {shlex.quote(repo)} remote set-url {shlex.quote(remote)} {shlex.quote(fixed)}"
                              if fixed else None),
                      "via": via})
    for host, (repos, reasons) in sorted(unknown.items()):
        n = plural(len(repos), "repo")
        first = reasons[0] if reasons else "no answer"
        more = f" (+{plural(len(reasons) - 1, 'other reason')}; -v lists them)" if len(reasons) > 1 else ""
        if first == f"remote check not supported for {host}":
            warnings.append(f"{first}; {n} unknown")
        else:
            warnings.append(f"remote check failed for {host}: {first}{more}; {n} unknown")
    return sorted(found, key=lambda e: (e["repo"], e["remote"]))


# ---------------------------------------------------------------- collect

def _sorted(items: List[dict], *fields: str) -> List[dict]:
    return sorted(items, key=lambda e: tuple("" if e.get(f) is None else str(e.get(f)) for f in fields))


def collect(roots: Sequence[Path], exclude: Sequence[str], cfg, *, online: bool, kinds: Set[str],
            stale_days: int, repos: Optional[Sequence[Path]] = None, roots_source: str = "args",
            announce_to=None) -> dict:
    """The report (the --json object without ok/command). repos skips discovery. cfg may be None."""
    from . import scan
    if repos is None:
        import inspect
        params = inspect.signature(scan.find).parameters
        paths = scan.find(list(roots), exclude=list(exclude)) if "exclude" in params else scan.find(list(roots))
    else:
        paths = list(repos)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        layouts = list(pool.map(_layout, paths))
    skipped = [{"path": str(lay["path"]), "reason": lay["skip"][0], "error": lay["skip"][1]}
               for lay in layouts if "skip" in lay]
    groups: Dict[str, List[str]] = {}
    notes: List[str] = []
    for lay in layouts:
        if lay.get("bare"):
            if repos is None:
                continue                  # its worktrees, when under the roots, are found on their own
            trees = _worktrees(_Git(str(lay["path"]), notes))
            if not trees:
                notes.append(f"{lay['path']}: bare repository with no worktrees{_NOTHING}")
            for t in trees:
                groups.setdefault(lay["common"], []).append(t)
        elif "common" in lay:
            groups.setdefault(lay["common"], []).append(str(lay["path"]))
    now = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda c: _probe(sorted(groups[c])[0], c, kinds, stale_days, now), sorted(groups)))
    results.sort(key=lambda r: r["repo"])
    warnings = notes + [w for r in results for w in r["warnings"]]
    skipped += [r["skipped"] for r in results if r["skipped"]]
    rep: dict = {"roots": [str(r) for r in roots], "roots_source": roots_source, "repos": len(results),
                 "online": online}
    for k in KINDS:
        rep[key(k)] = []
    for k in ("unpushed", "merged_gone", "stash", "dirty", "no_remote", "stale", "empty"):
        if k in kinds:
            rep[key(k)] = [e for r in results for e in r[key(k)]]
    if "duplicate" in kinds:
        rep["duplicates"] = _duplicates(results, cfg)
    if online and "remote_gone" in kinds:
        rep["remote_gone"] = remote_gone(results, cfg, warnings, announce_to=announce_to)
    rep["unpushed"] = _sorted(rep["unpushed"], "repo", "branch")
    rep["merged_gone"] = _sorted(rep["merged_gone"], "repo", "branch")
    rep["stashes"] = sorted(rep["stashes"], key=lambda e: (e["repo"], _stash_n(e["ref"])))
    for k in ("dirty", "no_remote", "stale", "empty"):
        rep[k] = _sorted(rep[k], "repo")
    rep["worktrees"] = {r["repo"]: sorted(r["worktrees"]) for r in results if r["worktrees"]}
    rep["skipped"] = _sorted(skipped, "path")
    rep["warnings"] = [redact(w) for w in warnings]
    rep["ok"] = not any(rep[k] for k in COUNTED if k != "remote_gone") and \
        not any(e["status"] != "unknown" for e in rep["remote_gone"])
    return rep


def _stash_n(ref: str) -> int:
    m = re.search(r"\{(\d+)\}", ref)
    return int(m[1]) if m else 0


# ---------------------------------------------------------------- text

def _tilde(path: str) -> str:
    from .undo import tilde
    return tilde(Path(path))


def _sh(path: str) -> str:
    """A tilde path quoted for the shell, the leading ~ left unquoted (as cli._sh)."""
    return path if path == "~" else "~/" + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)


def _fix_text(fix: str, repo: str) -> str:
    return fix.replace(f"-C {shlex.quote(repo)} ", f"-C {_sh(_tilde(repo))} ", 1)


def _detail_text(text: str, repos: Sequence[str]) -> str:
    for r in sorted(repos, key=len, reverse=True):
        text = text.replace(r, _tilde(r))
    return text


def _table(rows: List[List[str]]) -> List[str]:
    """Rows of cells, every column but the last padded to its widest cell; a trailing fix as '   → fix'."""
    if not rows:
        return []
    width = max(len(r) for r in rows)
    cols = [max((len(r[i]) for r in rows if len(r) > i + 1), default=0) for i in range(width)]
    lines = []
    for r in rows:
        cells = [c.ljust(cols[i]) if i < len(r) - 1 else c for i, c in enumerate(r)]
        lines.append("  " + "  ".join(cells).rstrip())
    return lines


def _with_fix(line: str, fix: Optional[str], repo: str) -> str:
    return f"{line}   → {_fix_text(fix, repo)}" if fix else line


def _unpushed_text(e: dict) -> str:
    if e["detached"]:
        return f"{plural(e['commits'], 'commit')} not on any branch"
    if not e["upstream"]:
        return f"{plural(e['commits'], 'commit')}, no upstream"
    if e["ahead"]:
        return f"{e['ahead']} ahead of {e['upstream']}"
    return f"{plural(e['commits'], 'commit')} not on any remote"


def _repos_in(items: List[dict]) -> int:
    return len({e["repo"] for e in items})


def render(report: dict, verbose: bool, *, only: Optional[Set[str]] = None, failed: Optional[int] = None) -> str:
    """The text report (spec 03 §2.3). only: the kinds --only asked for (None without --only); failed: how
    many checks failed (default: one per warning)."""
    rep = report
    lines = [f"{plural(rep['repos'], 'repo')} under {', '.join(_tilde(r) for r in rep['roots'])}"
             "  (remote state as of last fetch)", ""]
    body: List[str] = []

    def section(title: str, rows: List[Tuple[List[str], Optional[str], str]]) -> None:
        body.append(title)
        for line, (_, fix, repo) in zip(_table([r[0] for r in rows]), rows):
            body.append(_with_fix(line, fix, repo))

    up = rep["unpushed"]
    if up:
        section(f"unpushed ({plural(len(up), 'branch').replace('branchs', 'branches')} in "
                f"{plural(_repos_in(up), 'repo')})",
                [([_tilde(e["repo"]), "(detached HEAD)" if e["detached"] else e["branch"], _unpushed_text(e)],
                  e["fix"], e["repo"]) for e in up])
    mg = rep["merged_gone"]
    if mg:
        section(f"upstream gone ({plural(len(mg), 'branch').replace('branchs', 'branches')})",
                [([_tilde(e["repo"]), e["branch"], "merged, safe to delete" if not e["commits"]
                   else f"{plural(e['commits'], 'commit')} not on any remote"], None, e["repo"]) for e in mg])
    st = rep["stashes"]
    if st:
        section(f"stashes ({len(st)} in {plural(_repos_in(st), 'repo')})",
                [([_tilde(e["repo"]), f"{e['ref']} {e['date'][:10]}", e["subject"]], None, e["repo"]) for e in st])
    di = rep["dirty"]
    if di:
        section(f"dirty ({plural(len(di), 'repo')})",
                [([_tilde(e["repo"]), f"{e['modified']} modified, {e['untracked']} untracked"], None, e["repo"])
                 for e in di])
    nr = rep["no_remote"]
    if nr:
        section(f"no remote ({plural(len(nr), 'repo')})",
                [([_tilde(e["repo"]), f"{plural(e['commits'], 'commit')}, never pushed anywhere"], None, e["repo"])
                 for e in nr])
    du = rep["duplicates"]
    if du:
        body.append(f"duplicates ({plural(len(du), 'group')})")
        rows = []
        for g in du:
            state = ("diverged: " + _detail_text(g["detail"], g["repos"]) if g["diverged"]
                     else g["detail"] if g["diverged"] is None else "same HEAD")
            rows.append([g["key"] if "/" not in g["key"][:1] else _tilde(g["key"]),
                         "  ".join(_tilde(r) for r in g["repos"]), state])
        body.extend(_table(rows))
    rg = [e for e in rep["remote_gone"] if verbose or e["status"] != "unknown"]
    if rg:
        section(f"remote gone ({plural(_repos_in(rg), 'repo')})",
                [([_tilde(e["repo"]), e["url"],
                   f"missing via {e['via']}" if e["status"] == "missing"
                   else f"renamed to {e['renamed_to']}" if e["status"] == "renamed"
                   else "? unknown" + (f" ({e['detail']})" if e["detail"] else "")], e["fix"], e["repo"])
                 for e in rg])
    elif not rep["online"] and only is None:
        body.append("remote gone: run with --online to check remotes via gh/glab")
    show_stale = verbose or (only is not None and "stale" in only)
    if rep["stale"] and show_stale:
        section(f"stale ({plural(len(rep['stale']), 'repo')})",
                [([_tilde(e["repo"]), f"last commit {e['last_commit'][:10]}", f"{e['days']} days ago"], None,
                  e["repo"]) for e in rep["stale"]])
    if rep["empty"] and (verbose or (only is not None and "empty" in only)):
        section(f"empty ({plural(len(rep['empty']), 'repo')})", [([_tilde(e["repo"])], None, e["repo"])
                                                               for e in rep["empty"]])
    if verbose and rep["worktrees"]:
        section("worktrees", [([_tilde(m), "  ".join(_tilde(w) for w in ws)], None, m)
                              for m, ws in rep["worktrees"].items()])
    if verbose and rep["skipped"]:
        section(f"skipped ({len(rep['skipped'])})", [([_tilde(s["path"]), s["error"]], None, s["path"])
                                                     for s in rep["skipped"]])
    lines += body
    if body:
        lines.append("")
    lines.append(_summary(rep, show_stale, len(rep["warnings"]) if failed is None else failed))
    return "\n".join(lines)


def _summary(rep: dict, show_stale: bool, failed: int = 0) -> str:
    from .scan import skipped as skipped_text
    counted = [e for k in COUNTED if k not in ("duplicates", "remote_gone") for e in rep[k]]
    counted += [e for e in rep["remote_gone"] if e["status"] != "unknown"]
    repos = {e["repo"] for e in counted} | {r for g in rep["duplicates"] for r in g["repos"]}
    items = len(counted) + len(rep["duplicates"])
    where = ", ".join(_tilde(r) for r in rep["roots"])
    first = (f"{plural(items, 'item')} in {plural(len(repos), 'repo')}" if items
             else f"nothing lost found under {where} ({plural(failed, 'check')} failed)" if failed
             else f"✓ nothing lost under {where}")
    extra = []
    if rep["stale"] and not show_stale:
        extra.append(f"stale repos: {len(rep['stale'])} (-v lists them)")
    if rep["skipped"]:
        extra.append(skipped_text(rep["skipped"]))
    return " · ".join([first] + extra)


# ---------------------------------------------------------------- command

def _config(args) -> Tuple[object, object, List[str]]:
    """(full Config or None, Scan or None, warnings). A config whose other tables are broken still gives
    its [scan]; a broken [scan] is a warning unless -c named the file."""
    from . import config
    try:
        path, how = config.find(args.config, args.home)
    except ConfigError:
        if args.config:
            raise
        return None, None, []
    try:
        cfg = config.load_all(path, how)
        return cfg, cfg.scan, []
    except ConfigError:
        pass
    try:
        return None, config.load_scan(path), []
    except ConfigError as e:
        if args.config:
            raise
        where = "scanning ~" if not args.dirs else "ignoring it"
        return None, None, [f"{_tilde(str(path))}: {e}; {where}"]


def _roots(args, scan_cfg) -> Tuple[List[Path], List[str], str]:
    try:
        from gitpro.scan import effective_roots
    except ImportError:
        effective_roots = None
    if effective_roots is not None:
        roots, exclude, source = effective_roots(list(args.dirs), scan_cfg)
        return [Path(r) for r in roots], list(exclude), source
    if args.dirs:
        raw, source = list(args.dirs), "args"
    elif scan_cfg is not None:
        raw, source = list(scan_cfg.roots), "config"
    else:
        raw, source = ["~"], "default"
    out: List[Path] = []
    for r in raw:
        p = Path(r).expanduser().resolve()
        if p not in out:
            out.append(p)
    return out, list(scan_cfg.exclude) if scan_cfg is not None else [], source


def _kinds(only: Optional[str]) -> Set[str]:
    chosen = set()
    for k in (only or "").split(","):
        k = k.strip()
        if not k:
            continue
        if k not in KINDS:
            raise ConfigError(f"unknown kind '{k}' ({', '.join(KINDS)})")
        chosen.add(k)
    return chosen


def cmd_lost(args) -> int:
    cfg, scan_cfg, warnings = _config(args)
    only = _kinds(args.only) if args.only else None
    from . import forge
    online = bool(args.online) or (not args.offline and cfg is not None and cfg.forge.online)
    fell_back = False
    if online and not args.online and (only is None or "remote_gone" in only) and \
            not (forge.available("gh") or forge.available("glab")):
        # [forge] online = true is a preference, not a demand: without gh/glab, stay offline and say so.
        warnings.append("[forge] online = true, but neither gh nor glab is on PATH; checking offline "
                        "(remote gone skipped)")
        online, fell_back = False, True
    kinds = set(only) if only else set(OFFLINE_KINDS) | ({"remote_gone"} if online else set())
    if "remote_gone" in kinds and fell_back:
        return fail("lost --only remote_gone needs gh or glab on PATH (remote check); install one", args)
    if "remote_gone" in kinds and not online:
        accs = forge.accounts(cfg) if cfg is not None else []
        if args.json or not accs or args.offline:
            return fail("lost --only remote_gone needs the network; re-run with --online", args)
        return forge.no_online_error("lost", "whether each remote repo exists", accs, SENDS)
    if "remote_gone" in kinds and not (forge.available("gh") or forge.available("glab")):
        return fail("lost --online needs gh or glab on PATH (remote check); install one or run without --online",
                    args)
    stale_days = args.stale_days or (scan_cfg.stale_days if scan_cfg is not None else 180)
    for d in args.dirs:
        if not Path(d).expanduser().is_dir():
            return fail(f"{d}: no such directory", args)
    if args.repo:
        repos = [p.expanduser().resolve() for p in args.repo]
        for given, p in zip(args.repo, repos):
            if not p.is_dir():
                return fail(f"{given}: no such directory" if not p.exists() else f"{given}: not a directory", args)
        roots, exclude, source = repos, [], "args"
    else:
        repos = None
        roots, exclude, source = _roots(args, scan_cfg)
    rep = collect(roots, exclude, cfg, online=online, kinds=kinds, stale_days=stale_days, repos=repos,
                  roots_source=source, announce_to=sys.stderr if args.json else None)
    failed = sum(not w.endswith(_NOTHING) for w in rep["warnings"])
    rep["warnings"] = warnings + rep["warnings"]
    rc = 0 if rep["ok"] else 1
    if args.json:
        jsonout.emit("lost", rep)
        return rc
    for w in rep["warnings"]:
        print(f"⚠ {_detail_text(w, _paths(rep))}", file=sys.stderr)
    print(render(rep, args.verbose, only=only, failed=failed))
    return rc


def _paths(rep: dict) -> List[str]:
    paths = {e["repo"] for k in ("unpushed", "merged_gone", "stashes", "dirty", "no_remote", "remote_gone",
                                 "stale", "empty") for e in rep[k]}
    paths |= {s["path"] for s in rep["skipped"]} | set(rep["worktrees"])
    paths |= {w for ws in rep["worktrees"].values() for w in ws}
    return sorted(paths)


def _positive(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if n <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of days")
    return n


def register(sub, common, confirm) -> None:
    sp = sub.add_parser("lost", parents=[common],
                        help="unpushed branches, stashes, dirty trees, repos without a remote (read-only)")
    sp.add_argument("dirs", nargs="*", metavar="DIR", help="where to look (default: [scan] roots, else ~)")
    sp.add_argument("--repo", action="append", type=Path, metavar="PATH", help="check exactly this repo (repeatable)")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="also list stale and empty repos, worktrees, skipped paths and unknown remotes")
    only = sp.add_argument("--only", metavar="KIND[,KIND...]", help=", ".join(KINDS))
    only.gitpro_values = "kinds"
    net = sp.add_mutually_exclusive_group()
    net.add_argument("--online", action="store_true",
                     help="ask GitHub/GitLab via gh/glab whether each remote still exists (sends owner/repo names)")
    net.add_argument("--offline", action="store_true", help="stay offline even when [forge] online = true")
    sp.add_argument("--stale-days", type=_positive, metavar="N", help="stale after N days (default: [scan] stale_days, 180)")
    sp.set_defaults(func=lambda a: guarded(cmd_lost, a))
    capability("lost", "lost.online")
