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
# plus copilot, which is accepted so that asking for it says plainly that gitpro cannot cover it
AGENTS = ("claude", "claude-desktop", "codex", "copilot", "cursor", "gemini", "opencode", "none")
TITLE = {"claude": "Claude Code", "claude-desktop": "Claude Desktop", "codex": "Codex",
         "copilot": "GitHub Copilot CLI", "cursor": "Cursor", "gemini": "Gemini CLI", "opencode": "opencode"}
# Agents that gitpro has no hook or MCP registration for, and why (shown when one is asked for by name).
NO_COVERAGE = {"codex": "gitpro has no hook or MCP registration for Codex",
               "copilot": "gitpro has no hook or MCP registration for GitHub Copilot CLI (its MCP servers are in "
                          "~/.copilot/mcp-config.json, but its own help does not show that file's format)"}
QUESTION = "Apply these changes?"
LEARN = "Look through your repos to find your identities?"
LEARN_BUDGET = 20.0    # seconds for inspecting the repos found


def register(sub, common, confirm) -> None:
    from .args import capability
    p = sub.add_parser("setup", parents=[common],
                       help="set up or update this machine: identities from your repos, SSH keys, Git, SSH, "
                            "the commit guard and your AI agent")
    p.add_argument("paths", nargs="*", type=Path, metavar="PATH",
                   help="find identities in the repos under these folders (implies --add)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="show this machine's state and what setup would do; exit 1 when something is to do")
    p.add_argument("-y", "--yes", action="store_true",
                   help="apply without asking (the first run still asks for your identities)")
    p.add_argument("--json", action="store_true", help="with -n: machine-readable output")
    p.add_argument("--add", action="store_true", help="add identities to an existing config")
    p.add_argument("--agent", action="append", choices=AGENTS, metavar="NAME",
                   help=f"set up this AI agent's hook and MCP server ({', '.join(AGENTS[:-1])}); repeatable; "
                        "'none' sets up no agent (default: Claude Code when it is installed)")
    p.add_argument("--no-passphrase", action="store_true",
                   help="create missing SSH keys without a passphrase (not advised)")
    p.add_argument("--write", action="store_true", help=argparse.SUPPRESS)   # before 0.4 setup saved only with it
    p.set_defaults(func=cmd_setup)
    capability("setup", "setup.dry-run", "setup.learn")


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


def _ask(out, label: str, default: str = "", validate: Optional[Callable[[str], None]] = None) -> str:
    from rich.prompt import Prompt
    from rich.text import Text
    while True:
        value = Prompt.ask(Text(label), default=default, show_default=bool(default), console=out).strip()
        try:
            config._no_control_chars(value, label)
            if validate:
                validate(value)
            return value
        except ConfigError as e:
            out.print(Text(str(e), style="red"))


def _person(out, suggested: dict) -> dict:
    name = _ask(out, "Commit name", suggested.get("name", ""),
                lambda v: check(v and "<" not in v and ">" not in v, "Enter a name without < or >"))
    email = _ask(out, "Commit email", suggested.get("email", ""),
                 lambda v: config._check_person(config.Default(name=name, email=v), "identity", True))
    suggested.update(name=name, email=email)
    return {"name": name, "email": email}


def _key_path(home: Path) -> Callable[[str], None]:
    def key_path(v: str) -> None:
        if v:
            config._check_path(v, "SSH key")
            key = _key_file(home, v)
            check(not v.endswith(".pub"), "Enter the private-key path, not .pub")
            check(key.is_file() or not key.exists(), "Choose a private-key file, or a new path to create")
            check(key.exists() or not Path(str(key) + ".pub").exists(),
                  "That .pub has no private key next to it; choose another path")
    return key_path


def _key_questions(out, home: Path, values: dict) -> bool:
    """ssh_key, ssh_alias and sign for values (an identity); a key found in the repos' SSH alias is kept
    and only signing is asked. False when the answers must be asked again."""
    from rich.prompt import Confirm
    if not values.get("ssh_key"):
        public_keys = sorted(p for p in (home / ".ssh").glob("*.pub") if p.is_file())
        if public_keys:
            from rich.text import Text
            out.print("Existing SSH keys (only public-key filenames are inspected):")
            for pub in public_keys:
                out.print(Text(f"  {pub.with_suffix('')}"))
        key = _ask(out, f"SSH key path for {values['id']} (an existing key, or a new path setup creates; blank "
                        "keeps your existing authentication)", validate=_key_path(home))
        if not key:
            values["sign"] = False
            return True
        values["ssh_key"] = key
        alias = "gitpro-" + (re.sub(r"[^a-z0-9-]", "-", values["id"].lower()).strip("-") or "identity")
        values["ssh_alias"] = _ask(out, "SSH alias for this identity", alias,
                                   lambda v: check(config.HOST_RE.fullmatch(v) and v != values["host"],
                                                   "Choose an SSH alias different from the Git host"))
    values["sign"] = Confirm.ask("Sign commits with this SSH key?", default=False, console=out)
    private = _key_file(home, values["ssh_key"])
    if values["sign"] and private.exists() and not Path(str(private) + ".pub").is_file():
        out.print("The matching .pub file is missing. Choose a key with a public key to enable signing.")
        values.pop("ssh_key"), values.pop("ssh_alias", None)
        return False
    return True


def _identity(out, home: Path, ids: set, suggested: dict, found: Optional[dict] = None) -> dict:
    """One identity's questions; found (from the repos) gives the defaults."""
    from rich.prompt import Confirm
    found = found or {}
    proposed_id = found.get("id") or "work"
    n = 2
    while proposed_id in ids:
        proposed_id, n = f"{found.get('id') or 'work'}-{n}", n + 1
    ident = _ask(out, "Identity label (for example work or personal)", proposed_id,
                 lambda v: check(config.ID_RE.fullmatch(v) and v not in ids, "Use a unique identity label"))
    seen = {k: found[k] for k in ("name", "email") if found.get(k)}
    values = {"id": ident, **_person(out, dict(suggested, **seen) if seen else suggested)}
    host = _ask(out, "Git host", found.get("host") or suggested.get("host", "github.com"),
                lambda v: config._check_host(v, "Git host"))
    owners_default = ", ".join(found["owners"]) if found.get("owners") else (
        suggested.get("owners", "") if host == suggested.get("host") else "")
    while True:
        owners = _ask(out, "Owners or groups, comma-separated (blank means the entire host)", owners_default,
                      lambda v: check(all(config.OWNER_RE.fullmatch(s.strip()) for s in v.split(","))
                                      if v else True, "Enter valid, comma-separated owners or groups"))
        if owners or Confirm.ask(f"Match EVERY repository on {host}?", default=False, console=out):
            break
    values.update(host=host, owners=list(dict.fromkeys(s.strip() for s in owners.split(",") if s.strip())))
    if found.get("ssh_key") and found.get("host") == host:
        values.update({k: found[k] for k in ("ssh_alias", "ssh_key", "ssh_user", "ssh_port") if found.get(k)})
    return values


def _add(text: str, values: dict, path: Path, home: Path, out) -> Optional[str]:
    """text with values as one more [[identity]], or None (and why, on out) when the config check rejects it."""
    from rich.text import Text
    block = "\n\n" + _block("[[identity]]", values)
    try:
        _validate(text + block, path, home)
    except ConfigError as e:
        out.print(Text(f"{e}\nPlease enter this identity again; nothing has been saved.", style="red"))
        return None
    return text + block


def _learned(out, home: Path, path: Path, roots: List[Path], cfg: Optional[config.Config]) -> list:
    """Identities proposed from the repos under roots (spec 22 §5.2-5.4), shown; [] when none."""
    from rich.table import Table
    from rich.text import Text
    from . import learn, undo, view
    shown = ", ".join(undo.tilde(r) for r in roots)
    found = learn.learn(roots, home, cfg, budget=LEARN_BUDGET)
    view.step(f"looked at {view.plural(found.repos, 'repo')} in {shown} ({found.seconds:.1f} s)")
    if not found.identities:
        out.print("No identities found in those repos" + (": " + "; ".join(found.notes) if found.notes else "."))
        return []
    out.print(Text(f"\nFound {view.plural(len(found.identities), 'identity', 'identities')} in "
                   f"{view.plural(found.committed, 'repo')} with commits under {shown}:", style="bold"))
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    for _ in range(6):
        table.add_column()
    for n, i in enumerate(found.identities, 1):
        owners = ", ".join(i["owners"][:3]) + (f" (+{len(i['owners']) - 3})" if len(i["owners"]) > 3 else "")
        table.add_row(str(n), i["id"], Text(f"{i.get('name') or '(no name)'} <{i['email']}>"), i["host"],
                      Text(owners), Text(i.get("ssh_key") or "no key found", style="dim"))
        for e, c in i.get("also", []):
            table.add_row("", "", Text(f"also seen: {e} in {view.plural(c, 'repo')}", style="dim"), "", "", "")
    out.print(table)
    for u in found.unassigned:
        out.print(Text(f"  not assigned: {u}", style="dim"), soft_wrap=True)
    for note in found.notes:
        out.print(Text(f"  skipped: {note}", style="dim"), soft_wrap=True)
    return found.identities


def _wizard(out, home: Path, path: Path, original: str, from_example: bool,
            cfg: Optional[config.Config], roots: Optional[List[Path]] = None) -> str:
    """Ask for the identities; the config text to save (original when nothing was added). roots: look
    for identities there without asking; None asks first."""
    from rich.prompt import Confirm, Prompt
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

    if roots is None and Confirm.ask(LEARN, default=True, console=out):
        where = _ask(out, "Where (folders, separated by spaces)", "~")
        roots = [Path(w).expanduser() for w in where.split()] or [home]
    proposals = _learned(out, home, path, roots, cfg) if roots else []

    if cfg is None:
        out.print("A fallback identity applies to repositories that do not match a host/owner rule.")
        fallback = _person(out, suggested) if Confirm.ask("Set a fallback identity?", default=False, console=out) else {}
        text += _block("[default]", fallback)
        cfg = config.loads(text, path)

    ids = {i.id for i in cfg.identities}
    added = 0
    if proposals:
        choice = Prompt.ask("Use these? (e: go through them one by one)", choices=["y", "n", "e"], default="y",
                            console=out)
        for found in proposals if choice != "n" else []:
            if choice == "e":
                what = Prompt.ask(f"{found['id']} ({found['email']} on {found['host']}): keep, edit or skip",
                                  choices=["k", "e", "s"], default="k", console=out)
                if what == "s":
                    continue
            else:
                what = "k"
            while True:
                if what == "k" and found["id"] not in ids and found.get("name"):
                    values = {k: found[k] for k in ("id", "name", "email", "host", "owners", "ssh_alias", "ssh_key",
                                                    "ssh_user", "ssh_port") if found.get(k)}
                else:
                    values = _identity(out, home, ids, suggested, found)
                if not _key_questions(out, home, values):
                    what = "e"
                    continue
                new = _add(text, values, path, home, out)
                if new is not None:
                    break
                what = "e"
            text = new
            ids.add(values["id"])
            added += 1
    while Confirm.ask("Add an identity for a Git host?", default=not added, console=out):
        while True:
            values = _identity(out, home, ids, suggested)
            if not _key_questions(out, home, values):
                continue
            new = _add(text, values, path, home, out)
            if new is None:
                continue
            text = new
            ids.add(values["id"])
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
    coverage: List[dict] = field(default_factory=list)   # per agent: hook and MCP status (see inspect)
    uncovered: List[str] = field(default_factory=list)  # agents asked for by name that gitpro cannot cover
    skills: List[dict] = field(default_factory=list)    # installed gitpro skill copies: {"path","state"}
    skill_stale: list = field(default_factory=list)     # skillcopy.Copy that differ from the bundled skill
    skill_text: Optional[bytes] = None                  # the bundled skill

    def pending(self) -> bool:
        return bool(self.config or self.keys or self.changes or self.hooks or self.mcps or self.skill_stale)


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


def _skills(w: Work, home: Path) -> None:
    """Which installed gitpro skill copies differ from the one bundled with this gitpro (spec 23 section 5).
    Reads only. A copy that is not there is not reported: setup never creates one."""
    from . import skillcopy
    w.skill_text = skillcopy.bundled()
    if w.skill_text is None:
        w.notes.append("the gitpro skill is not bundled with this install; installed copies not checked")
        return
    for c in skillcopy.copies(home, w.skill_text):
        w.skills.append({"path": str(c.path), "state": c.state})
        if c.state == "differs":
            w.skill_stale.append(c)
        elif c.state == "unreadable":
            w.notes.append(f"{c.path}: the gitpro skill is there but cannot be read; left alone")
        else:
            w.done.append(f"gitpro skill {undo_tilde(c.path)}")


def undo_tilde(path: Path) -> str:
    from . import undo
    return undo.tilde(path)


def inspect(cfg: config.Config, env, names: List[str], explicit: bool = False) -> Work:
    """What the files, the guard and the agents still need; reads only. For each agent in names, w.coverage
    has {"agent", "hook", "mcp"[, "reason"]}, each status one of "installed" (gitpro's entry is in the
    agent's config file), "plugin" (a plugin that ships it is enabled), "missing" (setup would add it) or
    "unavailable". None of these says the agent loaded it. With explicit (the user named the agents), an
    agent with neither a hook nor an MCP server available goes to w.uncovered."""
    from . import agents, keys, mcp, plan
    w = Work()
    for k in keys.inventory(cfg.default, list(cfg.identities)):
        p = env.expand(k.path) if k.path else None
        if p is None or p.exists():
            continue
        if p.is_symlink() or keys.pub_path(p).exists():   # an agent-held key, or a dangling link: not ours
            w.notes.append(f"{k.path}: no private key, but {'a symlink' if p.is_symlink() else 'a .pub'} is there; "
                           "setup does not create a key over it")
        else:
            w.keys.append(k)
    _skills(w, env.home)
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
            w.coverage.append({"agent": a, "hook": "plugin", "mcp": "plugin"})
            continue
        row = {"agent": a}
        reasons: List[str] = []
        for what, key, change, todo, known in (("hook", "hook", agents.hook_change, w.hooks, agents.HOOK_AGENTS),
                                               ("MCP server", "mcp", mcp.install_change, w.mcps, mcp.AGENTS)):
            if a not in known:
                row[key] = "unavailable"
                continue
            try:
                line = change(a, env.home)
            except ConfigError as e:
                row[key] = "unavailable"
                reasons.append(f"{what} not set up: {e}")
                w.notes.append(f"{title} {what} not set up: {e}")
                continue
            if line:
                row[key] = "missing"
                todo.append((a, line))
            else:
                row[key] = "installed"
                w.done.append(f"{title} {what}")
        if row["hook"] == "unavailable" and row["mcp"] == "unavailable":
            why = NO_COVERAGE.get(a) or "; ".join(reasons) or f"gitpro has no hook or MCP entry for {title}"
            reasons = [why]
            if explicit:
                w.uncovered.append(f"{title}: {why}; the commit guard still checks its commits")
            else:
                w.notes.append(f"{title}: {why}; the commit guard still checks its commits")
        elif a not in agents.HOOK_AGENTS or a not in mcp.AGENTS:
            reasons.append("gitpro has no " + ("hook" if a not in agents.HOOK_AGENTS else "MCP registration")
                           + f" for {title}")
        if reasons:
            row["reason"] = "; ".join(reasons)
        w.coverage.append(row)
    return w


def rows(w: Work, env, passphrase: bool) -> List[Tuple[str, str]]:
    """(group, what) for each change setup would make, in the preview's order."""
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
    rows += [("gitpro skill", f"update {undo.tilde(c.path)} to this gitpro's version (a backup is kept)")
             for c in w.skill_stale]
    order = ["config", "SSH key", "Git, SSH", "guard"]
    rows.sort(key=lambda r: order.index(r[0]) if r[0] in order else len(order))   # stable: agents keep theirs
    return rows


def show(out, w: Work, env, passphrase: bool) -> None:
    from rich.table import Table
    from rich.text import Text
    rows_ = rows(w, env, passphrase)
    if rows_:
        table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
        table.add_column(style="bold")
        table.add_column()
        last = None
        for group, line in rows_:
            table.add_row("" if group == last else group, Text(line))
            last = group
        out.print(Text("\nSetup will:", style="bold"))
        out.print(table)
    if w.done:
        out.print(Text("Already in place: " + "; ".join(w.done), style="dim"), soft_wrap=True)
    for n in w.notes:
        out.print(Text("⚠ " + n, style="yellow"), soft_wrap=True)
    for n in w.uncovered:
        out.print(Text("✗ " + n, style="red"), soft_wrap=True)


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


def _apply_skills(w: Work) -> int:
    """Update the stale skill copies: each is written through its link (never replacing it), after a backup
    beside it. Returns the number that failed."""
    from . import files, undo, view
    failed = 0
    lines: List[str] = []
    for c in w.skill_stale:
        try:
            current = c.path.read_bytes()
            if c.old is None or current.decode("utf-8") != c.old:
                raise OSError("it changed since it was checked")
            bak = files._backup(c.path, current)
            lines.append(f"cp -p {shlex.quote(str(bak))} {shlex.quote(str(c.path))}")
            files.atomic_write(c.path, w.skill_text or b"", c.mode)
        except (OSError, UnicodeDecodeError) as e:
            failed += 1
            view.error(f"{c.path}: skill not updated: {e}")
            continue
        view.ok(f"updated {undo.tilde(c.path)} (backup: {undo.tilde(bak)})")
    if lines:
        script = undo.write("setup", lines, what="gitpro skill copies")
        print(f"↩ undo: sh {undo.tilde(script)}")
    return failed


def _identity_json(i, d) -> dict:
    return {"id": i.id, "name": i.name, "email": i.email, "host": i.host, "owners": list(i.owners),
            "ssh_alias": i.ssh_alias, "ssh_key": i.ssh_key, "gh_account": i.gh_account,
            "glab_account": i.glab_account, "clone_dir": i.clone_dir, "sign": config.signs(i, d)}


def state(args, home: Path, path: Path, cfg: Optional[config.Config], names: List[str]) -> int:
    """`setup -n`: this machine's identities, keys and what setup would still do (spec 22 §5.6). Never
    asks, never writes; exit 1 when something is to do."""
    from rich.text import Text
    from . import jsonout, keys, lint, plan, undo, view
    env = plan.Env(home)
    if cfg is None or _empty(cfg):
        todo = [{"group": "config", "what": f"no identities in {undo.tilde(path)} yet: run gitpro setup in a terminal"}]
        if args.json:
            jsonout.emit("setup", {"dry_run": True, "config": str(path), "identities": [], "default": None,
                                   "pending": todo, "done": [], "notes": [], "agents": [], "uncovered": [], "skills": []}, ok=False)
        else:
            print(f"Config: {path}")
            view.warn(todo[0]["what"])
        return 1
    w = inspect(cfg, env, names, explicit=bool(args.agent))
    pending = [{"group": g, "what": what} for g, what in rows(w, env, passphrase=not args.no_passphrase)]
    warnings = list(lint.warnings(cfg.default, list(cfg.identities), env))
    if args.json:
        d = cfg.default
        jsonout.emit("setup", {
            "dry_run": True, "config": str(cfg.path),
            "identities": [_identity_json(i, d) for i in cfg.identities],
            "default": {"name": d.name, "email": d.email, "gh_account": d.gh_account, "glab_account": d.glab_account,
                        "clone_dir": d.clone_dir, "sign": d.sign},
            "pending": pending, "done": w.done, "notes": w.notes + warnings, "agents": w.coverage,
            "uncovered": w.uncovered, "skills": w.skills}, ok=not (pending or w.uncovered))
        return 1 if pending or w.uncovered else 0
    out = view.console()
    out.print(Text(f"Config: {cfg.path}"))
    for identity in cfg.identities:
        scope = ", ".join(identity.owners) or "all owners"
        out.print(Text(f"  {identity.id}: {identity.name} <{identity.email}> on {identity.host} ({scope})"))
    show(out, w, env, passphrase=not args.no_passphrase)
    for msg in warnings:
        view.warn(msg)
    inv = keys.inventory(cfg.default, list(cfg.identities))
    if inv:
        out.print()
        keys.report(inv, env.expand)
    if pending:
        view.step("to do it: gitpro setup")
        return 1
    if w.uncovered:
        return 1
    view.ok("this machine is set up; nothing to change")
    return 0


def cmd_setup(args) -> int:
    from rich.prompt import Confirm
    from rich.syntax import Syntax
    from rich.text import Text
    from . import agents, keys, lint, mcp, plan, resolve, undo, view

    resolve.require_git()
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    home = args.home.expanduser().resolve()
    check(not (args.dry_run and args.yes), "choose one: -n shows what setup would do, -y does it")
    check(not args.json or args.dry_run, "--json goes with -n (gitpro setup -n --json)")
    roots = [p.expanduser() for p in args.paths] or None
    path = Path(args.config or os.environ.get(config.ENV_VAR) or home / config.USER_CONFIG).expanduser().absolute()
    before = _snapshot(path)
    original = before[0].decode("utf-8") if before else ""
    template = resources.files("gitpro").joinpath("example.toml").read_text(encoding="utf-8")
    from_example = bool(original) and config._load_toml(original, path) == config._load_toml(template)
    cfg = config.loads(original, path) if original and not from_example else None
    _validate(original if cfg is not None else "[default]\n", path, home)
    names = _agents(args, home)
    if args.dry_run:
        return state(args, home, path, cfg, names)
    out = view.console()
    out.print(Text("gitpro setup", style="bold"))
    out.print(Text(f"Config: {path}"))
    if cfg is not None:
        for identity in cfg.identities:
            scope = ", ".join(identity.owners) or "all owners"
            out.print(Text(f"  {identity.id}: {identity.name} <{identity.email}> on {identity.host} ({scope})"))

    text = original
    if cfg is None or args.add or roots or _empty(cfg):
        check(tty, "setup asks for your identities, so it needs an interactive terminal; run gitpro setup "
                   "there, or write the config yourself and run gitpro setup -y")
        for r in roots or ():
            check(r.is_dir(), f"{r}: not a folder")
        out.print("Setup asks a few questions, shows everything it will do, and asks once before writing.")
        out.print("It never changes your repos and never contacts an account. Ctrl-C stops it; nothing is "
                  "written before you say yes.\n")
        text = _wizard(out, home, path, original, from_example, cfg, roots)
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
    w = inspect(cfg, env, names, explicit=bool(args.agent))
    if text != original:
        w.config = (f"create {undo.tilde(path)}" if before is None else
                    f"update {undo.tilde(path)} (a backup is kept)")
    show(out, w, env, passphrase=not args.no_passphrase)
    failed = 0
    if not w.pending():
        view.ok("nothing to change" if w.uncovered else "this machine is set up; nothing to change")
    else:
        out.print(Text("Repos are not changed: 'gitpro scan' lists the ones that need a fix.", style="dim"))
        check(not w.keys or args.no_passphrase or tty,
              "creating SSH keys asks for a passphrase; run gitpro setup in a terminal, or add --no-passphrase")
        if not args.yes:
            check(tty, "no terminal to confirm on; run gitpro setup -y to apply, or gitpro setup -n to see "
                       "what it would do")
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
        failed += _apply_skills(w)
        if failed:
            view.error("a step failed (above); run gitpro setup again after fixing it")
    for msg in lint.warnings(cfg.default, list(cfg.identities), env):
        view.warn(msg)
    inv = keys.inventory(cfg.default, list(cfg.identities))
    if inv:
        out.print()
        keys.report(inv, env.expand)
    view.step("check your repos: gitpro scan (it changes nothing; it shows each fix)")
    return 1 if failed or w.uncovered else 0
