"""gitpro setup: one command for a machine. The first run asks for the identities; every run then
brings the machine in line with the config: the config file, missing SSH keys, the Git and SSH files,
the commit guard, and the AI agent's hook and MCP server. One preview, one yes. It never edits a repo
and never contacts an account: 'gitpro scan' shows the repos that need a fix (spec 21)."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import config
from .config import ConfigError, check

Snapshot = Optional[Tuple[bytes, os.stat_result]]
# agents.INSTALL_CHOICES + mcp.AGENTS (a test keeps them in step), and none
AGENTS = ("claude", "claude-desktop", "codex", "cursor", "gemini", "opencode", "none")
TITLE = {"claude": "Claude Code", "claude-desktop": "Claude Desktop", "codex": "Codex", "cursor": "Cursor",
         "gemini": "Gemini CLI", "opencode": "opencode"}
QUESTION = "Apply these changes?"


def register(sub, common, confirm) -> None:
    from .args import capability
    p = sub.add_parser("setup", parents=[common],
                       help="set up this machine: config, SSH keys, Git, SSH, the commit guard and your AI agent")
    p.add_argument("-y", "--yes", action="store_true",
                   help="apply without asking (the first run still asks for your identities)")
    p.add_argument("--add", action="store_true", help="add identities to an existing config")
    p.add_argument("--agent", action="append", choices=AGENTS, metavar="NAME",
                   help=f"set up this AI agent's hook and MCP server ({', '.join(AGENTS[:-1])}); repeatable; "
                        "'none' sets up no agent (default: Claude Code when it is installed)")
    p.add_argument("--no-passphrase", action="store_true",
                   help="create missing SSH keys without a passphrase (not advised)")
    p.add_argument("--write", action="store_true", help=argparse.SUPPRESS)   # before 0.4 setup saved only with it
    p.set_defaults(func=cmd_setup)
    capability("setup")


def _snapshot(path: Path) -> Snapshot:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ConfigError(f"cannot open {path}: {e}; setup requires a regular file, not a symlink") from None
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        check(stat.S_ISREG(info.st_mode), f"{path}: setup requires a regular file")
        check(info.st_uid == os.getuid() and not info.st_mode & 0o022,
              f"{path}: must be owned by you and not group/world-writable")
        return f.read(), info


def _same(before: Snapshot, after: Snapshot) -> bool:
    if before is None or after is None:
        return before is after
    a, b = before[1], after[1]
    return before[0] == after[0] and (a.st_dev, a.st_ino, a.st_mode, a.st_mtime_ns) == (
        b.st_dev, b.st_ino, b.st_mode, b.st_mtime_ns)


def _save(path: Path, text: str, before: Snapshot) -> Optional[Path]:
    from . import files
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".setup.lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ConfigError(f"{lock}: another setup may be saving; check it before retrying") from None
    os.close(fd)
    try:
        check(_same(before, _snapshot(path)), f"{path}: changed during setup; nothing overwritten, run setup again")
        backup = files._backup(path, before[0]) if before is not None else None
        check(_same(before, _snapshot(path)), f"{path}: changed while backing up; nothing overwritten")
        files.atomic_write(path, text.encode("utf-8"), 0o600, follow_symlinks=False,
                           overwrite=before is not None)
        return backup
    finally:
        lock.unlink()


def _block(table: str, values: dict) -> str:
    return table + "\n" + "".join(f"{k} = {json.dumps(v, ensure_ascii=False)}\n" for k, v in values.items())


def _suggestions(home: Path) -> dict:
    from . import resolve, view
    result = {}
    for field in ("name", "email"):
        r = resolve._git(home, "config", "--global", "--get", "user." + field)
        if r.returncode == 0:
            value = r.stdout.strip()
            if value and not config.CONTROL_RE.search(value):
                result[field] = value
        elif r.returncode != 1:
            view.warn(f"could not suggest {field} from Git config: {r.stderr.strip()}")
    repo = resolve.is_repo(Path.cwd())
    if repo:
        remotes = resolve.remotes(repo)
        url = remotes.get("origin") or next(iter(remotes.values()), "")
        host, owner, _ = resolve.parse_url(url)
        if host and config.HOST_RE.fullmatch(host):
            result["host"] = host
        if owner and config.OWNER_RE.fullmatch(owner):
            result["owners"] = owner
    return result


def _validate(text: str, path: Path, home: Path) -> config.Config:
    from . import plan
    cfg = config.loads(text, path, "--config")
    env = plan.Env(home)
    targets, _ = plan.build(cfg, env)
    reserved = [t.path for t in targets] + plan.guard_paths(env) + [home / plan.ALLOWED_SIGNERS]
    source = path.resolve()
    for output in reserved:
        target = output.resolve()
        check(source != target and source not in target.parents and target not in source.parents,
              f"config {path} conflicts with managed output {output}; choose another config path")
    return cfg


def _key_file(home: Path, v: str) -> Path:
    return home / v[2:] if v.startswith("~/") else Path(v)


def _wizard(out, home: Path, path: Path, original: str, from_example: bool,
            cfg: Optional[config.Config]) -> str:
    """Ask for the identities; the config text to save (original when nothing was added)."""
    from rich.prompt import Confirm, Prompt
    from rich.text import Text
    text = original if cfg is not None else (
        "# Created with gitpro setup.\n"
        "# Reference: https://github.com/Yantrakar/gitpro/blob/main/docs/guide.md\n\n")
    if cfg is not None:
        out.print("Existing settings and comments will be kept. This adds identities; it does not edit them.")
    elif from_example:
        out.print("This is the unchanged example template. Saving will replace its placeholders and keep a backup.")
    suggested = _suggestions(home)
    if cfg:
        person_default = cfg.default if cfg.default.email else next(iter(cfg.identities), None)
        if person_default:
            suggested.setdefault("name", person_default.name)
            suggested.setdefault("email", person_default.email)

    def ask(label: str, default: str = "", validate: Optional[Callable[[str], None]] = None) -> str:
        while True:
            value = Prompt.ask(Text(label), default=default, show_default=bool(default), console=out).strip()
            try:
                config._no_control_chars(value, label)
                if validate:
                    validate(value)
                return value
            except ConfigError as e:
                out.print(Text(str(e), style="red"))

    def person() -> dict:
        name = ask("Commit name", suggested.get("name", ""),
                   lambda v: check(v and "<" not in v and ">" not in v, "Enter a name without < or >"))
        email = ask("Commit email", suggested.get("email", ""),
                    lambda v: config._check_person(config.Default(name=name, email=v), "identity", True))
        suggested.update(name=name, email=email)
        return {"name": name, "email": email}

    if cfg is None:
        out.print("A fallback identity applies to repositories that do not match a host/owner rule.")
        fallback = person() if Confirm.ask("Set a fallback identity?", default=False, console=out) else {}
        text += _block("[default]", fallback)
        cfg = config.loads(text, path)

    ids = {i.id for i in cfg.identities}
    added = 0
    while Confirm.ask("Add an identity for a Git host?", default=not added, console=out):
        while True:
            proposed_id = "work"
            n = 2
            while proposed_id in ids:
                proposed_id, n = f"work-{n}", n + 1
            ident = ask("Identity label (for example work or personal)", proposed_id,
                        lambda v: check(config.ID_RE.fullmatch(v) and v not in ids, "Use a unique identity label"))
            values = {"id": ident, **person()}
            host = ask("Git host", suggested.get("host", "github.com"),
                       lambda v: config._check_host(v, "Git host"))
            while True:
                owners = ask("Owners or groups, comma-separated (blank means the entire host)",
                             suggested.get("owners", "") if host == suggested.get("host") else "",
                             lambda v: check(all(config.OWNER_RE.fullmatch(s.strip()) for s in v.split(","))
                                             if v else True, "Enter valid, comma-separated owners or groups"))
                if owners or Confirm.ask(f"Match EVERY repository on {host}?", default=False, console=out):
                    break
            values.update(host=host, owners=list(dict.fromkeys(s.strip() for s in owners.split(",") if s.strip())))
            public_keys = sorted(p for p in (home / ".ssh").glob("*.pub") if p.is_file())
            if public_keys:
                out.print("Existing SSH keys (only public-key filenames are inspected):")
                for pub in public_keys:
                    out.print(Text(f"  {pub.with_suffix('')}"))

            def key_path(v: str) -> None:
                if v:
                    config._check_path(v, "SSH key")
                    key = _key_file(home, v)
                    check(not v.endswith(".pub"), "Enter the private-key path, not .pub")
                    check(key.is_file() or not key.exists(), "Choose a private-key file, or a new path to create")
                    check(key.exists() or not Path(str(key) + ".pub").exists(),
                          "That .pub has no private key next to it; choose another path")

            key = ask("SSH key path (an existing key, or a new path setup creates; blank keeps your "
                      "existing authentication)", validate=key_path)
            if key:
                values["ssh_key"] = key
                alias = "gitpro-" + (re.sub(r"[^a-z0-9-]", "-", ident.lower()).strip("-") or "identity")
                values["ssh_alias"] = ask("SSH alias for this identity", alias,
                                          lambda v: check(config.HOST_RE.fullmatch(v) and v != host,
                                                          "Choose an SSH alias different from the Git host"))
                values["sign"] = Confirm.ask("Sign commits with this SSH key?", default=False, console=out)
                private = _key_file(home, key)
                if values["sign"] and private.exists() and not Path(str(private) + ".pub").is_file():
                    out.print("The matching .pub file is missing. Choose a key with a public key to enable signing.")
                    continue
            else:
                values["sign"] = False
            block = "\n\n" + _block("[[identity]]", values)
            try:
                cfg = _validate(text + block, path, home)
            except ConfigError as e:
                out.print(Text(f"{e}\nPlease enter this identity again; nothing has been saved.", style="red"))
                continue
            text += block
            ids.add(ident)
            added += 1
            break
    return text


# ---------------------------------------------------------------- what this machine still needs

@dataclass
class Work:
    config: Optional[str] = None                    # "create PATH" / "update PATH ..." when the config changes
    keys: list = field(default_factory=list)        # keys.Key to create
    guard: bool = False                             # build the files with the guard
    changes: list = field(default_factory=list)     # files.Change for Git, SSH and the guard
    hooks: List[Tuple[str, str]] = field(default_factory=list)   # (agent, what)
    mcps: List[Tuple[str, str]] = field(default_factory=list)
    done: List[str] = field(default_factory=list)   # already in place
    notes: List[str] = field(default_factory=list)  # left alone, and why

    def pending(self) -> bool:
        return bool(self.config or self.keys or self.changes or self.hooks or self.mcps)


def _empty(cfg: config.Config) -> bool:
    return cfg.default.email is None and not cfg.identities


def _guard(cfg: config.Config, env) -> Tuple[bool, Optional[str]]:
    """(set up the guard, why not). [guard] hooks = false keeps it off; another tool's core.hooksPath
    or a hand-written file where a hook goes leaves it off with a note (guard install refuses both)."""
    from . import plan, render
    from .guard import check_foreign
    if not plan.guard_by_setup(cfg):
        return False, None
    if plan.guard_installed(env):
        return True, None
    try:
        check_foreign(cfg, env.home)
        for t in plan.guard_targets(cfg, env):
            if t.path.exists() and not render.is_generated(t.path.read_text(errors="replace")):
                raise ConfigError(f"{t.path} exists and was not written by gitpro; move it away, then re-run")
    except ConfigError as e:
        return False, f"commit guard not set up: {e}"
    return True, None


def _changes(cfg: config.Config, env, guard: bool) -> list:
    from . import files, plan
    return files.diff(*plan.build(cfg, env, guard=guard))


def _agents(args, home: Path) -> List[str]:
    chosen = list(dict.fromkeys(args.agent or []))
    if "none" in chosen:
        check(chosen == ["none"], "--agent none cannot be combined with other agents")
        return []
    if chosen:
        return chosen
    return ["claude"] if (home / ".claude").is_dir() or shutil.which("claude") else []


def inspect(cfg: config.Config, env, names: List[str]) -> Work:
    """What the files, the guard and the agents still need; reads only."""
    from . import agents, keys, mcp, plan
    w = Work()
    w.keys = [k for k in keys.inventory(cfg.default, list(cfg.identities)) if k.path and not env.expand(k.path).exists()]
    w.guard, why = _guard(cfg, env)
    if why:
        w.notes.append(why)
    elif cfg.guard.hooks is False and not plan.guard_installed(env):
        w.done.append("commit guard off ([guard] hooks = false)")
    w.changes = _changes(cfg, env, w.guard)
    if not w.changes:
        w.done.append("Git and SSH files" + (", commit guard" if w.guard else ""))
    for a in names:
        title = TITLE[a]
        if a == "claude" and agents.plugin_enabled(env.home):
            w.done.append("Claude Code: the gitpro plugin provides the hook and the MCP server")
            continue
        parts = [("hook", agents.hook_change, w.hooks)] if a in agents.HOOK_AGENTS else []
        parts += [("MCP server", mcp.install_change, w.mcps)] if a in mcp.AGENTS else []
        if not parts:
            w.notes.append(f"{title}: gitpro has no hook or MCP entry for it; the commit guard still checks "
                           "its commits")
        for what, change, todo in parts:
            try:
                line = change(a, env.home)
            except ConfigError as e:
                w.notes.append(f"{title} {what} not set up: {e}")
                continue
            if line:
                todo.append((a, line))
            else:
                w.done.append(f"{title} {what}")
    return w


def show(out, w: Work, env, passphrase: bool) -> None:
    from rich.table import Table
    from rich.text import Text
    from . import plan, render, undo
    guard_files = set(plan.guard_paths(env))
    hooks_dir = env.home / plan.HOOKS_DIR
    rows: List[Tuple[str, str]] = []
    if w.config:
        rows.append(("config", w.config))
    for k in w.keys:
        rows.append(("SSH key", f"create {k.path} (ed25519; "
                     + ("ssh-keygen asks for a passphrase)" if passphrase else "no passphrase)")))
    stubs = 0
    for c in w.changes:
        if c.path in guard_files:
            if c.path.parent == hooks_dir and c.path.name in render.HOOK_NAMES:
                stubs += 1
                continue
            rows.append(("guard", f"{c.action} {undo.tilde(c.path)}"))
        else:
            rows.append(("Git, SSH", f"{c.action} {undo.tilde(c.path)}"))
    if stubs:
        rows.append(("guard", f"write {undo.tilde(hooks_dir)}/ ({stubs} hooks: each commit and push is checked "
                              "for the right identity)"))
    rows += [(f"{TITLE[a]} hook", f"{line} (checks the git commands it runs)") for a, line in w.hooks]
    rows += [(f"{TITLE[a]} MCP", f"{line} (read-only identity tools)") for a, line in w.mcps]
    order = ["config", "SSH key", "Git, SSH", "guard"]
    rows.sort(key=lambda r: order.index(r[0]) if r[0] in order else len(order))   # stable: agents keep theirs
    if rows:
        table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
        table.add_column(style="bold")
        table.add_column()
        last = None
        for group, line in rows:
            table.add_row("" if group == last else group, Text(line))
            last = group
        out.print(Text("\nSetup will:", style="bold"))
        out.print(table)
    if w.done:
        out.print(Text("Already in place: " + "; ".join(w.done), style="dim"), soft_wrap=True)
    for n in w.notes:
        out.print(Text("⚠ " + n, style="yellow"), soft_wrap=True)


def _apply_files(cfg: config.Config, env, guard: bool) -> None:
    from . import files, plan, undo, view
    from .guard import _backup_lines
    changes = _changes(cfg, env, guard)    # again: the keys just created go into allowed_signers
    if not changes:
        return
    lines = _backup_lines(changes, undo.state_dir())
    lines.append(f"rmdir {shlex.quote(str(env.home / plan.HOOKS_DIR))} 2>/dev/null || true   # only when empty")
    script = undo.write("setup", lines, what="Git, SSH and guard files")
    files.apply(changes)
    view.ok(f"wrote {view.plural(len(changes), 'file')} for Git, SSH" + (" and the commit guard" if guard else ""))
    print(f"↩ undo: sh {undo.tilde(script)}")


def cmd_setup(args) -> int:
    from rich.prompt import Confirm
    from rich.syntax import Syntax
    from rich.text import Text
    from . import agents, keys, lint, mcp, plan, resolve, undo, view

    resolve.require_git()
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    home = args.home.expanduser().resolve()
    path = Path(args.config or os.environ.get(config.ENV_VAR) or home / config.USER_CONFIG).expanduser().absolute()
    before = _snapshot(path)
    original = before[0].decode("utf-8") if before else ""
    template = resources.files("gitpro").joinpath("example.toml").read_text(encoding="utf-8")
    from_example = bool(original) and config._load_toml(original, path) == config._load_toml(template)
    cfg = config.loads(original, path) if original and not from_example else None
    _validate(original if cfg is not None else "[default]\n", path, home)
    names = _agents(args, home)
    out = view.console()
    out.print(Text("gitpro setup", style="bold"))
    out.print(Text(f"Config: {path}"))
    if cfg is not None:
        for identity in cfg.identities:
            scope = ", ".join(identity.owners) or "all owners"
            out.print(Text(f"  {identity.id}: {identity.name} <{identity.email}> on {identity.host} ({scope})"))

    text = original
    if cfg is None or args.add or _empty(cfg):
        check(tty, "setup asks for your identities, so it needs an interactive terminal; run gitpro setup "
                   "there, or write the config yourself (gitpro init) and run gitpro setup -y")
        out.print("Setup asks a few questions, shows everything it will do, and asks once before writing.")
        out.print("It never changes your repos and never contacts an account. Ctrl-C stops it; nothing is "
                  "written before you say yes.\n")
        text = _wizard(out, home, path, original, from_example, cfg)
        cfg = _validate(text, path, home)
        if _empty(cfg):
            if text == original:
                view.ok("configuration left unchanged")
                return 0
            view.aborted()
            return 1
        if text != original:
            added = original and not from_example
            out.print(Text("\nReview the configuration additions" if added else "\nReview your configuration",
                           style="bold"))
            out.print(Syntax(text[len(original):] if added else text, "toml", word_wrap=True))

    env = plan.Env(home)
    w = inspect(cfg, env, names)
    if text != original:
        w.config = (f"create {undo.tilde(path)}" if before is None else
                    f"update {undo.tilde(path)} (a backup is kept)")
    show(out, w, env, passphrase=not args.no_passphrase)
    failed = 0
    if not w.pending():
        view.ok("this machine is set up; nothing to change")
    else:
        out.print(Text("Repos are not changed: 'gitpro scan' lists the ones that need a fix.", style="dim"))
        check(not w.keys or args.no_passphrase or tty,
              "creating SSH keys asks for a passphrase; run gitpro setup in a terminal, or add --no-passphrase")
        if not args.yes:
            if not tty:
                print("Dry run: nothing written. Run gitpro setup -y to apply, or run it in a terminal.")
                return 0
            if not Confirm.ask(QUESTION, default=False, console=out):
                view.aborted()
                return 1
        if w.config:
            backup = _save(path, text, before)
            view.ok(f"saved {path}")
            if backup:
                view.step(f"backup: {backup}")
        try:
            if w.keys and keys.generate(w.keys, env.expand, passphrase=not args.no_passphrase):
                view.ok(f"created {view.plural(len(w.keys), 'SSH key')}")
            _apply_files(cfg, env, w.guard)
        except Exception:
            view.error("setup stopped; fix the error below, then run gitpro setup again: it picks up "
                       "where it stopped")
            raise
        for a, _ in w.hooks:
            failed += agents.install(argparse.Namespace(
                agent=a, scope="user", home=home, json=False, yes=True, write=True, experimental=True)) != 0
        for a, _ in w.mcps:
            failed += mcp.cmd_install(argparse.Namespace(
                agent=a, scope="user", home=home, json=False, yes=True, write=True, force=False)) != 0
        if failed:
            view.error("an agent step failed (above); run gitpro setup again after fixing it")
    for msg in lint.warnings(cfg.default, list(cfg.identities), env):
        view.warn(msg)
    inv = keys.inventory(cfg.default, list(cfg.identities))
    if inv:
        out.print()
        keys.report(inv, env.expand)
    view.step("check your repos: gitpro scan (it changes nothing; it shows each fix)")
    return 1 if failed else 0
