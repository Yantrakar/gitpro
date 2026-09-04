# gitpro specification

This directory specifies gitpro: what exists today (`00-existing.md`) and the
work packages that extend it (`NN-<name>.md`). Several agents build the work
packages in parallel, tests first. Each package file is self-contained: an
agent that has read this file, `00-existing.md` and its package file has
everything it needs. Package 01 is the exception that everyone else reads
too, because it defines the shared helpers; where a package needs something
from 01 the package file repeats the signature it uses.

Status: revised after the independent review (`REVIEW.md`); the product
decisions in `DECISIONS.md` are final and applied throughout.
`REVIEW-RESPONSE.md` says what was done per finding.

## 1. Product overview

gitpro gives every git repo the right **name, email, SSH key and commit
signature**, chosen by the repo's **remote URL**. The user describes their
identities once in `~/.config/gitpro.toml`; `gitpro apply` turns that into
plain git and ssh config (`includeIf hasconfig:remote.*.url`, `url.insteadOf`,
ssh `Host` aliases). Git does the matching itself; nothing of gitpro runs
while git runs, unless the user installs the guard.

The extensions in this spec keep that model and add:

- **Guardrails** that catch the wrong identity before it reaches a commit, a
  push, or a forge (git hooks, AI-agent hooks, VS Code status bar, audit of
  history).
- **Discovery** of repos, lost work and identities from what is already on the
  machine (scan roots, editor recents, ssh config, gh/glab; shell history only
  on request).
- **Keys and signing** health and account verification.
- **AI coding assistants** as first-class users: a Claude Code hook, an MCP
  server, `gitpro env` for forge tokens, devcontainer integration, and an
  audit of agent-authored commits.
- **Team policy** enforced locally and in CI. Everything is free and MIT
  (DECISIONS Q1).
- **Daily usability**: `doctor`, `why`, a prompt segment, shell completions.

Audience: power users with several accounts (work + personal, several
GitHub/GitLab accounts, self-hosted forges), often working through AI agents,
containers and worktrees.

## 2. Principles (non-negotiable)

Every work package inherits these. A package that needs an exception must say
so under "Decisions" and justify it.

1. **Suggest, user picks.** gitpro never edits the user's TOML config. It
   prints or opens suggestions (ready-to-paste `[[identity]]` blocks, snippets)
   and the user pastes them. The one exception is `init`, which only creates a
   file that does not exist.
2. **Every write is previewed, confirmed, reversible.** Any change to the
   user's system (home directory files, repo `.git` directories, editor
   settings, hooks) is shown before it happens, confirmed (`-y` skips the
   question and is required without a terminal), and either backed up
   (under `~/.local/state/gitpro/backup-<timestamp>/`, never next to the
   edited file) or recorded in an undo script written **before** the
   change under `~/.local/state/gitpro/`. `gitpro uninstall` reverses
   everything `apply` wrote; every other writing command names its own undo.
3. **Tracked team files are print-only** (DECISIONS Q5). gitpro never writes
   a file a team commits (`devcontainer.json`, `.husky/*`, a repo's
   `.claude/settings.json`, a repo's `.mcp.json`) unless the user passes
   `--write`, which exists only for `devcontainer.json` (13) and the project
   `.mcp.json` (11), and then with backup and undo. Repo-mode guard hooks
   (06) refuse tracked hook files.
4. **Network is opt-in per feature.** Read-only forge calls go through the
   user's own `gh` / `glab` CLI, never through gitpro's own HTTP client.
   Nothing sends repo contents anywhere. A command that will contact a network
   service says so on its first line, says exactly what it sends, and needs an
   explicit flag or config key. Default is offline. `gitpro auth` is the one
   command whose name is the flag (its whole purpose is `ssh -T`).
5. **Secrets stay where they are.** gitpro never reads a private key's
   contents (only `ssh-keygen` does, when invoked on the user's behalf), never
   prints tokens, and `gitpro env` prints the *command* that fetches a token
   (`export GH_TOKEN="$(gh auth token -u X)"`), never the value. There is no
   flag to print values. Where gitpro itself needs a token for a child
   process (`forge.run`), the token goes into the child's environment only.
6. **Power-user output.** Verbose when something goes wrong, terse when it
   went right. One symbol per kind of line, everywhere (section 5). `--json`
   on every command that reports something, so the VS Code extension, the MCP
   server and agents read the same data people see.
7. **Minimal dependencies.** Python >= 3.9 (section 11 lists what that
   forbids). Required: `rich` (and `tomli` on < 3.11). Optional extra `tui`:
   `textual`. New packages use the standard library (the MCP server too). A
   new dependency needs a written justification in its package file.
8. **Tests run offline** with a temporary `HOME` (`tests/conftest.py::home`)
   and fake `gh`, `glab`, `ssh`, `ssh-keygen`, `gitpro` executables placed
   first on `PATH` (section 10). No test touches the real home directory or
   the network.
9. **git does the work.** Prefer generating git/ssh config over running code
   at git time. Where code must run at git time (hooks), it must be fast
   (target < 150 ms; the hot path in 01 §2.2 exists for this), **fail open on
   gitpro's own errors** (a hook blocks only on exit code 3, which gitpro
   returns only for a confirmed mismatch; any other failure prints `⚠ … not
   blocking` and the commit proceeds), and have a documented bypass
   (`GITPRO_SKIP=<any non-empty value>`, `--no-verify`).
10. **Agents are told to ask the user, never how to bypass.** The agent hook
    never emits an `allow` decision (DECISIONS Q6): warn mode adds a visible
    message and leaves the normal permission prompt in place; block mode
    denies with a reason that names the fix and says to ask the user. The
    skill and the MCP server carry the same rule.

## 3. Glossary

| term | meaning |
|---|---|
| **config** | The user's TOML, found via `-c PATH`, else `$GITPRO_CONFIG`, else `~/.config/gitpro.toml`. |
| **identity** | One `[[identity]]` table: `id`, `name`, `email`, `host`, `owners`, optional key/alias/signing. |
| **default** | `[default]`: what repos get when no identity matches. May have no name/email (then git refuses to commit). |
| **owner** | First path segment(s) of a remote URL after the host: a GitHub user/org, a GitLab group/subgroup. Exact case. |
| **expected identity** | What the config says a repo should use, computed by gitpro from the repo's **raw** remote URLs with git's own wildmatch (`resolve.expected`). |
| **effective identity** | What git actually resolves in a repo (`git config --show-scope --show-origin`), including repo-level overrides and pins. |
| **mismatch** | expected and effective differ in name, email, signing key or SSH key. |
| **override** | A `user.name` / `user.email` set in a repo's own config file; it beats gitpro. |
| **committer / author** | git's two identities on a commit. The guard judges the **committer** (the person at the keyboard); the author is flagged only when it is one of the user's own other emails (DECISIONS Q4, 06 §2). |
| **pin** | `.git/git-identities` + `include.path` in `.git/config`, written by `gitpro pin`, so containers see the identity. |
| **managed file** | A file gitpro generates whole (`# Generated by gitpro` on line 1, or on line 2 after a `#!` line). Never hand-edited. |
| **managed block** | The region between `# BEGIN gitpro` and `# END gitpro` inside a hand-written file (`~/.gitconfig`, `~/.ssh/config`). |
| **forge** | A git hosting service: GitHub, GitLab (cloud or self-hosted), others. |
| **forge account** | The `gh` / `glab` login an identity maps to (`gh_account`, `glab_account`). glab has one login per host (DECISIONS Q11). |
| **guard** | The git hooks gitpro installs on request (`gitpro guard install`), globally through `core.hooksPath` or per repo, that chain to the repo's own hooks. |
| **hook block** | Exit code 3 from `gitpro guard check` / a hook entry: the only exit code a hook script turns into a refused commit or push. |
| **agent** | An AI coding assistant (Claude Code, Cursor, Gemini CLI, Codex CLI, opencode) running git on the user's behalf. Only the Claude Code adapter is built and tested in this release (DECISIONS Q10). |
| **policy** | A team-level TOML (allowed email domains per owner, signing required) checked locally and in CI. |
| **undo script** | `~/.local/state/gitpro/<command>-undo-<timestamp>.sh`; `sh <path>` reverses one writing run. Written by `undo.write` (01 §5.3). |
| **state dir** | `$XDG_STATE_HOME/gitpro` or `~/.local/state/gitpro`. Undo scripts and caches live there. |
| **capability** | A string in `gitpro --version --json` (`which.json`, `why`, `guard`, `lost`, …) that tells the extension and the skill which features this gitpro has. One version number for CLI, extension and plugin (DECISIONS Q12). |
| **hot path** | `gitpro guard …`, `gitpro agent-check`, `gitpro prompt` dispatched by `hot.py` without importing `rich` or `cli.py`. |

## 4. Architecture

### 4.1 Modules (today)

| module | role | writes? |
|---|---|---|
| `cli.py` | argparse, one `cmd_*` per subcommand, `main()` with exit codes | no |
| `config.py` | find, load, validate the TOML into `Default` and `list[Identity]` | no |
| `plan.py` | desired state: `build()` returns `Target`s (path, text, mode); `teardown()` for uninstall | no |
| `render.py` | pure text rendering of gitconfig / ssh config / allowed_signers; marked blocks | no |
| `files.py` | diff desired vs disk into `Change`s; `apply()` backs up and writes atomically | **yes** |
| `keys.py` | SSH key inventory, `ssh-keygen` generation, `.pub` report | keys only |
| `lint.py` | advisory warnings | no |
| `scan.py` | find repos, inspect identities, suggestions, `fix()` with undo script | repo `.git/config` |
| `pin.py` | pin/unpin a repo | repo `.git` |
| `view.py` | rich output helpers and the scan report renderer | no |
| `tui.py` | optional textual UI for `scan --fix` | no |

### 4.2 Modules added by this spec

| module | package | role |
|---|---|---|
| `args.py` | 01 | stdlib-only shared argparse pieces: `common()`, `confirm()`, `CAPABILITIES`, `capability()`; `cli` re-exports them |
| `hot.py` | 01 | console-script entry; dispatches `guard`, `agent-check`, `prompt` without importing `rich`/`cli`; `CALLER_UMASK` |
| `resolve.py` | 01 | expected vs effective identity for a repo; the single source of truth for every guard, hook, status bar and MCP tool; also registers `why` |
| `jsonout.py` | 01 | the `--json` envelope and error path shared by all commands |
| `undo.py` | 01 | shared undo-script writer (replaces `scan.fix`'s private one) |
| `jsonfile.py` | 01 | read/edit JSON and JSONC settings files with preview, backup, undo (used by 10, 11, 13) |
| `forge.py` | 01 (minimal) + 05 | `Account`, `gh_env`, `run`, `repo_exists`, `announce`, `no_online_error` from 01; the `forge` command from 05 |
| `scanroots.py` | 02 | `scan -c/--roots`, root and exclude selection |
| `lost.py` | 03 | lost and hidden work per repo |
| `discover.py` | 04 | read local artifacts (editor recents, ssh config, includeIf, repos; history on request) into identity suggestions |
| `guard.py` | 06 | hook scripts, `guard check` entry point, status; `HOOKS` table per 01 §5.6 |
| `clone.py` | 07 | `gitpro clone` |
| `audit.py`, `trailers.py` | 08 | history audit, trailer policy, `commit-msg` entry |
| `health.py` | 09 | key health, `auth` test, `keys --upload` |
| `agents.py` | 10 | agent hook adapters (Claude Code built; Cursor/Gemini experimental) and installers |
| `mcp.py` | 11 | stdio MCP server (stdlib) |
| `envcmd.py` | 12 | `gitpro env` |
| `devcontainer.py` | 13 | devcontainer.json snippet / `--write` |
| `policy.py` | 14 | policy file loading, local and CI verification, `team` |
| `doctor.py`, `prompt.py`, `completion.py` | 16 | diagnostics, prompt segment, completions |

**Dispatch** (01 §2.1, repeated here because every package relies on it):
`cli.EXTENSIONS` is a list of module names written in full by 01. Each
module exposes `register(sub, common, confirm)` (adds its subparsers and
`set_defaults(func=…)`) and optionally `extend(subs)` (adds flags to an
existing command through `cli.extend_command`). `build_parser()` imports
each listed module and skips a name that does not exist yet
(`ModuleNotFoundError` for that exact name only). `main()` runs
`args.func(args)`. Consequences:

- **No package after 01 edits `cli.py`.** The only `cli.py` edit after 01 is
  17's test that every listed module imports.
- A package that adds flags to an existing command owns those flags: `scan`
  → 02, `init --discover` → 04, `keys --health/--upload/--online/--identity/
  --json` → 09, `check --policy/--range/--repo` → 14.
- A module in `EXTENSIONS` imports at module level only the standard library
  and `gitpro.config`, `gitpro.jsonout`, `gitpro.undo`, `gitpro.files`
  (01 §2.1, enforced by 01's import-time test). `rich`, `view`, `resolve`,
  `scan` are imported inside functions.
- Hot modules (`guard`, `agents`, `prompt`) have `main(argv) -> int` and
  never import `cli`, `view` or `rich` (01 §2.2).
- Hook entries (`pre-commit`, `pre-push`, `commit-msg`) are a fixed table
  `guard.HOOKS` defined in 01 §5.6 and created by 06; 08 and 14 implement the
  functions it names (`gitpro.trailers:hook_message`,
  `gitpro.policy:hook_commit` …) and never edit `guard.py`. A missing module
  is skipped.

**VS Code** (15 §4): 15 owns `vscode/package.json` and `extension.ts`, and
creates `vscode/src/features.ts` (one `register*(context, cli)` call per
feature) plus a **stub file per later feature** (`roots.ts`, `lost.ts`,
`clone.ts`, `devcontainer.ts`, each exporting a no-op `register*`). 15
declares every command, view, menu and setting those packages need in
`package.json`, as listed in 15 §4. A later package **replaces its own stub
file** and touches nothing else under `vscode/`. A stub's command, when
run, shows `gitpro: <feature> is not in this build`.

### 4.3 Data flow

```
TOML config ──config.load_all──▶ Config(default, identities, scan, guard, clone, forge, agents, policy)
      │                              │
      │                         plan.build ──files.diff──▶ Changes ──files.apply──▶ ~/.gitconfig*, ~/.ssh/*
      │                              │
      └──▶ resolve.expected(repo) ◀── git remotes (raw URLs, git wildmatch)
                 │
                 ├── resolve.effective(repo) ◀── git config --show-scope --show-origin
                 │
                 ▼
          resolve.compare ──▶ Report(problems) ──▶ which/why, guard hooks, agent hook,
                                                   VS Code status bar, MCP, audit, policy, env, prompt
```

Everything that judges "is this repo right?" goes through `resolve`.

### 4.4 Processes gitpro spawns

`git`, `ssh`, `ssh-keygen`, `ssh-add`, `gh`, `glab`, `sh` (undo scripts),
`claude` (only `claude mcp add`, 11). `$EDITOR` never; gitpro prints paths
instead. Always `subprocess.run` with an argv list, never a shell string.
Timeouts: 10 s for local git, 30 s for `ssh -T` and forge calls, none for
`git clone`. Children that must not read the terminal get `stdin=DEVNULL`.

## 5. Output conventions

Symbols, one per kind of line, in every command, the extension log and the
skill:

| symbol | meaning | stream |
|---|---|---|
| `✓` | done, or all good | stdout |
| `⚠` | warning, advisory, skipped item | stderr (CLI), log warn (extension) |
| `✗` | error, blocked, or nothing done | stderr |
| `→` | next step for the user, or a change "from → to" | stdout |
| `↩` | how to undo | stdout |
| `·` | separator inside a line, secondary info | stdout |

Rules:

- Paths under the real home are printed with `~/` (`plan.Env.tilde`,
  `undo.tilde`).
- Commands the user should run are printed verbatim, copy-pasteable, shell
  quoted (`cli._sh`, `shlex.quote`, keeping a leading `~`).
- A dry run ends with the exact command that performs the change.
- A writing run ends with `✓ <what>` then `↩ undo: sh <script>` (or the backup
  path).
- `-v` adds detail; never changes what is counted.
- git config keys are printed in git's canonical lowercase form
  (`core.sshcommand`, `commit.gpgsign`) in JSON; text output may use the
  camelCase the user typed.
- Exit codes: `0` ok; `1` "something to do / mismatch found / refused by
  user" (check, audit, policy, declined confirmation, `lost` found
  something); `2` configuration, usage or runtime error (including "this
  command needs `--online`", see 01 §5.5 `no_online_error`); `3` **hook
  block** (only from `guard check` and hook entries: a confirmed mismatch
  with `on_mismatch = "block"`; hook scripts treat every other non-zero exit
  as "not blocking"); `130` interrupted.

## 6. The `--json` contract

Shared by the CLI, the VS Code extension, the MCP server and the skill.

- `--json` prints exactly one JSON object to stdout, `json.dumps(obj,
  indent=2, ensure_ascii=False)`, nothing else on stdout. Human text goes
  nowhere; warnings may still go to stderr.
- Every new object has `"ok": true|false` and `"command": "<subcommand>"`
  (`jsonout.emit`). `scan --json` (existing) gains these two keys additively;
  its other keys do not change.
- On a `ConfigError` or runtime error with `--json`, stdout gets
  `{"ok": false, "command": "...", "error": "<message>"}` and the exit code is
  the same as without `--json`. `main()` implements this once (01 §2.1,
  `_fail`). The non-POSIX refusal happens before parsing and is text only;
  the root refusal is after parsing and is JSON with `--json`.
- Paths are absolute strings (no `~`), except where a key is documented as
  "display" text.
- Keys are `snake_case`; git config keys are lowercase. Lists are
  stable-ordered (sorted as documented per command). Booleans are booleans,
  never `"true"`.
- A command that asks a question has no question with `--json`: it either
  requires `-y` (writing commands) or reports (reading commands). Existing
  exception, kept for compatibility: `scan --fix --json` writes without `-y`
  (see `00-existing.md`, known issues). New writing commands do **not** copy
  this: `--json` without `-y` and without a terminal is an error.
- The `warnings` key, when present, is a list of strings identical to the `⚠`
  lines.
- **Version and capabilities**: `gitpro --version --json` returns
  `{"ok": true, "command": "version", "version": "0.2.0", "python": "…",
  "capabilities": ["which.json", "why", …]}`. Each package registers the
  capability names it adds (`cli.capability(...)` inside `register`; the
  list per package is in its file). The extension and the skill detect
  features through this list, never through a minimum version (DECISIONS
  Q12). One version string for CLI (`gitpro/__init__.py`),
  `vscode/package.json`, `.claude-plugin/plugin.json` and
  `marketplace.json`; 01 sets `0.2.0.dev0`, 17 sets `0.2.0`.
- Each package documents its schema with an example.

The MCP server (11) returns exactly the same objects as the corresponding
`--json` commands.

## 7. Configuration reference (full TOML)

Keys marked **new** are added by the package in brackets. `config.py`
validation for all new keys is done in package 01 (`config.load_all` →
`Config`) so that other packages only read dataclass fields (01 §3.1 lists
them). Unknown keys are errors, as today.

```toml
# ---------------------------------------------------------------- existing
[default]                       # all keys optional
name = "Jane Doe"
email = "jane@personal.dev"
ssh_key = "~/.ssh/id_ed25519"
ssh_hosts = ["github.com"]      # ssh always uses ssh_key for these hosts
sign = false                    # SSH-sign commits and tags
gh_account = "janedoe"          # new [01]: gh login this identity maps to (host github.com)
glab_account = "janedoe"        # new [01]: glab login (host from the identity; for [default]: gitlab.com)
clone_dir = "~/src"             # new [01]: where `gitpro clone` puts repos of this identity
[default.git.<section>]         # extra git settings, as today
[default.trailers]              # new [01], used by 08/06
co_authored_by = "allow"        # "allow" | "forbid" | "require"
forbid_emails = []              # globs; a Co-Authored-By / author email matching one is reported/blocked

[[identity]]
id = "work-github"
name = "Jane Doe"
email = "jane.doe@employer.com"
host = "github.com"
owners = ["janedoe-employer", "employer-org"]
ssh_key = "~/.ssh/id_ed25519_work"
ssh_alias = "github-work"
rewrite = true
rewrite_https = false
ssh_user = "git"
ssh_port = 22
sign = true                     # overrides [default].sign
file = "~/.gitconfig-work-github"
gh_account = "janedoe-employer" # new [01]
glab_account = "jane.doe"       # new [01]
clone_dir = "~/work"            # new [01]
policy = "~/.config/gitpro/employer-policy.toml"   # new [01], used by 14: a policy file this identity must satisfy
[identity.git.<section>]
[identity.trailers]             # new [01]: same keys as [default.trailers]; unset keys inherit

# ---------------------------------------------------------------- new tables
[scan]                          # new [01], used by 02/03/04
roots = ["~", "/srv/git", "/mnt/d/work"]   # default ["~"]; each must start with ~/ or /
exclude = ["~/.cache", "**/node_modules", "~/go/pkg/mod"]  # default []; absolute-path globs after ~ expansion (02 §2); hidden dirs and node_modules are always skipped
stale_days = 180                # 03: a repo with no commit for this long is "stale"

[guard]                         # new [01], used by 06
hooks = false                   # true: plan/apply install the global chained hooks
on_mismatch = "block"           # "block" | "warn"
chain_to = ""                   # a previous global core.hooksPath to run after gitpro's hooks (must not be gitpro's own dir)

[clone]                         # new [01], used by 07
dir = "~/src"                   # fallback when the matching identity has no clone_dir
layout = "{host}/{owner}/{repo}"   # placeholders: {host} {owner} {repo} {id}

[forge]                         # new [01], used by 03/04/05/09
online = false                  # true: forge commands may run without --online
similar = false                 # true: `forge similar` may run without --send-repo-names

[agents]                        # new [01], used by 10/08
hook = "block"                  # "block" | "warn" | "off": what the agent hook does on a mismatch
bot_emails = ["*[bot]@users.noreply.github.com", "noreply@anthropic.com"]  # recognised as agent/bot authors in audit

[policy]                        # new [01], used by 14
files = []                      # policy files that apply to every repo (identity.policy adds per-identity ones)
```

Validation rules (package 01 §3.2): paths use `PATH_RE` as today; `*_account`
match `ID_RE`; `layout` may contain only the listed placeholders and
`[A-Za-z0-9._/-]`; enum keys are checked against their lists; `stale_days` is
a positive int; globs in `exclude`/`forbid_emails`/`bot_emails` are strings
without control characters; `chain_to` must not resolve to
`~/.config/gitpro/hooks`.

## 8. Global safety rules

Apply to every package; tests must cover the ones that can be tested.

1. **Refuse as root** and on non-POSIX, as today (`main()`, and `hot.main`).
2. **Config file permissions**: owned by the user, not group/world-writable.
   Same check for policy files and for any file whose contents become a
   command (hook scripts gitpro runs, `chain_to` hooks).
3. **No shell strings.** Every subprocess call is an argv list. Values from
   the config or from git output never reach a shell unquoted; when a command
   is printed for the user, or baked into a generated script, it is quoted
   with `shlex.quote` (keeping a leading `~` only in display text).
4. **Write order**: undo script or backup first, then the change, then the
   `✓` line. An interrupted run must be undoable. `undo.write` creates the
   script with `O_EXCL`, mode 0600 under a 0700 state dir, and no `set -e`.
5. **Atomic writes** via `files.atomic_write(path, text, mode)` (public since
   01; temp file in the same dir, fsync, rename). Mode `0600` for anything
   under `~/.ssh` or holding identity data; `0700` for executable hooks.
6. **Only touch what we wrote.** Generated files are recognised by their
   first line, or second line after a `#!` line (`render.is_generated`);
   blocks by markers. Anything else is left alone or refused with a message
   naming the file. Settings files (`jsonfile.edit_json`) are re-read and
   compared just before the replace; a concurrent change aborts the write.
7. **Symlinks**: never replace a symlink; write through it (`files.diff`
   resolves) or refuse.
8. **Repo-local writes** (`scan --fix`, `pin`, `guard install --repo`,
   `devcontainer --write`, `mcp install --scope project --write`) touch only
   the named repos and only the named keys, and never a tracked file without
   `--write` (principle 3).
9. **Network**: the first line of any command that will go online names the
   service, the CLI used (`gh`/`glab`), the account, and what is sent
   (`forge.announce`). A command that needs the network and lacks `--online`
   exits 2 through `forge.no_online_error` with the same sentence.
10. **Secrets**: never read `~/.config/gh/hosts.yml` tokens, `~/.ssh/id_*`
    (private) contents, `.netrc`, `.git-credentials`. Tokens obtained for a
    child (`forge.gh_env`) exist only in that child's environment. Fake
    executables in tests log `env:GH_TOKEN=set|unset` and tests assert that
    a token never appears on argv, stdout, stderr or JSON.
11. **Hooks fail open on gitpro errors**: a hook script blocks only on exit
    3; any other non-zero exit prints `⚠ gitpro guard: exited <rc>; not
    blocking` and continues. `GITPRO_SKIP` set to any non-empty value skips
    gitpro's check (the repo's own hooks still run).
12. **Agents** (skill, MCP, hooks) never get a write tool that is not already
    behind `-y` in the CLI. MCP exposes read-only tools only. The agent hook
    never emits `allow` and never teaches a bypass.
13. **Shell history** is read only with `--history` (04), and its lines are
    never printed or stored; only clone URLs with user-info and query strings
    removed.

## 9. Work packages, waves and file ownership

Order (DECISIONS Q9) and real dependencies give five waves. Within a wave
no two packages edit the same file. "May edit" is exhaustive: a package
that needs a change elsewhere writes it into its spec for the owning
package instead.

| wave | packages | why this wave |
|---|---|---|
| 1 | 01 | everything shared: dispatch, schema, `resolve`, helpers, fixtures, version |
| 2 | 06, 10, 12, 15, 16 | need only 01; 15 lays down the VS Code skeleton (stubs) others replace later |
| 3 | 02, 03, 07, 08, 09, 11 | 02/03/07 replace VS Code stubs from 15; 08 uses the `HOOKS` table from 06 |
| 4 | 04, 05, 13, 14 | 04 uses 02's `scan.find(max_depth)`; 05 extends 01's `forge.py` after 03/09 stopped needing changes to it; 13 replaces a stub; 14 needs 06 and 08 |
| 5 | 17 | documents all; runs the whole suite with **no skips** |

Final no-skip run (V-032): while packages are built in parallel, tests
that need a module from another package are `skipif` (section 9, "soft"
dependency), so a green run can hide a cross-package test that never ran.
17's last task, and a CI job added by 17 (`pytest -q -rs` with
`--strict-markers` and a check that the `-rs` summary lists **zero**
skips other than the documented platform ones), runs every test with all
17 modules present. A skip at that point is a bug in the package that
owns the test.

| # | file | title | creates | may edit (existing files) | depends on |
|---|---|---|---|---|---|
| 01 | `01-config-resolve-why.md` | Config schema, dispatch, `resolve`, `--json`, `which --json`, `why`, shared helpers, fixtures | `resolve.py`, `jsonout.py`, `undo.py`, `jsonfile.py`, `hot.py`, `forge.py` (minimal), `tests/test_config_new.py`, `test_resolve.py`, `test_cli_core.py`, `test_helpers.py` | `cli.py`, `config.py`, `plan.py`, `render.py`, `files.py`, `scan.py` (undo + ok/command only), `__main__.py`, `example.toml`, `pyproject.toml`, `__init__.py`, `vscode/package.json` (version only), `.claude-plugin/plugin.json` + `marketplace.json` (version only), `tests/conftest.py` | — |
| 02 | `02-scan-roots.md` | Saved scan roots/excludes; VS Code folder picker and multi-root | `scanroots.py`, `tests/test_scan_roots.py`, `vscode/src/test/roots.test.ts` | `scan.py` (`find`, `effective_roots`, `derive_id`), `view.py` (scan header only), `vscode/src/roots.ts` (replaces 15's stub) | 01, 15 |
| 03 | `03-lost-work.md` | `gitpro lost` | `lost.py`, `tests/test_lost.py`, `vscode/src/test/lost.test.ts` | `vscode/src/lost.ts` (replaces stub) | 01, 15 (02 soft: imports `scanroots` if present) |
| 04 | `04-discover.md` | `gitpro discover`, `init --discover` | `discover.py`, `tests/test_discover.py` | — | 01, 02 |
| 05 | `05-forge.md` | `gitpro forge` (read-only via gh/glab; opt-in similar search) | `tests/test_forge.py` | `forge.py` (only 05 edits it after 01) | 01, 02 |
| 06 | `06-guard.md` | Chained git hooks: pre-commit / pre-push identity guard | `guard.py`, `tests/test_guard.py` | `render.py` (hook scripts, hooksPath section), `plan.py` (hook targets when `[guard] hooks`) | 01 |
| 07 | `07-clone.md` | `gitpro clone <url>` | `clone.py`, `tests/test_clone.py` | `vscode/src/clone.ts` (replaces stub) | 01, 15 |
| 08 | `08-audit.md` | History audit, trailer policy, `commit-msg` entry | `audit.py`, `trailers.py`, `tests/test_audit.py`, `tests/test_trailers.py` | — | 01, 06 (table only) |
| 09 | `09-keys-health.md` | `keys --health`, `gitpro auth`, `keys --upload` | `health.py`, `tests/test_health.py` | — | 01 |
| 10 | `10-agent-hooks.md` | Claude Code PreToolUse hook and installer | `agents.py`, `hooks/hooks.json` (repo root), `tests/test_agents.py` | `.claude-plugin/plugin.json` (`hooks` key only) | 01 |
| 11 | `11-mcp.md` | `gitpro mcp` stdio server, read-only tools | `mcp.py`, `.mcp.json` (repo root), `tests/test_mcp.py` | `.claude-plugin/plugin.json` (`mcpServers` key only) | 01 (03, 08, 12 soft) |
| 12 | `12-env.md` | `gitpro env` | `envcmd.py`, `tests/test_envcmd.py` | — | 01 |
| 13 | `13-devcontainer.md` | devcontainer snippet / `--write` | `devcontainer.py`, `tests/test_devcontainer.py`, `vscode/src/test/devcontainer.test.ts` | `vscode/src/devcontainer.ts` (replaces stub) | 01, 15 |
| 14 | `14-policy.md` | Policy file, local + CI check, Action, `team` | `policy.py`, `action.yml`, `tests/test_policy.py` | — | 01, 06, 08 |
| 15 | `15-vscode-identity-bar.md` | Status bar with identity + key, quick actions, VS Code skeleton | `vscode/src/identityBar.ts`, `features.ts`, `capabilities.ts`, stubs `roots.ts`, `lost.ts`, `clone.ts`, `devcontainer.ts`, `vscode/src/test/identityBar.test.ts` | `vscode/package.json`, `vscode/src/extension.ts`, `vscode/src/test/integration/*` | 01 |
| 16 | `16-doctor-prompt-completion.md` | `doctor`, `prompt`, `completion` | `doctor.py`, `prompt.py`, `completion.py`, `tests/test_doctor.py`, `test_prompt.py`, `test_completion.py` | — | 01 (06, 09, 10, 11, 14 soft) |
| 17 | `17-skill-and-docs.md` | Skill, READMEs, plugin manifest, final version | `tests/test_docs.py` | `README.md`, `vscode/README.md`, `skills/gitpro/SKILL.md`, `.claude-plugin/plugin.json` (description), version to `0.2.0` in the four files, `cli.py` (no code; the import test lives in `test_docs.py`) | all |

"Soft" dependency: the package imports the other module inside a `try:`
and degrades (a `⚠ … not installed` line or an omitted section) when it is
missing; its tests that need the other module are marked `skipif`.

Merge rules:

- New code goes in new modules. `cli.py`, `config.py`, `conftest.py` are
  edited by 01 only. `forge.py` by 01 then 05 only. `guard.py` by 06 only.
- One test file per package (`tests/test_<module>.py`,
  `vscode/src/test/<name>.test.ts`); other packages' fixtures come from
  `conftest.py` (01) or are local to the test file.
- `README.md`, `vscode/README.md` and `SKILL.md` are edited by 17 only; each
  package writes its user documentation into its own spec file's "Docs
  text" section, which 17 copies.
- `vscode/package.json` and `extension.ts` are edited by 15 only; the
  contributions other packages need are listed in 15 §4 and the owning
  package replaces its stub file (section 4.2).
- `.claude-plugin/plugin.json`: 01 (version) and 17 (description, final
  version) only. 10 and 11 add `hooks/hooks.json` and `.mcp.json` at the
  plugin root and do not touch the manifest (V-027).

## 10. Shared test fixtures (defined in package 01 §7, used by all)

`tests/conftest.py` after 01, signatures every package may rely on:

```python
home                     # temp HOME; GITPRO_CONFIG, GIT_CONFIG_GLOBAL, XDG_* unset; GIT_CONFIG_NOSYSTEM=1; stdin not a tty
write_config(home, text, mode=0o600) -> Path
run(*argv) -> int        # cli.main; pair with capsys
git(repo, *args) -> str

fake_bin                 # dir first on PATH. fake_bin.add("gh", body): a Python script (shebang sys.executable)
                         # that logs shlex.join(argv) + " env:GH_TOKEN=set|unset env:GH_ENTERPRISE_TOKEN=set|unset
                         # env:GITLAB_TOKEN=set|unset stdin=<bytes>" to fake_bin.log("gh"), then runs `body`
                         # (Python; `argv`, `stdin`, `env` in scope; sys.exit(rc)). fake_bin.calls("gh") -> list[list[str]]
cfg(text=None, *, apply=True) -> Path
                         # default config: identities work-github (github.com, owners corp-org, ~/.ssh/id_work,
                         # gh_account me-work) and personal (github.com, owners me, ~/.ssh/id_personal), [default] with
                         # name/email; creates empty key + .pub files; runs apply -y unless apply=False
repo_with_remote(tmp_path, name, url, *, commits=0, author=("Work Me", "me@corp.test"), committer=None) -> Path
bare_remote(name) -> Path                        # bare repo to push to; use its path as the url
commit(repo, message, *, author=None, committer=None, trailers=(), date=None) -> sha
set_git_hooks_sandbox(monkeypatch, home)        # GIT_CONFIG_GLOBAL unset so HOME/.gitconfig applies
```

Conventions: a fake `gitpro` for hook tests is a Python script that runs
`gitpro.hot.main(sys.argv[1:])` (06 §10 shows it). Fakes never contain a
shell string. Tests that need a "pushed" state push to `bare_remote`; no
network. Every subprocess in a test has a timeout.

## 11. Python 3.9 rules (all packages; 01 §6)

CI tests 3.9. New modules start with `from __future__ import annotations`.
Forbidden: `match`, `zip(strict=)`, `dataclass(slots=, kw_only=)`,
`datetime.UTC`, `X | Y` at runtime (`isinstance`, aliases), `tomllib`
without the `tomli` fallback (`config._load_toml` wraps it), `str.removeprefix`
is fine (3.9). 01 ships an AST test that scans `src/gitpro` for the
forbidden forms.

## 12. Install line and plugin layout

- Install: `pipx install git+https://github.com/sandeepshelke/gitpro.git`
  (no PyPI name is assumed, DECISIONS Q7). The GitHub Action installs from
  its own checkout.
- The repo root is the Claude Code plugin root: `.claude-plugin/plugin.json`
  (manifest), `hooks/hooks.json` (10), `.mcp.json` (11), `skills/gitpro/`.

## 13. Open questions (not blocking; each package names its fallback)

| question | where | fallback used by the spec |
|---|---|---|
| Cursor `beforeShellExecution` and Gemini `BeforeTool` field names | 10 §2 | adapters marked experimental, not installed unless named |
| Whether `plugin.json` needs `hooks`/`mcpServers` keys or the files are picked up by path | 10, 11, 17 | files at the plugin root only, no manifest keys (V-027); 17 verifies against the plugin docs at release and adds keys then if needed |
| Whether Claude Code tolerates an extra marker key in a hook entry | 10 §2 | entry recognised by a fixed comment suffix in `command` instead |
| `gh ssh-key list` output for signing keys | 09 §2 | parse `--json` when accepted, else the text table; unknown type → `?` |
| `glab auth token` availability | 12 §2 | probed at runtime (`--help` exit code) |
| Newest MCP protocol version at build time | 11 §2 | one constant list; newest supported echoed |
