"""Talking to GitHub / GitLab through their CLIs (gh, glab): accounts, tokens in a child's env only.

Package 01 ships the runner (Account ... no_online_error); package 05 adds the read-only `forge` command
(repos, forks, email, similar) below it. Every forge call is a read: gh repo list/view, gh search repos,
gh api --method GET, glab auth status, glab repo list, glab api --method GET. Nothing is written.
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import jsonout

from .config import ConfigError

if TYPE_CHECKING:
    from .config import Config

GITHUB = "github.com"
GITLAB = "gitlab.com"
_GH_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
_GLAB_TOKEN_VARS = ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN")
TOKEN_TIMEOUT = 30


@dataclass(frozen=True)
class Account:
    host: str
    cli: str    # "gh" | "glab"
    login: str


def available(cli: str) -> bool:
    return shutil.which(cli) is not None


def accounts(cfg: "Config") -> List[Account]:
    """Every (host, cli, login) the config names: [default] first (gh on github.com, glab on gitlab.com),
    then each identity on its own host; duplicates dropped. No network."""
    d = cfg.default
    out = []
    if d.gh_account:
        out.append(Account(GITHUB, "gh", d.gh_account))
    if d.glab_account:
        out.append(Account(GITLAB, "glab", d.glab_account))
    for i in cfg.identities:
        if i.gh_account:
            out.append(Account(i.host, "gh", i.gh_account))
        if i.glab_account:
            out.append(Account(i.host, "glab", i.glab_account))
    return list(dict.fromkeys(out))


def _stripped(names: Tuple[str, ...], base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    return {k: v for k, v in (os.environ if base_env is None else base_env).items() if k not in names}


def gh_env(host: str, login: str, *, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """os.environ without ambient gh tokens, plus GH_HOST and the token of `login` on `host`
    (GH_TOKEN on github.com, GH_ENTERPRISE_TOKEN elsewhere). The token lives only in this dict.
    base_env lets scoped executors preserve their own child environment without global mutation."""
    env = _stripped(_GH_TOKEN_VARS, base_env)
    try:
        r = subprocess.run(["gh", "auth", "token", "-u", login, "-h", host], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=TOKEN_TIMEOUT)
    except FileNotFoundError:
        raise ConfigError("gh not found; install the GitHub CLI (https://cli.github.com)") from None
    except subprocess.TimeoutExpired:
        raise ConfigError("gh auth token timed out (keyring locked?)") from None
    token = r.stdout.strip()
    if r.returncode != 0 or not token:
        if "unknown shorthand flag" in r.stderr or "unknown flag" in r.stderr:
            raise ConfigError("gh 2.40 or newer is needed (for 'gh auth token -u'); upgrade gh")
        raise ConfigError(f"gh has no login for {login} on {host}; run: gh auth login -h {host}")
    env["GH_HOST"] = host
    env["GH_TOKEN" if host.lower() == GITHUB else "GH_ENTERPRISE_TOKEN"] = token
    return env


def _glab_env(host: str) -> Dict[str, str]:
    env = _stripped(_GLAB_TOKEN_VARS)
    env["GITLAB_HOST"] = host
    return env


def run(acc: Account, argv: List[str], *, timeout: int = 30,
        stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess:
    """[acc.cli, *argv] as acc, captured text. 127 when the CLI is missing, 124 on timeout; never raises
    for a failing command (gh_env may raise ConfigError when gh has no such login)."""
    if not available(acc.cli):
        return subprocess.CompletedProcess([acc.cli, *argv], 127, "", f"{acc.cli}: command not found\n")
    env = gh_env(acc.host, acc.login) if acc.cli == "gh" else _glab_env(acc.host)
    return _exec(acc, argv, env, timeout=timeout, stdin=stdin)


def _exec(acc: Account, argv: List[str], env: Dict[str, str], *, timeout: int = 30,
          stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess:
    """run() with a ready env (the token fetched already)."""
    cmd = [acc.cli, *argv]
    kw = {"input": stdin} if isinstance(stdin, (str, bytes)) else {"stdin": stdin}
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{acc.cli}: command not found\n")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{acc.cli} timed out after {timeout}s\n")


def logged_in(acc: Account) -> bool:
    """gh: the token works and `gh api user` names the login; glab: `glab auth status` names it."""
    if acc.cli == "gh":
        try:
            r = run(acc, ["api", "user", "--jq", ".login"])
        except ConfigError:
            return False
        return r.returncode == 0 and r.stdout.strip().lower() == acc.login.lower()
    r = run(acc, ["auth", "status", "-h", acc.host])
    return r.returncode == 0 and acc.login.lower() in (r.stdout + r.stderr).lower()


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def repo_exists(acc: Account, owner_repo: str, repo: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """("ok", None) | ("renamed", "new/name") | ("missing", None) | ("unknown", "<first stderr line>").
    Also callable as repo_exists(acc, owner, repo). Names compare case-insensitively."""
    name = f"{owner_repo}/{repo}" if repo is not None else owner_repo
    if acc.cli == "gh":
        r = run(acc, ["repo", "view", name, "--json", "nameWithOwner"])
        field, gone = "nameWithOwner", ("Could not resolve to a Repository",)
    else:
        r = run(acc, ["repo", "view", name, "--output", "json"])
        field, gone = "path_with_namespace", ("404 Not Found", "404 Project Not Found")
    if r.returncode != 0:
        if any(g in r.stderr for g in gone):
            return "missing", None
        return "unknown", _first_line(r.stderr) or f"{acc.cli} exited {r.returncode}"
    try:
        got = json.loads(r.stdout)[field]
    except (ValueError, KeyError, TypeError):
        return "unknown", f"unexpected {acc.cli} output"
    return ("ok", None) if str(got).lower() == name.lower() else ("renamed", str(got))


def _who(accs: List[Account]) -> str:
    groups: Dict[Tuple[str, str], List[str]] = {}
    for a in accs:
        groups.setdefault((a.host, a.cli), []).append(a.login)
    return " and ".join(f"{h} via {c} (account {', '.join(dict.fromkeys(ls))})" for (h, c), ls in groups.items())


def announce(what: str, accounts: List[Account], sends: str) -> None:
    """The first line of every online command: service, CLI, account, what is sent."""
    print(f"online: asking {_who(accounts)} {what}; sends {sends}", flush=True)


def no_online_error(command: str, what: str, accounts: List[Account], sends: str) -> int:
    """The refusal for a command that needs the network and lacks --online; returns 2."""
    print(f"✗ {command} needs the network: it asks {_who(accounts)} {what}; sends {sends}", file=sys.stderr)
    print("→ re-run with --online, or set [forge] online = true", file=sys.stderr)
    return 2


# ================================================================ 05: `gitpro forge` (read-only)

NEEDS_CLI = "needs gh or glab on PATH: install gh (https://cli.github.com) or glab (https://gitlab.com/gitlab-org/cli)"
GH_FIELDS = "nameWithOwner,isFork,isArchived,sshUrl,pushedAt"
GH_LIMIT = 1000                # gh repo list --limit
GLAB_PER_PAGE = 100
GLAB_MAX_PAGES = 100           # 10 000 projects; a guard against a CLI that ignores --page
SEARCH_PAUSE = 0.5             # seconds between two `gh search repos` calls
SEARCH_LIMIT = 10
WORKERS = 8                    # git processes reading remotes in parallel
_SECRET_RE = re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}"
                        r"|gl(?:pat|dt|rt|oas|ptt|cbt|imt|soat|ft|agent|ffct|wt)-[A-Za-z0-9_-]{8,})")
# a legacy 40-hex token, only where something says it is one (token=..., Authorization: token ..., ...)
_HEX_TOKEN_RE = re.compile(r"(?i)((?:token|bearer|password|passwd|secret|authorization|auth)"
                           r"[\"']?\s*[:=]?\s*[\"']?(?:(?:token|bearer|basic)\s+)?)[0-9a-f]{40}(?![0-9a-f])")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")
_GLAB_AS_RE = re.compile(r"Logged in to (\S+) as (\S+)", re.I)
_RATE_RE = re.compile(r"rate limit|HTTP 429|abuse detection", re.I)    # checked before _SCOPE_RE
_SCOPE_RE = re.compile(r"HTTP 40[34]|scope", re.I)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """text with token-looking words (ghp_, gho_, github_pat_, glpat-, gldt-, ..., a 40-hex token after
    "token"), URL credentials and each exact value in secrets masked."""
    from .resolve import redact_url
    for v in secrets:
        if v and len(v) >= 4:
            text = text.replace(v, "***")
    text = _SECRET_RE.sub("***", text)
    text = _HEX_TOKEN_RE.sub(lambda m: m[1] + "***", text)
    return _URL_RE.sub(lambda m: redact_url(m[0]) or "", text)


def safe(text: object) -> str:
    """text for a terminal: control and bidi characters shown as \\xNN / \\uNNNN, never interpreted."""
    return _CONTROL_RE.sub(lambda m: m[0].encode("unicode_escape").decode("ascii"), str(text))


def _strip_userinfo(url: str) -> str:
    """url for a suggested command: http(s) userinfo dropped, a password dropped elsewhere (ssh://git@ kept)."""
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/]*)@", url)
    if not m:
        return url
    user, sep, _secret = m[2].partition(":")
    if m[1].lower() in ("http", "https"):
        return f"{m[1]}://{url[m.end():]}"
    return f"{m[1]}://{user}@{url[m.end():]}" if sep else url


def _err_line(r: subprocess.CompletedProcess) -> str:
    return redact(_first_line(r.stderr)) or f"exit {r.returncode}"


_FAILED = object()      # Session.json_call: the call failed (already warned); a parsed `null` is None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def _tilde(path: object, home: Path) -> str:
    p = Path(str(path))
    try:
        rest = p.relative_to(home)
    except ValueError:
        return str(p)
    return "~" if str(rest) == "." else f"~/{rest}"


def _sh_path(path: object, home: Path) -> str:
    """A path for a command line shown to the user: quoted, ~ kept for paths under home."""
    shown = _tilde(path, home)
    if shown == "~" or shown.startswith("~/"):
        return "~" + ("/" + shlex.quote(shown[2:]) if shown != "~" else "")
    return shlex.quote(shown)


@dataclass(frozen=True)
class Target:
    """One identity (or [default]) mapped to one forge account."""
    id: str
    email: Optional[str]
    owners: Tuple[str, ...]
    acc: Account


def targets(cfg: "Config", ids: Optional[Iterable[str]] = None) -> Tuple[List[Target], List[str]]:
    """(targets, warnings): [default] first, then each identity, as forge.accounts orders them; ids
    restricts the list ("default" names [default]). An identity without an account is a warning."""
    known = ["default"] + [i.id for i in cfg.identities]
    want = list(dict.fromkeys(ids)) if ids else None
    for x in want or ():
        if x not in known:
            raise ConfigError(f"unknown identity '{x}' (known: {', '.join(known)})")
    out: List[Target] = []
    warnings: List[str] = []
    d = cfg.default
    if want is None or "default" in want:
        if d.gh_account:
            out.append(Target("default", d.email, (), Account(GITHUB, "gh", d.gh_account)))
        if d.glab_account:
            out.append(Target("default", d.email, (), Account(GITLAB, "glab", d.glab_account)))
        if want and not (d.gh_account or d.glab_account):
            warnings.append("[default] has no gh_account/glab_account; skipped")
    for i in cfg.identities:
        if want is not None and i.id not in want:
            continue
        if i.gh_account:
            out.append(Target(i.id, i.email, tuple(i.owners), Account(i.host, "gh", i.gh_account)))
        if i.glab_account:
            out.append(Target(i.id, i.email, tuple(i.owners), Account(i.host, "glab", i.glab_account)))
        if not (i.gh_account or i.glab_account):
            warnings.append(f"identity {i.id} has no gh_account/glab_account; skipped")
    return out, warnings


# ---------------------------------------------------------------- local repos ("cloned")

@dataclass
class Local:
    """The repos under [scan] roots (02's roots and excludes) with their raw remote URLs."""
    roots: List[Path]
    repos: List[Tuple[Path, Dict[str, str]]]
    aliases: Dict[str, str] = field(default_factory=dict)    # ssh alias -> real host

    def key(self, url: Optional[str]) -> Optional[Tuple[str, str]]:
        """(host, owner/repo), lowercased, for a raw remote URL; an ssh alias counts as its real host."""
        if not url:
            return None
        from .resolve import parse_url
        host, owner, name = parse_url(url)
        if not (host and owner and name):
            return None
        host = host.lower()
        return self.aliases.get(host, host), f"{owner}/{name}".lower()

    def by_origin(self) -> Dict[Tuple[str, str], Tuple[Path, Dict[str, str]]]:
        out: Dict[Tuple[str, str], Tuple[Path, Dict[str, str]]] = {}
        for path, rems in self.repos:
            k = self.key(rems.get("origin"))
            if k and k not in out:
                out[k] = (path, rems)
        return out


def _remotes(path: Path) -> Dict[str, str]:
    from .resolve import remotes
    try:
        return remotes(path)
    except Exception:   # an unreadable repo is simply not "cloned"
        return {}


def local_repos(cfg: "Config") -> Local:
    from concurrent.futures import ThreadPoolExecutor

    from . import scan
    roots, exclude, _source = scan.effective_roots([], cfg.scan)
    paths = scan.find(roots, exclude=exclude)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rems = list(pool.map(_remotes, paths))
    aliases = {i.ssh_alias.lower(): i.host.lower() for i in cfg.identities if i.ssh_alias}
    return Local(list(roots), list(zip(paths, rems)), aliases)


def _swap(url: Optional[str], old: str, new: str) -> Optional[str]:
    """url with its trailing owner/repo `old` replaced by `new` (scheme, user, alias kept)."""
    if not url:
        return None
    m = re.search(r"[:/](" + re.escape(old) + r")(\.git)?/?$", url, re.I)
    return url[:m.start(1)] + new + (m[2] or "") if m else None


def _clone_prefix() -> str:
    import importlib.util
    try:
        return "gitpro clone" if importlib.util.find_spec("gitpro.clone") else "git clone"
    except (ImportError, ValueError):
        return "git clone"


# ---------------------------------------------------------------- a session: calls, warnings, record

class Session:
    """One forge command run: the targets, what was sent where, warnings (queued until the online line
    is out, so it stays the first line), the local scan (lazy)."""

    def __init__(self, cfg: "Config", home: Path, ids: Optional[Iterable[str]] = None, *, as_json: bool = False):
        self.cfg, self.home, self.as_json = cfg, home, as_json
        self.targets, queued = targets(cfg, ids)
        self.warnings: List[str] = []
        self.sent: Dict[str, List[str]] = {}
        self.live = False
        self.explicit = False          # --online given (a config-only online never exits 2)
        self._queue: List[str] = []
        self._envs: Dict[Account, object] = {}     # the child env per account (or the ConfigError), once per run
        self._local: Optional[Local] = None
        self._glab: Dict[Account, Optional[str]] = {}
        self.tried: Set[Account] = set()
        self.answered: Set[Account] = set()
        for w in queued:
            self.warn(w)

    # output -------------------------------------------------------
    def secrets(self) -> List[str]:
        return [e.get(k, "") for e in self._envs.values() if isinstance(e, dict)
                for k in ("GH_TOKEN", "GH_ENTERPRISE_TOKEN")]

    def warn(self, msg: str) -> None:
        msg = redact(msg, self.secrets())
        self.warnings.append(msg)
        if self.live:
            print(f"⚠ {safe(msg)}", file=sys.stderr, flush=True)
        else:
            self._queue.append(msg)

    def flush(self) -> None:
        for msg in self._queue:
            print(f"⚠ {safe(msg)}", file=sys.stderr, flush=True)
        self._queue = []

    def announce(self, what: str, accs: List[Account], sends: str) -> None:
        sys.stdout.flush()
        with contextlib.redirect_stdout(sys.stderr if self.as_json else sys.stdout):
            announce(what, accs, sends)

    @property
    def local(self) -> Local:
        if self._local is None:
            self._local = local_repos(self.cfg)
        return self._local

    def accounts(self) -> List[Account]:
        return list(dict.fromkeys(t.acc for t in self.targets))

    def who(self, acc: Account) -> str:
        return ", ".join(dict.fromkeys(t.id for t in self.targets if t.acc == acc))

    # the gate -----------------------------------------------------
    def gate(self, command: str, what: str, sends: str, *, explicit: bool, args) -> Optional[int]:
        """None to go on (self.live says whether online), else the exit code: no --online (2), no
        account (2), no gh/glab with --online (2). [forge] online = true without gh/glab: ⚠, offline."""
        from .args import fail
        accs = self.accounts()
        if not accs:
            self.flush()
            return fail(f"{command}: no identity has a gh_account or glab_account; nothing to ask", args)
        self.explicit = bool(explicit)
        if not (explicit or self.cfg.forge.online):
            self.flush()
            if self.as_json:
                return fail(f"{command} needs the network: it asks {_who(accs)} {what}; sends {sends}; "
                            "re-run with --online, or set [forge] online = true", args)
            return no_online_error(command, what, accs, sends)
        clis = list(dict.fromkeys(a.cli for a in accs))
        missing = [c for c in clis if not available(c)]
        if len(missing) == len(clis):
            if explicit:
                self.flush()
                return fail(f"{command} {NEEDS_CLI}", args)
            self.warn(f"[forge] online = true, but {command} {NEEDS_CLI}; nothing checked")
            self.flush()
            return None
        for c in missing:
            ids = ", ".join(dict.fromkeys(t.id for t in self.targets if t.acc.cli == c))
            self.warn(f"{c} not found; {ids} skipped")
        self.targets = [t for t in self.targets if t.acc.cli not in missing]
        self.live = True
        self.announce(what, self.accounts(), sends)
        self.flush()
        return None

    # calls --------------------------------------------------------
    def env(self, acc: Account) -> Dict[str, str]:
        """The child env of acc, fetched once per run (gh auth token); raises the ConfigError again."""
        if acc not in self._envs:
            try:
                self._envs[acc] = gh_env(acc.host, acc.login) if acc.cli == "gh" else _glab_env(acc.host)
            except ConfigError as e:
                self._envs[acc] = e
        got = self._envs[acc]
        if isinstance(got, ConfigError):
            raise got
        return got  # type: ignore[return-value]

    def run(self, acc: Account, argv: List[str]) -> subprocess.CompletedProcess:
        """forge.run with the env cached for this run."""
        if not available(acc.cli):
            return subprocess.CompletedProcess([acc.cli, *argv], 127, "", f"{acc.cli}: command not found\n")
        return _exec(acc, argv, self.env(acc))

    def call(self, acc: Account, argv: List[str], label: str, who: Optional[str] = None, *,
             check: bool = True) -> Optional[subprocess.CompletedProcess]:
        """run(acc, argv), recorded under self.sent; None (with a ⚠) when it could not run, or failed and
        check is set. Error text is redacted."""
        who = who or self.who(acc)
        self.tried.add(acc)
        try:
            r = self.run(acc, argv)
        except ConfigError as e:
            self.warn(f"{redact(str(e))}; {who} skipped")
            return None
        if r.returncode == 127:
            self.warn(f"{acc.cli} not found; {who} skipped")
            return None
        self.sent.setdefault(acc.host, []).append(label)
        if r.returncode == 124:
            self.warn(f"{acc.cli} timed out; {who} skipped")
            return None
        if r.returncode != 0 and check:
            self.warn(f"{acc.host}: {_err_line(r)}; {who} skipped")
            return None
        return r

    def json_call(self, acc: Account, argv: List[str], label: str, who: Optional[str] = None):
        """The parsed JSON of a successful call (None for `null`), else _FAILED (with a ⚠)."""
        r = self.call(acc, argv, label, who)
        if r is None:
            return _FAILED
        try:
            return json.loads(r.stdout)
        except ValueError:
            self.warn(f"{acc.host}: unexpected {acc.cli} output; {who or self.who(acc)} skipped")
            return _FAILED

    def glab_ok(self, t: Target) -> bool:
        """glab acts as whoever is logged in on the host (one login per host): go on only when that is
        t.acc.login, else ⚠ and skip, so another login's data is never shown as this identity's."""
        acc = t.acc
        if acc not in self._glab:
            self._glab[acc] = self._glab_login(acc)
        active = self._glab[acc]
        if active is not None and active.lower() == acc.login.lower():
            return True
        fix = f"glab auth login --hostname {acc.host}"
        self.tried.add(acc)
        if active:
            self.warn(f"glab is logged in to {acc.host} as {active}, identity {t.id} expects {acc.login}; "
                      f"{t.id} skipped → {fix}")
        else:
            self.warn(f"glab is not logged in to {acc.host} as {acc.login}; {t.id} skipped → {fix}")
        return False

    def _glab_login(self, acc: Account) -> Optional[str]:
        r = self.call(acc, ["auth", "status", "-h", acc.host], "auth status", check=False)
        if r is None:
            return None
        text = f"{r.stdout}\n{r.stderr}"
        logins = [m[2].rstrip(".,") for m in _GLAB_AS_RE.finditer(text) if m[1].rstrip(".,:") == acc.host]
        if logins:
            return logins[0]
        if r.returncode != 0:
            return None
        r = self.call(acc, ["api", "--method", "GET", "user"], "api user", check=False)
        try:
            return str(json.loads(r.stdout)["username"]) if r is not None and r.returncode == 0 else None
        except (ValueError, KeyError, TypeError):
            return None

    def ready(self, t: Target) -> bool:
        return t.acc.cli != "glab" or self.glab_ok(t)

    def failed_all(self) -> bool:
        """The forge was asked and no account answered."""
        return self.live and bool(self.tried) and not self.answered

    def record(self) -> Dict[str, List[str]]:
        return {h: list(dict.fromkeys(v)) for h, v in self.sent.items()}


def glab_pages(acc: Account, argv: List[str]) -> Tuple[List[dict], Optional[subprocess.CompletedProcess]]:
    """Every item of a paged `glab ... --output json` listing (glab has no --limit): --page 1, 2, ...
    until a page is short or adds nothing new (`null` / empty is no items). (items, None), or (items so far, the failing CompletedProcess)."""
    argv = list(argv)
    if "--per-page" not in argv:
        argv += ["--per-page", str(GLAB_PER_PAGE)]
    per_page = int(argv[argv.index("--per-page") + 1])
    items: List[dict] = []
    seen: Set[object] = set()
    for page in range(1, GLAB_MAX_PAGES + 1):
        r = run(acc, [*argv, "--page", str(page)])
        if r.returncode != 0:
            return items, r
        try:
            got = json.loads(r.stdout.strip() or "null")
        except ValueError:
            return items, subprocess.CompletedProcess(r.args, 1, "", f"unexpected {acc.cli} output\n")
        if got is None:
            got = []
        if not isinstance(got, list):
            return items, subprocess.CompletedProcess(r.args, 1, "", f"unexpected {acc.cli} output\n")
        new = 0
        for x in got:
            if not isinstance(x, dict):
                continue
            k = x.get("id") or x.get("path_with_namespace") or json.dumps(x, sort_keys=True, default=str)
            if k not in seen:
                seen.add(k)
                items.append(x)
                new += 1
        if len(got) < per_page or not new:      # a short page, or a CLI that keeps sending the same page
            break
    return items, None


# ---------------------------------------------------------------- repos

def _row_gh(x: dict) -> dict:
    return {"name_with_owner": str(x.get("nameWithOwner") or ""), "ssh_url": x.get("sshUrl"),
            "fork": bool(x.get("isFork")), "archived": bool(x.get("isArchived")), "pushed_at": x.get("pushedAt")}


def _row_glab(x: dict) -> dict:
    parent = x.get("forked_from_project")
    return {"name_with_owner": str(x.get("path_with_namespace") or ""), "ssh_url": x.get("ssh_url_to_repo"),
            "fork": isinstance(parent, dict), "archived": bool(x.get("archived")),
            "pushed_at": x.get("last_activity_at"),
            "parent": parent.get("path_with_namespace") if isinstance(parent, dict) else None}


def _gh_list(s: Session, acc: Account, owner: str, who: str) -> Optional[List[dict]]:
    got = s.json_call(acc, ["repo", "list", owner, "--limit", str(GH_LIMIT), "--json", GH_FIELDS],
                      f"repo list {owner}", who)
    if got is _FAILED:
        return None
    if not isinstance(got, list):
        kind = "null" if got is None else type(got).__name__
        s.warn(f"{acc.host}: unexpected gh output for repo list {owner} ({kind}); {who} skipped")
        return None
    if len(got) >= GH_LIMIT:
        s.warn(f"{acc.host}: {owner}: showing the first {GH_LIMIT} repos; there may be more")
    return [_row_gh(x) for x in got if isinstance(x, dict) and x.get("nameWithOwner")]


def _glab_list(s: Session, acc: Account, who: str) -> Optional[List[dict]]:
    s.tried.add(acc)
    try:
        items, err = glab_pages(acc, ["repo", "list", "--mine", "--per-page", str(GLAB_PER_PAGE), "--output", "json"])
    except ConfigError as e:
        s.warn(f"{redact(str(e))}; {who} skipped")
        return None
    s.sent.setdefault(acc.host, []).append("repo list --mine")
    if err is not None:
        s.warn(f"glab timed out; {who} skipped" if err.returncode == 124 else f"{acc.host}: {_err_line(err)}; {who} skipped")
        return None
    return [_row_glab(x) for x in items if x.get("path_with_namespace")]


def _orgs(s: Session, acc: Account, who: str) -> List[str]:
    r = s.call(acc, ["api", "--method", "GET", "user/orgs", "--paginate", "--jq", ".[].login"], "user/orgs", check=False)
    if r is None:
        return []
    if r.returncode != 0:
        s.warn(f"{acc.host}: {_err_line(r)}; organisations of {acc.login} not listed")
        return []
    return [o.strip() for o in r.stdout.splitlines() if o.strip() and o.strip().lower() != acc.login.lower()]


def account_repos(s: Session, t: Target, *, orgs: bool = False) -> Optional[List[dict]]:
    """Every repo of t's account (and, with orgs, of its organisations), sorted; None when unreadable."""
    acc, who = t.acc, s.who(t.acc)
    if not s.ready(t):
        return None
    rows = _glab_list(s, acc, who) if acc.cli == "glab" else _gh_list(s, acc, acc.login, who)
    if rows is None:
        return None
    s.answered.add(acc)
    if orgs and acc.cli == "gh":
        seen = {r["name_with_owner"].lower() for r in rows}
        for org in _orgs(s, acc, who):
            more = _gh_list(s, acc, org, f"organisation {org}") or []
            for r in more:
                if r["name_with_owner"].lower() not in seen:
                    seen.add(r["name_with_owner"].lower())
                    rows.append(r)
    elif orgs:
        s.warn(f"--orgs: glab lists the projects of every group you belong to already ({who})")
    return sorted(rows, key=lambda r: r["name_with_owner"].lower())


def repos(s: Session, *, orgs: bool = False, not_cloned: bool = False) -> List[dict]:
    """One entry per account: {id, ids, host, cli, account, repos: [... cloned, clone_cmd]}."""
    if not s.live:
        return []
    index = s.local.by_origin()
    prefix = _clone_prefix()
    out = []
    for acc in s.accounts():
        t = next(x for x in s.targets if x.acc == acc)
        rows = account_repos(s, t, orgs=orgs)
        if rows is None:
            continue
        for r in rows:
            hit = index.get((acc.host.lower(), r["name_with_owner"].lower()))
            r["cloned"] = str(hit[0]) if hit else None
            r["clone_cmd"] = f"{prefix} {shlex.quote(r['ssh_url'])}" if not hit and r["ssh_url"] else None
            r.pop("parent", None)
        ids = list(dict.fromkeys(x.id for x in s.targets if x.acc == acc))
        shown = [r for r in rows if r["cloned"] is None] if not_cloned else rows
        out.append({"id": ids[0], "ids": ids, "host": acc.host, "cli": acc.cli, "account": acc.login,
                    "total": len(rows), "cloned_count": sum(r["cloned"] is not None for r in rows),
                    "archived_count": sum(r["archived"] for r in rows), "repos": shown})
    return out


def render_repos(groups: List[dict]) -> str:
    if not groups:
        return ""
    lw = max(len(", ".join(g["ids"])) for g in groups)
    aw = max(len(f"{g['account']}@{g['host']}") for g in groups)
    lines = []
    for g in groups:
        n, c = g["total"], g["cloned_count"]
        head = f"{_plural(n, 'repo')} · {c} cloned"
        if n - c:
            head += f" · {n - c} not cloned"
        if g["archived_count"]:
            head += f" · {g['archived_count']} archived"
        lines.append(f"{', '.join(g['ids']):<{lw}}  {safe(g['account'] + '@' + g['host']):<{aw}}   {head}")
        todo = [r for r in g["repos"] if r["cloned"] is None]
        if not todo:
            continue
        lines.append("  not cloned:")
        nw = max(len(safe(r["name_with_owner"])) for r in todo)
        for r in todo:
            pushed = f"pushed {safe(str(r['pushed_at'])[:10])}" if r["pushed_at"] else "never pushed"
            tail = "archived" if r["archived"] else (f"→ {safe(r['clone_cmd'])}" if r["clone_cmd"] else "")
            lines.append(f"    {safe(r['name_with_owner']):<{nw}}   {pushed:<17}  {tail}".rstrip())
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- forks

def _parent_of(view: object) -> Tuple[bool, Optional[str]]:
    """(known, parent nameWithOwner or None) from `gh repo view --json parent,nameWithOwner`."""
    if not isinstance(view, dict) or "parent" not in view:
        return False, None
    p = view["parent"]
    if p is None:
        return True, None
    if isinstance(p, dict):
        if p.get("nameWithOwner"):
            return True, str(p["nameWithOwner"])
        owner = p.get("owner")
        login = owner.get("login") if isinstance(owner, dict) else None
        if login and p.get("name"):
            return True, f"{login}/{p['name']}"
    return False, None


def _fork_entry(s: Session, acc: Account, nwo: str, parent: Optional[str], index) -> dict:
    """Status ok | missing | moved | local_path (upstream is a path, not compared) | parent_deleted |
    not_cloned. URLs shown are redacted; a suggested URL is built from a URL without its credentials."""
    from .resolve import parse_url, redact_url
    hit = index.get((acc.host.lower(), nwo.lower()))
    path, rems = hit if hit else (None, {})
    up = rems.get("upstream")
    e = {"name_with_owner": nwo, "parent": parent, "local": str(path) if path else None,
         "upstream_url": redact_url(up), "status": "ok", "fix": None, "account": acc.login, "host": acc.host}
    if parent is None:
        e["status"] = "parent_deleted"
        return e
    if path is None:
        e["status"] = "not_cloned"
        return e
    up_host, up_owner, up_name = parse_url(up) if up else (None, None, None)
    clean_up = _strip_userinfo(up) if up and up_host else None
    origin = rems.get("origin")
    clean_origin = _strip_userinfo(origin) if origin and parse_url(origin)[0] else None
    want = (_swap(clean_up, f"{up_owner}/{up_name}", parent) if clean_up and up_owner else None) \
        or _swap(clean_origin, nwo, parent) or f"git@{acc.host}:{parent}.git"
    q = shlex.quote
    if up is None:
        e["status"] = "missing"
        e["fix"] = f"git -C {q(str(path))} remote add upstream {q(want)}"
        return e
    if not up_host:
        e["status"] = "local_path"
        e["fix"] = f"git -C {q(str(path))} remote set-url upstream {q(want)}"
        return e
    if f"{up_owner}/{up_name}".lower() != parent.lower():
        e["status"] = "moved"
        e["fix"] = f"git -C {q(str(path))} remote set-url upstream {q(want)}"
    return e


def forks(s: Session) -> List[dict]:
    """Every fork of each account, compared with the local clone's upstream remote."""
    if not s.live:
        return []
    index = s.local.by_origin()
    out = []
    for acc in s.accounts():
        t = next(x for x in s.targets if x.acc == acc)
        rows = account_repos(s, t)
        if rows is None:
            continue
        for r in (r for r in rows if r["fork"]):
            nwo = r["name_with_owner"]
            if acc.cli == "glab":
                parent = r.get("parent")
            else:
                view = s.json_call(acc, ["repo", "view", nwo, "--json", "parent,nameWithOwner"], f"repo view {nwo}",
                                   f"fork {nwo}")
                if view is _FAILED:
                    continue
                known, parent = _parent_of(view)
                if not known:
                    s.warn(f"{acc.host}: unexpected gh output for {nwo}; skipped")
                    continue
            out.append(_fork_entry(s, acc, nwo, parent, index))
    return out


def render_forks(s: Session, entries: List[dict]) -> str:
    home = s.home
    lines = []
    for acc in s.accounts():
        if acc not in s.answered:
            continue
        mine = [e for e in entries if e["account"] == acc.login and e["host"] == acc.host]
        lines.append(f"forks of {acc.login} ({acc.host} via {acc.cli})")
        if not mine:
            lines.append("  no forks")
            continue
        cols = [(safe(e["name_with_owner"]), f"parent {safe(e['parent'] or '(deleted)')}",
                 f"local {safe(_tilde(e['local'], home))}" if e["local"] else "not cloned") for e in mine]
        w = [max(len(c[i]) for c in cols) for i in range(3)]
        for e, c in zip(mine, cols):
            lead = f"  {c[0]:<{w[0]}}   {c[1]:<{w[1]}}   {c[2]:<{w[2]}}   "
            status = {"ok": "upstream ok", "parent_deleted": "→ upstream is gone; keep or archive",
                      "not_cloned": "", "missing": "no upstream remote"}.get(e["status"])
            if e["status"] == "moved":
                from .resolve import parse_url
                _h, o, n = parse_url(e["upstream_url"])
                status = f"upstream remote points to {safe(o)}/{safe(n)}"
            elif e["status"] == "local_path":
                status = f"upstream remote is a local path ({safe(_tilde(e['upstream_url'], home))}); not compared"
            lines.append((lead + (status or "")).rstrip())
            if e["fix"]:
                fix = e["fix"].replace(f"git -C {shlex.quote(e['local'])} ", f"git -C {_sh_path(e['local'], home)} ", 1)
                lines.append(" " * len(lead) + f"→ {safe(fix)}")
    return "\n".join(lines) + ("\n" if lines else "")


# ---------------------------------------------------------------- email

def _emails_url(acc: Account) -> str:
    if acc.cli == "glab":
        return f"https://{acc.host}/-/profile/emails"
    return f"https://{acc.host}/settings/emails"


def _verified(s: Session, t: Target) -> Tuple[bool, Optional[Set[str]]]:
    """(answered, verified emails lowercased or None when unknown)."""
    acc, who = t.acc, s.who(t.acc)
    if not s.ready(t):
        return False, None
    if acc.cli == "gh":
        r = s.call(acc, ["api", "--method", "GET", "user/emails", "--paginate", "--jq",
                         ".[] | select(.verified) | .email"], "user/emails", who, check=False)
        if r is None:
            return False, None
        if r.returncode != 0:
            if _RATE_RE.search(r.stderr):
                s.warn(f"{acc.host}: rate limited ({_err_line(r)}); {who} skipped")
                return False, None
            if _SCOPE_RE.search(r.stderr):
                s.warn(f"gh token for {acc.login} lacks the user:email scope; run: "
                       f"gh auth refresh -h {acc.host} -s user:email")
                return True, None
            s.warn(f"{acc.host}: {_err_line(r)}; {who} skipped")
            return False, None
        return True, {ln.strip().lower() for ln in r.stdout.splitlines() if ln.strip()}
    user = s.json_call(acc, ["api", "--method", "GET", "user"], "api user", who)
    if not isinstance(user, dict):
        if user is not _FAILED:
            s.warn(f"{acc.host}: unexpected glab output for api user; {who} skipped")
        return False, None
    found = {str(user["email"]).lower()} if user.get("email") else set()
    r = s.call(acc, ["api", "--method", "GET", "user/emails"], "api user/emails", who, check=False)
    if r is not None and r.returncode == 0:
        try:
            more = json.loads(r.stdout)
        except ValueError:
            more = []
        for e in more if isinstance(more, list) else []:
            if isinstance(e, dict) and e.get("email") and e.get("confirmed_at", True):
                found.add(str(e["email"]).lower())
    return True, found


def emails(s: Session) -> List[dict]:
    """Per identity: is its email a verified email of its account? verified true / false / null."""
    if not s.live:
        return []
    cache: Dict[Account, Tuple[bool, Optional[Set[str]]]] = {}
    out = []
    for t in s.targets:
        if not t.email:
            continue
        if t.acc not in cache:
            cache[t.acc] = _verified(s, t)
        answered, got = cache[t.acc]
        if not answered:
            continue
        s.answered.add(t.acc)
        verified = None if got is None else t.email.lower() in got
        fix = (f"add it at {_emails_url(t.acc)} or fix the identity" if verified is False else None)
        out.append({"id": t.id, "email": t.email, "host": t.acc.host, "cli": t.acc.cli, "account": t.acc.login,
                    "verified": verified, "fix": fix})
    return out


def render_emails(entries: List[dict]) -> str:
    if not entries:
        return ""
    rows = []
    for e in entries:
        if e["verified"] is True:
            rows.append((e, f"verified on {e['host']} ({e['account']})", "✓"))
        elif e["verified"] is False:
            rows.append((e, f"not a verified email of {e['host']} account {e['account']}", "✗"))
        else:
            rows.append((e, f"could not check {e['host']} account {e['account']}", "⚠"))
    iw = max(len(e["id"]) for e in entries)
    ew = max(len(e["email"]) for e in entries)
    tw = max(len(r[1]) for r in rows)
    lines = [f"{e['id']:<{iw}}  {e['email']:<{ew}}  {text:<{tw}}  {sym}" + (f"  → {e['fix']}" if e["fix"] else "")
             for e, text, sym in rows]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- similar

def _name_of(path: Path, rems: Dict[str, str]) -> str:
    """The last segment of origin (or the first remote), else the dir name."""
    from .resolve import parse_url
    url = rems.get("origin") or next(iter(rems.values()), None)
    if url is None:
        return path.name
    return parse_url(url)[2] or path.name


def similar_names(s: Session, hosts: List[str], patterns: Iterable[str] = ()) -> Tuple[Dict[str, List[str]], int]:
    """({host: names to send}, number of local names not sent): a repo's name goes to a host only when the
    repo has no remote or every remote is on that host; --name patterns (fnmatch) narrow it further."""
    pats = list(patterns)
    want: Dict[str, Set[str]] = {h: set() for h in hosts}
    every: Set[str] = set()
    for path, rems in s.local.repos:
        name = _name_of(path, rems)
        every.add(name)
        if pats and not any(fnmatch.fnmatchcase(name, p) for p in pats):
            continue
        if not rems:
            for h in hosts:
                want[h].add(name)
            continue
        on = {(k[0] if k else None) for k in map(s.local.key, rems.values())}
        if len(on) == 1:
            h = next(iter(on))
            if h in want:
                want[h].add(name)
    chosen = {h: sorted(v, key=str.lower) for h, v in want.items()}
    sent = set().union(*want.values()) if want else set()
    return chosen, len(every - sent)


def _search_accounts(s: Session) -> Tuple[List[Account], List[str]]:
    """One gh account per host (searches are public; the first identity's login is used)."""
    accs: Dict[str, Account] = {}
    skipped = []
    for t in s.targets:
        if t.acc.cli != "gh":
            skipped.append(t.acc.host)
            continue
        accs.setdefault(t.acc.host.lower(), t.acc)
    return list(accs.values()), list(dict.fromkeys(skipped))


def similar(s: Session, names: Dict[str, List[str]], *, confirm: Callable[[str], bool]) -> Optional[dict]:
    """Search each host for public repos named like the local ones: one `gh search repos` per name,
    SEARCH_PAUSE apart, after confirm(question) (the announcement is out by then). None when declined.
    Stops on a host at the first error (rate limit): the rest is not sent. Ctrl-C stops every host and
    returns what was sent so far with "interrupted": true."""
    total = len(set().union(*names.values())) if names else 0
    if not confirm(f"send these {_plural(total, 'name')}?"):
        return None
    accs = {a.host.lower(): a for a in _search_accounts(s)[0]}
    mine: Dict[str, Set[str]] = {}
    for t in s.targets:
        mine.setdefault(t.acc.host.lower(), set()).update(o.lower() for o in (t.acc.login, *t.owners))
    index = s.local.by_origin()
    sent: Dict[str, List[str]] = {}
    failed: List[str] = []
    matches: List[dict] = []
    interrupted = False
    for host, todo in names.items():
        acc = accs[host]
        done: List[str] = []
        sent[acc.host] = done
        try:
            _search_host(s, acc, host, todo, done, failed, matches, mine, index)
        except KeyboardInterrupt:
            interrupted = True
            s.warn(f"interrupted: {acc.host} answered {_plural(len(done), 'name')}; the rest were not sent")
            break
    flat = sorted(set().union(*sent.values()) if sent else set(), key=str.lower)
    return {"sent": flat, "sent_to": sent, "failed": failed, "matches": matches, "interrupted": interrupted}


def _search_host(s: Session, acc: Account, host: str, todo: List[str], done: List[str], failed: List[str],
                 matches: List[dict], mine: Dict[str, Set[str]], index) -> None:
    for n, name in enumerate(todo):
        if n:
            time.sleep(SEARCH_PAUSE)
        argv = ["search", "repos", "--match", "name", "--limit", str(SEARCH_LIMIT),
                "--json", "nameWithOwner,isFork", "--", name]
        s.tried.add(acc)
        try:
            r = s.run(acc, argv)
        except ConfigError as e:
            left = "nothing sent to" if not done else f"{_plural(len(done), 'name')} sent; the rest not sent to"
            s.warn(f"{redact(str(e))}; {left} {acc.host}")
            return
        if r.returncode != 0:
            failed.append(name)
            if r.returncode == 124:
                s.warn(f"{acc.host}: gh timed out after {_plural(len(done), 'name')}; the rest were not sent")
            elif _RATE_RE.search(r.stderr):
                s.warn(f"{acc.host}: rate limited after {len(done)} names; the rest were not sent")
            else:
                s.warn(f"{acc.host}: {_err_line(r)}; stopped after {_plural(len(done), 'name')}; "
                       "the rest were not sent")
            return
        try:
            got = json.loads(r.stdout.strip() or "[]")
        except ValueError:
            got = []
        done.append(name)
        s.answered.add(acc)
        results = []
        for x in got if isinstance(got, list) else []:
            nwo = str(x.get("nameWithOwner") or "") if isinstance(x, dict) else ""
            if not nwo:
                continue
            hit = index.get((host, nwo.lower()))
            results.append({"name_with_owner": nwo, "fork": bool(x.get("isFork")),
                            "yours": nwo.split("/", 1)[0].lower() in mine.get(host, set()),
                            "cloned": str(hit[0]) if hit else None})
        matches.append({"name": name, "host": acc.host, "results": results})


def render_similar(rep: dict, home: Path) -> str:
    if not rep["matches"]:
        return ""
    w = max(len(safe(m["name"])) for m in rep["matches"])
    lines = []
    for m in rep["matches"]:
        res = m["results"]
        if not res:
            lines.append(f"  {safe(m['name']):<{w}}   0")
            continue
        shown = []
        for r in res:
            tags = (["yours"] if r["yours"] else ["not yours"]) + (["fork"] if r["fork"] else [])
            if r["cloned"]:
                tags.append(f"cloned {safe(_tilde(r['cloned'], home))}")
            shown.append(f"{safe(r['name_with_owner'])} ({', '.join(tags)})")
        word = "match" if len(res) == 1 else "matches"
        lines.append(f"  {safe(m['name']):<{w}}   {len(res)} public {word}: {', '.join(shown)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI glue

def _session(args) -> Session:
    from . import cli
    cfg, env = cli._load_all(args)
    return Session(cfg, env.home, args.identity, as_json=jsonout.wants_json(args))


def _finish(s: Session, args, command: str, key: str, data, text: str, *, rc: int = 0, ok: bool = True) -> int:
    """Print the report (or the JSON object). Exit 2 when --online was given and no account answered; a
    config-only [forge] online = true only warns (the ⚠ lines say why) and exits as usual."""
    if s.failed_all():
        if s.explicit:
            return _no_answer(s, command)
        s.warn(f"{command}: no account could be read; nothing checked")
    if s.as_json:
        jsonout.emit(command, {"ok": ok, "online": s.live, "sent": s.record(), key: data, "warnings": s.warnings})
    else:
        sys.stdout.write(text)
    return rc


def _no_answer(s: Session, command: str, extra: Optional[dict] = None) -> int:
    msg = f"{command}: no account could be read (see the warnings above)"
    if s.as_json:
        jsonout.emit(command, {"ok": False, "error": msg, "online": True, "sent": s.record(), **(extra or {}),
                               "warnings": s.warnings})
    else:
        print(f"✗ {msg}", file=sys.stderr)
    return 2


def cmd_repos(args) -> int:
    s = _session(args)
    what = ("for the repo lists and organisations of these accounts, and the repo list of each organisation"
            if args.orgs else "for the repo lists of these accounts")
    rc = s.gate("forge repos", what, "nothing else", explicit=args.online, args=args)
    if rc is not None:
        return rc
    groups = repos(s, orgs=args.orgs, not_cloned=args.not_cloned)
    return _finish(s, args, "forge repos", "identities", groups, render_repos(groups))


def cmd_forks(args) -> int:
    s = _session(args)
    rc = s.gate("forge forks", "for the repo lists of these accounts and the parent of each fork",
                "only the fork names it got back", explicit=args.online, args=args)
    if rc is not None:
        return rc
    entries = forks(s)
    return _finish(s, args, "forge forks", "forks", entries, render_forks(s, entries))


def cmd_email(args) -> int:
    s = _session(args)
    rc = s.gate("forge email", "for the verified emails of these accounts", "nothing; emails are compared locally",
                explicit=args.online, args=args)
    if rc is not None:
        return rc
    entries = emails(s)
    bad = any(e["verified"] is False for e in entries)
    return _finish(s, args, "forge email", "emails", entries, render_emails(entries), rc=1 if bad else 0, ok=not bad)


def _roots_text(s: Session) -> str:
    return ", ".join(safe(_tilde(r, s.home)) for r in s.local.roots)


def cmd_similar(args) -> int:
    from .args import fail
    command = "forge similar"
    s = _session(args)
    accs, glab_hosts = _search_accounts(s)
    for h in glab_hosts:
        s.warn(f"{h}: forge similar searches with gh only; glab accounts skipped")
    if not accs:
        s.flush()
        return fail(f"{command} needs a gh_account (it searches with gh search repos)", args)
    s.targets = [t for t in s.targets if t.acc.cli == "gh"]
    s.explicit = bool(args.online)
    online = bool(args.online or s.cfg.forge.online)
    if not (online and (args.send_repo_names or s.cfg.forge.similar)):
        names, _n = similar_names(s, [a.host.lower() for a in accs], args.name or ())
        total = len(set().union(*names.values()))
        where = " and ".join(f"{a.host} via gh" for a in accs)
        s.flush()
        msg = (f"{command} sends your local repo names ({_plural(total, 'name')} from {_roots_text(s)}) to {where} "
               "as search queries")
        if s.as_json:
            return fail(f"{msg}; re-run with --online --send-repo-names to allow that", args)
        print(f"✗ {msg}", file=sys.stderr)
        print("→ re-run with --online --send-repo-names to allow that", file=sys.stderr)
        return 2
    if not available("gh"):
        if args.online:
            s.flush()
            return fail(f"{command} {NEEDS_CLI}", args)
        s.warn(f"[forge] online = true, but {command} {NEEDS_CLI}; nothing checked")
        s.flush()
        return _similar_json(s, None, 0) if s.as_json else 0
    if s.as_json and not args.yes:
        s.flush()
        return fail(f"{command} --json needs -y (there is no question under --json)", args)
    if not args.yes and not sys.stdin.isatty():
        s.flush()
        return fail("no terminal to confirm on; re-run with --yes", args)
    names, not_sent = similar_names(s, [a.host.lower() for a in accs], args.name or ())
    names = {h: v for h, v in names.items() if v}
    if not names:
        s.flush()
        if s.as_json:
            return _similar_json(s, {"sent": [], "sent_to": {}, "failed": [], "matches": []}, not_sent)
        print(f"✓ no local repo names to send (scanned {_roots_text(s)})")
        return 0
    s.live = True
    by_host = {a.host.lower(): a for a in accs}
    for host, todo in names.items():
        s.announce(f"for public repos named like {_plural(len(todo), 'local repo')} (gh search repos <name>, "
                   "one search per name)", [by_host[host]],
                   f"these {_plural(len(todo), 'repo name')}: {', '.join(map(safe, todo))}; "
                   "no paths, emails or contents")
    s.flush()

    def ask(question: str) -> bool:
        if args.yes:
            return True
        from rich.markup import escape

        from . import view
        return view.confirm(escape(question))

    rep = similar(s, names, confirm=ask)
    if rep is None:
        print("✗ nothing sent", file=sys.stderr)
        return 1
    if rep.pop("interrupted", False):
        if s.as_json:
            _similar_json(s, {**rep, "error": "interrupted; the rest were not sent"}, not_sent, ok=False)
        else:
            sys.stdout.write(render_similar(rep, s.home))
            sent = ", ".join(map(safe, rep["sent"])) or "none"
            print(f"✗ interrupted; sent so far: {sent}", file=sys.stderr)
        return 130
    if s.failed_all() and s.explicit:
        return _no_answer(s, command, {**rep, "not_sent": not_sent})
    if s.as_json:
        return _similar_json(s, rep, not_sent)
    sys.stdout.write(render_similar(rep, s.home))
    return 0


def _similar_json(s: Session, rep: Optional[dict], not_sent: int, *, ok: bool = True) -> int:
    rep = rep or {"sent": [], "sent_to": {}, "failed": [], "matches": []}
    jsonout.emit("forge similar", {"ok": ok, "online": s.live, **rep, "not_sent": not_sent, "warnings": s.warnings})
    return 0


def _common_flags(p, *, what: str) -> None:
    p.add_argument("--identity", action="append", metavar="ID",
                   help="only this identity (repeatable; 'default' is [default])").gitpro_values = "identities"
    p.add_argument("--online", action="store_true", help=f"allow the network ({what})")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def register(sub, common, confirm) -> None:
    from .args import capability, fail
    p = sub.add_parser("forge", parents=[common], help="read-only view of your gh/glab accounts (online)",
                       description="Read your GitHub/GitLab accounts through gh/glab. Every call is a read; "
                                   "the first line says what is sent. Needs --online or [forge] online = true.")
    p.set_defaults(func=lambda a: fail("forge needs a subcommand: repos, forks, email or similar "
                                       "(see gitpro forge -h)", a), command="forge")
    fs = p.add_subparsers(dest="forge_command", metavar="SUBCOMMAND")
    r = fs.add_parser("repos", parents=[common], help="repos of each account, and which are not cloned here")
    _common_flags(r, what="gh repo list / glab repo list per account")
    r.add_argument("--not-cloned", action="store_true", help="list only the repos with no clone under [scan] roots")
    r.add_argument("--orgs", action="store_true", help="also the repos of each organisation the account is in (gh)")
    r.set_defaults(func=cmd_repos, command="forge repos")
    f = fs.add_parser("forks", parents=[common], help="forks whose upstream moved, vanished or has no local remote")
    _common_flags(f, what="repo list, then gh repo view per fork")
    f.set_defaults(func=cmd_forks, command="forge forks")
    e = fs.add_parser("email", parents=[common], help="is each identity's email verified on its account?")
    _common_flags(e, what="gh api user/emails / glab api user")
    e.set_defaults(func=cmd_email, command="forge email")
    sm = fs.add_parser("similar", parents=[common, confirm],
                       help="search public repos named like your local ones (sends repo names; opt-in)")
    _common_flags(sm, what="gh search repos per name")
    sm.add_argument("--send-repo-names", action="store_true",
                    help="allow sending your local repo names as searches (needed on every run, "
                         "or [forge] similar = true)")
    sm.add_argument("--name", action="append", metavar="PATTERN", help="only local names matching PATTERN "
                    "(fnmatch, repeatable)")
    sm.set_defaults(func=cmd_similar, command="forge similar")
    capability("forge", "forge.similar")
