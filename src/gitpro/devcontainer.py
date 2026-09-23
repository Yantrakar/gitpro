"""`gitpro devcontainer`: the devcontainer.json `initializeCommand` that runs `gitpro pin` on the host.

Containers do not see the host's gitpro files, so the identity is lost there unless the repo is pinned.
initializeCommand runs on the host before every container start; the line below pins the repo when the
host has gitpro and does nothing otherwise. devcontainer.json is a tracked team file (DECISIONS Q5/Q8):
gitpro prints the line, and edits the file only with --write (preview, confirm, backup, undo).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import config, jsonfile, jsonout, resolve
from .args import capability, fail, guarded
from .config import ConfigError

KEY = "initializeCommand"
CANDIDATES = (".devcontainer/devcontainer.json", ".devcontainer.json")
CONTAINER_ENV = ("REMOTE_CONTAINERS", "CODESPACES", "DEVCONTAINER")   # same list as vscode/src/identityBar.ts
DOCKERENV = Path("/.dockerenv")

# One string, because a string runs in the host's shell: `/bin/sh -c` on Linux, macOS and WSL, `cmd.exe /c`
# on Windows (an array would need `sh` on every host). Line 1 is true in sh; in cmd.exe `command` is not a
# command, so cmd exits 0 there (and cmd stops at the first line feed anyway). The workspace path is read
# from a quoted here-document, so no character in it is ever parsed as shell code; pin and `exit 0` sit on
# the read line itself, so even a path holding a newline and a GITPRO_PATH line ends the script before the
# rest of the path could run as a command.
SCRIPT_LINES = (
    "command true || exit 0",
    "# runs 'gitpro pin' on hosts that have gitpro, else nothing (line 1 stops Windows cmd)",
    "command -v gitpro >/dev/null 2>&1 || exit 0",
    """IFS= read -r dir <<'GITPRO_PATH'; gitpro pin "$dir" || true; exit 0""",
    "${localWorkspaceFolder}",
    "GITPRO_PATH",
)
LINE = "\n".join(SCRIPT_LINES)

NOTE = ("initializeCommand runs on the host before the container starts. On Linux, macOS and WSL this line "
        "runs gitpro pin when gitpro is installed and does nothing otherwise; on Windows (cmd.exe) its first "
        "line exits 0, so a colleague without gitpro is not affected and the container still starts (the "
        "build log then shows \"'command' is not recognized as an internal or external command\", which is "
        "harmless).")
MERGED = ("your command now runs alongside gitpro pin (in parallel); its output appears when it finishes")
TEAM = ("the line pins only on hosts that have gitpro: every teammate who opens this container needs gitpro "
        "on their host to get the right identity (without it, the line does nothing)")
_PIN_RE = re.compile(r"gitpro\s+pin\b")


# ---------------------------------------------------------------- pure parts

def _tilde(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def locate(repo: Path) -> Optional[Path]:
    """DIR/.devcontainer/devcontainer.json, else DIR/.devcontainer.json, else None."""
    for name in CANDIDATES:
        path = Path(repo) / name
        if path.is_file():
            return path
    return None


def has_pin(value: object) -> bool:
    """value (a string, an argv list or the object form) already runs `gitpro pin`."""
    if isinstance(value, str):
        return bool(_PIN_RE.search(value))
    if isinstance(value, list):
        return has_pin(" ".join(str(v) for v in value))
    if isinstance(value, dict):
        return any(has_pin(v) for v in value.values())
    return False


def _empty(value: object) -> bool:
    return value is None or value == [] or (isinstance(value, str) and not value.strip())


def propose(existing: object) -> object:
    """The initializeCommand to propose given the current one (None: absent). An existing command is kept
    verbatim, never evaluated: a string or array moves into the object form next to ours."""
    if has_pin(existing):
        return existing
    if _empty(existing):
        return LINE
    if isinstance(existing, dict):
        key = next((k for k in ("gitpro", "gitpro-pin") if k not in existing), None)
        if key is None:
            raise ConfigError(f"{KEY} already has keys \"gitpro\" and \"gitpro-pin\" that do not run gitpro "
                              f"pin; gitpro will not replace them: add the line by hand under another key: "
                              f"{json.dumps(LINE, ensure_ascii=False)}")
        return {key: LINE, **existing}
    if isinstance(existing, str):
        word = existing.split()[0]
        return {"gitpro": LINE, ("cmd" if word == "gitpro" else word): existing}
    if isinstance(existing, list):
        return {"gitpro": LINE, "cmd": existing}
    raise ConfigError(f"{KEY} is {json.dumps(existing)}, not a string, array or object; "
                      f"add the key by hand: {snippet(LINE)}")


def snippet(value: object) -> str:
    return f'"{KEY}": {json.dumps(value, ensure_ascii=False)}'


def in_container(env: Optional[dict] = None) -> bool:
    """The one detection (15's extension carries the same list): any of CONTAINER_ENV set, or /.dockerenv."""
    env = os.environ if env is None else env
    return any(env.get(k) for k in CONTAINER_ENV) or DOCKERENV.exists()


def _container_reason() -> str:
    names = [k for k in CONTAINER_ENV if os.environ.get(k)]
    return ", ".join(names) or str(DOCKERENV)


# ---------------------------------------------------------------- reading the file

def tracked(repo: Path, path: Path) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", str(path)],
                       capture_output=True, env=resolve._child_env(), stdin=subprocess.DEVNULL)
    return r.returncode == 0


@dataclass
class Found:
    repo: Path
    file: Path
    existing: object
    proposed: object
    has_pin: bool
    has_comments: bool
    tracked: bool

    @property
    def merged(self) -> bool:
        """The user's string or array command moves into the object form, next to ours (run in parallel)."""
        return not self.has_pin and isinstance(self.existing, (str, list)) and not _empty(self.existing)

    def to_json(self) -> dict:
        return {"file": str(self.file), "tracked": self.tracked, "has_pin": self.has_pin,
                "existing_command": self.existing, "proposed": self.proposed, "merged": self.merged,
                "has_comments": self.has_comments, "written": False}


def _read(path: Path) -> tuple:
    """(object, text); ConfigError naming the line to add by hand when the file cannot be used."""
    hand = f"; add the key by hand: {snippet(LINE)}"
    try:
        obj, text = jsonfile.read(path, jsonc=True)
    except ConfigError as e:
        raise ConfigError(f"{e}{hand}") from None
    if not isinstance(obj, dict):
        raise ConfigError(f"{path}: cannot parse (the top level is not an object){hand}")
    return obj, text


def inspect(repo: Path, path: Path) -> Found:
    obj, text = _read(path)
    existing = obj.get(KEY)
    return Found(repo, path, existing, propose(existing), has_pin(existing), jsonfile.strip_jsonc(text)[1],
                 tracked(repo, path))


def write(found: Found, *, confirm) -> jsonfile.Result:
    """Add or merge initializeCommand through jsonfile.edit_json: preview to confirm(diff), backup, undo, write."""
    def mutate(obj):
        obj[KEY] = propose(obj.get(KEY))
        return obj
    return jsonfile.edit_json(found.file, mutate, command="devcontainer", jsonc=True, confirm=confirm,
                              snippet=snippet(found.proposed))


# ---------------------------------------------------------------- status

def _pin_state(repo: Path) -> tuple:
    """(pinned, pin file, "Name <email>" or None)."""
    from . import pin   # lazily: pin pulls in render, and building the parser must not
    path = pin._pin_file(str(repo))
    included = pin.FILE in pin.git(str(repo), "config", "--local", "--get-all", "include.path", ok=(0, 1)).split("\n")
    if not (included and path.is_file()):
        return False, path, None
    vals = [pin.git(str(repo), "config", "-f", str(path), "--get", f"user.{k}", ok=(0, 1)) for k in ("name", "email")]
    return True, path, f"{vals[0]} <{vals[1]}>".strip()


def status(repo: Path, cfg: Optional[config.Config], cfg_error: Optional[str] = None) -> dict:
    """What `devcontainer status` reports: the file, the pin, the container. cfg None: no config here, or
    (cfg_error) one that cannot be read. Each problem says what it is `about`: devcontainer.json, config, pin."""
    file = locate(repo)
    problems: List[dict] = []
    dc_pin = False
    if file is not None:
        try:
            dc_pin = has_pin(_read(file)[0].get(KEY))
        except ConfigError as e:
            problems.append({"level": "warning", "about": "devcontainer.json", "message": str(e), "fix": None})
    if cfg_error:
        problems.append({"level": "warning", "about": "config", "fix": None,
                         "message": f"the config cannot be parsed: {cfg_error}"})
    pinned, pin_file, who = _pin_state(repo)
    stale: Optional[bool] = None
    if cfg is not None:
        stale = pinned and any(p.code == "pin_stale" for p in resolve.compare(repo, cfg).problems)
    inside = in_container()
    fix = f"gitpro pin {_tilde(repo)}"
    if inside and not pinned:
        no_cfg = " and no host config" if cfg is None and not cfg_error else ""
        problems.append({"level": "error", "about": "pin", "fix": fix,
                         "message": f"not pinned{no_cfg}: run on the host: {fix}"})
    if stale:
        problems.append({"level": "warning", "about": "pin", "fix": fix, "message": f"pin is stale → {fix}"})
    return {"repo": str(repo), "file": str(file) if file else None, "has_pin": dc_pin, "pinned": pinned,
            "pin_file": str(pin_file), "pin_identity": who, "pin_stale": bool(stale), "in_container": inside,
            "container_reason": _container_reason() if inside else None, "problems": problems}


def _rel(path: Path, repo: Path) -> str:
    try:
        return str(Path(path).relative_to(repo))
    except ValueError:
        return _tilde(path)


def _status_lines(st: dict, repo: Path, has_cfg: bool, cfg_error: Optional[str] = None) -> List[str]:
    if st["file"] is None:
        dc_line = "none (gitpro devcontainer prints the line once the repo has one)"
    else:
        rel = _rel(Path(st["file"]), repo)
        bad = [p for p in st["problems"] if p["about"] == "devcontainer.json"]
        dc_line = rel + "   " + ("cannot parse ⚠" if bad else "initializeCommand runs gitpro pin ✓" if st["has_pin"]
                                 else f"initializeCommand does not run gitpro pin → gitpro devcontainer {_tilde(repo)}")
    if st["pinned"]:
        state = ("stale ⚠" if st["pin_stale"] else "up to date ✓" if has_cfg
                 else "(the config cannot be parsed: not compared)" if cfg_error else "(no config here to compare)")
        pin_line = f"{_rel(Path(st['pin_file']), repo)}  {st['pin_identity']}  {state}"
    else:
        pin_line = "not pinned ✗" if st["in_container"] else f"not pinned → gitpro pin {_tilde(repo)}"
    where = f"yes ({st['container_reason']})" if st["in_container"] else "no"
    return [_tilde(repo), f"  devcontainer.json  {dc_line}", f"  pin                {pin_line}",
            f"  inside container   {where}"]


def cmd_status(args, top: Path) -> int:
    args.command = "devcontainer status"
    cfg: Optional[config.Config] = None
    cfg_error: Optional[str] = None
    try:
        path, how = config.find(args.config, Path(args.home).expanduser().resolve())
    except ConfigError:
        path = None     # no config on this machine (normal inside a container)
    if path is not None:
        try:
            cfg = config.load_all(path, how)
        except (ConfigError, OSError, UnicodeError) as e:
            cfg_error = str(e)
    st = status(top, cfg, cfg_error)
    rc = 1 if st["problems"] else 0
    if jsonout.wants_json(args):
        jsonout.emit("devcontainer status", st)
        return rc
    print("\n".join(_status_lines(st, top, cfg is not None, cfg_error)))
    for p in st["problems"]:
        print(("✗ " if p["level"] == "error" else "⚠ ") + p["message"], file=sys.stderr)
    return rc


# ---------------------------------------------------------------- the snippet and --write

def _print_snippet(f: Found) -> None:
    how = "tracked: committed to the repo" if f.tracked else "not committed: only on this machine"
    print(f"{_tilde(f.file)}  ({how})")
    if f.existing is None:
        print("add this key:")
    else:
        print(f"now:\n  {snippet(f.existing)}\nchange it to (your command is kept as it is):")
    print(f"  {snippet(f.proposed)}")
    if f.merged:
        print(f"· {MERGED}")
    if f.has_comments:
        print("· the file has comments, which --write would lose: add the key by hand")
    elif f.tracked:
        print("→ this file is shared with your team; gitpro does not edit it by default. "
              f"Own repo? gitpro devcontainer --write {_tilde(f.repo)}")
    else:
        print(f"→ gitpro does not edit it by default; to add it: gitpro devcontainer --write {_tilde(f.repo)}")
    print("→ in the container, 'gitpro which' shows the pinned identity; signing and ssh keys are not pinned "
          "(see 'gitpro pin')")
    print(f"· {NOTE}")


def _ask(args, f: Found):
    def confirm(diff: str) -> bool:
        if not args.yes and not sys.stdin.isatty():
            raise ConfigError("no terminal to confirm on; re-run with --yes")
        if not args.json:
            sys.stdout.write(diff if diff.endswith("\n") else diff + "\n")
            sign = "~" if f.existing is not None else "+"
            print(f"  backup  {_tilde(jsonfile.undo.state_dir())}/backup-<timestamp>/{f.file.name}")
            print(f"  update  {_tilde(f.file)}   ({sign} {KEY})")
        if args.yes:
            return True
        from . import view
        return view.confirm("write it?")
    return confirm


def _refuse(args, f: Found, why: str) -> int:
    """Exit 2 without writing: the snippet on stdout and `✗ <why>; add the key by hand` (JSON: ok false)."""
    if args.json:
        jsonout.emit("devcontainer", {**f.to_json(), "ok": False, "error": f"{why}; add the key by hand: "
                                                                            f"{snippet(f.proposed)}"})
        return 2
    _print_snippet(f)
    return fail(f"{why}; add the key by hand (printed above)", args)


def _outside(f: Found) -> Optional[Path]:
    """The file's real path when a symlink takes it out of the repo's real top level, else None."""
    real = Path(os.path.realpath(f.file))
    top = Path(os.path.realpath(f.repo))
    return None if top in real.parents else real


def cmd_write(args, f: Found) -> int:
    if args.json and not args.yes:
        return fail("--write --json needs -y: the question would mix with the JSON", args)
    if not args.json:
        if f.tracked:
            print(f"⚠ {_tilde(f.file)} is tracked by git and shared with your team", file=sys.stderr)
        print(f"⚠ {TEAM}", file=sys.stderr)
    if f.has_comments:
        return _refuse(args, f, f"{_tilde(f.file)} has comments")
    real = _outside(f)
    if real is not None:
        return _refuse(args, f, f"{_tilde(f.file)} is a symlink to {_tilde(real)}, outside the repo; gitpro "
                                "edits only files inside the repo")
    if not os.access(os.path.realpath(f.file), os.W_OK):
        return _refuse(args, f, f"{_tilde(f.file)} is read-only (make it writable to use --write)")
    res = write(f, confirm=_ask(args, f))
    if not res.changed:
        if args.json:
            jsonout.emit("devcontainer", f.to_json())
        else:
            print("✗ aborted, nothing written")
        return 1
    if args.json:
        jsonout.emit("devcontainer", {**f.to_json(), "written": True, "backup": str(res.backup),
                                      "undo": str(res.undo)})
        return 0
    print("✓ written; the next container start runs gitpro pin on the host")
    if f.merged:
        print(f"· {MERGED}")
    print(f"  backup  {_tilde(res.backup)}")
    print(f"↩ undo: sh {_tilde(res.undo)}")
    print(f"→ also pin now: gitpro pin {_tilde(f.repo)}")
    return 0


def _no_file(args, top: Path) -> int:
    msg = (f"no devcontainer.json in {_tilde(top)} (looked at {', '.join(CANDIDATES)}); create one with the "
           "Dev Containers extension or the devcontainer CLI first")
    if args.json:
        jsonout.emit("devcontainer", {"ok": False, "error": msg, "file": None, "tracked": False, "has_pin": False,
                                      "existing_command": None, "proposed": LINE, "has_comments": False,
                                      "written": False})
        return 2
    return fail(msg, args)


def cmd_devcontainer(args) -> int:
    words = list(args.words)
    is_status = bool(words) and words[0] == "status"
    if is_status:
        words.pop(0)
    if len(words) > 1:
        return fail(f"one directory at most (got {len(words)}); usage: gitpro devcontainer [status] [DIR]", args)
    if is_status and args.write:
        return fail("devcontainer status does not take --write", args)
    resolve.require_git()
    where = words[0] if words else "."
    top = resolve.is_repo(Path(where))
    if top is None:
        return fail(f"{where}: not a git repository", args)
    if is_status:
        return cmd_status(args, top)
    path = locate(top)
    if path is None:
        return _no_file(args, top)
    f = inspect(top, path)
    if f.has_pin:
        if args.json:
            jsonout.emit("devcontainer", f.to_json())
        else:
            print(f"✓ already set up: {_tilde(path)} runs gitpro pin in {KEY}")
        return 0
    if args.write:
        return cmd_write(args, f)
    if args.json:
        jsonout.emit("devcontainer", f.to_json())
    else:
        _print_snippet(f)
    return 0


def _intermixed(sp: argparse.ArgumentParser) -> None:
    """Parse options between the words too (`devcontainer status --json DIR`), as clone does."""
    def parse_known_args(args=None, namespace=None):
        # parse_known_intermixed_args calls parse_known_args itself: the class's, not this override.
        del sp.parse_known_args
        try:
            return sp.parse_known_intermixed_args(args, namespace)
        finally:
            sp.parse_known_args = parse_known_args     # type: ignore[method-assign]
    sp.parse_known_args = parse_known_args     # type: ignore[method-assign]


def register(sub, common, confirm) -> None:
    sp = sub.add_parser(
        "devcontainer", parents=[common, confirm], formatter_class=argparse.RawDescriptionHelpFormatter,
        help="print (or --write) the devcontainer.json line that runs gitpro pin on the host",
        description="Print the devcontainer.json initializeCommand that runs 'gitpro pin' on the host before "
                    "every container start.\ndevcontainer.json is shared with your team, so gitpro only prints "
                    "it; --write edits it with a backup and undo.\n'gitpro devcontainer status' shows the pin state.",
        epilog=NOTE)
    sp.add_argument("words", nargs="*", metavar="[status] [DIR]",
                    help="'status' to show the pin state; DIR is the repo (default: .)")
    sp.add_argument("--write", action="store_true",
                    help="edit devcontainer.json (preview, confirm, backup, undo); never creates it")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=lambda a: guarded(cmd_devcontainer, a))
    _intermixed(sp)
    capability("devcontainer", "devcontainer.write")
