"""`gitpro audit`: commits made as the wrong one of your identities, commits made with an AI agent, and the
trailer policy of the repo's identity.

Reading only, except `--rewrite`: it replays the unpushed commits of the current branch with git plumbing
(`commit-tree`, then one `update-ref` guarded by the old tip), fixing only the offending commits and
keeping every date; it never runs `rebase`, never touches pushed commits, the index or the work tree.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import config, jsonout, resolve, trailers, undo
from .args import ask as _confirm, capability, dry_run, preview_only
from .config import Config, ConfigError
from .util import sh_tilde as _sh

PROG = "gitpro"
DEFAULT_RANGE = "HEAD --not --remotes"
GIT_TIMEOUT = 10            # README §4.4: local git (small calls)
LONG_TIMEOUT = 120          # git log / rev-list over a whole history (--all on a big repo)
PUSHED_LIMIT = 1000         # pushed commits the default run checks (newest first); --all checks every one
SHOW = 10                   # rows per section without -v
FORMAT = "%H%x00%an%x00%ae%x00%cn%x00%ce%x00%cI%x00%P%x00%s%x00%(trailers:only,unfold)%x1e"
# Options --range may contain besides revisions; anything else (--output=..., say) is refused.
RANGE_OPTIONS = ("--not", "--all", "--remotes", "--branches", "--tags", "--first-parent", "--no-merges",
                 "--merges", "--since", "--until", "--after", "--before", "--max-count")
_CAUSES = ("ambiguous", "url_form", "override", "pin_stale", "global_override", "not_applied", "env_override")


@dataclass
class Commit:
    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    date: str                        # %cI
    parents: Tuple[str, ...]
    subject: str
    trailers: List[Tuple[str, str]]
    pushed: bool = False


# ---------------------------------------------------------------- git

def _git(repo: Path, *argv: str, stdin: Optional[str] = None, ok: Sequence[int] = (0,),
         env: Optional[Dict[str, str]] = None, what: Optional[str] = None,
         timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """git -C repo argv (never GIT_DIR & co. from the caller); ConfigError on a missing git, a timeout or
    an exit code not in ok ("<what> failed: <stderr>")."""
    try:
        r = subprocess.run(["git", "-C", str(repo), *argv], input=stdin, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env or resolve._child_env(),
                           timeout=timeout, **({} if stdin is not None else {"stdin": subprocess.DEVNULL}))
    except FileNotFoundError:
        raise ConfigError("git not found") from None
    except subprocess.TimeoutExpired:
        raise ConfigError(f"git {argv[0]} timed out after {timeout}s") from None
    if r.returncode not in ok:
        raise ConfigError(f"{what or 'git ' + argv[0]} failed: {r.stderr.strip() or r.returncode}")
    return r


def _lines(r: subprocess.CompletedProcess) -> List[str]:
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _parse_log(out: str) -> List[Commit]:
    commits = []
    for rec in out.split("\x1e"):
        rec = rec.lstrip("\n")
        if not rec:
            continue
        f = rec.split("\x00", 8)
        if len(f) != 9:
            continue
        commits.append(Commit(f[0], f[1], f[2], f[3], f[4], f[5], tuple(f[6].split()), f[7],
                              trailers.split_lines(f[8])))
    return commits


def log(repo: Path, revs: Sequence[str], *, shas: Optional[Sequence[str]] = None) -> List[Commit]:
    """The commits of a rev-list range (revs), newest first, or exactly `shas` in that order: one git log."""
    argv = ["log", "--no-color", "--no-show-signature", f"--format={FORMAT}"]
    if shas is not None:
        if not shas:
            return []
        return _parse_log(_git(repo, *argv, "--no-walk=unsorted", "--stdin", stdin="".join(s + "\n" for s in shas),
                               what="git log", timeout=LONG_TIMEOUT).stdout)
    return _parse_log(_git(repo, *argv, *revs, "--", what="git rev-list", timeout=LONG_TIMEOUT).stdout)


def range_tokens(text: str) -> List[str]:
    """--range split like a shell would; an option other than RANGE_OPTIONS is refused."""
    try:
        tokens = shlex.split(text)
    except ValueError as e:
        raise ConfigError(f"--range: {e}") from None
    if not tokens:
        raise ConfigError("--range is empty")
    for t in tokens:
        if t.startswith("-") and t.split("=", 1)[0] not in RANGE_OPTIONS:
            raise ConfigError(f"--range: {t} is not a revision")
    return tokens


def _branch(top: Path) -> Optional[str]:
    r = _git(top, "symbolic-ref", "-q", "--short", "HEAD", ok=(0, 1, 128))
    return r.stdout.strip() or None if r.returncode == 0 else None


def _has_head(top: Path) -> bool:
    return _git(top, "rev-parse", "-q", "--verify", "HEAD^{commit}", ok=range(256)).returncode == 0


# ---------------------------------------------------------------- classification

def classify(commit: Commit, cfg: Config) -> List[dict]:
    """Agent hits of one commit: agent_trailer (a Co-Authored-By whose email is in [agents].bot_emails),
    agent_author (author or committer email in bot_emails), co_authored (any other Co-Authored-By)."""
    bots = cfg.agents.bot_emails
    out: List[dict] = []
    for k, v in commit.trailers:
        if trailers.is_co_author(k):
            bot = any(trailers.glob_match(g, trailers.email_of(v)) for g in bots)
            out.append({"kind": "agent_trailer" if bot else "co_authored", "who": v})
    seen: Set[str] = set()
    for name, email in ((commit.author_name, commit.author_email), (commit.committer_name, commit.committer_email)):
        if email.casefold() not in seen and any(trailers.glob_match(g, email) for g in bots):
            seen.add(email.casefold())
            out.append({"kind": "agent_author", "who": f"{name} <{email}>".strip()})
    return out


def _violations(c: Commit, policy: trailers.Policy, own_cf: Set[str]) -> List[str]:
    """Trailer policy breaches of one of the user's own commits (a colleague's commits are never judged;
    merge commits need no Co-Authored-By under require)."""
    if c.author_email.casefold() not in own_cf and c.committer_email.casefold() not in own_cf:
        return []
    p = policy
    if len(c.parents) > 1 and p.co_authored_by == "require":
        p = trailers.Policy("allow", p.forbid_emails, p.who, p.table, p.emails_table)
    return trailers.check(c.trailers, p)


# ---------------------------------------------------------------- the audit

def _row(c: Commit) -> dict:
    return {"sha": c.sha, "date": c.date, "subject": c.subject, "pushed": c.pushed}


def collect(repo: Path, cfg: Config, range: Optional[str] = None, all: bool = False, *,
            home: Optional[Path] = None, authors: bool = True, agents: bool = True) -> dict:
    """The audit of repo as the `--json` object of spec 08 §3 (without "command"; "ok" included), plain
    JSON values only. Reads only. range: a git rev-list range (shell-split); all: every commit of HEAD;
    authors / agents: run that audit (the other one's keys are null)."""
    return _public(_collect(repo, cfg, range, all, home=home, authors=authors, agents=agents))


def _collect(repo: Path, cfg: Config, range: Optional[str] = None, all: bool = False, *,
             home: Optional[Path] = None, authors: bool = True, agents: bool = True) -> dict:
    """collect() plus keys starting with "_" that hold Python objects for render() and rewrite()."""
    resolve.require_git()
    top = resolve.is_repo(Path(repo))
    if top is None:
        raise ConfigError(f"{repo}: not a git repository")
    report = resolve.compare(top, cfg, home=home)
    exp = report.expected
    warnings: List[str] = []
    branch = _branch(top)
    pushed_set = set(_lines(_git(top, "rev-list", "--remotes", timeout=LONG_TIMEOUT)))
    label, checked_all = DEFAULT_RANGE, True
    if range:
        tokens = range_tokens(range)
        _git(top, "rev-list", "--count", *tokens, "--", what="git rev-list", timeout=LONG_TIMEOUT)
        commits = log(top, tokens)
        label, total = range, len(commits)
        for c in commits:
            c.pushed = c.sha in pushed_set
        n_pushed = sum(c.pushed for c in commits)
    elif not _has_head(top):
        commits, total, n_pushed = [], 0, 0
        label = "HEAD" if all else DEFAULT_RANGE
    else:
        shas = _lines(_git(top, "rev-list", "HEAD"))
        total = len(shas)
        unpushed = [s for s in shas if s not in pushed_set]
        pushed = [s for s in shas if s in pushed_set]
        n_pushed = len(pushed)
        if all:
            label = "HEAD"
            commits = log(top, ["HEAD"])
        else:
            checked_all = len(pushed) <= PUSHED_LIMIT
            commits = log(top, [], shas=unpushed + pushed[:PUSHED_LIMIT])
        for c in commits:
            c.pushed = c.sha in pushed_set

    data: dict = {
        "repo": str(top), "branch": branch,
        "identity": {"id": exp.id, "email": exp.email},
        "range": label, "commits": total, "pushed": n_pushed, "unpushed": total - n_pushed,
        "checked": len(commits),
        "wrong_author": None, "rewrite": None, "agents": None, "co_authored": None, "policy": None,
        "rewritten": None,
    }
    own = resolve.own_emails(cfg)
    ok = True
    if authors and exp.email is None:
        warnings.append(f"no identity for {_where(report)}; author check skipped")
    elif authors:
        offending = {e for e in own if e != exp.email}
        wrong = []
        for c in commits:
            why = "committer" if c.committer_email in offending else "author" if c.author_email in offending else None
            if why:
                wrong.append(dict(_row(c), author_email=c.author_email, committer_email=c.committer_email, why=why))
        data["wrong_author"] = wrong
        fix = {w["sha"] for w in wrong if not w["pushed"]}
        data["rewrite"] = plan_rewrite(top, branch, fix)
        data["_offending"] = offending
        ok = not wrong
    if agents:
        policy = trailers.policy_for(exp.match.identity, cfg.default)
        own_cf = {e.casefold() for e in own}
        hits, co_authored, bad = [], 0, []
        for c in commits:
            kinds = classify(c, cfg)
            hits += [dict(sha=c.sha, kind=k["kind"], who=k["who"], pushed=c.pushed, date=c.date, subject=c.subject,
                          committer_email=c.committer_email) for k in kinds if k["kind"] != "co_authored"]
            co_authored += any(k["kind"] == "co_authored" for k in kinds)
            msgs = _violations(c, policy, own_cf) if policy.active() else []
            if msgs:
                bad.append(dict(_row(c), committer_email=c.committer_email, messages=msgs))
        data["agents"] = hits
        data["co_authored"] = co_authored
        data["policy"] = {"identity": exp.id, "co_authored_by": policy.co_authored_by,
                          "forbid_emails": list(policy.forbid_emails), "violations": len(bad), "commits": bad}
        data["_policy"] = policy
        ok = ok and not bad
    data["ok"] = ok
    if not checked_all:
        warnings.append(f"checked the newest {PUSHED_LIMIT} of {n_pushed} pushed commits; --all checks every one")
    if warnings:
        data["warnings"] = warnings
    data["_report"] = report
    return data


def _where(report) -> str:
    """<host>/<owner> of the repo's first remote, else the remote url, else the repo."""
    urls = list(report.effective.remotes.values())
    if not urls:
        return resolve._tilde(str(report.repo))
    host, owner, _ = resolve.parse_url(urls[0])
    redact = getattr(resolve, "redact_url", None) or (lambda u: u)
    return f"{host}/{owner}" if host and owner else redact(urls[0])


# ---------------------------------------------------------------- rewrite

def plan_rewrite(top: Path, branch: Optional[str], fix: Set[str]) -> dict:
    """Whether the unpushed wrong-author commits `fix` can be replayed: the base is the newest commit of the
    branch's first-parent line that a remote-tracking ref contains (the merge-base with its upstream in the
    usual case); every commit above it must have one parent and the offenders must all be among them."""
    out = {"possible": False, "base": None, "commits": len(fix), "replayed": 0, "reason": None}
    if not fix:
        out["reason"] = "no unpushed wrong-author commits"
        return out
    rows = [ln.split() for ln in _lines(_git(top, "rev-list", "--first-parent", "--parents", "HEAD",
                                             "--not", "--remotes"))]
    chain = [(r[0], tuple(r[1:])) for r in reversed(rows)]
    if chain:
        out["base"] = chain[0][1][0] if chain[0][1] else None
    if branch is None:
        out["reason"] = "HEAD is detached"
    elif any(len(p) > 1 for _, p in chain):
        out["reason"] = "merge commits in range"
    elif not fix <= {s for s, _ in chain}:
        out["reason"] = "some of them are not on the current branch"
    else:
        first = next(n for n, (s, _) in enumerate(chain) if s in fix)
        out.update(possible=True, replayed=len(chain) - first)
        out["_chain"] = chain
    return out


_SIG_HEADERS = (b"gpgsig", b"gpgsig-sha256")
_RAW_IDENT = re.compile(rb"^(.*) <([^<>]*)> (.*)$", re.S)


def _raw_commits(top: Path, shas: Sequence[str]) -> Dict[str, bytes]:
    """The raw objects (`git cat-file commit`) of shas, byte for byte, in one `cat-file --batch`."""
    if not shas:
        return {}
    try:
        r = subprocess.run(["git", "-C", str(top), "cat-file", "--batch"], input="".join(s + "\n" for s in shas).encode(),
                           capture_output=True, env=resolve._child_env(), timeout=LONG_TIMEOUT)
    except FileNotFoundError:
        raise ConfigError("git not found") from None
    except subprocess.TimeoutExpired:
        raise ConfigError(f"git cat-file timed out after {LONG_TIMEOUT}s") from None
    if r.returncode:
        sys.stderr.write(r.stderr.decode("utf-8", "replace"))
        raise ConfigError("rewrite failed; the branch was not changed")
    out: Dict[str, bytes] = {}
    buf, pos = r.stdout, 0
    for sha in shas:
        nl = buf.index(b"\n", pos)
        head = buf[pos:nl].split()
        if len(head) != 3 or head[1] != b"commit":
            raise ConfigError(f"cannot read commit {sha[:12]}")
        size = int(head[2])
        out[sha] = buf[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return out


def _split(raw: bytes) -> Tuple[List[List[bytes]], bytes]:
    """(headers, message): each header is its first line plus its continuation lines (" ..."); message is
    everything after the first blank line, untouched."""
    head, sep, msg = raw.partition(b"\n\n")
    headers: List[List[bytes]] = []
    for line in head.split(b"\n"):
        if line.startswith(b" ") and headers:
            headers[-1].append(line)
        else:
            headers.append([line])
    return headers, msg if sep else b""


def _key(h: List[bytes]) -> bytes:
    return h[0].split(b" ", 1)[0]


def _value(headers: List[List[bytes]], key: bytes) -> Optional[bytes]:
    return next((h[0][len(key) + 1:] for h in headers if _key(h) == key), None)


def is_signed(raw: bytes) -> bool:
    return any(_key(h) in _SIG_HEADERS for h in _split(raw)[0])


def gpgsign(top: Path) -> bool:
    """commit.gpgsign as git would apply it in top (git commit-tree itself ignores it)."""
    r = _git(top, "config", "--bool", "commit.gpgsign", ok=range(256))
    return r.returncode == 0 and r.stdout.strip() == "true"


def _ident_of(headers: List[List[bytes]], key: bytes, sha: str) -> Tuple[bytes, bytes, bytes]:
    m = _RAW_IDENT.match(_value(headers, key) or b"")
    if not m:
        raise ConfigError(f"cannot read the {key.decode()} of {sha[:12]}")
    return m[1], m[2], m[3]


def _encoded(text: str, headers: List[List[bytes]]) -> bytes:
    enc = (_value(headers, b"encoding") or b"utf-8").decode("ascii", "replace")
    try:
        return text.encode(enc)
    except (LookupError, UnicodeEncodeError):
        return text.encode("utf-8")


def _write_commit(top: Path, body: bytes) -> str:
    r = subprocess.run(["git", "-C", str(top), "hash-object", "-t", "commit", "-w", "--stdin"], input=body,
                       capture_output=True, env=resolve._child_env(), timeout=GIT_TIMEOUT)
    if r.returncode or not r.stdout.strip():
        sys.stderr.write(r.stderr.decode("utf-8", "replace"))
        raise ConfigError("rewrite failed; the branch was not changed")
    return r.stdout.decode().strip()


def _commit_tree_signed(top: Path, sha: str, headers: List[List[bytes]], msg: bytes, parent: Optional[str],
                        author: Tuple[bytes, bytes, bytes], committer: Tuple[bytes, bytes, bytes], tmp: Path) -> str:
    """`git commit-tree -S` (signs as the user's config says: gpg.format, user.signingkey, gpg.program).
    Name, email and date go through git's ident parser, so unusual idents are normalised (`;` stripped,
    -0000 written as +0000) and extra headers are not kept; the tree, message and dates are."""
    msgfile = tmp / "msg"
    msgfile.write_bytes(msg)
    env: Dict = dict(resolve._child_env())
    for prefix, (name, email, date) in (("AUTHOR", author), ("COMMITTER", committer)):
        stamp = date.split()
        env[f"GIT_{prefix}_NAME".encode()] = name
        env[f"GIT_{prefix}_EMAIL".encode()] = email
        env[f"GIT_{prefix}_DATE".encode()] = b"@" + b" ".join(stamp[:2]) if stamp else date
    encoding = _value(headers, b"encoding")
    enc = ["-c", f"i18n.commitEncoding={encoding.decode('ascii', 'replace')}"] if encoding else []
    argv = ["git", "-C", str(top), *enc, "commit-tree", "-S", f"{sha}^{{tree}}",
            *(["-p", parent] if parent else []), "-F", str(msgfile)]
    r = subprocess.run(argv, capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=LONG_TIMEOUT)
    if r.returncode or not r.stdout.strip():
        sys.stderr.write(r.stderr.decode("utf-8", "replace"))
        raise ConfigError("rewrite failed; the branch was not changed")
    return r.stdout.decode().strip()


def replay(top: Path, chain: Sequence[Tuple[str, Tuple[str, ...]]], fix: Set[str], offending: Set[str],
           name: str, email: str, *, sign: Optional[bool] = None) -> Tuple[str, int, Dict[str, str]]:
    """Recreate every commit of chain (oldest first) on its new parent; the author/committer of commits in
    `fix` whose email is in `offending` (exact match) become name <email>, dates always the originals.
    Commits below the first change keep their sha. Without signing, each new object is the original's raw
    bytes with only the parent line (and the fixed ident lines) replaced and the now invalid signature
    dropped, written with `git hash-object`; with commit.gpgsign set (or sign=True) every new commit goes
    through `git commit-tree -S` instead. Returns (new tip, number of new commits, {old sha: new sha})."""
    sign = gpgsign(top) if sign is None else sign
    raws = _raw_commits(top, [s for s, _ in chain])
    parent = chain[0][1][0] if chain[0][1] else None
    mapping: Dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="gitpro-audit-") as tmp:
        for sha, parents in chain:
            headers, msg = _split(raws[sha])
            idents = {}
            fixed = False
            for key in (b"author", b"committer"):
                n, e, d = _ident_of(headers, key, sha)
                if sha in fix and e.decode("utf-8", "surrogateescape") in offending:
                    n, e, fixed = _encoded(name, headers), email.encode("utf-8"), True
                idents[key] = (n, e, d)
            if not fixed and parent == (parents[0] if parents else None):
                parent = sha
                continue
            if sign:
                new = _commit_tree_signed(top, sha, headers, msg, parent, idents[b"author"], idents[b"committer"],
                                          Path(tmp))
            else:
                out: List[bytes] = []
                parent_done = False
                for h in headers:
                    key = _key(h)
                    if key in _SIG_HEADERS:
                        continue
                    if key == b"parent":
                        if not parent_done and parent:
                            out.append(b"parent " + parent.encode())
                        parent_done = True
                        continue
                    if key in idents:
                        n, e, d = idents[key]
                        out.append(key + b" " + n + b" <" + e + b"> " + d)
                        continue
                    out.extend(h)
                new = _write_commit(top, b"\n".join(out) + b"\n\n" + msg)
            mapping[sha] = new
            parent = new
    return parent, len(mapping), mapping


def notes_of(top: Path, shas: Sequence[str]) -> List[str]:
    """The shas among `shas` that carry a note in the default notes ref."""
    r = _git(top, "notes", "list", ok=range(256), timeout=LONG_TIMEOUT)
    if r.returncode:
        return []
    noted = {ln.split()[1] for ln in _lines(r) if len(ln.split()) == 2}
    return [s for s in shas if s in noted]


def _copy_notes(top: Path, mapping: Dict[str, str]) -> Tuple[int, Optional[str]]:
    """`git notes copy --stdin` for the replayed commits that have notes: (copied, error or None)."""
    pairs = [(o, mapping[o]) for o in notes_of(top, list(mapping))]
    if not pairs:
        return 0, None
    r = _git(top, "notes", "copy", "--stdin", stdin="".join(f"{o} {n}\n" for o, n in pairs), ok=range(256))
    if r.returncode:
        return 0, r.stderr.strip() or f"git notes copy exited {r.returncode}"
    return len(pairs), None


def _in_progress(top: Path) -> Optional[str]:
    gd = Path(_git(top, "rev-parse", "--absolute-git-dir").stdout.strip())
    for name, what in (("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"), ("MERGE_HEAD", "a merge"),
                       ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert")):
        if (gd / name).exists():
            return f"{what} is in progress"
    return None


def _still_wrong(report) -> Optional[str]:
    """The refusal when git would not commit as the expected identity next time, else None."""
    exp = report.expected
    env_p = next((p for p in report.problems if p.code == "env_override"), None)
    now = report.effective.email.value
    if env_p is None and now == exp.email:
        return None
    if env_p is not None:
        now = os.environ.get("GIT_COMMITTER_EMAIL") or os.environ.get("GIT_AUTHOR_EMAIL") or now
    cause = next((p for p in report.problems if p.code in _CAUSES), None)
    fix = resolve._shown_fix(cause) if cause is not None else ""
    fix = fix or f"{PROG} why {shlex.quote(str(report.repo))}"
    return f"git would still commit as {now or '(no email)'}; fix the repo first: {fix}"


def refuse_rewrite(data: dict) -> Optional[str]:
    """Why --rewrite cannot run on this audit, or None. 'nothing' when there is nothing to do."""
    top = Path(data["repo"])
    if data["wrong_author"] is None:
        return "no identity for this repo; nothing to rewrite to"
    if not data["wrong_author"]:
        return "nothing"
    if data["branch"] is None:
        return "HEAD is detached"
    busy = _in_progress(top)
    if busy:
        return busy
    if _lines(_git(top, "status", "--porcelain", "--untracked-files=no")):
        return "working tree has changes; commit or stash them first"
    wrong = _still_wrong(data["_report"])
    if wrong:
        return wrong
    pushed = [w for w in data["wrong_author"] if w["pushed"]]
    if pushed:
        n = len(pushed)
        return (f"{n} of the wrong-author commits {'is' if n == 1 else 'are'} pushed; rewrite only covers "
                f"unpushed commits (use --range to pick them)")
    rw = data["rewrite"]
    if not rw["commits"]:
        return "nothing"
    if not rw["possible"]:
        return f"cannot rewrite: {rw['reason']}; rebase by hand"
    return None


def _undo_line(top: Path, branch: str, old: str, new: str) -> str:
    return " ".join(shlex.quote(a) for a in ["git", "-C", str(top), "update-ref", "-m", "gitpro audit undo",
                                             f"refs/heads/{branch}", old, new])


def rewrite(data: dict, exp_name: str, exp_email: str) -> dict:
    """Replay data's rewrite plan (refuse_rewrite(data) must be None and the user must have agreed): undo
    script first, then commit-tree, then one update-ref guarded by the old tip. {"count", "undo", ...}."""
    from . import files
    top, branch, rw = Path(data["repo"]), data["branch"], data["rewrite"]
    chain = rw["_chain"]
    old = _git(top, "rev-parse", "HEAD").stdout.strip()
    if old != chain[-1][0]:
        raise ConfigError(f"{branch} changed while {PROG} was running; nothing written")
    fix = {w["sha"] for w in data["wrong_author"]}
    script = undo.write("audit --rewrite", [_undo_line(top, branch, old, old)], what=f"branch {branch} of {top}")
    try:
        new, changed, mapping = replay(top, chain, fix, data["_offending"], exp_name, exp_email)
        text = script.read_text(encoding="utf-8").replace(_undo_line(top, branch, old, old),
                                                          _undo_line(top, branch, old, new))
        files.atomic_write(script, text, 0o700)
        r = _git(top, "update-ref", "-m", f"{PROG} audit --rewrite", f"refs/heads/{branch}", new, old,
                 ok=range(256))
        if r.returncode:
            raise ConfigError(f"{branch} changed while {PROG} was running; nothing written")
    except BaseException:
        try:
            script.unlink()          # the branch was not changed: nothing to undo
        except OSError:
            pass
        raise
    notes, notes_error = _copy_notes(top, mapping)
    return {"count": len(fix), "undo": str(script), "old_tip": old, "new_tip": new, "replayed": changed,
            "notes": notes, "notes_error": notes_error}


# ---------------------------------------------------------------- text

def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def _rows(items: List[dict], verbose: bool, fmt) -> List[str]:
    shown = items if verbose else items[:SHOW]
    out = [fmt(i) for i in shown]
    if len(items) > len(shown):
        out.append(f"  … and {len(items) - len(shown)} more (-v lists all)")
    return out


def _rewrite_cmd(data: dict, args) -> str:
    argv = [PROG, "audit", "--fix"]
    if getattr(args, "config", None):
        argv += ["-c", str(args.config)]
    rw = data["rewrite"]
    if getattr(args, "range", None):
        argv += ["--range", args.range]
    elif any(w["pushed"] for w in data["wrong_author"]) and rw.get("base"):
        argv += ["--range", f"{rw['base'][:12]}..HEAD"]
    shown = " ".join(shlex.quote(a) for a in argv)
    return shown + " " + _sh(resolve._tilde(data["repo"]))


def render(data: dict, args) -> List[str]:
    """The text report, one line per item."""
    verbose = bool(getattr(args, "verbose", False))
    ident = data["identity"]
    who = (f"identity {ident['id'] or '[default]'} <{ident['email']}>" if ident["email"] else "no identity")
    where = f" in {data['range']}" if getattr(args, "range", None) else ""
    lines = [f"{resolve._tilde(data['repo'])}  {who}  {data['commits']} commit{'s' * (data['commits'] != 1)}{where} "
             f"({data['pushed']} pushed, {data['unpushed']} unpushed, as of last fetch)"]
    wrong, agents, pol = data["wrong_author"], data["agents"], data["policy"]
    nothing_else = not agents and not data["co_authored"] and not (pol and pol["violations"])
    if wrong == []:
        clean = f"✓ all {data['checked']} commits are committed as {ident['email']}"
        lines += ["", clean + ("; no agent trailers" if agents is not None and nothing_else else "")]
    elif wrong:
        up = [w for w in wrong if not w["pushed"]]
        lines += ["", f"wrong author ({len(up)} unpushed, {len(wrong) - len(up)} pushed)"]

        def row(w: dict) -> str:
            e = w["committer_email"] if w["why"] == "committer" else w["author_email"]
            state = "pushed · report only" if w["pushed"] else "unpushed"
            return f"  {w['sha'][:7]}  {w['date'][:10]}  {e:<24} {_short(w['subject'], 32):<32} {state}"
        lines += _rows(wrong, verbose, row)
        rw = data["rewrite"]
        if up and rw["possible"]:
            n = len(up)
            lines.append(f"→ rewrite the {n} unpushed: {_rewrite_cmd(data, args)}")
            if rw["replayed"] > n:
                lines.append(f"  (fixes only {'that commit' if n == 1 else f'those {n} commits'} and keeps every date; "
                             f"the commits above them get new ids but are otherwise unchanged)")
        elif up:
            lines.append(f"→ {len(up)} unpushed commit{'s' * (len(up) != 1)} need{'s' * (len(up) == 1)} a manual "
                         f"rebase ({rw['reason']})")
        if len(up) < len(wrong):
            lines.append("  pushed commits are left alone; rewriting shared history needs a force push and "
                         "everyone's agreement")
    if agents:
        lines += ["", f"agent commits ({len({a['sha'] for a in agents})})"]
        groups: Dict[Tuple[str, str], List[dict]] = {}
        for a in agents:
            groups.setdefault((a["kind"], a["who"]), []).append(a)
        policy = data.get("_policy")
        bad_shas = {b["sha"] for b in (pol["commits"] if pol else [])}
        for (kind, who_), items in groups.items():
            label = f"Co-Authored-By: {who_}" if kind == "agent_trailer" else f"author {who_}"
            mark = ""
            if kind == "agent_trailer" and policy is not None and policy.active():
                bad = any(a["sha"] in bad_shas for a in items)
                mark = f"   policy {ident['id'] or '[default]'}: {policy.co_authored_by}  {'✗' if bad else '✓'}"
            lines.append(f"  {label:<56} {len(items)} commit{'s' * (len(items) != 1)}{mark}")
            lines += _rows(items, verbose, lambda a: f"  {a['sha'][:7]}  {a['date'][:10]}  {a['committer_email']:<16} "
                                                     f"{_short(a['subject'], 28):<28} {label}")
    if data["co_authored"]:
        n = data["co_authored"]
        lines += ["", f"co-authored by people: {n} commit{'s' * (n != 1)}"]
    if pol and pol["violations"]:
        n = pol["violations"]
        policy = data["_policy"]
        lines += ["", f"✗ trailer policy of {policy.who}: {n} commit{'s' * (n != 1)} violate{'s' * (n == 1)} it"]
        lines += _rows(pol["commits"], verbose,
                       lambda b: f"  {b['sha'][:7]}  {b['date'][:10]}  {_short(b['subject'], 28):<28} "
                                 f"{'; '.join(b['messages'])}")
        setting = (f"{policy.table} co_authored_by = \"{policy.co_authored_by}\"" if policy.table
                   else f"{policy.emails_table} forbid_emails")
        lines.append(f"→ {setting} for {ident['id'] or '[default]'}; the commit-msg guard blocks new ones "
                     f"({PROG} guard status)")
    return lines


# ---------------------------------------------------------------- command

def _public(data: dict) -> dict:
    out = {k: v for k, v in data.items() if not k.startswith("_")}
    if isinstance(out.get("rewrite"), dict):
        out["rewrite"] = {k: v for k, v in out["rewrite"].items() if not k.startswith("_")}
    return out


def _preview(data: dict, exp) -> List[str]:
    rw = data["rewrite"]
    branch = data["branch"]
    base = rw["base"][:7] if rw["base"] else "the root"
    lines = [f"→ rewrite {rw['commits']} unpushed commit{'s' * (rw['commits'] != 1)} on {branch} (base {base}):"]
    off = data["_offending"]
    for w in reversed([w for w in data["wrong_author"] if not w["pushed"]]):
        what = [k for k, e in (("author", w["author_email"]), ("committer", w["committer_email"])) if e in off]
        emails = sorted({w[f"{k}_email"] for k in what})
        lines.append(f"  {w['sha'][:7]}  {_short(w['subject'], 32):<32} {', '.join(what)} {', '.join(emails)} "
                     f"→ {exp.email}")
    rest = rw["replayed"] - rw["commits"]
    if rest:
        lines.append(f"  {rest} descendant{'s' * (rest != 1)} get{'s' * (rest == 1)} new ids (same tree, message, "
                     f"author, committer and dates)")
    top = Path(data["repo"])
    replayed = [sha for sha, _ in rw["_chain"][len(rw["_chain"]) - rw["replayed"]:]]
    signed = sum(is_signed(raw) for raw in _raw_commits(top, replayed).values())
    r = rw["replayed"]
    if gpgsign(top):
        lines.append(f"  the {r} new commit{'s are' if r != 1 else ' is'} signed (commit.gpgsign = true)"
                     + (f"; {signed} of them had a signature before" if signed else ""))
    elif signed:
        lines.append(f"  {signed} signed commit{'s lose their' if signed != 1 else ' loses its'} signature "
                     f"(commit.gpgsign is off)")
    noted = len(notes_of(top, replayed))
    if noted:
        lines.append(f"  {noted} note{'s are' if noted != 1 else ' is'} copied to the new commit{'s' * (noted != 1)}")
    lines.append(f"  then: git -C {_sh(resolve._tilde(data['repo']))} update-ref -m 'gitpro audit --rewrite' "
                 f"refs/heads/{shlex.quote(branch)} <new tip> {rw['_chain'][-1][0][:12]}")
    return lines


def cmd_audit(args) -> int:
    as_json = jsonout.wants_json(args)
    flag = "--rewrite" if getattr(args, "rewrite_flag", False) else "--fix"   # errors name the flag given
    args.rewrite = args.rewrite or getattr(args, "rewrite_flag", False)
    if args.rewrite and getattr(args, "agents", False):
        raise ConfigError(f"{flag} needs the author audit; drop --agents")
    show_only = bool(args.rewrite and args.dry_run and preview_only(args))
    if args.rewrite and as_json and not args.yes and not show_only:
        raise ConfigError(f"--json needs -y with {flag} (there is no terminal to confirm on)")
    home = Path(args.home).expanduser().resolve()
    path, how = config.find(args.config, home)
    cfg = config.load_all(path, how)
    data = _collect(Path(args.dir), cfg, args.range, args.all, home=home,
                   authors=not args.agents, agents=not args.authors)
    if not as_json:
        for w in data.get("warnings", []):
            print(f"⚠ {w}", file=sys.stderr)
        print("\n".join(render(data, args)))
    if not args.rewrite:
        if as_json:
            jsonout.emit("audit", _public(data))
        return 0 if data["ok"] else 1
    why = refuse_rewrite(data)
    if why == "nothing":
        if as_json:
            jsonout.emit("audit", _public(data))
        else:
            print("✓ nothing to rewrite: no unpushed wrong-author commits")
        return 0 if data["ok"] else 1
    if why:
        raise ConfigError(why)
    exp = data["_report"].expected
    if not as_json:
        print()
        print("\n".join(_preview(data, exp)))
    n = data["rewrite"]["commits"]
    if show_only:
        if as_json:
            jsonout.emit("audit", dict(_public(data), dry_run=True))
        else:
            print(f"Dry run: nothing written. Rewrite the {n} unpushed commit{'s' * (n != 1)} with -y.")
        return 0 if data["ok"] else 1
    if not _confirm(f"rewrite {n} unpushed commit{'s' * (n != 1)}?", args.yes):
        print("✗ aborted, nothing written")
        return 1
    done = rewrite(data, exp.name or "", exp.email)
    rest_ok = not (data["policy"] and data["policy"]["violations"])
    data["rewritten"] = {"count": done["count"], "undo": done["undo"], "old_tip": done["old_tip"],
                         "new_tip": done["new_tip"], "notes": done["notes"]}
    if done["notes_error"]:
        data.setdefault("warnings", []).append(f"notes not copied: {done['notes_error']}")
    data["ok"] = rest_ok
    if as_json:
        jsonout.emit("audit", _public(data))
    else:
        c, r = done["count"], done["replayed"]
        print(f"✓ rewrote {c} commit{'s' * (c != 1)} ({r} new sha{'s' * (r != 1)}: {c} fixed, "
              f"{r - c} descendant{'s' * (r - c != 1)} re-parented)")
        if done["notes"]:
            print(f"✓ copied {done['notes']} note{'s' * (done['notes'] != 1)} to the new commits")
        if done["notes_error"]:
            print(f"⚠ notes not copied: {done['notes_error']}", file=sys.stderr)
        print(f"↩ undo: sh {_sh(undo.tilde(Path(done['undo'])))}")
    return 0 if rest_ok else 1


def register(sub, common, confirm) -> None:
    sp = sub.add_parser(
        "audit", parents=[common, confirm],
        help="find commits made as the wrong identity or with an AI agent; fix unpushed ones with --fix",
        description="Audit the history of a repo: commits whose committer (or your own other email as author) is "
                    "not the repo's identity, commits made with an AI agent (Co-Authored-By trailers, bot "
                    "emails) and the [*.trailers] policy. Default: the unpushed commits (as of the last fetch) "
                    "plus the newest pushed ones.",
        epilog="--fix fixes only the unpushed offending commits, keeps every date and never runs rebase; "
               "pushed commits are only reported. \"Pushed\" means reachable from a remote-tracking ref "
               "(refs/remotes/*) as of the last fetch: commits published only through a tag, `git push <url>` "
               "or Gerrit's refs/for/* count as unpushed, so fetch first. With commit.gpgsign set the new "
               "commits are signed; notes are copied to them. With the guard installed (gitpro setup) the commit-msg "
               "hook applies the trailer policy at commit time; skip it once in your own shell with "
               "GITPRO_SKIP=1 git commit ...")
    sp.add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--range", metavar="A..B", help="audit this git rev-list range instead")
    g.add_argument("--all", action="store_true", help="audit every commit reachable from HEAD")
    w = sp.add_mutually_exclusive_group()
    w.add_argument("--agents", action="store_true", help="only the agent / trailer audit")
    w.add_argument("--authors", action="store_true", help="only the wrong-author audit")
    sp.add_argument("--fix", dest="rewrite", action="store_true",
                    help="fix the unpushed wrong-author commits (asks first; writes an undo script)")
    sp.add_argument("--rewrite", dest="rewrite_flag", action="store_true", help=argparse.SUPPRESS)  # 0.4: --fix
    dry_run(sp)
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.add_argument("-v", "--verbose", action="store_true", help="list every commit, not the first ten")
    sp.set_defaults(func=cmd_audit)
    capability("audit", "audit.rewrite", "audit.fix", "trailers")
