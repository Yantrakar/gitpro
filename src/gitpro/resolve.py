"""Expected vs effective identity for a repo: the one place that judges "is this repo right?".

expected() picks the identity the same way git's includeIf "hasconfig:remote.*.url:<pattern>" does
(render.url_patterns against the raw remote urls, git's wildmatch, later identity wins).
effective() asks git what it resolves and where each value comes from. compare() lists the problems,
each with the command that fixes it. Read-only; three git calls; no network.

Module level imports only the standard library, config and jsonout (render loads lazily), so
`gitpro.cli` can register `why` without paying for it.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from . import config, jsonout
from .config import Config, ConfigError, Identity, check
from .util import tilde

MIN_GIT = (2, 36)
TIMEOUT = 30
PIN_FILE = "git-identities"           # pin.FILE; in the repo's common git dir
MANAGED_GITCONFIG = ".gitconfig-identities"   # plan.MANAGED_GITCONFIG, relative to home

PROBLEM_CODES = ("ambiguous", "no_identity", "url_form", "override", "pin_stale", "global_override", "not_applied",
                 "key_mismatch", "sign_mismatch", "env_override", "policy")

# Hook point for other packages (14: policy). Each entry is called as
#   check(report: Report) -> Iterable[Problem]
# with the report of the built-in checks (report.repo, .expected, .effective, .problems); its problems
# are appended in list order. Use code "policy" (or a code in PROBLEM_CODES). A check that raises is
# skipped with a "⚠" line on stderr; compare() never crashes because of one. Packages append to this
# list when they are imported; compare() never imports them.
EXTRA_CHECKS: List[Callable[["Report"], Iterable["Problem"]]] = []

ENV_VARS = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
CONFIG_ENV = ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")
# Removed from every child git's environment: a hook asking about another repo must not be redirected.
_REDIRECT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR")
_KEYS = {"user.name": "name", "user.email": "email", "user.signingkey": "signingkey",
         "commit.gpgsign": "gpgsign", "core.sshcommand": "sshcommand"}
_KEYS_RE = r"^(user\.(name|email|signingkey)|commit\.gpgsign|core\.sshcommand)$"


# ---------------------------------------------------------------- data

@dataclass(frozen=True)
class Match:
    identity: Optional[Identity]     # None -> [default]
    remote: Optional[str]            # remote whose url matched ("origin"); None when nothing matched or a url was given
    url: Optional[str]               # the raw url that matched
    pattern: Optional[str]           # the url_patterns() entry that matched
    rule: str                        # "matched origin by pattern ..." / "no identity pattern matches <url>"


@dataclass(frozen=True)
class Expected:
    id: Optional[str]
    name: Optional[str]
    email: Optional[str]
    ssh_key: Optional[str]           # the -i path of the core.sshCommand render writes (expanded), or None
    sign: bool                       # the commit.gpgsign render writes (git extras included)
    file: Optional[str]              # identity.file (expanded) or ~/.gitconfig-identities (expanded)
    match: Match
    signingkey: Optional[str] = None  # the user.signingkey render writes when sign is true (expanded), or None


@dataclass(frozen=True)
class Setting:
    value: Optional[str]
    origin: Optional[str]            # "file:/abs/path", "command line:", "standard input:", ...
    scope: Optional[str]             # repo | pin | worktree | generated | global | system | command | other


UNSET = Setting(None, None, None)


@dataclass(frozen=True)
class Effective:
    name: Setting
    email: Setting
    signingkey: Setting
    gpgsign: Setting
    sshcommand: Setting
    pinned: bool
    remotes: Dict[str, str]          # raw urls, name -> url (only *.url, never pushurl)


@dataclass(frozen=True)
class Problem:
    code: str
    message: str
    fix: Optional[str]                         # one shell command (or a short instruction), or None
    fix_argv: Optional[Tuple[str, ...]] = None  # the same fix as an argv a tool may run, or None


@dataclass(frozen=True)
class Report:
    repo: Path
    expected: Expected
    effective: Effective
    problems: Tuple[Problem, ...]

    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> dict:
        e, m, f = self.expected, self.expected.match, self.effective
        return {
            "ok": self.ok(),
            "repo": str(self.repo),
            "remotes": {n: redact_url(u) for n, u in f.remotes.items()},
            "expected": {"id": e.id, "name": e.name, "email": e.email, "ssh_key": e.ssh_key, "sign": e.sign,
                         "file": e.file, "match": {"remote": m.remote, "url": redact_url(m.url), "pattern": m.pattern,
                                                   "rule": m.rule}},
            "effective": {**{k: _setting_json(getattr(f, k)) for k in _KEYS.values()}, "pinned": f.pinned},
            "problems": [{"code": p.code, "message": p.message, "fix": p.fix,
                          "fix_argv": list(p.fix_argv) if p.fix_argv else None} for p in self.problems],
        }


def _setting_json(s: Setting) -> dict:
    return {"value": s.value, "origin": s.origin, "scope": s.scope}


# ---------------------------------------------------------------- git

def _child_env() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _REDIRECT_VARS}


def _git(repo: Path, *args: str, env: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              env=_child_env() if env is None else dict(env),
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT)
    except FileNotFoundError:
        raise ConfigError("git not found") from None
    except subprocess.TimeoutExpired:
        raise ConfigError(f"git {' '.join(args[:2])} timed out in {repo}") from None


def git_version() -> Tuple[int, int]:
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True,
                             timeout=TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        raise ConfigError("git not found") from None
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m[1]), int(m[2])) if m else (0, 0)


def require_git() -> None:
    ver = git_version()
    check(ver >= MIN_GIT, f"git {ver[0]}.{ver[1]} is too old; includeIf hasconfig needs "
                          f"{MIN_GIT[0]}.{MIN_GIT[1]}+ (Ubuntu: ppa:git-core/ppa, macOS: brew install git)")


@dataclass(frozen=True)
class _Layout:
    git_dir: Path
    common_dir: Path
    top: Optional[Path]              # the work tree; None for a bare repo
    base: Path                       # what git's relative "file:" origins are relative to

    @property
    def root(self) -> Path:
        return self.top or self.git_dir


def _work_tree_of(repo: Path, git_dir: Path, common_dir: Path,
                  env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """The work tree when git ran inside a git dir (.git/ or .git/worktrees/<name>/); None when bare."""
    if _git(repo, "rev-parse", "--is-bare-repository", env=env).stdout.strip() != "false":
        return None
    if git_dir != common_dir:                       # linked worktree: gitdir names its .git file
        try:
            return Path((git_dir / "gitdir").read_text(encoding="utf-8").strip()).parent
        except OSError:
            return None
    wt = _git(repo, "config", "--get", "core.worktree", env=env).stdout.strip()
    return Path(os.path.normpath(os.path.join(str(git_dir), wt))) if wt else git_dir.parent


def _layout(repo: Path, env: Optional[Mapping[str, str]] = None) -> Optional[_Layout]:
    # In a bare repo, or inside a .git dir, --show-toplevel fails (exit 128) after printing the first
    # two lines; use them anyway. git then does not chdir, so relative origins are relative to repo.
    r = _git(repo, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel", env=env)
    lines = r.stdout.splitlines()
    if len(lines) < 2:
        return None
    git_dir, common_dir = Path(lines[0]), Path(lines[1])
    if len(lines) > 2 and lines[2]:
        return _Layout(git_dir, common_dir, Path(lines[2]), Path(lines[2]))
    return _Layout(git_dir, common_dir, _work_tree_of(repo, git_dir, common_dir, env), Path(os.path.abspath(repo)))


def _need_layout(repo: Path, env: Optional[Mapping[str, str]] = None) -> _Layout:
    lay = _layout(repo, env)
    if lay is None:
        raise ConfigError(f"{repo}: not a git repository")
    return lay


def is_repo(path: Path) -> Optional[Path]:
    """The work-tree top level holding path (the git dir for a bare repo), or None."""
    lay = _layout(Path(path))
    return lay.root if lay else None


def _remote_urls(repo: Path, env: Optional[Mapping[str, str]] = None) -> List[Tuple[str, str]]:
    """Every (name, raw url) in config order, several per remote when it has several urls: includeIf
    hasconfig:remote.*.url matches any of them (before insteadOf, never pushurl)."""
    out: List[Tuple[str, str]] = []
    for entry in _git(repo, "config", "-z", "--get-regexp", r"^remote\..*\.url$", env=env).stdout.split("\0"):
        key, nl, value = entry.partition("\n")
        if nl and key.startswith("remote.") and key.endswith(".url"):
            out.append((key[len("remote."):-len(".url")], value))
    return out


def _by_name(urls: List[Tuple[str, str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name, url in urls:
        out.setdefault(name, url)
    return out


def remotes(repo: Path) -> Dict[str, str]:
    """name -> raw url (the first url of each remote), in config order. For display; matching uses
    every url of every remote, as git does."""
    return _by_name(_remote_urls(repo))


# ---------------------------------------------------------------- urls

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(?:([^@/]*)@)?(\[[^\]]*\]|[^/:]*)(?::(\d*))?(/.*)?$")
_SCP_RE = re.compile(r"^(?:([^@/:]+)@)?(\[[^\]]*\]|[^/:]+):(.*)$")


def parse_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(host, owner, repo) for display, clone and discover; never used for matching. owner is everything
    between the host and the last path segment (nested groups); local paths and file:// give
    (None, None, basename)."""
    def split(path: str) -> Tuple[Optional[str], Optional[str]]:
        parts = [p for p in path.split("/") if p]
        if not parts:
            return None, None
        repo = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
        return ("/".join(parts[:-1]) or None), (repo or None)

    m = _SCHEME_RE.match(url)
    if m:
        if m[1].lower() == "file":
            return (None, None, split(m[5] or "")[1])
        owner, repo = split(m[5] or "")
        return (m[3] or None), owner, repo
    m = _SCP_RE.match(url)
    if m and "/" not in url.split(":", 1)[0]:
        owner, repo = split(m[3])
        return m[2], owner, repo
    return (None, None, split(url)[1])


_USERINFO_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/]*)@")
# Query parameters whose value is a secret (case-insensitive). Keep in sync with vscode/src/exec.ts SECRET_KEYS.
SECRET_QUERY_KEYS = ("access_token", "private_token", "oauth_token", "refresh_token", "id_token", "token", "auth",
                     "authorization", "password", "passwd", "pwd", "secret", "client_secret", "api_key", "apikey",
                     "key", "sig", "signature", "x-amz-signature", "x-amz-credential", "x-amz-security-token", "jwt")
_SECRET_QUERY_RE = re.compile(r"([?&;](?:" + "|".join(re.escape(k) for k in SECRET_QUERY_KEYS) + r")=)[^&;#\s]*",
                              re.IGNORECASE)


_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+")


def redact_text(value: str) -> str:
    """Redact credential-bearing URLs embedded in a diagnostic or configuration excerpt."""
    return _URL_IN_TEXT.sub(lambda m: redact_url(m[0]) or m[0], value) if "://" in value else value


def redact_url(url: Optional[str]) -> Optional[str]:
    """The url for display: a password in the userinfo becomes *** (scheme://user:***@host/...), and so
    does a bare token in an http(s) userinfo (https://***@host/...); so does the value of a secret query
    parameter (?access_token=***, SECRET_QUERY_KEYS). ssh://git@host and scp forms are unchanged. Display
    only; matching always uses the real url."""
    if not url:
        return url
    if "?" in url:
        q = url.index("?")
        url = url[:q] + _SECRET_QUERY_RE.sub(r"\1***", url[q:])
    m = _USERINFO_RE.match(url)
    if not m:
        return url
    user, sep, _secret = m[2].partition(":")
    if sep:
        info = f"{user}:***"
    elif m[1].lower() in ("http", "https"):
        info = "***"
    else:
        return url
    return f"{m[1]}://{info}@{url[m.end():]}"


_POSIX_CLASSES = {"alnum": "a-zA-Z0-9", "alpha": "a-zA-Z", "blank": " \\t", "cntrl": "\\x00-\\x1f\\x7f",
                  "digit": "0-9", "graph": "!-~", "lower": "a-z", "print": " -~", "punct": "!-/:-@\\[-`{-~",
                  "space": " \\t\\n\\r\\f\\v", "upper": "A-Z", "xdigit": "0-9A-Fa-f"}


def _class(pattern: str, i: int) -> Tuple[Optional[str], int]:
    """Translate the bracket expression starting at pattern[i] == "["; (None, i) when it is not closed."""
    j = i + 1
    negate = j < len(pattern) and pattern[j] in "!^"
    if negate:
        j += 1
    items: List[str] = []
    first = True
    while j < len(pattern):
        c = pattern[j]
        if c == "]" and not first:
            body = "".join(items) or "\\]"
            # With WM_PATHNAME a bracket expression never matches "/".
            return (f"[^/{body}]" if negate else f"(?!/)[{body}]"), j + 1
        first = False
        if c == "[" and pattern.startswith("[:", j):
            end = pattern.find(":]", j + 2)
            if end != -1 and pattern[j + 2:end] in _POSIX_CLASSES:
                items.append(_POSIX_CLASSES[pattern[j + 2:end]])
                j = end + 2
                continue
        if c == "\\" and j + 1 < len(pattern):
            j += 1
            c = pattern[j]
        if j + 2 < len(pattern) and pattern[j + 1] == "-" and pattern[j + 2] != "]":
            hi = pattern[j + 2]
            if hi == "\\" and j + 3 < len(pattern):
                hi = pattern[j + 3]
                j += 1
            items.append(f"{re.escape(c)}-{re.escape(hi)}")
            j += 3
            continue
        items.append(re.escape(c))
        j += 1
    return None, i


_WM_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _wm_regex(pattern: str) -> "re.Pattern[str]":
    rx = _WM_CACHE.get(pattern)
    if rx is not None:
        return rx
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            whole = j - i >= 2 and (i == 0 or pattern[i - 1] == "/") and (j == n or pattern[j] == "/")
            if not whole:
                out.append("[^/]*")             # "*", or "**" inside a component: does not cross "/"
            elif j == n:
                out.append(".*")                # trailing "/**" or a bare "**": anything, including "/"
            else:
                out.append("(?:.*/)?")          # "**/": zero or more leading components
                j += 1
            i = j
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            cls, end = _class(pattern, i)
            if cls is None:
                out.append(re.escape(c))
                i += 1
            else:
                out.append(cls)
                i = end
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    rx = re.compile("".join(out), re.S)
    _WM_CACHE[pattern] = rx
    return rx


def wildmatch(pattern: str, text: str) -> bool:
    """git's wildmatch with WM_PATHNAME, as includeIf hasconfig:remote.*.url uses it: case-sensitive,
    whole string; "*", "?" and [...] never match "/"; "**" crosses "/" only as a whole path component
    ("a/**", "**/b", "a/**/b", "**"), elsewhere it is "*"."""
    return _wm_regex(pattern).fullmatch(text) is not None


def _identity_match(i: Identity, urls: Sequence[Tuple[Optional[str], str]]
                    ) -> Optional[Tuple[Optional[str], str, str]]:
    from .render import url_patterns
    pats = url_patterns(i)
    for name, url in urls:
        for p in pats:
            if wildmatch(p, url):
                return name, url, p
    return None


def match_url(url: str, cfg: Config) -> Tuple[Optional[Identity], Optional[str]]:
    """(identity, pattern) git would include for a remote with this raw url; the later identity wins.
    (None, None) when nothing matches ([default] applies)."""
    found: Tuple[Optional[Identity], Optional[str]] = (None, None)
    for i in cfg.identities:
        m = _identity_match(i, [(None, url)])
        if m:
            found = (i, m[2])
    return found


# ---------------------------------------------------------------- expected

def _expand(p: str) -> str:
    return os.path.expanduser(p)


def _candidates(urls: List[Tuple[str, str]], remote: Optional[str],
                remote_url: Optional[str] = None) -> List[Tuple[Optional[str], str]]:
    if remote_url is not None:
        name = remote if any(n == remote for n, _ in urls) else None
        return [(name, remote_url)]
    if remote is None:
        return list(urls)
    named = [(n, u) for n, u in urls if n == remote]
    return list(named) if named else [(None, remote)]   # `git push <url>` hands the hook the url, not a name


def _git_value(sections: dict, section: str, key: str) -> Optional[str]:
    """The value of section.key in a render settings dict (git's case-insensitive names; for a
    multi-valued key, the last value, as `git config --get` reports it)."""
    def gval(v: object) -> str:
        return ("true" if v else "false") if isinstance(v, bool) else str(v)
    found: Optional[str] = None
    for sec, body in sections.items():
        if sec.lower() != section or not isinstance(body, dict):
            continue
        for k, v in body.items():
            if k.lower() == key and not isinstance(v, dict):
                v = v[-1] if isinstance(v, list) and v else v
                found = None if isinstance(v, list) else gval(v)
    return found


def _rendered(cfg: Config, i: Optional[Identity]) -> Tuple[Optional[str], bool, Optional[str]]:
    """(ssh key, gpgsign, signingkey) as the generated files give them: the identity file (i.git over
    its base) beats the managed file ([default.git] over its base), exactly as render merges them."""
    from . import render

    def expand(p: str) -> Path:
        return Path(_expand(p))
    layers = [render.default_settings(cfg.default, expand)]
    if i is not None:
        layers.append(render.identity_settings(i, cfg.default, expand))

    def get(section: str, key: str) -> Optional[str]:
        vals = [_git_value(layer, section, key) for layer in layers]
        return next((v for v in reversed(vals) if v is not None), None)
    sign = _bool(get("commit", "gpgsign"))
    signingkey = get("user", "signingkey")
    return (ssh_key_of(get("core", "sshcommand")), sign,
            _expand(signingkey) if sign and signingkey else None)


def _winner(cfg: Config, urls: Sequence[Tuple[Optional[str], str]]
            ) -> Optional[Tuple[Identity, Tuple[Optional[str], str, str]]]:
    found = None
    for i in cfg.identities:          # every matching include applies; the later one wins
        m = _identity_match(i, urls)
        if m:
            found = (i, m)
    return found


def _expected_of(cfg: Config, i: Identity, m: Tuple[Optional[str], str, str], rule: str = "") -> Expected:
    name, url, pattern = m
    key, sign, signingkey = _rendered(cfg, i)
    return Expected(i.id, i.name, i.email, key, sign, _expand(i.file),
                    Match(i, name, url, pattern, rule or f"matched {name or redact_url(url)} by pattern {pattern}"),
                    signingkey)


def _expected_for(cfg: Config, urls: List[Tuple[Optional[str], str]]) -> Expected:
    found = _winner(cfg, urls)
    d = cfg.default
    if found:
        return _expected_of(cfg, *found)
    shown = ", ".join(redact_url(u) or "" for _, u in urls)
    rule = f"no identity pattern matches {shown}" if urls else "no remote url, so no identity pattern can match"
    key, sign, signingkey = _rendered(cfg, None)
    return Expected(None, d.name, d.email, key, sign, _expand("~/" + MANAGED_GITCONFIG),
                    Match(None, None, None, None, rule), signingkey)


def expected(repo: Path, cfg: Config, *, remote: Optional[str] = None,
             remote_url: Optional[str] = None) -> Expected:
    """The identity git should apply in repo. remote= restricts matching to one remote name, or to a
    url (pre-push with `git push <url>`). remote_url= judges that destination instead of the fetch URLs.
    Without either, a repo whose remotes belong to different identities expects the one its pin chose."""
    lay = _need_layout(Path(repo))
    urls = _remote_urls(Path(repo))
    if remote is None and remote_url is None:
        chosen = _settled(cfg, conflicting(cfg, urls), lay)
        if chosen:
            return chosen[1]
    return _expected_for(cfg, _candidates(urls, remote, remote_url))


# ---------------------------------------------------------------- several remotes, several identities

PIN_MARK = "# gitpro pin: identity "    # pin.py writes it when a remote was chosen; resolve reads it back

Row = Tuple[str, Identity, Tuple[Optional[str], str, str]]     # remote, its identity, the match


def by_remote(cfg: Config, urls: Sequence[Tuple[Optional[str], str]]) -> List[Row]:
    """(remote, identity, match) for every remote some identity matches, in remote order: the
    identity git would apply if that remote were the repo's only one."""
    names = list(dict.fromkeys(n for n, _ in urls if n is not None))
    out = []
    for name in names:
        found = _winner(cfg, [(n, u) for n, u in urls if n == name])
        if found:
            out.append((name, found[0], found[1]))
    return out


def conflicting(cfg: Config, urls: Sequence[Tuple[Optional[str], str]]) -> List[Row]:
    """by_remote() when its remotes belong to two or more identities, else []. git then applies all of
    their includes and the later one wins, whichever remote the work goes to."""
    rows = by_remote(cfg, urls)
    return rows if len({i.id for _, i, _ in rows}) > 1 else []


def push_remote(repo: Path, names: Sequence[str], env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The remote a bare `git push` uses from the current branch: branch.<b>.pushRemote,
    remote.pushDefault, branch.<b>.remote, then origin. None when that is not one of names."""
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", env=env).stdout.strip()
    keys = ([f"branch.{branch}.pushRemote"] if branch else []) + ["remote.pushDefault"] \
        + ([f"branch.{branch}.remote"] if branch else [])
    for key in keys:
        value = _git(repo, "config", "--get", key, env=env).stdout.strip()
        if value:
            return value if value in names else None
    return "origin" if "origin" in names else None


def pinned_identity(lay: _Layout) -> Optional[str]:
    """The identity id `gitpro pin --remote` wrote into the repo's pin file, or None."""
    try:
        with open(lay.common_dir / PIN_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(PIN_MARK):
                    return line[len(PIN_MARK):].strip() or None
    except OSError:
        pass
    return None


def _settled(cfg: Config, rows: List[Row], lay: _Layout) -> Optional[Tuple[str, Expected]]:
    """(remote, expected) when the repo's pin settles conflicting rows: the identity its marker names.
    A pin without the marker (0.3) does not: 0.3 pinned whatever git applied, which is the guess the
    ambiguous check exists to refuse (owner, 2026-10-16: "force re-pin")."""
    chosen = pinned_identity(lay) if rows else None
    if not chosen:
        return None
    hits = [r for r in rows if r[1].id == chosen]
    if not hits:
        return None
    name, i, m = hits[0]
    return name, _expected_of(cfg, i, m, f"pinned to {i.id}, the identity of {name}")


def _ambiguous(rows: List[Row], applied: Expected, top: Path, push: Optional[str],
               old_pin: bool = False) -> Problem:
    shown = ", ".join(f"{name} → {i.id}" for name, i, _ in rows)
    msg = (f"the remotes belong to different identities ({shown}); git applies {applied.id} to all of them "
           "because its include comes later")
    if old_pin:
        msg += "; this repo's pin is from gitpro 0.3 and does not say which remote, so pin it again"
    if push is not None:
        return _p("ambiguous", msg, ["gitpro", "pin", "-y", "--remote", push, str(top)])
    names = "|".join(dict.fromkeys(name for name, _, _ in rows))
    return _p("ambiguous", msg, None, f"pin the remote this repo's work goes to: gitpro pin --remote "
                                      f"<{names}> {shlex.quote(str(top))}")


# ---------------------------------------------------------------- effective

def _scope(git_scope: str, origin: str, lay: _Layout, generated: Dict[str, bool]) -> str:
    path = origin[len("file:"):] if origin.startswith("file:") else None
    if git_scope == "local":
        if path and os.path.realpath(path) == os.path.realpath(lay.common_dir / PIN_FILE):
            return "pin"
        return "repo"
    if git_scope == "global":
        if path is not None:
            if path not in generated:
                from .render import is_generated
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        head = f.readline() + f.readline()
                    generated[path] = is_generated(head)
                except OSError:
                    generated[path] = False
            if generated[path]:
                return "generated"
        return "global"
    if git_scope in ("worktree", "system", "command"):
        return git_scope
    return "other"


def _absolute(origin: str, lay: _Layout) -> str:
    # git prints local origins relative to the work-tree top level (bare: the git dir); make them absolute.
    if origin.startswith("file:") and not origin[5:].startswith("/"):
        return "file:" + os.path.normpath(os.path.join(str(lay.base), origin[5:]))
    return origin


def _effective(repo: Path, env: Optional[Mapping[str, str]] = None) -> Tuple[Effective, _Layout, List[Tuple[str, str]]]:
    lay = _need_layout(repo, env)
    r = _git(repo, "config", "-z", "--show-scope", "--show-origin", "--get-regexp", _KEYS_RE, env=env)
    if r.returncode not in (0, 1):
        raise ConfigError(r.stderr.strip() or f"git config failed in {repo} ({r.returncode})")
    parts = r.stdout.split("\0")
    found: Dict[str, Setting] = {}
    pinned = False
    generated: Dict[str, bool] = {}
    for k in range(0, len(parts) - 2, 3):
        git_scope, origin, kv = parts[k], parts[k + 1], parts[k + 2]
        key, _, value = kv.partition("\n")
        if key.lower() not in _KEYS:
            continue
        origin = _absolute(origin, lay)
        scope = _scope(git_scope, origin, lay, generated)
        pinned = pinned or scope == "pin"
        found[_KEYS[key.lower()]] = Setting(value, origin, scope)   # git prints every definition; the last wins
    urls = _remote_urls(repo, env)
    eff = Effective(found.get("name", UNSET), found.get("email", UNSET), found.get("signingkey", UNSET),
                    found.get("gpgsign", UNSET), found.get("sshcommand", UNSET), pinned, _by_name(urls))
    return eff, lay, urls


def effective(repo: Path) -> Effective:
    """What git resolves in repo for user.name/email/signingkey, commit.gpgsign, core.sshcommand, and
    where each value comes from (the last definition git prints wins)."""
    return _effective(Path(repo))[0]


# ---------------------------------------------------------------- compare

def _bool(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("true", "yes", "on", "1")


def ssh_key_of(command: Optional[str]) -> Optional[str]:
    """The -i path of an ssh command line, ~ expanded; None when there is none."""
    if not command:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for n, w in enumerate(words):
        if w == "-i" and n + 1 < len(words):
            return _expand(words[n + 1])
        if w.startswith("-i") and len(w) > 2:
            return _expand(w[2:])
    return None


def _same_path(a: Optional[str], b: Optional[str]) -> bool:
    return a is not None and b is not None and os.path.normpath(_expand(a)) == os.path.normpath(_expand(b))


def _who(e: Expected) -> str:
    return f"the {e.id} identity" if e.id else "[default]"


def _q(argv: Sequence[str]) -> str:
    return shlex.join(list(argv))


def _p(code: str, message: str, argv: Optional[Sequence[str]], fix: Optional[str] = None) -> Problem:
    return Problem(code, message, fix if fix is not None else (_q(argv) if argv else None),
                   tuple(argv) if argv else None)


_DEFAULT_PORT = {"ssh": 22, "https": 443, "http": 80}
_CODECOMMIT = re.compile(r"^git-codecommit\.[a-z0-9-]+\.amazonaws\.com$", re.I)
_Fix = Tuple[Optional[str], str, Identity, str, Optional[Tuple[str, str]]]


def is_codecommit(host: str) -> bool:
    return bool(_CODECOMMIT.match(host))


def _repairs(url: str, users: List[str]) -> List[Tuple[str, str, Optional[Tuple[str, int]]]]:
    tries: List[Tuple[str, str, Optional[Tuple[str, int]]]] = []
    m = re.match(r"^(https?://)[^@/]+@(.*)$", url)
    if m:
        tries.append((m[1] + m[2], "no user in the URL", None))
    m = re.match(r"^ssh://([^@/]+)(/.*)$", url)
    if m:
        tries += [(f"ssh://{u}@{m[1]}{m[2]}", f"user {u}@ in the URL", None) for u in users]
    elif "://" not in url:
        m = _SCP_RE.match(url)
        if m and m[1] is None and "/" not in url.split(":", 1)[0]:
            tries += [(f"{u}@{url}", f"user {u}@ in the URL", None) for u in users]
    m = re.match(r"^((ssh|https?)://[^/]*?):(\d+)(/.*)$", url)
    if m:
        tries.append((m[1] + m[4], "no port in the URL", (m[2], int(m[3]))))
    return tries


def _url_fixes(url: str, cfg: Config, label: str = "the remote") -> List[_Fix]:
    """[(fixed url, reason, identity, pattern, note)] for the repairs, in order: git+ssh:// or ssh+git://
    written as ssh://, then (on that url) userinfo removed, missing user added, port removed, then
    letter case changed to an identity's. First repair that makes some identity match wins. When the
    fixed url would point at the wrong place (a non-default port removed, user git on CodeCommit),
    fixed is None and note is (message tail, fix text) instead."""
    users = list(dict.fromkeys(i.ssh_user for i in cfg.identities)) or ["git"]
    tries: List[Tuple[str, str, Optional[Tuple[str, int]]]] = []
    m = re.match(r"^(git\+ssh|ssh\+git)://(.*)$", url)
    if m:
        pre = f"ssh:// instead of {m[1]}://"
        tries.append((f"ssh://{m[2]}", pre, None))
        tries += [(f, f"{pre}, {r}", p) for f, r, p in _repairs(f"ssh://{m[2]}", users)]
    tries += _repairs(url, users)
    for fixed, reason, port in tries:
        i, pat = match_url(fixed, cfg)
        if i is None:
            continue
        if port and port[1] != _DEFAULT_PORT[port[0]]:
            if port[0] == "ssh":
                tail = f"it looks like {i.id}, whose patterns have no port {port[1]}"
                fix = f"set ssh_port = {port[1]} on identity {i.id} in the gitpro config, then run: gitpro apply"
                if i.ssh_port:
                    fix += f" (it is {i.ssh_port} now; an identity has one ssh_port)"
            else:
                tail = f"it looks like {i.id}, but gitpro's https patterns never carry a port"
                fix = f"switch {label} to an ssh URL for {i.id}"
            return [(None, reason, i, pat, (tail, fix))]
        if reason.endswith("user git@ in the URL") and is_codecommit(i.host):
            return [(None, reason, i, pat, (
                f"it looks like {i.id}, but CodeCommit's SSH user is the SSH key ID, not git",
                f'set ssh_user = "<your SSH key ID>" on identity {i.id} in the gitpro config, then run: gitpro apply'))]
        return [(fixed, reason, i, pat, None)]
    from .render import url_patterns
    for i in cfg.identities:
        for pat in url_patterns(i):
            prefix = pat.split("*", 1)[0]
            if url.lower().startswith(prefix.lower()) and not url.startswith(prefix):
                fixed = prefix + url[len(prefix):]
                j, jpat = match_url(fixed, cfg)
                if j is not None:
                    return [(fixed, "letter case differs; git matches case-sensitively", j, jpat, None)]
    return []


def _env_problem(exp: Expected, eff: Effective, env: Mapping[str, str]) -> Optional[Problem]:
    want = {"NAME": exp.name, "EMAIL": exp.email}
    names = [v for v in ENV_VARS if env.get(v) and env.get(v) != want[v.rsplit("_", 1)[1]]]
    cmd = [k for k, s in (("user.name", eff.name), ("user.email", eff.email))
           if s.scope == "command" and s.value != want[k.split(".")[1].upper()]]
    if not names and not cmd:
        return None
    parts = []
    if names:
        parts.append(" and ".join(f"{v}={env[v]}" for v in names) + " in the environment")
    if cmd:
        parts.append(" and ".join(cmd) + " from the command line (git -c or GIT_CONFIG_*)")
        names += [v for v in CONFIG_ENV if env.get(v)]
    msg = f"{'; '.join(parts)} override{'s' if len(names) + len(cmd) == 1 else ''} {_who(exp)}"
    return Problem("env_override", msg, ("unset " + " ".join(names)) if names else None, None)


def _home_has_block(home: Path) -> bool:
    from .render import has_block
    try:
        return has_block((home / ".gitconfig").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False


def compare(repo: Path, cfg: Config, *, remote: Optional[str] = None,
            remote_url: Optional[str] = None, env: Optional[Mapping[str, str]] = None,
            home: Optional[Path] = None, git_env: Optional[Mapping[str, str]] = None,
            extra_checks: bool = True) -> Report:
    """Expected vs effective, with every problem that applies (order: PROBLEM_CODES, then EXTRA_CHECKS).
    remote= is a remote name or a url; remote_url= overrides its fetch URLs with the push destination.
    env= is the environment whose GIT_AUTHOR_*/GIT_COMMITTER_* are
    judged (default os.environ; a git hook passes one without GIT_AUTHOR_*, which git always exports
    there). home= is where ~/.gitconfig is looked for (default: the real home; `--home` passes its own).
    By default child Git strips repository-redirection variables. git_env= supplies a caller-owned,
    already scoped child environment without modifying os.environ. extra_checks=False lets strict
    operation evaluators run policy checks explicitly rather than inherit fail-open hook behavior."""
    repo = Path(repo)
    eff, lay, urls = _effective(repo, git_env)
    top = lay.root
    candidates = _candidates(urls, remote, remote_url)
    exp = _expected_for(cfg, candidates)
    env = os.environ if env is None else env
    out: List[Problem] = []
    first_url = candidates[0][1] if candidates else None
    repin: Optional[List[str]] = None       # pin's command, when the remotes belong to different identities

    if remote is None and remote_url is None:   # pre-push judges its destination instead
        rows = conflicting(cfg, urls)
        settled = _settled(cfg, rows, lay)
        if settled:
            exp = settled[1]
            repin = ["gitpro", "pin", "-y", "--remote", settled[0], str(top)]
        elif rows:
            out.append(_ambiguous(rows, exp, top, push_remote(top, [n for n, _, _ in rows], git_env),
                                  old_pin=eff.pinned and pinned_identity(lay) is None))
    who = _who(exp)

    if exp.email is None:
        out.append(_p("no_identity", f"no identity for {redact_url(first_url) or top}",
                      ["gitpro", "scan", "--suggest", "--repo", str(top)],
                      f"add an identity: gitpro scan --suggest --repo {shlex.quote(str(top))}"))

    if exp.match.identity is None:
        for name, url in candidates:
            fixes = _url_fixes(url, cfg, name or "the remote")
            if fixes:
                fixed, reason, i, pat, note = fixes[0]
                if note:
                    out.append(_p("url_form", f"{name or 'the remote'} is {redact_url(url)}; {note[0]}", None, note[1]))
                    break
                argv = ["git", "-C", str(top), "remote", "set-url", name, fixed] \
                    if name and remote_url is None else None
                need = pat.replace("**", "…")
                out.append(_p("url_form", f"{name or 'the remote'} is {redact_url(url)}; the {i.id} patterns need {need} "
                                          f"({reason})", argv))
                break

    for key, s, want in (("name", eff.name, exp.name), ("email", eff.email, exp.email)):
        if s.scope in ("repo", "worktree") and want is not None and s.value != want:
            where = "the repo's .git/config" if s.scope == "repo" else "this worktree's config.worktree"
            argv = ["git", "-C", str(top), "config"] + (["--worktree"] if s.scope == "worktree" else []) \
                + ["--unset", f"user.{key}"]
            out.append(_p("override", f"user.{key} is set in {where} and overrides {who}", argv))

    stale = [(k, s) for k, s, want in (("name", eff.name, exp.name), ("email", eff.email, exp.email))
             if s.scope == "pin" and want is not None and s.value != want]
    if stale:
        what = ", ".join(f"user.{k} {s.value}" for k, s in stale)
        out.append(_p("pin_stale", f"the repo's pin ({PIN_FILE}) still says {what}; {who} is "
                                   f"{exp.name} <{exp.email}>", repin or ["gitpro", "pin", "-y", str(top)]))

    beats = [(k, s) for k, s, want in (("name", eff.name, exp.name), ("email", eff.email, exp.email))
             if s.scope in ("global", "system", "other") and want is not None and s.value != want]
    if beats:
        k, s = beats[0]
        out.append(_p("global_override", f"user.{k} comes from {_shown_origin(s, top)} ({s.scope}) and beats "
                                         f"{who}; the generated include must come last", ["gitpro", "apply"]))

    home = Path.home() if home is None else Path(home)
    old = [k for k, s, want in (("name", eff.name, exp.name), ("email", eff.email, exp.email))
           if s.scope == "generated" and want is not None and s.value != want]
    if exp.file and not os.path.exists(exp.file):
        out.append(_p("not_applied", f"{_tilde(exp.file)} does not exist; the config has not been applied",
                      ["gitpro", "apply"]))
    elif not _home_has_block(home):
        out.append(_p("not_applied", "~/.gitconfig has no gitpro block; the config has not been applied",
                      ["gitpro", "apply"]))
    elif old:
        out.append(_p("not_applied", f"the generated files give user.{old[0]} "
                                     f"{getattr(eff, old[0]).value}; they are older than the config",
                      ["gitpro", "apply"]))

    def fix_for(s: Setting, key: str) -> List[str]:
        if s.scope in ("repo", "worktree"):
            return ["git", "-C", str(top), "config"] + (["--worktree"] if s.scope == "worktree" else []) \
                + ["--unset", key]
        if s.scope == "global" and (s.origin or "").startswith("file:"):
            return ["git", "config", "--file", s.origin[5:], "--unset", key]
        if repin and s.scope in ("generated", "pin", None):
            return repin                    # the other identity's include decides it; a pin by remote overrides it
        return ["gitpro", "apply"]

    def own(s: Setting) -> str:
        # a value from the user's own config, outside gitpro's generated files
        if s.scope == "global":
            return f" (from your own {_shown_origin(s, top)}, outside gitpro's block)"
        if s.scope in ("system", "other"):
            return f" (from {_shown_origin(s, top)}, {s.scope} config outside gitpro)"
        return ""

    if exp.ssh_key:
        got = ssh_key_of(eff.sshcommand.value)
        if not _same_path(got, exp.ssh_key):
            msg = (f"core.sshcommand uses {_tilde(got)}{own(eff.sshcommand)}; {who} uses {_tilde(exp.ssh_key)}"
                   if got else f"core.sshcommand is not set; {who} uses {_tilde(exp.ssh_key)}")
            out.append(_p("key_mismatch", msg, fix_for(eff.sshcommand, "core.sshCommand")))

    sign_now = _bool(eff.gpgsign.value)
    if sign_now != exp.sign:
        out.append(_p("sign_mismatch", f"commit.gpgsign is {'true' if sign_now else 'false'}{own(eff.gpgsign)}; {who} "
                                       f"{'signs' if exp.sign else 'does not sign'} commits",
                      fix_for(eff.gpgsign, "commit.gpgsign")))
    elif exp.sign and exp.signingkey and not _same_path(eff.signingkey.value, exp.signingkey):
        out.append(_p("sign_mismatch", f"user.signingkey is {_tilde(eff.signingkey.value) or '(unset)'}"
                                       f"{own(eff.signingkey)}; {who} "
                                       f"signs with {_tilde(exp.signingkey)}",
                      fix_for(eff.signingkey, "user.signingkey")))

    envp = _env_problem(exp, eff, env)
    if envp:
        out.append(envp)

    base = Report(top, exp, eff, tuple(out))
    for extra in list(EXTRA_CHECKS) if extra_checks else ():
        try:
            out.extend(extra(base) or ())
        except Exception as e:  # noqa: BLE001 - another package's check must never break compare
            name = getattr(extra, "__qualname__", None) or repr(extra)
            print(f"⚠ skipped check {getattr(extra, '__module__', '?')}.{name}: {e}", file=sys.stderr)
    return Report(top, exp, eff, tuple(out))


def own_emails(cfg: Config) -> Set[str]:
    """Every email in [default] and the identities."""
    return {e for e in [cfg.default.email, *(i.email for i in cfg.identities)] if e}


def identity_conflicts(expected_email: Optional[str], own: Iterable[str], author: Optional[str],
                       committer: Optional[str]) -> Tuple[bool, bool]:
    """Shared committer/author rule; a colleague's authorship is not an identity override."""
    if expected_email is None:
        return False, False
    want = expected_email.casefold()
    return (committer is not None and committer.casefold() != want,
            author is not None and author.casefold() != want and
            author.casefold() in {email.casefold() for email in own})


# ---------------------------------------------------------------- display

def _tilde(path: Optional[str]) -> str:
    return tilde(path) if path else ""


def _shown_origin(s: Setting, top: Path) -> str:
    o = s.origin or ""
    if not o.startswith("file:"):
        return o.rstrip(":") or "?"
    path = o[5:]
    if path.startswith(str(top) + "/"):
        return path[len(str(top)) + 1:]
    return _tilde(path)


def _shown_fix(p: Problem) -> str:
    if not p.fix_argv:
        return p.fix or ""
    home = str(Path.home())

    def word(a: str) -> str:
        if a.startswith(home + "/"):
            return "~/" + shlex.quote(a[len(home) + 1:])
        return shlex.quote(a)
    # keep a leading explanation ("add an identity: ") that the argv itself does not carry
    cut = (p.fix or "").find(p.fix_argv[0])
    prefix = p.fix[:cut] if cut > 0 else ""
    return prefix + " ".join(word(a) for a in p.fix_argv)


def render_text(rep: Report, cfg: Config) -> str:
    e, f, top = rep.expected, rep.effective, rep.repo
    lines = [_tilde(str(top))]
    if cfg.path:
        lines.append(f"  config    {_tilde(str(cfg.path))}" + (f" ({cfg.how})" if cfg.how else ""))
    rems = [f"{n} {redact_url(u)}" for n, u in f.remotes.items()] or ["(none)"]
    lines.append(f"  remotes   {rems[0]}")
    lines += [f"            {r}" for r in rems[1:]]
    label = e.id or "(default)"
    if e.email:
        lines.append(f"  expected  {label}  {e.name or '(no name)'} <{e.email}>")
    else:
        lines.append(f"  expected  {label}  no name/email in [default]; git will refuse to commit")
    lines.append(f"            {e.match.rule}")
    if f.email.value or f.name.value:
        src = f.email if f.email.value else f.name
        where = _shown_origin(src, top) + (" (pin)" if src.scope == "pin" else "")
        lines.append(f"  effective {f.name.value or '(no name)'} <{f.email.value or 'no email'}>   [{where}]"
                     + ("  · pinned" if f.pinned else ""))
    else:
        lines.append("  effective (unset)")
    key = ssh_key_of(f.sshcommand.value)
    if key:
        lines.append(f"  ssh key   {_tilde(key)}   via core.sshcommand")
    elif e.ssh_key:
        lines.append("  ssh key   (none set)")
    if f.email.value or f.name.value:
        on = _bool(f.gpgsign.value)
        lines.append("  signing   " + (f"on   {_tilde(_expand(f.signingkey.value or ''))}".rstrip() if on else "off"))
    if rep.ok():
        lines.append("✓ identity matches")
    for p in rep.problems:
        lines.append(f"✗ {p.message}")
        if p.fix or p.fix_argv:
            extra = f"        (or: gitpro scan --fix --repo {_tilde(str(top))})" if p.code == "override" else ""
            lines.append(f"→ {_shown_fix(p)}{extra}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- `gitpro why`

def register(sub, common, confirm) -> None:
    from .args import capability
    sp = sub.add_parser("why", parents=[common],
                        help="explain which identity a repo uses, where each value comes from, and how to fix it")
    sp.add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    sp.add_argument("--remote", metavar="NAME", help="judge the identity of this remote only")
    sp.add_argument("--committer", metavar="EMAIL",
                    help="with --json: would the guard let this committer commit (default: the email git uses)")
    sp.add_argument("--author", metavar="EMAIL", help="with --json: the commit's author (default: the committer)")
    sp.add_argument("--message", metavar="TEXT", help="with --json: check this commit message's trailers too")
    sp.add_argument("--json", action="store_true",
                    help="machine-readable output, with the repo's git settings and the guard's judgement of a commit")
    sp.set_defaults(func=cmd_why)
    capability("why", "why.config", "why.commit")


def cmd_why(args) -> int:
    require_git()
    from . import cli, commitcheck
    home = Path(args.home).expanduser().resolve()
    as_json = getattr(args, "json", False)
    try:
        path, how = config.find(args.config, home)
        cfg = config.load_all(path, how)
    except config.ConfigError as e:
        if not as_json or args.config:
            raise
        # No usable config: still an error, but say what git does here (the extension's status bar needs it).
        try:
            data = cli.which_data(Path(args.dir))
        except config.ConfigError:
            data = {}                         # not a repo: the config error is the one to report
        jsonout.emit("why", {"error": str(e), "config": None, **data}, ok=False)
        return 2
    rep = compare(Path(args.dir), cfg, remote=args.remote, home=home)
    if as_json:
        data = rep.to_json()
        data.pop("ok")
        commit = commitcheck.judge(rep, cfg, committer_email=args.committer, author_email=args.author,
                                   message=args.message)
        jsonout.emit("why", {"repo": data.pop("repo"), "config": {"path": str(cfg.path), "how": cfg.how}, **data,
                             "settings": cli.which_data(rep.repo)["settings"], "commit": commit},
                     ok=rep.ok())
    else:
        print(render_text(rep, cfg), end="")
    return 0 if rep.ok() else 1
