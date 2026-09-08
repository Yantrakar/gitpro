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


def _stripped(names: Tuple[str, ...]) -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in names}


def gh_env(host: str, login: str) -> Dict[str, str]:
    """os.environ without ambient gh tokens, plus GH_HOST and the token of `login` on `host`
    (GH_TOKEN on github.com, GH_ENTERPRISE_TOKEN elsewhere). The token lives only in this dict."""
    env = _stripped(_GH_TOKEN_VARS)
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


