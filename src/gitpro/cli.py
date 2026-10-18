"""Per-remote git identities (name, email, SSH key, commit signing) from one TOML file.

Each identity is bound to a host and owners (GitHub users/orgs, GitLab groups);
git picks it from the repo's remote URL via includeIf hasconfig (git >= 2.36).
Every command is idempotent: a second 'setup' changes nothing.

config file (first match wins):
  -c PATH, $GITPRO_CONFIG, ~/.config/gitpro.toml

first run on a new machine:
  pipx install gitpro
  gitpro setup             # asks for your identities, shows everything, asks once, then sets it all up
  gitpro scan              # every repo and the identity git uses there; it shows each fix

problems and questions:
  gitpro report --print    # a redacted draft of a bug report; nothing is sent unless you say so
  https://github.com/Yantrakar/gitpro/issues

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

from . import __version__, config, deprecation, jsonout
from .args import CAPABILITIES, ask as _confirm, capability, common, confirm, dry_run, fail, guarded, preview_only, preview_result
from .util import sh_tilde as _sh

PROG = "gitpro"

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
    "gitpro.report",        # 19: report
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
    sp = sub.add_parser("check", parents=[com], help="CI: check commits and identities against a team policy (--policy FILE, --range A..B, --repo DIR)")
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
    s.add_argument("--lost", action="store_true",
                   help="also list work that is only on this machine: unpushed branches, stashes, dirty trees, "
                        "repos without a remote, stale and empty repos (offline)")
    dry_run(s)
    s.set_defaults(func=cmd_scan)
    sp = sub.add_parser("pin", parents=[com, conf],
                        help="choose a repo's identity: --remote R, --remove (preview; -y writes it)")
    sp.add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    sp.add_argument("--remote", metavar="NAME",
                    help="pin the identity of this remote, with its SSH key and signing (for a repo whose "
                         "remotes belong to different identities)")
    sp.add_argument("--remove", action="store_true", help="remove the pin (preview; -y removes it)")
    dry_run(sp)
    sp.set_defaults(func=cmd_pin)
    sp = sub.add_parser("unpin", parents=[conf], help="preview removing the pin (-y removes it)")
    sp.add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    sp.set_defaults(func=cmd_unpin)
    u = sub.add_parser("uninstall", parents=[conf],
                       help="remove everything setup wrote (keeps your config file and SSH keys)")
    u.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    u.set_defaults(func=cmd_uninstall)
    capability("which.json", "check.json", "dry-run-defaults.v1", "scan.lost", "pin.remove", "deprecations.v1")

    mods = list(_extension_modules())
    for mod in mods:
        reg = getattr(mod, "register", None)
        if reg:
            reg(sub, com, conf)
    for mod in mods:  # second pass: flags on existing commands
        ext = getattr(mod, "extend", None)
        if ext:
            ext(sub.choices)
    _surface(sub)
    return p, sub.choices


# ---------------------------------------------------------------- the 0.4 surface (spec 22 §4, §10)

# `gitpro --help` lists these, in this order; the rest of the registered commands are hidden.
MAIN = ("setup", "why", "scan", "pin", "clone", "audit", "env", "uninstall")
MORE = ("completion", "prompt", "report", "team", "policy", "check", "keys", "auth", "forge", "guard")
# Called by hooks, agents and the extension, never typed.
HIDDEN = ("agent-check", "agent", "mcp", "guard check")
# Deprecated in 0.4.0, removed in 0.5.0: the command and what replaces it (None: nothing does).
DEPRECATED: Dict[str, Optional[str]] = {
    "init": "gitpro setup", "discover": "gitpro setup PATH", "plan": "gitpro setup -n",
    "apply": "gitpro setup -y", "doctor": "gitpro setup -n and gitpro why",
    "which": "gitpro why --json", "lost": "gitpro scan --lost", "unpin": "gitpro pin --remove",
    "devcontainer": 'the VS Code command "Set up devcontainer pin"',
    "agent": "gitpro setup -n", "agent install": "gitpro setup --agent NAME", "agent status": "gitpro setup -n",
    "mcp install": "gitpro setup --agent NAME",
    "preflight": None, "run": None, "operation-status": None, "operation-reconcile": None, "change": None,
}
KEPT = ("agent uninstall", "mcp uninstall")   # under a deprecated or hidden command, but not deprecated
# Deprecated forms of a kept command: (command, what is deprecated, when it was used, replacement, flag to hide).
DEPRECATED_FORMS = (
    # check --policy/--repo/--range is the team policy check CI runs; only the drift check moves to setup -n
    ("check", "check without --policy, --repo or --range",
     lambda a: not (getattr(a, "policy_files", None) or getattr(a, "range", None) is not None
                    or getattr(a, "repo", None)), "gitpro setup -n", None),
    ("keys", "keys --generate", lambda a: getattr(a, "generate", False), "gitpro setup", "--generate"),
    ("keys", "keys --no-passphrase", lambda a: False, None, "--no-passphrase"),   # only with --generate
    ("scan", "scan --suggest", lambda a: getattr(a, "suggest", False), "gitpro setup --add PATH", "--suggest"),
    ("audit", "audit --rewrite", lambda a: getattr(a, "rewrite_flag", False), "gitpro audit --fix", None),
    ("setup", "setup --write", lambda a: getattr(a, "write", False), "gitpro setup", None),
    ("guard install", "guard install without --repo", lambda a: not getattr(a, "repo", None), "gitpro setup", None),
)


def _children(parser: argparse.ArgumentParser):
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


def _find(sub, path: str) -> Optional[argparse.ArgumentParser]:
    parser = None
    for word in path.split():
        action = sub if parser is None else _children(parser)
        parser = action.choices.get(word) if action is not None else None
        if parser is None:
            return None
    return parser


def _hide(action, name: str) -> None:
    """Out of --help; completion skips a parser marked gitpro_hidden."""
    action._choices_actions[:] = [c for c in action._choices_actions if c.dest != name]
    if name in action.choices:
        action.choices[name].gitpro_hidden = True


def _deprecate(parser, old: str, use: Optional[str], when=None) -> None:
    func = parser.get_default("func")
    if func is None:
        return

    def run(args):
        if when is None or when(args):
            if deprecation.silenced():
                pass
            elif jsonout.wants_json(args):
                jsonout.EXTRA["deprecated"] = deprecation.field(use)
            else:
                deprecation.warn(f"gitpro {old}", use)
        try:
            return guarded(func, args)          # an error's JSON object carries the field too
        finally:
            jsonout.EXTRA.pop("deprecated", None)   # in-process callers (MCP) run several commands
    parser.set_defaults(func=run)


def _deprecate_tree(sub, path: str, use: Optional[str]) -> None:
    parser = _find(sub, path)
    if parser is None:
        return
    _deprecate(parser, path, use)
    nested = _children(parser)
    for name, child in (nested.choices.items() if nested is not None else ()):
        full = f"{path} {name}"
        if full not in DEPRECATED and full not in KEPT:
            _deprecate_tree(sub, full, use)


def _surface(sub) -> None:
    """Hide, order and deprecate the registered commands (spec 22 §4, §10.3)."""
    for path, use in DEPRECATED.items():
        _deprecate_tree(sub, path, use)
    for path, old, when, use, flag in DEPRECATED_FORMS:
        parser = _find(sub, path)
        if parser is None:
            continue
        _deprecate(parser, old, use, when)
        for a in parser._actions:
            if flag in a.option_strings:
                a.help = argparse.SUPPRESS
    for path in list(DEPRECATED) + list(HIDDEN):
        *parent, name = path.split()
        owner = _find(sub, " ".join(parent)) if parent else None
        action = sub if not parent else _children(owner) if owner is not None else None
        if action is not None:
            _hide(action, name)
    shown = {c.dest: c for c in sub._choices_actions}
    first = [shown.pop(n) for n in MAIN if n in shown]
    more = [shown.pop(n) for n in MORE if n in shown]
    sub._choices_actions[:] = first + list(shown.values()) + \
        ([argparse._SubParsersAction._ChoicesPseudoAction("more:", (), "")] if more else []) + more


def shown_commands(sub) -> list:
    """The names `gitpro --help` lists, in its order."""
    return [c.dest for c in sub._choices_actions if c.dest in sub.choices]


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
    for name in shown_commands(_children(p)):
        print("  " + " ".join(subs[name].format_usage().split()[1:]))
    print()
    try:
        cfg, env = _load_all(argparse.Namespace(config=None, home=Path.home()))
        n = len(_changes(cfg, env))
    except config.ConfigError as e:
        print(f"status: {e}")
        return 0
    state = "up to date" if not n else f"{n} pending change(s) -- see them with 'gitpro setup -n', write them " \
        "with 'gitpro setup -y'"
    print(f"status: {cfg.path} ({cfg.how}), {len(cfg.identities)} identities, {state}")
    return 0


def cmd_init(args) -> int:
    from importlib import resources

    from . import files, view
    target = (args.path or Path.home() / config.USER_CONFIG).expanduser()
    if target.exists():
        view.ok(f"{target} already exists, left unchanged")
        return 0
    text = resources.files(__package__).joinpath("example.toml").read_text()
    if preview_only(args):
        print(f"Would create {target}:\n{text}")
        return preview_result(args, "init", {"path": str(target), "content": text})
    files.atomic_write(target, text, 0o600, overwrite=False)   # no half-written config for the next init to refuse
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


def which_data(path: Path) -> dict:
    """`which --json` without the envelope: repo, remotes and the five settings with their origin."""
    from . import resolve
    repo = resolve.is_repo(path)
    if repo is None:
        raise config.ConfigError(f"{path}: not a git repository")
    eff = resolve.effective(path)
    settings = dict(zip(WHICH_KEYS, (eff.name, eff.email, eff.signingkey, eff.gpgsign, eff.sshcommand)))
    return {"repo": str(repo), "remotes": {n: resolve.redact_url(u) for n, u in eff.remotes.items()},
            "settings": {k: {"value": s.value, "origin": s.origin, "scope": s.scope} for k, s in settings.items()}}


def cmd_which(args) -> int:
    from . import resolve
    if resolve.is_repo(Path(args.dir)) is None:
        raise config.ConfigError(f"{args.dir}: not a git repository")
    if getattr(args, "json", False):
        jsonout.emit("which", which_data(Path(args.dir)))
        return 0
    eff = resolve.effective(Path(args.dir))
    settings = dict(zip(WHICH_KEYS, (eff.name, eff.email, eff.signingkey, eff.gpgsign, eff.sshcommand)))
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

    if args.lost and args.fix:
        raise config.ConfigError("--lost lists work only you can sort out, so it does not go with --fix; "
                                 "run gitpro scan --lost without --fix")
    # -n shows the fix and changes nothing; --json without -y still writes (00-existing, known issue 2)
    preview = bool(args.fix and args.dry_run and preview_only(args))
    if preview:
        args.fix = False
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
    try:
        cfg = config.load_all(*config.find(getattr(args, "config", None), home))
    except config.ConfigError:
        cfg = None                   # scan works without a config; the remote check needs one
    rep.update(scan.checks(repos, cfg))
    rep.update(exclude=list(exclude), skipped_excluded=len(excluded),
               roots_source=getattr(args, "roots_source", "args" if args.repo or args.dirs else "default"))
    pairs = scan.fixes(repos)
    fixable = list({id(r): r for r, _ in pairs}.values())
    lost_rep = _lost(roots, exclude, cfg, paths, rep["roots_source"]) if args.lost else None
    if lost_rep is not None:
        rep["lost"] = {k: v for k, v in lost_rep.items() if k not in ("ok", "command")}

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

    lost_found = int(lost_rep is not None and not lost_rep["ok"])   # 1, as `lost` exits
    if args.json and not args.fix:
        jsonout.emit("scan", dict(rep, dry_run=True) if preview else rep)
        return lost_found
    if not args.json:
        where = "".join(f" --repo {_sh(show(p))}" for p in roots) if args.repo else \
            "".join(f" {_sh(show(d))}" for d in roots if args.dirs)
        view.scan(rep, show, verbose=args.verbose, suggest=args.suggest, fix=args.fix, prog=PROG,
                  again=f"{PROG} scan --fix{where}", exact=bool(args.repo))
        if not args.fix:
            view.attention(rep, show)
        if lost_rep is not None:
            from . import lost
            for w in lost_rep["warnings"]:
                print(f"⚠ {w}", file=sys.stderr)
            print()
            print(lost.render(lost_rep, args.verbose))
        if getattr(args, "roots_hint", None) and not args.fix:
            print()
            view.step(args.roots_hint)
    if not args.fix:
        return lost_found
    if not fixable:
        if args.json:
            jsonout.emit("scan", {"removed": [], "undo": None})
        else:
            print("\n✓ nothing to fix")
        return 0
    n = len(pairs)
    if ask:
        print()
        if not _confirm(f"Remove {view.plural(n, 'setting')} from {view.plural(len(fixable), 'repo')}?", False):
            view.aborted()
            return 1
    return _fix(args, fixable, home)


def _lost(roots, exclude, cfg, paths, source) -> dict:
    """lost's offline checks for the repos scan found (spec 22 §7)."""
    from . import lost
    scan_cfg = getattr(cfg, "scan", None)
    stale = getattr(scan_cfg, "stale_days", None) or 180
    return lost.collect(roots, exclude, cfg, online=False, kinds=set(lost.OFFLINE_KINDS), stale_days=stale,
                        repos=paths, roots_source=source)


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
    from . import pin, resolve, view
    if args.remove:
        if args.remote is not None:
            raise config.ConfigError("choose one: --remove takes the pin out, --remote sets one")
        return cmd_unpin(args)
    remote, cfg = args.remote, None
    if remote is None:
        try:
            cfg, _ = _load_all(args)
        except config.ConfigError:
            cfg = None                    # no usable config: a plain pin needs none
        rows = resolve.conflicting(cfg, resolve._remote_urls(Path(args.dir))) if cfg else []
        if rows:
            remote = _choose_remote(rows, args)
            if remote is None:
                return 2
    if remote is not None:
        if cfg is None:
            cfg, _ = _load_all(args)
        i, path, warnings = pin.pin_remote(args.dir, cfg, remote, _env(args).expand, dry_run=preview_only(args))
        for w in warnings:
            view.warn(w)
        if preview_only(args):
            print(f"Would pin {i.id} ({i.name} <{i.email}>), the identity of {remote}, in {path}")
            return preview_result(args, "pin", {})
        view.ok(f"pinned {i.id} ({i.name} <{i.email}>), the identity of {remote}, with its SSH key and signing, "
                f"in {path}")
        view.step(f"re-run 'gitpro pin --remote {remote}' after changing the config")
        return 0
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


def _choose_remote(rows, args):
    """The remote to pin in a repo whose remotes belong to different identities: asked on a
    terminal, else an error that names --remote."""
    from . import view
    names = list(dict.fromkeys(n for n, _, _ in rows))
    shown = ", ".join(f"{n} → {i.id}" for n, i, _ in rows)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        view.error(f"the remotes belong to different identities ({shown}); choose one: "
                   f"gitpro pin --remote <{'|'.join(names)}>")
        return None
    from rich.prompt import Prompt
    view.warn(f"the remotes belong to different identities ({shown})")
    return Prompt.ask("Which remote does this repo's work go to", choices=names, default=names[0])


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


def main(argv: Optional[list] = None) -> int:
    global CALLER_UMASK
    if os.name != "posix":
        print(f"✗ {PROG} supports Linux and macOS only", file=sys.stderr)
        return 2
    jsonout.EXTRA.clear()
    p, subs = build_parser()
    args = p.parse_args(argv)
    if args.version:
        return cmd_version(args)
    if os.geteuid() == 0:
        return fail("run as the user who owns these identities, not root", args)
    CALLER_UMASK = os.umask(0o077)
    if args.command is None:
        return guarded(lambda a: cmd_status(p, subs), args)
    return guarded(args.func, args)
