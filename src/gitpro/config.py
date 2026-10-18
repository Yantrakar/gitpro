"""Find, load and validate the identities TOML. Reads only."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import MISSING, dataclass, field, fields, replace
from pathlib import Path
from typing import Optional, Tuple

from .util import expand_home as _expand

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_toml = tomllib  # the one TOML parser (tomllib, or tomli before 3.11)

ENV_VAR = "GITPRO_CONFIG"
USER_CONFIG = ".config/gitpro.toml"  # relative to home

# Leading alphanumeric everywhere so no value can be parsed as an ssh/git option.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
USER_RE = ID_RE
OWNER_RE = re.compile(r"^~?[A-Za-z0-9*][A-Za-z0-9._*-]*(?:/[A-Za-z0-9*][A-Za-z0-9._*-]*)*$")  # ~: SourceHut users
PATH_RE = re.compile(r"^(?:~/|/)[A-Za-z0-9._/@+-]+$")
EMAIL_RE = re.compile(r'^[^@\s<>"]+@[^@\s<>"]+$')
CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
SCAN_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")  # [scan] roots: tab included
# Reserved for documentation (RFC 2606): never real, so a value left over from the example.
PLACEHOLDER_RE = re.compile(r"(?:^|\.)(?:example|example\.(?:com|net|org))$", re.I)


class ConfigError(Exception):
    pass


def check(cond: object, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


class Strings(tuple):
    """A config list: a tuple (hashable, immutable) that still equals a list with the same items,
    so code and tests written when these fields were lists keep working."""
    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return list(self) == other
        return tuple.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        eq = self.__eq__(other)
        return eq if eq is NotImplemented else not eq

    __hash__ = tuple.__hash__


def _tuples(obj: object, *names: str) -> None:
    """Turn list values of these fields into Strings (frozen-safe, from __post_init__)."""
    for n in names:
        v = getattr(obj, n)
        if isinstance(v, (list, tuple)) and not isinstance(v, Strings):
            object.__setattr__(obj, n, Strings(v))


@dataclass(frozen=True)
class Trailers:
    co_authored_by: Optional[str] = None             # "allow" | "forbid" | "require" | None = inherit
    forbid_emails: Optional[Tuple[str, ...]] = None  # globs

    def __post_init__(self) -> None:
        _tuples(self, "forbid_emails")


@dataclass(frozen=True)
class Default:
    name: Optional[str] = None
    email: Optional[str] = None
    ssh_key: Optional[str] = None
    ssh_hosts: Tuple[str, ...] = ()
    sign: bool = False
    git: dict = field(default_factory=dict, hash=False)  # the one mutable field; nobody mutates it
    gh_account: Optional[str] = None
    glab_account: Optional[str] = None
    clone_dir: Optional[str] = None
    trailers: Trailers = Trailers()

    def __post_init__(self) -> None:
        _tuples(self, "ssh_hosts")


@dataclass(frozen=True)
class Identity:
    id: str
    name: str
    email: str
    host: str
    owners: Tuple[str, ...] = ()
    ssh_key: Optional[str] = None
    ssh_alias: Optional[str] = None
    ssh_user: str = "git"
    ssh_port: Optional[int] = None
    rewrite: bool = True
    rewrite_https: bool = False
    sign: Optional[bool] = None  # None: inherit [default].sign
    file: Optional[str] = None
    git: dict = field(default_factory=dict, hash=False)
    gh_account: Optional[str] = None
    glab_account: Optional[str] = None
    clone_dir: Optional[str] = None
    policy: Optional[str] = None
    trailers: Trailers = Trailers()

    def __post_init__(self) -> None:
        _tuples(self, "owners")


@dataclass(frozen=True)
class Scan:
    roots: Tuple[str, ...] = ("~",)
    exclude: Tuple[str, ...] = ()
    stale_days: int = 180

    def __post_init__(self) -> None:
        _tuples(self, "roots", "exclude")


@dataclass(frozen=True)
class Guard:
    hooks: Optional[bool] = None    # None: setup installs them, apply keeps what is installed; false: never
    on_mismatch: str = "block"  # "block" | "warn"
    chain_to: str = ""


@dataclass(frozen=True)
class Clone:
    dir: Optional[str] = None
    layout: str = "{host}/{owner}/{repo}"


@dataclass(frozen=True)
class Forge:
    online: bool = False
    similar: bool = False


@dataclass(frozen=True)
class Agents:
    hook: str = "block"  # "block" | "warn" | "off"
    bot_emails: Tuple[str, ...] = ("*[bot]@users.noreply.github.com", "noreply@anthropic.com")

    def __post_init__(self) -> None:
        _tuples(self, "bot_emails")


@dataclass(frozen=True)
class Policy:
    files: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _tuples(self, "files")


@dataclass(frozen=True)
class Config:
    """Everything in the TOML. Config(default, identities) wraps the old (Default, list[Identity]) pair."""
    default: Default
    identities: Tuple[Identity, ...]
    scan: Scan = Scan()
    guard: Guard = Guard()
    clone: Clone = Clone()
    forge: Forge = Forge()
    agents: Agents = Agents()
    policy: Policy = Policy()
    path: Optional[Path] = None
    how: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.identities, tuple):
            object.__setattr__(self, "identities", tuple(self.identities))


def signs(i: Identity, d: Default) -> bool:
    return d.sign if i.sign is None else i.sign


def find(explicit: Path | None, home: Path) -> tuple[Path, str]:
    """Return (config path, where it was found)."""
    if explicit:
        return explicit.expanduser(), "--config"
    if os.environ.get(ENV_VAR):
        return Path(os.environ[ENV_VAR]).expanduser(), f"${ENV_VAR}"
    if (home / USER_CONFIG).is_file():
        return home / USER_CONFIG, "user config"
    raise ConfigError(f"no config found (looked at -c, ${ENV_VAR}, ~/{USER_CONFIG}); "
                      "create one with: gitpro setup")


# ---------------------------------------------------------------- validation

def _no_control_chars(value: object, where: str) -> None:
    if isinstance(value, str):
        check(not CONTROL_RE.search(value), f"{where}: control characters not allowed")
    elif isinstance(value, dict):
        for k, v in value.items():
            _no_control_chars(k, where)
            _no_control_chars(v, f"{where}.{k}")
    elif isinstance(value, list):
        for v in value:
            _no_control_chars(v, where)


def _build(cls, table: object, where: str, nested: Tuple[str, ...] = ()):
    """A frozen `cls` from a TOML table: unknown keys and missing required keys are errors.
    Keys in `nested` are already-built objects (trailers) and skip the control-char walk."""
    check(isinstance(table, dict), f"{where}: expected a table")
    _no_control_chars({k: v for k, v in table.items() if k not in nested}, where)
    known = [f.name for f in fields(cls)]
    unknown = sorted(set(table) - set(known))
    check(not unknown, f"{where}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(known)})")
    missing = [f.name for f in fields(cls)
               if f.default is MISSING and f.default_factory is MISSING and f.name not in table]
    check(not missing, f"{where}: missing {', '.join(missing)}")
    return cls(**table)


def _build_nested(cls, table: object, where: str):
    """A nested table such as [default.trailers]; same rules as _build."""
    return _build(cls, table, where)


def _check_path(p: object, where: str) -> None:
    if p is not None:
        check(isinstance(p, str) and PATH_RE.match(p) and ".." not in p.split("/"),
              f"{where}: must start with ~/ or /, use only [A-Za-z0-9._/@+-], no '..'")


def _check_host(h: object, where: str) -> None:
    check(isinstance(h, str) and HOST_RE.match(h), f"{where}: bad host {h!r}")
    check(not PLACEHOLDER_RE.search(h), f"{where}: {h} is a placeholder from the example; use your real host")


def _check_person(obj: Default | Identity, where: str, required: bool) -> None:
    if not required and obj.name is None and obj.email is None:
        return
    check(isinstance(obj.name, str) and obj.name.strip() and not re.search(r"[<>]", obj.name),
          f"{where}: bad or missing name")
    check(isinstance(obj.email, str) and EMAIL_RE.match(obj.email), f"{where}: bad or missing email")
    check(not PLACEHOLDER_RE.search(obj.email.rsplit("@", 1)[1]),
          f"{where}: {obj.email} is a placeholder from the example; use your real email")


def _check_bool(v: object, where: str, optional: bool = False) -> None:
    check(isinstance(v, bool) or (optional and v is None), f"{where}: must be true or false")


def _dupes(values) -> set:
    seen, out = set(), set()
    for v in values:
        (out if v in seen else seen).add(v)
    return out


def _check_list(v: object, where: str, what: str = "a list") -> None:
    check(isinstance(v, tuple), f"{where} must be {what}")


def _check_strings(v: object, where: str) -> None:
    """A list of glob strings (control characters were already refused by _build)."""
    check(isinstance(v, tuple) and all(isinstance(x, str) for x in v), f"{where} must be a list of strings")


def _check_dir(p: object, where: str, key: str) -> None:
    """PATH_RE paths with the 3.2 message: `<where>: <key> must start with ~/ or /`."""
    if p is not None:
        check(isinstance(p, str) and PATH_RE.match(p) and ".." not in p.split("/"),
              f"{where}: {key} must start with ~/ or /, use only [A-Za-z0-9._/@+-], no '..'")


def _check_account(v: object, where: str, key: str) -> None:
    if v is not None:
        check(isinstance(v, str) and ID_RE.match(v), f"{where}: {key} must match [A-Za-z0-9][A-Za-z0-9._-]*")


def _check_enum(v: object, allowed: Tuple[str, ...], where: str, key: str) -> None:
    if len(allowed) == 2:
        words = f'"{allowed[0]}" or "{allowed[1]}"'
    else:
        words = ", ".join(f'"{a}"' for a in allowed[:-1]) + f' or "{allowed[-1]}"'
    check(v in allowed, f"{where}: {key} must be {words}")


def _with_trailers(table: object, where: str) -> object:
    """Copy of an identity/default table with its `trailers` sub-table built and validated;
    `where` names the sub-table ("[default.trailers]", "identity 'x'.trailers")."""
    if not isinstance(table, dict) or "trailers" not in table:
        return table
    t = _build_nested(Trailers, table["trailers"], where)
    if t.co_authored_by is not None:
        _check_enum(t.co_authored_by, ("allow", "forbid", "require"), where, "co_authored_by")
    if t.forbid_emails is not None:
        _check_strings(t.forbid_emails, f"{where}: forbid_emails")
    return dict(table, trailers=t)


def _check_extras(obj: "Default | Identity", where: str) -> None:
    """The keys package 01 added to [default] and [[identity]]."""
    _check_account(obj.gh_account, where, "gh_account")
    _check_account(obj.glab_account, where, "glab_account")
    _check_dir(obj.clone_dir, where, "clone_dir")


LAYOUT_RE = re.compile(r"^(?:\{(?:host|owner|repo|id)\}|[A-Za-z0-9._/-])+$")
TOP_LEVEL = ("agents", "clone", "default", "forge", "guard", "identity", "policy", "scan")
HOOKS_DIR = ".config/gitpro/hooks"  # gitpro's own global hooks dir, relative to home (06)


def scan_root_error(r: object) -> str | None:
    """Why r cannot be a [scan] roots entry, else None. Roots are only walked, never written into a
    gitconfig or ssh config, so unlike PATH_RE any character but a control character is allowed
    (spaces, non-ASCII, WSL's /mnt/c/Users/First Last, /)."""
    if not isinstance(r, str):
        return "roots must be strings"
    shown = json.dumps(r, ensure_ascii=False).replace("\x7f", "\\u007f")
    if SCAN_CONTROL_RE.search(r):
        return f"root {shown} contains a control character"
    if not (r == "~" or r.startswith(("~/", "/"))):
        return f"roots must start with ~/ or / (or be ~): {shown}"
    if ".." in r.split("/"):
        return f"root {shown} must not contain a .. segment"
    return None


def _scan(table: object) -> Scan:
    w = "[scan]"
    if isinstance(table, dict) and "roots" in table:
        check(isinstance(table["roots"], list), f"{w}: roots must be a list")
    s = _build(Scan, table, w)
    _check_list(s.roots, f"{w}: roots")
    for r in s.roots:
        err = scan_root_error(r)
        check(err is None, f"{w}: {err}")
    _check_strings(s.exclude, f"{w}: exclude")
    check(type(s.stale_days) is int and s.stale_days > 0, f"{w}: stale_days must be a positive integer")
    return s


def _guard(table: object, home: Path) -> Guard:
    w = "[guard]"
    g = _build(Guard, table, w)
    _check_bool(g.hooks, f"{w}.hooks", optional=True)
    _check_enum(g.on_mismatch, ("block", "warn"), w, "on_mismatch")
    check(isinstance(g.chain_to, str), f"{w}: chain_to must start with ~/ or /")
    if g.chain_to:
        check(PATH_RE.match(g.chain_to) and ".." not in g.chain_to.split("/"),
              f"{w}: chain_to must start with ~/ or /")
        own = os.path.realpath(home / HOOKS_DIR)
        check(os.path.realpath(_expand(g.chain_to, home)) != own,
              f"{w}: chain_to must not be gitpro's own hooks dir (~/{HOOKS_DIR})")
    return g


def _clone(table: object) -> Clone:
    w = "[clone]"
    c = _build(Clone, table, w)
    _check_dir(c.dir, w, "dir")
    lay = c.layout
    check(isinstance(lay, str) and LAYOUT_RE.match(lay) and not lay.startswith("/")
          and ".." not in lay.split("/") and "" not in lay.split("/"),
          f"{w}: layout may use only {{host}} {{owner}} {{repo}} {{id}} and [A-Za-z0-9._/-]")
    return c


def _forge(table: object) -> Forge:
    f = _build(Forge, table, "[forge]")
    _check_bool(f.online, "[forge].online")
    _check_bool(f.similar, "[forge].similar")
    return f


def _agents(table: object) -> Agents:
    a = _build(Agents, table, "[agents]")
    _check_enum(a.hook, ("block", "warn", "off"), "[agents]", "hook")
    _check_strings(a.bot_emails, "[agents]: bot_emails")
    return a


def _policy(table: object) -> Policy:
    p = _build(Policy, table, "[policy]")
    _check_list(p.files, "[policy]: files")
    for f in p.files:
        _check_dir(f, "[policy]", "files")
    return p


def _read(path: Path) -> dict:
    """Permission check + TOML parse; the shared first step of load_all and load_scan."""
    try:
        st = path.stat()
        raw = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    # The config decides what commands git runs (sshCommand, credential helpers).
    check(st.st_uid == os.getuid() and not st.st_mode & 0o022,
          f"{path}: must be owned by you and not group/world-writable (chmod 600 {path})")
    return _load_toml(raw, path)


def _load_toml(text: str, path: object = "config") -> dict:
    try:
        return _toml.loads(text)
    except _toml.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None


def load_all(path: Path, how: str = "") -> Config:
    """Load and validate every table; the Config other packages read."""
    return _parse_all(_read(path), path, how)


def loads(text: str, path: Path, how: str = "") -> Config:
    """Validate proposed configuration without writing it to disk."""
    return _parse_all(_load_toml(text, path), path, how)


def _parse_all(data: dict, path: Path, how: str) -> Config:
    home = Path.home()

    unknown = sorted(set(data) - set(TOP_LEVEL))
    check(not unknown, f"{path}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(TOP_LEVEL)})")
    check("default" in data, f"{path}: missing [default]")
    check(isinstance(data.get("identity", []), list), f"{path}: write identities as [[identity]]")

    d = _build(Default, _with_trailers(data["default"], "[default.trailers]"), "default",
               nested=("trailers",))
    _check_person(d, "default", required=False)
    _check_path(d.ssh_key, "default.ssh_key")
    _check_bool(d.sign, "default.sign")
    check(isinstance(d.git, dict), "default.git: must be a table")
    check(isinstance(d.ssh_hosts, tuple), "default.ssh_hosts: must be a list")
    check(not d.ssh_hosts or d.ssh_key, "default: ssh_hosts needs ssh_key")
    check(not d.sign or (d.ssh_key and d.email), "default: sign needs ssh_key and email")
    for h in d.ssh_hosts:
        _check_host(h, "default.ssh_hosts")
    _check_extras(d, "[default]")

    ids = []
    for n, tbl in enumerate(data.get("identity", [])):
        tid = tbl.get("id") if isinstance(tbl, dict) else None
        tw = f"identity {tid!r}" if isinstance(tid, str) and ID_RE.match(tid) else f"identity[{n}]"
        i = _build(Identity, _with_trailers(tbl, f"{tw}.trailers"), f"identity[{n}]", nested=("trailers",))
        check(isinstance(i.id, str) and ID_RE.match(i.id), f"identity[{n}]: bad id {i.id!r}")
        w = f"identity {i.id!r}"
        _check_person(i, w, required=True)
        _check_host(i.host, w)
        check(isinstance(i.owners, tuple), f"{w}: owners must be a list")
        for o in i.owners:
            check(isinstance(o, str) and OWNER_RE.match(o), f"{w}: bad owner {o!r}")
        check(isinstance(i.ssh_user, str) and USER_RE.match(i.ssh_user), f"{w}: bad ssh_user")
        check(i.ssh_port is None or (type(i.ssh_port) is int and 0 < i.ssh_port < 65536),
              f"{w}: bad ssh_port")
        _check_path(i.ssh_key, f"{w}.ssh_key")
        _check_path(i.file, f"{w}.file")
        _check_bool(i.rewrite, f"{w}.rewrite")
        _check_bool(i.rewrite_https, f"{w}.rewrite_https")
        _check_bool(i.sign, f"{w}.sign", optional=True)
        check(isinstance(i.git, dict), f"{w}.git: must be a table")
        check(not signs(i, d) or i.ssh_key or d.ssh_key, f"{w}: signing needs ssh_key here or in [default]")
        if i.ssh_alias is not None:
            check(isinstance(i.ssh_alias, str) and HOST_RE.match(i.ssh_alias) and i.ssh_alias != i.host,
                  f"{w}: bad ssh_alias")
            check(i.ssh_key, f"{w}: ssh_alias needs ssh_key")
        _check_extras(i, w)
        _check_dir(i.policy, w, "policy")
        ids.append(replace(i, file=i.file or f"~/.gitconfig-{i.id}"))

    check(not _dupes(i.id for i in ids), f"duplicate ids: {_dupes(i.id for i in ids)}")
    check(not _dupes(i.file for i in ids), f"duplicate files: {_dupes(i.file for i in ids)}")
    ssh_names = list(d.ssh_hosts) + [i.ssh_alias for i in ids if i.ssh_alias]
    check(not _dupes(ssh_names), f"duplicate ssh Host names: {_dupes(ssh_names)}")

    return Config(
        default=d, identities=tuple(ids),
        scan=_scan(data.get("scan", {})),
        guard=_guard(data.get("guard", {}), home),
        clone=_clone(data.get("clone", {})),
        forge=_forge(data.get("forge", {})),
        agents=_agents(data.get("agents", {})),
        policy=_policy(data.get("policy", {})),
        path=path, how=how,
    )


def load(path: Path) -> "tuple[Default, list[Identity]]":
    """The pre-0.2 shape, kept for existing callers: (Default, list of identities)."""
    c = load_all(path)
    return c.default, list(c.identities)


def load_scan(path: Path) -> Scan:
    """Only [scan], for scan (02) and lost (03), which must work on a config `init` just wrote with
    placeholders: every other table is ignored, so placeholders or errors elsewhere never matter.

    It raises ConfigError (it does not return Scan() itself, unlike the first draft of spec 01 §3.3)
    when the file is missing, unreadable, not valid TOML, or its [scan] table is bad. The caller
    decides what that means: scan/lost catch it, print `⚠ <path>: <error>; scanning ~` once and use
    Scan(); a caller that needs the configured roots can report the error instead."""
    data = _read(path)
    return _scan(data.get("scan", {}))
