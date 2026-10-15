# Changelog

All notable changes to gitpro. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Release notes for
earlier versions are on [GitHub releases](https://github.com/Yantrakar/gitpro/releases).

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
  The JSON gains `lost`.
- `gitpro pin --remove` and `gitpro audit --fix`.
- MCP: `why` takes `remote`, `author_email`, `committer_email` and `message`.
  New `setup`, `env` and `report` tools. `scan` takes `lost`.
- The capabilities `why.commit`, `scan.lost`, `pin.remove`, `audit.fix` and
  `deprecations.v1`.
- `-y`, `--json`, `-v`, a `PATH` and `-c PATH` mean the same in every command.
  `-n` is on `setup`.

### Changed

- The terminal, VS Code and MCP share one command surface. `gitpro --help`
  lists `setup`, `why`, `scan`, `pin`, `clone`, `audit`, `env` and `uninstall`,
  then a "more" group.
- `gitpro setup` now also installs the Claude Code hook and the MCP server
  with `--agent NAME`, and replaces `init`, `apply` and the agent and MCP
  install commands.
- `gitpro setup` without a terminal and without `-y` or `-n` now exits 2 and
  names both. In 0.3 it printed a preview and exited 0.
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
