# 00 — The existing toolset (gitpro 0.1.0)

This file describes gitpro exactly as it behaves at commit `487953a`. It is
the baseline every work package builds on. Behaviour here is **not** to be
changed by a package unless that package's file says so explicitly.

Sources: `src/gitpro/*.py`, `tests/`, `vscode/src/*.ts`,
`skills/gitpro/SKILL.md`. Outputs below were captured by running the CLI
against a scratch `HOME`.

## 1. Invocation, config lookup, exit codes

- Entry point `gitpro = gitpro.cli:main`; `python -m gitpro` works too.
- `main()` refuses non-POSIX (`✗ gitpro supports Linux and macOS only`, exit
  2) and root (`✗ run as the user who owns these identities, not root`, exit
  2). It sets `umask 077`, so every file gitpro creates without an explicit
  mode is `0600`/`0700`.
- Exceptions: `ConfigError` → `✗ <message>` on stderr, exit 2.
  `CalledProcessError` → `✗ <cmd> failed (<rc>)`, exit 2. `OSError`,
  `UnicodeError` → `✗ <message>`, exit 2. `KeyboardInterrupt`/`EOFError` → a
  newline on stderr, exit 130.
- No subcommand: prints the full help, then `command options (COMMAND -h for
  details):` with one usage line per subcommand, then a `status:` line:
  `status: <path> (<how>), <n> identities, up to date` or
  `..., <n> pending change(s) -- review with 'plan', write with 'apply'` or
  `status: <ConfigError message>`. Exit 0 in all cases.
- Config lookup (`config.find`): `-c PATH` → `"--config"`; `$GITPRO_CONFIG` →
  `"$GITPRO_CONFIG"`; `~/.config/gitpro.toml` if it is a file → `"user
  config"`; else `ConfigError("no config found (looked at -c, $GITPRO_CONFIG,
  ~/.config/gitpro.toml); create one with: gitpro init")`.
- Only `plan`, `apply`, `check`, `keys` take `-c/--config` and the hidden
  `--home PATH` (default `Path.home()`). `uninstall` takes `--home` only.
  `init`, `which`, `scan`, `pin`, `unpin` take neither.
- `_load()` first checks `git --version` >= 2.36
  (`git X.Y is too old; includeIf hasconfig needs 2.36+ (Ubuntu:
  ppa:git-core/ppa, macOS: brew install git)`), or `git not found`. Only the
  `_load` commands check; `which`, `scan`, `pin` do not.
- `-y/--yes` exists on `apply`, `scan`, `uninstall`. Without `-y` and without
  a TTY on stdin: `ConfigError("no terminal to confirm on; re-run with
  --yes")`. With a TTY: `rich.prompt.Confirm.ask(question, default=False)`.
  Declining prints `✗ aborted, nothing written` and returns 1.

## 2. Config file (`config.py`)

### 2.1 Loading and file checks

- `load(path)`: file must exist (`<path>: not found`), be owned by the
  current uid and have no group/world write bit (`<path>: must be owned by
  you and not group/world-writable (chmod 600 <path>)`). TOML parse errors are
  `<path>: <tomllib message>`.
- Top-level keys allowed: `default`, `identity` only. `[default]` is required
  (even empty). `identity` must be an array of tables (`write identities as
  [[identity]]`).
- Unknown keys anywhere: `<where>: unknown key(s) a, b (allowed: ...)`.
  Missing required: `<where>: missing name, email, host`.
- Control characters (anything in `\x00-\x08\x0a-\x1f\x7f`) are rejected in
  every string, key or nested value.

### 2.2 `[default]` (`Default` dataclass)

| key | type | rule |
|---|---|---|
| `name`, `email` | str | both optional; if either is given both are validated (`bad or missing name` — no `<`/`>`; `EMAIL_RE`); email domain must not be a placeholder (`example`, `example.com/net/org`) |
| `ssh_key` | str | `PATH_RE`: starts with `~/` or `/`, chars `[A-Za-z0-9._/@+-]`, no `..` segment |
| `ssh_hosts` | list[str] | each `HOST_RE`, not placeholder; needs `ssh_key` |
| `sign` | bool | needs `ssh_key` and `email` |
| `git` | table | extra git settings; rendered after the generated ones (`render.merge`) |

### 2.3 `[[identity]]` (`Identity` dataclass)

| key | type | rule |
|---|---|---|
| `id` | str, required | `ID_RE` `^[A-Za-z0-9][A-Za-z0-9._-]*$`; unique |
| `name`, `email`, `host` | str, required | as default; `host` `HOST_RE`, not placeholder |
| `owners` | list[str] | each `OWNER_RE` (`*` allowed, `/` for subgroups) |
| `ssh_key` | str | `PATH_RE` |
| `ssh_alias` | str | `HOST_RE`, != host, needs `ssh_key` |
| `ssh_user` | str | default `git`, `ID_RE` |
| `ssh_port` | int | 1–65535 |
| `rewrite` | bool | default true |
| `rewrite_https` | bool | default false |
| `sign` | bool or absent | absent inherits `[default].sign`; signing needs an `ssh_key` here or in default |
| `file` | str | default `~/.gitconfig-<id>`; `PATH_RE`; must resolve inside home (checked in `plan.build`); unique |
| `git` | table | extra git settings |

Cross checks: duplicate ids, duplicate files, duplicate ssh Host names
(`ssh_hosts` + aliases).

## 3. Generated files (`plan.py`, `render.py`)

For the example config in `README.md` with `sign = true` on default,
`apply` writes:

| path | kind | content |
|---|---|---|
| `~/.gitconfig-identities` | managed, mode kept/0644 | header; `# managed: <tilde path>` lines (sorted manifest of the other generated files); `[user] useConfigOnly = true` plus default name/email; signing block for default (`gpg.format=ssh`, `user.signingkey=<abs .pub>`, `commit.gpgsign`, `tag.gpgsign`, `gpg.ssh.allowedSignersFile=<abs>`); `default.git`; then per identity a comment `# <id>: <host> <owners or (all owners)>`, one `[includeIf "hasconfig:remote.*.url:<pattern>"] path = "<file>"` per `render.url_patterns(i)`, and `[url "git@<alias>:<owner>/"] insteadOf = ...` blocks when `ssh_alias` and `rewrite` |
| `~/.gitconfig-<id>` | managed | `[user] name, email`; `[core] sshCommand = "ssh -i <key> -o IdentitiesOnly=yes"` when `ssh_key`; signing block when `signs(i,d)`; otherwise, when default signs, `commit.gpgsign=false` and `tag.gpgsign=false`; `identity.git` |
| `~/.config/git/allowed_signers` | managed | one `<email> namespaces="git" <type> <key>` per signing (email, key) pair whose `.pub` exists; only written when anyone signs |
| `~/.gitconfig` | block, **last** | `# BEGIN gitpro -- keep last` / `[include] path = ~/.gitconfig-identities` / `# END gitpro` |
| `~/.ssh/config.d/git-identities` | managed, 0600 strict | `Host <h>` blocks for `ssh_hosts` (IdentityFile, IdentitiesOnly yes); `Host <alias>` blocks with HostName, User, Port |
| `~/.ssh/config` | block, **first**, 0600 strict | `# BEGIN gitpro -- keep first` / `Match all` / `Include config.d/git-identities` / `# END gitpro` |

`url_patterns(i)` for owners `o` (or `""` when none), hosts `host` and
`alias`: `git@host:o/**`, `ssh://git@host/o/**`, (`ssh://git@host:port/o/**`
when `ssh_port`), `https://host/o/**`. Later `includeIf` blocks win, so the
later identity in the file wins. The `insteadOf` rewrite is skipped for
owners containing `*` (lint warns).

Marked blocks (`render.with_block`): exactly one block per file; re-running
is a no-op; an unpaired marker is refused (`<where>: unpaired '# BEGIN
gitpro' / '# END gitpro' marker; fix it by hand`). Old tool names
(`git-identities`, `git_identities.py`) are recognised and migrated.

Stale generated files: files listed in the previous manifest but no longer
produced are removed if they still start with the generated marker.

## 4. Commands

### 4.1 `init [PATH]`

Creates `PATH` (default `~/.config/gitpro.toml`) from `example.toml`,
`O_EXCL`, mode 0600, parent `0700`. Existing: `✓ <path> already exists, left
unchanged`, exit 0. Created: `✓ created <path>` then `→ replace the
placeholders in it, then run: gitpro plan[ -c <path>]`. Never overwrites.

### 4.2 `plan`

Prints `config: <path> (<how>)`, then for each change `  <action:7> <path>`
(`create`, `update`, `chmod` with ` -> <mode>`, `remove`) followed by a
unified diff (`a<path>` / `b<path>`). `up to date: nothing to change` when
none. Then lint warnings as `⚠` lines on stderr (note: stderr may appear
before stdout when piped). Exit 0 always (errors exit 2).

### 4.3 `apply [-y]`

Same header and change list without diffs; asks `apply N change(s)? ('gitpro
plan' shows the diffs)`; writes; `✓ applied N change(s)`; lint warnings; if
any key is configured: `→ SSH keys: 'gitpro keys' lists the public keys to
register with each account`. A hand-written file is backed up once as
`<file>.bak.<YYYYmmdd-HHMMSS>[.n]` (0600) only when its non-gitpro lines
would change (`files._needs_backup`): `  backup  <path>` is printed. Writes
are atomic (temp + rename, fsync). Exit 1 if declined.

### 4.4 `check`

Prints the change list (no diffs); exit 1 if any change, else 0 with `up to
date: nothing to change`.

### 4.5 `keys [--generate] [--no-passphrase]`

`keys.inventory`: one `Key` per distinct path with `uses` (`"github.com
(default)"`, `"github.com: corp-org [work-github]"`, `"signing for [id]"`)
and `sign`. Output:

```
SSH keys -- register each public key with the account(s) listed:
  ~/.ssh/id_personal  (auth + signing)  ->  github.com (default)
    ssh-ed25519 AAAAPERSONAL me@laptop
  ~/.ssh/id_work  (auth + signing)  ->  github.com: corp-org [work-github]
    (missing -- run: gitpro keys --generate)
test: ssh -T git@github.com
test: ssh -T git@github-work
first connect: compare the host key fingerprint with the provider's published one
```

`--generate` runs `ssh-keygen -t ed25519 -a 100 -f <path> -C "<email>
<hostname>"` for each missing key (interactive passphrase prompt unless
`--no-passphrase` adds `-N ""`), then `keys created -- run 'gitpro apply' so
the signing config picks them up`. Existing keys are never touched. Without
any key configured: `no SSH keys configured (set ssh_key in [default] or an
identity)`.

### 4.6 `which [DIR]`

Runs plain `git -C DIR`; no config needed. Output:

```
remotes:
  remote.origin.url git@github.com:acme/app.git
  user.name        Personal Me   [file:/home/me/.gitconfig-identities]
  user.email       me@personal.test   [file:/home/me/.gitconfig-identities]
  user.signingkey  /home/me/.ssh/id_personal.pub   [file:/home/me/.gitconfig-identities]
  commit.gpgsign   true   [file:/home/me/.gitconfig-identities]
  core.sshCommand  (unset)
```

Keys shown, in order: `user.name`, `user.email`, `user.signingkey`,
`commit.gpgsign`, `core.sshCommand`. `remotes: (none)` when none. Origins are
printed raw (`file:/abs/path`, `command line:`, etc.), not tilde'd.

### 4.7 `scan [DIR…] [--repo PATH]… [--fix] [--suggest] [-v] [--json] [--plain] [-y]`

- Roots: `DIR…` (expanded, resolved) or `~`. `--repo PATH` (repeatable)
  inspects exactly those paths and skips discovery.
- `scan.find`: `os.walk` (no symlink following); a directory is a repo when it
  contains a `.git` entry (dir or file: worktrees and submodules count);
  nested repos are found; hidden directories (`.`-prefixed) and
  `node_modules` are not descended into. Sorted by path.
- `scan.inspect` per repo: remote URLs (`origin` else first), `user.name` /
  `user.email` with origins (`git config -z --show-origin --get-regexp`),
  values set in the repo's own files (`--local`), and the values "beneath"
  them (what git would resolve if the local file did not set them). A repo
  git refuses to read is recorded with `skip_reason`: `with a broken .git
  link` (gitdir file points to a missing path), `owned by <user>` (other
  uid), else `git can't read` with git's first stderr line.
- `scan.report` (also the `--json` object):

```json
{
  "roots": ["/abs/root"],
  "repos": 2,
  "unreadable": [{"path": "...", "reason": "owned by root", "error": "owned by root: git ignores other users' repos unless listed in safe.directory"}],
  "identities": [{"name": "Work Me", "email": "me@corp.test", "source": "~/.gitconfig-work-github", "repos": 1, "owners": {"github.com/corp-org": 1}}],
  "overrides": [{"path": "/abs/stale", "short": "stale", "remote": "git@github.com:someone/app.git", "owner": "github.com/someone",
                 "now": {"name": "Personal Me", "email": "old@personal.test"},
                 "after": {"name": "Personal Me", "email": "me@personal.test"},
                 "settings": [{"key": "user.email", "value": "old@personal.test", "without": "me@personal.test"}],
                 "fixable": ["user.email"]}],
  "suggestions": [{"id": "someone", "name": "Personal Me", "email": "old@personal.test", "host": "github.com", "owners": ["someone"], "changes_others": 0, "toml": "[[identity]]\n..."}],
  "unmatched": []
}
```

  `source` is `"repo"`, `"pin"`, `"[default]"` (when it is
  `~/.gitconfig-identities`), a tilde path, or `""`. `identities` are sorted:
  repo-sourced last, then by repo count descending. `owners` sorted by count
  desc then name. `short` is the fewest trailing path parts that
  disambiguate. A setting is `fixable` when something beneath provides it.
- Text output (see `view.scan`): header `N repos under <roots>  hidden
  directories and node_modules skipped[ · M skipped: a owned by root, b with a
  broken .git link (-v lists them)]`; `-v` prints `  ⚠ skipped <path>:
  <error>` per skipped repo; a table `repos / identity / from [/ remote
  owners]`, with repo-sourced identities collapsed into `see below  set in the
  repo` unless `-v`; then the overrides tree grouped by identity-after-fix →
  identity-now → repos (`from X <x>:  a, b` or `unchanged: c`), the group
  `their own: nothing else sets one, so fix keeps it` for unfixable ones; then
  either the `--suggest` blocks (TOML, syntax highlighted on a TTY, and `#
  <path> (<owner>): no identity can match it; after fixing it uses <who>` for
  unmatched) or `to keep some of these identities instead: gitpro scan
  --suggest`; finally `dry run, nothing changed. Remove N settings from M
  repos: gitpro scan --fix <dirs>` (or `--repo` forms). With no overrides: `✓
  no repo sets user.name or user.email itself: gitpro decides everywhere`.
- Suggestions (`scan.suggest`): for repos whose fix would change name/email
  and whose remote parses as `host/owner`, one block per (host, name, email)
  with all owners; comments count repos per owner, `# also changes N other
  repo(s) of these owners to this email`, and `# <owner> is in another block
  too: an identity matches by owner, so keep only one`. Ids are derived from
  the most common owner, lowercased, de-duplicated with `-<host label>`.
  Repos with no remote, a local path, or only one of name/email are
  `unmatched`.
- `--fix`: with a TTY on stdout and stdin, without `--plain`, without `-y`,
  and with the `textual` extra installed, opens the TUI (section 5). Else
  prints the report (`fix=True` suppresses the table and the dry-run line),
  asks `Remove N settings from M repos?`, then `scan.fix`: writes
  `~/.local/state/gitpro/scan-undo-<stamp>[-n].sh` (`#!/bin/sh`, a header
  comment `# gitpro scan --fix undo, written <stamp>`, one `git -C <repo>
  config <key> <value>` per removed setting, every argument `shlex.quote`d;
  the file is created with `O_EXCL` and gets mode 0600 from the umask, the
  state dir 0700) **before** removing
  anything with `git config --local --unset-all`, dropping an emptied
  `[user]` section. Output: one `  <repo>  <key> <value>  →  <now>` per
  setting, `✓ removed N settings from M repos`, `↩ undo: sh <script>`. Only
  fixable settings are removed. `--fix` with nothing fixable: `✓ nothing to
  fix`.
- `--fix --json`: no question is asked even without `-y` (see known issues);
  prints `{"removed": [{"path","key","value"}], "undo": "<abs path>"|null}`.
- `$XDG_STATE_HOME` overrides `~/.local/state`.

### 4.8 `pin [DIR]` / `unpin [DIR]`

`pin`: refuses if `<gitdir>/git-identities` exists and is not generated by
us; runs `unpin` first; resolves `user.name` and `user.email` with
`--show-origin` and requires each to come from a file whose first line is the
generated marker (`<key> comes from <origin>, not from gitpro; run 'gitpro
apply', and run pin on the host, not inside the container`; or `git resolves
no <key> for this repo; nothing to pin`); writes
`<gitdir>/git-identities` (header, `# Re-run 'gitpro pin' after changing the
config.`, `[user] name/email`) and `git config --local --add include.path
git-identities`. Prints `✓ pinned Name <email> in <path>`, a `⚠ git resolves
...; a repo-local setting overrides the pin` warning when a local setting
still wins, and `→ signing and SSH key selection are not pinned; re-run after
changing the config`. Works from the common git dir, so worktrees share one
pin.

`unpin`: removes the `include.path` entries (`--fixed-value`), the `[include]`
section if empty, and the file if generated. `✓ unpinned` or `✓ not pinned,
nothing to do`.

### 4.9 `uninstall [-y]`

`plan.teardown`: strips the block from `~/.gitconfig` and `~/.ssh/config`
(removing the file if nothing else is in it and it is not a symlink), removes
`~/.gitconfig-identities`, `~/.ssh/config.d/git-identities`, and every file in
the manifest that is generated. Prints the change list, asks `undo N
change(s)?`, then `✓ removed N change(s); your config file and SSH keys were
left in place`. Pins are not removed. Empty directories (`~/.ssh/config.d`,
`~/.config/git`) are left behind.

### 4.10 Lint warnings (`lint.warnings`, after `plan`/`apply`)

- glob owner with alias: `identity 'x': owner 'o*' is a glob; insteadOf needs a literal prefix, so its URLs aren't rewritten to <alias>`
- key fallback: `identity 'x': ssh tries <key> first, then <default key> ([default].ssh_hosts); if <key> isn't registered you are signed in as the default account. Add an ssh_alias to offer only this identity's key`
- `~/.gitconfig also sets these directly; the generated config is included after them and wins, so they only confuse -- consider removing:` + lines (`user.name|email|signingkey`, `core.sshcommand`)
- `~/.ssh is group/world-writable; ssh may refuse it -- chmod 700 ~/.ssh`
- `~/.ssh/config also defines Host a b; the generated block comes first and wins, remove the old one`
- `<key> missing -- run: gitpro keys --generate`; `<key> is accessible by others -- chmod 600 <path>`

## 5. The TUI (`tui.py`, optional `textual`)

`ScanApp(rep, show)` lists fixable overrides sorted by (identity after,
unchanged-last, identity now, short name). Columns: tick (`☑`/`☐`), repo,
now, after (the "after" column is omitted and a `After the fix they all use
<who>` line shown when all share one target). Sub-title: `N repos ticked, M
settings to remove`. Bindings: `space` toggle, `a` all/none, `s` suggestion
modal (copy with `c`; `no identity can keep this one: it has no remote
gitpro can match` when none), `enter` → `Ask` modal `Remove N settings from M
repos?` (`y`/`n`), `q`/`escape` quit → `None` (CLI prints `✗ aborted,
nothing written`, exit 1). A detail pane shows path, owner and each setting
`key value  →  without` or `(kept: nothing else sets it)`. `choose()` returns
the ticked paths; the CLI then runs the same `_fix` as `-y`.

## 6. VS Code extension (`vscode/`)

- Activation `onStartupFinished`. Output channel `gitpro` (log channel,
  timestamped). Settings: `gitpro.path` (default `gitpro`; in Development
  mode falls back to `../.venv/bin/gitpro` of the checkout), `gitpro.scanDirs`
  (string[]).
- **Status bar** (left, priority 0, command `gitpro.which`): needs only git.
  Directory = active file's dir, else first workspace folder. Hidden outside
  a repo. Text `$(account) <email>` with tooltip `**Name** <email>` / `email
  from \`<origin path>\`` / remotes list; or `$(warning) no git identity` with
  warning background and a fix hint (in a dev container: `on the host, run
  gitpro pin in this repo`; else: add an identity or a default). Refreshes on
  active editor change, window focus, workspace folder change, and
  `**/.git/config` changes; a newer refresh supersedes an older one.
- **Commands**: `gitpro.which` (runs `which <dir>`, shows the log),
  `gitpro.plan`, `gitpro.apply` (runs `plan`; if stdout contains `up to date:
  nothing to change` shows an info message; else modal `Apply the changes
  shown in the gitpro output?` → `apply --yes` → refresh), `gitpro.pin` (in a
  container: warning to pin on the host; else `pin <dir>`), `gitpro.unpin`,
  `gitpro.openConfig` (`$GITPRO_CONFIG` or `~/.config/gitpro.toml`; offers
  `Create from Example` → `init <file>`), `gitpro.refresh`, `gitpro.scan`,
  `gitpro.scanFix`, `gitpro.scanReview`, `gitpro.scanSuggest`,
  `gitpro.openRepoConfig` (hidden from the palette).
- gitpro not found (`ENOENT`): error message with the pipx install line and
  an `Open Settings` button. Non-zero exit: error message with the last
  stderr line (minus `✗ `) and `Show Log`. Unparseable JSON: `gitpro printed
  no JSON; is it up to date?`.
- **Repo Overrides view** (activity bar container `gitpro`, view
  `gitpro.overrides`): welcome texts by context `gitpro.scanState` (`""`,
  `scanning`, `failed`, `done`). Scan runs `scan --json <scanDirs>` in the
  home dir with a 600 s timeout and progress in the view. Tree = `scan.tree`:
  group (identity after fix, or `Kept` with description `N repos · nothing
  else sets an identity`) → before (`from X <x>` / `unchanged` / the identity
  itself under Kept) → repo (label short name, description owner[` · kept`],
  tooltip path + `key: old → new` lines, checkbox only when fixable, click
  opens `.git/config` via `git rev-parse --git-path config`). View message:
  `N repos under ~ · M skipped: ...` when there are overrides.
- **Fix Checked**: `plan(groups, unchecked)`; modal `Remove N settings from M
  repos? They'll use <identity|the identity they are listed under>.`; runs
  `scan --fix --yes --json --repo P…`; logs each removal and `↩ undo: sh
  <script>`; info message `Removed N settings from M repos. Undo script:
  ~/...` with `Undo` (runs `sh <script>` in home, then rescans) and `Show
  Log`; rescans.
- **Review One by One**: steps through fixable repos in tree order with a
  multi-step quick pick `Review <short> (i/n)`: `Fix` (description `now →
  after` or `already uses X`, detail `removes user.email from its
  .git/config[: a duplicate, nothing changes]`), `Skip` (`keeps <now>`), and
  after a separator `Fix this and the N after it`, `Skip this and the N after
  it`; Back button; a button opens `.git/config`. Answers set the checkboxes;
  Escape restores them; finishing runs Fix Checked's confirmation.
- **Suggest Identities**: opens an untitled TOML document: header comments,
  each suggestion's `toml`, and `# unmatched: <path> (<owner>): no identity
  can match it; after fixing it uses <who>` lines.
- **Log levels**: info — every command (`$ gitpro scan --json  (in ~)`),
  exit code and time, stderr lines (as warn on success, error on failure),
  scan summary, each override and what replaces it, review answers, each
  removal and undo path; debug — each skipped repo with reason, checkbox
  changes (`☑ path`/`☐ path`); trace — raw stdout.
- Tests: `node --test` unit tests for `identity.ts`/`scan.ts`; an integration
  test (`@vscode/test-electron`) that checks command registration, status
  text, scan/fix/review/suggest against a temp repo and `GIT_CONFIG_GLOBAL`.

## 7. The Claude Code skill (`skills/gitpro/SKILL.md`)

Front matter `name: gitpro`, a description listing trigger phrases. Rules:
never run `apply`, `uninstall`, `scan --fix` without an explicit yes in the
conversation; `plan` first; edit only the TOML; never read/print private
keys; do not run `keys --generate` (passphrase prompt); do not invent owners,
hosts or emails. Sections: install, config summary, workflow for a change
(read config → smallest edit → `plan` → yes → `apply -y` → `keys` → `which`),
"before committing for the user: run `gitpro which`", a command table, and
troubleshooting. Packaged as a Claude Code plugin via
`.claude-plugin/plugin.json` and `marketplace.json`.

## 8. Tests (`tests/`)

`conftest.py`: `home` fixture (temp `HOME`, env vars `GITPRO_CONFIG`,
`GIT_CONFIG_GLOBAL`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` removed,
`GIT_CONFIG_NOSYSTEM=1`, stdin replaced by an empty `StringIO` so nothing is
a TTY), `write_config(home, text, mode)`, `run(*argv)` → `cli.main`, `git(repo,
*args)`, `repo_with_remote(tmp_path, name, url)`. 55 tests: config
validation, render helpers, scan skip reasons, end-to-end apply/check/
uninstall/signing/migration/pin/scan/fix/suggest/TUI (TUI test skipped
without textual).

## 9. Known issues and inconsistencies (baseline)

Packages may fix the ones listed with a package number; the rest are
documented so nobody "fixes" them by accident.

1. `which` on a directory that is not a repo (or does not exist) prints
   `remotes: (none)` and `(unset)` for every key and exits 0. → fixed by 01
   (`✗ <dir>: not a git repository`, exit 2).
2. `scan --fix --json` writes without `-y` and without a terminal (the
   extension relies on `--yes --json`, so adding the requirement is
   compatible). Kept as is for 0.x; new commands must not copy it.
3. `scan` ignores `-c`/`--home`: it uses the real `Path.home()` and the
   generated files' real locations. Tests work because `HOME` is set. → 02
   adds `-c` to `scan` (for `[scan]` roots) but keeps `Path.home()` for
   labels.
4. The `-v` override tree wraps long absolute paths onto the next line
   (rich `Tree` wrapping); cosmetic.
5. `apply` prints lint warnings on stderr; when both streams go to a pipe the
   `⚠` lines can appear before `config:`. Cosmetic.
6. `uninstall` leaves empty `~/.ssh/config.d` and `~/.config/git`
   directories. Acceptable.
7. `pin` writes `git-identities` with `Path.write_text` (not atomic; mode from
   umask = 0600). Acceptable; 13 does not change it.
8. The VS Code `Apply` command detects "nothing to change" by matching stdout
   text. → 15 switches it to `check --json` (01 adds `--json` to `check`).
9. The status bar only knows the first workspace folder when no editor is
   active; multi-root workspaces show the first folder's identity. → 15.
10. `init` does not accept `-c`; the path is positional. Kept.
11. `keys --generate` is interactive (passphrase prompt) and has no `--json`.
    Kept; 09 adds `--health` and `auth` with `--json`.
12. The git version check is skipped by `which`, `scan`, `pin`. Kept (they
    work on older git).
13. `example.toml` says `ssh_port = 2222` under "Optional, with their
    defaults" although the default is 22; cosmetic, 01 fixes the comment.
14. `version` is `0.1.0` in both `pyproject`/`__init__` and the extension; no
    compatibility handshake between extension and CLI beyond "printed no
    JSON". → 01 adds `gitpro --version --json` with a `capabilities` list
    and one shared version string; 15 reads the capabilities and never
    enforces a minimum version (DECISIONS Q12).
15. `scan.suggest` derives ids from the most common owner (lowercased,
    `-<host label>` suffix on clashes) and `scan.fix` writes its own undo
    script. → 01 moves the undo writer to `undo.write`; 04 reuses the id
    rule for `discover` suggestions (it calls `scan.suggest`, not a copy).
16. `main()` sets `umask 077` for the whole process, so child processes
    (`git clone`) inherit it and create 0600/0700 files. → 01 captures the
    caller's umask as `cli.CALLER_UMASK` (and in `hot`); 07 restores it
    for the `git clone` child only.
17. **Bug** (V-001): `render.url_patterns` emits `git@<host>:**` for an
    identity without `owners`. git's wildmatch treats `**` as "any depth"
    only when it is a whole path component, so `git@github.com:**` does
    not match `git@github.com:o/r.git` (verified with git 2.43
    `hasconfig`): an owner-less identity never applies to an ssh remote
    today. → 01 fixes `url_patterns` to emit `git@<host>:*/**` plus
    `git@<host>:*` (and the `ssh://`/`https://` forms with `/*/**` and
    `/*`), and `resolve.wildmatch` implements the same rule (01 §4.1).
