"""Advisory checks. Never block a run, never write."""
from __future__ import annotations

import re
import subprocess

from . import keys, render
from .config import Default, Identity
from .plan import Env
from .resolve import is_codecommit


def warnings(d: Default, ids: list[Identity], env: Env) -> list[str]:
    out = []
    for i in ids:
        if is_codecommit(i.host) and i.ssh_user == "git" and (i.ssh_key or i.ssh_alias):
            out.append(f"identity {i.id!r}: CodeCommit's SSH user is the SSH key ID, not git; set ssh_user to it")
        if i.ssh_alias and i.rewrite:
            out += [f"identity {i.id!r}: owner {o!r} is a glob; insteadOf needs a literal prefix, so "
                    f"its URLs aren't rewritten to {i.ssh_alias}" for o in i.owners if "*" in o]
        if i.ssh_key and not i.ssh_alias and i.host in d.ssh_hosts:
            out.append(f"identity {i.id!r}: ssh tries {i.ssh_key} first, then {d.ssh_key} ([default].ssh_hosts); "
                       f"if {i.ssh_key} isn't registered you are signed in as the default account. "
                       "Add an ssh_alias to offer only this identity's key")

    gitconfig = env.home / ".gitconfig"
    if gitconfig.exists():
        stray = subprocess.run(["git", "config", "--file", str(gitconfig), "--get-regexp",
                                r"^(user\.(name|email|signingkey)|core\.sshcommand)$"],
                               capture_output=True, text=True).stdout.strip()
        if stray:
            out.append(f"{gitconfig} also sets these directly; the generated config is included after them "
                       "and wins, so they only confuse -- consider removing:\n    " + stray.replace("\n", "\n    "))

    ssh_dir = env.home / ".ssh"
    if ssh_dir.exists() and ssh_dir.stat().st_mode & 0o022:
        out.append(f"{ssh_dir} is group/world-writable; ssh may refuse it -- chmod 700 {ssh_dir}")
    ssh_cfg = ssh_dir / "config"
    if ssh_cfg.exists():
        text = render.without_block(ssh_cfg.read_text())
        theirs = {h for line in re.findall(r"^\s*Host\s+(.+)$", text, re.M | re.I) for h in line.split()}
        ours = set(d.ssh_hosts) | {i.ssh_alias for i in ids if i.ssh_alias}
        if ours & theirs:
            out.append(f"{ssh_cfg} also defines Host {' '.join(sorted(ours & theirs))}; "
                       "the generated block comes first and wins, remove the old one")

    for k in keys.inventory(d, ids):
        p = env.expand(k.path)
        if not p.exists():
            out.append(f"{k.path} missing -- run: gitpro keys --generate")
        elif p.stat().st_mode & 0o077:
            out.append(f"{k.path} is accessible by others -- chmod 600 {p}")
    return out
