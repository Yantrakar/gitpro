"""Argument pieces shared by cli.py and the hot modules (guard, agents, prompt). Stdlib only.

A hot module exposes add_arguments(p). Its register() does
    add_arguments(sub.add_parser(name, parents=[common, confirm]))
and its main(argv) does
    p = argparse.ArgumentParser(prog="gitpro <name>", parents=[args.common(), args.confirm()])
    add_arguments(p)
    return args.guarded(lambda a: a.func(a), p.parse_args(argv[1:]))
so both routes accept the same arguments without importing cli.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Set

CAPABILITIES: Set[str] = set()   # filled by register()/extend(); `gitpro --version --json` lists them sorted


def capability(*names: str) -> None:
    """Announce what this build can do ("why", "clone.dry-run", ...); clients probe the list."""
    CAPABILITIES.update(names)


def common() -> argparse.ArgumentParser:
    """Parent parser: -c/--config PATH and the hidden --home."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("-c", "--config", type=Path, metavar="PATH", help="config file (default: see gitpro -h)")
    p.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    return p


def confirm() -> argparse.ArgumentParser:
    """Explicit execution: --write retains confirmation; -y also answers yes."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation (needed without a terminal)")
    p.add_argument("--write", action="store_true", help="execute after confirmation (default: preview only)")
    return p


def dry_run(p: argparse.ArgumentParser) -> None:
    """-n/--dry-run for a command that already previews without -y (pin, scan, audit; spec 22 R4)."""
    p.add_argument("-n", "--dry-run", action="store_true", help="show only, change nothing (as without -y)")


def preview_only(args: object) -> bool:
    if getattr(args, "dry_run", False):
        if getattr(args, "yes", False):
            from .config import ConfigError
            raise ConfigError("choose one: -n shows the change, -y makes it")
        return True
    return not (getattr(args, "yes", False) or getattr(args, "write", False))


def preview_result(args: object, command: str, data: dict) -> int:
    if getattr(args, "json", False):
        from . import jsonout
        jsonout.emit(command, dict(data, dry_run=True))
    else:
        print("Dry run: nothing written. Use -y/--yes or the command's --write option to execute.")
    return 0


def ask(question: str, yes: bool) -> bool:
    """True for -y; else ask on the terminal. Without one, a ConfigError (the caller exits 2)."""
    if yes:
        return True
    if not sys.stdin.isatty():
        from .config import ConfigError
        raise ConfigError("no terminal to confirm on; re-run with --yes")
    from . import view
    return view.confirm(question)


def fail(msg: str, args: object) -> int:
    """Exit 2 with msg: the JSON error object on stdout under --json (nothing on stderr), else ✗ on stderr."""
    if getattr(args, "json", False):
        from . import jsonout
        jsonout.error(getattr(args, "command", None) or "gitpro", msg)
    else:
        print(f"✗ {msg}", file=sys.stderr)
    return 2


_CMD_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def _command_path(args: object) -> str:
    """The command words of parsed args ("forge similar"), or "gitpro"."""
    words = [getattr(args, "command", None)]
    for key, val in sorted(vars(args).items()) if hasattr(args, "__dict__") else []:
        if key != "command" and key.endswith(("_command", "_action")):
            words.append(val)
    return " ".join(w for w in words if isinstance(w, str) and _CMD_RE.match(w)) or "gitpro"


def _internal_error(args: object, exc: Exception) -> int:
    """An unexpected exception: write the crash record, print two lines (the traceback only with
    GITPRO_DEBUG=1), exit 2."""
    from . import incident
    command = _command_path(args)
    incident.record_crash(command, exc)
    name = type(exc).__qualname__
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_.]{0,80}$", name):
        name = "Exception"
    if os.environ.get("GITPRO_DEBUG") == "1":
        import traceback
        traceback.print_exception(type(exc), exc, exc.__traceback__)
    if getattr(args, "json", False):
        from . import jsonout
        jsonout.error(getattr(args, "command", None) or "gitpro", f"internal error ({name})")
    else:
        print(f"✗ internal error ({name}): this is a gitpro bug", file=sys.stderr)
        print("→ draft a report with: gitpro report --crash", file=sys.stderr)
    return 2



def guarded(func: Callable[[argparse.Namespace], int], args: argparse.Namespace) -> int:
    """func(args) with gitpro's error handling: ConfigError, CalledProcessError, OSError and UnicodeError
    become exit 2 through fail(); Ctrl-C / EOF exit 130."""
    from .config import ConfigError
    try:
        return func(args)
    except ConfigError as e:
        return fail(str(e), args)
    except subprocess.CalledProcessError as e:
        cmd = e.cmd if isinstance(e.cmd, str) else shlex.join(str(c) for c in e.cmd)
        return fail(f"{cmd} failed ({e.returncode})", args)
    except (OSError, UnicodeError) as e:
        return fail(str(e), args)
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        return 130
    except Exception as e:   # noqa: BLE001 - a bug in gitpro: record it, say so, exit 2
        return _internal_error(args, e)
