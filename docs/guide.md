# Gitpro user guide

[Back to the quick start](../README.md)

Reference: [configuration](#configuration), [commands](#commands),
[daily use](#daily-use), [SSH keys](#keys), [teams and CI](#teams),
[troubleshooting](#troubleshooting), [development](#development). Commands
replaced in 0.4.0 are in [Deprecated in 0.4.0, removed in 0.5.0](#deprecated-in-040-removed-in-050).

Every repo gets the right git name, email, SSH key and commit signature, chosen
by the repo's **remote URL**. Your identities live in one TOML file, which `gitpro setup` writes.

```mermaid
flowchart LR
    repo["git commit / push"] --> url{"remote URL"}
    url -- "github.com/employer-org/…" --> work["<b>work-github</b><br/>jane.doe@employer.com<br/>~/.ssh/id_ed25519_work"]
    url -- "gitlab.employer.com/platform/…" --> gitlab["<b>work-gitlab</b><br/>jane.doe@employer.com"]
    url -- "anything else" --> default["<b>default</b><br/>jane@personal.dev<br/>~/.ssh/id_ed25519"]
```

Where a repo sits on disk makes no difference.

On top of that, gitpro adds **guardrails** that catch the wrong identity
before it reaches a commit, a push or a pull request: git hooks, a hook for
AI coding assistants such as Claude Code, a VS Code status bar, and an audit
of history. It finds your repos, your lost work and your identities from what
is already on the machine. It checks your SSH keys and forge accounts, gives
AI assistants a read-only MCP server and the right `gh` token, and lets a team
enforce a policy locally and in CI. Everything is free and MIT licensed, and
nothing goes online unless you pass `--online`.

```sh
pipx install gitpro
```

## Install

Requires Linux or macOS, **git 2.36+**, Python 3.9+ and OpenSSH.

```sh
pipx install gitpro
```

This installs a `gitpro` command from [PyPI](https://pypi.org/project/gitpro/). To upgrade: `pipx upgrade gitpro`. `gitpro --version` prints the
version.

## Quick start

```sh
gitpro setup             # offers to look through your repos, proposes your identities, one preview, one yes
gitpro why               # in a repo: the identity it should use, the one it uses, and the fix
gitpro scan              # every repo under ~, and what needs a fix
```

`gitpro setup` is the one command for a machine. It offers to look through
your repos and proposes your identities from what they already use. It then
shows everything it will set up and applies it after one yes. It never changes
a repo; `gitpro scan` shows which repos need attention. The details are under
[setup](#setup-set-up-this-machine).

## How it works

`setup` turns the TOML into plain git and ssh config. git does the matching
itself, so nothing from this tool runs when you use git.

```mermaid
flowchart LR
    toml["~/.config/gitpro.toml<br/><i>created by setup or edited by you</i>"] -- "gitpro setup" --> files

    subgraph files ["what git and ssh read"]
        direction TB
        gc["~/.gitconfig<br/><i>yours + one marked block</i>"] -- "include" --> ids["~/.gitconfig-identities<br/>default identity"]
        ids -- "includeIf the remote URL matches" --> one["~/.gitconfig-work-github<br/>name, email, SSH key, signing"]
        sc["~/.ssh/config<br/><i>yours + one marked block</i>"] -- "Include" --> alias["~/.ssh/config.d/git-identities<br/>Host github-work → work key"]
    end
```

- In `~/.gitconfig` and `~/.ssh/config` it manages only a block between
  `# BEGIN gitpro` and `# END gitpro`, and leaves the rest
  alone. The first time it changes a file you wrote, it saves a copy as
  `<file>.bak.<timestamp>`.
- The other files are fully generated. Edit the TOML, not them.
  Changing an identity's `file` to a symlink alias of its current file keeps
  that file; cleanup removes only outputs that are no longer used.
- With `sign = true` it also writes `~/.config/git/allowed_signers`, so git can
  verify your own signatures.
- Running `setup` again changes nothing. `uninstall` removes everything it wrote.

The guard, which `gitpro setup` installs (`[guard] hooks = false` opts out), is
the one exception to "nothing runs when you use git": it adds git hooks that call gitpro before a commit or
push. See [guard](#guard-stop-a-wrong-identity-commit).

> **Upgrading from 0.1: identities without `owners` now match ssh remotes.**
> 0.1 wrote the pattern `git@<host>:**` for an identity with no `owners`.
> git never matches that against `git@<host>:owner/repo.git`, so such an
> identity applied only to https remotes. 0.2 writes `git@<host>:*/**` and
> `git@<host>:*` instead. Run `gitpro setup -n` to see the change and `gitpro setup`
> to rewrite the generated gitconfig. Repos on that host that used
> `[default]` until now then get that identity.

## Configuration

Below is every key gitpro accepts, with a short comment each. You need only
`[default]` and the `[[identity]]` blocks you use; every table after them is
optional and shown with its default. `gitpro setup` writes this file for you
from your answers.

```toml
[default]                          # repos that no identity matches; every key is optional
name = "Jane Doe"
email = "jane@personal.dev"
ssh_key = "~/.ssh/id_ed25519"
ssh_hosts = ["github.com"]         # use ssh_key for these hosts
sign = true                        # SSH-sign commits and tags
gh_account = "janedoe"             # the gh login of this identity (clone, env, forge, keys --upload)
glab_account = "janedoe"           # the glab login (for [default]: on gitlab.com)
clone_dir = "~/src"                # where `gitpro clone` puts repos of this identity

[default.trailers]                 # what commit trailers may say; identities inherit it
co_authored_by = "allow"           # "allow" | "forbid" | "require" a Co-Authored-By trailer
forbid_emails = []                 # globs; a co-author or author email matching one is reported/blocked

[default.git.pull]                 # any other git setting: [default.git.<section>]
rebase = true

[[identity]]
id = "work-gitlab"
name = "Jane Doe"
email = "jane.doe@employer.com"
host = "gitlab.employer.com"
owners = ["platform"]              # GitLab groups/subgroups; omit to match the whole host

[[identity]]
id = "work-github"                 # short name; settings go to ~/.gitconfig-<id>
name = "Jane Doe"
email = "jane.doe@employer.com"
host = "github.com"
owners = ["janedoe-employer", "employer-org"]   # the account and its orgs
ssh_key = "~/.ssh/id_ed25519_work"
ssh_alias = "github-work"          # ssh Host that offers only ssh_key
rewrite = true                     # send git@github.com:<owner>/… through ssh_alias
rewrite_https = false              # also send https://github.com/<owner>/… over SSH through ssh_alias
ssh_user = "git"                   # for a non-standard SSH user
ssh_port = 22                      # for a non-standard SSH port
sign = true                        # overrides [default].sign
file = "~/.gitconfig-work-github"  # where this identity's settings go
gh_account = "janedoe-employer"    # the gh login of this identity
glab_account = "jane.doe"          # the glab login of this identity
clone_dir = "~/work"               # where `gitpro clone` puts this identity's repos
policy = "~/.config/gitpro/employer-policy.toml"   # a team policy file this identity must satisfy

[identity.trailers]                # same keys as [default.trailers]; unset keys inherit
co_authored_by = "forbid"
forbid_emails = ["*@users.noreply.github.com"]

[identity.git.pull]                # extra git settings for this identity only
ff = "only"

[scan]                             # scan and the VS Code views
roots = ["~"]                      # where your repos live; each starts with ~/ or /
exclude = ["~/.cache", "~/go/pkg/mod"]   # globs; hidden dirs and node_modules are always skipped
stale_days = 180                   # `scan --lost -v`: a repo with no commit for this long is "stale"

[guard]                            # git hooks that check the identity (gitpro guard)
hooks = false                      # true: setup installs and keeps the global hooks
on_mismatch = "block"              # "block" | "warn"
chain_to = ""                      # your previous global hooks dir, run after gitpro's hooks

[clone]                            # gitpro clone
dir = "~/src"                      # used when the identity has no clone_dir (default: the current directory)
layout = "{host}/{owner}/{repo}"   # placeholders: {host} {owner} {repo} {id}

[forge]                            # gh/glab checks
online = false                     # true: forge commands may go online without --online
similar = false                    # true: `forge similar` may run without --send-repo-names

[agents]                           # AI coding assistants
hook = "block"                     # "block" | "warn" | "off": what the agent hook does on a mismatch
bot_emails = ["*[bot]@users.noreply.github.com", "noreply@anthropic.com"]   # agent/bot authors in audit

[policy]                           # team policy
files = []                         # policy files for every repo (an identity's policy adds its own)
```

`[default]`: every key is optional.

| key | |
|---|---|
| `name`, `email` | Identity for repos no `[[identity]]` matches. Leave both out and git refuses to commit there ("Please tell me who you are"). |
| `ssh_key` | Key used for `ssh_hosts`, and for signing. |
| `ssh_hosts` | Hosts where ssh always uses `ssh_key`. |
| `sign` | `true`: sign commits and tags with SSH keys. Default `false`. |
| `gh_account`, `glab_account` | The `gh`/`glab` login that belongs to this identity. Used by `clone`, `env`, `forge` and `keys --upload`. |
| `clone_dir` | Where `gitpro clone` puts repos of this identity. |

`[[identity]]`: `id`, `name`, `email` and `host` are required.

| key | |
|---|---|
| `id` | Short name. Its settings go to `~/.gitconfig-<id>` (change with `file`). |
| `host` | Git server, e.g. `github.com`. |
| `owners` | Users, orgs or groups in the remote URL path, **exact case**. Omit to match the whole host. |
| `ssh_key` | This identity's own key. |
| `ssh_alias` | ssh `Host` that offers only `ssh_key`. Without it, if `host` is in `[default].ssh_hosts`, ssh falls back to `[default].ssh_key`, so an unregistered `ssh_key` signs you in as the default account. |
| `rewrite` | With `ssh_alias`, `git@<host>:<owner>/…` URLs go through the alias. Default `true`. |
| `rewrite_https` | Also send `https://<host>/<owner>/…` over SSH through the alias. Default `false`. |
| `ssh_user`, `ssh_port` | For a non-standard SSH user or port. Default `git`, 22. AWS CodeCommit's SSH user is the SSH key ID. |
| `sign` | Overrides `[default].sign`. Explicit `false` also disables signing inherited from an earlier matching identity. |
| `file` | Where this identity's generated settings go. Must be inside your home directory and must not overlap another identity file, gitpro's config, or its managed output files (including through symlinks). Agent change workflows also refuse Git metadata directories and `.git` entries. |
| `gh_account`, `glab_account`, `clone_dir` | As in `[default]`, for this identity. |
| `policy` | A [team policy](#teams) file this identity must satisfy. |

Trailers (`[default.trailers]`, `[identity.trailers]`):

| key | |
|---|---|
| `co_authored_by` | `"allow"` (default), `"forbid"` or `"require"` a `Co-Authored-By` trailer, for example the one AI assistants add. `gitpro audit` reports violations; with the guard installed, the `commit-msg` hook blocks them. |
| `forbid_emails` | Globs. A `Co-Authored-By` or author email that matches one is reported, and blocked by the guard. |

The optional tables:

| table | key | |
|---|---|---|
| `[scan]` | `roots` | Where your repos live. Default `["~"]`. Used by `scan` and the VS Code views. |
| | `exclude` | Globs to skip. A pattern without a leading `/` or `~/` matches at any depth. Hidden directories and `node_modules` are always skipped. |
| | `stale_days` | `gitpro scan --lost -v` calls a repo stale after this many days without a commit. Default 180. |
| `[guard]` | `hooks` | `true`: `setup` installs the global guard hooks and keeps them in sync. `false`: `gitpro setup` leaves the guard off. Unset (default): `gitpro setup` installs it and keeps an installed guard. |
| | `on_mismatch` | `"block"` (default) refuses the commit or push; `"warn"` only prints the warning. |
| | `chain_to` | A previous global `core.hooksPath` whose hooks run after gitpro's. |
| `[clone]` | `dir` | Where `gitpro clone` puts a repo when the identity has no `clone_dir`. Default: the current directory. |
| | `layout` | Folder shape under that directory. Default `{host}/{owner}/{repo}`; `{id}` is the identity id. |
| `[forge]` | `online` | `true`: commands that ask `gh`/`glab` may do so without `--online`. If `gh`/`glab` is missing, they fall back to offline with a `⚠`. Only an explicit `--online` fails. |
| | `similar` | `true`: `forge similar` may run without `--send-repo-names`. |
| `[agents]` | `hook` | What the AI-assistant hook does on a mismatch: `"block"` (default), `"warn"` or `"off"`. |
| | `bot_emails` | Author emails that `gitpro audit` counts as agents or bots. `[bot]` is matched literally. |
| `[policy]` | `files` | Policy files that apply to every repo, on top of each identity's `policy`. |

A repo matches an identity when any of its remotes matches. When several
identities match, the settings of the later one win. Any other git setting can
be added under `[default.git.<section>]` or `[identity.git.<section>]`.

The config is read from `-c PATH`, else `$GITPRO_CONFIG`, else
`~/.config/gitpro.toml`. Because it controls commands git runs, it must
be owned by you and not group/world-writable. Unknown keys are errors.

## Commands

`gitpro --help` lists these, in this order. Every command has `--help`.

| command | what it does |
|---|---|
| `setup [PATH...]` | Set up or update this machine. It learns your identities from your repos. See [setup](#setup-set-up-this-machine). |
| `why [PATH]` | Explain one repo: the identity it should use, the one it uses, why, whether a commit is blocked, and the fix. |
| `scan [PATH...]` | Check all repos. `--fix` fixes what needs no choice. `--lost` adds unpushed work. |
| `pin [PATH]` | Choose the identity of a repo. `--remote R` picks the remote to follow. `--remove` removes the pin. |
| `clone URL [DIR]` | `git clone` as the identity, SSH alias and folder the URL calls for. |
| `audit [PATH]` | Find commits made as the wrong identity or with an AI agent. `--fix` rewrites the unpushed ones. |
| `env [PATH]` | Print shell lines that set `GH_TOKEN` and friends for the repo's identity. |
| `uninstall` | Remove what gitpro wrote. |

Under "more" in `--help`:

| command | what it does |
|---|---|
| `completion SHELL` | Print a tab-completion script for bash, zsh or fish. |
| `prompt` | A short identity segment for your shell prompt. |
| `report [--crash\|--denial\|--security]` | A redacted draft of a bug report, opened as a prefilled GitHub issue form only when you say yes. |
| `team` | Does this machine meet each identity's team policy? |
| `policy show [PATH]`, `policy lint FILE` | Show the policy rules for a repo; validate a policy file. |
| `keys` | List each SSH key, its public key and where to register it. `--health` checks them; `--upload --online` registers them. |
| `auth` | Run `ssh -T` for every host and alias and show which login each one gets (online). |
| `forge repos`, `forge forks`, `forge email`, `forge similar` | Read-only views of your GitHub/GitLab accounts through `gh`/`glab` (online). |
| `guard install --repo DIR`, `guard uninstall`, `guard status`, `guard test` | Git hooks that refuse a commit or push with the wrong identity. |

A few commands are hidden because hooks, agents and the editor call them, not
you: `agent-check`, `guard check`, `mcp` (the MCP server), `agent uninstall`
and `mcp uninstall`. Commands that were replaced in 0.4.0 are hidden too; see
[Deprecated in 0.4.0, removed in 0.5.0](#deprecated-in-040-removed-in-050).

The same flags mean the same thing in every command:

| flag | meaning |
|---|---|
| `-n`, `--dry-run` | `setup`: show what would change. Change nothing. |
| `-y`, `--yes` | Do not ask. |
| `--json` | One JSON object on stdout (see [Exit codes and JSON](#exit-codes-and-json)). |
| `-v` | More detail. |
| `PATH` | The repo or folder to look at. |
| `-c PATH` | The config file. |

On a terminal, a command that changes something shows the change and asks
once. In 0.4.0 this is how `setup` works. `pin`, `scan --fix` and
`audit --fix` keep their 0.3 behavior in 0.4.x: without `-y` they show a
preview.

The terminal, the VS Code extension and the MCP server use the same commands,
options and JSON fields. The extension runs the command and asks its yes in a
dialog. An MCP tool has the command's name, runs its read-only form and
returns the fix command for you to run.

### setup: set up this machine

```
gitpro setup [PATH...] [-n] [-y] [--add] [--agent NAME] [--no-passphrase] [--json] [-c PATH]
```

`gitpro setup` is the one command for a machine, for the first run and every
run after.

| state | what setup does |
|---|---|
| no config, an empty config, or `--add` | finds your identities (below), asks what it cannot decide, then shows one preview |
| a config with identities | shows one preview only; asks no identity questions |
| `-n` | shows the state of this machine; never asks, never writes |

Finding identities needs a terminal. Without one, setup exits 2.

**Finding identities from your repos.** Your repos already know who you are.

1. *Where.* With `PATH` arguments, setup looks there. Without them it asks
   `Look through your repos to find your identities? [Y/n]`, then `Where [~]`.
   Give space-separated folders; `~` expands. Answering no skips the search.
2. *What it reads.* Everything is local and read-only:
   - every repo under those folders. Hidden directories, `node_modules` and
     the usual caches are skipped; it goes 6 levels deep and stops after 20
     seconds;
   - for each repo, the host and owner of its remote, and the name and email
     it uses. In this order: set in the repo; set by an `includeIf` file; the
     identity of the commits made in it on this machine;
   - the SSH config: aliases, their host and `IdentityFile`;
   - the `includeIf` rules in your global Git config;
   - the recent folders of VS Code and JetBrains, to find more repos.
3. *What it never reads.* A private key, your shell history, or a forge
   account.

Progress is one line on stderr, for example `looked at 104 repos in ~ (2.1 s)`.

**The commit identity of a repo.** Git adds a line to `<git dir>/logs/HEAD`
(the reflog) for every commit made in a worktree, with the committer's name
and email. A repo's commit identity is the name and email that appear most
often on the lines whose message starts with `commit` (`commit:`,
`commit (initial):`, `commit (amend):`, `commit (merge):`). Setup reads at
most the last 1 MiB of the file. Lines from `checkout`, `pull`, `reset` and
other actions are not commits and are ignored. A reflog expires after 90 days
by default, so this shows recent use.

**From what it found to identities.**

1. Repos are grouped by host, then by owner.
2. Each owner gets the email that most of its repos use, and the most frequent
   name with that email. If an owner's repos use more than one email, setup
   keeps the most used one and shows the others, for example
   `also seen: me@old.test in 2 repos`.
3. Owners on one host with the same email form one identity.
4. An owner with no email (no commits, nothing set) joins the host's identity
   when the host has exactly one. Otherwise it is listed as "not assigned" and
   left out. Setup does not guess.
5. When the owners' remotes use an SSH alias whose `IdentityFile` is one file,
   that file is the identity's SSH key. Otherwise setup asks for the key as
   before: an existing key, a new path to create, or blank.
6. With `--add`, owners that are already in the config are skipped.
7. Hosts and owners the config check rejects are listed as skipped, with the
   reason.

Then setup shows what it found and asks:

```text
Found 3 identities in 52 repos under ~:

  1  sandeepshelke   Sandeep <me@personal.test>   github.com   sandeepshelke, Yantrakar (+11)   ~/.ssh/id_personal
  2  corp            Sandeep <me@corp.test>       github.com   corp-org                         no key found
     also seen: me@personal.test in 2 corp-org repos
  3  nanoframes      Sandeep <me@nano.test>       gitlab.com   nanoframes, xplaque              ~/.ssh/id_nano

  not assigned: github.com/someone (1 repo, no commits)

Use these? [Y/n/e]  (e: go through them one by one)
```

- `y` keeps all of them. For each one without a key, setup asks the key
  question, then the signing question.
- `e` goes through the identities one by one: keep, edit (the usual questions,
  with the found values as defaults), or skip.
- `n` drops them and asks for identities by hand.

Then setup offers a fallback identity (`[default]`): `Set a fallback identity? [Y/n]`.
It defaults to your global `user.name` and `user.email`. If nothing was found
(no repos, or none with a remote), setup goes straight to the questions.

**One preview, one yes.** Setup shows one table of everything this machine
still needs, grouped:

| group | what setup does | undo |
|---|---|---|
| config | saves the TOML (an existing file is backed up first) | the backup it names |
| SSH key | creates each missing key with `ssh-keygen -t ed25519`; it asks for a passphrase (`--no-passphrase` skips it) | delete the new key |
| Git and SSH files | writes the Git and SSH files for your identities | the `setup-undo-*.sh` it prints |
| guard | installs the commit guard (see [guard](#guard-stop-a-wrong-identity-commit)); `[guard] hooks = false` keeps it off | the same script, or `gitpro guard uninstall` |
| agents | when Claude Code is installed: the agent hook and the MCP server (skipped when the gitpro plugin is enabled) | the `agent-undo-*.sh` and `mcp-undo-*.sh` it prints |

It also lists what is "Already in place" and any warnings, then asks
`Apply these changes? [y/N]`. `-y` answers yes. Ctrl-C, or no, writes nothing.
Setup never edits a repo. It ends with the public keys to register with each
account, and `gitpro scan`, which lists the repos that need a fix.
`--agent NAME` sets up another agent (repeatable); `--agent none` sets up none.

`gitpro setup --add` adds identities without rewriting existing settings or
comments. An existing config gets a backup before saving. Symlink config
files are refused: name the real path instead. The config cannot share a path
with managed Git/SSH output. Setup checks for edits made while its questions
were open and coordinates saves with other wizard sessions. If a crashed
session leaves a `.setup.lock` file, check that no setup is still saving
before removing it. Use `gitpro setup -c PATH` to save a custom file.
Otherwise `$GITPRO_CONFIG`, then `~/.config/gitpro.toml`, sets the path.

**`setup -n`: the state of this machine.** `gitpro setup -n` never asks,
never writes and needs no terminal. It prints the identities, the keys and
where to register them, and the same "Setup will:" table of what is still to
do. If nothing is to do it prints `✓ this machine is set up; nothing to change`.
It exits 0 when nothing is to do, 1 when something is, and 2 on an error.
With `--json`:

```json
{"ok": true, "command": "setup", "dry_run": true, "config": "~/.config/gitpro.toml",
 "identities": [{"id": "work", "name": "…", "email": "…", "host": "github.com",
                 "owners": ["corp-org"], "ssh_alias": "…", "ssh_key": "~/.ssh/id_work",
                 "gh_account": null, "glab_account": null, "clone_dir": null, "sign": false}],
 "default": {"name": "…", "email": "…", "gh_account": null, "glab_account": null,
             "clone_dir": null, "sign": false},
 "pending": [{"group": "SSH key", "what": "create ~/.ssh/id_work (ed25519)"}],
 "done": ["Git and SSH files, commit guard"], "notes": []}
```

`ok` is false when `pending` is not empty. With no config, `setup -n` exits 1
and `pending` holds `{"group": "config", "what": "no config yet: run gitpro setup in a terminal"}`.

**Without a terminal.**

| | no `-y`, no `-n` | `-y` | `-n` |
|---|---|---|---|
| config exists | exit 2: "no terminal to confirm on; run gitpro setup -y to apply, or gitpro setup -n to see what it would do" | applies | prints the state |
| no config | exit 2: setup asks for your identities, so it needs a terminal | exit 2, same reason | prints the state, exit 1 |

For provisioning scripts, write the config first, then run `gitpro setup -y`.
For a commented template instead of the questions, see
[Configuration](#configuration).

### why, scan, pin, clone, audit, env, uninstall

- **`why [PATH]`** is described in [why](#why-which-identity-and-how-to-fix-it).
- **`scan [PATH...]`** lists repos by the identity git uses and what needs a
  fix. `--fix` removes [repo-level overrides](#repo-level-overrides).
  `--lost` adds [work that is only on this machine](#scan---lost-work-that-is-only-on-this-machine).
  `--lost` with `--fix` is refused (exit 2): lost work needs a person.
- **`pin [PATH]`** copies a repo's identity into its `.git` directory, for
  [dev containers](#dev-containers). In a repo whose remotes match two
  identities, `pin` lists the remotes and asks which to follow; without a
  terminal it exits with an error that names `--remote`. `pin --remote R`
  pins the identity that remote R's URL matches. `pin --remove [PATH]` removes
  the pin. `--remove` with `--remote` is refused. In 0.4.x the JSON of
  `pin --remove` still says `"command": "unpin"`, for the VS Code extension.
- **`clone URL [DIR]`**: see [clone](#clone-the-right-identity-from-the-first-commit).
- **`audit [PATH]`**: see [audit](#audit-commits-made-as-the-wrong-identity-or-by-an-agent).
  `audit --fix` rewrites the unpushed commits (it was `audit --rewrite`).
- **`env [PATH]`**: see [env](#env-the-right-token-for-gh-and-glab).
- **`uninstall`**: removes what gitpro wrote. See [Undo everything](#undo-everything).

## Example: a second GitHub account

1. Run `gitpro setup`. If the repos of the second account are on this machine,
   setup finds its owners and email from them and proposes it as an identity.
   Otherwise add the identity by hand with `gitpro setup --add`, or add an
   `[[identity]]` to the config with `host = "github.com"`, the account and its
   orgs as `owners`, its own `ssh_key` and an `ssh_alias` (as in the example
   above). Setup creates the key if it is missing.
2. Run `gitpro setup -y` if you edited the config by hand, to write the Git and
   SSH files.
3. Add the public key that `gitpro keys` prints to that account under
   *Settings → SSH and GPG keys*. With `sign`, add it a second time as a
   *Signing key*.
4. Check with `ssh -T git@github-work`. GitHub replies with the account name.
5. Clone with `gitpro clone git@github.com:employer-org/app.git`, or with
   plain `git clone`.

## Daily use

### why: which identity, and how to fix it

`gitpro why [DIR]` explains which identity a repo uses, where each value
comes from, and prints the command that fixes a mismatch. `gitpro why --json`
and `gitpro --version --json` give the same data to tools. The `settings`
field of `why --json` holds the identity settings git uses in the repo.

```text
$ gitpro why
~/work/github.com/employer-org/app
  config    ~/.config/gitpro.toml (user config)
  remotes   origin git@github.com:employer-org/app.git
  expected  work-github  Jane Doe <jane.doe@employer.com>
            matched origin by pattern git@github.com:employer-org/**
  effective Jane Doe <jane@personal.dev>   [.git/config]
  ssh key   ~/.ssh/id_ed25519_work   via core.sshcommand
  signing   off
✗ user.email is set in the repo's .git/config and overrides the work-github identity
→ git -C ~/work/github.com/employer-org/app config --unset user.email        (or: gitpro scan --fix --repo ~/work/github.com/employer-org/app)
```

After the fix, the last line reads `✓ identity matches`. `why` changes
nothing; it only tells you what to run.

```
gitpro why [PATH] [--remote R] [--author EMAIL] [--committer EMAIL] [--message TEXT] [--json]
```

To ask whether the guard would block a commit, give the emails and the message
the commit would have. `--committer` defaults to the email git would use, and
`--author` to the committer. `--message` is checked against the `Co-Authored-By`
rules. `why --json` then has a `commit` object:
`{"would_block": bool, "mode": "block"|"warn"|"off", "problems": [{code, message, fix}], "reason": str|null, "fix": str|null}`.
The MCP tool `why` takes `path`, `remote`, `author_email`, `committer_email`
and `message` and returns the same JSON.

Remote URLs can carry secrets, so gitpro masks them wherever it shows a URL
or writes one to JSON. That includes `why`, `scan`,
`clone`, `env`, `check --range`, the guard and agent hook messages, the MCP
server and the VS Code log. A password or token in the user part becomes
`***`, and so does the value of a secret query parameter, such as
`access_token=`, `private_token=`, `token=` or `sig=`:

| in `.git/config` | shown as |
|---|---|
| `https://jane:ghp_abc123@github.com/org/app.git` | `https://jane:***@github.com/org/app.git` |
| `https://ghp_abc123@github.com/org/app.git` | `https://***@github.com/org/app.git` |
| `https://gitlab.com/org/app.git?private_token=glpat-abc123` | `https://gitlab.com/org/app.git?private_token=***` |
| `git@github.com:org/app.git` | `git@github.com:org/app.git` |

The masking is for display only. Matching and every git command use the real
URL, and a masked URL cannot be pasted back. `scan --lost` masks a little
differently: it writes `[REDACTED]` for the user part, and it also masks
anything that looks like a GitHub or GitLab token in its warnings. Better still, keep tokens out of
remote URLs altogether and use `gh`, `glab` or a credential helper.
`core.sshCommand` values are shown exactly as git has them, not redacted: if
you put a secret inside your own `sshCommand`, `why` and the MCP
server show it too.

### prompt: your identity in the shell prompt

`gitpro prompt` prints `✓ work-github` or `✗ wrong@email` for your shell
prompt, within 80 ms. Other problems show as `⚠ work-github`; a repo with no
identity at all shows `⚠ no identity`. Outside a repo it prints nothing.
`gitpro prompt --install-help` shows the shell lines:

```text
$ gitpro prompt --install-help
bash:     PS1='$(gitpro prompt) '"$PS1"
zsh:      autoload -Uz add-zsh-hook; _gitpro_prompt() { psvar[1]=$(gitpro prompt); }; add-zsh-hook precmd _gitpro_prompt; PS1='%1v '"$PS1"
fish:     function fish_prompt; gitpro prompt; echo -n ' '; prompt_pwd; echo -n ' > '; end
starship: [custom.gitpro] command = "gitpro prompt" when = "git rev-parse --git-dir" shell = ["sh"]
```

`gitpro prompt --format` picks what to show (`{sym} {id} {email} {key}
{state}`), and `gitpro prompt --max-ms` how long it may take before it prints
the last cached value instead. The cache is kept per worktree.

### completion: tab completion

`eval "$(gitpro completion bash)"` (or `zsh`, `fish`) adds tab completion.
Put that line in `~/.bashrc` (or `~/.zshrc`); for fish, run
`gitpro completion fish > ~/.config/fish/completions/gitpro.fish`. It completes
commands, options and your identity ids. Regenerate it after upgrading.

### guard: stop a wrong-identity commit

`gitpro setup` adds global `pre-commit`/`pre-push` hooks that
refuse a commit or push whose committer email is not the identity the
remote belongs to, then run the repo's own hooks (every hook is
forwarded, including husky-style `post-checkout`). If gitpro itself
fails, the commit goes through. Skip the check once with `GITPRO_SKIP=1`.
`[guard] on_mismatch = "warn"` turns the block into a warning.

For pushes, the guard checks the actual destination URL Git gives the hook,
including `remote.<name>.pushurl` and URL rewrites, rather than the fetch URL.
Commit identity selection still uses the configured fetch URLs.

`gitpro guard install` without `--repo` is deprecated; `gitpro setup` installs
the same files. In 0.3.4 it printed:

```text
$ gitpro guard install -y
  create  ~/.config/gitpro/hooks/pre-commit
  create  ~/.config/gitpro/hooks/pre-push
  create  ~/.config/gitpro/hooks/commit-msg
  create  ~/.config/gitpro/hooks/applypatch-msg … (17 pass-through hooks)
  create  ~/.config/gitpro/hooks/README
  create  ~/.gitconfig-gitpro-guard
  update  ~/.gitconfig-identities        (+ include ~/.gitconfig-gitpro-guard)
✓ guard installed; test it: gitpro guard test
↩ undo: gitpro guard uninstall   (or: sh ~/.local/state/gitpro/guard-undo-20261002-092448.sh)
→ 'gitpro apply' keeps these hooks; gitpro guard uninstall removes them (or set [guard] hooks = true to make the TOML the source of truth)
```

A blocked commit looks like this:

```text
$ git commit -m "Fix typo"
✗ gitpro: this commit would be committed as jane@personal.dev but origin (git@github.com:employer-org/app.git) belongs to identity work-github <jane.doe@employer.com>
  cause: user.email is set in the repo's .git/config and overrides the work-github identity
  fix:   git -C ~/work/github.com/employer-org/app config --unset user.email
  skip once: GITPRO_SKIP=1 git commit ...
```

`gitpro guard test` runs the same check without committing (it exits 3 when
the hook would block). `gitpro guard status` shows what is installed:

```text
$ gitpro guard status
guard: installed (global, ~/.config/gitpro/hooks)  on_mismatch = block
  pre-commit ✓   pre-push ✓   commit-msg ✓ (trailers: installed; policy: installed)
  17 pass-through hooks ✓   (not forwarded: push-to-checkout, reference-transaction, post-index-change, fsmonitor-watchman, p4-*)
  chain_to: (none)
  gitpro on PATH: ~/.local/bin/gitpro
system core.hooksPath: (none)
```

The `commit-msg` hook also applies your [trailer policy](#trailer-policy) and
any [team policy](#teams). `push-to-checkout` is never forwarded, because
installing it would break `receive.denyCurrentBranch = updateInstead`. If you
already had a global `core.hooksPath`, set `[guard] chain_to` to it and its
hooks run after gitpro's.

Repos that set their own `core.hooksPath` (husky, lefthook, pre-commit) are
not covered by the global hooks: run `gitpro guard install --repo <dir>` for
an untracked hook file, or add these lines near the top of your tracked
hook yourself:

```sh
if [ -z "${GITPRO_SKIP:-}" ] && command -v gitpro >/dev/null 2>&1; then
  rc=0; gitpro guard check pre-commit "$@" || rc=$?
  if [ "$rc" -eq 3 ]; then exit 1; fi
fi
```

`gitpro guard status --repos` lists the repos with their own
`core.hooksPath`. The hooks block only on exit code 3; any other failure of
gitpro prints `⚠ gitpro guard: exited <rc>; not blocking` and lets git carry
on. If you see that line, run `gitpro why` in the repo and `gitpro setup -n`.

### clone: the right identity from the first commit

`gitpro clone <url>` picks the identity from the URL, rewrites it to the
identity's ssh alias when the identity has one, clones into
`clone_dir/<host>/<owner>/<repo>` and checks the result. An https URL
stays https unless you pass `--ssh`. Set `clone_dir` per identity or
`[clone] dir`; change the folder shape with `[clone] layout`.
`gitpro clone --dry-run` shows the plan.

```text
$ gitpro clone --dry-run git@github.com:employer-org/infra.git
clone git@github-work:employer-org/infra.git
  into      ~/work/github.com/employer-org/infra
  identity  work-github  Jane Doe <jane.doe@employer.com>   (owner employer-org on github.com)
  ssh       alias github-work → github.com, key ~/.ssh/id_ed25519_work
→ gitpro clone git@github.com:employer-org/infra.git -y
```

The URL can also be `host/owner/repo`, or `owner/repo` together with
`gitpro clone --identity ID`. `--no-rewrite` keeps the URL as given.
Arguments after `--` go to `git clone`. gitpro refuses to clone into a
directory that already exists, even an empty one. With no `clone_dir` and no
`[clone] dir`, the repo goes under the current directory.

### env: the right token for gh and glab

`eval "$(gitpro env)"` sets `GH_HOST` and `GH_TOKEN` (or
`GH_ENTERPRISE_TOKEN`, `GITLAB_TOKEN`) via `gh auth token -u <account>`
for the identity of the current repo. gitpro prints the command that
fetches the token, never the token. Add `--only ssh` for
`GIT_SSH_COMMAND`, `--only author` for author variables. `gitpro env
--unset` reverses it; `--shell fish|pwsh` for other shells. With
direnv: `echo 'eval "$(gitpro env)"' >> .envrc`.

```text
$ gitpro env
# gitpro env for ~/work/github.com/employer-org/app: identity work-github <jane.doe@employer.com> (origin git@github.com:employer-org/app.git)
export GH_HOST='github.com'
export GH_TOKEN="$(gh auth token -u 'janedoe-employer' -h 'github.com')"
# repo .git/config overrides the identity; these env vars win over it
export GIT_AUTHOR_NAME='Jane Doe'
export GIT_AUTHOR_EMAIL='jane.doe@employer.com'
export GIT_COMMITTER_NAME='Jane Doe'
export GIT_COMMITTER_EMAIL='jane.doe@employer.com'
# ⚠ user.email is set in the repo's .git/config and overrides the work-github identity
# usage: eval "$(gitpro env)"      undo: eval "$(gitpro env --unset)"
# direnv: echo 'eval "$(gitpro env)"' >> .envrc && direnv allow
```

The `GIT_AUTHOR_*`/`GIT_COMMITTER_*` lines appear only when the repo's own
config overrides the identity, as here. `gitpro env --check` runs the token
command once to see that it works.

## Repo-level overrides

A `user.name` or `user.email` in a repo's own `.git/config` wins over gitpro,
so an old one keeps committing with an old address. `gitpro scan` lists them,
grouped by the identity they'd use without the override:

```text
$ gitpro scan
3 repos under ~/src, ~/work  hidden directories and node_modules skipped

repos  identity                      from
    2  Jane Doe <jane@personal.dev>  [default]
    1  see below                     set in the repo

1 repo set their own user.name/user.email, which beats gitpro. Without it they use:

Jane Doe <jane.doe@employer.com>  (1 repo)
└── from Jane Doe <jane@personal.dev>:  ~/work/app

to keep some of these identities instead: gitpro scan --suggest

dry run, nothing changed. Remove 1 setting from 1 repo: gitpro scan --fix
```

`gitpro scan` changes nothing. `gitpro scan --fix` removes the overrides. If
gitpro was installed with the `tui` extra
(`pipx install 'gitpro[tui]'`),
it opens a list where you tick the repos to fix. Without the extra, or with
`--plain`, it asks y/N. Either way it saves an undo script under
`~/.local/state/gitpro/` and prints the command to run it. It keeps any
override whose removal would leave the repo with no identity.

To keep a repo's current identity instead, add a gitpro identity for it before
fixing: `gitpro setup --add PATH` learns it from the repos under `PATH` and
shows it before saving. (The sample above is 0.3.4 output, where `scan
--suggest` printed `[[identity]]` blocks; `scan --suggest` is deprecated.) An
identity applies to every repo of its owners.

`scan` also reports a repo whose remotes match two identities (`ambiguous`;
commits there are blocked until you choose with `gitpro pin --remote R`) and a
repo with its own `core.hooksPath`, where the guard does not run. Name
directories to scan only the repos under them (`gitpro scan ~/work`),
or `gitpro scan --repo PATH` for exactly one repo. `-v` adds the remote
owners behind each identity. `--json` is for scripts and the
VS Code extension.

## Dev containers

VS Code Dev Containers copy `~/.gitconfig` into the container, but not the
files it includes, so no identity applies inside. The same happens when a
container mounts only `~/.gitconfig`. Fix it per repo, on the host:

```sh
gitpro pin -y # in the repo, after reviewing the preview
```

This writes the repo's current name and email to `.git/git-identities` and
includes it from `.git/config`. The container sees both because they are part
of the mounted repo. Run `pin` again after you change your config. Commit
signing and the choice of SSH key are not pinned.

Re-pinning validates the replacement identity before changing the pin. If it
cannot resolve a gitpro-managed identity or write the replacement, the existing
pin is kept; a failed re-pin does not unpin the repo. Symlinked pin files are
refused rather than writing through them.
If adding the include fails, rollback restores the old pin's exact bytes and
permissions.

`gitpro pin --remove` removes the pin. In VS Code, "Set up devcontainer pin"
makes the container do this on every start; see
[devcontainer](#devcontainer-keep-the-identity-in-a-container).

## Finding things

### Scan roots

Put your repo locations in the config once:

```toml
[scan]
roots = ["~", "/mnt/d/work"]
exclude = ["~/.cache", "~/go/pkg/mod"]
```

`gitpro scan` and the VS Code view use them. `gitpro scan
--roots` shows what is in effect. Exclude patterns match absolute
directories; a pattern without a leading `/` or `~/` matches at any depth.

```text
$ gitpro scan --roots
roots (from ~/.config/gitpro.toml):
  ~/src
  ~/work
exclude:
  **/node_modules  (always)
  **/.*  (always: hidden directories)
```

Directories named on the command line (`gitpro scan ~/work`) replace the
roots for that run.

### scan --lost: work that is only on this machine

`gitpro scan --lost` adds the offline checks for work that exists only on this
machine, for the same repos: unpushed branches, branches whose upstream is
gone, stashes, dirty trees, repos with no remote, diverged duplicate clones
and (with `-v`) stale and empty repos. They are shown after scan's sections.
With `--json`, scan has a `lost` object. It never deletes anything and never
prints a command that would. `--lost` with `--fix` is refused (exit 2): lost
work needs a person. The deprecated `gitpro lost` also has `--online`, which
asks GitHub/GitLab via `gh`/`glab` whether each remote still exists, and
`--only`, `--repo` and `--stale-days`.

The lost sections look like this:

```text
$ gitpro scan --lost
3 repos under ~/src, ~/work  (remote state as of last fetch)

unpushed (3 branches in 2 repos)
  ~/src/dotfiles                      master          1 commit, no upstream   → git -C ~/src/dotfiles push -u origin master
  ~/work/github.com/employer-org/app  feature/export  3 commits, no upstream   → git -C ~/work/github.com/employer-org/app push -u origin feature/export
  ~/work/github.com/employer-org/app  main            2 ahead of origin/main
stashes (1 in 1 repo)
  ~/work/github.com/employer-org/app  stash@{0} 2026-09-30  WIP on master: c2991c7 Add retry to API client
dirty (1 repo)
  ~/src/dotfiles  1 modified, 0 untracked
remote gone: run with --online to check remotes via gh/glab

5 items in 2 repos
```

It exits 1 when it found something, so you can run it before wiping or
moving a machine. `gitpro scan --repo PATH --lost` checks one repo. With the
deprecated `gitpro lost --online`, the first line says what is sent:

```text
$ gitpro lost --online
online: asking github.com via gh (account janedoe, janedoe-employer) whether 2 repos exist; sends owner/repo names only
```

If an account is not logged in, `lost --online` warns, marks those repos unknown and
carries on.

`scan --lost` is read-only and stays offline: git runs with `protocol.allow=never`
and without lazy fetches, so it never downloads objects of a partial clone
(on git older than 2.44 such a branch shows "could not compare"). It runs no
Git LFS filters while it checks for changes, but other clean filters
configured in a repo can still run. The deprecated `gitpro lost --offline` stays offline
even when `[forge] online = true`.

"Unpushed" means not reachable from your remote-tracking refs
(`refs/remotes/*`) as of your last fetch. Run `git fetch` first for a fresh
answer.

### forge: your GitHub and GitLab accounts

`gitpro forge repos|forks|email|similar --online` read your accounts
through `gh`/`glab`: repos you have not cloned, forks whose upstream moved,
whether each identity's email is verified. Every call is read-only and the
first line says what is sent. `forge similar` additionally needs
`--send-repo-names` on every run because it sends your local repo names
as searches; it shows the list and asks first.

Without `--online` (or `[forge] online = true`) a forge command tells you what
it would send and stops:

```text
$ gitpro forge repos
✗ forge repos needs the network: it asks github.com via gh (account janedoe, janedoe-employer) for the repo lists of these accounts; sends nothing else
→ re-run with --online, or set [forge] online = true
```

`gitpro forge repos --online --not-cloned` lists only the repos with no clone
under your scan roots. If one account fails (not logged in, or glab is logged
in as someone else), gitpro warns and carries on with the others. glab keeps
one login per host, so two GitLab identities on the same host cannot both be
checked. `forge similar` searches GitHub only.

## Keys

`gitpro keys` lists each key, its public key and the accounts to register it
with. `gitpro setup` creates the missing keys (`keys --generate` is deprecated). `gitpro keys --health` checks each key: type and size, passphrase, file
modes, ssh-agent (a passphrase key that is not loaded will prompt on
every push), and (with `--online`) whether it is registered on the right
account via `gh`/`glab`.

```text
$ gitpro keys --health
· no ssh-agent: a key with a passphrase prompts on every use
~/.ssh/id_ed25519       ed25519  auth  →  github.com (default)
  ⚠ no passphrase → ssh-keygen -p -f ~/.ssh/id_ed25519
  ✓ perms
~/.ssh/id_ed25519_work  ed25519  auth  →  github.com: janedoe-employer, employer-org [work-github]
  ⚠ no passphrase → ssh-keygen -p -f ~/.ssh/id_ed25519_work
  ✓ perms
registered: run with --online to check via gh/glab
0 keys fine, 2 warnings, 0 problems
```

gitpro never opens your private keys. It asks `ssh-keygen` whether a key has
a passphrase and discards that command's output.

`gitpro auth` runs `ssh -T` for every host and alias and shows which login
each one gets. It goes online, and its first line says so:

```text
$ gitpro auth
online: connecting with ssh to github-work (ssh -T); sends your public key, nothing else
```

`gitpro keys --upload --online` registers missing public keys on the mapped
account (the identity's `gh_account`/`glab_account`). It shows each key and
asks before adding it; `gitpro keys --upload --title T` sets the key title.
Without `--online` it explains what it would send:

```text
$ gitpro keys --upload
✗ keys --upload needs the network: it asks github.com via gh (account janedoe, janedoe-employer) which keys are registered, then adds the missing public keys; sends your public keys, after you confirm each one
→ re-run with --online, or set [forge] online = true
```

## AI assistants

gitpro treats AI coding assistants as users that need the same guardrails as
you, and never lets them write anything without your yes.

### Agent hook

`gitpro setup` adds a Claude Code `PreToolUse` hook when Claude Code is
installed (plugin users have it already). Use `gitpro setup --agent NAME` for
another agent, and `gitpro setup -n` to see which hooks are in place. Before
the agent runs `git commit`, `git push`, `gh pr create` and similar,
`gitpro agent-check` verifies the repo's identity and, on a mismatch, denies
the call with a reason that asks the agent to check with you. It never grants
anything. `[agents] hook = "warn"` only prints a warning. Cursor and Gemini
adapters are experimental, and setup installs them only when you name them with
`--agent`. `agent install` and `agent status` are deprecated; `agent uninstall`
(hidden) removes the hook.

When the agent tries to commit as the wrong person, Claude Code shows it this
reason and the agent stops to ask you:

```text
gitpro: this commit would be committed as jane@personal.dev but origin (git@github.com:employer-org/app.git) belongs to identity work-github <jane.doe@employer.com>. cause: user.email is set in the repo's .git/config and overrides the work-github identity. Ask the user before changing anything; the fix is `git -C ~/work/github.com/employer-org/app config --unset user.email`.
```

To turn the check off, set `GITPRO_SKIP=1` in the shell that starts the agent, or
`[agents] hook = "warn"` or `"off"`. The deny reason never mentions the
bypass: the agent is told to ask you. `gitpro uninstall` leaves agent hooks in
place; `gitpro agent uninstall` removes them. The hook judges `git commit`,
`git push`, `git merge`/`rebase`/`cherry-pick`/`am`
including `--continue`, `gh pr create` and the like; `--abort`, `--skip`,
`--quit`, dry runs and `--help` pass. Repo aliases are looked up read-only
and `!` shell aliases are never run. When the command's directory is not
known (`cd "$DIR"`), the hook judges the nearest directory it can know.
`gitpro agent install --scope project` (deprecated) puts the hook in
`<repo>/.claude/settings.local.json` instead of your user settings.

The plugin's hook runs `gitpro` from a plain `sh`, so gitpro must be on the
`PATH` that `sh` sees (pipx puts it in `~/.local/bin`). Without it, the hook
does nothing.

### MCP server

`gitpro mcp` is a stdio MCP server with read-only tools by default. Its tools
have the names of the commands and return the same JSON as the matching
`--json` command. A `fix` field is a command to show you, not to run.

| tool | what it returns |
|---|---|
| `why` | `why --json`. Takes `path`, `remote`, `author_email`, `committer_email` and `message`. The `settings` field holds the identity settings git uses; the `commit` field says whether the guard would block the commit. |
| `setup` | the state of the machine, as `setup -n --json`: identities, what is still to do |
| `scan` | `scan --json`. With `lost: true` it adds the lost-work checks. |
| `audit` | the same JSON as `audit --json` |
| `env` | the shell lines for the repo's identity: the commands that fetch a token, never a token |
| `report` | a redacted bug report draft |

Register it with `gitpro setup --agent claude|cursor|gemini|opencode`, or by
installing the gitpro plugin. `gitpro mcp uninstall` (hidden) removes the
registration. Project-level `.mcp.json` is a team file: gitpro prints what to
add and does not edit it unasked.

Deprecated tools stay in the tool list in 0.4.x and are removed in 0.5.0; see
[Deprecated in 0.4.0, removed in 0.5.0](#deprecated-in-040-removed-in-050) for
each tool and its replacement.

`gitpro mcp --log FILE` records every request and response (mode 0600) when
you need to see what the assistant asked. The tools of the deprecated agent
operation protocol are described in
[agent workflows](agent-workflows.md#deprecated-the-operation-protocol).

### env for agents

An agent that runs `gh` or `glab` should use the account of the repo it works
in. Start it from a shell where `eval "$(gitpro env)"` ran, or use direnv
(see [env](#env-the-right-token-for-gh-and-glab)). gitpro hands out the
command that fetches the token, never the token.

### devcontainer: keep the identity in a container

Containers do not see the host's gitpro files. In VS Code, "Set up devcontainer pin"
writes the entry for you. The command `gitpro devcontainer` (deprecated) prints the `initializeCommand` entry that runs `gitpro pin` on the host
before every container start (and does nothing on hosts without gitpro).
`devcontainer.json` is shared with your team, so gitpro only prints it;
`--write` edits it with a backup and undo. `gitpro devcontainer status`
shows the pin state.

```text
$ gitpro devcontainer
~/work/github.com/employer-org/app/.devcontainer/devcontainer.json  (not committed: only on this machine)
add this key:
  "initializeCommand": "command true || exit 0\n# runs 'gitpro pin' on hosts that have gitpro, else nothing (line 1 stops Windows cmd)\ncommand -v gitpro >/dev/null 2>&1 || exit 0\nIFS= read -r dir <<'GITPRO_PATH'; gitpro pin -y \"$dir\" || true; exit 0\n${localWorkspaceFolder}\nGITPRO_PATH"
→ gitpro does not edit it by default; to add it: gitpro devcontainer --write ~/work/github.com/employer-org/app
→ in the container, 'gitpro which' shows the pinned identity; signing and ssh keys are not pinned (see 'gitpro pin')
· initializeCommand runs on the host before the container starts. On Linux, macOS and WSL this line runs gitpro pin when gitpro is installed and does nothing otherwise; on Windows (cmd.exe) its first line exits 0, so a colleague without gitpro is not affected and the container still starts (the build log then shows "'command' is not recognized as an internal or external command", which is harmless).
```

The entry is one multi-line string, not an array: the Dev Containers CLI runs
a string through `/bin/sh -c` (or `cmd /c` on Windows), and the array form
fails to start on Windows. The workspace path is read on its own line, so a
path with unusual characters cannot run commands. If your file already has an
`initializeCommand`, `--write` keeps it and runs it alongside `gitpro pin`
(in parallel), and says so. `--write` refuses a file with comments (they
would be lost), shows a diff, keeps a backup and prints the undo command.

The Windows behaviour above is reasoned from how `cmd.exe` and the Dev
Containers CLI work. It has not been tested on a Windows machine.

```text
$ gitpro devcontainer status
~/work/github.com/employer-org/app
  devcontainer.json  none (gitpro devcontainer prints the line once the repo has one)
  pin                not pinned → gitpro pin ~/work/github.com/employer-org/app
  inside container   no
```

### audit: commits made as the wrong identity or by an agent

`gitpro audit` lists commits whose committer (or your own other email as
author) is not the repo's identity: unpushed ones get `gitpro audit
--fix`, which fixes only those commits, keeps every date and never
runs `rebase` (the commits above them get new ids but are otherwise
unchanged); pushed ones are reported only. It also lists commits made with an AI
agent (`Co-Authored-By` bot trailers). Set `co_authored_by = "forbid" |
"require"` under `[identity.trailers]` to enforce a policy; with the
guard installed the `commit-msg` hook applies it at commit time.

```text
$ gitpro audit
~/work/github.com/employer-org/app  identity work-github <jane.doe@employer.com>  3 commits (1 pushed, 2 unpushed, as of last fetch)

wrong author (2 unpushed, 0 pushed)
  c2991c7  2026-09-30  jane@personal.dev        Add retry to API client          unpushed
  9bc2d19  2026-09-30  jane@personal.dev        Fix login redirect               unpushed
→ rewrite the 2 unpushed: gitpro audit --rewrite ~/work/github.com/employer-org/app

agent commits (1)
  Co-Authored-By: Claude <noreply@anthropic.com>           1 commit
  c2991c7  2026-09-30  jane@personal.dev Add retry to API client      Co-Authored-By: Claude <noreply@anthropic.com>
```

`gitpro audit --fix` (the deprecated name is `--rewrite`) shows the plan, asks
(unless you pass `-y`), and leaves an undo script. The samples on this page
are 0.3.4 output, where the option was `--rewrite`. In another repo:

```text
$ gitpro audit --rewrite -y
…
→ rewrite 3 unpushed commits on main (base the root):
  48dd877  Initial commit                   author, committer jane@personal.dev → jane.doe@employer.com
  50df5d1  Add CLI flags                    author, committer jane@personal.dev → jane.doe@employer.com
  a473d40  Fix docs link                    author, committer jane@personal.dev → jane.doe@employer.com
  then: git -C ~/work/github.com/employer-org/tools update-ref -m 'gitpro audit --rewrite' refs/heads/main <new tip> a473d402465f
✓ rewrote 3 commits (3 new shas: 3 fixed, 0 descendants re-parented)
↩ undo: sh ~/.local/state/gitpro/audit-undo-20261002-093136.sh
```

It needs a clean working tree, refuses merge commits and anything pushed to
any remote, and moves the branch in one guarded step. With
`commit.gpgsign = true` the new commits are signed; colleagues' commits keep
their exact bytes. By default `audit` checks every unpushed commit plus the
newest 1000 pushed ones; `gitpro audit --all` checks everything,
`gitpro audit --range A..B` a range.

"Pushed" means reachable from a remote-tracking ref (`refs/remotes/*`) as of
your last fetch. A commit published only through a tag, `git push <url>` or
Gerrit's `refs/for/*` counts as unpushed, so fetch before you rewrite.

### Trailer policy

`[default.trailers]` and `[identity.trailers]` say whether commits may carry a
`Co-Authored-By` trailer, the line AI assistants add:

```toml
[identity.trailers]
co_authored_by = "forbid"                         # or "require", or "allow" (default)
forbid_emails = ["*@users.noreply.github.com"]    # never allowed as co-author or author
```

`gitpro audit` reports violations in history. With the guard installed, the
`commit-msg` hook blocks a new commit that breaks the policy. Only your own
commits are judged, and merges are exempt from `require`. Unlike the identity
block, the trailer block prints no skip hint; `GITPRO_SKIP=1` in your own
shell still skips it.

## Teams

Teams write their rules once in a policy TOML: allowed email domains per
owner, whether commits must be signed, and trailer rules. Point an identity's
`policy` at it. Then:

- `gitpro check --repo .` (in a repo) or `gitpro check --policy FILE` (every
  identity) reports what this machine breaks.
- `gitpro why` shows a repo's policy problems next to its identity problems.
- The guard blocks commits that break the policy.
- `gitpro check --policy FILE --range base..head` checks a pull request in CI.
  The policy file is read from the PR's base, so protect it with CODEOWNERS.
- `gitpro team` is the one-screen summary. It ends with the
  `gitpro check --policy …` command that shows the details.

Plain `gitpro check` (deprecated; use `gitpro setup -n`) only says whether
`gitpro apply` would change anything, and never looks at policies.

### The policy file

```toml
# gitpro policy, published by the org; users point an identity's policy at it
[policy]
name = "Employer engineering"
version = 1
contact = "it-sec@employer.com"            # shown in every failure message
allowed_signers = "allowed_signers"        # optional: ssh allowed_signers file, relative to this file

[[rule]]
host = "github.com"
owners = ["employer-org", "employer-org-*"]   # globs; empty = every owner on the host
allowed_email_domains = ["employer.com"]
allowed_emails = []                           # exceptions: full addresses or globs
require_signing = true                        # commits must be signed
signing_formats = ["ssh"]                     # "ssh" | "gpg" | "x509"; empty = any
co_authored_by = "forbid"                     # allow | forbid | require
forbid_emails = ["*@users.noreply.github.com"]   # trailer/author globs that fail
require_verified_email = false                # the forge email must be verified (checked by gitpro team --online)
```

Every matching rule applies. A repo gets the policies in `[policy] files`, plus
the one its identity names. Owner globs work like git's: `*` stays within one
path segment, `**` crosses segments, and a rule for `employer-org` also covers
its GitLab subgroups. On your machine, a policy file must be owned by you and
not group- or world-writable (0600 and 0644 are fine). `gitpro policy lint
FILE` validates a file and lists every error:

```text
$ gitpro policy lint ~/work/gitpro-policy.toml
✓ ~/work/gitpro-policy.toml: policy Employer engineering, 1 rule
```

`gitpro policy show` lists the rules for the repo you are in:

```text
$ gitpro policy show
policy Employer engineering (~/work/gitpro-policy.toml), contact it-sec@employer.com
  rule 1: github.com/employer-org, employer-org-*
    email domains   employer.com
    signing         required (ssh)
    co_authored_by  forbid
```

### Check this machine

`gitpro check --repo .` checks your identity for this repo against its
policies. `gitpro check --policy FILE` checks every identity that uses that
policy, from any directory:

```text
$ gitpro check --repo .
policy Employer engineering (~/work/gitpro-policy.toml) for work-github
  email domain   jane.doe@employer.com ✓ employer.com
  signing        required           ✗ identity work-github has sign = false → set sign = true, run gitpro apply
  signing key    ~/.ssh/id_ed25519_work.pub ⚠ not checked (run gitpro team --online to verify it is registered)
  trailers       forbid             ⚠ policy forbids Co-Authored-By but the identity allows it → set [identity.trailers] co_authored_by = "forbid"
1 problem
```

Plain `gitpro check` (deprecated) in the same repo still prints `up to date: nothing to
change`, because `apply` has nothing to do. `gitpro why` shows the policy problems too:

```text
$ gitpro why
…
✗ policy Employer engineering requires signed commits but identity work-github has sign = false
→ set sign = true, run gitpro apply
```

With the guard installed, a commit that breaks the policy is blocked. The
message names the policy and its contact.

### Check a pull request in CI

`gitpro check --policy FILE --range A..B` checks every commit in the range:
the author and committer emails, the signature and the trailers. You can give
`--policy` more than once. Without it, gitpro uses the policies the config
names for the repo, or else `.gitpro-policy.toml` in the repo root. CI needs
no gitpro config: rules are matched on the `origin` URL alone.

Commit subjects cannot hide records from policy checks. Missing or malformed
commit records stop the check with an error rather than being skipped.

**The base judges the range.** A policy file inside the repo is read as it is
at the range's base commit, never from the checkout. That covers
`.gitpro-policy.toml`, a `--policy` path inside the repo, and the
`allowed_signers` file such a policy names. So a pull request cannot loosen
the policy that judges it. Symlinks introduced in the checkout cannot redirect
the configured policy to another base-tree path, including when the checkout
is accessed through an external symlink alias. A PR cannot add `gmail.com` or
list its own key, for example. Here the last commit tries that:

```text
$ gitpro check --range origin/main..HEAD
⚠ this range changes .gitpro-policy.toml; it is judged by the base version (24804e0)
policy Employer engineering for github.com/employer-org/app, 4 commits in origin/main..HEAD
  de5afc7  2026-10-02  pat@gmail.com         ✗ email domain gmail.com not allowed (employer.com)
  d36cac2  2026-10-01  jane.doe@employer.com ✗ Co-Authored-By: Claude <noreply@anthropic.com> is not allowed for policy Employer engineering ([[rule]] co_authored_by = "forbid")
  58492f5  2026-10-01  pat@gmail.com         ✗ email domain gmail.com not allowed (employer.com)
  1b7cfd0  2026-09-30  jane.doe@employer.com ✓
3 of 4 commits violate the policy; contact it-sec@employer.com
```

A policy change takes effect once it is merged:

- A policy that the pull request adds is not enforced until it is merged. The
  check says `⚠ .gitpro-policy.toml: policy added in this range; not enforced
  until merged`, then `no policy enforced for N commits`. It exits 0 unless
  another policy applies.
- A change to the policy made in a merge commit counts as a change too.
- The warnings go to stderr, and to `warnings` in the JSON.

The base must be a commit of a protected branch, because its policy is the one
that judges. The GitHub Action's default range (`base.sha..head.sha` of the
pull request) meets that. A custom range whose base the PR author can choose
does not. A range with no single base commit (`HEAD`, or `A B ^C`) is an error
when the policy lives in the repo:

```text
$ gitpro check --range HEAD
✗ the range HEAD has no single base commit to read .gitpro-policy.toml from; use BASE..HEAD, or pass --policy FILE from outside the repo
```

A `--policy` file outside the repo is read as it is on disk.

Protect the policy file and `allowed_signers` with CODEOWNERS. Then add a
branch protection rule that requires code owner review, so a change to them
is reviewed before it starts to judge:

```text
# .github/CODEOWNERS (GitHub) or CODEOWNERS (GitLab)
/.gitpro-policy.toml   @employer-org/security
/allowed_signers       @employer-org/security
```

**Signatures.** A policy that ships `allowed_signers` checks each ssh
signature against that file, using the base version in `--range` mode. git
does not tie a signing key to the committer email. A key listed for `alice@`
also verifies a commit that claims to be from `bob@`. So gitpro checks the
committer email against the principals listed for the signing key:

- The principals come from every `allowed_signers` line whose key fingerprint
  matches the signature's key.
- OpenSSH pattern-list rules apply. `*` and `?` are globs, and a matching
  `!pattern` denies.
- A mismatch is a violation: `signed by <principals> (allowed_signers), but
  committed as <email>`.
- A signature by a key that is not in the file is a violation when the policy
  requires signing.
- When the policy ships no `allowed_signers`, a signature that cannot be
  checked here (an unknown ssh key, a missing gpg key) is accepted with a `⚠`.
  The summary counts these, and so does `unverifiable` in the JSON.
- gpg and x509 signatures are checked against the runner's keyring only, and
  their signer is not compared with the committer.

`gitpro check` exits 1 on any violation, 0 when the range is clean and 2 on
errors.

**GitHub Actions.** This repo is also a GitHub Action. It installs gitpro from
its own checkout into a virtual environment under `$RUNNER_TEMP`, then checks
the pull request's commits:

```yaml
name: gitpro policy
on: pull_request
jobs:
  policy:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write        # only with comment: "true"
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # the whole history; a shallow clone is refused
      - uses: Yantrakar/gitpro@<ref>
        with:
          policy: .gitpro-policy.toml
          # comment: "true"       # post the violations as a PR comment
```

| input | default | what it does |
|---|---|---|
| `policy` | `.gitpro-policy.toml` | The policy file. Inside the repo, it is read from the range base. |
| `range` | `base.sha..head.sha` of the pull request | The commits to check. Keep the base on a protected branch. |
| `comment` | `"false"` | `"true"` posts the violations as a PR comment (needs `pull-requests: write`). The comment is skipped on fork PRs, whose token is read-only; the check still fails the job. |
| `working-directory` | `.` | The checkout to check, when it is not the workspace root. |

Replace `<ref>` with a tag or commit. The Action stops with an error on a
shallow clone, and on events other than `pull_request` unless you pass
`range`. The JSON report is written to `$RUNNER_TEMP/gitpro-policy.json` for
later steps.

**GitLab CI.** There is no packaged job, but the one below does the same work.
It has not been run on GitLab. gitpro refuses to run as root, and most CI
images run as root, so the job runs gitpro as an ordinary user:

```yaml
gitpro-policy:
  image: python:3.12
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  variables:
    GIT_DEPTH: "0"                # the whole history; a shallow clone is refused
  script:
    - git fetch --quiet origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
    - useradd --create-home ci && chown -R ci "$CI_PROJECT_DIR"
    - su ci -c 'python3 -m venv ~/gitpro && ~/gitpro/bin/pip install --quiet "git+https://github.com/Yantrakar/gitpro.git@<ref>"'
    - su ci -s /bin/bash -c 'set -o pipefail; ~/gitpro/bin/gitpro check --policy .gitpro-policy.toml --range "${CI_MERGE_REQUEST_TARGET_BRANCH_SHA:-$CI_MERGE_REQUEST_DIFF_BASE_SHA}..$CI_COMMIT_SHA" --json | tee gitpro-policy.json'
  artifacts:
    when: always
    paths: [gitpro-policy.json]
```

Replace `<ref>` with a tag or commit. Both base variables point at a commit of
the protected target branch:

- `CI_MERGE_REQUEST_TARGET_BRANCH_SHA` is the target branch's tip. GitLab sets
  it only in merged results pipelines. The snippet prefers it, so the current
  target-branch policy judges.
- `CI_MERGE_REQUEST_DIFF_BASE_SHA` is the merge request's fork point, and is
  always set. With it, the policy as it was at the fork point judges, so a
  policy tightened on the target branch since then is not applied.

Protect `.gitpro-policy.toml` with a CODEOWNERS entry, and turn on "require
code owner approval" on the protected branch.

### team: one-screen summary

```text
$ gitpro team
team status (this machine)
  identity     policy                       email  signing  key registered  verified      trailers
  work-github  Employer engineering         ✓      ✗        ⚠ (--online)    ·             ⚠
guard: installed (block)     agent hook: claude · cursor · gemini ·
1 identity violates its policy → gitpro check --policy ~/work/gitpro-policy.toml
```

`gitpro team --online` also asks `gh`/`glab` two things: whether each signing
key is registered (`key registered`), and whether the identity's email is
verified on the forge (`verified`). gitpro prints one line first that says
what it asks and of which accounts. `verified` shows `·` when no rule sets
`require_verified_email`.

When the forge gives no answer, the cell shows `⚠`, never `✗`. That happens
when an account is not logged in, or a token lacks the email scope. The
reason is printed as a `⚠` line above the table. In `--json` it goes to
`warnings`, and the check's message reads `could not check (<reason>)`:

```text
$ gitpro team --online
online: asking github.com via gh (account janedoe, janedoe-employer) which SSH keys are registered on your accounts and which of your emails they have verified; sends only the list requests; keys and emails are compared locally
⚠ gh has no login for janedoe on github.com; run: gh auth login -h github.com; default skipped
⚠ gh has no login for janedoe-employer on github.com; run: gh auth login -h github.com; work-github skipped
team status (this machine)
  identity     policy                       email  signing  key registered  verified      trailers
  work-github  Employer engineering         ✓      ✗        ⚠               ⚠             ⚠
guard: installed (block)     agent hook: claude · cursor · gemini ·
1 identity violates its policy → gitpro check --policy ~/work/gitpro-policy.toml
```

`team --online` needs `gh` or `glab`, and an identity with a `gh_account` or
`glab_account`. Without them, it exits 2.

## Undo everything

`gitpro uninstall` removes everything `setup` wrote: the marked blocks in
`~/.gitconfig` and `~/.ssh/config` (and those files, if nothing else is left
in them), `~/.gitconfig-identities`, every `~/.gitconfig-<id>`,
`~/.ssh/config.d/git-identities`, `~/.config/git/allowed_signers`, and the
global guard hooks in `~/.config/gitpro/hooks` with
`~/.gitconfig-gitpro-guard`. It shows the list and asks first. It keeps your
config and your SSH keys.

These are not part of `uninstall`. Each one printed its own `↩ undo:` line
when it ran, and has its own command:

| what | undo |
|---|---|
| per-repo guard (`guard install --repo DIR`) | `gitpro guard uninstall --repo DIR` |
| agent hook (`setup --agent`) | `gitpro agent uninstall` (same `--agent`/`--scope`) |
| MCP registration (`setup --agent`) | `gitpro mcp uninstall` (same `--agent`/`--scope`) |
| `devcontainer --write` (deprecated) | the `↩ undo:` script it printed, or remove the `initializeCommand` key |
| `pin` | `gitpro pin --remove` in each pinned repo |
| `scan --fix` | the `↩ undo:` script it printed |
| `audit --fix` | the `↩ undo:` script it printed (it moves the branch back) |
| `clone` | remove the cloned directory |

Undo scripts live in `~/.local/state/gitpro/`; backups of files gitpro edited
are next to them. They are plain `sh` scripts you can read before running.

## Exit codes and JSON

| code | meaning |
|---|---|
| `0` | ok |
| `1` | something to do or a problem found: `setup -n` has something to do, `scan` or `audit` found something, a policy violation, or you said no at a question |
| `2` | configuration, usage or runtime error, including "this command needs `--online`" and "no terminal to confirm on" (the message names `-y` and `-n`) |
| `3` | hook block: only from `guard check` (what the hooks call) and `guard test`, for a confirmed mismatch with `on_mismatch = "block"`. Hooks treat every other non-zero code as "not blocking". |
| `130` | interrupted |

Output uses one symbol per kind of line: `✓` done or fine, `⚠` warning,
`✗` error or blocked, `→` the next step (or a change "from → to"), `↩` how to
undo, `·` secondary detail. Paths under your home are shown with `~/`.
Commands to run are printed ready to copy.

With `--json`, a command prints exactly one JSON object on stdout and nothing
else. Every object has `"ok"` and `"command"`; on an error it is
`{"ok": false, "command": "…", "error": "…"}` with the same exit code as
without `--json`. Paths are absolute. A writing command with `--json` needs
`-y`, because there is no question to answer. `gitpro --version --json` adds
the Python version and a `capabilities` list:

```text
$ gitpro --version --json
{
  "ok": true,
  "command": "version",
  "version": "0.4.0",
  "python": "3.12.3",
  "capabilities": [
    "agent-check",
    "agent.install",
    …
    "why.config"
  ]
}
```

Tools (the VS Code extension, the skill) look for a capability instead of
comparing version numbers.

## Deprecated in 0.4.0, removed in 0.5.0

0.4.0 reduces gitpro to the commands above. The commands, flags and MCP tools
below still work in 0.4.x and are removed in 0.5.0. Use the replacement.

### Commands and flags

| deprecated | use | note |
|---|---|---|
| `init` | `gitpro setup` | |
| `discover` | `gitpro setup PATH` | |
| `plan` | `gitpro setup -n` | |
| `apply` | `gitpro setup -y` | |
| `check` | `gitpro setup -n` | Exit 1 when something is to do. Plain `check` is `apply` in check mode. `check --repo`, `--policy` and `--range` check a [team policy](#teams); this row names no replacement for them. |
| `doctor` | `gitpro setup -n` and `gitpro why` | |
| `which` | `gitpro why --json` | its `settings` field |
| `lost` | `gitpro scan --lost` | |
| `unpin` | `gitpro pin --remove` | |
| `devcontainer` | VS Code: "Set up devcontainer pin" | |
| `keys --generate` | `gitpro setup` | `keys` itself stays |
| `guard install` without `--repo` | `gitpro setup` | `guard install --repo`, `guard uninstall`, `guard status` and `guard test` stay |
| `agent install`, `agent status` | `gitpro setup --agent NAME`, `gitpro setup -n` | `agent uninstall` stays (hidden) |
| `mcp install` | `gitpro setup --agent NAME` | `mcp uninstall` stays (hidden) |
| `scan --suggest` | `gitpro setup --add PATH` | |
| `audit --rewrite` | `gitpro audit --fix` | |
| `setup --write` | `gitpro setup` | already a no-op |
| `preflight`, `run`, `operation-status`, `operation-reconcile`, `change …` | none | nothing uses them |

The last row is the agent operation protocol: `preflight`, `run`,
`operation-status`, `operation-reconcile`, `change propose`, `change approve`,
`change apply`, `change status` and `change recover`. No replacement is
planned because nothing used it. The documentation of the protocol is kept in
[agent workflows](agent-workflows.md#deprecated-the-operation-protocol) until it is removed.

### MCP tools

| deprecated | use |
|---|---|
| `which_identity` | `why` (its `settings`) |
| `check_commit` | `why` with `committer_email`, `author_email` and `message` (its `commit`) |
| `identities` | `setup` (its state) |
| `lost` | `scan` with `lost: true` |
| `env_for` | `env` (same output) |
| `draft_report` | `report` (same draft) |
| `preflight`, `operation_status`, `reconcile_operation`, `propose_change`, `change_status`, `apply_change`, `recover_change`, `run_operation` | none |

`setup`, `env` and `report` are new tools. `env` is the same as `env_for` and
`report` is the same as `draft_report`.

### How a deprecated command behaves in 0.4.x

- It does what it did in 0.3.4: same output, same files, same exit codes.
- It is hidden from `gitpro --help` and from shell completion.
- It prints one line on stderr before it runs, for example
  `⚠ gitpro apply is deprecated and will be removed in 0.5.0; use: gitpro setup -y`.
  For a command with no replacement the line ends
  `…removed in 0.5.0; nothing replaces it`.
- With `--json` nothing goes to stderr. The JSON object gains
  `"deprecated": {"removed_in": "0.5.0", "use": "gitpro setup -y"}`; `use` is
  `null` when nothing replaces it.
- `GITPRO_NO_DEPRECATION=1` turns off both the line and the field.
- A deprecated MCP tool stays in the tool list. Its description starts with
  `Deprecated, removed in 0.5.0; use <tool>.` and its result has the
  `deprecated` field.
- `gitpro --version --json` lists the capability `deprecations.v1`, so a
  client can tell that it is there. Other capabilities stay announced in 0.4.x.

An 0.3 VS Code extension keeps working with gitpro 0.4.x.

### What happens in 0.5.0

The deprecated commands, flags and tools are removed. Proposed, not decided:
keep `unpin`, `lost`, `audit --rewrite`, `check` and `apply` as aliases of
their replacements, and make the other removed names exit 2 with a line such
as `gitpro plan was removed in 0.5.0; use: gitpro setup -n`.

## Troubleshooting

- **Wrong or no identity**: run `gitpro why` in the repo and compare
  its remote URL with `host` and `owners`. Owners are case-sensitive.
  `gitpro why` explains the decision and prints the fix.
- **Push or clone signs in as the wrong account**: register the identity's key
  with that account (`gitpro keys`). With an `ssh_alias`, ssh offers only
  that key, so you get an error instead of the wrong account. `gitpro auth`
  shows which account each host and alias signs in as.
- **One repo ignores the config**: a repo-level `user.email` overrides it.
  `gitpro scan DIR` shows it; `gitpro scan --fix DIR` removes it.
- **Not sure what is wrong**: `gitpro setup -n` shows what is missing on this machine, and `gitpro why` explains one repo.
- **Inside a container**: run `gitpro pin` in the repo on the host, or use
  "Set up devcontainer pin" in VS Code so it happens on every start.
- **husky, lefthook or pre-commit**: the global guard does not reach repos
  with their own `core.hooksPath`; use `gitpro guard install --repo DIR`.
- **A hook blocked my commit**: read the `cause:` and `fix:` lines. Skip once
  with `GITPRO_SKIP=1 git commit …` if you are sure.
- **A hook printed `exited N; not blocking`**: gitpro failed and let the
  commit through; `gitpro why` in the repo and `gitpro setup -n` show why.
- **The agent hook does nothing**: the hook runs `gitpro` from a plain `sh`;
  check that `command -v gitpro` works there.
- **`scan --lost` or `audit` says a pushed commit is unpushed**: both go by your
  remote-tracking refs as of the last fetch. Run `git fetch` and try again.
- **`gitpro check` passes but `gitpro why` shows a policy problem**: plain
  `check` (deprecated) only asks whether `apply` would change anything. Use
  `gitpro check --repo .` for the policy.
- **CI ignores the policy change in my pull request**: in `--range` mode the
  policy is read from the base commit. Your change takes effect once it is
  merged ([Check a pull request in CI](#check-a-pull-request-in-ci)).
- **Something else**: run `gitpro report` in the repo where it happened
  ([Reporting a problem](#reporting-a-problem)), or open an issue at
  <https://github.com/Yantrakar/gitpro/issues> yourself. Report a security
  problem privately with `gitpro report --security`, as
  [SECURITY.md](../SECURITY.md) describes.

### Reporting a problem

`gitpro report` drafts a GitHub issue for you, shows you the draft and asks
before it does anything with it:

```sh
gitpro report -m "scan missed a repo"   # a bug, with the why of this repo
gitpro report --crash                   # the last internal error
gitpro report --denial                  # the last commit or push gitpro blocked
gitpro report --security                # the private security advisory form
```

On yes it opens the prefilled issue form in your browser (with no browser,
it prints the URL); you read it again and submit it there. `--print` only
prints the draft and the URL, and so does a run with no terminal and no
`-y`. `--online` instead files the issue with `gh issue create`, as one of
your own `gh` logins, when you say yes (`--as ID` picks the identity). `--security`
opens GitHub's private advisory form with nothing filled in, and never
works with `--online`.

What goes in:
- the versions of gitpro, Python, the OS, git and ssh, and how gitpro was
  installed;
- a redacted `gitpro why` for the repo (`--repo PATH`, default the current
  directory): the URL form of each remote, the rule that matched, which
  config scope set each value;
- for `--crash`: the exception type, the command and the file, line and
  function of each frame inside gitpro;
- for `--denial`: the agent, the action, the reason and the problem codes;
- your `-m` text.

What never goes in: emails, names, identity ids, hosts other than the big
public forges, owners, repo names, key and file paths, logins, your machine
and user name, tokens and keys. Each becomes a placeholder such as `email-1`
or `owner-1`, so the draft still shows that two values differ. If the
finished draft still contains one of your config's values, a path under your
home folder, an email, a token or a key, gitpro sends and opens nothing,
exits 2 and names only the kind of value and the section. Text you add in
the browser is yours to check.

gitpro keeps two small files for this in its state directory
(`$XDG_STATE_HOME/gitpro`, or `~/.local/state/gitpro`), readable only by
you:
- `last-crash.json`: the last internal error: its time, gitpro version,
  command, exception type and the frames inside gitpro. Never the error
  message, arguments, variables or other paths.
- `denials.jsonl`: the last 20 commits or pushes gitpro blocked: time,
  version, hook or agent, action, reason and problem codes. Never the
  emails, ids, paths or the repo.

Neither file is sent anywhere unless you run `gitpro report` and say yes.
An internal error prints `✗ internal error (<Type>): this is a gitpro bug`
and exits 2; set `GITPRO_DEBUG=1` to also see the traceback.

## VS Code and Claude Code

- **VS Code**: the extension in [`vscode/`](../vscode/) shows the current repo's
  identity and SSH key in the status bar, turns red on a mismatch and offers
  the fix, and runs `setup`, `scan`, `pin`, `clone` and more. It has views for
  repo-level overrides and lost work. Install it from the Marketplace with
  `code --install-extension yantrakar.gitpro`, or build it from source with
  `cd vscode && npm ci && npx vsce package && code --install-extension gitpro-0.4.0.vsix`.
  See [`vscode/README.md`](../vscode/README.md).
- **Claude Code**: a [skill](../skills/gitpro/SKILL.md) teaches Claude to
  edit the config from plain requests, show `setup -n`, and run `setup -y` only after you
  confirm. Install it as a plugin:

  ```
  /plugin marketplace add Yantrakar/gitpro
  /plugin install gitpro@gitpro
  ```

  The plugin also brings the [agent hook](#agent-hook) and the
  [MCP server](#mcp-server). Both need `gitpro` on the `PATH` of a plain
  `sh`.

  Other agents that read `SKILL.md` files can use
  [`skills/gitpro/`](../skills/gitpro/) as is.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e . pytest pytest-xdist && .venv/bin/pytest -n auto
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
.venv/bin/python tests/smoke_wheel.py dist/gitpro-0.4.0-py3-none-any.whl
.venv/bin/python tests/smoke_providers.py      # remote URL forms of common git hosts (-v: every case)
cd vscode && npm ci && npm test                  # extension unit tests
xvfb-run -a npm run test:integration             # starts VS Code; drop xvfb-run with a display
```

`.venv/bin/pytest -q -rs` lists skipped tests; with every module present,
only platform-specific tests may skip. `-n auto` (pytest-xdist) runs the
tests in parallel, one worker per CPU; CI does the same.
The shared `home` fixture also starts in its isolated home and sets a Git
discovery ceiling, so scratch directories inside a checkout cannot accidentally
install project settings into that checkout.
The wheel smoke runs the extracted distribution outside the checkout with a
disposable home. It exercises default approval refusal, operator-scoped apply,
idempotent replay, undo and MCP execution gates without network access or live
configuration changes. CI runs it on the Python/Linux/macOS matrix and retains
wheel/VSIX artifacts; uploading an artifact does not publish a release.
The provider smoke sets up identities for GitHub, GitLab, Bitbucket, Azure DevOps,
Codeberg, SourceHut, Gitea, Gitee, Hugging Face, AWS CodeCommit and Launchpad
in a disposable home, runs `gitpro apply`, and checks 45 remote URLs offline:
git and gitpro must pick the same, expected identity, and a URL no pattern
covers must get `gitpro why`'s `url_form` fix. CI runs it after the tests.

To release, bump the version in `src/gitpro/__init__.py`, `vscode/package.json`
and the plugin manifests, merge, then push a tag `vX.Y.Z`. The release workflow
checks the tag against both versions, builds and tests once, and publishes the
same files to GitHub Releases (with `SHA256SUMS`), PyPI (trusted publishing,
environment `pypi`) and the VS Code Marketplace (`marketplace.yml`: a Microsoft
Entra managed identity, variables `AZURE_CLIENT_ID` and `AZURE_TENANT_ID`,
environment `marketplace`; skipped while the variables are unset, and runnable
by hand for an existing tag). Once PyPI has the release, `mcp-registry.yml`
publishes `server.json` to the MCP Registry, signed in with the workflow's
GitHub OIDC token (environment `mcp-registry`). A registry version cannot be
changed after it is published, so a version already there is skipped; run the
workflow by hand to publish an existing tag.

Live GitHub qualification is separate and opt-in: `python tests/smoke_github.py --help`.
It previews by default; `-y` authorizes a private clone/configuration, candidate
commit, push to a new `gitpro-e2e-*` branch, and draft PR. Supply an isolated
operator-selected `gh` profile (including `workflow` scope if the candidate edits
CI). It checks execution, idempotent replay and read-only reconciliation using
the packaged CLI. Evidence and the test resources are retained: inspect CI,
then close that draft PR and delete only its temporary branch. Never retry an
interrupted write with a new key or delete unrelated resources.
`GH_CONFIG_DIR` isolates config files, but some `gh` versions share an active
credential slot in the OS keyring. Check the actual `gh api user` login before
and after authentication, not just the account label in `gh auth status`, and
restore the original account with `gh auth switch` if necessary. The live smoke
refuses to write when its selected profile authenticates as another account.

## License

MIT
