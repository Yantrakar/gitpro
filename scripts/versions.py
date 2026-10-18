#!/usr/bin/env python3
"""The release version lives in one place: the file src/gitpro/VERSION.

Line 1 is the active version, then a comment (`0.4.0  2026-10-16  summary`); lines 2 and on are older
versions, newest first, in the same form. Only the first word of line 1 is the version.

    python3 scripts/versions.py            # prints it; exit 1 and the reason on stderr if it is not X.Y.Z
    python3 scripts/versions.py --stamp    # writes it into vscode/package.json and server.json

PyPI gets it from hatch (pyproject.toml reads VERSION; gitpro.__version__ reads it too); gitpro.dev gets it from design/site_meta.py.
The VSIX and the MCP Registry entry need a version inside a file, so the workflows run --stamp on their
checkout right before packaging or publishing. --stamp is never committed: server.json in git holds no version, and vscode/package.json holds the
placeholder 0.0.0, because VS Code loads no extension without a version (the integration test, F5).
The edits are made on the text, so each file keeps its formatting. Standard library only: the release
check job runs this before anything is installed.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"\d+\.\d+\.\d+")


def version(root: Path = ROOT) -> str:
    """The first word of line 1 of src/gitpro/VERSION; ValueError with the reason when it is missing or not X.Y.Z."""
    try:
        words = (root / "src/gitpro/VERSION").read_text(encoding="utf-8").split("\n", 1)[0].split()
    except OSError as e:
        raise ValueError(f"src/gitpro/VERSION cannot be read: {e.strerror or e}") from None
    text = words[0] if words else ""
    if not SEMVER.fullmatch(text):
        raise ValueError(f"version {text!r} is not X.Y.Z (src/gitpro/VERSION)")
    return text


def _insert(text: str, line: str, anchor: str, after: bool = False) -> str:
    """Add `line` (with the anchor line's indent) before, or with after=True after, each line matching anchor."""
    def put(m: re.Match) -> str:
        ind = re.match(r" *", m[0])[0]
        return m[0] + f"{ind}{line}\n" if after else f"{ind}{line}\n" + m[0]
    return re.sub(anchor, put, text, flags=re.M)


def stamp(root: Path = ROOT) -> List[str]:
    """Write the version into root's vscode/package.json (and the root entries of package-lock.json when
    it has them) and server.json (version and every packages[].version). Returns the files written.
    Running it twice gives the same bytes."""
    v = version(root)
    field = f'"version": "{v}",'
    done = []

    def edit(rel: str, fn) -> None:
        path = root / rel
        path.write_text(fn(path.read_text(encoding="utf-8")), encoding="utf-8")
        done.append(rel)

    def package(text: str) -> str:
        text = re.sub(r'^  "version": "[^"]*",\n', "", text, flags=re.M)
        if '\n  "icon":' not in text:
            raise ValueError("vscode/package.json has no icon line to put the version before")
        return _insert(text, field, r'^  "icon":.*\n')

    def lock(text: str) -> str:      # kept in step only when it carries a version
        text = re.sub(r'^(  "version": )"[^"]*"', rf'\g<1>"{v}"', text, count=1, flags=re.M)
        return re.sub(r'(^    "": \{\n(?:      [^\n]*\n)*?      "version": )"[^"]*"', rf'\g<1>"{v}"', text,
                      count=1, flags=re.M)

    def server(text: str) -> str:
        text = re.sub(r'^ *"version": "[^"]*",\n', "", text, flags=re.M)
        if '\n  "packages": [' not in text or '"identifier":' not in text:
            raise ValueError("server.json has no packages")
        text = _insert(text, field, r'^  "packages": \[.*\n')
        return _insert(text, field, r'^ *"identifier": .*\n', after=True)

    edit("vscode/package.json", package)
    lock_path = root / "vscode/package-lock.json"
    if lock_path.exists() and re.search(r'^  "version":', lock_path.read_text(encoding="utf-8"), re.M):
        edit("vscode/package-lock.json", lock)
    edit("server.json", server)
    return done


def main(argv: List[str]) -> int:
    if argv not in ([], ["--stamp"]):
        print("usage: python3 scripts/versions.py [--stamp]", file=sys.stderr)
        return 2
    try:
        v = version()
        if argv:
            for rel in stamp():
                print(f"stamped {rel} with {v}")
        else:
            print(v)
    except (ValueError, OSError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
