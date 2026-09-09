"""Terminal output for scan, with rich: colors and wrapping on a terminal, plain text when piped."""
from __future__ import annotations

from typing import Callable

from rich.console import Console
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from .scan import skipped


def console() -> Console:
    c = Console(highlight=False)
    return c if c.is_terminal else Console(highlight=False, width=120)


# One symbol per kind of message, everywhere: ✓ done, ⚠ warning, ✗ error or nothing done, → next step or change.
def _say(symbol: str, style: str, msg: str, err: bool = False) -> None:
    out = Console(stderr=True, highlight=False) if err else console()
    out.print(Text(symbol + " ", style=style) + Text(msg), soft_wrap=True)


def ok(msg: str) -> None:
    _say("✓", "green", msg)


def warn(msg: str) -> None:
    _say("⚠", "yellow", msg, err=True)


def error(msg: str) -> None:
    _say("✗", "bold red", msg, err=True)


def aborted() -> None:
    _say("✗", "yellow", "aborted, nothing written")


def step(msg: str) -> None:
    _say("→", "cyan", msg)


def confirm(question: str) -> bool:
    return Confirm.ask(question, default=False)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def who(d: dict[str, str]) -> str:
    return f"{d['name']} <{d['email']}>".strip() if d["email"] else "(no email)"


def source(src: str) -> str:
    return {"repo": "set in the repo", "pin": "pinned in the repo", "": "nowhere"}.get(src, src)


def scan(rep: dict, show: Callable[[str], str], *, verbose: bool, suggest: bool, fix: bool, prog: str,
         again: str, exact: bool) -> None:
    """again: the command to run the fix for the same directories; exact: rep['roots'] are the repos themselves."""
    out = console()
    bad = rep["unreadable"]
    head = Text(plural(rep["repos"], "repo") + ("" if exact else f" under {', '.join(map(show, rep['roots']))}"),
                style="bold")
    n = rep.get("skipped_excluded", 0)
    skips = f"hidden directories, node_modules and {plural(n, 'excluded dir')}" if n else \
        "hidden directories and node_modules"
    head.append(("" if exact else f"  {skips} skipped")
                + (f" · {skipped(bad)}" + ("" if verbose else " (-v lists them)") if bad else ""), style="dim")
    out.print(head, soft_wrap=True)
    if verbose:
        for b in bad:
            out.print(Text(f"  ⚠ skipped {show(b['path'])}: {b['error']}", style="dim"), soft_wrap=True)

    if not fix:
        table = Table(box=None, header_style="dim", pad_edge=False, padding=(0, 2, 0, 0))
        table.add_column("repos", justify="right")
        table.add_column("identity")
        table.add_column("from", style="dim")
        if verbose:
            table.add_column("remote owners", style="dim", ratio=1)
        own = [i for i in rep["identities"] if i["source"] == "repo"]
        for i in rep["identities"]:
            if i["source"] == "repo" and not verbose:
                continue
            owners = ", ".join(f"{o} {n}" for o, n in i["owners"].items())
            table.add_row(str(i["repos"]), Text(who(i)), Text(source(i["source"])), *([Text(owners)] if verbose else []))
        if own and not verbose:
            table.add_row(str(sum(i["repos"] for i in own)), Text("see below", style="dim"), Text(source("repo")))
        out.print()
        out.print(table)

    over = rep["overrides"]
    if not over:
        out.print(Text("\n✓ no repo sets user.name or user.email itself: gitpro decides everywhere", style="green"))
        return
    out.print(Text(f"\n{plural(len(over), 'repo')} set their own user.name/user.email, which beats {prog}.", style="bold")
              + Text(" Without it they use:"), soft_wrap=True)
    # identity after fix -> identity now -> repos
    groups: dict[str, dict[str, list[dict]]] = {}
    for r in over:
        after = who(r["after"]) if r["fixable"] else ""
        groups.setdefault(after, {}).setdefault(who(r["now"]), []).append(r)
    for after, befores in sorted(groups.items(), key=lambda kv: (not kv[0], kv[0])):
        n = sum(map(len, befores.values()))
        title = Text(after, style="bold green") if after else Text("their own: nothing else sets one, so fix keeps it")
        tree = Tree(title + Text(f"  ({n} repo{'s' * (n != 1)})", style="dim"), guide_style="dim")
        for before, rs in sorted(befores.items(), key=lambda kv: (kv[0] == after, kv[0])):
            names = ", ".join(show(r["path"]) if verbose else r["short"] for r in rs)
            if before == after:
                tree.add(Text("unchanged: ", style="dim") + Text(names))
            else:
                tree.add(Text("from ", style="dim") + Text(before, style="yellow") + Text(":  ", style="dim")
                         + Text(names))
        out.print()
        out.print(tree)

    if suggest:
        out.print(f"\nTo keep an identity, paste its block into your config and run '{prog} apply' before the fix.")
        out.print("An identity applies to every repo of its owners.")
        for s in rep["suggestions"]:
            out.print()
            if out.is_terminal:
                out.print(Syntax(s["toml"], "toml", theme="ansi_dark", background_color="default"))
            else:
                out.print(s["toml"], markup=False, soft_wrap=True)
        for path in rep["unmatched"]:
            r = next(o for o in over if o["path"] == path)
            out.print(f"\n# {show(path)} ({r['owner']}): no identity can match it; after fixing it uses {who(r['after'])}",
                      markup=False, soft_wrap=True)
    elif rep["suggestions"] and not fix:
        out.print(Text(f"\nto keep some of these identities instead: {prog} scan --suggest", style="dim"), soft_wrap=True)

    if not fix:
        settings = sum(len(r["fixable"]) for r in over)
        if settings:
            out.print(Text(f"\ndry run, nothing changed. Remove {plural(settings, 'setting')} from "
                           f"{plural(sum(bool(r['fixable']) for r in over), 'repo')}: ") + Text(again, style="bold cyan"), soft_wrap=True)


def fixed(removed: list[tuple[str, str, str, str]], undo: str) -> None:
    """removed: (repo, key, value, what git uses now) for each setting."""
    out = console()
    out.print()
    for repo, key, value, now in removed:
        out.print(Text(f"  {repo}  ", style="dim") + Text(f"{key} {value}") + Text(f"  →  {now}", style="green"),
                  soft_wrap=True)
    settings, repos = len(removed), len({r for r, *_ in removed})
    out.print(Text(f"✓ removed {plural(settings, 'setting')} from {plural(repos, 'repo')}", style="green"))
    out.print(Text("↩ undo: ", style="dim") + Text(f"sh {undo}", style="bold"), soft_wrap=True)
