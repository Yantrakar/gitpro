"""Interactive scan --fix (optional: pip install 'gitpro[tui]'): tick the repos to fix, preview, confirm."""
from __future__ import annotations

from typing import Callable, List, Optional

from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, Static

from .view import plural, who

ON, OFF = "☑", "☐"


class Ask(ModalScreen[bool]):
    BINDINGS = [Binding("y", "answer(True)", "Remove"), Binding("n,escape", "answer(False)", "Cancel")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question)
            with Horizontal(classes="buttons"):
                yield Button("Remove", variant="error", id="yes")
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class Suggestion(ModalScreen[None]):
    BINDINGS = [Binding("c", "copy", "Copy"), Binding("escape,q,s", "app.pop_screen", "Close")]

    def __init__(self, toml: str) -> None:
        super().__init__()
        self.toml = toml

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("To keep this identity, paste the block into your config and run 'gitpro setup'. "
                         "The repos it matches then keep it after the fix.\n")
            yield Static(Syntax(self.toml, "toml", theme="ansi_dark", background_color="default"))
            yield Footer()

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.toml)
        self.notify("copied")


class ScanApp(App[Optional[List[str]]]):  # not list[str] | None: evaluated at runtime on 3.9
    TITLE = "gitpro scan"
    CSS = """
    #target { padding: 0 1; color: $text-muted; }
    DataTable { height: 1fr; }
    #detail { height: auto; max-height: 12; border-top: solid $primary; padding: 0 1; }
    Ask, Suggestion { align: center middle; }
    .dialog Static { width: 100%; }
    .dialog { width: 90%; max-width: 100; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 2; }
    """
    BINDINGS = [
        Binding("space", "toggle", "Tick"),
        Binding("a", "toggle_all", "All"),
        Binding("s", "suggest", "Keep identity"),
        Binding("enter", "fix", "Fix ticked", priority=True),
        Binding("q,escape", "quit", "Quit"),
    ]

    def __init__(self, rep: dict, show: Callable[[str], str]) -> None:
        super().__init__()
        self.rep, self.show = rep, show
        fixable = [r for r in rep["overrides"] if r["fixable"]]
        # Same order as the plain output: by identity after the fix, then by the identity now, unchanged last.
        self.rows = sorted(fixable, key=lambda r: (who(r["after"]), who(r["now"]) == who(r["after"]),
                                                   who(r["now"]), r["short"]))
        self.ticked = {r["path"] for r in self.rows}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="target")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column(" ", key="tick")
        targets = {who(r["after"]) for r in self.rows}
        one = len(targets) == 1
        table.add_columns("repo", "now (yellow: changes)" if one else "now", *([] if one else ["after the fix"]))
        target = self.query_one("#target", Static)
        if one:
            target.update(Text("After the fix they all use ") + Text(targets.pop(), style="bold green"))
        else:
            target.display = False
        for r in self.rows:
            now, after = who(r["now"]), who(r["after"])
            cells = [Text(now, style="yellow" if now != after else "dim")]
            if not one:
                cells.append(Text("unchanged", style="dim") if now == after else Text(after, style="green"))
            table.add_row(ON, r["short"], *cells, key=r["path"])
        self.sub_title = self.counts()
        table.focus()

    def counts(self) -> str:
        n = sum(len(r["fixable"]) for r in self.rows if r["path"] in self.ticked)
        return f"{plural(len(self.ticked), 'repo')} ticked, {plural(n, 'setting')} to remove"

    def current(self) -> dict | None:
        table = self.query_one(DataTable)
        return self.rows[table.cursor_row] if self.rows else None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        r = self.current()
        if not r:
            return
        text = Text(self.show(r["path"]), style="bold")
        text.append(f"   {r['owner']}\n", style="dim")
        for s in r["settings"]:
            text.append(f"  {s['key']:<11} {s['value']}")
            text.append("  →  " + s["without"] if s["without"] else "  (kept: nothing else sets it)",
                        style="green" if s["without"] and s["without"] != s["value"] else "dim")
            text.append("\n")
        if self.suggestion(r):
            text.append("s: keep this identity with a gitpro identity instead", style="dim")
        self.query_one("#detail", Static).update(text)

    def suggestion(self, r: dict) -> dict | None:
        return next((s for s in self.rep["suggestions"] if s["email"] == r["now"]["email"]
                     and r["owner"] in {f"{s['host']}/{o}" for o in s["owners"]}), None)

    def action_toggle(self) -> None:
        r = self.current()
        if r:
            self.ticked ^= {r["path"]}
            self.query_one(DataTable).update_cell(r["path"], "tick", ON if r["path"] in self.ticked else OFF)
            self.sub_title = self.counts()

    def action_toggle_all(self) -> None:
        self.ticked = set() if self.ticked else {r["path"] for r in self.rows}
        table = self.query_one(DataTable)
        for r in self.rows:
            table.update_cell(r["path"], "tick", ON if r["path"] in self.ticked else OFF)
        self.sub_title = self.counts()

    def action_suggest(self) -> None:
        r = self.current()
        s = r and self.suggestion(r)
        if s:
            self.push_screen(Suggestion(s["toml"]))
        else:
            self.notify("no identity can keep this one: it has no remote gitpro can match", severity="warning")

    def action_fix(self) -> None:
        if not self.ticked:
            self.notify("nothing ticked", severity="warning")
            return
        n = sum(len(r["fixable"]) for r in self.rows if r["path"] in self.ticked)
        self.push_screen(Ask(f"Remove {plural(n, 'setting')} from {plural(len(self.ticked), 'repo')}?"),
                         lambda yes: self.exit(sorted(self.ticked)) if yes else None)

    def action_quit(self) -> None:
        self.exit(None)


def choose(rep: dict, show: Callable[[str], str]) -> list[str] | None:
    """Paths of the repos to fix, or None if the user quit."""
    return ScanApp(rep, show).run()
