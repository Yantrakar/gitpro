# Changelog

All notable changes to gitpro. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Release notes for
earlier versions are on [GitHub releases](https://github.com/Yantrakar/gitpro/releases).

## Unreleased

### Added

- `why --json` and the MCP tool `why`: `commit.guard_would_block`, the Git guard's
  verdict (its own identity check under `[guard] on_mismatch`, plus a trailer
  problem). `commit.would_block` stays the agent hook's verdict and is now
  documented as that.
- `setup -n --json`: `agents` (per agent, `hook` and `mcp` as `installed`,
  `plugin`, `missing` or `unavailable`), `uncovered` and `skills`. `setup
  --agent copilot` is accepted so that it can say gitpro has no hook or MCP
  registration for GitHub Copilot CLI.
- `setup -n` reports an installed gitpro skill (`~/.claude/skills/gitpro`,
  `~/.copilot/skills/gitpro`, `~/.agents/skills/gitpro`) that differs from the
  one in this gitpro, and `setup -y` updates only those copies, with a backup in
  gitpro's state folder (`skill-backups/`), not in the skills folder, and an undo
  line. It never creates a copy.
- The skill (`skills/gitpro/SKILL.md`) is inside the wheel and the sdist.
- Spec 23, `docs/spec/23-agent-setup.md`.

### Changed

- `scan --fix --json` without `-y` only previews, like `-n`, and prints
  `"dry_run": true`. 0.3 to 0.4.5 wrote without `-y`. Use `-y`; the VS Code
  extension already does.
- `setup --agent NAME` for an agent gitpro cannot cover (Codex, Copilot CLI)
  prints a `✗` line and exits 1, in `-n` and in `-y`. `setup -n` also exits 1
  when an installed skill copy differs.
- The skill: a rule set for committing, pushing and opening a pull request
  (check that gitpro is there, read `commit.problems` and not the exit code, a
  blocked commit is not done, push and PR fail closed), the missing deprecation
  rows (`setup --write`, `scan --suggest`), and corrected statements about
  `setup`, pins, `uninstall`, ambiguity and "read-only".

### Fixed

- A repo with no remote and no `[default]` name and email: the fix no longer
  offers `gitpro setup --add PATH` (there is no remote to learn from), and the
  text no longer says git will refuse to commit when git has its own email.
- The MCP tool `why` returns the same error object as `why --json` (`error`,
  `config: null`, and the repo fields), with `isError`.

## 0.4.5 (2026-10-22)

### Fixed

- Release: the short announcement is plain text. The release runs releasepro
  0.2.2, which drops a Markdown link's URL from the short post, and the
  workflow names the releasepro version in one place.

## 0.4.4 (2026-10-22)

### Changed

- Release: [releasepro](https://github.com/Yantrakar/releasepro) runs it.
  `releasepro run --as LOGIN` replaces `scripts/release.py`, with the same
  steps, and `releasepro.toml` names the six channels. The GitHub release's
  body is now the version's section of this changelog, with where to get it,
  and the release gets a draft announcement, `ANNOUNCEMENT.md`. The release
  stops before CI when this changelog has no section for the version.

## 0.4.3 (2026-10-21)

### Changed

- Release: gitpro.dev is published last, once PyPI, the Marketplace, Open VSX
  and the MCP Registry have the version; a failed channel keeps it on the
  previous one. The MCP Registry waits for PyPI only, and `scripts/release.py`
  approves each gate as it appears, uploads the VSIX by hand after the run only
  when the `marketplace` job is skipped, and waits for the late listings in its
  final check.

## 0.4.2 (2026-10-20)

### Added

- The README opens with a short demo: the commit guard refuses a commit in a
  work repo that would use the personal email, `gitpro scan --fix` removes the
  repo's own email setting, the commit then goes through with the work email,
  and a personal repo keeps the personal one. `design/demo.py` records it from
  real gitpro and git output in a throwaway home and writes `site/demo.gif`,
  which gitpro.dev serves.
- The VS Code extension is on Open VSX (`yantrakar.gitpro`), and the release
  run publishes it there with trusted publishing, so no token is stored.

### Changed

- Hints, fixes, errors and help name only commands that stay in 0.5.0:
  `gitpro setup` instead of `apply`, `keys --generate`, global `guard install`,
  `agent install` and `mcp install`; `gitpro setup --add PATH` instead of
  `scan --suggest` and `discover`; `gitpro audit --fix` instead of
  `--rewrite`. Fixes that are `gitpro setup` now have `fix_argv: null`, since
  setup needs a terminal; the extension shows **Set up…** for them. An
  extension of 0.4.1 or older shows only the text for those problems.
  `doctor` and `agent status` share these hints and print the new wording too.
- VS Code: the Marketplace listing has search keywords, and its description
  starts with the problem gitpro solves.
- Release: `scripts/release.py` prints each job as it changes state. Run
  again after it stops (Ctrl-C, a failed job, a gate answered no), it follows
  the run still going on that commit or offers to re-run the failed jobs. It
  checks Open VSX at the end when the `openvsx` job published.
- Release: the run publishes the VSIX to the VS Code Marketplace too, signed
  in with a Microsoft Entra managed identity, so no token is stored.
  `scripts/release.py` waits for that job instead of asking for an upload;
  the upload by hand is left for a run where the job is skipped.

## 0.4.1 (2026-10-18)

### Fixed

- `gitpro why` and the MCP `check_commit` tool mask a password or token in the
  remote URL they name when a commit's identity is wrong.
- The MCP server also masks secret query parameters (`?access_token=...`) in
  every string it returns, and masks URL credentials in its error text.
- `gitpro keys --health --online`, `gitpro keys --upload` and `gitpro doctor`
  mask tokens in the gh and glab error output they print.
- `gitpro scan --lost` no longer shows a password or token from a remote URL in
  the key of a duplicate.
- `gitpro why` and the MCP `check_commit` tool read commit trailers with the
  same parser and the repo's own trailer settings as the commit-msg hook, so
  their verdict matches the hook's.
- `gitpro agent install --scope project` also clears `GIT_COMMON_DIR` and
  `GIT_PREFIX` before running git, so inside a hook it can't write another
  repo's `info/exclude`.
- Writes are fsynced, including the directory after a rename, and text is
  written as UTF-8 whatever the locale. Backups and incident records are
  fsynced, and `gitpro init` writes its config atomically.
- VS Code: the log masks tokens in CLI and git output, and the dev container
  edit is written atomically and refused if the file changed while its dialog
  was open.

### Changed

- The version is written in one place, `src/gitpro/VERSION`; the VSIX and the
  MCP Registry entry get it at build time. The Claude Code plugin manifests no
  longer carry a version, so Claude Code tracks the plugin by commit.
- Release: re-running a failed job after the tag was created works, PyPI
  uploads skip files already there, `scripts/release.py --run` checks that the
  run is for the local commit, and the PyPI gate no longer waits behind the
  Marketplace upload.
- Shared helpers replace duplicated code in the CLI and the VS Code extension;
  commands, flags, output and `--json` are unchanged.

## 0.4.0 (2026-10-16)

### Added

- `gitpro setup` offers to look through your repos and proposes identities from
  them: the host, the owner, the name and the email. The email comes from the
  commit identity in the reflog. It also reads the SSH config, `includeIf`
  rules and VS Code and JetBrains recents. It never reads private keys, shell
  history or forge accounts.
- `gitpro setup PATH...` looks for identities in the repos under those
  folders without asking first, and adds them to an existing config.
- `gitpro setup -n` shows what is still to do and exits 0 or 1. `--json` has
  `identities`, `pending` and `done`.
- `gitpro why --committer`, `--author` and `--message` say whether the guard
  would block a commit. `why --remote` picks a remote. The JSON gains
  `settings` and `commit`.
- `gitpro scan --lost` checks for work that is only on this machine, offline.
  The JSON gains `lost`. It exits 1 when it finds lost work, as `lost` did.
- `gitpro pin --remove` and `gitpro audit --fix`.
- MCP: `why` takes `remote`, `author_email`, `committer_email` and `message`.
  New `setup`, `env` and `report` tools. `scan` takes `lost`.
- The capabilities `why.commit`, `scan.lost`, `pin.remove`, `audit.fix`,
  `setup.dry-run`, `setup.learn` and `deprecations.v1`.
- `-y`, `--json`, `-v`, a `PATH` and `-c PATH` mean the same in every command
  that has them. `-n` (`--dry-run`) is on `setup`, `pin`, `scan` and `audit`;
  on `pin`, `scan --fix` and `audit --fix` it shows the same preview as
  without `-y`, with the same exit code. `-n -y` is refused.

### Changed

- The commit guard refuses a commit in a repo whose remotes belong to
  different identities, for example a fork where `origin` is yours and
  `upstream` is the company's, until the repo is pinned. 0.3 applied the
  identity that comes later in your config to every remote and said nothing.
  Hooks that 0.3 installed enforce the new rule as soon as you upgrade; your
  config does not change. Pin the remote your work goes to with
  `gitpro pin --remote NAME`, set `[guard] on_mismatch = "warn"` for a warning
  instead, or skip once with `GITPRO_SKIP=1`. `why` and `scan` report such a
  repo as `ambiguous`. A pin made with 0.3 does not settle such a repo, since
  0.3 pinned the identity git applied; pin it again with `--remote`.
- The terminal, VS Code and MCP share one command surface. `gitpro --help`
  lists `setup`, `why`, `scan`, `pin`, `clone`, `audit`, `env` and `uninstall`,
  then a "more" group.
- `gitpro setup` replaces `init`, `apply` and the agent and MCP install
  commands. It does more than `apply`: it also installs the commit guard
  (`[guard] hooks = false` keeps it off) and, when Claude Code is on the
  machine, its hook and the MCP server (`--agent NAME` picks other agents,
  `--agent none` skips them). So `gitpro setup -y` is not a drop-in for
  `gitpro apply -y`, and `gitpro setup -n` exits 1 where `gitpro check` exited
  0, until the guard and the hook are in place.
- `gitpro setup` without a terminal and without `-y` or `-n` exits 2, as in
  0.3. The message now names both flags.
- `gitpro why --json` without a config still exits 2 with the error, and now
  also has the repo's `settings` and `remotes`.
- "No config found" errors (and `doctor`'s fix for them) now name
  `gitpro setup` instead of `gitpro init`.
- `check` without `--policy`, `--repo` or `--range` is deprecated; the team
  policy check (`check --policy FILE --range A..B`, `check --repo DIR`) stays
  and is listed under "more".
- VS Code: the Plan, Apply, Doctor and scan Suggest/Review commands are gone;
  Set Up runs `gitpro setup` in a terminal. The extension writes the
  devcontainer pin itself (a modal shows the line first) and uses
  `why --json`, `scan --lost` and `pin --remove` when the CLI has them, with
  the 0.3 commands as the fallback.
- The user guide, the agent guide, the skill and the website follow the new
  surface.

### Fixed

- `gitpro scan --fix` in a repo with linked worktrees removed a setting once
  per worktree and failed on the second; it now removes each setting once.
- Creating an SSH key (`setup`, `keys --generate`) no longer replaces a `.pub`
  or a symlink already at the key's path; it says so and creates no key.
- The undo script for a settings file gitpro created removes the file only if
  it still holds what gitpro wrote. The Gemini hook and MCP server share
  `~/.gemini/settings.json`, and undoing the first removed the second.

### Deprecated

These still work in 0.4.x, print a line on stderr (or add `deprecated` to
`--json`) and are removed in 0.5.0. `GITPRO_NO_DEPRECATION=1` turns the line off.

- Commands: `init`, `apply`, `plan`, `check` without `--policy`/`--repo`/
  `--range`, `doctor`, `which`, `lost`, `discover`, `unpin`,
  `audit --rewrite`, `guard install` without `--repo`, `agent install`,
  `agent status`, `mcp install`, `devcontainer`, `keys --generate`,
  `scan --suggest`, `setup --write`, and the operation commands (`preflight`,
  `run`, `operation-status`, `operation-reconcile`, `change ...`). Each names
  its replacement. They are hidden from `--help` and shell completion.
- MCP tools: `which_identity`, `check_commit`, `identities`, `lost`, `env_for`,
  `draft_report`, and the operation tools. Their descriptions start with
  "Deprecated, removed in 0.5.0; use <tool>."

## 0.3.4

`gitpro report`, support links and xdist CI. See the
[GitHub release](https://github.com/Yantrakar/gitpro/releases).

## 0.3.3

The gitpro icon, links to gitpro.dev and MCP Registry metadata.

## 0.3.2 and earlier

See [GitHub releases](https://github.com/Yantrakar/gitpro/releases).
