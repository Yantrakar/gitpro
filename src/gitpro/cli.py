"""Per-remote git identities (name, email, SSH key, commit signing) from one TOML file.

Each identity is bound to a host and owners (GitHub users/orgs, GitLab groups);
git picks it from the repo's remote URL via includeIf hasconfig (git >= 2.36).
Every command is idempotent: a second 'apply' changes nothing.

config file (first match wins):
  -c PATH, $GITPRO_CONFIG, ~/.config/gitpro.toml

first run on a new machine:
  gitpro setup --write     # guided configuration; confirm before saving
  gitpro plan              # review every change as a diff
  gitpro apply -y          # explicitly write the reviewed changes
  gitpro keys              # public keys to register with each account
  gitpro scan              # every repo and the identity git uses there

-- for developers (not part of --help) --
CALLER_UMASK is the umask gitpro was started with. It is None until main() runs; main() saves it and
then tightens the process umask to 077, so every file gitpro writes is private. A command that creates
files the user owns in the usual way (clone, init of a work tree) passes umask=caller_umask() to
subprocess.run. Code that runs outside main() (tests, the hot path, library callers) uses
caller_umask() as well: it returns the saved value, else hot.CALLER_UMASK, else the current umask,
read without changing it.
"""
from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple

from . import __version__, config, jsonout
from .args import CAPABILITIES, capability, common, confirm, fail, guarded, preview_only, preview_result

PROG = "gitpro"
MIN_GIT = (2, 36)

# One line per package, alphabetical by module name. 01 writes the whole list up front; a module that
# is not built yet is skipped (see _extension_modules). No later package edits this file.
EXTENSIONS = [
    "gitpro.agents",        # 10: agent-check, agent
    "gitpro.audit",         # 08: audit
    "gitpro.changes",       # approved persistent agent changes
    "gitpro.clone",         # 07: clone
    "gitpro.completion",    # 16: completion
    "gitpro.devcontainer",  # 13: devcontainer
    "gitpro.discover",      # 04: discover, init --discover
    "gitpro.doctor",        # 16: doctor
    "gitpro.envcmd",        # 12: env
    "gitpro.forge",         # 05: forge (01 ships the module; 05 adds the command)
    "gitpro.guard",         # 06: guard
    "gitpro.health",        # 09: auth, keys --health/--upload
    "gitpro.lost",          # 03: lost
    "gitpro.mcp",           # 11: mcp
    "gitpro.operations",    # versioned agent preflight and scoped execution
    "gitpro.policy",        # 14: policy, team, check --policy
    "gitpro.prompt",        # 16: prompt
    "gitpro.resolve",       # 01: why
    "gitpro.scanroots",     # 02: scan -c/--roots
    "gitpro.setup",         # guided configuration
]

CALLER_UMASK: Optional[int] = None  # the umask gitpro was started with; main() then tightens it to 077

Subs = Dict[str, argparse.ArgumentParser]


def caller_umask() -> int:
    """The umask gitpro was started with: CALLER_UMASK once main() (or hot.main()) ran, else the
    current process umask, read without changing it."""
    if CALLER_UMASK is not None:
        return CALLER_UMASK
    from . import hot
    if hot.CALLER_UMASK is not None:
        return hot.CALLER_UMASK
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    cur = os.umask(0o077)   # no /proc (macOS): the only portable read sets it; restore at once
    os.umask(cur)
    return cur


def _extension_modules() -> Iterator:
    """Import every listed module; skip the ones that do not exist yet. Only a ModuleNotFoundError for
    that exact name is skipped, so a broken import inside a module still fails loudly. Yields modules
    that have register() or extend() (or both)."""
    for name in EXTENSIONS:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name == name:
                continue
            raise
        if hasattr(mod, "register") or hasattr(mod, "extend"):
            yield mod


def extend_command(subs: Subs, name: str, add_flags, handler) -> None:
    """Let a package add flags to an existing command without editing cmd_<name>.
    add_flags(parser) adds the options; handler(args) returns an int to take over, or None to fall
    through to the original command."""
    p = subs[name]
    add_flags(p)
    orig = p.get_default("func")

    def func(args):
        rc = handler(args)
        return orig(args) if rc is None else rc
    p.set_defaults(func=func)


def build_parser() -> Tuple[argparse.ArgumentParser, Subs]:
    com, conf = common(), confirm()
    doc = __doc__.split("\n-- for developers")[0].rstrip("\n")
    p = argparse.ArgumentParser(prog=PROG, description=doc.split("\n\n")[0],
                                epilog="\n\n".join(doc.split("\n\n")[1:]),
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="store_true", help="print the version (with --json: and the capabilities)")
    p.add_argument("--json", dest="version_json", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sp = sub.add_parser("init", parents=[conf], help="preview a config example (-y creates it; never overwrites)")
    sp.add_argument("path", nargs="?", type=Path, help=f"default: ~/{config.USER_CONFIG}")
    sp.set_defaults(func=cmd_init)
    sub.add_parser("plan", parents=[com], help="show what apply would change, with diffs").set_defaults(func=cmd_plan)
    sub.add_parser("apply", parents=[com, conf], help="write the changes (backs up hand-written files)") \
        .set_defaults(func=cmd_apply)
    sp = sub.add_parser("check", parents=[com], help="exit 1 if apply would change anything (for CI/ansible)")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_check)
    k = sub.add_parser("keys", parents=[com], help="list SSH keys and the accounts to register them with")
    k.add_argument("--generate", action="store_true", help="create missing keys (ed25519, never overwrites)")
    k.add_argument("--no-passphrase", action="store_true", help="with --generate: empty passphrase (not advised)")
    k.set_defaults(func=cmd_keys)
    sp = sub.add_parser("which", help="show which identity git uses in a repo")
    sp.add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_which)
    s = sub.add_parser("scan", parents=[conf], help="list repos and the identity git uses in each")
    s.add_argument("dirs", nargs="*", type=Path, metavar="DIR", help="where to look (default: your home directory)")
    s.add_argument("--fix", action="store_true", help="remove repo-level user.name/user.email that override gitpro")
    s.add_argument("--suggest", action="store_true", help="print [[identity]] blocks that keep each repo's identity")
    s.add_argument("-v", "--verbose", action="store_true", help="full paths, and the remote owners behind each identity")
    s.add_argument("--repo", action="append", type=Path, metavar="PATH",
                   help="only this repo, not the repos under it (repeatable)")
    s.add_argument("--json", action="store_true", help="machine-readable output (with --fix: what was removed)")
    s.add_argument("--plain", action="store_true", help="with --fix: a y/N question instead of the interactive list")
    s.set_defaults(func=cmd_scan)
    sp = sub.add_parser("pin", parents=[conf], help="preview pinning the repo identity (-y writes it)")
    sp.add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    sp.set_defaults(func=cmd_pin)
    sp = sub.add_parser("unpin", parents=[conf], help="preview removing the pin (-y removes it)")
    sp.add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    sp.set_defaults(func=cmd_unpin)
    u = sub.add_parser("uninstall", parents=[conf],
                       help="remove everything apply wrote (keeps your config file and SSH keys)")
    u.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    u.set_defaults(func=cmd_uninstall)
    capability("which.json", "check.json", "dry-run-defaults.v1")

    mods = list(_extension_modules())
    for mod in mods:
        reg = getattr(mod, "register", None)
        if reg:
            reg(sub, com, conf)
    for mod in mods:  # second pass: flags on existing commands
        ext = getattr(mod, "extend", None)
        if ext:
            ext(sub.choices)
    return p, sub.choices


def _env(args: argparse.Namespace):
    from . import plan
    return plan.Env(args.home.expanduser().resolve())


def _load_all(args: argparse.Namespace):
    """(Config, plan.Env) for a command that needs the config; checks the git version first."""
    from . import resolve
    resolve.require_git()
    env = _env(args)
    path, how = config.find(args.config, env.home)
    return config.load_all(path, how), env


def _warn(cfg, env) -> None:
    from . import lint, view
    for msg in lint.warnings(cfg.default, list(cfg.identities), env):
        view.warn(msg)


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise config.ConfigError("no terminal to confirm on; re-run with --yes")
    from . import view
    return view.confirm(question)


def _changes(cfg, env) -> list:
    from . import files, plan
    return files.diff(*plan.build(cfg, env))


def cmd_version(args) -> int:
    if getattr(args, "version_json", False):
        jsonout.emit("version", {"version": __version__, "python": ".".join(map(str, sys.version_info[:3])),
                                 "capabilities": sorted(CAPABILITIES)})
    else:
        print(f"{PROG} {__version__}")
    return 0


def cmd_status(p: argparse.ArgumentParser, subs: Subs) -> int:
    p.print_help()
    print("\ncommand options (COMMAND -h for details):")
    for s in subs.values():
        print("  " + " ".join(s.format_usage().split()[1:]))
    print()
    try:
        cfg, env = _load_all(argparse.Namespace(config=None, home=Path.home()))
        n = len(_changes(cfg, env))
    except config.ConfigError as e:
        print(f"status: {e}")
        return 0
    state = "up to date" if not n else f"{n} pending change(s) -- review with 'plan', write with 'apply'"
    print(f"status: {cfg.path} ({cfg.how}), {len(cfg.identities)} identities, {state}")
    return 0


def cmd_init(args) -> int:
    from importlib import resources

    from . import view
    target = (args.path or Path.home() / config.USER_CONFIG).expanduser()
    if target.exists():
        view.ok(f"{target} already exists, left unchanged")
        return 0
    text = resources.files(__package__).joinpath("example.toml").read_text()
    if preview_only(args):
        print(f"Would create {target}:\n{text}")
        return preview_result(args, "init", {"path": str(target), "content": text})
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    extra = "" if args.path is None else f" -c {target}"
    view.ok(f"created {target}")
    view.step(f"replace the placeholders in it, then run: {PROG} plan{extra}")
    return 0


def cmd_plan(args) -> int:
    from . import files
    cfg, env = _load_all(args)
    print(f"config: {cfg.path} ({cfg.how})")
    files.show(_changes(cfg, env), diffs=True)
    _warn(cfg, env)
    return 0


def cmd_apply(args) -> int:
    from . import files, keys, view
    cfg, env = _load_all(args)
    print(f"config: {cfg.path} ({cfg.how})")
    changes = _changes(cfg, env)
    files.show(changes, diffs=preview_only(args))
    if preview_only(args):
        _warn(cfg, env)
        return preview_result(args, "apply", {})
    if changes:
        if not _confirm(f"apply {len(changes)} change(s)? ('{PROG} plan' shows the diffs)", args.yes):
            view.aborted()
            return 1
        files.apply(changes)
        view.ok(f"applied {len(changes)} change(s)")
    _warn(cfg, env)
    if keys.inventory(cfg.default, list(cfg.identities)):
        view.step(f"SSH keys: '{PROG} keys' lists the public keys to register with each account")
    return 0


def cmd_check(args) -> int:
    from . import files
    cfg, env = _load_all(args)
    changes = _changes(cfg, env)
    if getattr(args, "json", False):
        jsonout.emit("check", {
            "changes": [{"action": c.action, "path": str(c.path),
                         "mode": f"{c.mode:04o}" if c.action in ("create", "chmod") else None} for c in changes],
            "up_to_date": not changes})
    else:
        files.show(changes, diffs=False)
    return 1 if changes else 0


def cmd_keys(args) -> int:
    from . import keys
    cfg, env = _load_all(args)
    d, ids = cfg.default, list(cfg.identities)
    inv = keys.inventory(d, ids)
    if not inv:
        print("no SSH keys configured (set ssh_key in [default] or an identity)")
        return 0
    if args.generate and keys.generate(inv, env.expand, passphrase=not args.no_passphrase):
        print(f"keys created -- run '{PROG} apply' so the signing config picks them up")
    keys.report(inv, env.expand)
    for h in d.ssh_hosts:
        print(f"test: ssh -T git@{h}")
    for i in ids:
        if i.ssh_alias:
            print(f"test: ssh -T {i.ssh_user}@{i.ssh_alias}")
    print("first connect: compare the host key fingerprint with the provider's published one")
    return 0


WHICH_KEYS = ("user.name", "user.email", "user.signingkey", "commit.gpgsign", "core.sshcommand")


def cmd_which(args) -> int:
    from . import resolve
    if resolve.is_repo(Path(args.dir)) is None:
        raise config.ConfigError(f"{args.dir}: not a git repository")
    eff = resolve.effective(Path(args.dir))
    settings = dict(zip(WHICH_KEYS, (eff.name, eff.email, eff.signingkey, eff.gpgsign, eff.sshcommand)))
    if getattr(args, "json", False):
        jsonout.emit("which", {
            "repo": str(resolve.is_repo(Path(args.dir))), "remotes": {n: resolve.redact_url(u) for n, u in eff.remotes.items()},
            "settings": {k: {"value": s.value, "origin": s.origin, "scope": s.scope} for k, s in settings.items()}})
        return 0
    print("remotes:" + ("".join(f"\n  remote.{n}.url {resolve.redact_url(u)}" for n, u in eff.remotes.items()) if eff.remotes
                        else " (none)"))
    for key, s in settings.items():
        origin = s.origin or ""
        if origin.startswith("file:"):
            origin = "file:" + resolve._tilde(origin[5:])
        print(f"  {key:16} {s.value if s.value is not None else '(unset)'}" + (f"   [{origin}]" if origin else ""))
    return 0


def cmd_scan(args) -> int:
    from . import plan, scan, view
    home = Path.home().resolve()
    managed = str(home / plan.MANAGED_GITCONFIG)

    def show(p) -> str:
        p = Path(p)
        return "~" if p == home else plan.Env(home).tilde(p)

    def label(src: str) -> str:
        return "[default]" if src == managed else show(src) if src.startswith("/") else src

    ask = args.fix and not args.yes and not args.json
    if ask and not sys.stdin.isatty():
        raise config.ConfigError("no terminal to confirm on; re-run with --yes")
    # scanroots (02) sets roots_effective, exclude, roots_source and roots_hint; --repo ignores them.
    exclude, excluded = getattr(args, "exclude", ()), set()
    if args.repo:
        roots = [p.expanduser().resolve() for p in args.repo]
        paths = roots
    else:
        roots = getattr(args, "roots_effective", None) or [d.expanduser().resolve() for d in args.dirs] or [home]
        paths = scan.find(roots, exclude=exclude, excluded=excluded)
    repos, failed = scan.inspect_all(paths)
    rep = scan.report(roots, repos, failed, label)
    rep.update(exclude=list(exclude), skipped_excluded=len(excluded),
               roots_source=getattr(args, "roots_source", "args" if args.repo or args.dirs else "default"))
    fixable = [r for r in repos if any(k in r.beneath for k in r.local)]

    if ask and fixable and not args.plain and sys.stdout.isatty():
        try:
            from . import tui
        except ImportError:  # the optional textual extra is not installed
            pass
        else:
            chosen = tui.choose(rep, show)
            if not chosen:
                view.aborted()
                return 1
            return _fix(args, [r for r in fixable if str(r.path) in chosen], home)

    if args.json and not args.fix:
        jsonout.emit("scan", rep)
        return 0
    if not args.json:
        where = "".join(f" --repo {_sh(show(p))}" for p in roots) if args.repo else \
            "".join(f" {_sh(show(d))}" for d in roots if args.dirs)
        view.scan(rep, show, verbose=args.verbose, suggest=args.suggest, fix=args.fix, prog=PROG,
                  again=f"{PROG} scan --fix{where}", exact=bool(args.repo))
        if getattr(args, "roots_hint", None) and not args.fix:
            print()
            view.step(args.roots_hint)
    if not args.fix:
        return 0
    if not fixable:
        if args.json:
            jsonout.emit("scan", {"removed": [], "undo": None})
        else:
            print("\n✓ nothing to fix")
        return 0
    n = sum(k in r.beneath for r in fixable for k in r.local)
    if ask:
        print()
        if not _confirm(f"Remove {view.plural(n, 'setting')} from {view.plural(len(fixable), 'repo')}?", False):
            view.aborted()
            return 1
    return _fix(args, fixable, home)


def _sh(path: str) -> str:
    """Quote a path for the shell, leaving a leading ~ unquoted so the shell still expands it."""
    import shlex
    return path if path == "~" else "~/" + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)


def _fix(args, repos: list, home: Path) -> int:
    from . import plan, scan
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state") / PROG
    pairs, undo = scan.fix(repos, state, PROG)
    if args.json:
        jsonout.emit("scan", {"removed": [{"path": str(r.path), "key": k, "value": r.local[k]} for r, k in pairs],
                              "undo": str(undo) if undo else None})
    elif pairs:
        from . import view
        env = plan.Env(home)
        view.fixed([(env.tilde(r.path), k, r.local[k], r.beneath[k]) for r, k in pairs], env.tilde(undo))
    return 0


def cmd_pin(args) -> int:
    from . import pin, view
    name, email, path = pin.pin(args.dir, dry_run=preview_only(args))
    if preview_only(args):
        print(f"Would pin {name} <{email}> in {path}")
        return preview_result(args, "pin", {})
    view.ok(f"pinned {name} <{email}> in {path}")
    got = subprocess.run(["git", "-C", args.dir, "config", "--show-origin", "--get", "user.email"],
                         capture_output=True, text=True).stdout.strip()
    if not got.endswith("\t" + email):
        view.warn(f"git resolves {got!r}; a repo-local setting overrides the pin")
    view.step("signing and SSH key selection are not pinned; re-run after changing the config")
    return 0


def cmd_unpin(args) -> int:
    from . import pin, view
    changed = pin.unpin(args.dir, dry_run=preview_only(args))
    if preview_only(args):
        print("Would remove the pin" if changed else "Not pinned, nothing to do")
        return preview_result(args, "unpin", {})
    view.ok("unpinned" if changed else "not pinned, nothing to do")
    return 0


def cmd_uninstall(args) -> int:
    from . import files, plan, view
    changes = files.diff(*plan.teardown(_env(args)))
    files.show(changes, diffs=preview_only(args))
    if preview_only(args):
        return preview_result(args, "uninstall", {})
    if not changes:
        return 0
    if not _confirm(f"undo {len(changes)} change(s)?", args.yes):
        view.aborted()
        return 1
    files.apply(changes)
    plan.prune_hooks_dir(_env(args))   # 06: the guard's hooks dir, when empty
    view.ok(f"removed {len(changes)} change(s); your config file and SSH keys were left in place")
    return 0


def _fail(msg: str, args) -> int:
    return fail(msg, args)


def main(argv: Optional[list] = None) -> int:
    global CALLER_UMASK
    if os.name != "posix":
        print(f"✗ {PROG} supports Linux and macOS only", file=sys.stderr)
        return 2
    p, subs = build_parser()
    args = p.parse_args(argv)
    if args.version:
        return cmd_version(args)
    if os.geteuid() == 0:
        return _fail("run as the user who owns these identities, not root", args)
    CALLER_UMASK = os.umask(0o077)
    if args.command is None:
        return guarded(lambda a: cmd_status(p, subs), args)
    return guarded(args.func, args)
