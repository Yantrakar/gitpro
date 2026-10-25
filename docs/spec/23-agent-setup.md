# 23 — Agent setup: commit verdicts, agent coverage and the shipped skill

Depends on 01 (`resolve`, `why`), 06 (guard), 10 (agent hooks), 11 (MCP), 17 (skill and docs),
22 (commands). Facts below are as of the branch `fix/agent-setup`, which starts from 0.4.5.

This package answers a review of how an unattended agent uses gitpro (a headless Copilot
agent had the skill but not the CLI, never committed, and wrote its DONE file early).

Files this package edits:
- `src/gitpro/cli.py` (`cmd_scan`), `src/gitpro/guard.py`, `src/gitpro/commitcheck.py`,
  `src/gitpro/resolve.py`, `src/gitpro/mcp.py`, `src/gitpro/setup.py`, new
  `src/gitpro/skillcopy.py`, `pyproject.toml`;
- `skills/gitpro/SKILL.md`, `docs/guide.md`, `CHANGELOG.md`, and the spec text named in section 7;
- tests: new `tests/test_agent_setup_023.py`, and `tests/test_surface.py`, `test_setup.py`,
  `test_cli_core.py`, `test_end_to_end.py`, `test_review_040.py`.

Not edited (off limits for this change): `site/`, `.github/`, `releasepro.toml`, `action.yml`,
`server.json`, `src/gitpro/VERSION`.

`docs/spec/README.md` section 9 lists only packages 01 to 17, so this package has no row
there.

## 1. Facts (before the change)

1. `scan --fix --json` without `-y` wrote: `cli.py` previewed only for `-n`
   (`args.dry_run and preview_only(args)`). `tests/test_review_040.py` pinned that, and
   00-existing known issue 2, 22-commands R4, DECISIONS "Consent in 0.4.x" and the guide
   recorded it as known.
2. `commit.would_block` came from `commitcheck.judge`, which uses `cfg.agents.hook`
   (`commitcheck.py:127`). The Git guard uses `[guard] on_mismatch`. The guard allows a
   commit when gitpro has no expected email (`guard.py:286`), while `commitcheck.GUARD_CODES`
   includes `no_identity`. `why` returns 0 whenever the repo has no problem
   (`resolve.py:1096`), whatever `commit.problems` holds; `tests/test_surface.py` expects exit
   0 for a wrong committer.
3. A repo with no remote and no `[default]` name and email got a `no_identity` problem whose
   fix said `gitpro setup --add PATH`, which cannot help (there is no remote to learn from),
   and the text said "git will refuse to commit" even when git had its own email.
4. The MCP `why` tool turned a config error into a plain text error; `why --json` printed an
   object (`error`, `config: null`, and the repo fields).
5. `setup` named agents (`--agent`) with no hook and no MCP entry (Codex) only by silence,
   and did not accept Copilot CLI. Nothing said which agent had which part installed.
6. The skill lived only in `skills/gitpro/SKILL.md`; the wheel
   (`packages = ["src/gitpro"]`) and the sdist (`/src/gitpro`, `/README.md`, `/LICENSE`) did
   not contain it. Nothing compared an installed copy to the repo's.
7. The shipped skill had the Install and "Before committing" text of 0.4.0: no rule for a
   missing CLI, a blocked commit, a repo with no remote, or push and PR; its tables lacked
   `setup --write` and `scan --suggest`.

## 2. Principles used

- Facts only. Where gitpro cannot check, it says so ("installed" means the entry is in the
  agent's config file, not that the agent loaded it).
- A preview never writes. Writes follow gitpro's write rules: shown first, confirmed,
  atomic, backup, an undo line, and through a symlink never replacing it.
- No new dependency. Python 3.9 forms only (`from __future__ import annotations`,
  `typing` generics).

## 3. Changes: commands and JSON

### 3.1 `scan --fix --json` previews without `-y`

`cli.py:498`: `preview` is true for `--fix` when `-n` asks for it, or when `--json` is given
without `-y`. The preview prints the JSON object with `"dry_run": true` and no `removed` or
`undo`, exits as `-n` does, and changes nothing. `scan --fix --json -y` writes and prints
`removed` and `undo` as before. The VS Code extension runs `scan --fix --yes --json`
(`vscode/src/scan.ts`), so it is unaffected.

Why: a documented preview must not change repository config, and the exception existed only
for 0.3 compatibility.

### 3.2 `commit.guard_would_block`

`why --json` and the MCP tool `why` add `commit.guard_would_block`. It is
`guard.commit_verdict` (`guard.py:312`), which runs the guard's own `identity_commit` check
with no output and no denial record, plus a `trailer` problem when `[guard] on_mismatch` is
`block` (`commitcheck.py:103`). Team-policy hooks and `GITPRO_SKIP` are not evaluated.
`commit.would_block` and `commit.mode` stay the agent hook's verdict (`[agents] hook`) and are
documented as that. `why` still exits 0 for a wrong committer; callers read `commit.problems`.

### 3.3 A repo with no remote

`resolve.py:849`: with no identity candidates the `no_identity` fix is "add a name and email
under [default] in <config>", not `setup --add`. `resolve.py:1014`: the expected line reads
"gitpro has no identity for this repo (no name/email in [default]); " then "git will use
<email> from <origin>" when git has an effective email, else "git will refuse to commit". With
candidates the fix is unchanged.

### 3.4 MCP `why` errors match `why --json`

`resolve.why_error(e, directory)` (`resolve.py:1061`) builds the error object once:
`{"error", "config": null, <repo fields when directory is a repo>}`. `cmd_why` prints it
(exit 2). The MCP `tool_why` raises `ToolError(message, data)` with the same object in the
envelope, and `Server.failed_with` returns it as text, with `structuredContent` for protocol
versions that have it, and `isError: true`. An explicit `-c` config that fails stays a plain
error, as the CLI does. `Server.failed(message)` is unchanged.

### 3.5 `setup`: coverage per agent

- `--agent` accepts `copilot` (GitHub Copilot CLI), only so that asking for it gets a clear
  answer. `setup.NO_COVERAGE` holds the reason for Codex and Copilot.
- `setup -n --json` has `agents`: one row per agent setup looks at,
  `{"agent", "hook", "mcp"[, "reason"]}`, each status `installed`, `plugin`, `missing` or
  `unavailable`. `plugin` is Claude Code with the gitpro plugin enabled (a key in
  `enabledPlugins`; whether the plugin loaded is not checked).
- `uncovered`: a list of lines for agents named with `--agent` that have neither a hook nor an
  MCP entry. The text shows each as a red `✗` line. `ok` is false and the exit code is 1 when
  `pending` or `uncovered` is not empty, in `-n` and in `-y` (after the work that can be done).
  Without `--agent` such an agent is a note only.
- Hooks: Claude Code, Cursor, Gemini CLI (`agents.HOOK_AGENTS`). MCP server entries: Claude
  Code, Claude Desktop, Cursor, Gemini CLI, opencode (`mcp.AGENTS`). Codex has neither.
- Copilot CLI: `copilot mcp --help` (1.0.103) shows the user file
  `~/.copilot/mcp-config.json`, `COPILOT_HOME`, and `copilot mcp add NAME -- COMMAND`, but not
  the file's JSON format, and the file can hold tokens, so it was not read. Copilot is listed
  `unavailable`. Not checked: whether Copilot CLI has a hook point.

## 4. Changes: the skill ships with the package

`pyproject.toml`: `[tool.hatch.build.targets.wheel.force-include]` puts
`skills/gitpro/SKILL.md` at `gitpro/skill/SKILL.md` in the wheel, and the sdist includes
`/skills/gitpro/SKILL.md`. `skills/gitpro/SKILL.md` stays the one source.
`skillcopy.bundled()` reads the packaged copy, else the repo file (a source checkout).

## 5. Changes: installed skill copies

`skillcopy.copies(home, bundled)` looks at `~/.claude/skills/gitpro/SKILL.md`,
`~/.copilot/skills/gitpro/SKILL.md` and `~/.agents/skills/gitpro/SKILL.md`, resolves links,
and lists each physical file once, as `current`, `differs` or `unreadable`. A location with
no file is not a copy.

- `setup -n --json`: `skills` is `[{"path": <physical file>, "state": …}]`. A `differs` copy is
  a pending row (group `gitpro skill`), so `-n` exits 1 and `ok` is false.
- `setup -y` (after the usual confirmation): for each differing copy, read it again (refuse if
  it changed since the check), write a backup `SKILL.md.bak.<stamp>` next to the file
  (`files._backup`), replace the content atomically with the same mode through the link
  (`files.atomic_write`; the link stays), and add `cp -p <backup> <file>` to the setup undo
  script. A failure is reported and makes `setup` exit 1.
- It never creates a copy. `uninstall` does not touch skill copies.

## 6. Changes: the skill text

`skills/gitpro/SKILL.md`: the Install and "Before committing" text becomes the section
"Before you commit, push or open a pull request" (eight rules: check `gitpro --version` early;
ask before installing; with nobody to ask, record "blocked: gitpro missing", keep doing work
that needs no commit, never write a done marker while a commit is blocked; run `why --json`
with the real author, committer and message, and read `problems` and `commit.problems`
because exit 0 alone is not a pass; a repo with no remote still uses `[default]`; a local
commit without gitpro only when the operator allowed it for that repo, the repo has no remote,
and git's author and committer are the named identity, logged in the run log, with no
environment variable; push and PR fail closed; never `--no-verify`, `GITPRO_SKIP` or
turning off signing). Also fixed: the deprecation table (`setup --write`, `scan --suggest`);
"every default tool is read-only"; per-setting origins in `--json`; Git's include order versus
gitpro's ambiguity rule and pins; signing and key not pinned only for ordinary `pin`;
`uninstall` keeps the config and keys; exit codes scoped to their command; `setup` without a
terminal needs `-y` or `-n` only when there is something to ask or change.

## 7. Docs

`CHANGELOG.md` (Unreleased), `docs/guide.md` (consent paragraph, `setup -n` JSON and agents
and skills, `why` commit object and exit codes, MCP row, skill paragraph), 00-existing known
issue 2 and the `--fix --json` line, 22-commands R4 text, DECISIONS "Consent in 0.4.x",
README (spec) JSON rule.

## 8. Tests

`tests/test_agent_setup_023.py` (22 tests) covers: the scan preview with and without `-y`;
`guard_would_block` against both modes, a good commit, and no denial record; the no-remote
messages; MCP and CLI `why` error parity; per-agent coverage (missing, installed, plugin,
Codex and Copilot exit 1 in `-n` and `-y`); the skill (reported once through links, current
is clean, `-y` updates through the link with backup, mode and undo line, never creates one,
a symlinked file stays a link); the build config; and the bundled skill equal to the source.
All use a temporary HOME. `tests/test_surface.py` adds the JSON fields.

## 9. Not changed, and not checked

- `why` does not check the push target, the account or the outgoing commits.
- The Copilot CLI MCP and hook formats are not implemented (section 3.5).
- No environment variable lets an agent skip the guard.
- `site/` (llms.txt, index.html) may still describe `scan --fix --json` as writing and the
  skill as plugin-only; it is off limits for this change.

## 10. Release

No version change here. The CHANGELOG entry is under Unreleased; the release process names
the version.
