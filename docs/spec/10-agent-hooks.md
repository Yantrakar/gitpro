# 10 — AI agent hooks: Claude Code PreToolUse (and experimental equivalents)

Wave 2. Depends on 01 only (`resolve`, `[agents]`, `jsonfile.edit_json`,
`undo.write`, `hot`). 05 is **not** a dependency: the hook never calls
`gh`.

Files this package may create or edit: new `src/gitpro/agents.py`,
`tests/test_agents.py`, new `hooks/hooks.json` at the repo root. Nothing
else: `.claude-plugin/plugin.json` is **not** edited (V-027: the plugin
loader picks up `hooks/hooks.json` and `.mcp.json` by path from the
plugin root; the manifest's `hooks`/`mcpServers` keys are for other
locations only). 17 owns the manifest.

## 1. Purpose and user story

AI agents run `git commit`, `git push`, `gh pr create` in whatever repo
they are pointed at, often with a stale `GIT_AUTHOR_EMAIL`, in a
devcontainer without the host's config, or in the wrong clone. The git
guard (06) catches commits at hook time; the agent hook catches the **tool
call** before the shell runs and gives the agent a message it can act on.
It also covers `gh`/`glab` commands that bypass git hooks.

User story: "Claude Code is about to run `git commit -m …` in
`~/work/app`. The PreToolUse hook runs `gitpro agent-check`, sees the
repo-local override, denies the call with a reason that tells Claude to ask
me. I say 'fix it', Claude runs `git config --unset user.email`, commits."

Two rules from DECISIONS.md Q6 shape everything below:

- the hook **never emits an allow decision**. Either it says nothing (the
  agent's normal permission flow continues) or it denies.
- `warn` mode = no decision at all plus a visible message on stderr; the
  agent's permission flow is unchanged.

## 2. CLI surface

```
gitpro agent-check [--agent claude|cursor|gemini|generic] [--cwd DIR]       # reads the agent's JSON on stdin
gitpro agent install [--agent claude] [--scope user|project] [-y] [--json]
gitpro agent uninstall [--agent claude] [--scope user|project] [-y] [--json]
gitpro agent status [--json]
```

`agent-check` is a **hot path** command (`hot.HOT` in 01): `agents.main(argv)`
is called by `gitpro.hot:main` before the full parser is built. Module
level imports: stdlib, `gitpro.config`, `gitpro.resolve`. `agent install`
and friends are registered normally by `agents.register`.

### 2.1 `agent-check`

Input: the agent's hook payload on stdin. Claude Code `PreToolUse`:

```json
{"session_id": "...", "cwd": "/home/me/work/app", "hook_event_name": "PreToolUse",
 "tool_name": "Bash", "tool_input": {"command": "git commit -m 'x'", "description": "..."}}
```

Logic (`agents.decide(payload, cfg, agent) -> Decision | None`):

1. Only `tool_name == "Bash"` (Claude) matters; other tools → return
   before anything else: no output, exit 0, no git call.
2. Extract the command and tokenise it (`agents.parse_command`):
   `lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")`,
   `lex.whitespace = " \t\r"`, `lex.whitespace_split = True` (V-026:
   with the default `punctuation_chars=True` a newline is whitespace and
   two lines merge into one command; naming the characters makes `\n` a
   token). Then: every token consisting only of punctuation characters is
   a **separator** (`;`, `&&`, `||`, `|`, `&`, `\n`, `(`, `)`, and odd
   runs like `|&` or `;;`); `>`, `>>`, `<`, `>&`, `2>`-style tokens (a
   digit glued to `>` is split by `whitespace_split`; the digit is a
   plain word, harmless) are **redirections** and consume the next word;
   the rest are words of the current simple command. Shell-quoted words
   come out unquoted (posix mode), so `git commit -m 'a; b'` is one
   command. For `sh -c '<string>'`, `bash -c`, `zsh -c`, `env … sh -c`,
   and for every word containing a backtick pair or `$(…)`, recurse into
   the inner string (backticks: the text between the first and the next
   unescaped backtick; `$(`: balanced parentheses). Depth is capped at 4.
   Tokeniser failure (unbalanced quote) → **conservative fallback**: a regex scan
   for `\bgit\b\s+(commit|push|am|cherry-pick|rebase|merge|revert|tag)\b`
   and `\bgh\b\s+(pr|release|repo|api)\b`; a match is treated as a
   relevant command in `cwd` with no overrides. For each simple command
   take the first word after `env`, `VAR=val` prefixes, `sudo`,
   `command`, `nice`, `time`, `xargs`:
   - `git` with subcommand in `{commit, push, am, cherry-pick, rebase,
     merge, revert, tag, notes, commit-tree}` → identity-relevant. `git
     -C DIR` sets the repo dir; `--author=…`/`--author …`, `-c
     user.email=…`, `-c user.name=…`, `GIT_AUTHOR_EMAIL=`/
     `GIT_COMMITTER_EMAIL=` prefixes are read as overrides.
   - `gh` with `{pr create, pr merge, pr close, release create, repo
     create, repo delete, api -X POST|PUT|PATCH|DELETE}`, `glab` with
     `{mr create, mr merge, release create, repo create}` →
     forge-relevant.
   - anything else → ignored.
   - a `cd X` earlier in the same simple-command chain (`cd X && git
     …`) moves the repo dir for the commands after it; relative `X` is
     joined to `cwd`.
3. Repo dir = `-C` arg (a relative `-C` is joined to the `cd` dir, else
   to `cwd`), else `cd` prefix, else `--cwd`, else payload `cwd`. Not a
   repo → nothing (git will complain itself).
4. `report = resolve.compare(repo)` (push: with the remote named in the
   command when present). Committer email = `-c user.email` or
   `GIT_COMMITTER_EMAIL=` prefix or `report.effective.email`; author email =
   `--author` or `GIT_AUTHOR_EMAIL=` or the committer email. The
   same judgement as 06 §2: committer ≠ expected → mismatch; author ≠
   expected only when the author is one of the user's own emails.
5. Problems: a mismatch, or `report.problems` with code in `override`,
   `pin_stale`, `no_identity`, `env_override` → decision per `[agents]
   hook`:
   - `block` (default once installed) → **deny**
   - `warn` → no decision; text on stderr; exit 0
   - `off` → nothing
   Forge commands: checked for the identity problems above only (no `gh
   auth status` call; the agent's `gh` runs with whatever env it has;
   `gitpro env` (12) is the tool for that). `GITPRO_SKIP` non-empty in
   the hook's environment → nothing, with one stderr line `gitpro
   agent-check: skipped (GITPRO_SKIP)`.

Deny output for Claude Code (stdout, exit 0 — Claude reads the JSON):

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
 "permissionDecisionReason": "gitpro: this commit would be committed as me@personal.test but origin (git@github.com:corp-org/app.git) belongs to identity work-github <me@corp.test>. cause: user.email is set in the repo's .git/config. Ask the user before changing anything; the fix is `git -C /home/me/work/app config --unset user.email`; the user can bypass with GITPRO_SKIP=1."}}
```

The reason always ends with the three parts *cause*, *ask the user*, *fix
command* in that order, so the agent proposes rather than acts. The same
text is also written to stderr prefixed `✗ gitpro:` (visible in the
transcript). gitpro does **not** use exit 2 (Claude's older "block"
signal) because the JSON form carries the reason; see §12.

Warn output: stderr only:

```
⚠ gitpro: this commit would be committed as me@personal.test but origin … belongs to work-github <me@corp.test>; fix: git -C … config --unset user.email
```

exit 0, **nothing on stdout**. Off: nothing at all.

There is no code path that prints `"allow"`, `"permissionDecision":
"allow"`, `additionalContext`, or any other positive decision (test 16).

Other agents (`--agent`), all **experimental** (DECISIONS Q10): the
decision logic is shared, the envelope differs, and the field names are
isolated in `agents.ADAPTERS` and marked `verified = False` until an
implementer checks them against the tool's docs (README §13):

| agent | input | deny output | warn |
|---|---|---|---|
| `cursor` | `{"command", "cwd"}` (Cursor `beforeShellExecution`) | `{"permission": "deny", "userMessage": "...", "agentMessage": "..."}` | stderr only |
| `gemini` | `{"tool_name", "tool_input": {"command"}, "cwd"}` (Gemini CLI `BeforeTool`) | `{"decision": "deny", "reason": "..."}` | stderr only |
| `generic` | `{"command": "...", "cwd": "..."}` | stderr text, exit 1 | stderr, exit 0 |

`agent install --agent cursor|gemini` prints `⚠ experimental: field names
unverified; check the hook JSON against <tool>'s docs` first and needs
`--experimental` to proceed. Codex and opencode: `agent install --agent
codex` prints `⚠ codex has no pre-tool hook; use the git guard (gitpro
guard install) and the MCP server (gitpro mcp) instead`, exit 0.

### 2.2 `agent install`

Claude Code, user scope: edits `~/.claude/settings.json`. Project scope:
`.claude/settings.local.json` in the repo (**not** `.claude/settings.json`,
which is a tracked team file; DECISIONS Q5). Adds, idempotently:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
  "command": "sh -c 'command -v gitpro >/dev/null 2>&1 || exit 0; exec gitpro agent-check --agent claude'",
  "timeout": 10}]}]}}
```

The `command -v` prefix means a machine (remote SSH, container) without
gitpro gets no hook error. Entries are recognised by `command` containing
`gitpro agent-check` (fixed marker; a `__gitpro__` key is **not** added,
since unknown keys may be rejected by the agent). Written with
`jsonfile.edit_json(path, mutate, command="agent install", create={},
confirm=…)` (01): preview diff, confirmation, backup
`~/.local/state/gitpro/backup-<ts>/settings.json` (01 §5.7: backups
never sit next to the file), atomic write preserving other keys, undo script
via `undo.write`. Existing entry → `✓ already installed, nothing to do`,
exit 0. `~/.claude/settings.json` is plain JSON (not JSONC); a file with
comments is refused by `edit_json` with the file untouched.

```
  update  ~/.claude/settings.json   (+ hooks.PreToolUse: gitpro agent-check --agent claude)
install the Claude Code hook? [y/n]
✓ installed; Claude Code picks it up on its next session
↩ undo: sh ~/.local/state/gitpro/agent-undo-20261002-101500.sh   (restores from ~/.local/state/gitpro/backup-20261002-101500/settings.json)
```

The **plugin route**: the repo-root `hooks/hooks.json` (the plugin root is
the repo root, README §12) carries the same entry:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
  "command": "sh -c 'command -v gitpro >/dev/null 2>&1 || exit 0; exec gitpro agent-check --agent claude'", "timeout": 10}]}]}}
```

and nothing is added to `.claude-plugin/plugin.json`: Claude Code loads
`hooks/hooks.json` from the plugin root by path (V-027). `agent install
--agent claude` first prints `→ plugin users already have this hook
(gitpro agent status shows it)`; installing both is harmless (the same
command runs twice and gives the same answer).

The installed entry and the plugin entry both start with `command -v
gitpro`, which runs in the agent's own shell environment. Claude Code's
hook shell may not have the user's `~/.local/bin` on PATH (a desktop
launch, a container); `agent status` (2.3) checks this (V-028).

### 2.3 `agent status`

```
agents:   [agents] hook = block
  claude   hook: user ~/.claude/settings.json; plugin: enabled (gitpro@sandeepshelke)
  cursor   not installed (experimental) → gitpro agent install --agent cursor --experimental
  gemini   not installed (experimental)
  codex    no pre-tool hook; covered by the git guard (installed) and MCP
```

Plugin detection: `~/.claude/settings.json` `enabledPlugins` contains a
key starting with `gitpro@` (and `.claude/settings.local.json` of the cwd
likewise). Guard status via `gitpro.guard.status()["installed"]` when
`gitpro.guard` imports (06 §3.6), else `null`.

`gitpro on PATH` line (V-028): `agent status` runs `sh -lc 'command -v
gitpro'` **and** `sh -c 'command -v gitpro'` (login shell vs. the plain
`sh` the hook entry uses). When the plain one fails it prints `⚠ gitpro
is not on PATH for sh (found by login shell at <path>); the hook exits
silently without it → gitpro agent install --agent claude rewrites the
entry with the absolute path`. `agent install` writes the entry as
`sh -c 'command -v gitpro >/dev/null 2>&1 || exec <abs path> "$@"; exec
gitpro agent-check --agent claude' sh --agent claude`-style: the plain
PATH first, then the absolute path of the installing gitpro
(`__GITPRO_BIN__` from 06 §3.2 has the same meaning), then exit 0. The
plugin's `hooks/hooks.json` cannot know a path and stays PATH-only; its
limitation is in the plugin description (17).

Project scope (V-028): after creating `.claude/settings.local.json`,
`agent install --scope project` runs `git check-ignore -q
.claude/settings.local.json`; when it is not ignored, it appends the path
to `<git dir>/info/exclude` (a local, untracked file; backed up and in
the undo script) and prints `→ added .claude/settings.local.json to
.git/info/exclude so it is not committed`.

Capabilities registered: `agent-check`, `agent.install`, `agent.status`.

## 3. `--json`

`agent status --json`: `{"ok": true, "command": "agent status", "mode":
"block", "agents": {"claude": {"user": "/home/me/.claude/settings.json"|null,
"project": "/repo/.claude/settings.local.json"|null, "plugin":
"gitpro@sandeepshelke"|null, "experimental": false}, "cursor": {...,
"experimental": true}, ...}, "guard_installed": true|false|null}`. `agent
install/uninstall --json` (needs `-y`): `{"ok", "command", "file",
"changed", "backup", "undo"}` (fields from `jsonfile.Result`).
`agent-check` output is the agent envelope, not the gitpro envelope.

## 4. Config keys

`[agents] hook = "block"|"warn"|"off"` (default `block` — the hook only
exists once installed; `off` keeps it installed but silent); `bot_emails`
(08). `[guard] on_mismatch` is **not** consulted here.

## 5. VS Code surface

None.

## 6. Files created/changed and undo

`~/.claude/settings.json` or `<repo>/.claude/settings.local.json`, backed
up before the change; undo restores the backup. `uninstall` removes only
the gitpro entry and empty `hooks`/`PreToolUse` containers it leaves
behind, same backup/undo. Cursor `~/.cursor/hooks.json`, Gemini
`~/.gemini/settings.json`: same mechanism (experimental). `gitpro
uninstall` (00) does **not** remove agent hooks; `agent status` reminds.

## 7. Failure modes

| situation | message | exit |
|---|---|---|
| stdin not JSON / empty | stderr `⚠ gitpro agent-check: cannot parse hook input` | 0, no stdout |
| any exception in the check | stderr `⚠ gitpro agent-check: <msg>` (fail open: no decision) | 0 |
| config missing | nothing (no identities → nothing to judge) | 0 |
| `settings.json` invalid JSON | `✗ ~/.claude/settings.json is not valid JSON; fix it by hand (nothing written)` | 2 |
| `settings.json` symlink | written through (`atomic_write` follows symlinks, 01) | — |
| `--agent codex` install | `⚠ … no pre-tool hook …` | 0 |
| `--agent cursor` without `--experimental` | `✗ cursor adapter is experimental; add --experimental` | 2 |

Time budget: `agent-check` < 150 ms on the ignore path (parse, early
return before any git call). Test 17 checks import time.

## 8. Security / privacy

The hook receives the full command line and prints nothing of it except
the git/gh subcommand name and the override values it judged (an email).
It never logs commands. `settings.json` may contain other hooks and env;
gitpro changes only its own entry through `edit_json` with a backup. The
hook never contacts the network and never runs `gh`.

## 9. Out of scope

Hooks for MCP tool calls (agents that commit through an MCP git server
are covered by the git guard), Windows, Codex/opencode (no pre-tool hook
at spec time).

## 10. Acceptance tests (`tests/test_agents.py`)

Use `cfg()` and `repo_with_remote`. Run checks through `agents.main`
with `monkeypatch.setattr("sys.stdin", io.StringIO(payload))` and
`capsys`; `settings.json` tests through `cli.main` with `home`.

1. Payload `tool_name: "Read"` → no output, exit 0; monkeypatched
   `subprocess.run` counter stays 0.
2. `Bash` with `ls -la` → no output, exit 0, no git call.
3. `git commit -m x` in a correct repo → no output (**not** an allow
   JSON), exit 0.
4. Repo-local wrong `user.email` → stdout JSON `permissionDecision ==
   "deny"`, reason contains `config --unset user.email`, `Ask the user`
   and `GITPRO_SKIP`; stderr starts `✗ gitpro:`; exit 0.
5. `[agents] hook = "warn"` → **stdout empty**, stderr `⚠ gitpro:`, exit
   0; `"off"` → no output at all.
6. Command parsing: `cd /other/repo && git push origin main` → repo from
   `cd`; `git -C /r push` → from `-C`; `GIT_AUTHOR_EMAIL=x@y git commit`
   → author `x@y` (own email → deny; foreign email → no decision);
   `GIT_COMMITTER_EMAIL=x@y git commit` → committer `x@y` → deny;
   `echo "git commit" ` → no decision (the string is an argument);
   `sh -c 'cd /r && git commit -m x'` → recursed; `git log | git commit`
   → the second command checked; a command with an unbalanced quote
   containing `git commit` → fallback path, judged in `cwd`.
7. `git commit --author="Me <me@personal.test>"` (own email) → deny
   naming `me@personal.test`; `--author="Pat <pat@other.test>"` → no
   decision.
8. `gh pr create` in a repo with a `no_identity` problem → deny; in a
   good repo → nothing; no `gh` fake needed (no `gh` call, fake_bin log
   empty).
9. `--agent generic` with a mismatch → stderr text, exit 1; `--agent
   cursor` → JSON `permission == "deny"`; `--agent gemini` → `decision ==
   "deny"`; each with a clean repo → no output.
10. Malformed stdin → stderr `cannot parse`, exit 0, stdout empty.
    `GITPRO_SKIP=1` in env → stdout empty, stderr `skipped`.
11. `agent install -y` into an empty home → `~/.claude/settings.json`
    created with the entry whose `command` contains `command -v gitpro`
    and `gitpro agent-check --agent claude`; second run → `already
    installed`; existing file with other hooks → merged, other hooks
    intact, backup exists, undo script restores it byte-for-byte.
12. `agent install` with invalid JSON in settings → exit 2, file
    unchanged. `--scope project` → `.claude/settings.local.json` written,
    `.claude/settings.json` untouched (or absent).
13. `agent uninstall -y` → entry removed, empty containers removed, other
    keys intact.
14. `agent status --json` reflects user/project/plugin installs
    (`enabledPlugins: {"gitpro@sandeepshelke": true}` → `plugin` set).
15. Static: repo-root `hooks/hooks.json` parses, its command contains
    `gitpro agent-check --agent claude`; `.claude-plugin/plugin.json`
    parses and has **no** `hooks` key (10 does not edit it).
15a. Tokeniser (V-026): `"git status\ngit commit -m x"` → two commands,
    the second relevant; `"echo hi > out && git push"` → `git push` found,
    `out` is not a command; `"git commit -m 'a; b'"` → one command;
    `"echo $(git commit -m x)"` and `` "echo `git push`" `` → the inner
    command found; `"(cd sub && git commit -m x)"` → repo dir `cwd/sub`;
    `"cd sub && git -C ../other commit"` → repo dir `cwd/other`; `"ls |&
    cat"` → no command.
15b. `agent status` with `gitpro` on PATH only for a login shell (fake
    `sh` that fails `command -v` unless `-l`) → the `⚠ gitpro is not on
    PATH for sh` line; `agent install -y` then writes an entry containing
    the absolute path of the fake gitpro.
15c. `agent install --scope project -y` in a repo without a `.gitignore`
    → `.git/info/exclude` contains `.claude/settings.local.json`, the
    `→ added` line is printed; with the file already ignored → exclude
    untouched.
16. For every mode (`block`, `warn`, `off`), every adapter and every
    payload in tests 1–10: no stdout or stderr output contains the
    substring `"allow"` or `additionalContext`.
17. `python -X importtime -c "import gitpro.agents"` shows no `rich`,
    `gitpro.cli`, `gitpro.view`, `gitpro.forge`.
18. `--agent cursor` install without `--experimental` → exit 2, nothing
    written.

## 11. Modules touched

New `agents.py` (`ADAPTERS`, `parse_command(cmd) -> list[Call]`,
`decide(payload, cfg, agent)`, `add_arguments(p)`, `main(argv)`,
`install/uninstall/status`, `register`), new `hooks/hooks.json`.
`.claude-plugin/plugin.json` is not touched.

## 12. Decisions

- Deny: JSON on stdout + text on stderr, exit 0. Not exit 2: it would
  also block but loses the structured reason.
- No allow, ever (Q6). gitpro's silence means "no opinion"; the agent's
  own permission settings apply.
- `warn` is stderr-only so the message reaches the user without altering
  the agent's decision.
- Fail open: a broken gitpro must never stop an agent; the git guard is
  the second line.
- Project scope writes `settings.local.json` (untracked by convention).

## 13. Docs text (for 17)

> `gitpro agent install` adds a Claude Code `PreToolUse` hook (plugin
> users have it already). Before the agent runs `git commit`, `git push`,
> `gh pr create` and similar, `gitpro agent-check` verifies the repo's
> identity and, on a mismatch, denies the call with a reason that asks the
> agent to check with you. It never grants anything. `[agents] hook =
> "warn"` only prints a warning. Cursor and Gemini adapters are
> experimental.
