"""The gitpro skill that ships inside the package, and the copies of it that agents already have.

`setup -n` reports an installed copy that differs from the bundled one; `setup -y` updates the copies that
exist and never creates one (spec 23 section 5). Standard library only."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import List, Optional

# Where agents look for a user-level skill, relative to the home folder. ~/.claude/skills and
# ~/.copilot/skills are often links to ~/.agents/skills: one physical file counts once.
LOCATIONS = (".claude/skills/gitpro", ".copilot/skills/gitpro", ".agents/skills/gitpro")
NAME = "SKILL.md"
PACKAGED = "skill/SKILL.md"          # inside the wheel (pyproject force-include)


def bundled() -> Optional[bytes]:
    """The skill of this gitpro: the packaged copy, else (a source checkout) skills/gitpro/SKILL.md."""
    try:
        return resources.files("gitpro").joinpath(PACKAGED).read_bytes()
    except (OSError, ModuleNotFoundError):
        pass
    try:
        return (Path(__file__).resolve().parents[2] / "skills" / "gitpro" / NAME).read_bytes()
    except OSError:
        return None


@dataclass
class Copy:
    path: Path                                   # the physical file (symlinks resolved)
    locations: List[Path] = field(default_factory=list)   # the paths under home that lead to it
    state: str = "current"                       # current | differs | unreadable
    old: Optional[str] = None                    # its text, when it differs
    mode: int = 0o644


def copies(home: Path, want: Optional[bytes]) -> List[Copy]:
    """The installed copies under home, each physical file once, in LOCATIONS order. A location with no
    SKILL.md (or a dangling link) is not a copy. Reads only."""
    found: dict = {}
    for loc in LOCATIONS:
        link = home / loc / NAME
        try:
            if not link.is_file():
                continue
            real = Path(os.path.realpath(link))
        except OSError:
            continue
        c = found.get(real)
        if c is None:
            c = found[real] = Copy(real)
            try:
                data = real.read_bytes()
                c.mode = real.stat().st_mode & 0o777
                if want is not None and data != want:
                    c.state = "differs"
                    c.old = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                c.state = "unreadable"
        c.locations.append(link)
    return list(found.values())
