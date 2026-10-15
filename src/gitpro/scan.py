"""Find the repos under some directories, report the identity git uses in each, remove repo-level overrides.

A user.name or user.email in a repo's own .git/config wins over every identity gitpro writes, so a
forgotten one keeps committing with an old address. scan shows them next to what git would use
without them; fix removes them.
"""
from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
import shlex
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import undo
from .config import HOST_RE, OWNER_RE, ConfigError, Scan
from .pin import FILE as PIN_FILE, git

KEYS = ("user.name", "user.email")
SKIP = {"node_modules"}  # and every hidden directory
_GLOB = re.compile(r"[*?[]")  # where an exclude pattern's literal prefix ends
_USER = r"^user\.(name|email)$"
# scp-like (git@host:owner/x), ssh://user@host:port/owner/x, https://host/owner/x
_URL = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?([^/:]+)(?::\d+(?=/))?[:/]([^/]+)")


@dataclass
class Repo:
    path: Path
    remote: str                            # origin's URL, else the first remote's; "" without remotes
    now: dict[str, tuple[str, str]]        # key -> (value, origin) that git resolves
    local: dict[str, str]                  # key -> value set in the repo's own config file
    beneath: dict[str, str]                # key -> value git would resolve without those
    urls: dict[str, str] = field(default_factory=dict)   # remote.<name>.url -> URL
    hooks_path: str = ""                   # the repo's own core.hooksPath ("" when it has none)

    def source(self, key: str = "user.email") -> str:
        """Where the value git uses comes from: 'repo', 'pin', a file path, or '' when unset."""
        if key not in self.now:
            return ""
        if key in self.local:
            return "repo"
        path = Path(self.now[key][1][len("file:"):])
        if path.is_absolute():
            return str(path)
        return "pin" if path.name == PIN_FILE else str(self.path / path)

    def after_fix(self) -> dict[str, str]:
        """name and email once fix removed what it can (a setting nothing else provides stays)."""
        return {k: self.beneath.get(k, self.now[k][0]) for k in KEYS if k in self.now}

    def using(self) -> dict[str, str]:
        return {k: self.now[k][0] for k in KEYS if k in self.now}


def find(roots, *, exclude=(), max_depth: int | None = None, excluded: set | None = None) -> list[Path]:
    """Every directory holding a .git (repo, worktree or submodule), nested ones included.

    Hidden directories and node_modules are always skipped. exclude: patterns as in [scan] exclude
    (see excluded_by); a directory one matches is not entered, and is added to `excluded` when given.
    A root that is itself excluded is skipped. max_depth: how many levels below each root to look
    (None: no limit)."""
    pats = _patterns(exclude)
    found = set()
    for root in roots:
        root = Path(root)
        if pats and _match(str(root), pats):
            continue
        base = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if ".git" in dirnames or ".git" in filenames:
                found.add(Path(dirpath))
            if max_depth is not None and len(Path(dirpath).parts) - base >= max_depth:
                dirnames[:] = []
                continue
            keep = []
            for d in dirnames:
                if d.startswith(".") or d in SKIP:
                    continue
                full = os.path.join(dirpath, d)
                if pats and _match(full, pats):
                    if excluded is not None:
                        excluded.add(Path(full))
                    continue
                keep.append(d)
            dirnames[:] = keep
    return sorted(found)


def _normalise(pattern: str, home: str) -> str:
    """A [scan] exclude pattern as an fnmatch pattern over absolute, resolved paths: ~/ is the home
    directory, a relative pattern matches at any depth (**/ in front), ** is fnmatch's * (which
    crosses /). An absolute pattern's part before its first glob character is resolved (roots are,
    so ~/work/old still matches when ~/work is a symlink) and escaped (a [ in a path is literal)."""
    p = pattern.rstrip("/") or "/"
    if p == "~" or p.startswith("~/"):
        lit, p = home, p[1:]  # the home directory is literal, whatever it contains
    elif p.startswith("/"):
        lit = ""
    else:
        return ("**/" + p).replace("**", "*")
    m = _GLOB.search(p)
    cut = len(p) if m is None else p.rfind("/", 0, m.start())
    head, tail = os.path.realpath(lit + p[:cut] or "/"), p[cut:]
    if head == "/" and tail:
        head = ""
    return glob.escape(head) + tail.replace("**", "*")


def _patterns(exclude) -> list[tuple[str, str]]:
    home = str(Path.home())
    return [(pat, _normalise(pat, home)) for pat in exclude]


def _match(path: str, pats: list[tuple[str, str]]) -> str | None:
    return next((pat for pat, norm in pats if fnmatch.fnmatchcase(path, norm)), None)


def excluded_by(path: Path, exclude) -> str | None:
    """The first exclude pattern (as written) that matches the absolute directory path, else None."""
    return _match(str(path), _patterns(exclude))


def effective_roots(args_roots: list[str], cfg_scan: Scan | None) -> tuple[list[Path], list[str], str]:
    """-> (roots, exclude, source): the command line's directories ("args"), else [scan] roots from a
    config that loaded ("config"), else the home directory ("default"). exclude comes from the config
    in every case; --repo never consults this. Roots are absolute and resolved, duplicates dropped."""
    if args_roots:
        roots, source = args_roots, "args"
    elif cfg_scan is not None and cfg_scan.roots:  # roots = [] counts as unset (scanroots warns)
        roots, source = list(cfg_scan.roots), "config"
    else:
        roots, source = ["~"], "default"
    out: list[Path] = []
    for r in roots:
        p = Path(r).expanduser().resolve()
        if p not in out:
            out.append(p)
    return out, list(cfg_scan.exclude) if cfg_scan is not None else [], source


def _entries(repo: str, *scope: str) -> list[tuple[str, str, str]]:
    """(origin, key, value) for user.name and user.email, in the order git reads them (the last one wins)."""
    toks = git(repo, "config", *scope, "-z", "--show-origin", "--get-regexp", _USER, ok=(0, 1)).split("\0")
    out = []
    for origin, entry in zip(toks[0::2], toks[1::2]):
        key, _, value = entry.partition("\n")
        out.append((origin, key, value))
    return out


def inspect(path: Path) -> Repo:
    r = str(path)
    urls, hooks_path = {}, ""
    for line in git(r, "config", "--show-scope", "--get-regexp", r"^(remote\..*\.url|core\.hookspath)$",
                    ok=(0, 1)).splitlines():
        scope, _, entry = line.partition("\t")
        key, _, value = entry.partition(" ")
        if key != "core.hookspath":
            urls[key] = value
        elif scope in ("local", "worktree"):
            hooks_path = value
    own = _entries(r, "--local")
    own_origins = {origin for origin, _, _ in own}
    now, beneath = {}, {}
    for origin, key, value in _entries(r):
        now[key] = (value, origin)
        if origin not in own_origins:
            beneath[key] = value
    return Repo(path, urls.get("remote.origin.url") or next(iter(urls.values()), ""), now,
                {key: value for _, key, value in own}, beneath, urls, hooks_path)


def checks(repos: list[Repo], cfg) -> dict:
    """What scan adds to the report (spec 21 §6). ambiguous: repos whose remotes belong to different
    identities, with the pin fix (needs the config; [] without one). own_hooks: repos with their own
    core.hooksPath, where the global guard does not run unless the repo's hooks carry its block."""
    from .guard import REPO_HOOKS, _has_block
    from .resolve import compare
    amb, own = [], []
    for r in repos:
        if cfg is not None and len(r.urls) > 1:
            try:
                p = next((p for p in compare(r.path, cfg).problems if p.code == "ambiguous"), None)
            except (ConfigError, OSError):
                p = None
            if p is not None:
                amb.append({"path": str(r.path), "message": p.message, "fix": p.fix,
                            "fix_argv": list(p.fix_argv) if p.fix_argv else None})
        if r.hooks_path:
            hooks = Path(os.path.expanduser(r.hooks_path))
            hooks = hooks if hooks.is_absolute() else r.path / hooks
            guarded = any(_has_block(hooks / n) for n in REPO_HOOKS)
            own.append({"path": str(r.path), "hooks_path": r.hooks_path, "guarded": guarded,
                        "fix": None if guarded else f"gitpro guard install --repo {shlex.quote(str(r.path))}"})
    return {"ambiguous": amb, "own_hooks": own}


def _owner_name(uid: int) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return f"uid {uid}"


def skip_reason(path: Path, error: str) -> tuple[str, str]:
    """(reason, detail) for a repo git refused to read. The reason groups repos in the summary
    ("owned by root"); the detail says what is wrong with this one. git's own message is often
    misleading here: for another user's repo it says "--local can only be used inside a git repository"."""
    dotgit = path / ".git"
    if dotgit.is_file():
        try:
            line = dotgit.read_text(errors="replace").splitlines()[0]
        except (OSError, IndexError):
            line = ""
        if line.startswith("gitdir:"):
            target = line[len("gitdir:"):].strip()
            if not (path / target).exists():
                return "with a broken .git link", f"broken .git: points to missing {target}"
    getuid = getattr(os, "getuid", None)  # not on Windows
    if getuid:
        try:
            uid = (dotgit if dotgit.exists() else path).stat().st_uid
        except OSError:
            uid = getuid()
        if uid != getuid():
            name = _owner_name(uid)
            return (f"owned by {name}",
                    f"owned by {name}: git ignores other users' repos unless listed in safe.directory")
    return "git can't read", error


def inspect_all(paths: list[Path]) -> tuple[list[Repo], list[tuple[Path, str, str]]]:
    """(repos, [(path, reason, detail) for the ones git refused to read, e.g. owned by another user])."""
    repos, failed = [], []
    for p in paths:
        try:
            repos.append(inspect(p))
        except ConfigError as e:
            failed.append((p, *skip_reason(p, str(e).splitlines()[0] if str(e) else "git failed")))
    return repos, failed


def skipped(bad: list[dict]) -> str:
    """'30 skipped: 26 owned by root, 4 with a broken .git link' for report()["unreadable"]."""
    if not bad:
        return ""
    counts = Counter(b["reason"] for b in bad).most_common()
    return f"{len(bad)} skipped: " + ", ".join(f"{n} {why}" for why, n in counts)


def host_owner(url: str) -> tuple[str, str] | None:
    """(host, owner) of a remote URL, parsed from its redacted form: a userinfo secret never ends up in either."""
    from .resolve import redact_url
    m = _URL.match(redact_url(url) or url)
    return (m[1], m[2]) if m else None


def owner(url: str) -> str:
    """'host/owner' of a remote URL; the URL itself (redacted) when it has no host (a local path)."""
    if not url:
        return "no remote"
    from .resolve import redact_url
    url = redact_url(url) or url
    ho = host_owner(url)
    return "/".join(ho) if ho else url


def derive_id(owner: str, host: str, taken: set) -> str:
    """The [[identity]] id for an owner: lowercase, other characters as '-', then '-<first host label>'
    until it is not in taken. taken is not changed; the caller adds the id it uses."""
    ident = re.sub(r"[^A-Za-z0-9._-]", "-", owner).lower()
    while ident in taken:
        ident += "-" + host.split(".")[0]
    return ident


def suggest(repos: list[Repo]) -> tuple[list[dict], list[Repo]]:
    """[[identity]] blocks that keep the name and email fix would change, and the repos no identity can match.

    An identity matches by host and owner, so each block says how many other repos of those owners it changes.
    """
    wanted: dict[tuple[str, str, str], Counter] = {}
    unmatched = []
    for r in repos:
        now = r.using()
        if not r.local or r.after_fix() == now:
            continue
        ho = host_owner(r.remote)
        if not (ho and HOST_RE.match(ho[0]) and OWNER_RE.match(ho[1]) and len(now) == 2):
            unmatched.append(r)
            continue
        wanted.setdefault((ho[0], now["user.name"], now["user.email"]), Counter())[ho[1]] += 1
    claimed = Counter((host, o) for (host, _, _), owners in wanted.items() for o in owners)
    blocks, ids = [], set()
    for (host, name, email), owners in sorted(wanted.items()):
        names = sorted(owners, key=lambda o: (-owners[o], o))
        others = sum(1 for r in repos if host_owner(r.remote) in {(host, o) for o in names}
                     and r.after_fix().get("user.email") != email and r.using().get("user.email") != email)
        ident = derive_id(names[0], host, ids)
        ids.add(ident)
        repos_note = ", ".join(f"{o} {owners[o]}" for o in names)
        blocks.append({"id": ident, "name": name, "email": email, "host": host, "owners": names, "changes_others": others,
                       "toml": "\n".join([
            "[[identity]]",
            f"id = {json.dumps(ident)}",
            f"name = {json.dumps(name)}",
            f"email = {json.dumps(email)}",
            f"host = {json.dumps(host)}",
            f"owners = {json.dumps(names)}   # repos with this setting: {repos_note}",
            *([f"# also changes {others} other repo(s) of these owners to this email"] if others else []),
            *(f"# {o} is in another block too: an identity matches by owner, so keep only one"
              for o in names if claimed[(host, o)] > 1),
        ])})
    return blocks, unmatched


def who(ident: dict[str, str]) -> str:
    """'name <email>' of a {'user.name', 'user.email'} dict; '(no email)' without an email."""
    email = ident.get("user.email")
    return f"{ident.get('user.name', '')} <{email}>".strip() if email else "(no email)"


def short_names(paths: list[Path]) -> dict[Path, str]:
    """The fewest trailing path parts that tell the paths apart: 'mcp/ai-collab', 'LLM/ai-collab'."""
    out = {}
    for p in paths:
        n = 1
        while n < len(p.parts) and sum(q.parts[-n:] == p.parts[-n:] for q in paths) > 1:
            n += 1
        out[p] = "/".join(p.parts[-n:])
    return out


def report(roots: list[Path], repos: list[Repo], failed: list[tuple[Path, str, str]],
           label: Callable[[str], str]) -> dict:
    """Everything scan shows, as plain data (also what --json prints). label names a source() for people."""
    groups: dict[tuple[str, str, str], Counter] = {}
    for r in repos:
        now = r.using()
        groups.setdefault((now.get("user.name", ""), now.get("user.email", ""), label(r.source())),
                          Counter())[owner(r.remote)] += 1
    over = [r for r in repos if r.local]
    short = short_names([r.path for r in over])
    blocks, unmatched = suggest(repos)
    pair = lambda d: {"name": d.get("user.name", ""), "email": d.get("user.email", "")}  # noqa: E731
    from .resolve import redact_url
    return {
        "roots": [str(p) for p in roots],
        "repos": len(repos),
        "unreadable": [{"path": str(p), "reason": why, "error": detail} for p, why, detail in failed],
        "identities": [
            {"name": n, "email": e, "source": src, "repos": sum(c.values()),
             "owners": dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))}
            for (n, e, src), c in sorted(groups.items(), key=lambda kv: (kv[0][2] == "repo", -sum(kv[1].values())))],
        "overrides": [
            {"path": str(r.path), "short": short[r.path], "remote": redact_url(r.remote) or "", "owner": owner(r.remote),
             "now": pair(r.using()), "after": pair(r.after_fix()),
             "settings": [{"key": k, "value": v, "without": r.beneath.get(k)} for k, v in r.local.items()],
             "fixable": [k for k in r.local if k in r.beneath]}
            for r in over],
        "suggestions": blocks,
        "unmatched": [str(r.path) for r in unmatched],
    }


def fix(repos: list[Repo], state: Path, prog: str = "gitpro") -> tuple[list[tuple[Repo, str]], Path | None]:
    """Remove every override something else can replace; ([(repo, key) removed], script that puts them back)."""
    pairs = [(r, k) for r in repos for k in r.local if k in r.beneath]
    if not pairs:
        return [], None
    # Written first, so an interrupted fix can still be undone.
    script = undo.write("scan --fix", [f"git -C {shlex.quote(str(r.path))} config {k} {shlex.quote(r.local[k])}"
                                       for r, k in pairs], what="removed repo-local user settings", state=state)
    for r, k in pairs:
        remove(r, k)
    return pairs, script


def remove(repo: Repo, key: str) -> None:
    """Remove key from the repo's own config, and the [user] section if that leaves it empty."""
    r = str(repo.path)
    git(r, "config", "--local", "--unset-all", key)
    if not git(r, "config", "--local", "--get-regexp", r"^user\.", ok=(0, 1)):
        # Older git leaves the emptied [user] header behind; newer git already dropped it (exit 128).
        git(r, "config", "--local", "--remove-section", "user", ok=(0, 128))
