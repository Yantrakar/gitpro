"""scan -c/--config and --roots: where scan looks, from the command line or the config's [scan] table.

Roots: DIR... on the command line, else [scan] roots, else ~. [scan] exclude applies to both, never to
--repo. gitpro never edits the config: with roots on the command line it prints the line to add.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import config, jsonout, undo

_ALWAYS = {"**/node_modules", "node_modules"}   # patterns every walk applies anyway


def extend(subs) -> None:
    from .cli import capability, extend_command
    extend_command(subs, "scan", add_flags, handler)
    capability("scan.roots")


def add_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("-c", "--config", type=Path, metavar="PATH", help="config file (default: see gitpro -h)")
    p.add_argument("--home", type=Path, help=argparse.SUPPRESS)  # accepted, ignored (labels use $HOME)
    p.add_argument("--roots", action="store_true", help="print the directories scan looks in, and what it skips")


def _warn(msg: str) -> None:
    print(f"⚠ {msg}", file=sys.stderr)


def scan_config(explicit: Path | None, *, has_args: bool) -> "tuple[config.Scan | None, Path | None]":
    """(Scan or None, config path or None). Lenient: only [scan] matters; a bad [scan] table or TOML
    prints one warning and counts as no config. A config that cannot be found is no config."""
    home = Path.home()
    try:
        path, _ = config.find(explicit, home)
    except config.ConfigError:
        return None, None
    try:
        return config.load_scan(path), path
    except config.ConfigError as e:
        msg = str(e)
        msg = msg[len(f"{path}: "):] if msg.startswith(f"{path}: ") else msg
        _warn(f"{undo.tilde(path)}: {msg}; " + ("its [scan] table is ignored" if has_args else "scanning ~"))
        return None, path


def handler(args) -> int | None:
    """--roots prints and exits; otherwise sets args.roots_effective, exclude, roots_source and
    roots_hint for cmd_scan, or exits 2 when no root is left to scan. --repo: as before."""
    from . import scan
    from .args import fail
    if args.repo and not args.roots:
        return None
    cfg_scan, path = scan_config(args.config, has_args=bool(args.dirs))
    if cfg_scan is not None and not cfg_scan.roots and not args.dirs and path is not None:
        _warn(f"{undo.tilde(path)}: [scan] roots is empty; scanning ~")
    written = [str(d) for d in args.dirs]
    roots, exclude, source = scan.effective_roots(written, cfg_scan)
    if args.roots:
        if source != "args":
            written = list(cfg_scan.roots) if source == "config" and cfg_scan is not None else ["~"]
        return print_roots(args, roots, exclude, source, path if cfg_scan is not None else None,
                           as_written(written))
    existing = [r for r in roots if r.is_dir()]
    if not existing:
        return fail(f"no scan root exists (looked at: {', '.join(undo.tilde(r) for r in roots)})", args)
    for r in roots:
        if r not in existing:
            _warn(f"root does not exist: {undo.tilde(r)}")
    usable = []
    for r in existing:
        pat = scan.excluded_by(r, exclude)
        if pat:
            _warn(f"root {undo.tilde(r)} is excluded by '{pat}'; skipped")
        else:
            usable.append(r)
    if not usable:
        return fail("every scan root is excluded by [scan] exclude", args)
    args.roots_effective, args.exclude, args.roots_source = usable, exclude, source
    if source == "args" and path is not None and path.is_file():
        args.roots_hint = roots_hint(usable, cfg_scan, path)
    return None


def roots_hint(usable: list[Path], cfg_scan, path: Path) -> str | None:
    """The footer for roots given on the command line: the [scan] roots line that keeps them. Only
    roots [scan] would accept; none when there are none, or they are what the config has already."""
    from . import scan
    keep = [r for r in usable if config.scan_root_error(undo.tilde(r)) is None]
    if not keep:
        return None
    if cfg_scan is not None and cfg_scan.roots:
        configured, _, _ = scan.effective_roots([], cfg_scan)
        if set(configured) == set(keep):
            return None
    return f"to keep these roots: add to {undo.tilde(path)}: [scan] roots = {toml_list(keep)}"


def as_written(written: list[str]) -> dict:
    """{resolved root: the first way it was written}, to show ~/work rather than where it points."""
    out: dict = {}
    for w in written:
        out.setdefault(Path(w).expanduser().resolve(), w)
    return out


def toml_list(roots) -> str:
    """["~", "/mnt/d/work"]: roots as a TOML array, home shown as ~ (what [scan] roots accepts)."""
    return json.dumps([undo.tilde(r) for r in roots], ensure_ascii=False).replace("\x7f", "\\u007f")


def _label(root: Path, written: str | None) -> str:
    """~/work as written, and where it really is when that differs: ~/work → /mnt/data/work."""
    if written is None:
        return undo.tilde(root)
    plain = Path(written).expanduser()
    same = plain.is_absolute() and Path(os.path.normpath(plain)) == root
    return written if same else f"{written} → {undo.tilde(root)}"


def print_roots(args, roots, exclude, source: str, path: Path | None, written: dict | None = None) -> int:
    """`scan --roots`: the effective roots (as written, resolved when different) and exclude
    patterns, text or --json (roots resolved; written: as given)."""
    written = written or {}
    missing = [r for r in roots if not r.is_dir()]
    if getattr(args, "json", False):
        jsonout.emit("scan", {"roots": [str(r) for r in roots], "exclude": list(exclude), "source": source,
                              "config": str(path) if path else None, "missing": [str(r) for r in missing],
                              "written": [written.get(r, str(r)) for r in roots]})
        return 0
    where = {"config": f"from {undo.tilde(path)}" if path else "from config", "args": "from command line",
             "default": "default"}[source]
    lines = [f"roots ({where}):"]
    lines += [f"  {_label(r, written.get(r))}" + ("  (missing)" if r in missing else "") for r in roots]
    lines.append("exclude:")
    lines += [f"  {e}" + ("  (always)" if e in _ALWAYS else "") for e in exclude]
    if not _ALWAYS & set(exclude):
        lines.append("  **/node_modules  (always)")
    lines.append("  **/.*  (always: hidden directories)")
    print("\n".join(lines))
    return 0
