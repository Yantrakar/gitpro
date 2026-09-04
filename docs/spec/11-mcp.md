# 11 — `gitpro mcp`: stdio MCP server with read-only tools

Wave 3. Depends on 01 (`resolve`, `jsonout`, `jsonfile.edit_json`,
`undo.write`, `config`). Tools `lost`, `audit`, `env_for` and the trailer
part of `check_commit` need 03, 08, 12 and are registered only when those
modules import (try-import at `tools/list` time).

Files this package may create or edit: new `src/gitpro/mcp.py`,
`tests/test_mcp.py`, new `.mcp.json` at the repo root (plugin root).
Nothing else: `.claude-plugin/plugin.json` is **not** edited (V-027: the
plugin loader picks `.mcp.json` up from the plugin root by path). 17 owns
the manifest.

## 1. Purpose and user story

Agents that speak MCP (Claude Code, Claude Desktop, Cursor, Gemini CLI,
opencode, Codex) can ask gitpro directly instead of parsing CLI output:
"which identity does this repo use", "would this commit be right", "what
env do I need for `gh` here". All tools are read-only and return the same
objects as the `--json` commands.

User story: "My agent calls `why` with the repo path before committing,
gets `expected`/`effective`/`problems`, and proposes the fix command to me
instead of guessing."

## 2. CLI surface

```
gitpro mcp [-c PATH] [--log FILE]
gitpro mcp install [--agent claude|claude-desktop|cursor|gemini|opencode] [--scope user|project] [--write] [-y] [--json]
gitpro mcp uninstall [--agent …] [--scope …] [--write] [-y] [--json]
```

### 2.1 Server

`gitpro mcp` reads JSON-RPC 2.0 messages on stdin, writes responses on
stdout, logs to stderr (and `--log FILE`). Transport: newline-delimited
JSON, one message per line, as the MCP stdio transport specifies. Any
accidental `print` inside a tool must not corrupt the stream: the server
runs every tool under `contextlib.redirect_stdout(sys.stderr)` and writes
responses to the saved real stdout handle; child processes are started
with `stdin=DEVNULL` and captured output (01's `run` helpers already do
this; the server sets `GIT_TERMINAL_PROMPT=0` in its own environment at
start).

Protocol versions: `mcp.PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26",
"2025-06-18", "2025-11-25")` (newest last; the implementer updates the
constant). `initialize` echoes the client's `protocolVersion` when it is
in the tuple, else answers with the newest. `structuredContent` is
included only when the negotiated version is `2025-06-18` or newer
(older clients may reject unknown result fields).

Methods:

| method | behaviour |
|---|---|
| `initialize` | `{"protocolVersion", "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "gitpro", "version": <gitpro version>}, "instructions": "<one paragraph: tools are read-only; to change something propose the printed fix command to the user>"}` |
| `notifications/initialized`, any `notifications/*` | no reply ever (a request without `id` is a notification; replying would be a protocol error) |
| `ping` | `{}` |
| `tools/list` | tool list with JSON Schemas; `nextCursor` omitted |
| `tools/call` | `{"content": [{"type": "text", "text": json.dumps(obj)}], "structuredContent": obj (≥ 2025-06-18), "isError": false}`; a gitpro error → `isError: true`, content text `✗ <message>` |
| `resources/list`, `prompts/list` | empty lists (some clients call them unconditionally) |
| anything else | JSON-RPC error `-32601` |

Tools (`inputSchema` → result = the named `--json` object):

| tool | input | result | package |
|---|---|---|---|
| `which_identity` | `{"path": string}` | `which --json` | 01 |
| `why` | `{"path": string, "remote"?: string}` | `why --json` | 01 |
| `check_commit` | `{"path": string, "author_email"?: string, "committer_email"?: string, "remote"?: string, "message"?: string}` | `{"ok", "would_block", "mode": "block"|"warn"|"off", "problems": [...], "reason", "fix"}` — the guard's judgement (06 §2 rule, computed from `resolve.compare` here without importing `guard`; `message` is checked by `trailers.check` when 08 imports) | 01 (+08) |
| `scan` | `{"roots"?: [string], "repos"?: [string]}` | `scan --json` (never fixes; roots capped at 20 entries, each must be an existing directory) | 00 |
| `env_for` | `{"path": string, "shell"?: "sh"|"fish"|"pwsh"}` | `env --json` (commands, never values) | 12 |
| `lost` | `{"roots"?: [string], "only"?: [string]}` | `lost --json` (offline only; `online` not exposed; roots capped at 20) | 03 |
| `audit` | `{"path": string, "range"?: string, "all"?: bool}` | `audit --json` (never rewrites) | 08 |
| `identities` | `{}` | `{"config": path, "default": {...}, "identities": [{id, name, email, host, owners, ssh_alias, gh_account, glab_account, clone_dir, sign}]}` — no `[*.git]` tables, no key contents | 11 |

Every tool has `annotations: {"readOnlyHint": true, "destructiveHint":
false, "idempotentHint": true, "openWorldHint": false}`. Tools whose
module is not importable are omitted from `tools/list` and answer
`-32602 unknown tool` if called.

Config is loaded per `tools/call`. Errors in a tool never kill the
server. The server exits 0 on EOF.

### 2.2 `mcp install`

Writes the server registration for the chosen agent:

| agent | scope `user` | scope `project` |
|---|---|---|
| `claude` (Code) | preferred: run `claude mcp add --scope user --transport stdio gitpro -- gitpro mcp` when `claude` is on PATH (the CLI owns `~/.claude.json`); else edit `~/.claude.json` `mcpServers` with `edit_json` | `.mcp.json` in the repo — a **tracked team file**: refused without `--write` (DECISIONS Q5): `✗ .mcp.json is shared with your team; re-run with --write to edit it (backup + undo), or use --scope user` |
| `claude-desktop` | `~/.config/Claude/claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/…`) | — |
| `cursor` | `~/.cursor/mcp.json` | `.cursor/mcp.json` (needs `--write`) |
| `gemini` | `~/.gemini/settings.json` `mcpServers` | — |
| `opencode` | `~/.config/opencode/opencode.json` `mcp` → `{"gitpro": {"type": "local", "command": ["gitpro", "mcp"]}}` | — |

Entry: `{"gitpro": {"type": "stdio", "command": "gitpro", "args":
["mcp"]}}`. All edits go through `jsonfile.edit_json` (JSON, not JSONC;
a file with comments is refused unchanged), with preview, confirm,
backup, undo via `undo.write` (`mcp-undo-<ts>.sh`). Idempotent. Plugin
users need nothing: the repo-root `.mcp.json` (`{"mcpServers": {"gitpro":
{"type": "stdio", "command": "gitpro", "args": ["mcp"]}}}`) is loaded by
the plugin by path; `.claude-plugin/plugin.json` is not changed (V-027).
`mcp install --agent claude` prints `→ plugin users already have the
server` first.

## 3. `--json`

Not applicable to the server. `mcp install --json` (needs `-y`) →
`{"ok", "command": "mcp install", "file", "changed", "backup", "undo",
"via": "claude-cli"|"file"}`.

## 4. Config keys

None new.

## 5. VS Code surface

None.

## 6. Files created/changed

`mcp install` edits the listed JSON file (backup + undo). The server
writes nothing (`--log FILE` appends to the named file).

## 7. Failure modes

| situation | behaviour |
|---|---|
| bad JSON line on stdin | JSON-RPC error `-32700` (id `null`), continue |
| request without `method` | `-32600` |
| unknown method | `-32601` |
| unknown tool / arguments fail schema | `-32602` |
| gitpro `ConfigError` inside a tool | `isError: true`, `✗ <message>`; server continues |
| `path` not a repo / not a directory | `isError: true`, `not a git repository` |
| unexpected exception in a tool | `isError: true`, `✗ internal error: <type>`; traceback to stderr/log; server continues |
| client protocol version unknown | reply with the newest supported |
| `.mcp.json` project scope without `--write` | exit 2, nothing written |
| `claude mcp add` fails | falls back to the file edit with `⚠ claude mcp add failed (<rc>); editing ~/.claude.json directly` |

## 8. Security / privacy

Read-only by construction: no tool calls `scan.fix`, `audit.rewrite`,
`apply`, or any installer. `identities` never includes `[*.git]` tables.
It does expose every identity's name, email and account id to any MCP
client the user has registered the server with; that is the same data
`gitpro which` prints and it stays on the machine (documented in 17).
`env_for` returns commands, not values. Paths from the client are
resolved and must be directories the user can read. Stderr logging names
the tool only; `--log FILE` logs full requests (documented). The server
never prompts and never reads a terminal.

## 9. Out of scope

Resources and prompts, HTTP/SSE transport, write tools (README rule 8.12),
sampling, `listChanged` notifications.

## 10. Acceptance tests (`tests/test_mcp.py`)

Drive `mcp.serve(stdin, stdout, cfg_path, log=None)` in-process with
`io.StringIO`, plus one subprocess test via `python -m gitpro mcp`.

1. `initialize` → `protocolVersion == mcp.PROTOCOL_VERSIONS[-1]`,
   `serverInfo.name == "gitpro"`, `serverInfo.version ==
   gitpro.__version__`, `capabilities.tools`.
2. `initialize` with `"2024-11-05"` → echoed back; a later
   `tools/call` result has **no** `structuredContent`; with
   `"2025-06-18"` → has it.
3. `notifications/initialized` → nothing written to stdout (stdout is
   byte-empty after it); `ping` → `{}`.
4. `tools/list` → includes `which_identity`, `why`, `check_commit`,
   `scan`, `identities`; each has `inputSchema` and
   `annotations.readOnlyHint`; with `lost`/`audit`/`envcmd` importable
   they appear too (parametrise on availability).
5. `tools/call which_identity {path}` → `structuredContent.ok` true and
   `settings["user.email"].value` as `which --json`.
6. `tools/call why {path}` with an override → `problems[0].code ==
   "override"`, `isError` false.
7. `check_commit {path, committer_email: "me@personal.test"}` →
   `would_block` true, `fix` present; `author_email: "pat@other.test"`
   with the right committer → `would_block` false; `[agents] hook =
   "warn"` → `would_block` false, `mode` warn.
8. `scan {repos: [path]}` → same as `scan --json --repo path`; the
   repo's `.git/config` is unchanged afterwards; `roots` with 21 entries →
   `-32602`.
9. `identities {}` → ids and emails; no `git` tables; no key contents.
10. Unknown tool → `-32602`; missing `path` → `-32602`; invalid JSON line
    → `-32700`; missing `method` → `-32600`; the next request is still
    answered.
11. `path` not a repo → `isError: true`, text contains `not a git
    repository`. A tool monkeypatched to `print("oops")` → `oops` appears
    on stderr, stdout still holds exactly one valid JSON line per request.
12. No config and `which_identity` → works; `why` → `isError: true` with
    `no config found`.
13. Subprocess: `python -m gitpro mcp` with `initialize`, `tools/list` on
    stdin → two JSON lines on stdout, exit 0 at EOF; stderr contains no
    tool arguments.
14. `mcp install --agent claude -y` with no `claude` on PATH → writes
    `~/.claude.json` `mcpServers.gitpro`, backup + undo; idempotent. With
    fake `claude` on `fake_bin` → `claude mcp add --scope user …` logged,
    `~/.claude.json` untouched, `via == "claude-cli"`.
15. `--scope project` without `--write` → exit 2, no `.mcp.json`; with
    `--write -y` → written with backup when it existed; a `.mcp.json`
    with a `//` comment → refused, unchanged.
16. Static: repo-root `.mcp.json` parses and names `gitpro mcp`;
    `.claude-plugin/plugin.json` parses and has no `mcpServers` key.
17. `tools/call` on a `lost` tool when `gitpro.lost` is absent
    (monkeypatch `sys.modules["gitpro.lost"] = None`) → `-32602`.

## 11. Modules touched

New `mcp.py` (`PROTOCOL_VERSIONS`, `serve(stdin, stdout, cfg_path, log)`,
`TOOLS` registry with lazy imports, `install/uninstall`, `register`),
new root `.mcp.json`. `.claude-plugin/plugin.json` is not touched.

## 12. Docs text (for 17)

> `gitpro mcp` is a stdio MCP server with read-only tools:
> `which_identity`, `why`, `check_commit`, `scan`, `identities`, plus
> `lost`, `audit`, `env_for` when installed. Register it with `gitpro mcp
> install --agent claude|cursor|gemini|opencode` or by installing the
> gitpro plugin. It never writes; it tells the agent the command that
> would. Project-level `.mcp.json` is a team file: `--write` is required.
