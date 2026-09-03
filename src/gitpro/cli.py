"""Per-remote git identities (name, email, SSH key, commit signing) from one TOML file.

Each identity is bound to a host and owners (GitHub users/orgs, GitLab groups);
git picks it from the repo's remote URL via includeIf hasconfig (git >= 2.36).
Every command is idempotent: a second 'apply' changes nothing.

config file (first match wins):
  -c PATH, $GITPRO_CONFIG, ~/.config/gitpro.toml

first run on a new machine:
  gitpro init              # then edit ~/.config/gitpro.toml
  gitpro keys --generate   # create missing SSH keys (optional)
  gitpro plan              # review every change as a diff
  gitpro apply             # write the changes
  gitpro keys              # public keys to register with each account
  gitpro scan              # every repo and the identity git uses there
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import __version__, config, files, keys, lint, pin, plan, scan, view

PROG = "gitpro"
MIN_GIT = (2, 36)


def build_parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", type=Path, metavar="PATH", help="config file (default: see above)")
    common.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    confirm = argparse.ArgumentParser(add_help=False)
    confirm.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation (needed without a terminal)")

    p = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0],
                                epilog="\n\n".join(__doc__.split("\n\n")[1:]),
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("init", help="create a config from the commented example (never overwrites)") \
        .add_argument("path", nargs="?", type=Path, help=f"default: ~/{config.USER_CONFIG}")
    sub.add_parser("plan", parents=[common], help="show what apply would change, with diffs")
    sub.add_parser("apply", parents=[common, confirm], help="write the changes (backs up hand-written files)")
    sub.add_parser("check", parents=[common], help="exit 1 if apply would change anything (for CI/ansible)")
    k = sub.add_parser("keys", parents=[common], help="list SSH keys and the accounts to register them with")
    k.add_argument("--generate", action="store_true", help="create missing keys (ed25519, never overwrites)")
    k.add_argument("--no-passphrase", action="store_true", help="with --generate: empty passphrase (not advised)")
    sub.add_parser("which", help="show which identity git uses in a repo") \
        .add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    s = sub.add_parser("scan", parents=[confirm], help="list repos and the identity git uses in each")
    s.add_argument("dirs", nargs="*", type=Path, metavar="DIR", help="where to look (default: your home directory)")
    s.add_argument("--fix", action="store_true", help="remove repo-level user.name/user.email that override gitpro")
    s.add_argument("--suggest", action="store_true", help="print [[identity]] blocks that keep each repo's identity")
    s.add_argument("-v", "--verbose", action="store_true", help="full paths, and the remote owners behind each identity")
    s.add_argument("--repo", action="append", type=Path, metavar="PATH",
                   help="only this repo, not the repos under it (repeatable)")
    s.add_argument("--json", action="store_true", help="machine-readable output (with --fix: what was removed)")
    s.add_argument("--plain", action="store_true", help="with --fix: a y/N question instead of the interactive list")
    sub.add_parser("pin", help="copy the repo's name and email into its .git (for dev containers)") \
        .add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    sub.add_parser("unpin", help="undo pin") \
        .add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    u = sub.add_parser("uninstall", parents=[confirm],
                       help="remove everything apply wrote (keeps your config file and SSH keys)")
    u.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    return p, sub.choices


def _git_version() -> tuple[int, int]:
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise config.ConfigError("git not found") from None
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m[1]), int(m[2])) if m else (0, 0)


def _env(args: argparse.Namespace) -> plan.Env:
    return plan.Env(args.home.expanduser().resolve())


def _load(args: argparse.Namespace):
    ver = _git_version()
    config.check(ver >= MIN_GIT, f"git {ver[0]}.{ver[1]} is too old; includeIf hasconfig needs "
                                 f"{MIN_GIT[0]}.{MIN_GIT[1]}+ (Ubuntu: ppa:git-core/ppa, macOS: brew install git)")
    env = _env(args)
    path, how = config.find(args.config, env.home)
    d, ids = config.load(path)
    return d, ids, env, path, how


def _warn(d, ids, env) -> None:
    for msg in lint.warnings(d, ids, env):
        view.warn(msg)


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise config.ConfigError("no terminal to confirm on; re-run with --yes")
    return view.confirm(question)


def cmd_status(p: argparse.ArgumentParser, subs: dict[str, argparse.ArgumentParser]) -> int:
    p.print_help()
    print("\ncommand options (COMMAND -h for details):")
    for s in subs.values():
        print("  " + " ".join(s.format_usage().split()[1:]))
    print()
    try:
        d, ids, env, path, how = _load(argparse.Namespace(config=None, home=Path.home()))
        n = len(files.diff(*plan.build(d, ids, env)))
    except config.ConfigError as e:
        print(f"status: {e}")
        return 0
    state = "up to date" if not n else f"{n} pending change(s) -- review with 'plan', write with 'apply'"
    print(f"status: {path} ({how}), {len(ids)} identities, {state}")
    return 0


def cmd_init(args) -> int:
    target = (args.path or Path.home() / config.USER_CONFIG).expanduser()
    if target.exists():
        view.ok(f"{target} already exists, left unchanged")
        return 0
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = resources.files(__package__).joinpath("example.toml").read_text()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    extra = "" if args.path is None else f" -c {target}"
    view.ok(f"created {target}")
    view.step(f"replace the placeholders in it, then run: {PROG} plan{extra}")
    return 0


def cmd_plan(args) -> int:
    d, ids, env, path, how = _load(args)
    print(f"config: {path} ({how})")
    files.show(files.diff(*plan.build(d, ids, env)), diffs=True)
    _warn(d, ids, env)
    return 0


def cmd_apply(args) -> int:
    d, ids, env, path, how = _load(args)
    print(f"config: {path} ({how})")
    changes = files.diff(*plan.build(d, ids, env))
    files.show(changes, diffs=False)
    if changes:
        if not _confirm(f"apply {len(changes)} change(s)? ('{PROG} plan' shows the diffs)", args.yes):
            view.aborted()
            return 1
        files.apply(changes)
        view.ok(f"applied {len(changes)} change(s)")
    _warn(d, ids, env)
    if keys.inventory(d, ids):
        view.step(f"SSH keys: '{PROG} keys' lists the public keys to register with each account")
    return 0


def cmd_check(args) -> int:
    d, ids, env, _, _ = _load(args)
    changes = files.diff(*plan.build(d, ids, env))
    files.show(changes, diffs=False)
    return 1 if changes else 0


def cmd_keys(args) -> int:
    d, ids, env, _, _ = _load(args)
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


def cmd_which(args) -> int:
    def git(*a: str) -> str:
        return subprocess.run(["git", "-C", args.dir, *a], capture_output=True, text=True).stdout.strip()

    remotes = git("config", "--get-regexp", r"^remote\..*\.url$")
    print("remotes:" + ("".join(f"\n  {r}" for r in remotes.splitlines()) if remotes else " (none)"))
    for key in ("user.name", "user.email", "user.signingkey", "commit.gpgsign", "core.sshCommand"):
        origin, _, value = git("config", "--show-origin", "--get", key).partition("\t")
        print(f"  {key:16} {value or '(unset)'}" + (f"   [{origin}]" if origin else ""))
    return 0


def cmd_scan(args) -> int:
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
    if args.repo:
        roots = [p.expanduser().resolve() for p in args.repo]
        paths = roots
    else:
        roots = [d.expanduser().resolve() for d in args.dirs] or [home]
        paths = scan.find(roots)
    repos, failed = scan.inspect_all(paths)
    rep = scan.report(roots, repos, failed, label)
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
        print(json.dumps(rep, indent=2))
        return 0
    if not args.json:
        where = "".join(f" --repo {_sh(show(p))}" for p in roots) if args.repo else \
            "".join(f" {_sh(show(d))}" for d in roots if args.dirs)
        view.scan(rep, show, verbose=args.verbose, suggest=args.suggest, fix=args.fix, prog=PROG,
                  again=f"{PROG} scan --fix{where}", exact=bool(args.repo))
    if not args.fix:
        return 0
    if not fixable:
        print(json.dumps({"removed": [], "undo": None}) if args.json else "\n✓ nothing to fix")
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
    return path if path == "~" else "~/" + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)


def _fix(args, repos: list, home: Path) -> int:
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state") / PROG
    pairs, undo = scan.fix(repos, state, PROG)
    if args.json:
        print(json.dumps({"removed": [{"path": str(r.path), "key": k, "value": r.local[k]} for r, k in pairs],
                          "undo": str(undo) if undo else None}, indent=2))
    elif pairs:
        env = plan.Env(home)
        view.fixed([(env.tilde(r.path), k, r.local[k], r.beneath[k]) for r, k in pairs], env.tilde(undo))
    return 0


def cmd_pin(args) -> int:
    name, email, path = pin.pin(args.dir)
    view.ok(f"pinned {name} <{email}> in {path}")
    got = subprocess.run(["git", "-C", args.dir, "config", "--show-origin", "--get", "user.email"],
                         capture_output=True, text=True).stdout.strip()
    if not got.endswith("\t" + email):
        view.warn(f"git resolves {got!r}; a repo-local setting overrides the pin")
    view.step("signing and SSH key selection are not pinned; re-run after changing the config")
    return 0


def cmd_unpin(args) -> int:
    view.ok("unpinned" if pin.unpin(args.dir) else "not pinned, nothing to do")
    return 0


def cmd_uninstall(args) -> int:
    changes = files.diff(*plan.teardown(_env(args)))
    files.show(changes, diffs=False)
    if not changes:
        return 0
    if not _confirm(f"undo {len(changes)} change(s)?", args.yes):
        view.aborted()
        return 1
    files.apply(changes)
    view.ok(f"removed {len(changes)} change(s); your config file and SSH keys were left in place")
    return 0


COMMANDS = {"init": cmd_init, "plan": cmd_plan, "apply": cmd_apply, "check": cmd_check,
            "keys": cmd_keys, "which": cmd_which, "scan": cmd_scan, "pin": cmd_pin, "unpin": cmd_unpin,
            "uninstall": cmd_uninstall}


def main(argv: list[str] | None = None) -> int:
    if os.name != "posix":
        view.error(f"{PROG} supports Linux and macOS only")
        return 2
    p, subs = build_parser()
    args = p.parse_args(argv)
    if os.geteuid() == 0:
        view.error("run as the user who owns these identities, not root")
        return 2
    os.umask(0o077)
    try:
        return COMMANDS[args.command](args) if args.command else cmd_status(p, subs)
    except config.ConfigError as e:
        view.error(str(e))
        return 2
    except subprocess.CalledProcessError as e:
        view.error(f"{' '.join(e.cmd)} failed ({e.returncode})")
        return 2
    except (OSError, UnicodeError) as e:
        view.error(str(e))
        return 2
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        return 130
