"""Guided, offline configuration: save the TOML, never apply Git or SSH changes."""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
import sys
from importlib import resources
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import config
from .config import ConfigError, check

Snapshot = Optional[Tuple[bytes, os.stat_result]]


def register(sub, common, confirm) -> None:
    from .args import capability
    p = sub.add_parser("setup", parents=[common], help="configure identities with a guided terminal wizard")
    p.add_argument("--write", action="store_true", help="allow saving after interactive review (default: preview only)")
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


def cmd_setup(args) -> int:
    from rich.prompt import Confirm, Prompt
    from rich.syntax import Syntax
    from rich.text import Text
    from . import resolve, view

    check(sys.stdin.isatty() and sys.stdout.isatty(),
          "setup needs an interactive terminal; run gitpro setup there, or use gitpro init to create an example")
    resolve.require_git()
    home = args.home.expanduser().resolve()
    path = Path(args.config or os.environ.get(config.ENV_VAR) or home / config.USER_CONFIG).expanduser().absolute()
    before = _snapshot(path)
    original = before[0].decode("utf-8") if before else ""
    template = resources.files("gitpro").joinpath("example.toml").read_text(encoding="utf-8")
    from_example = bool(original) and config._load_toml(original, path) == config._load_toml(template)
    cfg = config.loads(original, path) if original and not from_example else None
    _validate(original if cfg is not None else "[default]\n", path, home)
    text = original if cfg is not None else (
        "# Created with gitpro setup.\n"
        "# Reference: https://github.com/sandeepshelke/gitpro/blob/main/docs/guide.md\n\n")
    out = view.console()
    out.print(Text("gitpro setup", style="bold"))
    out.print("Configure once, then let Git pick the identity for each remote.")
    out.print(Text(f"Config: {path}"))
    out.print("Offline. No keys are created, no accounts are contacted, and no Git/SSH settings are applied.")
    out.print("Ctrl-C cancels the wizard before you confirm Save.\n")
    if cfg is not None:
        out.print("Existing settings and comments will be kept. This wizard adds identities; it does not edit them.")
        for identity in cfg.identities:
            scope = ", ".join(identity.owners) or "all owners"
            out.print(Text(f"  {identity.id}: {identity.name} <{identity.email}> on {identity.host} ({scope})"))
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
                    key = home / v[2:] if v.startswith("~/") else Path(v)
                    check(not v.endswith(".pub") and key.is_file(), "Choose an existing private-key path, not .pub")

            key = ask("SSH key path (blank keeps your existing authentication)", validate=key_path)
            if key:
                values["ssh_key"] = key
                alias = "gitpro-" + (re.sub(r"[^a-z0-9-]", "-", ident.lower()).strip("-") or "identity")
                values["ssh_alias"] = ask("SSH alias for this identity", alias,
                                          lambda v: check(config.HOST_RE.fullmatch(v) and v != host,
                                                          "Choose an SSH alias different from the Git host"))
                values["sign"] = Confirm.ask("Sign commits with this SSH key?", default=False, console=out)
                if values["sign"]:
                    private = home / key[2:] if key.startswith("~/") else Path(key)
                    if not Path(str(private) + ".pub").is_file():
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
    if text == original:
        view.ok("configuration left unchanged")
        return 0
    if cfg.default.email is None and not cfg.identities:
        view.aborted()
        return 1
    _validate(text, path, home)
    out.print(Text("\nReview the configuration additions" if original and not from_example else
                        "\nReview your configuration", style="bold"))
    preview = text[len(original):] if original and not from_example else text
    out.print(Syntax(preview, "toml", word_wrap=True))
    if not args.write:
        out.print("Dry run: nothing saved. Run gitpro setup --write to review and save.")
        return 0
    out.print("Only the config file will be saved. Existing config files are backed up first.")
    if not Confirm.ask("Save this configuration?", default=False, console=out):
        view.aborted()
        return 1
    backup = _save(path, text, before)
    view.ok(f"saved {path}")
    if backup:
        view.step(f"backup: {backup}")
    extra = " -c " + shlex.quote(str(path))
    view.step(f"review Git/SSH changes: gitpro plan{extra}")
    view.step(f"apply them when ready: gitpro apply{extra}")
    return 0
