"""Per-remote git identities generated from one TOML file (see cli.py)."""
from __future__ import annotations

import os

# The release version is written only in the VERSION file next to this one (hatch reads it too):
# the first word of its first line. os and open() only: the hooks import this package on every
# commit, so nothing heavier is loaded.
with open(os.path.join(os.path.dirname(__file__), "VERSION"), encoding="utf-8") as _f:
    __version__ = _f.readline().split()[0]
