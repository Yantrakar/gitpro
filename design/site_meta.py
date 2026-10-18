#!/usr/bin/env python3
"""Write the release version and the sitemap dates into site/ before gitpro.dev is published.

The version (from src/gitpro/VERSION) goes into the hero eyebrow and the JSON-LD softwareVersion of
index.html; the committed copy holds the placeholder 0.0.0. Each sitemap <lastmod> becomes the date of the last commit that changed its page, so it needs the
full history: pages.yml checks out with fetch-depth 0. Run: python3 design/site_meta.py [--check]

--check changes nothing and exits 1 when a file would change.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
ORIGIN = "https://gitpro.dev/"


def release() -> str:
    """The release version, read by scripts/versions.py (stdlib only, so it is loaded by path)."""
    spec = importlib.util.spec_from_file_location("versions", ROOT / "scripts/versions.py")
    versions = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(versions)
    try:
        return versions.version(ROOT)
    except ValueError as e:
        raise SystemExit(str(e)) from None


def with_version(index: str, version: str) -> str:
    index, n = re.subn(r'("softwareVersion": ")[^"]+(")', rf"\g<1>{version}\g<2>", index)
    index, m = re.subn(r"(<p class=\"eyebrow\">OPEN SOURCE · MIT · v)[^<]+(</p>)", rf"\g<1>{version}\g<2>", index)
    if (n, m) != (1, 1):
        raise SystemExit(f"index.html: expected one softwareVersion and one eyebrow version, found {n} and {m}")
    return index


def page(loc: str) -> Path:
    if not loc.startswith(ORIGIN):
        raise SystemExit(f"sitemap: {loc} is not on {ORIGIN}")
    rel = loc[len(ORIGIN):]
    return SITE / rel / "index.html" if rel == "" or rel.endswith("/") else SITE / rel


def git_date(path: Path) -> str:
    out = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%cs", "--", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    if not out:
        raise SystemExit(f"sitemap: no commit changed {path.relative_to(ROOT)}")
    return out


def with_dates(sitemap: str, date_of: Callable[[Path], str]) -> str:
    def entry(m: re.Match) -> str:
        return f"<url><loc>{m[1]}</loc><lastmod>{date_of(page(m[1]))}</lastmod></url>"
    return re.sub(r"<url><loc>([^<]+)</loc><lastmod>[^<]*</lastmod></url>", entry, sitemap)


def main(argv: list) -> int:
    if argv not in ([], ["--check"]):
        raise SystemExit("usage: python3 design/site_meta.py [--check]")
    changed = []
    for path, update in ((SITE / "index.html", lambda t: with_version(t, release())),
                         (SITE / "sitemap.xml", lambda t: with_dates(t, git_date))):
        old = path.read_text(encoding="utf-8")
        new = update(old)
        if new != old:
            changed.append(path.relative_to(ROOT))
            if not argv:
                path.write_text(new, encoding="utf-8")
    for rel in changed:
        print(f"{'would change' if argv else 'updated'} {rel}")
    return 1 if argv and changed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
