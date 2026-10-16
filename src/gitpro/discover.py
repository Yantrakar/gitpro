"""`gitpro discover` and `init --discover`: suggest [[identity]] blocks and [scan] roots from what is on the machine.

Sources (spec 04): editor recents (the VS Code family, JetBrains), ~/.ssh/config host aliases,
includeIf rules in the global gitconfig and the repos on disk; on request also shell history
(--history) and the gh/glab logins (--online). Discovery only reads: it prints a TOML snippet to
paste and never writes the config. `init --discover` appends the snippet, every line commented, to
the file init has just created.

Privacy: history lines are never stored or shown, only the sanitised clone URLs found in them (no
userinfo, query or fragment; anything token-like drops the URL). Private keys, gh's hosts.yml,
.netrc and .git-credentials are never opened; of ~/.ssh only config files and .pub files are read.
Tokens are never read: with --online gh and glab are asked for their logins, and gh for the
verified emails of the active login.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

from . import config, files, jsonout, undo
from .config import EMAIL_RE, HOST_RE, ID_RE, OWNER_RE, PATH_RE, PLACEHOLDER_RE

PROG = "gitpro"
SOURCES = ("history", "vscode", "jetbrains", "ssh", "includeif", "repos", "gh", "glab")
DEFAULT_SOURCES = ("vscode", "jetbrains", "ssh", "includeif", "repos")
ONLINE_SOURCES = ("gh", "glab")
BUDGET = 5.0            # seconds per source (history, repos)
HISTORY_CAP = 200_000   # lines per history file
MAX_DEPTH = 3           # how deep `repos` walks under each root
HOME_SKIP = ["~/Library", "~/snap", "~/go/pkg", "__pycache__"]
TIMEOUT = 30            # seconds for each gh/glab/git call
FORGE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "dev.azure.com", "ssh.dev.azure.com")
VSCODE_APPS = ("Code", "Code - Insiders", "VSCodium", "Cursor")
VSCODE_KEY = "history.recentlyOpenedPathsList"
JSON_KEYS = ("sources", "hosts", "aliases", "roots", "accounts", "rules", "config", "toml", "warnings")

_TOKEN_RE = re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                       r"|glpat-[A-Za-z0-9_-]{15,}|glptt-[A-Za-z0-9_-]{15,}|gldt-[A-Za-z0-9_-]{15,}"
                       r"|x-access-token:|oauth2:|Authorization")
_BAD_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SHOW_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")   # replaced by ? in what is shown
_TOML_ESCAPE = re.compile("[\x7f-\x9f\u2028\u2029]")              # json.dumps leaves these as they are
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SCP = re.compile(r"^(?:([^@/:]+)@)?([^@/:\[\]]+):(.*)$")
_GLOB = re.compile(r"[*?\[]")
_CLONE_RE = re.compile(r"\b(git|gh|glab)\s+(repo\s+)?clone\b")
_ZSH_EXT = re.compile(r"^: \d+:\d+;")
_SEPARATOR = set(";&|()<>")
# git clone options that take the next word as their value
_GIT_VALUE_OPTS = {"-b", "--branch", "-o", "--origin", "-c", "--config", "--depth", "--reference",
                   "--reference-if-able", "--template", "--separate-git-dir", "-u", "--upload-pack", "-j",
                   "--jobs", "--filter", "--server-option", "--shallow-since", "--shallow-exclude",
                   "--bundle-uri", "--ref-format", "--revision"}
_FORGE_VALUE_OPTS = {"-u", "--upstream-remote-name", "-g", "--group"}
_BLOCK_MARK = re.compile(r"^# (BEGIN|END) (?:gitpro|git-identities|git_identities\.py)\b")


# ---------------------------------------------------------------- small helpers

def _plural(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _tilde(path, home: Optional[Path] = None) -> str:
    """path with the home directory (--home, else the real one) shown as ~."""
    if home is None:
        return undo.tilde(Path(path))
    p, h = str(path), str(home).rstrip("/")
    return "~" if p == h else "~/" + p[len(h) + 1:] if p.startswith(h + "/") else p


def _temp_dirs() -> List[str]:
    import tempfile
    out = {"/tmp", "/var/tmp", os.environ.get("TMPDIR") or "/tmp", tempfile.gettempdir()}
    for t in list(out):
        with contextlib.suppress(OSError):
            out.add(os.path.realpath(t))
    return sorted(os.path.normpath(t) for t in out if t)


def _is_ssh_url(url: str) -> bool:
    """scp-like (host:path) or ssh:// URLs, the only ones an ssh Host alias applies to."""
    m = _SCHEME.match(url)
    return m is None or m.group(0)[:-3].lower() in ("ssh", "git+ssh", "ssh+git")


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/") or root == ""


def _usable_name(n: object) -> bool:
    return isinstance(n, str) and bool(n.strip()) and not _CONTROL.search(n) and not re.search(r"[<>]", n)


def _usable_email(e: object) -> bool:
    return (isinstance(e, str) and bool(EMAIL_RE.match(e)) and not _CONTROL.search(e)
            and not PLACEHOLDER_RE.search(e.rsplit("@", 1)[1]))


def _usable_host(h: object) -> bool:
    return isinstance(h, str) and bool(HOST_RE.match(h)) and not PLACEHOLDER_RE.search(h)


def _is_forge(host: str) -> bool:
    return host in FORGE_HOSTS or host.endswith(".visualstudio.com")


def _env(strip=()) -> Dict[str, str]:
    drop = set(strip) | {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR"}
    return {k: v for k, v in os.environ.items() if k not in drop}


def _first_line(text: str) -> str:
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    line = _CONTROL.sub("?", _TOKEN_RE.sub("…", line))
    return line[:200]


# ---------------------------------------------------------------- sanitise

def _clean(url: object) -> Tuple[Optional[str], bool]:
    """(sanitised url or None, whether it was dropped for looking like a secret)."""
    if not isinstance(url, str) or not url:
        return None, False
    if _TOKEN_RE.search(url):
        return None, True
    if _BAD_CHARS.search(url):
        return None, False
    if _SCHEME.match(url):
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 -- raises ValueError on a bad port
        except ValueError:
            return None, False
        if parts.scheme.lower() == "file":
            return None, False
        netloc = parts.netloc.rpartition("@")[2]
        if not netloc or "@" in parts.path:
            return None, False
        out = f"{parts.scheme.lower()}://{netloc}{parts.path}"
    else:
        m = _SCP.match(url)
        if not m:
            return None, False
        user, host, path = m.groups()
        path = re.split(r"[?#]", path, maxsplit=1)[0]
        if "@" in path:
            return None, False
        out = (f"{user}@" if user and ID_RE.match(user) else "") + f"{host}:{path}"
    if _TOKEN_RE.search(out):
        return None, True
    from .resolve import parse_url
    host, owner, repo = parse_url(out)
    if not (host and owner and repo and HOST_RE.match(host)):
        return None, False
    return out, False


def sanitise(url: str) -> Optional[str]:
    """url without userinfo, query and fragment (an scp-style ssh user is kept when it is a plain
    name such as git); None when it looks like it holds a secret or has no host/owner/repo."""
    return _clean(url)[0]


# ---------------------------------------------------------------- shell history

def _unmetafy(b: bytes) -> bytes:
    """zsh writes some bytes as 0x83 followed by the byte XOR 0x20."""
    if b"\x83" not in b:
        return b
    out = bytearray()
    it = iter(b)
    for c in it:
        if c == 0x83:
            nxt = next(it, None)
            if nxt is None:
                break
            out.append(nxt ^ 0x20)
        else:
            out.append(c)
    return bytes(out)


def _history_files(home: Path) -> List[Tuple[Path, str]]:
    """Existing history files with their kind (bash, zsh, fish, auto); only stats them."""
    data = os.environ.get("XDG_DATA_HOME") or str(home / ".local/share")
    cands = [(home / ".bash_history", "bash"), (home / ".zsh_history", "zsh"),
             (Path(data) / "fish/fish_history", "fish")]
    hf = os.environ.get("HISTFILE")
    if hf:
        p = Path(os.path.expanduser(hf))
        p = p if p.is_absolute() else home / p
        name = p.name.lower()
        cands.append((p, "zsh" if "zsh" in name else "fish" if "fish" in name else "auto"))
    out, seen = [], set()
    for p, kind in cands:
        try:
            if not p.is_file():
                continue
            key = p.resolve()
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            out.append((p, kind))
    return out


def _decode(line: bytes, kind: str) -> str:
    if kind == "zsh":
        return _unmetafy(line).decode("utf-8", "replace")
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if kind == "auto":
        try:
            return _unmetafy(line).decode("utf-8")
        except UnicodeDecodeError:
            pass
    return line.decode("utf-8", "replace")


def _tail(path: Path, n: int, deadline: float) -> Tuple[List[bytes], bool]:
    """The last n lines of a file, read backwards from its end; (lines, whether older lines were left out)."""
    chunks: List[bytes] = []
    count = 0
    with open(path, "rb") as f:
        pos = f.seek(0, os.SEEK_END)
        while pos > 0 and count <= n and time.monotonic() <= deadline:
            step = min(1 << 20, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step)
            chunks.append(chunk)
            count += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if pos > 0 and lines:
        lines.pop(0)            # the part of a line cut by where reading started
    return lines[-n:] if n else [], pos > 0 or len(lines) > n


def _history_commands(path: Path, kind: str, cut: List[bool]) -> Iterator[str]:
    """The commands in the newest HISTORY_CAP lines of one history file, within its own BUDGET.
    Sets cut[0] when older lines were left out or time ran out."""
    deadline = time.monotonic() + BUDGET
    lines, cut[0] = _tail(path, HISTORY_CAP, deadline)
    for raw in lines:
        if time.monotonic() > deadline:
            cut[0] = True
            return
        text = _decode(raw.rstrip(b"\r"), kind)
        if kind == "fish" or (kind == "auto" and text.startswith("- cmd: ")):
            if not text.startswith("- cmd: "):
                continue
            text = text[7:]
        m = _ZSH_EXT.match(text)
        yield text[m.end():] if m else text


def _tokens(cmd: str) -> List[str]:
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return cmd.split()


def _forge_shorthand(arg: str, prog: str) -> Optional[str]:
    """gh/glab accept OWNER/REPO (glab: GROUP/SUB/REPO); turn it into an https URL."""
    if _SCHEME.match(arg) or re.match(r"^[^/]+@[^/]+:", arg):
        return arg
    parts = [p for p in arg.split("/") if p]
    default = "github.com" if prog == "gh" else "gitlab.com"
    if len(parts) >= 3 and "." in parts[0]:
        host = parts.pop(0)
    elif len(parts) == 2 or (prog == "glab" and len(parts) > 2):
        host = default
    else:
        return None
    return f"https://{host}/{'/'.join(parts)}"


def _target(arg: str, home: Path) -> Optional[Path]:
    """A clone target dir as an absolute path; None when it is relative (the cwd is unknown)."""
    for pre in ("~/", "$HOME/", "${HOME}/"):
        if arg.startswith(pre):
            p = str(home) + "/" + arg[len(pre):]
            break
    else:
        if arg in ("~", "$HOME", "${HOME}"):
            p = str(home)
        elif arg.startswith("/"):
            p = arg
        else:
            return None
    if _CONTROL.search(p):
        return None
    return Path(os.path.normpath(p))


def parse_clone(cmd: str, home: Path) -> List[Tuple[str, Optional[Path]]]:
    """(url as written, target dir or None) for each git/gh/glab clone in a shell command."""
    segs: List[List[str]] = [[]]
    for t in _tokens(cmd):
        if t and set(t) <= _SEPARATOR:
            segs.append([])
        else:
            segs[-1].append(t)
    out = []
    for seg in segs:
        for k, tok in enumerate(seg):
            prog = tok.rsplit("/", 1)[-1]
            if prog == "git" and seg[k + 1:k + 2] == ["clone"]:
                args = seg[k + 2:]
            elif prog in ("gh", "glab") and seg[k + 1:k + 3] == ["repo", "clone"]:
                args = seg[k + 3:]
            else:
                continue
            opts = _GIT_VALUE_OPTS if prog == "git" else _FORGE_VALUE_OPTS
            pos: List[str] = []
            n = 0
            group = False
            while n < len(args):
                a = args[n]
                if a == "--":
                    if prog == "git":
                        pos.extend(args[n + 1:])
                    break
                if a.startswith("-") and len(a) > 1:
                    group = group or a in ("-g", "--group")
                    n += 2 if a in opts else 1
                    continue
                pos.append(a)
                n += 1
            if not pos or group:
                break
            url: Optional[str] = pos[0]
            if prog != "git":
                url = _forge_shorthand(pos[0], prog)
            if url:
                out.append((url, _target(pos[1], home) if len(pos) > 1 else None))
            break
    return out


# ---------------------------------------------------------------- the collection context

class _Ctx:
    def __init__(self, home: Path):
        self.home = home
        self.warnings: List[str] = []
        self.findings: List[str] = []       # -v lines
        self.owners: Dict[Tuple[str, str], dict] = {}
        self.roots: List[str] = []          # candidate roots
        self.repo_paths: Dict[str, str] = {}  # resolved path -> path as found
        self.ssh_map: Dict[str, dict] = {}  # lowercased ssh Host alias -> entry
        self.include_files: set = set()     # resolved paths of includeIf targets (not gitpro's)
        self.walked = 0
        self.reflog = False                 # setup: also the commit identity of each repo (spec 22 §5.3)
        self.committed = 0                  # repos with commit lines in their reflog

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"⚠ {msg}", file=sys.stderr, flush=True)

    def note(self, msg: str) -> None:
        self.findings.append(_c(msg))

    def add_root(self, p) -> None:
        p = os.path.normpath(str(p))
        home = str(self.home)
        if p == "/" or _under(home, p) and p != home:   # never / or a parent of the home directory
            return
        if not _under(p, home) and any(_under(p, t) for t in _temp_dirs()):
            return                                       # a clone in /tmp is not a place to scan
        if p not in self.roots:
            self.roots.append(p)

    def rec(self, host: str, owner: str) -> dict:
        """The record of host/owner; owners that differ only in case share one (forges ignore case)."""
        rec = self.owners.setdefault((host, owner.lower()), {
            "repos": set(), "emails": Counter(), "names": Counter(), "paths": [], "aliases": Counter(),
            "rule": None, "sources": set(), "spellings": Counter(),
            "people": Counter()})
        rec["spellings"][owner] += 1
        return rec

    def add_url(self, source: str, url: str, *, path=None, person: Optional[dict] = None) -> None:
        from .resolve import parse_url
        host, owner, repo = parse_url(url)
        if not (host and owner and repo) or _GLOB.search(owner):
            return
        host = host.lower()
        # an ssh Host alias renames the host of ssh remotes only; https://<alias>/ is a real host name
        alias = self.ssh_map.get(host) if _is_ssh_url(url) else None
        if alias:
            host = alias["hostname"].lower()
        rec = self.rec(host, owner)
        rec["sources"].add(source)
        if path is not None:
            rec["paths"].append(os.path.normpath(str(path)))
        if alias and not alias["same_forge"]:
            rec["aliases"][alias["alias"]] += 1
        key = repo.lower()
        if key in rec["repos"]:
            return
        rec["repos"].add(key)
        person = person or {}
        email, name = person.get("user.email"), person.get("user.name")
        if isinstance(email, str) and EMAIL_RE.match(email) and not _CONTROL.search(email):
            rec["emails"][email] += 1
        if isinstance(name, str) and name.strip() and not re.search(r"[\x00-\x08\x0a-\x1f\x7f]", name):
            rec["names"][name.strip()] += 1
            if isinstance(email, str) and EMAIL_RE.match(email) and not _CONTROL.search(email):
                rec["people"][(name.strip(), email)] += 1

    def add_repo(self, source: str, path: Path) -> None:
        try:
            key = str(Path(path).resolve())
        except OSError:
            return
        if key in self.repo_paths:
            return
        self.repo_paths[key] = os.path.normpath(str(path))
        from . import scan
        try:
            repo = scan.inspect(Path(path))
        except (config.ConfigError, OSError, subprocess.SubprocessError, UnicodeError):
            self.note(f"{source}  {_tilde(path, self.home)}  (could not inspect)")
            return
        url = sanitise(repo.remote) if repo.remote else None
        self.note(f"{source}  {_tilde(path, self.home)}  {url or ('(remote not usable)' if repo.remote else '(no remote)')}")
        if url:
            person = self._person(repo)
            if self.reflog:
                from .scan import commit_identity
                found = commit_identity(Path(path))
                if found:
                    self.committed += 1
                    if "user.email" not in person:
                        person = {"user.name": found[0], "user.email": found[1]}
            self.add_url(source, url, path=path, person=person)

    def _person(self, repo) -> dict:
        """user.email/user.name of a repo when they say something about its owner: set in the repo
        itself, or by an includeIf file (not the plain global value every repo shares)."""
        out = {}
        for key in ("user.email", "user.name"):
            if key in repo.local:
                out[key] = repo.local[key]
            elif key in repo.now:
                value, origin = repo.now[key]
                if origin.startswith("file:"):
                    p = Path(origin[5:])
                    p = p if p.is_absolute() else repo.path / p
                    try:
                        if str(p.resolve()) in self.include_files:
                            out[key] = value
                    except OSError:
                        pass
        return out


# ---------------------------------------------------------------- sources

def _history(home: Path, ctx: _Ctx) -> dict:
    """Clone lines in the newest HISTORY_CAP lines of each history file (each with its own BUDGET)."""
    found = _history_files(home)
    info: dict = {"files": [], "lines": 0, "secrets_skipped": 0, "truncated": []}
    if not found:
        ctx.note("history: no shell history file found")
    for p, kind in found:
        cut = [False]
        try:
            for cmd in _history_commands(p, kind, cut):
                if not _CLONE_RE.search(cmd):
                    continue
                info["lines"] += 1
                for raw, target in parse_clone(cmd, home):
                    url, secret = _clean(raw)
                    if secret:
                        info["secrets_skipped"] += 1
                    if url is None:
                        continue
                    ctx.add_url("history", url, path=target)
                    ctx.note(f"history  {url}" + (f" → {_tilde(target, home)}" if target else ""))
                    if target is not None:
                        ctx.add_root(target.parent)
        except OSError as e:
            ctx.note(f"history: could not read {_tilde(p, home)} ({_first_line(e.strerror or str(e))})")
            continue
        info["files"].append(str(p))
        if cut[0]:
            info["truncated"].append(str(p))
            ctx.note(f"history: read only the newest {HISTORY_CAP} lines (or {BUDGET:g} s) of {_tilde(p, home)}")
    return info


def _vscdb_recents(sqlite3, db: Path):
    uri = "file:" + quote(str(db)) + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (VSCODE_KEY,)).fetchone()
    finally:
        con.close()
    if row is None:
        return []
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("unexpected format")
    entries = data.get("entries") or []
    if not isinstance(entries, list):
        raise ValueError("unexpected format")
    return entries


def _vscode(home: Path, ctx: _Ctx) -> Tuple[dict, List[Path]]:
    info = {"files": [], "entries": 0, "remote_entries": 0}
    paths: List[Path] = []
    dbs = [base / app / "User/globalStorage/state.vscdb"
           for base in (home / ".config", home / "Library/Application Support") for app in VSCODE_APPS]
    dbs = [p for p in dbs if p.is_file()]
    if not dbs:
        ctx.note("vscode: state.vscdb not found")
        return info, paths
    try:
        import sqlite3
    except ImportError:
        ctx.warn("vscode: python has no sqlite3 module; skipped")
        return info, paths
    for db in dbs:
        if not os.access(db, os.R_OK):      # unreadable: skipped silently (spec 04 section 7), -v only
            ctx.note(f"vscode: {_tilde(db, home)} is not readable; skipped")
            continue
        info["files"].append(str(db))
        try:
            entries = _vscdb_recents(sqlite3, db)
        except (sqlite3.Error, ValueError, UnicodeError) as e:
            ctx.warn(f"vscode: could not read recents ({_first_line(str(e))})")
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            ws = e.get("workspace")
            uris = [e.get("folderUri"), e.get("fileUri"), ws.get("configPath") if isinstance(ws, dict) else None]
            uris = [u for u in uris if isinstance(u, str)]
            if any(u.startswith("vscode-remote:") for u in uris):
                info["remote_entries"] += 1
                continue
            folder = e.get("folderUri")
            if not isinstance(folder, str):
                continue
            sp = urlsplit(folder)
            if sp.scheme != "file" or sp.netloc not in ("", "localhost"):
                continue
            info["entries"] += 1
            paths.append(Path(unquote(sp.path)))
    return info, paths


def _jetbrains(home: Path, ctx: _Ctx) -> Tuple[dict, List[Path]]:
    info = {"files": [], "entries": 0}
    paths: List[Path] = []
    xmls: List[Path] = []
    for base in (home / ".config/JetBrains", home / "Library/Application Support/JetBrains"):
        if base.is_dir():
            xmls += sorted(base.glob("*/options/recentProjects.xml"))
    if not xmls:
        ctx.note("jetbrains: recentProjects.xml not found")
        return info, paths
    import xml.etree.ElementTree as ET
    seen = set()
    for f in xmls:
        info["files"].append(str(f))
        try:
            tree = ET.parse(str(f))
        except (ET.ParseError, OSError) as e:
            ctx.warn(f"jetbrains: could not read {_tilde(f, home)} ({_first_line(str(e))})")
            continue
        found = [el.get("key") for el in tree.iter("entry")]
        for opt in tree.iter("option"):
            if opt.get("name") == "recentPaths":
                found += [sub.get("value") for sub in opt.iter("option") if sub is not opt]
        for raw in found:
            if not isinstance(raw, str) or not raw.startswith(("$USER_HOME$", "/")):
                continue
            p = os.path.normpath(raw.replace("$USER_HOME$", str(home), 1))
            if p not in seen:
                seen.add(p)
                paths.append(Path(p))
    info["entries"] = len(paths)
    return info, paths


def _ssh_words(value: str) -> List[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def _ssh(home: Path, ctx: _Ctx) -> Tuple[dict, List[dict]]:
    """Host entries of ~/.ssh/config, its Include files (depth 1) and ~/.ssh/config.d/*; the gitpro
    block is skipped. Only Host, HostName, User, Port, IdentityFile and Include are looked at."""
    sshdir = home / ".ssh"
    read: List[Path] = []
    by_alias: Dict[str, dict] = {}

    def glob_ssh(pattern: str) -> List[Path]:
        import glob
        p = os.path.expanduser(pattern) if pattern.startswith("~") else pattern
        if pattern.startswith("~/"):
            p = str(home) + pattern[1:]
        if not os.path.isabs(p):
            p = str(sshdir / p)
        return [Path(x) for x in sorted(glob.glob(p)) if os.path.isfile(x)]

    def parse(path: Path, top: bool) -> None:
        if path in read or _not_ssh_config(path):
            return
        try:
            if not path.is_file() or path.stat().st_size > 1_000_000:
                return
            with open(path, "rb") as f:
                head = f.read(64)
                if _KEY_HEAD.match(head):      # a key after all: stop at its first line
                    ctx.note(f"ssh: {_tilde(path, home)} looks like a key; not read")
                    return
                text = (head + f.read()).decode("utf-8", "replace")
        except OSError:
            return
        read.append(path)
        current: Optional[List[dict]] = None
        managed = False
        for raw in text.splitlines():
            line = raw.strip()
            m = _BLOCK_MARK.match(line)
            if m:
                managed = m[1] == "BEGIN"
                current = None
                continue
            if managed or not line or line.startswith("#"):
                continue
            m = re.match(r"^(\S+?)(?:\s*=\s*|\s+)(.*)$", line)
            if not m:
                continue
            key, value = m[1].lower(), m[2].strip()
            if key == "include":
                if top:
                    for pat in _ssh_words(value):
                        for inc in glob_ssh(pat):
                            parse(inc, False)
                continue
            if key == "match":
                current = None
            elif key == "host":
                current = []
                for pat in _ssh_words(value):
                    if any(c in pat for c in "*?!") or _CONTROL.search(pat):
                        continue
                    e = by_alias.setdefault(pat.lower(), {"alias": pat, "file": str(path)})
                    current.append(e)
            elif current and key in ("hostname", "user", "port", "identityfile"):
                words = _ssh_words(value)
                if words:
                    for e in current:
                        e.setdefault(key, words[0])   # ssh: the first value obtained wins

    parse(sshdir / "config", True)
    if (sshdir / "config.d").is_dir():
        for p in sorted((sshdir / "config.d").iterdir()):
            parse(p, False)
    if not read:
        ctx.note("ssh: ~/.ssh/config not found")
    entries = []
    for e in by_alias.values():
        hn = e.get("hostname")
        if not hn or "%" in hn or hn.lower() == e["alias"].lower() or not HOST_RE.match(hn):
            continue
        port = e.get("port")
        entry = {
            "alias": e["alias"], "hostname": hn, "user": e.get("user"),
            "port": int(port) if port and port.isdigit() and 0 < int(port) < 65536 else None,
            "key": e.get("identityfile"), "file": e["file"], "pub_email": None, "same_forge": False}
        alias, hn_l = e["alias"].lower(), hn.lower()
        if _is_forge(alias) or hn_l in SAME_FORGE:
            # `Host github.com / HostName ssh.github.com / Port 443`: the forge itself reached another
            # way, not an identity alias. Remotes keep (or get) the forge's host; no ssh_alias/ssh_port.
            ctx.note(f"ssh  {_c(e['alias'])} → {hn}: the forge itself (another port or address); not an alias")
            if _is_forge(alias):
                continue
            entry.update(hostname=SAME_FORGE[hn_l], same_forge=True)
        else:
            entry["pub_email"] = _pub_email(e.get("identityfile"), home)
        entries.append(entry)
    return {"files": [str(p) for p in read], "aliases": 0}, entries


def _not_ssh_config(path: Path) -> bool:
    """Files an `Include ~/.ssh/*` or config.d may name that are keys or host lists, never configs."""
    name = path.name
    return (name.startswith("id_") or name.endswith((".pub", ".pem", ".key", ".ppk", ".p12", ".pfx"))
            or name.startswith(("known_hosts", "authorized_keys")) or path.with_name(name + ".pub").exists())


_KEY_HEAD = re.compile(rb"^\s*(-----BEGIN|PuTTY-User-Key-File|openssh-key-v1)")
SAME_FORGE = {"ssh.github.com": "github.com", "altssh.gitlab.com": "gitlab.com"}


def _pub_email(key: Optional[str], home: Path) -> Optional[str]:
    """The email in the comment of <IdentityFile>.pub (never the private key itself)."""
    if not key or "%" in key or _CONTROL.search(key):
        return None
    p = str(home) + key[1:] if key.startswith("~/") else key
    priv = Path(p) if os.path.isabs(p) else home / p
    pub = priv.with_name(priv.name + ".pub")
    try:
        if not pub.is_file() or pub.stat().st_size > 16384:
            return None
        words = pub.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0].split()
    except OSError:
        return None
    for w in words[2:]:
        if _usable_email(w):
            return w
    return None


def _git(*args: str, cwd: Path) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, errors="replace", timeout=TIMEOUT, env=_env())
    except (OSError, subprocess.SubprocessError):
        return None


def _gitdir_root(pattern: str, home: Path, origin: Optional[Path]) -> Optional[str]:
    if pattern.startswith("~/"):
        pattern = str(home) + pattern[1:]
    elif pattern.startswith("./") and origin is not None:
        pattern = str(origin.parent) + pattern[1:]
    elif not pattern.startswith("/"):
        return None          # "**/x": anywhere
    m = _GLOB.search(pattern)
    root = pattern.rstrip("/") if m is None else pattern[:m.start()].rsplit("/", 1)[0]
    root = os.path.normpath(root) if root else ""
    return root if root not in ("", "/") and not _CONTROL.search(root) else None


def _pattern_owner(pattern: str) -> Tuple[Optional[str], Optional[str]]:
    """(host, owner) of a hasconfig:remote.*.url: pattern such as git@gitlab.com:grp/**."""
    from .resolve import parse_url
    host, owner, repo = parse_url(pattern)
    if not host or not HOST_RE.match(host):
        return None, None
    segs = (owner.split("/") if owner else []) + ([repo] if repo else [])
    lead = []
    for s in segs:
        if _GLOB.search(s):
            break
        lead.append(s)
    if len(lead) == len(segs):
        lead = lead[:-1]
    owner = "/".join(lead) or None
    return host.lower(), owner


HASCONFIG = "hasconfig:remote.*.url:"


def _redact_pattern(pattern: str) -> Optional[str]:
    """A hasconfig URL pattern without its userinfo (an scp user that is a plain name such as git is
    kept); None when it looks like it holds a token."""
    if _TOKEN_RE.search(pattern):
        return None
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/]*)(.*)$", pattern, re.S)
    if m:
        out = m[1] + m[2].rpartition("@")[2] + m[3]
    else:
        authority = pattern.split("/", 1)[0]
        at = authority.rfind("@")
        user = authority[:at]
        out = pattern if at < 0 else (f"{user}@" if ID_RE.match(user) else "") + pattern[at + 1:]
    return None if _TOKEN_RE.search(out) else _c(out)


def _includeif(home: Path, ctx: _Ctx) -> List[dict]:
    if not home.is_dir():
        ctx.note("includeif: no home directory; skipped")
        return []
    r = _git("config", "--global", "-z", "--show-origin", "--get-regexp", r"^includeif\.", cwd=home)
    if r is None:
        ctx.note("includeif: could not run git; skipped")
        return []
    if r.returncode not in (0, 1):
        ctx.warn(f"includeif: could not read the global gitconfig ({_first_line(r.stderr)})")
        return []
    rules = []
    toks = r.stdout.split("\0")
    for origin, entry in zip(toks[0::2], toks[1::2]):
        key, _, value = entry.partition("\n")
        if not key.endswith(".path") or _TOKEN_RE.search(key):
            continue
        cond = key[len("includeif."):-len(".path")]
        if cond.startswith(HASCONFIG):
            pattern = _redact_pattern(cond[len(HASCONFIG):])
            if pattern is None:
                ctx.note("includeif: a hasconfig rule whose URL looks like it holds a secret; skipped")
                continue
            cond = HASCONFIG + pattern
        ofile = Path(origin[5:]) if origin.startswith("file:") else None
        value = value.strip()
        if value.startswith("~/"):
            inc: Optional[Path] = home / value[2:]
        elif os.path.isabs(value):
            inc = Path(value)
        elif ofile is not None and value:
            inc = ofile.parent / value
        else:
            inc = None
        rule = {"condition": _CONTROL.sub("?", cond), "kind": None, "root": None, "host": None, "owner": None,
                "file": str(inc) if inc else None, "name": None, "email": None}
        if cond.startswith(("gitdir:", "gitdir/i:")):
            rule["kind"] = "gitdir"
            rule["root"] = _gitdir_root(cond.split(":", 1)[1], home, ofile)
        elif cond.startswith(HASCONFIG):
            rule["kind"] = "hasconfig"
            rule["host"], rule["owner"] = _pattern_owner(cond[len(HASCONFIG):])
        else:
            continue
        if inc is not None and inc.is_file():
            try:
                head = inc.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                head = ""
            if files.is_generated(head):
                ctx.note(f"includeif  {rule['condition']} → {_tilde(inc, home)} (written by gitpro; skipped)")
                continue
            try:
                ctx.include_files.add(str(inc.resolve()))
            except OSError:
                pass
            for k in ("name", "email"):
                g = _git("config", "-f", str(inc), "--get", f"user.{k}", cwd=home)
                if g is not None and g.returncode == 0 and g.stdout.strip():
                    rule[k] = g.stdout.strip()
        who = " ".join(x for x in (rule["name"], f"<{rule['email']}>" if rule["email"] else None) if x)
        ctx.note(f"includeif  {rule['condition']} → {_tilde(inc, home) if inc else '?'}" + (f"  ({who})" if who else ""))
        rules.append(rule)
    if not rules:
        ctx.note("includeif: no includeIf rules in the global gitconfig")
    return rules


# ---------------------------------------------------------------- online: gh / glab

def _run_cli(argv: List[str], env: Dict[str, str]) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                              timeout=TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def _account(host: str, cli: str, login: str, active: bool) -> Optional[dict]:
    if not (isinstance(host, str) and HOST_RE.match(host) and isinstance(login, str) and ID_RE.match(login)):
        return None
    return {"host": host.lower(), "cli": cli, "login": login, "emails": [], "verified": False, "_active": active}


def _parse_logins(text: str, cli: str) -> List[dict]:
    out: List[dict] = []
    for line in text.splitlines():
        m = re.search(r"Logged in to (\S+) (?:account|as) (\S+)", line)
        if m:
            acc = _account(m[1], cli, m[2], cli == "glab")
            if acc:
                acc["_active"] = None if cli == "gh" else True
                out.append(acc)
            continue
        m = re.search(r"Active account: (true|false)", line)
        if m and out:
            out[-1]["_active"] = m[1] == "true"
    for a in out:   # older gh prints no "Active account" line: the first login per host is the active one
        if a["_active"] is None:
            a["_active"] = not any(b["_active"] for b in out if b["host"] == a["host"] and b is not a)
    return out


def _gh_logins(env: Dict[str, str]) -> List[dict]:
    r = _run_cli(["gh", "auth", "status", "--json", "hosts"], env)
    if r is not None and r.returncode == 0:
        try:
            hosts = json.loads(r.stdout)["hosts"]
        except (ValueError, KeyError, TypeError):
            hosts = None
        if isinstance(hosts, dict):
            out = []
            for host, items in hosts.items():
                for it in items if isinstance(items, list) else []:
                    if isinstance(it, dict) and it.get("state", "success") == "success":
                        acc = _account(host, "gh", it.get("login"), bool(it.get("active")))
                        if acc:
                            out.append(acc)
            return out
    r = _run_cli(["gh", "auth", "status"], env)   # gh before --json: parse the text form
    return _parse_logins(r.stdout + "\n" + r.stderr, "gh") if r is not None else []


def _gh_emails(acc: dict, env: Dict[str, str], ctx: _Ctx) -> None:
    r = _run_cli(["gh", "api", "user/emails", "--hostname", acc["host"]], env)
    emails = None
    if r is not None and r.returncode == 0:
        try:
            data = json.loads(r.stdout)
            emails = [e["email"] for e in data if isinstance(e, dict) and e.get("verified") and _usable_email(e.get("email"))]
        except (ValueError, KeyError, TypeError):
            emails = None
    if emails is None:
        detail = _first_line(r.stderr) if r is not None else "gh failed"
        ctx.warn(f"gh: could not read the verified emails of {acc['login']} on {acc['host']} ({detail}); "
                 f"to allow it: gh auth refresh -h {acc['host']} -s user:email")
        return
    acc["emails"] = emails
    acc["verified"] = bool(emails)


def _online(sources: List[str], ctx: _Ctx, quiet: bool) -> Tuple[List[dict], Dict[str, Optional[dict]]]:
    from . import forge
    accounts: List[dict] = []
    info: Dict[str, Optional[dict]] = {"gh": None, "glab": None}
    gh_env = dict(_env(forge._GH_TOKEN_VARS), GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    if "gh" in sources and forge.available("gh"):
        accs = _gh_logins(gh_env)
        info["gh"] = {"logins": len(accs)}
        if not accs:
            ctx.warn("gh has no login; account discovery skipped")
        accounts += accs
    if "glab" in sources and forge.available("glab"):
        r = _run_cli(["glab", "auth", "status"], dict(_env(forge._GLAB_TOKEN_VARS), NO_PROMPT="1", NO_COLOR="1"))
        accs = _parse_logins(r.stdout + "\n" + r.stderr, "glab") if r is not None else []
        info["glab"] = {"logins": len(accs)}
        if not accs:
            ctx.warn("glab has no login; account discovery skipped")
        accounts += accs
    if accounts:
        what = "for your logins" + (" and verified emails" if any(a["cli"] == "gh" for a in accounts) else "")
        target = sys.stderr if quiet else sys.stdout
        with contextlib.redirect_stdout(target):
            forge.announce(what, [forge.Account(a["host"], a["cli"], a["login"]) for a in accounts],
                           sends="only what gh/glab send to sign in (they use their own stored login)")
        for a in accounts:
            if a["cli"] == "gh" and a["_active"]:
                _gh_emails(a, gh_env, ctx)
    for a in accounts:
        ctx.note(f"{a['cli']}  {a['host']}  {a['login']}" + (f"  verified: {', '.join(a['emails'])}" if a["emails"] else ""))
    return accounts, info


# ---------------------------------------------------------------- the existing config

def _expand(r: str, home: Path) -> str:
    if r == "~":
        return str(home)
    return os.path.normpath(str(home) + r[1:] if r.startswith("~/") else r)


def _load_config(ctx: _Ctx, explicit: Optional[Path], find: bool, home: Path):
    info = {"path": None, "loaded": False, "ids": [], "emails": [], "scan_roots": None, "exclude": [], "aliases": []}
    if not find and explicit is None:
        return info, None
    try:
        path, how = config.find(explicit, home)
    except config.ConfigError:
        return info, None
    info["path"] = str(path)
    try:
        raw = config._read(path)
    except config.ConfigError as e:
        ctx.warn(f"{e}; its identities and [scan] roots are ignored")
        return info, None
    try:
        sc = config.load_scan(path)
        if isinstance(raw.get("scan"), dict) and "roots" in raw["scan"]:
            info["scan_roots"] = list(sc.roots)
        info["exclude"] = list(sc.exclude)
    except config.ConfigError as e:
        ctx.warn(f"{e}; its [scan] roots are ignored")
    try:
        cfg = config.load_all(path, how)
    except config.ConfigError as e:
        ctx.warn(f"{e}; its identities are ignored")
        return info, None
    info.update(loaded=True, ids=[i.id for i in cfg.identities],
                emails=sorted({i.email.lower() for i in cfg.identities if i.email}),
                aliases=[i.ssh_alias for i in cfg.identities if i.ssh_alias] + list(cfg.default.ssh_hosts))
    return info, cfg


def _config_id(cfg, host: str, owner: str) -> Optional[str]:
    if cfg is None:
        return None
    from .resolve import match_url
    for url in (f"git@{host}:{owner}/x.git", f"https://{host}/{owner}/x.git", f"ssh://git@{host}/{owner}/x.git"):
        ident, _ = match_url(url, cfg)
        if ident is not None:
            return ident.id
    return None


# ---------------------------------------------------------------- collect

def _collapse(paths) -> List[str]:
    out: List[str] = []
    for p in sorted(set(paths), key=lambda s: (s.rstrip("/").count("/"), s)):
        if not any(_under(p, q) for q in out):
            out.append(p)
    return sorted(out)


def collect(sources, *, history: bool = False, online: bool = False, home: Optional[Path] = None,
            config_path: Optional[Path] = None, find_config: bool = True, quiet: bool = False,
            roots: Optional[List[Path]] = None, reflog: bool = False, budget: float = BUDGET) -> dict:
    """Read the sources and join them. Returns the --json body (sources, hosts, aliases, roots,
    accounts, rules, config, toml, warnings) plus "findings" (the -v lines) and "history_hint".
    history/online gate their sources whatever `sources` says. quiet: the online announcement goes
    to stderr (for --json). roots: look for repos under exactly these (editor folders only when under
    one of them), at any depth, for up to `budget` seconds; reflog: a repo whose name and email are not
    set in it gets the identity of the commits made in it (setup, spec 22 §5.3)."""
    home = Path(home) if home else Path.home()
    sources = [s for s in SOURCES if s in sources
               and (s != "history" or history) and (s not in ONLINE_SOURCES or online)]
    ctx = _Ctx(home)
    ctx.reflog = reflog
    only = [os.path.normpath(str(r)) for r in roots] if roots is not None else None
    cfg_info, cfg = _load_config(ctx, config_path, find_config, home)
    srcs: Dict[str, Optional[dict]] = {s: None for s in SOURCES}

    ssh_entries: List[dict] = []
    if "ssh" in sources:
        srcs["ssh"], ssh_entries = _ssh(home, ctx)
        ctx.ssh_map = {e["alias"].lower(): e for e in ssh_entries}
    rules: List[dict] = []
    if "includeif" in sources:
        rules = _includeif(home, ctx)
        srcs["includeif"] = {"rules": len(rules)}
        for r in rules:
            if r["root"]:
                ctx.add_root(r["root"])
    if "history" in sources:
        srcs["history"] = _history(home, ctx)
    editor: List[Tuple[str, Path]] = []
    if "vscode" in sources:
        srcs["vscode"], paths = _vscode(home, ctx)
        editor += [("vscode", p) for p in paths]
    if "jetbrains" in sources:
        srcs["jetbrains"], paths = _jetbrains(home, ctx)
        editor += [("jetbrains", p) for p in paths]
    for src, p in editor:
        if only is not None and not any(_under(os.path.normpath(str(p)), r) for r in only):
            continue
        try:
            is_repo = (p / ".git").exists()
        except OSError:
            is_repo = False
        if is_repo:
            ctx.add_repo(src, p)
            ctx.add_root(p.parent)

    cfg_roots = [_expand(r, home) for r in (cfg_info["scan_roots"] or [])]
    if "repos" in sources:
        from . import scan
        walk = [r for r in _collapse(ctx.roots + cfg_roots) if os.path.isdir(r)]
        exclude = list(cfg_info["exclude"])
        depth: Optional[int] = MAX_DEPTH
        if only is not None:
            walk, depth = [r for r in _collapse(only) if os.path.isdir(r)], None
            if str(home) in walk:
                exclude += HOME_SKIP
        elif not walk and cfg_info["scan_roots"] is None and home.is_dir():
            # nothing else says where repos live: look under ~ (hidden dirs and node_modules are
            # skipped by scan.find; these caches too)
            walk = [str(home)]
            exclude += HOME_SKIP
            ctx.note(f"repos: no roots found; looked under ~ (depth {MAX_DEPTH})")
        deadline = time.monotonic() + budget     # for the walk, then again for reading what it found
        found = scan.find([Path(r) for r in walk], exclude=exclude, max_depth=depth, deadline=deadline)
        srcs["repos"] = {"roots": walk, "count": len(found)}
        if time.monotonic() > deadline:
            ctx.warn(f"repos: stopped looking after {budget:g} s; reading the {len(found)} found by then")
        deadline = time.monotonic() + budget
        for n, p in enumerate(found):
            if time.monotonic() > deadline:
                ctx.warn(f"repos: stopped after {budget:g} s ({n} of {len(found)} repos looked at)")
                break
            ctx.add_repo("repos", p)

    accounts: List[dict] = []
    if online and any(s in sources for s in ONLINE_SOURCES):
        accounts, info = _online(sources, ctx, quiet)
        srcs.update(info)

    d = _join(ctx, rules, accounts, ssh_entries, cfg, cfg_info, cfg_roots)
    d["sources"] = srcs
    if srcs["ssh"] is not None:
        srcs["ssh"]["aliases"] = len(d["aliases"])
    d["history_hint"] = None if "history" in sources else [_tilde(p, home) for p, _ in _history_files(home)]
    d["home"] = str(home)
    if reflog:
        d["committed"] = ctx.committed
    d["toml"] = render_toml(d)
    return d


def _join(ctx: _Ctx, rules, accounts, ssh_entries, cfg, cfg_info, cfg_roots) -> dict:
    for r in rules:
        if r["kind"] == "hasconfig" and r["host"] and r["owner"]:
            rec = ctx.rec(r["host"], r["owner"])
            rec["rule"] = rec["rule"] or r
    for rec in ctx.owners.values():
        if rec["rule"] is None:
            rec["rule"] = next((r for r in rules if r["kind"] == "gitdir" and r["root"]
                                and any(_under(p, r["root"]) for p in rec["paths"])), None)
    seen = {h for h, _ in ctx.owners} | {a["host"] for a in accounts}
    def shown(key, rec) -> str:
        return rec["spellings"].most_common(1)[0][0] if rec["spellings"] else key[1]

    aliases = []
    for e in sorted(ssh_entries, key=lambda e: e["alias"]):
        hn = e["hostname"].lower()
        if not e["same_forge"] and (_is_forge(hn) or hn in seen):
            aliases.append({"alias": e["alias"], "host": hn, "user": e["user"], "port": e["port"], "key": e["key"],
                            "file": e["file"], "pub_email": e["pub_email"],
                            "used_by": sorted(shown(k, rec) for k, rec in ctx.owners.items()
                                              if rec["aliases"].get(e["alias"]))})
            ctx.note(f"ssh  {e['alias']} → {hn}" + (f"  key {e['key']}" if e["key"] else "")
                     + f"  ({_tilde(e['file'], ctx.home)})")

    by_host: Dict[str, List[dict]] = {}
    for k, rec in ctx.owners.items():
        h, o = k[0], shown(k, rec)
        cid = _config_id(cfg, h, o)
        r = rec["rule"]
        by_host.setdefault(h, []).append({
            "owner": o, "repos": len(rec["repos"]), "emails": dict(rec["emails"].most_common()),
            "names": dict(rec["names"].most_common()), "in_config": cid is not None, "config_id": cid,
            "aliases": dict(rec["aliases"]), "sources": sorted(rec["sources"]),
            "rule": {"condition": r["condition"], "name": r["name"], "email": r["email"]} if r else None,
            **({"people": [[n, e, c] for (n, e), c in rec["people"].most_common()]} if ctx.reflog else {})})
    hosts = [{"host": h, "owners": sorted(os_, key=lambda o: (-o["repos"], o["owner"]))} for h, os_ in by_host.items()]
    hosts.sort(key=lambda h: (-sum(o["repos"] for o in h["owners"]), h["host"]))

    real = list(ctx.repo_paths.values())
    roots = [{"path": r, "repos": sum(1 for p in real if _under(p, r)), "exists": True}
             for r in _collapse(ctx.roots + cfg_roots) if os.path.isdir(r)]
    accs = [{k: v for k, v in a.items() if not k.startswith("_")} for a in accounts]
    return {"hosts": hosts, "aliases": aliases, "roots": roots, "accounts": accs, "rules": rules,
            "config": cfg_info, "warnings": ctx.warnings, "findings": ctx.findings}


# ---------------------------------------------------------------- TOML

def _s(v: str) -> str:
    """v as a TOML basic string. Beyond JSON's escapes, DEL, the C1 controls and U+2028/U+2029 are
    escaped too, so no editor or splitlines() sees a line break inside the value."""
    return _TOML_ESCAPE.sub(lambda m: f"\\u{ord(m[0]):04x}", json.dumps(v, ensure_ascii=False))


def _c(text: str) -> str:
    """text for a comment or the terminal: control characters (and Unicode line breaks) as ?."""
    return _SHOW_CONTROL.sub("?", str(text))


def _new_id(owner: str, host: str, taken: set) -> str:
    """scan.derive_id, but always a valid id (never "-")."""
    from .scan import derive_id
    base = re.sub(r"^[^A-Za-z0-9]+", "", re.sub(r"[^A-Za-z0-9._-]", "-", owner)) or host.split(".")[0] or "id"
    base = base if ID_RE.match(base) else "id"
    return derive_id(base, host, taken)


def _line(s: str, comment: Optional[str] = None) -> str:
    return s if not comment else f"{s.ljust(32)} # {_c(comment)}"


def _top(counts, ok) -> Tuple[Optional[str], int]:
    best = [(n, v) for v, n in (counts or {}).items() if ok(v)]
    best.sort(key=lambda x: -x[0])
    return (best[0][1], best[0][0]) if best else (None, 0)


def _template() -> List[str]:
    return ["[[identity]]",
            _line('id = "work"', "fill in: a short name for this identity"),
            _line('name = ""', "fill in"),
            _line('email = ""', "fill in"),
            _line('host = ""', "fill in, e.g. github.com"),
            _line("owners = []", "fill in: the users, orgs or groups of this identity"),
            ""]


def render_toml(d: dict) -> str:
    """The snippet to paste: [scan] roots (or a comment when the config already sets them), one
    [[identity]] per (host, email), notes as comments. Every value is a TOML string escaped with
    JSON rules; values gitpro's config check would reject are left blank with a comment."""
    cfgi = d.get("config") or {}
    taken = set(cfgi.get("ids") or ())
    taken_aliases = set(cfgi.get("aliases") or ())
    cfg_emails = set(cfgi.get("emails") or ())
    home = Path(d["home"]) if d.get("home") else Path.home()
    out: List[str] = []
    notes: List[str] = []

    roots = []
    for r in d.get("roots") or []:
        shown = _tilde(r["path"], home)
        if config.scan_root_error(shown) is None:
            roots.append(shown)
    scan_roots = cfgi.get("scan_roots")
    if scan_roots is None:
        out += ["[scan]", "roots = [" + ", ".join(_s(r) for r in roots or ["~"]) + "]", ""]
    else:
        have = [_expand(r, home) for r in scan_roots]
        new = [r for r in roots if not any(_under(_expand(r, home), h) for h in have)]
        if new:
            out += ["# add to [scan] roots: " + _c(", ".join(_s(r) for r in new)), ""]

    blocks: List[dict] = []
    index: Dict[Tuple[str, str], dict] = {}
    for h in d.get("hosts") or []:
        host = h["host"]
        if not _usable_host(host):
            notes.append(f"{host}: not a host name gitpro accepts (or an example placeholder); skipped")
            continue
        for o in h.get("owners") or []:
            where = f"{host}/{o['owner']}"
            if o.get("in_config"):
                notes.append(f"{where}: already in config" + (f" (identity {o['config_id']})" if o.get("config_id") else ""))
                continue
            if not OWNER_RE.match(o["owner"]):
                notes.append(f"{where}: owners can hold only A-Z a-z 0-9 . _ - / and a leading ~; add it by hand if it is yours")
                continue
            rule = o.get("rule") or {}
            email = _top(o.get("emails"), _usable_email)[0]
            if email is None and _usable_email(rule.get("email")):
                email = rule["email"]
            verified = None
            if email is None:     # a verified email of the gh login named like the owner
                verified = next((a for a in d.get("accounts") or [] if a["host"] == host
                                 and a["login"].lower() == o["owner"].lower()
                                 and any(_usable_email(e) for e in a.get("emails") or ())), None)
                if verified is not None:
                    email = next(e for e in verified["emails"] if _usable_email(e))
            key = (host, email or "")
            if key not in index:
                index[key] = {"host": host, "email": email, "owners": [],
                              "verified": f"{verified['cli']} login {verified['login']}" if verified else None}
                blocks.append(index[key])
            index[key]["owners"].append(o)

    aliases = [a for a in d.get("aliases") or [] if a["alias"] not in taken_aliases]
    assigned: set = set()
    accounts = d.get("accounts") or []
    used_accounts: set = set()
    body: List[str] = []
    for b in blocks:
        owners = b["owners"]
        names: Counter = Counter()
        for o in owners:
            for n, c in (o.get("names") or {}).items():
                if _usable_name(n):
                    names[n] += c
        rule = next((o["rule"] for o in owners if o.get("rule")), None) or {}
        ident = _new_id(owners[0]["owner"], b["host"], taken)
        taken.add(ident)
        lines = ["[[identity]]", f"id = {_s(ident)}"]
        if names:
            name, n = names.most_common(1)[0]
            others = [x for x in names if x != name][:2]
            lines.append(_line(f"name = {_s(name)}", f"seen in {_plural(n, 'repo')}"
                               + (f"; also seen: {', '.join(others)}" if others else "")))
        elif _usable_name(rule.get("name")):
            lines.append(_line(f"name = {_s(rule['name'])}", f"from includeIf {rule['condition']}"))
        else:
            lines.append(_line('name = ""', "not seen; fill in"))
        if b["email"]:
            n = sum((o.get("emails") or {}).get(b["email"], 0) for o in owners)
            lines.append(_line(f"email = {_s(b['email'])}", f"seen in {_plural(n, 'repo')}" if n
                               else f"verified on the {b['verified']}" if b.get("verified")
                               else f"from includeIf {rule.get('condition', '')}"))
        else:
            lines.append(_line('email = ""', "fill in"))
        lines.append(f"host = {_s(b['host'])}")
        check = [o for o in owners if o.get("repos", 0) <= 1]
        lines.append(_line("owners = [" + ", ".join(_s(o["owner"]) for o in owners) + "]",
                           ", ".join(f"{o['owner']}: {_plural(o.get('repos', 0), 'repo')}" for o in check)
                           + ", check" if check else None))

        mine = {o["owner"] for o in owners}
        cands = [a for a in aliases if a["host"] == b["host"] and a["alias"] not in assigned]
        scored = sorted(((len(mine & set(a.get("used_by") or ())), a) for a in cands), key=lambda x: -x[0])
        pick = scored[0][1] if scored and scored[0][0] > 0 else None
        if pick is None and b["email"]:
            pick = next((a for a in cands if (a.get("pub_email") or "").lower() == b["email"].lower()), None)
        if pick is not None:
            assigned.add(pick["alias"])
            lines += _alias_lines(pick, b["host"], home)

        for cli in ("gh", "glab"):
            accs = [a for a in accounts if a["host"] == b["host"] and a["cli"] == cli
                    and (cli, a["host"], a["login"]) not in used_accounts and ID_RE.match(a["login"])]
            acc = next((a for a in accs if b["email"] and b["email"].lower() in {e.lower() for e in a["emails"]}), None)
            why = "has this email verified"
            if acc is None:
                acc = next((a for a in accs if a["login"].lower() in {m.lower() for m in mine}), None)
                why = "login matches an owner"
            if acc is not None:
                used_accounts.add((cli, acc["host"], acc["login"]))
                lines.append(_line(f"{cli}_account = {_s(acc['login'])}", f"{cli} login; {why}"))
        body += lines + [""]
        if b["email"]:
            cfg_emails.add(b["email"].lower())

    for r in d.get("rules") or []:
        if r.get("kind") != "gitdir" or not _usable_email(r.get("email")) or r["email"].lower() in cfg_emails:
            continue
        cfg_emails.add(r["email"].lower())
        base = Path(r["root"]).name if r.get("root") else "includeif"
        ident = _new_id(base or "includeif", "includeif", taken)
        taken.add(ident)
        body += [f"# from includeIf {_c(r['condition'])}" + (f" in {_c(_tilde(r['file'], home))}" if r.get("file") else "")
                 + ": no repo seen, fill in host and owners",
                 "[[identity]]", f"id = {_s(ident)}",
                 _line(f"name = {_s(r['name'])}", None) if _usable_name(r.get("name")) else _line('name = ""', "fill in"),
                 f"email = {_s(r['email'])}",
                 _line('host = ""', "fill in, e.g. github.com"),
                 _line("owners = []", "fill in: the users, orgs or groups"), ""]

    for a in aliases:
        if a["alias"] not in assigned:
            notes.append(f"ssh alias {a['alias']} → {a['host']}" + (f" (key {a['key']})" if a.get("key") else "")
                         + ": no repo seen using it; add ssh_alias and ssh_key to the identity it belongs to")
    out += [f"# {_c(n)}" for n in notes] + ([""] if notes else [])
    out += body or _template()
    return "\n".join(out).rstrip("\n") + "\n"


def _alias_lines(a: dict, host: str, home: Optional[Path] = None) -> List[str]:
    key = a.get("key")
    alias_ok = bool(HOST_RE.match(a["alias"])) and a["alias"] != host
    key_ok = isinstance(key, str) and bool(PATH_RE.match(key)) and ".." not in key.split("/")
    src = f"from {_tilde(a['file'], home)}" if a.get("file") else None
    if not (alias_ok and key_ok):
        why = "needs a key path gitpro accepts (~/... or /..., no spaces)" if alias_ok else "not a name gitpro accepts"
        lines = [_line(f"# ssh_alias = {_s(a['alias'])}", why)]
        if key:
            lines.append(f"# ssh_key = {_s(key)}")
        return lines
    lines = [_line(f"ssh_alias = {_s(a['alias'])}", src), f"ssh_key = {_s(key)}"]
    if a.get("user") and a["user"] != "git" and ID_RE.match(a["user"]):
        lines.append(f"ssh_user = {_s(a['user'])}")
    if a.get("port") and a["port"] != 22:
        lines.append(f"ssh_port = {int(a['port'])}")
    return lines


# ---------------------------------------------------------------- text output

def _summary(d: dict) -> List[str]:
    s = d["sources"]
    home = Path(d["home"]) if d.get("home") else None
    parts = []
    if s.get("vscode") is not None:
        v = s["vscode"]
        parts.append(f"vscode ({_plural(v['entries'], 'recent')}"
                     + (f", {_plural(v['remote_entries'], 'remote entry', 'remote entries')} skipped"
                        if v["remote_entries"] else "") + ")")
    if s.get("jetbrains") is not None:
        parts.append(f"jetbrains ({_plural(s['jetbrains']['entries'], 'recent')})")
    if s.get("ssh") is not None:
        parts.append(f"ssh ({_plural(s['ssh']['aliases'], 'alias', 'aliases')})")
    if s.get("includeif") is not None:
        parts.append(f"includeif ({_plural(s['includeif']['rules'], 'rule')})")
    if s.get("repos") is not None:
        parts.append(f"repos ({s['repos']['count']})")
    for cli in ("gh", "glab"):
        if s.get(cli) is not None:
            parts.append(f"{cli} ({_plural(s[cli]['logins'], 'login')})")
    lines = ["discovered from: " + ", ".join(parts)] if parts else []
    h = s.get("history")
    if h is not None:
        if not h["files"]:
            lines.append("history: no shell history file found")
        else:
            lines.append(f"history: {_plural(h['lines'], 'clone line')} in {', '.join(_tilde(f, home) for f in h['files'])}"
                         + (f" ({h['secrets_skipped']} skipped: looked like a secret)" if h["secrets_skipped"] else ""))
            if h.get("truncated"):
                lines.append(f"history: read only the newest {HISTORY_CAP} lines of "
                             + ", ".join(_tilde(f, home) for f in h["truncated"]))
    elif d.get("history_hint") is not None:
        what = ", ".join(d["history_hint"]) or "your shell history"
        lines.append(f"history: not read (add --history to read {what} for git clone lines)")
    return lines


def _nothing(d: dict) -> bool:
    return not (d["hosts"] or d["aliases"] or d["rules"] or d["accounts"] or d["roots"])


def print_text(d: dict, verbose: bool, final: str, summary: bool = True) -> None:
    home = Path(d["home"]) if d.get("home") else None
    if summary:
        for ln in _summary(d):
            print(ln)
    if verbose and d["findings"]:
        print("\nfindings:")
        for ln in d["findings"]:
            print(f"  {ln}")
    print()
    if _nothing(d):
        print("nothing discovered; roots: ~")
    else:
        if d["hosts"]:
            print("hosts and owners seen:")
            w = max(len(h["host"]) for h in d["hosts"])
            for h in d["hosts"]:
                items = []
                for i, o in enumerate(h["owners"]):
                    n = f"{_plural(o['repos'], 'repo')}" if i == 0 else str(o["repos"])
                    items.append(f"{o['owner']} ({n}{', in config' if o['in_config'] else ''})")
                print(f"  {h['host']:<{w}}  " + "  ".join(items))
        if d["aliases"]:
            print("ssh aliases:")
            for a in d["aliases"]:
                extra = "".join(f"  {k} {_c(a[k])}" for k in ("user", "port") if a.get(k))
                print(f"  {_c(a['alias'])} → {a['host']}" + (f"  key {_c(a['key'])}" if a.get("key") else "") + extra)
        if d["roots"]:
            print("roots:")
            print("  " + "  ".join(f"{_c(_tilde(r['path'], home))} ({_plural(r['repos'], 'repo') if i == 0 else r['repos']})"
                                   for i, r in enumerate(d["roots"])))
        emails: Dict[str, List[str]] = {}
        for h in d["hosts"]:
            for o in h["owners"]:
                for e, n in o["emails"].items():
                    emails.setdefault(e, []).append(f"{h['host']}/{o['owner']} ×{n}")
        if emails:
            print("emails seen in repos:")
            for e, where in emails.items():
                print(f"  {_c(e)}  ({', '.join(where)})")
        if d["accounts"]:
            print("accounts:")
            for a in d["accounts"]:
                print(f"  {a['host']}  {a['cli']}  {a['login']}"
                      + (f"  verified: {', '.join(a['emails'])}" if a["emails"] else ""))
    cfg_path = d["config"].get("path")
    print()
    print(f"# ---- paste into {_tilde(cfg_path, home) if cfg_path else '~/' + config.USER_CONFIG} and fill in the blanks ----")
    print(d["toml"].rstrip("\n"))
    print()
    print(final)


# ---------------------------------------------------------------- commands

def choose_sources(given, history: bool, online: bool) -> Tuple[List[str], Optional[str]]:
    """(sources, error message or None) from --source values (each may be comma-separated)."""
    if not given:
        out = list(DEFAULT_SOURCES)
        if history:
            out.insert(0, "history")
        if online:
            out += list(ONLINE_SOURCES)
        return out, None
    out = []
    for value in given:
        for s in (x.strip() for x in value.split(",")):
            if not s:
                continue
            if s not in SOURCES:
                return [], f"unknown source {s!r} ({', '.join(SOURCES)})"
            if s == "history" and not history:
                return [], "history is read only with --history"
            if s in ONLINE_SOURCES and not online:
                return [], f"{s} is asked only with --online"
            if s not in out:
                out.append(s)
    return out, None


def _no_cli(sources) -> bool:
    from shutil import which
    return any(s in sources for s in ONLINE_SOURCES) and not (which("gh") or which("glab"))


NO_CLI = "--online needs gh or glab on PATH, and neither was found; install one or run without --online"


def cmd_discover(args) -> int:
    from .args import fail
    sources, err = choose_sources(args.source, args.history, args.online)
    if err:
        return fail(err, args)
    if args.online and _no_cli(sources):
        return fail(f"discover {NO_CLI}", args)
    d = collect(sources, history=args.history, online=args.online, home=args.home, config_path=args.config,
                quiet=args.json)
    if args.source:
        d["history_hint"] = None
    if args.json:
        jsonout.emit("discover", {k: d[k] for k in JSON_KEYS})
    else:
        print_text(d, args.verbose, f"→ then: {PROG} plan")
    return 0


def register(sub, common, confirm) -> None:
    from .args import capability
    p = sub.add_parser("discover", parents=[common],
                       help="suggest identities and scan roots from what is on this machine (prints TOML, writes nothing)")
    p.add_argument("--source", action="append", metavar="S[,S...]",
                   help=f"only these sources ({', '.join(SOURCES)})")
    p.add_argument("--history", action="store_true", help="also read git clone lines from your shell history")
    p.add_argument("--online", action="store_true", help="also ask gh/glab for your logins and verified emails")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("-v", "--verbose", action="store_true", help="list each finding with its source")
    p.set_defaults(func=cmd_discover)
    capability("discover")


def extend(subs) -> None:
    from .cli import capability, extend_command
    extend_command(subs, "init", _init_flags, _init_handler)
    capability("init.discover")


def _init_flags(p) -> None:
    p.add_argument("--discover", action="store_true",
                   help="append identities discovered on this machine, commented, to the new config")
    p.add_argument("--history", action="store_true", help="with --discover: also read shell history")
    p.add_argument("--online", action="store_true", help="with --discover: also ask gh/glab for your logins")


def _commented(d: dict) -> str:
    lines = ["", f"# ---- discovered on {date.today().isoformat()} by {PROG} init --discover; uncomment what is right ----"]
    lines += [f"# {ln}" if ln.strip() else "" for ln in d["toml"].rstrip("\n").split("\n")]
    return "\n".join(lines) + "\n"


def _init_handler(args) -> Optional[int]:
    from .args import fail
    if not args.discover:
        if args.history or args.online:
            return fail("--history and --online go with --discover", args)
        return None
    from importlib import resources

    from . import cli
    sources, _ = choose_sources(None, args.history, args.online)
    if args.online and _no_cli(sources):
        return fail(f"init --discover {NO_CLI}", args)
    home = Path.home()
    target = (args.path or home / config.USER_CONFIG).expanduser()
    if target.exists():
        rc = cli.cmd_init(args)
        d = collect(sources, history=args.history, online=args.online, home=home,
                    config_path=target if target.is_file() else None, find_config=target.is_file())
        print_text(d, False, "→ paste what you need")
        return rc
    d = collect(sources, history=args.history, online=args.online, home=home, find_config=False)
    rc = cli.cmd_init(args)
    example = resources.files(__package__).joinpath("example.toml").read_text()
    if cli.preview_only(args):
        print(_commented(d))
        return rc
    try:
        created = rc == 0 and target.read_text() == example
    except OSError:
        created = False
    if not created:    # someone else wrote the file meanwhile: never append to it
        print_text(d, False, "→ paste what you need")
        return rc
    with open(target, "a", encoding="utf-8") as f:
        f.write(_commented(d))
    for ln in _summary(d):
        print(ln)
    n = d["toml"].count("[[identity]]")
    print(f"✓ appended the discovered section ({_plural(n, 'identity block')}, commented) to {target}; "
          "uncomment what is right")
    return rc
