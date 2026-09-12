"""Desired state: every managed file with its full content. Reads the disk, never writes."""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import render
from .config import HOOKS_DIR, Config, Default, Identity, check, signs

MANAGED_GITCONFIG = ".gitconfig-identities"
GUARD_GITCONFIG = ".gitconfig-gitpro-guard"  # core.hooksPath for the guard hooks (06)
MANAGED_SSH = "config.d/git-identities"  # relative to ~/.ssh, as ssh's Include expects
ALLOWED_SIGNERS = ".config/git/allowed_signers"


@dataclass(frozen=True)
class Env:
    home: Path

    def expand(self, p: str) -> Path:
        return self.home / p[2:] if p.startswith("~/") else Path(p)

    def tilde(self, p: Path) -> str:
        if self.home == Path.home().resolve() and self.home in p.parents:
            return "~/" + str(p.relative_to(self.home))
        return str(p)


@dataclass(frozen=True)
class Target:
    path: Path
    text: str | None  # None: remove the file
    mode: int = 0o644
    strict: bool = False  # force mode; otherwise keep the existing mode minus group/world write


def _read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def _signers(d: Default, ids: list[Identity], env: Env) -> list[tuple[str, str]] | None:
    pairs = [(d.email, d.ssh_key)] if d.sign else []
    pairs += [(i.email, i.ssh_key or d.ssh_key) for i in ids if signs(i, d)]
    if not pairs:
        return None
    entries = []
    for email, key in dict.fromkeys(pairs):
        pub = render.pub_path(env.expand(key))
        if pub.exists():
            entries.append((email, " ".join(pub.read_text().split()[:2])))
    return entries


# ---------------------------------------------------------------- guard hooks (06)

def gitpro_bin() -> str:
    """The installing gitpro, baked into the hook scripts as the last fallback ("" when unknown)."""
    beside = Path(sys.executable).parent / "gitpro"
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    arg0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if arg0 is not None and arg0.name == "gitpro" and os.access(arg0, os.X_OK):
        return str(arg0.resolve())
    return ""


@functools.lru_cache(maxsize=1)
def hook_timeout() -> str:
    """`timeout --foreground 30` when this machine's timeout(1) takes --foreground (GNU coreutils), else ""
    (busybox and macOS: the check runs unbounded; every git call inside it has its own 5 s limit)."""
    if not shutil.which("timeout"):
        return ""
    try:
        ok = subprocess.run(["timeout", "--foreground", "5", "true"], capture_output=True,
                            stdin=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    return "timeout --foreground 30" if ok else ""


def guard_paths(env: Env) -> list[Path]:
    """Every file the guard owns: the hook scripts, the README, ~/.gitconfig-gitpro-guard."""
    hooks = env.home / HOOKS_DIR
    return [*(hooks / n for n in render.HOOK_NAMES), hooks / "README", env.home / GUARD_GITCONFIG]


def guard_manifest(env: Env) -> list[str]:
    return sorted(env.tilde(p) for p in guard_paths(env))


def guard_include(env: Env) -> str:
    return env.tilde(env.home / GUARD_GITCONFIG)


def guard_installed(env: Env) -> bool:
    """`guard install` ran: ~/.gitconfig-gitpro-guard exists and gitpro wrote it."""
    p = env.home / GUARD_GITCONFIG
    return p.is_file() and render.is_generated(_read(p))


def guard_wanted(cfg: Config, env: Env) -> bool:
    return bool(cfg.guard.hooks) or guard_installed(env)


def prune_hooks_dir(env: Env) -> None:
    """Remove ~/.config/gitpro/hooks after an uninstall, only when nothing is left in it."""
    try:
        (env.home / HOOKS_DIR).rmdir()
    except OSError:
        pass


def guard_targets(cfg: Config, env: Env) -> list[Target]:
    hooks = env.home / HOOKS_DIR
    chain_dir = env.expand(cfg.guard.chain_to) if cfg.guard.chain_to else None
    chain = str(chain_dir) if chain_dir is not None and chain_dir.is_dir() else None   # missing: no chain
    gp = gitpro_bin()
    out = [Target(hooks / n, render.hook_script(n, chain, gp, hook_timeout()) if n in render.GUARD_HOOKS
                  else render.hook_stub(n, chain), 0o700, strict=True) for n in render.HOOK_NAMES]
    return out + [Target(hooks / "README", render.hooks_readme(), 0o600),
                  Target(env.home / GUARD_GITCONFIG, render.hooks_gitconfig(hooks), 0o600, strict=True)]


def build(cfg: Config, env: Env) -> tuple[list[Target], list[Path]]:
    """Return (targets, stale generated files to remove). Callers holding (Default, list[Identity])
    wrap them: build(Config(d, ids), env)."""
    d, ids = cfg.default, list(cfg.identities)
    for i in ids:
        check(env.home in env.expand(i.file).resolve().parents,
              f"identity {i.id!r}: file must be inside {env.home}")

    extra = [Target(env.expand(i.file), render.identity_gitconfig(i, d, env.expand)) for i in ids]
    signers = _signers(d, ids, env)
    signers_path = env.home / ALLOWED_SIGNERS
    if signers is not None:
        extra.append(Target(signers_path, render.allowed_signers(signers)))
    manifest = sorted(env.tilde(t.path) for t in extra)

    managed = env.home / MANAGED_GITCONFIG
    gitconfig = env.home / ".gitconfig"
    ssh_dir = env.home / ".ssh"
    managed_text = render.managed_gitconfig(
        d, ids, env.expand, str(signers_path) if signers is not None else None, manifest)
    guard: list[Target] = []
    if guard_wanted(cfg, env):
        from .guard import check_chain_to, check_foreign
        check_chain_to(cfg, env, missing_ok=True)   # every render: refuses an unsafe dir, warns about a missing one
        if not guard_installed(env):   # first install through [guard] hooks = true: same refusal as guard install
            check_foreign(cfg, env.home)
        guard = guard_targets(cfg, env)
        managed_text = render.with_guard_include(managed_text, guard_include(env), guard_manifest(env))
        manifest = sorted(manifest + guard_manifest(env))
    targets = [
        Target(managed, managed_text),
        *extra,
        *guard,
        # Last in ~/.gitconfig so it overrides anything hand-written above it.
        Target(gitconfig, render.with_block(
            _read(gitconfig), f"[include]\n{render.INDENT}path = {env.tilde(managed)}", first=False, where="~/.gitconfig")),
        Target(ssh_dir / MANAGED_SSH, render.ssh_config(d, ids), 0o600, strict=True),
        # First in ~/.ssh/config; "Match all" stops a Host block prepended above it from scoping the Include.
        Target(ssh_dir / "config", render.with_block(
            _read(ssh_dir / "config"), f"Match all\nInclude {MANAGED_SSH}", first=True,
            where="~/.ssh/config"), 0o600, strict=True),
    ]
    stale = [env.expand(p) for p in render.manifest_of(_read(managed)) if p not in manifest]
    return targets, stale


def teardown(env: Env) -> tuple[list[Target], list[Path]]:
    """Undo build without needing the config: (files to strip our block from, generated files)."""
    managed = env.home / MANAGED_GITCONFIG
    targets = []
    for path, where in ((env.home / ".gitconfig", "~/.gitconfig"), (env.home / ".ssh/config", "~/.ssh/config")):
        text = _read(path)
        if render.has_block(text):
            # A file holding nothing but our block was created by apply: remove it (never a symlink's target).
            rest = render.strip_block(text, where)
            targets.append(Target(path, rest if rest or path.is_symlink() else None))
    generated = [env.expand(p) for p in render.manifest_of(_read(managed))]
    # The guard files too, even when the manifest lost them; diff removes only files gitpro generated.
    return targets, [managed, env.home / ".ssh" / MANAGED_SSH, *generated, *guard_paths(env)]
