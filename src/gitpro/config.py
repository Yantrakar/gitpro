"""Find, load and validate the identities TOML. Reads only."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ENV_VAR = "GITPRO_CONFIG"
USER_CONFIG = ".config/gitpro.toml"  # relative to home

# Leading alphanumeric everywhere so no value can be parsed as an ssh/git option.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
USER_RE = ID_RE
OWNER_RE = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9._*-]*(?:/[A-Za-z0-9*][A-Za-z0-9._*-]*)*$")
PATH_RE = re.compile(r"^(?:~/|/)[A-Za-z0-9._/@+-]+$")
EMAIL_RE = re.compile(r'^[^@\s<>"]+@[^@\s<>"]+$')
CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
# Reserved for documentation (RFC 2606): never real, so a value left over from the example.
PLACEHOLDER_RE = re.compile(r"(?:^|\.)(?:example|example\.(?:com|net|org))$", re.I)


class ConfigError(Exception):
    pass


def check(cond: object, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


@dataclass
class Default:
    name: str | None = None
    email: str | None = None
    ssh_key: str | None = None
    ssh_hosts: list[str] = field(default_factory=list)
    sign: bool = False
    git: dict = field(default_factory=dict)


@dataclass
class Identity:
    id: str
    name: str
    email: str
    host: str
    owners: list[str] = field(default_factory=list)
    ssh_key: str | None = None
    ssh_alias: str | None = None
    ssh_user: str = "git"
    ssh_port: int | None = None
    rewrite: bool = True
    rewrite_https: bool = False
    sign: bool | None = None  # None: inherit [default].sign
    file: str | None = None
    git: dict = field(default_factory=dict)


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
                      "create one with: gitpro init")


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


def _build(cls, table: object, where: str):
    check(isinstance(table, dict), f"{where}: expected a table")
    _no_control_chars(table, where)
    known = [f.name for f in fields(cls)]
    unknown = sorted(set(table) - set(known))
    check(not unknown, f"{where}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(known)})")
    missing = [f.name for f in fields(cls)
               if f.default is MISSING and f.default_factory is MISSING and f.name not in table]
    check(not missing, f"{where}: missing {', '.join(missing)}")
    return cls(**table)


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


def load(path: Path) -> tuple[Default, list[Identity]]:
    try:
        st = path.stat()
        raw = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    # The config decides what commands git runs (sshCommand, credential helpers).
    check(st.st_uid == os.getuid() and not st.st_mode & 0o022,
          f"{path}: must be owned by you and not group/world-writable (chmod 600 {path})")
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None

    check(set(data) <= {"default", "identity"},
          f"{path}: unknown top-level keys {sorted(set(data) - {'default', 'identity'})}")
    check("default" in data, f"{path}: missing [default]")
    check(isinstance(data.get("identity", []), list), f"{path}: write identities as [[identity]]")

    d = _build(Default, data["default"], "default")
    _check_person(d, "default", required=False)
    _check_path(d.ssh_key, "default.ssh_key")
    _check_bool(d.sign, "default.sign")
    check(isinstance(d.git, dict), "default.git: must be a table")
    check(isinstance(d.ssh_hosts, list), "default.ssh_hosts: must be a list")
    check(not d.ssh_hosts or d.ssh_key, "default: ssh_hosts needs ssh_key")
    check(not d.sign or (d.ssh_key and d.email), "default: sign needs ssh_key and email")
    for h in d.ssh_hosts:
        _check_host(h, "default.ssh_hosts")

    ids: list[Identity] = []
    for n, tbl in enumerate(data.get("identity", [])):
        i = _build(Identity, tbl, f"identity[{n}]")
        check(isinstance(i.id, str) and ID_RE.match(i.id), f"identity[{n}]: bad id {i.id!r}")
        w = f"identity {i.id!r}"
        _check_person(i, w, required=True)
        _check_host(i.host, w)
        check(isinstance(i.owners, list), f"{w}: owners must be a list")
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
        i.file = i.file or f"~/.gitconfig-{i.id}"
        ids.append(i)

    check(not _dupes(i.id for i in ids), f"duplicate ids: {_dupes(i.id for i in ids)}")
    check(not _dupes(i.file for i in ids), f"duplicate files: {_dupes(i.file for i in ids)}")
    ssh_names = list(d.ssh_hosts) + [i.ssh_alias for i in ids if i.ssh_alias]
    check(not _dupes(ssh_names), f"duplicate ssh Host names: {_dupes(ssh_names)}")
    return d, ids
