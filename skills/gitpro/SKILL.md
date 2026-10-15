---
name: gitpro
description: Set up and manage which git name, email, SSH key and commit signing each repo uses, chosen by the repo's remote URL, with the gitpro CLI. Use when the user wants separate work/personal git identities, a second GitHub or GitLab account, commit signing per account, asks why a commit has the wrong email, commits from a dev container, or before committing, pushing or opening a pull request for the user.
license: MIT
---

# gitpro

`gitpro` turns one TOML file into plain git and ssh config. Git then
picks the identity for each repo from its remote URL. Nothing of the tool runs
while git runs, unless the user installed the optional guard hooks.

Docs: https://github.com/Yantrakar/gitpro/blob/main/docs/guide.md

## Rules

- **Never run `setup`, `uninstall`, `scan --fix`, `pin`, `clone` or `audit --fix`
  without the user's explicit yes in this conversation.** Run `gitpro setup -n`
  first, show the user what it says is still to do, and only after they
  confirm run `gitpro setup -y` (`-y` is required without a terminal; without
  `-y` or `-n` and without a terminal, `setup` exits 2).
- Edit only the TOML config. The files it generates (`~/.gitconfig-identities`,
  `~/.gitconfig-<id>`, `~/.ssh/config.d/git-identities`, the marked blocks in
  `~/.gitconfig` and `~/.ssh/config`) are overwritten on every `setup`.
- Never read, print or copy private SSH keys. Public keys (`.pub`) are fine.
- `gitpro setup` sets up the whole machine (config, SSH keys, Git and SSH
  files, the guard, the Claude Code hook and MCP server) after one yes. It
  offers to look through the user's repos and proposes identities from them
  (it never reads private keys, shell history or forge accounts). The user runs
  it in their terminal: the first run asks questions, and new keys ask for a
  passphrase. Don't run it yourself without their explicit yes.
- Don't invent owners, hosts or emails. Ask the user, or read them from the
  remote URLs of their repos (`git remote -v`), or let `gitpro setup` find
  them.
- Before any `git commit`, `git push` or `gh pr create` for the user, run
  `gitpro why` in that repo (or call the `why` MCP tool). If it reports a
  problem (a `✗` line), show the user the `→` fix line and ask before running
  it. Never use `--no-verify` or set `GITPRO_SKIP=1` unless the user says so
  in this conversation.
- Never run `setup`, `guard install --repo`, `audit --fix`, `keys --upload` or
  `clone` without an explicit yes. Always show the dry run first: `setup -n`,
  `guard status`, `audit`, `clone --dry-run`. These commands ask on a
  terminal and you have none, so pass `-y` only after the user said yes.
- `devcontainer.json`, `.mcp.json` and `.claude/settings.json` are team
  files. gitpro prints what to add; edit them only when the user owns the
  repo and asks for it.
- Never pass `--online`, `--history` or `--send-repo-names` unless the user
  asks. Before an online command, tell the user what its first `online:` line
  (or its "needs the network" message) says will be sent.
- When gitpro crashes (`✗ internal error (…): this is a gitpro bug`) or blocks
  a commit or push the user thinks it should not, offer `gitpro report`
  (`--crash`, `--denial`; `--security` for a vulnerability). Show the user
  `gitpro report --print` and never open or file it without their yes in
  this conversation.
- `gitpro env` prints the commands that fetch tokens, never the tokens. Do
  not try to obtain token values, and never read `gh`/`glab` config files,
  `.netrc` or `.git-credentials`.
- Use `--json` when you need to read a result; plain output when you show it
  to the user. With `--json`, a writing command also needs `-y`.
- A `***` in a URL gitpro prints is a password, token or secret query value
  that it masked on purpose. The masking is for display only: do not try to
  recover the value or paste the masked URL into a git command.
- Commands and MCP tools marked deprecated still work in 0.4.x and are removed
  in 0.5.0. Use their replacements (see the end of the Commands section).
  `GITPRO_NO_DEPRECATION=1` silences the deprecation line.

## Install

Check with `gitpro --version`. If it is missing, ask before installing:

```sh
pipx install gitpro
```

Needs Linux or macOS, git 2.36+, Python 3.9+, OpenSSH. gitpro is on
PyPI. `gitpro --version --json` lists `capabilities`; check for one there
(for example `why` or `mcp`) instead of comparing version numbers.

## The config

Path: `-c PATH`, else `$GITPRO_CONFIG`, else
`~/.config/gitpro.toml`. `gitpro setup` finds the user's identities in their repos (or asks), writes
the config and can add identities while preserving an existing config. The
example config uses placeholder emails and hosts (`*.example`, `example.com`)
that gitpro refuses until they are replaced.

```toml
[default]                          # repos no identity matches (all keys optional)
name = "Jane Doe"
email = "jane@personal.dev"
ssh_key = "~/.ssh/id_ed25519"
ssh_hosts = ["github.com"]         # ssh always uses ssh_key for these hosts
sign = true                        # SSH-sign commits and tags

[[identity]]
id = "work-github"                 # short name; settings go to ~/.gitconfig-<id>
name = "Jane Doe"
email = "jane.doe@employer.com"
host = "github.com"
owners = ["janedoe-employer", "employer-org"]   # exact case, as in the remote URL
ssh_key = "~/.ssh/id_ed25519_work"
ssh_alias = "github-work"          # ssh Host that offers only ssh_key
```

- `id`, `name`, `email`, `host` are required for an identity.
- `owners`: GitHub users/orgs or GitLab groups/subgroups from the URL path,
  case-sensitive. Omit to match every repo on `host`.
- A repo matches when any remote matches; if several identities match, the
  later one in the file wins.
- Two accounts on the same host need separate `ssh_key`s. Give the extra one an
  `ssh_alias`, otherwise ssh can fall back to the default key and sign in as
  the default account.
- Other identity keys: `rewrite` (default true), `rewrite_https`, `ssh_user`,
  `ssh_port`, `sign`, `file`, `gh_account`, `glab_account`, `clone_dir`,
  `policy`, a `trailers` table (`co_authored_by`, `forbid_emails`), and extra
  git settings under `[default.git.<section>]` / `[identity.git.<section>]`.
- Optional tables: `[scan]` (`roots`, `exclude`, `stale_days`), `[guard]`
  (`hooks`, `on_mismatch`, `chain_to`), `[clone]` (`dir`, `layout`),
  `[forge]` (`online`, `similar`), `[agents]` (`hook`, `bot_emails`),
  `[policy]` (`files`). The user guide's configuration section explains each.
- Unknown keys are errors. Read the error message and fix the TOML.

## Workflow for a change

1. Read the config. Make the smallest edit that does what the user asked, with
   their permission to edit configuration. Or let `gitpro setup --add PATH`
   (run by the user) learn identities from their repos.
2. `gitpro setup -n`. Show what it says is still to do and any `⚠` warning lines.
3. On the user's yes: `gitpro setup -y`.
4. If keys were added: `gitpro keys` prints each public key and the
   account to register it with (twice on GitHub with `sign`: as an
   authentication key and as a signing key). Test with `ssh -T git@<alias>`.
5. Verify in a repo: `gitpro why <dir>`.

## Other workflows

- **"The repo commits as the wrong person"**: `gitpro why` → show its `→`
  fix and run it on the user's yes → `gitpro why` again until it says
  `✓ identity matches`. If the repo has two remotes for two identities, `why`
  and `scan` say so; the user chooses with `gitpro pin --remote R`. If history
  is already wrong: `gitpro audit` → show the list → on the user's yes,
  `gitpro audit --fix -y` (it rewrites only unpushed commits, needs a clean
  working tree and prints an `↩ undo:` line; tell the user to fetch first,
  since "pushed" means as of the last fetch).
- **"Set up a new machine"**: the user runs `pipx install gitpro` and
  `gitpro setup` in their terminal (it offers to find their identities in
  their repos) → `gitpro scan` (repos that need a fix) → `gitpro auth` (online:
  say so first) if they want the forge checks.
- **"Lost work before wiping or moving a machine"**: `gitpro scan --lost`
  (offline, read-only). Suggest a `git fetch` first so "unpushed" is current.
- **"Make the agent safe"**: `gitpro setup -n` shows which hooks are in place.
  Recommend the gitpro plugin (it brings the hook and the MCP server) or
  `gitpro setup --agent claude`. The hook never grants anything; it only denies
  with a reason.
- **"Clone a repo"**: `gitpro clone --dry-run <url>` → show the plan → on
  the user's yes, `gitpro clone <url> -y`.
- **"Our team has rules"**: `gitpro team`, then the `gitpro check --policy
  FILE` command it ends with (or `gitpro check --repo <dir>` for one repo)
  for the details, and `gitpro policy show` for the rules. `gitpro why`
  also shows a repo's policy problems. Plain `gitpro check` does not look at
  policies.
- **"Set up the policy check in CI"**: GitHub, the Action in the user guide (`uses:
  Yantrakar/gitpro@<ref>` with `fetch-depth: 0`); GitLab, the guide's
  job. In CI, gitpro reads a policy file in the repo, and its
  `allowed_signers`, as they are at the range base, so a pull request cannot
  loosen the policy that judges it. A policy change in the PR takes effect
  only once it is merged. Keep the range base on a protected branch (the
  Action's default is), and recommend CODEOWNERS on `.gitpro-policy.toml`
  and `allowed_signers`. These are team files: show the snippet and let the
  user add it.

## Before committing for the user

Run `gitpro why` in the repo. It prints the remotes, the identity the repo
should use and why, the effective name and email with the file each comes
from, the SSH key and signing, and a `→` fix for every problem. If it shows a
`✗` line, stop and show the user the fix instead of committing. To ask whether
the guard would block a particular commit, add `--committer EMAIL`,
`--author EMAIL` and `--message TEXT` with `--json`; the `commit` object says
`would_block`. `gitpro why --json` has a `settings` field with the effective
settings.

## Commands

Every command has `--help`. `gitpro --help` lists `setup`, `why`, `scan`, `pin`,
`clone`, `audit`, `env` and `uninstall`, then a "more" group.

The same flags mean the same everywhere: `-y` does not ask,
`--json` prints one JSON object, `-v` adds detail, a `PATH` names the repo or
folder, `-c PATH` the config. `setup -n` shows only; `setup` asks once on a terminal. `pin`,
`scan --fix` and `audit --fix` show a preview without `-y`. A preview never
grants approval.

| command | writes? | needs yes? | network |
|---|---|---|---|
| `why`, `prompt`, `completion`, `scan`, `scan --roots`, `scan --lost`, `audit`, `keys`, `keys --health`, `guard status`, `guard test`, `policy show`, `policy lint`, `team`, `env`, `setup -n`, `clone --dry-run` | no | no | no (`--online` is opt-in where present: ask first) |
| `report` | no | **yes**, to open the form or send; `-y` answers it | with `--online` (`gh`): say so |
| `guard check`, `agent-check`, `mcp` | no; git hooks, the agent hook and the MCP client call these | do not run them yourself | no |
| `auth`, `forge repos`, `forge forks`, `forge email` | no | no, but say it goes online | **yes**, say so |
| `setup` | yes: config, keys, Git and SSH files, guard, agents; previews first | **yes**, user runs it in their terminal; `-y` only after their yes | no |
| `scan --fix`, `pin`, `pin --remove`, `uninstall`, `guard install --repo`, `guard uninstall`, `agent uninstall`, `mcp uninstall`, `audit --fix` | yes | **yes**, then `-y` | no |
| `clone` | yes: a new directory | **yes**, then `-y` | yes: `git clone` |
| `keys --upload`, `forge similar` | adds keys to the account / sends repo names | **yes** + `--online` (+ `--send-repo-names`) | **yes** |

Deprecated in 0.4.0, removed in 0.5.0. They behave as in 0.3.4, print a line on
stderr and are hidden from `--help`:

| command | writes? | needs yes? | use instead |
|---|---|---|---|
| `init` | yes (deprecated): creates the example config | **yes**, then `-y` | `gitpro setup` |
| `apply` | yes (deprecated) | **yes**, then `-y` | `gitpro setup -y` |
| `unpin` | yes (deprecated) | **yes**, then `-y` | `gitpro pin --remove` |
| `audit --rewrite` | yes (deprecated) | **yes**, then `-y` | `gitpro audit --fix` |
| `guard install` | yes (deprecated without `--repo`) | **yes**, then `-y` | `gitpro setup` |
| `agent install`, `mcp install` | yes (deprecated) | **yes**, then `-y` | `gitpro setup --agent NAME` |
| `devcontainer --write` | yes (deprecated): **team file** | **yes**, then `-y` | VS Code "Set up devcontainer pin" |
| `keys --generate` | yes (deprecated): creates keys, interactive | do not run: ask the user to | `gitpro setup` |
| `plan`, plain `check`, `doctor`, `which`, `lost`, `discover`, `agent status`, `devcontainer`, `devcontainer status` | no (deprecated) | no | `setup -n`, `setup -n`, `setup -n` and `why`, `why --json`, `scan --lost`, `setup PATH`, `setup -n`, VS Code, VS Code |
| `preflight`, `operation-status`, `operation-reconcile`, `change propose`, `change status` | no (deprecated) | no | none |
| `run` | yes (deprecated) | **yes**, then `-y` | none |
| `change approve`, `change apply`, `change recover` | yes (deprecated) | **yes** | none |

Notes:

- Plain `check` only exits 1 when `apply` would change something; it never
  looks at policies. `check --repo DIR` checks the repo's identity against its
  team policy, `check --policy FILE` checks every identity that uses FILE,
  and `check --policy FILE --range A..B` checks commits (CI; the base's policy
  judges). Only plain `check` is deprecated (use `setup -n`); the policy forms
  stay and are listed under "more".
- `setup -n` exits 0 when nothing is to do, 1 when something is, 2 on an error.
  `--json` has `identities`, `pending` and `done`; `ok` is false when
  `pending` is not empty.
- `team --online` shows `⚠` (never `✗`) when the forge did not answer; the
  reason is on stderr and in `warnings`.
- `scan --json`, `why --json` and the other `--json` forms print one JSON
  object for you to read. `scan --lost` adds a `lost` object. `--lost` with
  `--fix` is refused.
- `scan --fix` keeps an override that is a repo's only identity. Without `-y`
  it shows a preview. It prints the undo script's path.
- `pin --remote R` pins the identity that remote R's URL matches, for a repo
  whose remotes match two identities. `pin --remove` with `--remote` is refused.
- `uninstall` removes everything `setup` wrote and the global guard hooks. It
  does not remove agent hooks, MCP registrations, per-repo hooks, pins or
  `devcontainer.json` entries; each of those has its own undo.
- `env` output is meant for `eval "$(gitpro env)"` in the user's shell.
- A deprecated command with `--json` has `"deprecated": {"removed_in": "0.5.0", "use": …}`
  in its object and prints nothing on stderr.

## MCP server

The gitpro plugin (or `gitpro setup --agent NAME`) gives you an MCP server.
Its tools have the commands' names and return the same JSON as the matching
`--json` command:

- `why`: the repo's identity. Takes `path`, `remote`, `author_email`,
  `committer_email` and `message`. Its `settings` field is the effective
  settings; its `commit` field says whether the guard would block the commit.
- `setup`: the state of the machine (`setup -n --json`): identities and what is
  still to do. Changes nothing.
- `scan`: the repos and what needs a fix. With `lost: true` it adds the lost-work
  checks.
- `audit`: commits made as the wrong identity.
- `env`: the commands that fetch the forge token, never a token.
- `report`: a redacted bug report draft.

Deprecated tools stay in the list in 0.4.x, with a description that starts
`Deprecated, removed in 0.5.0; use <tool>.`, and are removed in 0.5.0:

| deprecated tool | use |
|---|---|
| `which_identity` | `why` (its `settings`) |
| `check_commit` | `why` with `committer_email`, `author_email`, `message` (its `commit`) |
| `identities` | `setup` |
| `lost` | `scan` with `lost: true` |
| `env_for` | `env` |
| `draft_report` | `report` |
| `preflight`, `operation_status`, `reconcile_operation`, `propose_change`, `change_status`, `apply_change`, `recover_change`, `run_operation` | none |

- Before committing, call `why`. Pass `committer_email` and `message` if you
  need to know whether the guard would block the commit.
- Every tool is read-only.
- A `fix` field is a command to propose to the user, not to run.

## Troubleshooting

- Not sure what is wrong: `gitpro setup -n` shows what is missing on the
  machine, and `gitpro why` explains one repo.
- Wrong or no identity: compare `why` output's remote URL with `host` and
  `owners` (case!). A repo-level `user.email` overrides everything:
  `gitpro scan <dir>` shows it, `gitpro scan --fix -y <dir>` removes it.
- "Please tell me who you are": no identity matches and `[default]` has no
  name/email. This is intended; add an identity or a default.
- Inside a dev container (VS Code Dev Containers copy `~/.gitconfig` but not
  its includes): on the host, run `gitpro pin` in the repo, or use VS Code's
  "Set up devcontainer pin" so it happens on every start. Signing and
  SSH key choice are not pinned.
- Push or clone signs in as the wrong account: the identity's key isn't
  registered with that account, or it has no `ssh_alias` while the host is in
  `[default].ssh_hosts`. `gitpro auth` (online) shows which account each
  alias signs in as.
- husky, lefthook or pre-commit: repos with their own `core.hooksPath` are
  not covered by the global guard; suggest `gitpro guard install --repo <dir>`.
- "A hook blocked me": read the `cause:` and `fix:` lines, show them to the
  user and ask before running the fix.
- "A hook printed `exited N; not blocking`": the guard failed open and the
  commit went through. Run `gitpro why` in the repo and `gitpro setup -n`.
- The agent hook does nothing: it runs `gitpro` from a plain `sh`, so gitpro
  must be on that `PATH` (`command -v gitpro`).
- Something else, or a gitpro bug: `gitpro report --print` drafts a redacted
  issue (versions and `why`); the user decides whether to open it.
