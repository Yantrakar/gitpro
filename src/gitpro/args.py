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


def preview_only(args: object) -> bool:
    return not (getattr(args, "yes", False) or getattr(args, "write", False))


def preview_result(args: object, command: str, data: dict) -> int:
    if getattr(args, "json", False):
        from . import jsonout
        jsonout.emit(command, dict(data, dry_run=True))
    else:
        print("Dry run: nothing written. Use -y/--yes or the command's --write option to execute.")
    return 0


def fail(msg: str, args: object) -> int:
    """Exit 2 with msg: the JSON error object on stdout under --json (nothing on stderr), else ✗ on stderr."""
    if getattr(args, "json", False):
        from . import jsonout
        jsonout.error(getattr(args, "command", None) or "gitpro", msg)
    else:
        print(f"✗ {msg}", file=sys.stderr)
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
