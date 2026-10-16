"""What `gitpro setup` learns from the repos on this machine (spec 22 §5.2-5.4): discover's sources
under the given folders, plus the identity of the commits made in each repo, grouped into proposed
identities. Reads only; never a private key, shell history or a forge account."""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import config


@dataclass
class Learned:
    identities: List[dict] = field(default_factory=list)   # id, name, email, host, owners, repos, ssh_*, also
    unassigned: List[str] = field(default_factory=list)    # "host/owner (n repos, no commits)"
    notes: List[str] = field(default_factory=list)         # skipped, and why
    repos: int = 0
    committed: int = 0
    seconds: float = 0.0


def learn(roots: List[Path], home: Path, cfg: Optional[config.Config], *, budget: float) -> Learned:
    from . import discover
    start = time.monotonic()
    d = discover.collect(("vscode", "jetbrains", "ssh", "includeif", "repos"), home=home,
                         config_path=cfg.path if cfg is not None else None, find_config=cfg is not None,
                         quiet=True, roots=[Path(r).expanduser() for r in roots], reflog=True, budget=budget)
    out = propose(d, cfg)
    out.repos = (d["sources"].get("repos") or {}).get("count", 0)
    out.committed = d.get("committed", 0)
    out.seconds = time.monotonic() - start
    return out


def _pick_alias(aliases: List[dict], host: str, owners: set, taken: set) -> Optional[dict]:
    """The SSH alias these owners' remotes use (discover's render_toml rule), when gitpro accepts it."""
    from .config import HOST_RE, PATH_RE
    cands = [a for a in aliases if a["host"] == host and a["alias"] not in taken]
    scored = sorted(((len(owners & set(a.get("used_by") or ())), a) for a in cands), key=lambda x: -x[0])
    if not scored or scored[0][0] == 0:
        return None
    a = scored[0][1]
    key = a.get("key")
    if not (HOST_RE.match(a["alias"]) and a["alias"] != host and isinstance(key, str) and PATH_RE.match(key)
            and ".." not in key.split("/")):
        return None
    return a


def propose(d: dict, cfg: Optional[config.Config]) -> Learned:
    """discover.collect's result (with reflog=True) as proposed identities."""
    from . import discover
    out = Learned()
    taken = {i.id for i in cfg.identities} if cfg is not None else set()
    taken_aliases = {i.ssh_alias for i in cfg.identities if i.ssh_alias} if cfg is not None else set()
    for h in d.get("hosts") or []:
        host = h["host"]
        if not discover._usable_host(host):
            out.notes.append(f"{host}: not a host name gitpro accepts")
            continue
        groups: dict = {}
        no_email: List[dict] = []
        for o in h.get("owners") or []:
            where = f"{host}/{o['owner']}"
            if o.get("in_config"):
                continue
            if not config.OWNER_RE.fullmatch(o["owner"]):
                out.notes.append(f"{where}: not an owner name gitpro accepts")
                continue
            emails = Counter({e: n for e, n in (o.get("emails") or {}).items() if discover._usable_email(e)})
            if not emails:
                no_email.append(o)
                continue
            top = sorted(emails.items(), key=lambda x: (-x[1], x[0]))[0][0]
            groups.setdefault(top, []).append((o, [(e, n) for e, n in emails.most_common() if e != top]))
        if len(groups) == 1:
            only = next(iter(groups.values()))
            only += [(o, []) for o in no_email]
        else:
            out.unassigned += [f"{host}/{o['owner']} ({o['repos']} repo{'s' * (o['repos'] != 1)}, no commits)"
                               for o in no_email]
        for email, members in groups.items():
            members.sort(key=lambda m: (-m[0]["repos"], m[0]["owner"]))
            owners = [m[0]["owner"] for m in members]
            names: Counter = Counter()
            for o, _ in members:
                for n, e, c in o.get("people") or []:
                    if e == email and discover._usable_name(n):
                        names[n] += c
            if not names:
                for o, _ in members:
                    for n, c in (o.get("names") or {}).items():
                        if discover._usable_name(n):
                            names[n] += c
            also: Counter = Counter()
            for _, others in members:
                for e, c in others:
                    also[e] += c
            ident = discover._new_id(owners[0], host, taken)
            taken.add(ident)
            i = {"id": ident, "name": names.most_common(1)[0][0] if names else None, "email": email, "host": host,
                 "owners": owners, "repos": sum(m[0]["repos"] for m in members), "also": also.most_common()}
            a = _pick_alias(d.get("aliases") or [], host, set(owners), taken_aliases)
            if a is not None:
                taken_aliases.add(a["alias"])
                i.update(ssh_alias=a["alias"], ssh_key=a["key"])
                if a.get("user") and a["user"] != "git" and config.ID_RE.match(a["user"]):
                    i["ssh_user"] = a["user"]
                if a.get("port") and a["port"] != 22:
                    i["ssh_port"] = int(a["port"])
            out.identities.append(i)
    out.identities.sort(key=lambda i: (-i["repos"], i["id"]))
    return out
