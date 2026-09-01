"""SSH keys: which key serves which identity, generating missing ones, printing .pub files."""
from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass, field

from .config import Default, Identity, signs
from .render import Expand, pub_path


@dataclass
class Key:
    path: str
    email: str
    uses: list[str] = field(default_factory=list)
    sign: bool = False


def inventory(d: Default, ids: list[Identity]) -> list[Key]:
    keys: dict[str, Key] = {}

    def use(path: str, email: str, what: str, sign: bool) -> None:
        k = keys.setdefault(path, Key(path, email))
        k.uses.append(what)
        k.sign |= sign

    if d.ssh_key:
        use(d.ssh_key, d.email or "", f"{', '.join(d.ssh_hosts) or 'any host'} (default)", d.sign)
    for i in ids:
        if i.ssh_key:
            use(i.ssh_key, i.email, f"{i.host}: {', '.join(i.owners) or '*'} [{i.id}]", signs(i, d))
        elif signs(i, d):
            use(d.ssh_key, i.email, f"signing for [{i.id}]", True)
    return list(keys.values())


def generate(keys: list[Key], expand: Expand, passphrase: bool = True) -> int:
    """Create missing keys; existing ones are never touched. Returns how many were created."""
    created = 0
    for k in keys:
        p = expand(k.path)
        if p.exists():
            continue
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        cmd = ["ssh-keygen", "-t", "ed25519", "-a", "100", "-f", str(p),
               "-C", f"{k.email} {socket.gethostname()}"]
        subprocess.run(cmd + ([] if passphrase else ["-N", ""]), check=True)
        created += 1
    return created


def report(keys: list[Key], expand: Expand) -> None:
    print("SSH keys -- register each public key with the account(s) listed:")
    for k in keys:
        role = "auth + signing" if k.sign else "auth"
        print(f"  {k.path}  ({role})  ->  {'; '.join(k.uses)}")
        pub = pub_path(expand(k.path))
        print(f"    {pub.read_text().strip()}" if pub.exists() else "    (missing -- run: gitpro keys --generate)")
