"""`gitpro prompt`: a shell prompt segment (`✓ work-github`, `✗ old@personal.test`), cheap enough for every prompt.

A hot module (hot.HOT): module level imports only the standard library and args; config and resolve load
on a cache miss (a hit costs a few stat calls on top of Python's start-up); never cli, view or rich. A cache in the state dir, keyed by the repo's git dir, holds the last answer plus the
(path, size, mtime_ns, inode) of every file that can change it and a hash of the identity env vars. A hit
needs no git call and no TOML parse; a miss runs resolve.compare (three git calls) and rewrites the cache.
Prints one line or nothing, and always exits 0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import args as gargs

CACHE_NAME = "prompt-cache.json"
CACHE_VERSION = 1
MAX_ENTRIES = 200
DEFAULT_MAX_MS = 80
SYMBOLS = {"ok": "✓", "mismatch": "✗", "warn": "⚠", "none": "⚠"}
# Any of these makes the answer depend on things gitpro cannot stat cheaply: no cache read or write.
BYPASS_VARS = ("GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_PARAMETERS")
ENV_PREFIXES = ("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG_")
CONFIG_ENV_VAR = "GITPRO_CONFIG"              # config.ENV_VAR
USER_CONFIG = ".config/gitpro.toml"           # config.USER_CONFIG
PIN_FILE = "git-identities"                   # resolve.PIN_FILE
MANAGED_GITCONFIG = ".gitconfig-identities"   # resolve.MANAGED_GITCONFIG
ENV_NAMES = ("HOME", CONFIG_ENV_VAR)
# resolve problem codes that leave the email right: `⚠ id` (state "warn"); every other code is `✗ email`.
WARN_CODES = ("key_mismatch", "not_applied", "sign_mismatch", "policy")

INSTALL_HELP = """\
bash:     PS1='$(gitpro prompt) '"$PS1"
zsh:      autoload -Uz add-zsh-hook; _gitpro_prompt() { psvar[1]=$(gitpro prompt); }; add-zsh-hook precmd _gitpro_prompt; PS1='%1v '"$PS1"
fish:     function fish_prompt; gitpro prompt; echo -n ' '; prompt_pwd; echo -n ' > '; end
starship: [custom.gitpro] command = "gitpro prompt" when = "git rev-parse --git-dir" shell = ["sh"]
"""

Stat = List[object]   # [path, size, mtime_ns, inode]; the last three are None for a missing file


# ---------------------------------------------------------------- finding the repo without git

def _read_line(path: Path) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.readline().strip()


def _git_dirs(start: Path) -> Optional[Tuple[Path, Path]]:
    """(git dir, common dir) of the repo holding start, found by walking up like git does; None outside
    a repo. Reads at most two small files; never runs git."""
    d = start.resolve()
    for cand in (d, *d.parents):
        dot = cand / ".git"
        if dot.is_dir():
            git_dir = dot
        elif dot.is_file():
            line = _read_line(dot)
            if not line.startswith("gitdir:"):
                return None
            git_dir = Path(os.path.normpath(cand / line[len("gitdir:"):].strip()))
        elif (cand / "HEAD").is_file() and (cand / "objects").is_dir() and (cand / "refs").is_dir():
            git_dir = cand          # inside a git dir, or a bare repo
        else:
            continue
        common = git_dir
        if (git_dir / "commondir").is_file():
            common = Path(os.path.normpath(git_dir / _read_line(git_dir / "commondir")))
        return git_dir, common
    return None


# ---------------------------------------------------------------- the cache key

def _stat(path: object) -> Stat:
    try:
        st = os.stat(str(path))
    except OSError:
        return [str(path), None, None, None]
    return [str(path), st.st_size, st.st_mtime_ns, st.st_ino]


def _xdg_git_config(home: Path) -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "git" / "config"


def _head_inputs(git_dir: Path, common: Path, cfg_path: Path, home: Path) -> List[str]:
    """The inputs known before the TOML is read; the identity files follow them in the stored key."""
    return [str(p) for p in (cfg_path, common / "config", git_dir / "config.worktree", common / PIN_FILE,
                             home / ".gitconfig", home / MANAGED_GITCONFIG, _xdg_git_config(home))]


def cache_key(git_dir: Path, common: Path, cfg_path: Path, home: Path, extra: Sequence[object] = ()) -> List[Stat]:
    """[(path, size, mtime_ns, inode)] of every file that can change the answer: the TOML, .git/config,
    config.worktree, the pin file, ~/.gitconfig, ~/.gitconfig-identities, git's XDG config, and `extra`
    (each identity's ~/.gitconfig-<id>)."""
    return [_stat(p) for p in [*_head_inputs(git_dir, common, cfg_path, home), *map(str, extra)]]


def env_hash(env: Optional[Dict[str, str]] = None) -> str:
    """sha256 of the env vars that affect identity (never stored in clear)."""
    env = os.environ if env is None else env
    items = sorted((k, v) for k, v in env.items() if k.startswith(ENV_PREFIXES) or k in ENV_NAMES)
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()


def cache_bypassed(env: Optional[Dict[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return any(env.get(v) for v in BYPASS_VARS)


def cache_path(home: Optional[Path] = None) -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str((home or Path.home()) / ".local/state")
    return Path(base) / "gitpro" / CACHE_NAME


def _load_cache(path: Path) -> dict:
    """The cache's entries; {} when the file is missing, unreadable or the wrong shape."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION or not isinstance(data.get("entries"), dict):
        return {}
    return {k: v for k, v in data["entries"].items() if _valid_entry(v)}


def _valid_entry(e: object) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("key"), list) and isinstance(e.get("env"), str)
            and isinstance(e.get("result"), dict)
            and isinstance(e.get("t"), (int, float)) and not isinstance(e.get("t"), bool)
            and all(isinstance(k, list) and len(k) == 4 and isinstance(k[0], str) for k in e["key"]))


def _save_cache(path: Path, entries: dict) -> None:
    from .files import atomic_write
    if len(entries) > MAX_ENTRIES:
        keep = sorted(entries, key=lambda k: entries[k].get("t", 0))[-MAX_ENTRIES:]
        entries = {k: entries[k] for k in keep}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(path, json.dumps({"version": CACHE_VERSION, "entries": entries}, ensure_ascii=False), 0o600)


def _hit(entry: dict, head: List[str], env: str) -> bool:
    key = entry["key"]
    return (entry["env"] == env and [k[0] for k in key[:len(head)]] == head
            and all(_stat(k[0]) == k for k in key))


# ---------------------------------------------------------------- the answer

def find_config(explicit: Optional[Path], home: Path) -> Path:
    """config.find without importing config (same order: -c, $GITPRO_CONFIG, ~/.config/gitpro.toml)."""
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get(CONFIG_ENV_VAR):
        return Path(os.environ[CONFIG_ENV_VAR]).expanduser()
    if (home / USER_CONFIG).is_file():
        return home / USER_CONFIG
    from . import config
    return config.find(None, home)[0]     # raises the usual "no config found"


def _compute(repo: Path, cfg_path: Path) -> Tuple[dict, List[str]]:
    """(result, identity files) for repo: three git calls through resolve.compare."""
    from . import config, resolve
    cfg = config.load_all(cfg_path)
    rep = resolve.compare(repo, cfg)
    exp, eff = rep.expected, rep.effective
    key = resolve.ssh_key_of(eff.sshcommand.value) or exp.ssh_key
    if exp.email is None:
        state = "none"                      # spec test 10: `⚠ no identity`
    elif (eff.email.value or "").lower() != exp.email.lower() or \
            any(p.code not in WARN_CODES for p in rep.problems):
        state = "mismatch"                  # git would (or might) commit as the wrong email
    else:
        state = "warn" if rep.problems else "ok"   # right email, something else off (key, sign, not applied...)
    result = {"id": exp.id or ("default" if exp.email else ""), "email": eff.email.value or "",
              "key": os.path.basename(key) if key else "", "state": state}
    files = [os.path.expanduser(i.file) for i in cfg.identities if i.file]
    return result, files


def render(result: dict, fmt: Optional[str]) -> str:
    """The segment text for a cached or computed result."""
    state = result.get("state", "none")
    vals = {"id": result.get("id", ""), "email": result.get("email", ""), "key": result.get("key", ""),
            "state": state, "sym": SYMBOLS.get(state, "⚠")}
    if fmt is not None:
        return fmt.format(**vals)
    if state == "ok":
        return f"{vals['sym']} {vals['id']}"
    if state == "mismatch":
        return f"{vals['sym']} {vals['email'] or '(no email)'}"
    if state == "warn":
        return f"{vals['sym']} {vals['id']}"
    return f"{vals['sym']} no identity"


def segment(dir: object = ".", fmt: Optional[str] = None, max_ms: int = DEFAULT_MAX_MS, *,
            config_path: Optional[Path] = None, home: Optional[Path] = None) -> str:
    """The prompt text for the repo holding dir ("" outside a repo, or over budget with nothing cached).
    Raises on errors; main() turns every error into empty output."""
    home = Path(home) if home else Path.home()
    found = _git_dirs(Path(dir))
    if found is None:
        return ""
    git_dir, common = found
    cfg_path = find_config(config_path, home)
    use_cache = not cache_bypassed()
    cpath = cache_path(home)
    entries = _load_cache(cpath) if use_cache else {}
    slot = str(git_dir)
    old = entries.get(slot)
    head = _head_inputs(git_dir, common, cfg_path, home)
    env = env_hash()
    if old is not None and _hit(old, head, env):
        return render(old["result"], fmt)

    start = time.monotonic()
    key_before = cache_key(git_dir, common, cfg_path, home)
    result, files = _compute(Path(dir), cfg_path)
    elapsed_ms = (time.monotonic() - start) * 1000
    if use_cache:
        # Stat before computing, so an edit made while git ran invalidates this entry next time.
        key = key_before + [_stat(f) for f in files]
        entries[slot] = {"key": key, "env": env, "result": result, "t": time.time()}
        try:
            _save_cache(cpath, entries)
        except OSError:     # read-only or broken state dir: still answer, just uncached
            pass
    if elapsed_ms > max_ms:
        return render(old["result"], fmt) if old is not None else ""
    return render(result, fmt)


# ---------------------------------------------------------------- command

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    p.add_argument("--format", dest="fmt", metavar="FMT",
                   help='placeholders {id} {email} {key} {state} {sym} (default: "{sym} {id}", '
                        "the effective email on a mismatch)")
    p.add_argument("--max-ms", type=int, default=DEFAULT_MAX_MS, metavar="N",
                   help=f"print nothing (or the last cached value) when git takes longer (default: {DEFAULT_MAX_MS})")
    p.add_argument("--install-help", action="store_true", help="print the lines to add to your shell's prompt")
    p.set_defaults(func=cmd_prompt)


def _write(text: str) -> None:
    """Write text to stdout; an unencodable string or a closed pipe prints nothing (never raises)."""
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (OSError, UnicodeError, ValueError):
        try:   # stop the interpreter's own flush at exit from failing again
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass


def cmd_prompt(args: argparse.Namespace) -> int:
    if args.install_help:
        _write(INSTALL_HELP)
        return 0
    try:
        text = segment(args.dir, args.fmt, args.max_ms, config_path=args.config, home=args.home)
    except (Exception, KeyboardInterrupt):   # a prompt never shows a traceback: empty output, exit 0
        text = ""
    if text:
        _write(text + "\n")
    return 0


def register(sub, common, confirm) -> None:
    add_arguments(sub.add_parser("prompt", parents=[common], help="shell prompt segment: ✓ id or ✗ wrong email"))
    gargs.capability("prompt")


class _BadArgs(Exception):
    pass


class _QuietParser(argparse.ArgumentParser):
    """A usage error prints nothing (a prompt must never print usage text into PS1)."""

    def error(self, message):
        raise _BadArgs(message)


def main(argv: Sequence[str]) -> int:
    """Always 0: bad arguments, errors and Ctrl-C print nothing (--help still prints help)."""
    p = _QuietParser(prog="gitpro prompt", parents=[gargs.common()])
    add_arguments(p)
    try:
        return cmd_prompt(p.parse_args(list(argv)[1:]))
    except (_BadArgs, KeyboardInterrupt):
        return 0
