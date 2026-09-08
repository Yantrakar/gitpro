"""Entry point (`gitpro` script and `python -m gitpro`). Nothing but stdlib at module level.

The hot subcommands (git hooks, agent hooks, shell prompt) run on every commit or prompt, so they go
straight to their own module without importing cli.py, rich or the views. Everything else, and any
hot subcommand whose package is not built yet, falls through to cli.main.
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import List, Optional

HOT = {                      # subcommand -> module with main(argv) -> int (argv includes the subcommand)
    "guard": "gitpro.guard",          # 06
    "agent-check": "gitpro.agents",   # 10
    "prompt": "gitpro.prompt",        # 16
}

CALLER_UMASK: Optional[int] = None    # the umask gitpro was started with (set before tightening to 077)


def main(argv: Optional[List[str]] = None) -> int:
    global CALLER_UMASK
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in HOT and os.name == "posix" and os.geteuid() != 0:
        name = HOT[argv[0]]
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:           # a broken import inside the module is not hidden
                raise
            mod = None                   # package not built yet: fall through to cli
        if mod is not None:
            CALLER_UMASK = os.umask(0o077)
            return mod.main(argv)
    from gitpro.cli import main as cli_main
    return cli_main(argv)
