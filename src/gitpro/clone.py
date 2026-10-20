"""`gitpro clone <url>`: git clone with the identity, ssh alias and folder the URL calls for (spec 07).

plan() picks the identity exactly as git will afterwards (resolve.match_url on the URL), rewrites the
URL to the identity's ssh alias when the identity asks for it, and chooses the target directory from
clone_dir / [clone] and the layout. run() runs `git clone` with the caller's umask. cmd_clone() shows
the plan, asks, clones and checks the result with resolve.compare. No network beyond git's own.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

from . import jsonout, resolve
from .args import capability
from .config import SCAN_CONTROL_RE, Config, ConfigError, Identity
from .util import expand_home as _expand

# scheme -> what kind of access it is ("auth" in --json for a URL that is kept as given)
_SCHEMES = {"http": "https", "https": "https", "ssh": "ssh", "git+ssh": "ssh", "ssh+git": "ssh",
            "git": "git", "file": "local"}
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
_SEG = r"[A-Za-z0-9._-]+"
_SHORT_RE = re.compile(rf"^{_SEG}(?:/{_SEG})+$")       # owner/repo, group/sub/repo, host/owner/repo
_PLACEHOLDER_RE = re.compile(r"\{(host|owner|repo|id)\}")


@dataclass(frozen=True)
class ClonePlan:
    url: str                       # as given
    clone_url: str                 # what git clone gets (and stores as origin)
    dir: Path                      # absolute target
    identity: Optional[Identity]   # None -> [default]
    auth: str                      # ssh-alias | ssh | https | git | local
    host: Optional[str]            # the real host (never the alias); None for a local source
    owner: Optional[str]
    repo: str
    name: Optional[str]            # who git will commit as (identity, else [default])
    email: Optional[str]
    key: Optional[str]             # the ssh key git will use (from the identity matching clone_url)
    forced: bool                   # identity came from --identity
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _Source:
    url: str                       # the URL to match and clone (shorthands expanded)
    kind: str                      # https | ssh | git | local
    host: Optional[str]
    owner: Optional[str]
    repo: str


# ---------------------------------------------------------------- plan

def _ssh_url(user: str, host: str, port: Optional[int], path: str) -> str:
    return f"ssh://{user}@{host}:{port}/{path}.git" if port else f"{user}@{host}:{path}.git"


def _find(cfg: Config, want: str) -> Identity:
    for i in cfg.identities:
        if i.id == want:
            return i
    have = ", ".join(i.id for i in cfg.identities) or "none"
    raise ConfigError(f"no identity {want!r} (have: {have})")


def _source(url: str, ident: Optional[Identity], cwd: Path) -> _Source:
    """What the user typed, classified: a URL with a scheme, scp-like, a local path, or a shorthand."""
    bad = ConfigError(f"cannot parse {url!r} as a git URL")
    if not url.strip() or SCAN_CONTROL_RE.search(url) or url.startswith("-"):
        raise bad
    m = _SCHEME_RE.match(url)
    if m:
        kind = _SCHEMES.get(m[1].lower())
        if kind == "https":        # a browser URL: ?tab=readme, #readme are not part of the repo
            url = url.split("#", 1)[0].split("?", 1)[0]
        host, owner, repo = resolve.parse_url(url)
        if kind is None or not repo or (kind != "local" and not host):
            raise bad
        return _Source(url, kind, None if kind == "local" else host, None if kind == "local" else owner, repo)
    if "::" in url:                # transport helpers (ext::, fd::) are not clone sources gitpro plans
        raise bad
    local = url.startswith(("/", "./", "../")) or os.path.exists(cwd / url)
    if not local and ":" in url and "/" not in url.split(":", 1)[0]:     # git's scp-like rule
        host, owner, repo = resolve.parse_url(url)
        if not host or not repo:
            raise bad
        return _Source(url, "ssh", host, owner, repo)
    if not local and _SHORT_RE.match(url):
        parts = url.split("/")
        if len(parts) >= 3 and "." in parts[0]:                      # gh-style host/owner/repo
            full = "https://" + url
            host, owner, repo = resolve.parse_url(full)
            return _Source(full, "https", host, owner, repo or parts[-1])
        if ident is None:
            raise ConfigError(f"'{url}' needs --identity ID (which host?)")
        owner, repo = "/".join(parts[:-1]), resolve.parse_url(url)[2] or parts[-1]
        if ident.ssh_key or ident.ssh_alias:
            full = _ssh_url(ident.ssh_user, ident.host, ident.ssh_port, f"{owner}/{repo}")
            return _Source(full, "ssh", ident.host, owner, repo)
        return _Source(f"https://{ident.host}/{owner}/{repo}", "https", ident.host, owner, repo)
    repo = resolve.parse_url(url)[2]
    if not local or not repo:
        raise bad
    return _Source(url, "local", None, None, repo)


def _tilde(path: Path, home: Path) -> str:
    from .plan import Env
    return Env(home).tilde(Path(path))


def _target(src: _Source, cfg: Config, ident: Optional[Identity], host: Optional[str], dir: Optional[str],
            cwd: Path, home: Path) -> Path:
    if dir:
        return Path(os.path.abspath(cwd / os.path.expanduser(str(dir))))
    raw = (ident.clone_dir if ident else cfg.default.clone_dir) or cfg.clone.dir
    base = Path(os.path.abspath(_expand(raw, home) if raw else cwd))
    if src.kind == "local":
        rel = src.repo
    else:
        vals = {"host": host or "", "owner": src.owner or "", "repo": src.repo, "id": ident.id if ident else "default"}
        rel = _PLACEHOLDER_RE.sub(lambda m: vals[m[1]], cfg.clone.layout)
    parts = [s for s in rel.split("/") if s]
    target = base.joinpath(*parts) if parts else base
    if not parts or any(s in (".", "..") for s in parts) or os.path.commonpath([base, target]) != str(base):
        raise ConfigError(f"target leaves {_tilde(base, home)}")
    return target


def plan(url: str, cfg: Config, *, dir: Optional[str] = None, identity: Optional[str] = None, ssh: bool = False,
         no_rewrite: bool = False, cwd: Optional[Path] = None, home: Optional[Path] = None) -> ClonePlan:
    """Everything `gitpro clone` will do, without doing it. Raises ConfigError for an unparsable URL, an
    unknown --identity, a target outside its base, or a target that exists."""
    home = Path.home() if home is None else Path(home)
    cwd = Path(os.getcwd() if cwd is None else cwd)
    forced = identity is not None
    ident = _find(cfg, identity) if forced else None
    src = _source(url, ident, cwd)
    if not forced:
        ident = resolve.match_url(src.url, cfg)[0]
    # An alias URL names the alias' host, also when its owner matches no identity (spec §2.4: {host} is real).
    real = next((i.host for i in cfg.identities if src.host and i.ssh_alias == src.host), src.host)
    on_host = ident is not None and src.host is not None and (real == ident.host or src.host == ident.ssh_alias)
    host = ident.host if on_host else real
    path = "/".join(x for x in (src.owner, src.repo) if x)
    if on_host and ident.ssh_alias and ident.rewrite and not no_rewrite:
        # What the generated url.insteadOf gives: user@alias:owner/... (the alias' ssh Host carries the port).
        clone_url, auth = f"{ident.ssh_user}@{ident.ssh_alias}:{path}.git", "ssh-alias"
    elif ssh and src.kind in ("https", "git"):
        clone_url, auth = _ssh_url(ident.ssh_user if on_host else "git", host, ident.ssh_port if on_host else None,
                                   path), "ssh"
    else:
        clone_url, auth = src.url, src.kind
    target = _target(src, cfg, ident, host, dir, cwd, home)
    if os.path.lexists(target):
        raise ConfigError(f"{_tilde(target, home)} already exists")
    warnings = []
    after = resolve.match_url(clone_url, cfg)[0]       # what git will include for the stored remote url
    if after != ident:
        warnings.append(f"git will use {after.id if after else '[default]'} for {resolve.redact_url(clone_url)}, not "
                        f"{ident.id if ident else '[default]'}")
    d = cfg.default
    # The key git will use: the identity whose includeIf matches the stored URL (warned above when it differs).
    key = after.ssh_key if after else (d.ssh_key if host in d.ssh_hosts else None)
    return ClonePlan(url, clone_url, target, ident, auth, host, src.owner, src.repo,
                     ident.name if ident else d.name, ident.email if ident else d.email, key, forced,
                     tuple(warnings))


def preview(p: ClonePlan, home: Path) -> List[str]:
    """The lines shown before asking (and by --dry-run)."""
    key = p.key if not p.key or p.key.startswith("~/") else _tilde(Path(p.key), home)
    lines = [f"clone {resolve.redact_url(p.clone_url)}", f"  into      {_tilde(p.dir, home)}"]
    i = p.identity
    if i is not None:
        why = "--identity" if p.forced else (f"owner {p.owner} on {p.host}" if i.owners else f"any owner on {p.host}")
        lines.append(f"  identity  {i.id}  {i.name} <{i.email}>   ({why})")
    else:
        who = f"{p.name or '(no name)'} <{p.email}>" if p.email else "no name/email in [default]; git will refuse to commit"
        where = f"{p.host}/{p.owner}" if p.host and p.owner else p.host or "a local path"
        lines.append(f"  identity  (default)  {who}   (no identity matches {where})")
    if p.auth == "ssh-alias":
        lines.append(f"  ssh       alias {i.ssh_alias} → {i.host}" + (f", key {key}" if key else ""))
    elif p.auth == "ssh":
        lines.append(f"  ssh       key {key}" if key else "  ssh       your ssh config picks the key")
    elif p.auth == "https":
        lines.append("  auth      https (credential helper)" + (f"; add --ssh to use key {key}" if key else ""))
    return lines


# ---------------------------------------------------------------- run

class _Terminated(Exception):
    pass


@contextlib.contextmanager
def _sigterm_raises() -> Iterator[None]:
    """The first SIGTERM (VS Code's cancel) raises _Terminated; later ones are ignored so cleanup finishes."""
    fired = []

    def handler(signum, frame):
        if not fired:
            fired.append(signum)
            raise _Terminated()
    try:
        old = signal.signal(signal.SIGTERM, handler)
    except ValueError:             # not the main thread: leave signals alone
        old = None
    try:
        yield
    finally:
        if old is not None:
            signal.signal(signal.SIGTERM, old)


@contextlib.contextmanager
def _umask(mask: Optional[int]) -> Iterator[None]:
    old = os.umask(mask) if mask is not None else None
    try:
        yield
    finally:
        if old is not None:
            os.umask(old)


def _make_parents(path: Path, umask: Optional[int]) -> List[Path]:
    """mkdir -p path (0755 under the caller's umask); returns the directories this created, outermost first."""
    missing = []
    while not os.path.lexists(path) and path != path.parent:
        missing.append(path)
        path = path.parent
    created = []
    with _umask(umask):
        for d in reversed(missing):
            try:
                os.mkdir(d, 0o755)
            except FileExistsError:
                if not d.is_dir():
                    raise
                continue           # someone else made it meanwhile: not ours to remove
            created.append(d)
    return created


def _make_target(path: Path, umask: Optional[int]) -> Tuple[int, int]:
    """Create the empty target (atomic: fails when it exists) and return its (st_dev, st_ino)."""
    with _umask(umask):
        try:
            os.mkdir(path, 0o777)  # what git itself would use: the caller's umask decides
        except FileExistsError:
            raise ConfigError(f"{path} already exists") from None
    st = os.lstat(path)
    return st.st_dev, st.st_ino


def _cleanup(path: Path, made: Optional[Tuple[int, int]], parents: List[Path]) -> bool:
    """Remove the target if it is still the directory gitpro made, then the parents gitpro made that are empty."""
    removed = False
    if made is not None:
        try:
            st = os.lstat(path)
        except OSError:
            st = None
        if st is not None and stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == made:
            shutil.rmtree(path, ignore_errors=True)
            removed = not os.path.lexists(path)
    for d in reversed(parents):
        try:
            os.rmdir(d)
        except OSError:
            break
    return removed


def _detach() -> bool:
    """Start git in its own session (so a cancel can stop git *and* its ssh) unless a person may have to answer
    ssh on the terminal: ssh prompts on /dev/tty, which a new session no longer has."""
    if os.name == "nt":
        return False
    if not (os.isatty(0) or os.isatty(2)):
        return True
    try:
        os.close(os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY))
    except OSError:
        return True
    return False


def _stop(proc: subprocess.Popen, group: bool) -> None:
    """SIGTERM git (and, detached, everything it started, ssh included); SIGKILL after 2 s."""
    if not group:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return

    def send(sig: int) -> bool:
        try:
            os.killpg(proc.pid, sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    deadline = time.monotonic() + 2
    send(signal.SIGTERM)
    while True:
        proc.poll()  # reap the leader, but its exit does not mean ssh has stopped
        if not send(0):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            send(signal.SIGKILL)
            break
        time.sleep(min(0.05, remaining))
    proc.wait()


def run(p: ClonePlan, git_args: Sequence[str] = (), *, umask: Optional[int] = None, stdout=None) -> int:
    """git clone [git_args] -- <clone_url> <dir>; returns git's exit code. stdin and stderr are inherited
    (progress, passphrases); umask is the caller's, so the clone is not private to gitpro's 077.
    The target is created here (atomically, empty) and git clones into it. When git fails or is interrupted
    (Ctrl-C, SIGTERM) git is stopped, and the target and any parents this made are removed, the target only
    while it is still the directory made here."""
    argv = ["git", "clone", *git_args, "--", p.clone_url, str(p.dir)]
    env = {k: v for k, v in os.environ.items() if k not in resolve._REDIRECT_VARS}
    kw = {} if umask is None else {"umask": umask}
    group = _detach()
    parents: List[Path] = []
    made: Optional[Tuple[int, int]] = None
    proc: Optional[subprocess.Popen] = None
    with _sigterm_raises():
        try:
            parents = _make_parents(p.dir.parent, umask)
            made = _make_target(p.dir, umask)
            try:
                proc = subprocess.Popen(argv, env=env, stdout=stdout, start_new_session=group, **kw)
            except FileNotFoundError:
                raise ConfigError("git not found") from None
            rc = proc.wait()
        except (KeyboardInterrupt, _Terminated):
            if proc is not None:
                _stop(proc, group)
            what = f"; removed the partial clone {p.dir}" if _cleanup(p.dir, made, parents) else ""
            print(f"✗ interrupted{what}", file=sys.stderr)
            raise KeyboardInterrupt from None
        except BaseException:
            _cleanup(p.dir, made, parents)
            raise
        if rc != 0:
            _cleanup(p.dir, made, parents)
        return rc


# ---------------------------------------------------------------- command

def _again(args) -> str:
    """The command that performs what --dry-run showed."""
    from .cli import _sh
    words = [shlex.quote(w) for w in ("gitpro", "clone", args.url)]
    if args.dir:
        words.append(_sh(str(args.dir)))
    if args.config:
        words += ["-c", _sh(str(args.config))]
    if args.identity:
        words += ["--identity", shlex.quote(args.identity)]
    words += [f for f, on in (("--ssh", args.ssh), ("--no-rewrite", args.no_rewrite)) if on]
    words.append("-y")
    if args.git_args:
        words += ["--", *map(shlex.quote, args.git_args)]
    return " ".join(words)


def _json(p: ClonePlan, **extra) -> dict:
    out = {"url": resolve.redact_url(p.url), "clone_url": resolve.redact_url(p.clone_url), "dir": str(p.dir),
           "identity": p.identity.id if p.identity else None, "auth": p.auth}   # url, clone_url: for display
    out.update(extra)
    if p.warnings:
        out["warnings"] = [f"⚠ {w}" for w in p.warnings]
    return out


def _problem_line(prob: resolve.Problem, p: ClonePlan, top: Path, home: Path) -> str:
    from .cli import _sh
    if prob.code == "no_identity":
        where = f"{p.host}/{p.owner}" if p.host and p.owner else resolve.redact_url(p.clone_url)
        return (f"no identity for {where}: git will refuse to commit here → "
                f"gitpro setup --add {_sh(_tilde(top, home))}")
    fix = resolve._shown_fix(prob)
    return prob.message + (f" → {fix}" if fix else "")


def cmd_clone(args) -> int:
    from . import cli, view
    cfg, env = cli._load_all(args)
    as_json = bool(getattr(args, "json", False))
    p = plan(args.url, cfg, dir=args.dir, identity=args.identity, ssh=args.ssh, no_rewrite=args.no_rewrite,
             home=env.home)
    if not as_json:
        print("\n".join(preview(p, env.home)), flush=True)
        for w in p.warnings:
            view.warn(w)
    if args.dry_run or not (args.yes or args.write):
        if as_json:
            jsonout.emit("clone", _json(p, dry_run=True, problems=[], git_exit=None))
        else:
            view.step(_again(args))
        return 0
    if as_json and not args.yes:
        raise ConfigError("no terminal to confirm on with --json; re-run with --yes (or --dry-run)")
    if not cli._confirm("clone?", args.yes):
        view.aborted()
        return 1
    sys.stdout.flush()
    rc = run(p, args.git_args, umask=cli.caller_umask(), stdout=2 if as_json else None)
    if rc != 0:
        msg = f"git clone failed ({rc})"
        if as_json:
            jsonout.emit("clone", _json(p, dry_run=False, problems=[], git_exit=rc, error=msg), ok=False)
        else:
            view.error(msg)
        return 2
    try:
        rep: Optional[resolve.Report] = resolve.compare(p.dir, cfg, home=env.home)
    except ConfigError as e:
        rep = None
        view.warn(f"cloned, but could not check the identity: {e}")
    if as_json:
        probs = rep.to_json()["problems"] if rep else []
        jsonout.emit("clone", _json(p, dry_run=False, problems=probs, git_exit=0))
        return 0
    shown = _tilde(p.dir, env.home)
    if rep is not None and rep.ok():
        e = rep.expected
        view.ok(f"cloned into {shown}; identity {e.id or '(default)'} <{e.email}>")
    else:
        view.ok(f"cloned into {shown}")
        if rep is not None:
            view.warn(_problem_line(rep.problems[0], p, rep.repo, env.home))
    print(f"↩ undo: rm -r {cli._sh(shown)}")
    return 0


def _split_git_args(sp: argparse.ArgumentParser) -> None:
    """Everything after the first `--` goes verbatim to git clone (args.git_args), never to argparse; the rest
    is parsed intermixed, so options may sit between URL and DIR (`clone URL --ssh DIR`)."""
    def parse_known_args(args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        tail: List[str] = []
        if "--" in args:
            cut = args.index("--")
            args, tail = args[:cut], args[cut + 1:]
        # parse_known_intermixed_args calls parse_known_args itself: the class's, not this override.
        del sp.parse_known_args
        try:
            ns, extras = sp.parse_known_intermixed_args(args, namespace)
        finally:
            sp.parse_known_args = parse_known_args     # type: ignore[method-assign]
        ns.git_args = tail
        return ns, extras
    sp.parse_known_args = parse_known_args     # type: ignore[method-assign]


def register(sub, common, confirm) -> None:
    sp = sub.add_parser(
        "clone", parents=[common, confirm],
        help="git clone with the identity, ssh alias and folder the URL calls for",
        usage="gitpro clone [-h] URL [DIR] [--identity ID] [--ssh] [--no-rewrite] [--dry-run] [-y] [--json] "
              "[-- GIT_CLONE_ARGS...]",
        description="Picks the identity from the URL, rewrites it to the identity's ssh alias when the identity "
                    "has one, clones into clone_dir/<host>/<owner>/<repo> and checks the result. An https URL "
                    "stays https unless you pass --ssh. Set clone_dir per identity or [clone] dir; change the "
                    "folder shape with [clone] layout. Arguments after -- go to git clone.")
    sp.add_argument("url", metavar="URL", help="git URL, host/owner/repo, or owner/repo with --identity")
    sp.add_argument("dir", nargs="?", metavar="DIR", help="target directory (default: from clone_dir and layout)")
    sp.add_argument("--identity", metavar="ID", help="use this identity (and its clone_dir), not the URL's") \
        .gitpro_values = "identities"
    sp.add_argument("--ssh", action="store_true", help="clone an https URL over ssh with the identity's key")
    sp.add_argument("--no-rewrite", action="store_true", help="keep the URL; don't switch to the identity's ssh alias")
    sp.add_argument("--dry-run", action="store_true", help="show the plan and exit")
    sp.add_argument("--json", action="store_true", help="machine-readable output (needs -y or --dry-run)")
    sp.set_defaults(func=cmd_clone, git_args=[])
    _split_git_args(sp)
    capability("clone")
