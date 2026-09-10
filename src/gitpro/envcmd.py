"""`gitpro env`: shell lines that point gh / glab / ssh / git at the identity of the current repo.

The lines FETCH the token (`gh auth token -u ...`); gitpro never runs that command (except for the
explicit --check probe, which discards the output) and never sees or prints a token.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import config, forge, jsonout, resolve
from .args import capability, fail, guarded
from .config import Config, ConfigError

SHELLS = ("sh", "fish", "pwsh")
KEYS = ("gh", "glab", "ssh", "author")
DEFAULT_ONLY = ("gh", "glab")
SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
PROBE_TIMEOUT = 10
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
SSH_NOTE = "# overrides core.sshCommand for every repo in this shell; unset when you cd elsewhere"
AUTHOR_NOTE = "# repo .git/config overrides the identity; these env vars win over it"
GLAB_OLD = "# glab auth token not available in your glab; set GITLAB_TOKEN yourself"


@dataclass
class Item:
    """One variable: a known value, or a command whose output is the value."""
    var: str
    value: Optional[str] = None
    cmd: Optional[List[Tuple[str, bool]]] = None   # (word, quote-it) pairs
    notes: List[str] = field(default_factory=list)
    strip: Optional[str] = None                    # variable to clear for the command's own run

    def plain(self) -> Optional[str]:
        return " ".join(w for w, _ in self.cmd) if self.cmd else None


@dataclass
class Plan:
    who: str                  # identity id, or "default" for the [default] fallback
    fallback: bool            # no identity matched; [default] accounts apply
    items: List[Item]
    notes: List[str]          # comments that belong to no variable (after the header)
    problems: List[str]
    forge: Optional[dict] = None
    ssh_key: Optional[str] = None
    ok: bool = True           # False: no identity, nothing to set (exit 1)


# ---------------------------------------------------------------- quoting

def _sh(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _fish(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _pwsh(s: str) -> str:
    """PowerShell also treats U+2018..U+201B as single quotes; every one is doubled."""
    return "'" + "".join(c * 2 if c in "'\u2018\u2019\u201a\u201b" else c for c in s) + "'"


_QUOTE = {"sh": _sh, "fish": _fish, "pwsh": _pwsh}


def _command(cmd: Sequence[Tuple[str, bool]], q) -> str:
    return " ".join(q(w) if quoted else w for w, quoted in cmd)


def _set_line(shell: str, it: Item) -> str:
    q = _QUOTE[shell]
    if it.cmd:
        c = _command(it.cmd, q)
        if it.strip:
            c = {"sh": f"unset {it.strip}; {c}", "fish": f"env -u {it.strip} {c}",
                 "pwsh": f"& {{ Remove-Item Env:{it.strip} -ErrorAction SilentlyContinue; {c} }}"}[shell]
        return {"sh": f'export {it.var}="$({c})"', "fish": f"set -gx {it.var} ({c})",
                "pwsh": f"$env:{it.var} = ({c})"}[shell]
    v = q(it.value or "")
    return {"sh": f"export {it.var}={v}", "fish": f"set -gx {it.var} {v}", "pwsh": f"$env:{it.var} = {v}"}[shell]


UNSET_VARS = {"gh": ("GH_HOST", "GH_TOKEN", "GH_ENTERPRISE_TOKEN"), "glab": ("GITLAB_HOST", "GITLAB_TOKEN"),
              "ssh": ("GIT_SSH_COMMAND",),
              "author": ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}


def _unset_lines(shell: str, keys: Sequence[str]) -> List[str]:
    """Clear every variable the requested keys can set, whatever the repo matched."""
    names = [n for k in KEYS if k in keys for n in UNSET_VARS[k]]
    if not names:
        return []
    if shell == "sh":
        return ["unset " + " ".join(names)]
    if shell == "fish":
        return [f"set -e -g {n}" for n in names]
    return ["Remove-Item " + ", ".join(f"Env:{n}" for n in names) + " -ErrorAction SilentlyContinue"]


def _comment(text: str) -> str:
    """One comment line: control characters dropped, so no text can end the comment early."""
    return "# " + _CTRL.sub(" ", text)


# ---------------------------------------------------------------- planning

def _valid(value: str, what: str) -> str:
    if not SAFE.match(value):
        raise ConfigError(f"invalid {what} {value!r}: use only letters, digits, '.', '_' and '-'")
    return value


def _glab_has_token_cmd() -> bool:
    """`glab auth token --help` exits 0; False when glab is missing or too old."""
    if not shutil.which("glab"):
        return False
    try:
        r = subprocess.run(["glab", "auth", "token", "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _ambient(var: str) -> List[str]:
    """Note the presence of a token variable, never its value."""
    if os.environ.get(var):
        return [f"# {var} is already set in this shell; the line below replaces it"]
    return []


def _accounts(rep: resolve.Report, cfg: Config) -> Tuple[str, bool, str, Optional[str], Optional[str]]:
    """(who, fallback, host, gh_account, glab_account). A matched identity uses only its own accounts.
    With no match, [default]'s gh account applies to a github.com remote and its glab account to gitlab.com."""
    i, d = rep.expected.match.identity, cfg.default
    if i is not None:
        return i.id, False, i.host, i.gh_account, i.glab_account
    rems = rep.effective.remotes
    host = resolve.parse_url(next(iter(rems.values())))[0] if rems else None
    host = (host or "").lower()
    return ("default", True, host, d.gh_account if host == forge.GITHUB else None,
            d.glab_account if host == forge.GITLAB else None)


def _ssh_command(key: str) -> str:
    """ssh -i KEY -o IdentitiesOnly=yes; ~/ is kept when the rest is plain (git runs the line through a shell)."""
    home = str(Path.home())
    rest = key[len(home) + 1:] if key.startswith(home + "/") else ""
    shown = "~/" + rest if rest and re.match(r"^[A-Za-z0-9._/-]+$", rest) else shlex.quote(key)
    return f"ssh -i {shown} -o IdentitiesOnly=yes"


def _gh_items(host: str, account: str) -> Tuple[List[Item], dict]:
    host, account = _valid(host, "host"), _valid(account, "gh_account")
    var = "GH_TOKEN" if host == forge.GITHUB else "GH_ENTERPRISE_TOKEN"
    cmd = [("gh", False), ("auth", False), ("token", False), ("-u", False), (account, True),
           ("-h", False), (host, True)]
    items = [Item("GH_HOST", host), Item(var, cmd=cmd, notes=_ambient(var))]
    return items, {"host": host, "cli": "gh", "account": account, "token_var": var}


def _glab_items(host: str, account: str) -> Tuple[List[Item], List[str], dict]:
    host, account = _valid(host, "host"), _valid(account, "glab_account")
    info = {"host": host, "cli": "glab", "account": account, "token_var": "GITLAB_TOKEN"}
    if not shutil.which("glab"):
        return [], [], info
    if not _glab_has_token_cmd():
        return [], [GLAB_OLD], info
    cmd = [("glab", False), ("auth", False), ("token", False), ("-h", False), (host, True)]
    return [Item("GITLAB_HOST", host), Item("GITLAB_TOKEN", cmd=cmd, notes=_ambient("GITLAB_TOKEN"), strip="GITLAB_TOKEN")], [], info


def keys_for(rep: resolve.Report, only: Optional[Sequence[str]]) -> Tuple[str, ...]:
    """The keys to handle: --only, else gh,glab (plus author when the repo overrides the identity)."""
    if only:
        return tuple(only)
    over = any(p.code in ("override", "env_override") for p in rep.problems)
    return DEFAULT_ONLY + (("author",) if over else ())


def build(rep: resolve.Report, cfg: Config, only: Sequence[str], explicit: bool) -> Plan:
    """What to set for the repo in rep. only: the keys asked for; explicit: they came from --only."""
    who, fallback, host, gh, glab = _accounts(rep, cfg)
    exp = rep.expected
    problems = [p for p in rep.problems if p.code != "no_identity"]
    items: List[Item] = []
    notes: List[str] = []
    forge_info: Optional[dict] = None
    if "gh" in only and gh:
        got, forge_info = _gh_items(host, gh)
        items += got
    if "glab" in only and glab:
        got, extra, info = _glab_items(host, glab)
        items += got
        notes += extra
        forge_info = forge_info or info
    if "ssh" in only:
        if exp.ssh_key:
            items.append(Item("GIT_SSH_COMMAND", _ssh_command(exp.ssh_key), notes=[SSH_NOTE]))
        else:
            notes.append(f"# identity {who} has no ssh_key; nothing to set for ssh")
    overridden = any(p.code in ("override", "env_override") for p in problems)
    if "author" in only:
        if exp.name and exp.email:
            first = True
            for var, val in (("GIT_AUTHOR_NAME", exp.name), ("GIT_AUTHOR_EMAIL", exp.email),
                             ("GIT_COMMITTER_NAME", exp.name), ("GIT_COMMITTER_EMAIL", exp.email)):
                items.append(Item(var, val, notes=[AUTHOR_NOTE] if first and overridden else []))
                first = False
        else:
            notes.append(f"# identity {who} has no name/email; no author variables to set")
    if not items and not notes:
        if "gh" in only and "glab" in only and not gh and not glab and not fallback:
            notes.append(f"# gitpro env: identity {who} has no gh_account/glab_account; nothing to set")
        else:
            notes.append(f"# gitpro env: nothing to set for identity {who}")
    return Plan(who, fallback, items, notes, [p.message for p in problems], forge_info, exp.ssh_key)


def _describe(rep: resolve.Report, plan: Plan) -> str:
    e = rep.expected
    m = e.match
    if plan.fallback:
        return "no identity matched; using [default]"
    url = resolve.redact_url(m.url)
    return f"identity {plan.who}" + (f" <{e.email}>" if e.email else "") + f" ({m.remote or 'url'} {url})"


def _tail(shell: str, only: Sequence[str], explicit: bool) -> List[str]:
    flag = f" --only {','.join(only)}" if explicit else ""
    if shell == "sh":
        return [f'# usage: eval "$(gitpro env{flag})"      undo: eval "$(gitpro env{flag} --unset)"',
                "# direnv: echo 'eval \"$(gitpro env)\"' >> .envrc && direnv allow"]
    pipe = {"fish": "| source", "pwsh": "| Out-String | Invoke-Expression"}[shell]
    return [f"# usage: gitpro env --shell {shell}{flag} {pipe}      undo: gitpro env --shell {shell}{flag} "
            f"--unset {pipe}"]


def lines(rep: resolve.Report, cfg: Config, shell: str = "sh", only: Optional[Sequence[str]] = None,
          unset: bool = False, plan: Optional[Plan] = None) -> List[str]:
    """The complete output, comments included, one string per line."""
    shell = "sh" if shell in ("bash", "zsh") else shell
    keys = keys_for(rep, only)
    plan = plan or build(rep, cfg, keys, True)
    out = [_comment(f"gitpro env for {_tilde(rep.repo)}: {_describe(rep, plan)}")]
    if unset:
        out += _unset_lines(shell, keys)
    else:
        out += plan.notes
        for it in plan.items:
            out += it.notes + [_set_line(shell, it)]
    out += [_comment("⚠ " + m) for m in plan.problems]
    return out + _tail(shell, keys, bool(only))


def _tilde(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def as_json(rep: resolve.Report, cfg: Config, shell: str = "sh", only: Optional[Sequence[str]] = None,
            unset: bool = False, plan: Optional[Plan] = None) -> dict:
    """The --json payload: values gitpro knows in `env`, commands for the rest in `env_commands`."""
    keys = keys_for(rep, only)
    plan = plan or build(rep, cfg, keys, True)
    e = rep.expected
    ident = {"id": e.id, "name": e.name, "email": e.email} if e.id else None
    body = [ln for ln in lines(rep, cfg, shell, only, unset, plan) if not ln.startswith("#")]
    return {"repo": str(rep.repo), "identity": ident, "forge": plan.forge, "ssh_key": plan.ssh_key,
            "env": {i.var: i.value for i in plan.items if i.cmd is None},
            "env_commands": {i.var: i.plain() for i in plan.items if i.cmd},
            "lines": body, "notes": plan.notes, "problems": plan.problems}


# ---------------------------------------------------------------- the command

def _parse_only(raw: Optional[str], args) -> Optional[List[str]]:
    if raw is None:
        return None
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    for k in keys:
        if k not in KEYS:
            raise ConfigError(f"unknown key '{k}' ({', '.join(KEYS)})")
    return keys


def _check_gh(host: str, account: str) -> None:
    """--check: run the printed `gh auth token` once, discard the output (never shown)."""
    try:
        r = subprocess.run(["gh", "auth", "token", "-u", account, "-h", host], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=forge.TOKEN_TIMEOUT,
                           env=forge._stripped(forge._GH_TOKEN_VARS))
    except FileNotFoundError:
        raise ConfigError("gh not found; install the GitHub CLI (https://cli.github.com)") from None
    except subprocess.TimeoutExpired:
        raise ConfigError("gh auth token timed out (keyring locked?)") from None
    if r.returncode != 0 or not r.stdout.strip():
        if "unknown shorthand flag" in r.stderr or "unknown flag" in r.stderr:
            raise ConfigError("gh 2.40 or newer is needed for multiple accounts")
        raise ConfigError(f"gh has no login for {account} on {host}; run: gh auth login -h {host}")


def _no_output(args, text: str, rc: int) -> int:
    """A comment-only answer (safe to eval) or, under --json, the error object."""
    if getattr(args, "json", False):
        jsonout.error("env", text[2:] if text.startswith("# ") else text)
    else:
        print(text)
    return rc


def cmd_env(args) -> int:
    only = _parse_only(args.only, args)
    shell = {"bash": "sh", "zsh": "sh"}.get(args.shell, args.shell)
    if shell not in SHELLS:
        return fail(f"unknown shell '{args.shell}' ({', '.join(SHELLS)})", args)
    resolve.require_git()
    home = Path(args.home).expanduser().resolve()
    try:
        path, how = config.find(args.config, home)
    except ConfigError as e:
        return _no_output(args, f"# gitpro env: {e}", 2)
    cfg = config.load_all(path, how)
    top = resolve.is_repo(Path(args.dir))
    if top is None:
        return _no_output(args, f"# gitpro env: {_CTRL.sub(' ', str(args.dir))} is not a git repository", 2)
    rep = resolve.compare(top, cfg)
    keys = keys_for(rep, only)
    plan = build(rep, cfg, keys, True)
    who, fallback, host, gh, glab = _accounts(rep, cfg)
    if fallback and not (gh or glab) and not args.unset:
        rems = rep.effective.remotes
        h, owner, _ = resolve.parse_url(next(iter(rems.values()))) if rems else (None, None, None)
        shown = "/".join(x for x in (h, owner) if x) or str(top)
        return _no_output(args, _comment(f"gitpro env: no identity for {shown}; nothing to set"), 1)
    if args.check:
        if gh and "gh" in keys:
            _check_gh(_valid(host, "host"), _valid(gh, "gh_account"))
            print(f"✓ gh token for {gh} on {host} is available", file=sys.stderr)
        else:
            print("· nothing to check: no gh account for this repo", file=sys.stderr)
    if getattr(args, "json", False):
        jsonout.emit("env", as_json(rep, cfg, shell, only, args.unset, plan))
    else:
        print("\n".join(lines(rep, cfg, shell, only, args.unset, plan)))
    return 0


def register(sub, common, confirm) -> None:
    sp = sub.add_parser("env", parents=[common],
                        help="print shell lines that set GH_TOKEN etc. for the repo's identity (fetches, never prints, tokens)")
    sp.add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    sp.add_argument("--shell", default="sh", metavar="SHELL", help="sh (also bash, zsh), fish or pwsh (default: sh)")
    sp.add_argument("--only", metavar="KEY[,KEY...]", help="gh, glab, ssh, author (default: gh,glab)")
    sp.add_argument("--unset", action="store_true", help="print the lines that undo the same variables")
    sp.add_argument("--check", action="store_true", help="run the gh auth token command once to see it works")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=lambda a: guarded(cmd_env, a))
    capability("env")
