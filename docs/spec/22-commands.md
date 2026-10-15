# 22. One command surface (0.4.0)

Status: **released in 0.4.0 (2026-10-16)**, 2026-10-16, PR #20. Builds on
spec 21 (setup, the `ambiguous` problem, scan's checks), whose §7 this spec
details and replaces. What users need is in the README, `docs/guide.md` and
`CHANGELOG.md`; the choices made while building it, and what is still open,
are in `DECISIONS.md` (2026-10-16). Where this text and the code differ, the
code and those files win.

## 1. What the owner asked

- "the problem is this is all robotic. the repos already have identity. all
  we need is the user is given option to scan during setup. and that should
  gather info all repos in given path whatever required. and that is then
  used to setup. so this way one step fixes all"
- "use modern unix style to design commands"
- "terminal, vscode, mcp everywhere it should be same. also commands be
  deduped and reduced to bare minimum of what we do"
- "of course code needs to be reviewed and modularized and deduped. but user
  interface needs to be simple and nondup"
- On `lost`: "include in scan e.g. --lost". On the agent operation protocol:
  "drop whats not used".
- "you may deprecate those. and declare in version 0.5.0 we will drop it.
  suggest alternate if we want to support backward compatibility"

Decisions in this spec that the owner has not seen yet are marked
**(Claude's choice)**; the owner can reverse any of them.

## 2. What gitpro does

Five jobs, and nothing else:

1. **Set up a machine** so that every repo gets the identity of its remote.
2. **Explain one repo**: which identity, why, whether a commit is blocked, the fix.
3. **Check all repos** and fix what needs no choice.
4. **Choose** the identity of a repo whose remotes disagree, or clone one right.
5. **Find commits** already made as the wrong identity.

Supporting jobs: forge tokens for a shell (`env`), shell integration
(`completion`, `prompt`), a bug report (`report`), team policy (`team`,
`policy`), and removing gitpro (`uninstall`).

## 3. Rules (all commands, all front ends)

R1. **One command per job.** No two commands do the same job.

R2. **One surface.** The terminal runs the command. The VS Code extension
runs the same command with the same flags and reads the same `--json`; it
asks its yes in a dialog and passes `-y`. An MCP tool has the command's name
and runs its read-only form; it returns the fix command for the user to run.
Names, options and JSON fields are the same in all three.

R3. **Flags mean one thing everywhere**: `-n, --dry-run` show only;
`-y, --yes` don't ask; `--json` machine-readable on stdout; `-v` more; a
positional `PATH` names the repo or folder; `-c PATH` the config.

R4. **Changes.** On a terminal a command that changes something shows the
change and asks once; `-y` skips the question; `-n` only shows. In 0.4.0 this
holds for `setup`: without a terminal and without `-y` or `-n` it exits 2
naming both. `pin`, `scan --fix` and `audit --fix` keep their 0.3 behavior
(a preview without `-y`) in 0.4.x and also accept `-n`; they move to R4 in
0.5.0 **(Claude's choice: no behavior change for kept commands in a minor
release)**.

R5. **Check, then `--fix`.** A command that finds problems fixes them with
`--fix`. Variants of one job are flags (`scan --lost`), not commands.

R6. **Streams and exit codes.** Results on stdout, messages on stderr.
Exit 0 ok, 1 problems found (or, for `-n`, something to do), 2 error or
refused, 130 interrupted. No color when piped or with `NO_COLOR`.

## 4. The commands

`gitpro --help` lists, in this order:

| command | job | VS Code | MCP tool |
|---|---|---|---|
| `setup [PATH...]` | set up or update this machine (§5) | Set Up | `setup` (runs `setup -n --json`) |
| `why [PATH]` | explain one repo (§6) | status bar, Why | `why` |
| `scan [PATH...]` | check all repos; `--fix`, `--lost` (§7) | Scan, Fix Checked | `scan` |
| `pin [PATH]` | choose a repo's identity; `--remote R`, `--remove` (§8) | Pin, Unpin | |
| `clone URL [DIR]` | clone as the right identity | Clone | |
| `audit [PATH]` | commits made as the wrong identity; `--fix` (§9) | | `audit` |
| `env [PATH]` | shell lines with the forge token of the repo's identity | Copy env | `env` |
| `uninstall` | remove what gitpro wrote | | |

then, under "more": `completion`, `prompt`, `report` (MCP `report`), `team`,
`policy`, `keys`, `auth`, `forge`, `guard`.

`keys`, `auth`, `forge`, `team` and `policy` are features, not duplicates,
and stay as they are in 0.4.0 except `keys --generate` (§10). Whether they
fold into `setup -n --online` is open; it needs usage data first (§13).

Hidden (called by hooks, agents and the extension, never typed):
`agent-check`, `guard check`, `mcp` (the server), `agent uninstall`,
`mcp uninstall`, and every deprecated command (§10).

## 5. `gitpro setup`

```
gitpro setup [PATH...] [-n] [-y] [--add] [--agent NAME] [--no-passphrase] [--json] [-c PATH]
```

### 5.1 Which run it is

| state | what setup does |
|---|---|
| no config, an empty config, `--add` or a `PATH` | finds identities (§5.2), asks what it cannot decide, then §5.4 |
| a config with identities | §5.4 only; asks no identity questions |
| `-n` | §5.5; never asks, never writes |

Finding identities needs a terminal. Without one, setup exits 2 (§5.7).
A `PATH` that is not a folder exits 2 ("not a folder").

### 5.2 Finding identities from the repos

1. **Where.** With `PATH` arguments, setup looks there. Without them it asks
   `Look through your repos to find your identities? [Y/n]`, then
   `Where [~]` (space-separated folders; `~` expands). No skips this step and
   goes to §5.3 with nothing found.
2. **What it reads** (all local, all read-only, the sources of `discover`):
   - every repo under the folders, at any depth: hidden directories,
     `node_modules` and, under `~`, discover's `HOME_SKIP` caches are
     skipped; 20 s budget (`setup.LEARN_BUDGET`; `discover` alone keeps
     `MAX_DEPTH = 3` and `BUDGET = 5.0`);
   - per repo: its remote's host and owner; the name and email it uses, in
     this order: set in the repo; set by an `includeIf` file; **the identity
     of the commits made in it on this machine** (§5.3);
   - SSH config: aliases, their host and `IdentityFile`;
   - `includeIf` rules in the global Git config;
   - VS Code and JetBrains recent folders (to find more repos).
   It never reads a private key, shell history, or a forge account.
3. **Progress** is one line on stderr: `looked at 104 repos in ~ (2.1 s)`.

### 5.3 The commit identity of a repo

Git appends a line to `<git dir>/logs/HEAD` for every commit made in a
worktree, with the committer's name and email:

```
<old> <new> Name <email> <time> <tz>\tcommit: message
```

A repo's commit identity is the most frequent `(name, email)` among the
lines whose message starts with `commit` (`commit:`, `commit (initial):`,
`commit (amend):`, `commit (merge):`), reading at most the last 1 MiB of the
file. Lines from `checkout`, `pull`, `reset` and others are ignored: they are
not commits. A reflog expires after 90 days by default, so this is recent use.

Measured on the owner's machine (2026-10-16): 104 repos under `~`; 52 have
commit lines; grouped by host and top-level owner they form 6 groups with 8
distinct emails; 4 groups have commits under more than one email.

### 5.4 From what was found to identities

1. Records are grouped by host, then owner (discover's `hosts`).
2. Each owner gets the email most of its repos use, and the most frequent
   name with that email. An owner whose repos use more than one email keeps
   the most used one, and the others are shown: `also seen: me@old.test in
   2 repos`.
3. Owners of one host with the same email form one identity: `id` from the
   owner with most repos (discover's `_new_id`), `host`, `owners` sorted by
   repo count.
4. Owners with no email (no commits, no setting) join the host's identity
   when the host has exactly one; otherwise they are listed as "not
   assigned" and left out **(Claude's choice: no guessing)**.
5. **SSH key**: when the owners' remotes use an SSH alias whose
   `IdentityFile` is one file, that file is the identity's `ssh_key`.
   Otherwise setup asks for it as before (an existing key, a new path to
   create, or blank).
6. Owners already in the config (`--add`) are skipped.
7. Hosts that gitpro's config check rejects and owners `OWNER_RE` rejects
   are listed as skipped with the reason.

Setup then shows what it found:

```
Found 3 identities in 52 repos under ~:

  1  sandeepshelke   Sandeep <me@personal.test>   github.com   sandeepshelke, Yantrakar (+11)   ~/.ssh/id_personal
  2  corp            Sandeep <me@corp.test>       github.com   corp-org                         no key found
     also seen: me@personal.test in 2 corp-org repos
  3  nanoframes      Sandeep <me@nano.test>       gitlab.com   nanoframes, xplaque              ~/.ssh/id_nano

  not assigned: github.com/someone (1 repo, no commits)

Use these? [Y/n/e]  (e: go through them one by one)
```

- `y` keeps all of them; for each one without a key setup asks the key
  question, then the signing question as before.
- `e` goes through each identity: keep, edit (the old questions with the
  found values as defaults), or skip.
- `n` drops them and asks for identities by hand (the 0.3 questions).

Then, with no config yet, the fallback identity (`[default]`):
`Set a fallback identity? [y/N]`,
defaulting to the global `user.name` / `user.email`. Then §5.4's preview.

Nothing found (no repos, or none with a remote) goes straight to the 0.3
questions.

### 5.5 One preview, one yes

Unchanged from spec 21 §4: one table "Setup will:" grouped config, SSH key,
Git and SSH files, guard, agents; "Already in place: …"; warnings; then
`Apply these changes? [y/N]`. `-y` answers yes. It never edits a repo.

### 5.6 `setup -n`: the state of this machine

Never asks, never writes, needs no terminal. It prints the identities, the
keys and where to register them (`keys.report`), and the same "Setup will:"
table of what is still to do, or `✓ this machine is set up; nothing to change`.

Exit 0 when nothing is to do, 1 when something is, 2 on error. `--json`:

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

`ok` is false when `pending` is not empty. This replaces `check` (exit 1 when
something would change), the machine half of `doctor`, MCP `identities`, and
`agent status`.

With no config, `setup -n` exits 1 with `pending` = `[{"group": "config",
"what": "no config yet: run gitpro setup in a terminal"}]`.

### 5.7 Without a terminal

| | no `-y`, no `-n` | `-y` | `-n` |
|---|---|---|---|
| config exists | exit 2: "no terminal to confirm on; run gitpro setup -y to apply, or gitpro setup -n to see what it would do" | applies | §5.6 |
| no config | exit 2 (§5.1) | exit 2 (§5.1) | §5.6 |

This replaces 0.4's earlier "Dry run: nothing written …" exit 0.

## 6. `gitpro why`

```
gitpro why [PATH] [--remote R] [--author EMAIL] [--committer EMAIL] [--message TEXT] [--json]
```

Unchanged text output. Without a config, `--json` still exits 2 with
`{"error", "config": null, "repo", "remotes", "settings"}` (`settings` only
when PATH is a repo). `--json` gains:

- `settings`: exactly `which --json`'s `settings` object (so the extension
  and agents need only `why`);
- `commit`: the guard's judgement of a commit by `--committer` (default:
  the email git would use) and `--author` (default: the committer), with
  `--message` checked against the Co-Authored-By rules:
  `{"would_block": bool, "mode": "block"|"warn"|"off", "problems": [{code, message, fix}],
  "reason": str|null, "fix": str|null}` — the body of MCP `check_commit`
  today, from the same code (moved from `mcp.py` to `commitcheck.py`).

MCP `why` takes `path`, `remote`, `author_email`, `committer_email`,
`message` and returns `why --json` with those passed through. Capability
`why.commit`.

## 7. `gitpro scan`

```
gitpro scan [PATH...] [--repo PATH] [--fix] [-y] [--lost] [--json] [-v]
```

Unchanged plus spec 21 §6 (ambiguous, own hooks). `--lost` adds the
offline checks of `lost` (unpushed branches, stashes, dirty trees, repos
without a remote, stale and empty repos, duplicate clones) for the same
repos, rendered after scan's sections with `lost`'s renderer; `--json` gets
`"lost": {…}`, the body of `lost --json` without `ok`/`command`. `--lost`
with `--fix` is refused (exit 2): lost work needs a person. MCP `scan` gains
`lost: boolean`. Capability `scan.lost`.

## 8. `gitpro pin`

`pin --remove [PATH]` does what `unpin` does, with the same flags and JSON
(`command` stays `"unpin"` in 0.4.x for the extension). `--remove` with
`--remote` is refused. Capability `pin.remove`.

## 9. `gitpro audit`

`audit --fix` is `audit --rewrite`, same flags and behavior. Capability
`audit.fix`.

## 10. Deprecated in 0.4.0, removed in 0.5.0

### 10.1 Commands and flags

| deprecated | use | note |
|---|---|---|
| `init` | `gitpro setup` | |
| `discover` | `gitpro setup PATH` | |
| `plan` | `gitpro setup -n` | |
| `apply` | `gitpro setup -y` | |
| `check` without `--policy`, `--repo` or `--range` | `gitpro setup -n` | exit 1 when something is to do; the team policy check stays and is listed under "more" |
| `doctor` | `gitpro setup -n` and `gitpro why` | |
| `which` | `gitpro why --json` | its `settings` field |
| `lost` | `gitpro scan --lost` | |
| `unpin` | `gitpro pin --remove` | |
| `devcontainer` | VS Code: "Set up devcontainer pin" | |
| `keys --generate` | `gitpro setup` | `keys` itself stays; `keys --no-passphrase` is hidden, not deprecated |
| `guard install` without `--repo` | `gitpro setup` | `guard install --repo`, `guard uninstall`, `guard status`, `guard test` stay |
| `agent install`, `agent status` | `gitpro setup --agent NAME`, `gitpro setup -n` | `agent uninstall` stays (hidden) |
| `mcp install` | `gitpro setup --agent NAME` | `mcp uninstall` stays (hidden) |
| `scan --suggest` | `gitpro setup --add PATH` | |
| `audit --rewrite` | `gitpro audit --fix` | |
| `setup --write` | `gitpro setup` | already a no-op |
| `preflight`, `run`, `operation-status`, `operation-reconcile`, `change …` | none | nothing uses them (spec 21 §7.3) |

### 10.2 MCP tools

| deprecated | use |
|---|---|
| `which_identity` | `why` (its `settings`) |
| `check_commit` | `why` with `committer_email` / `author_email` / `message` (its `commit`) |
| `identities` | `setup` |
| `lost` | `scan` with `lost: true` |
| `env_for` | `env` |
| `draft_report` | `report` |
| `preflight`, `operation_status`, `reconcile_operation`, `propose_change`, `change_status`, `apply_change`, `recover_change`, `run_operation` | none |

New tools: `setup`, `env` (same as `env_for`), `report` (same as
`draft_report`).

### 10.3 How a deprecated thing behaves in 0.4.x

- It does exactly what it did in 0.3.4 (same output, files, exit codes).
- It is hidden from `gitpro --help` and from shell completion.
- It prints one line on stderr before running:
  `⚠ gitpro apply is deprecated and will be removed in 0.5.0; use: gitpro setup -y`
  (for "none": `…removed in 0.5.0; nothing replaces it`).
- With `--json` the object gains
  `"deprecated": {"removed_in": "0.5.0", "use": "gitpro setup -y"}` (`use`
  is null for "none"); nothing goes to stderr then, so JSON consumers that
  capture both streams stay clean.
- `GITPRO_NO_DEPRECATION=1` turns off both.
- A deprecated MCP tool stays in `tools/list`; its description starts with
  `Deprecated, removed in 0.5.0; use <tool>.` and its result has the
  `deprecated` field.
- Capabilities stay announced in 0.4.x; `deprecations.v1` is new so a client
  can tell.

### 10.4 0.5.0

Deprecated commands, flags and tools are removed with their code and tests.
Then (proposed, owner to decide):

- **Aliases** where the mapping is exact, with no second code path:
  `unpin` → `pin --remove`, `lost` → `scan --lost`, `audit --rewrite` →
  `audit --fix`, `check` → `setup -n`, `apply` → `setup -y`.
- **Tombstones** for the rest: the name stays in the parser and exits 2
  with `gitpro plan was removed in 0.5.0; use: gitpro setup -n` instead of
  argparse's "invalid choice". One line each; no end date.

## 11. VS Code extension 0.4.0

- `Show Identity` and the status bar use `why --json` (its `settings`) when
  the CLI has capability `why.commit`; otherwise `which --json` as before.
- Commands removed from the palette: Plan, Apply, Doctor, Suggest
  Identities, Review One by One. "Set Up Identities" becomes "Set Up" and
  runs `gitpro setup` in a terminal.
- "Find Lost Work" runs `scan --lost --json` when the CLI has `scan.lost`,
  else `lost --json --offline`.
- "Unpin" runs `pin --remove -y` when the CLI has `pin.remove`, else
  `unpin -y`.
- "Set up devcontainer pin" writes the devcontainer line itself instead of
  calling `devcontainer --write`, after a modal that shows the line; JSONC,
  unparseable or read-only files get Copy only. No backup is kept.
- Open Config still offers `init` when there is no config (open, see
  DECISIONS).
- An 0.3 extension keeps working with gitpro 0.4.x (§10.3).

## 12. Tests

- Every 0.3 test of a deprecated command keeps passing with
  `GITPRO_NO_DEPRECATION=1` (set for all tests by `conftest.py`).
- Per deprecated command and flag: the stderr line, the `deprecated` JSON
  field, and that `GITPRO_NO_DEPRECATION=1` silences both.
- `--help` lists §4's commands in order and none of §10's.
- Per new form (`setup -n`, `setup PATH`, `why --committer`, `scan --lost`,
  `pin --remove`, `audit --fix`): a test that it matches the deprecated form
  it replaces.
- setup discovery: reflog parsing (commit lines only, the 1 MiB tail),
  grouping, the most-used email with "also seen", owners without email, the
  alias key, `y`/`e`/`n`, nothing found.
- `setup` without a terminal: exit 2 without `-y`/`-n`.
- MCP: new tools, deprecated descriptions and fields.

## 13. Open

Moved to `DECISIONS.md` (2026-10-16, "Open"). Kept here for the record:

- `keys`, `auth`, `forge`, `team`, `policy`: fold into `setup -n --online`
  and `why`, or keep. Needs usage data.
- Moving `pin`, `scan --fix` and `audit --fix` to R4 in 0.5.0.
- 0.5.0 aliases and tombstones (§10.4).
- Code dedupe behind the surface: `discover` becomes the collection library
  for `setup`; `doctor`/`check`/`plan` code goes in 0.5.0 with the commands.
