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

- **Never run `apply`, `uninstall` or `scan --fix` without the user's explicit yes in this
  conversation.** Run `gitpro plan` first, show the user the diff, and
  only after they confirm run `gitpro apply -y` (`-y` is required
  without a terminal).
- Edit only the TOML config. The files it generates (`~/.gitconfig-identities`,
  `~/.gitconfig-<id>`, `~/.ssh/config.d/git-identities`, the marked blocks in
  `~/.gitconfig` and `~/.ssh/config`) are overwritten on every `apply`.
- Never read, print or copy private SSH keys. Public keys (`.pub`) are fine.
- Don't run `keys --generate` yourself: it prompts for a passphrase. Ask the
  user to run it in their terminal. `--no-passphrase` only if they ask for it.
- `gitpro setup` is a preview-only terminal wizard. To save, the user runs
  `gitpro setup --write` and confirms Save; there is no noninteractive `-y`.
- Don't invent owners, hosts or emails. Ask the user, or read them from the
  remote URLs of their repos (`git remote -v`).
- Before any `git commit`, `git push` or `gh pr create` for the user, run
  `gitpro why` in that repo (or call the `why` MCP tool). If it reports a
  problem (a `✗` line), show the user the `→` fix line and ask before running
  it. Never use `--no-verify` or set `GITPRO_SKIP=1` unless the user says so
  in this conversation.
- Never run `gitpro guard install`, `agent install`, `mcp install`,
  `devcontainer --write`, `keys --upload`, `audit --rewrite` or `clone`
  without an explicit yes. Always show the dry run first: `guard status`,
  `agent status`, `devcontainer`, `audit`, `clone --dry-run`. These commands
  ask on a terminal and you have none, so pass `-y` only after the user said
  yes.
- `devcontainer.json`, `.mcp.json` and `.claude/settings.json` are team
  files. gitpro prints what to add; use `--write` only when the user owns the
  repo and asks for it.
- Never pass `--online`, `--history` or `--send-repo-names` unless the user
  asks. Before an online command, tell the user what its first `online:` line
  (or its "needs the network" message) says will be sent.
- `gitpro env` prints the commands that fetch tokens, never the tokens. Do
  not try to obtain token values, and never read `gh`/`glab` config files,
  `.netrc` or `.git-credentials`.
- Use `--json` when you need to read a result; plain output when you show it
  to the user. With `--json`, a writing command also needs `-y`.
- A `***` in a URL gitpro prints is a password, token or secret query value
  that it masked on purpose. The masking is for display only: do not try to
  recover the value or paste the masked URL into a git command.

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
`~/.config/gitpro.toml`. `gitpro setup` guides the user through basic configuration
and can add identities while preserving an existing config. Alternatively, `gitpro init` writes a
commented example; `plan` refuses to run until its placeholder emails and
hosts (`*.example`, `example.com`) are replaced.

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

When `change-propose.v1` is available, use the
[approved change workflow](../../docs/agent-workflows.md#approved-persistent-changes)
instead of editing live configuration. Supply a complete explicit TOML document
(`configure`), render existing settings (`regenerate`), or request the bounded
`repo-identity` repair. `gitpro change propose` / MCP `propose_change` returns an
exact proposal without changing target files. Save it privately and ask the
human to run `gitpro change approve --write --proposal FILE` in their terminal.
**Never drive that prompt or manufacture a TTY to approve on their behalf.**
After approval and explicit host/user execution permission, use
`gitpro change apply --plan-id ID -y` / `apply_change`, then preflight the operation.
No-op proposals need no apply. Repeated apply uses the same plan ID.

With `approval-policy.v1`, an operator may explicitly preauthorize narrow kinds
and file/repository scopes using `--approval-policy`. Do not create, broaden or
select a policy on the agent's own authority. A covered proposal can be applied
headlessly using its exact plan ID and `--proposal FILE`; MCP `apply_change` takes
the normalized `request` and uses only the server operator's startup policy.
An uncovered proposal still requires human approval. Host execution permission,
stale-plan checks and all ordinary safeguards remain. Policy files and startup
arguments must be protected by the host; same-UID file permissions are not an
enforced boundary against an unrestricted agent.

For an interrupted change, inspect `gitpro change status` / `change_status`;
`gitpro change recover` / `recover_change` uses the approved rollback, not a
forward retry, and still needs execution permission. Do not overwrite recovery
conflicts or remove unknown locks. An `undo` proposal references the applied
`change_id` and requires a separate approval. Never use a fresh plan to conceal
an indeterminate previous attempt.

For older installations, with explicit permission to edit configuration:

1. Read the config. Make the smallest edit that does what the user asked.
2. `gitpro plan`. Show the diff and any `⚠` warning lines.
3. On the user's yes: `gitpro apply -y`.
4. If keys were added: `gitpro keys` prints each public key and the
   account to register it with (twice on GitHub with `sign`: as an
   authentication key and as a signing key). Test with `ssh -T git@<alias>`.
5. Verify in a repo: `gitpro why <dir>` (or `gitpro which <dir>`).

## Other workflows

- **"The repo commits as the wrong person"**: `gitpro why` → show its `→`
  fix and run it on the user's yes → `gitpro why` again until it says
  `✓ identity matches`. If history is already wrong: `gitpro audit` → show
  the list → on the user's yes, `gitpro audit --rewrite -y` (it rewrites only
  unpushed commits, needs a clean working tree and prints an `↩ undo:` line;
  tell the user to fetch first, since "pushed" means as of the last fetch).
- **"Set up a new machine"**: the user runs `gitpro setup --write` in their terminal →
  `gitpro plan` → yes → `gitpro apply -y` → `gitpro keys` →
  `gitpro auth` (online: say so first) → `gitpro doctor`.
  For broader discovery, `gitpro init --discover` remains available (add
  `--history` only if the user wants shell history read); then review the template.
- **"Lost work before wiping or moving a machine"**: `gitpro lost` (offline,
  read-only), then `gitpro lost --online` if the user asks. Suggest a
  `git fetch` first so "unpushed" is current.
- **"Make the agent safe"**: `gitpro agent status`. Recommend the gitpro
  plugin (it brings the hook and the MCP server) or `gitpro agent install`.
  The hook never grants anything; it only denies with a reason.
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

When capabilities include `preflight.v1` and `run.v1`, prefer the
[structured agent workflow](../../docs/agent-workflows.md):
construct a version-1 request for the exact commit, push or GitHub PR;
call `preflight` through CLI/MCP; proceed only on `decision: "ready"` and
host/user authorization. A ready `plan_token` detects changed state, **not**
permission. `gitpro run` needs that token, a unique idempotency key and `-y`.
Network preflight/execution also needs explicitly approved `--online`.
MCP `run_operation` is available only when the operator enabled execution.
Never enable it or change persistent configuration without user approval.

Retry a lost response only with the **same request, original token and key**.
Use `gitpro operation-status` / MCP `operation_status` to inspect the durable
result. `gitpro operation-reconcile` / MCP `reconcile_operation` observes recorded
intent without retrying writes or changing the ledger. Remote inspection needs
explicit network permission. `observed` does not prove attribution or permit a
retry (`retry_safe` remains false). For `indeterminate` or `outcome_unknown`,
inspect effects and stop; do not
invent a fresh key to repeat the write. New intended operations require a new
preflight and key. Do not use `eval` or obtain token values for this workflow.

For older installations:

Run `gitpro why` in the repo. It prints the remotes, the identity the repo
should use and why, the effective name and email with the file each comes
from, the SSH key and signing, and a `→` fix for every problem. If it shows a
`✗` line, stop and show the user the fix instead of committing. `gitpro
which` shows the raw effective settings.

## Commands

Every command has `--help`.

With `dry-run-defaults.v1`, bare writing commands preview rather than execute.
Use `-y`/`--yes` only after host/user authorization; `--write` retains interactive
confirmation where applicable. Existing explicit action flags (`--fix`,
`--rewrite`, `--generate`, `--upload`) retain their safeguards. A preview never
grants approval. `run` without execution consent returns preflight, while
`change apply` previews and `change recover` reads status.

| command | writes? | needs yes? | network |
|---|---|---|---|
| `preflight`, `operation-status`, `operation-reconcile` | no | no; `--online` requires explicit permission | preflight/reconciliation can read destination/account state with `--online` |
| `run` | yes: reviewed Git/forge operation and durable journal; never identity config | **yes**, then `-y`; original plan token and idempotency key required | push/PR require `--online` |
| `change propose`, `change status` | no target writes | no | no |
| `change approve` | only with `--write`: immutable approval and backup journal | **yes**, human reviews in their own terminal; no `-y` | no |
| `change apply`, `change recover` | yes: authorized exact files or conflict-safe restoration | **yes**, then `-y`, plus human approval or an operator-scoped grant; recovery retains original rollback permission | no |
| `which`, `why`, `doctor`, `prompt`, `completion`, `check`, `plan`, `scan`, `scan --roots`, `scan --suggest`, `lost`, `audit`, `keys`, `keys --health`, `guard status`, `guard test`, `agent status`, `devcontainer`, `devcontainer status`, `policy show`, `policy lint`, `team`, `env`, `discover`, `clone --dry-run` | no | no | no (`--online` is opt-in where present: ask first) |
| `report` | no | **yes**, to open the form or send; `-y` answers it | with `--online` (`gh`): say so |
| `guard check`, `agent-check`, `mcp` | no; git hooks, the agent hook and the MCP client call these | do not run them yourself | no |
| `auth`, `forge repos`, `forge forks`, `forge email` | no | no, but say it goes online | **yes**, say so |
| `discover --history`, `init --discover --history` | reads shell history (`init` also writes the config) | **yes** | no |
| `init`, `init --discover` | previews; `-y` creates the config and never overwrites | **yes** | no |
| `setup` | previews; `--write` permits saving with backup before updating | **yes**, user confirms Save in their terminal | no |
| `apply`, `scan --fix`, `pin`, `unpin`, `uninstall`, `guard install`, `guard uninstall`, `agent install`, `agent uninstall`, `mcp install`, `mcp uninstall`, `audit --rewrite` | yes | **yes**, then `-y` | no |
| `clone` | yes: a new directory | **yes**, then `-y` | yes: `git clone` |
| `devcontainer --write`, `mcp install --scope project --write` | yes, **team file** | **yes**, then `-y` | no |
| `keys --upload`, `forge similar` | adds keys to the account / sends repo names | **yes** + `--online` (+ `--send-repo-names`) | **yes** |
| `keys --generate` | creates keys, interactive | do not run: ask the user to | no |

Notes:

- Plain `check` only exits 1 when `apply` would change something; it never
  looks at policies. `check --repo DIR` checks the repo's identity against its
  team policy, `check --policy FILE` checks every identity that uses FILE,
  and `check --policy FILE --range A..B` checks commits (CI; the base's policy
  judges).
- `team --online` shows `⚠` (never `✗`) when the forge did not answer; the
  reason is on stderr and in `warnings`.
- `scan --json`, `why --json`, `lost --json` and the other `--json` forms
  print one JSON object for you to read.
- `scan --fix` keeps an override that is a repo's only identity. Without `-y`
  it is interactive. It prints the undo script's path.
- `uninstall` removes everything `apply` wrote and the global guard hooks. It
  does not remove agent hooks, MCP registrations, per-repo hooks, pins or
  `devcontainer.json` entries; each of those has its own undo.
- `env` output is meant for `eval "$(gitpro env)"` in the user's shell.

## MCP server

The gitpro plugin (or `gitpro mcp install`) gives you an MCP server with
these tools: `which_identity`, `why`, `check_commit`, `scan`,
`identities`, `env_for`, `lost`, `audit`, `preflight`, `operation_status`, `reconcile_operation`,
`propose_change`, `change_status`.
The explicitly enabled execution server additionally exposes `run_operation`,
`apply_change` and `recover_change`. There is no approval tool or client-selectable
policy. The operator can configure `gitpro mcp --allow-execution --approval-policy PATH`.

- Before committing, call `why`, then `check_commit` with the commit message
  if you need to know whether the guard would block it.
- Every default tool is read-only and returns the same JSON as the matching `--json`
  command.
- A `fix` field is a command to propose to the user, not to run.
- `env_for` returns the commands that fetch a token, never a token.

## Troubleshooting

- Not sure what is wrong: `gitpro doctor` runs every check and lists the fixes.
- Wrong or no identity: compare `which` output's remote URL with `host` and
  `owners` (case!). A repo-level `user.email` overrides everything:
  `gitpro scan <dir>` shows it, `gitpro scan --fix -y <dir>` removes it.
- "Please tell me who you are": no identity matches and `[default]` has no
  name/email. This is intended; add an identity or a default.
- Inside a dev container (VS Code Dev Containers copy `~/.gitconfig` but not
  its includes): on the host, run `gitpro pin` in the repo, or add the entry
  that `gitpro devcontainer` prints so it happens on every start. Signing and
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
  commit went through. Run `gitpro doctor`.
- The agent hook does nothing: it runs `gitpro` from a plain `sh`, so gitpro
  must be on that `PATH` (`command -v gitpro`).
