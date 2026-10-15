# 21 · Redesign: two steps, one judgement, no silent winners

Status: **draft for the product owner's review**. Nothing here is built.
Written 2026-10-15 against `main` at 021adb6 (0.3.4) plus PR #19.

The owner asked for this rethink after a review of the blind spots:

> "i never ran gitpro setup. we need only 2 steps. pipx install gitpro and
> gitpro setup. and it should cover all. so gitpro guard install etc are
> adding to confusion. also fix the blindspots"
>
> "rethink in overall terms. dont keep patching"
> "architecture, design and overall function/application characteristics"

This file is not a work package like 01-20. It states what gitpro is for, where
the 0.3 design breaks that purpose (each point with the evidence that shows it),
the design that replaces it, and the steps to get there. The work packages that
build it come after the owner accepts it (section 10).

## 1. What gitpro is for

**Job.** Every commit and push a person makes from their machines carries the
identity (name, email, SSH key, signature, forge account) that belongs to the
place the work goes, without the person thinking about it.

**Model (kept).** The user describes identities once in
`~/.config/gitpro.toml`. gitpro turns that into plain git and ssh config:
`includeIf "hasconfig:remote.*.url:<pattern>"` blocks, ssh `Host` aliases,
`allowed_signers`. Git does the matching at git time; gitpro's code runs at git
time only in the guard hooks. This is gitpro's strongest property and nothing
below changes it.

**Characteristics the product must have.** These are the yardstick for
sections 3-7.

| | characteristic | meaning |
|---|---|---|
| C1 | **Two steps** | `pipx install gitpro`, then `gitpro setup`. After that the machine is fully set up and protected. Running `gitpro setup` again at any time brings the machine up to date. |
| C2 | **Machine and repos are separate** | `setup` owns files in the home directory and never edits a repo. `scan` owns repos: it reports what is wrong in each and how to fix it, and changes a repo only when the user asks for that fix. |
| C3 | **Protected by default** | The commit-time check is installed by `setup`. A wrong identity is stopped before the commit exists, not only at push. |
| C4 | **No silent winners** | When the config cannot say which identity a repo should use, gitpro says so everywhere and blocks the commit with the fix. It never reports ✓ for a choice git made by file order. |
| C5 | **One judgement** | `why`, `scan`, the hooks, the agent hook, the VS Code status bar and the MCP tools return the same verdict for the same repo, because they all call the same function (today: `resolve`). |
| C6 | **One consent grammar** | Every writing command previews by default, asks once, and takes the same flags. |
| C7 | **Small surface** | The commands a person needs fit on one screen. Plumbing for hooks, agents and the extension still exists but is not in the way. |

The 0.3 principles in `README.md` §2 stay in force (suggest, user picks;
preview, confirm, reversible; tracked team files print-only; network opt-in;
secrets stay where they are; fail open; agents are told to ask the user),
except where section 8 records a change.

## 2. Where 0.3 breaks these

Every row was observed on 2026-10-15, on the owner's machine or in the sandbox
`scratchpad/remote-probe` (an isolated `HOME`).

| | what happens in 0.3 | evidence | breaks |
|---|---|---|---|
| B1 | **Onboarding is six or more commands.** `setup` saves the TOML only, and needs `--write` to do that. Its last lines tell the user to run `gitpro plan` and `gitpro apply`. Keys, the guard, agent hooks and MCP are each a separate command. | `setup.py:136` prints "No keys are created, no accounts are contacted, and no Git/SSH settings are applied"; `setup.py:258-259`; `gitpro --help` "first run" lists `setup --write`, `plan`, `apply -y`, `keys`, `scan`; the guide adds `keys --generate`, `guard install`, `agent install`, `mcp install --agent …`, `doctor`. | C1 |
| B2 | **The guard is off unless installed by hand.** The owner's own machine has none. | DECISIONS Q4 ("Nothing installs until the user runs `guard install`"); `gitpro guard status` on the owner's machine: "guard: not installed". | C3 |
| B3 | **Two remotes that match two identities: the later include wins, and gitpro calls it correct.** origin `git@github.com:corp-org/app.git` (work), upstream `git@github.com:me/app.git` (personal): git uses personal. `gitpro why` shows ✓ "matched upstream by pattern git@github.com:me/**". The pre-commit guard allows the commit. Only pre-push blocks ("this remote belongs to work"), after the commit exists, and the fix is then `audit --rewrite`. | Sandbox `fork2`. `resolve._expected_for`: "every matching include applies; the later one wins". `git help config`: "If there exists at least one remote URL that matches this pattern, the include condition is met." | C4, C3 |
| B4 | **`pin` copies the silent winner.** In the same repo `gitpro pin` would pin "Personal Me", the identity of upstream, not of origin where the work is pushed. | Sandbox `fork2`: "Would pin Personal Me <me@personal.test> in …/fork2/.git/git-identities". `pin.pin` writes the identity git resolves now. | C4 |
| B5 | **Consent flags differ per command.** `apply`, `pin`, `scan`, `clone`, `audit`: `-y` and `--write`. `setup`: `--write` only. `guard`: `-y` on the parent (`gitpro guard -y install`), `--write` on the subcommand. `keys`: `--generate`, `--upload`, and `-y` only with `--upload`. `scan --fix`, `audit --rewrite`: action flags on top of `-y`/`--write`. | `--help` of each command, 2026-10-15. | C6 |
| B6 | **35 top-level commands.** Plumbing sits next to daily commands: `agent-check`, `operation-status`, `operation-reconcile`, `preflight`, `run`, `change`. Several read commands answer overlapping questions: `which`, `why`, `doctor`, `check`, `prompt`. | `gitpro --help`. | C7 |
| B7 | **The machine's desired state is in several places.** `plan.build` covers the git and ssh files, and the global guard when `[guard] hooks = true` or it is already installed (`plan.py:107`). Keys (`keys --generate`), agent hooks (`agent install`) and MCP (`mcp install`) have their own installers and status. `doctor` collects their status through optional `doctor_section` functions (`guard.py:547`, `mcp.py:1056`, `policy.py:1756`). No single place can answer "what does this machine still need?", which is what `setup` must answer. | the files named. | C1, C5 |

Blind spots that are **not** design breaks, kept here so they are not lost:

| | case | 0.3 behaviour | 0.4 |
|---|---|---|---|
| N1 | Repo with no remote | `[default]` applies. | Unchanged; `why` and `scan` name it as "no remote: default identity" (section 6). |
| N2 | Remote owner no identity covers | `[default]` applies. | Unchanged; `why` and `scan` name the owner and `scan --suggest` offers a block. |
| N3 | `GIT_AUTHOR_*` / `GIT_COMMITTER_*` in the environment | `why` shows ✗ and names `unset GIT_AUTHOR_EMAIL`. The hooks ignore `GIT_AUTHOR_*` because git itself exports them to hooks (`guard.py:166`). | Unchanged. |
| N4 | Repo with its own `core.hooksPath` (husky and others) | The global guard does not run there; `guard status` lists such repos (`guard.py:475`). | `scan` lists them with the fix (`gitpro guard install --repo PATH`, which refuses hook files tracked by git, `guard.py:946`). `setup` does not touch them (C2). |
| N5 | URL forms with no pattern | Package 18. | Unchanged. |
| N6 | Commits made off this machine (web UI, API, bots, machines without gitpro) | Local hooks cannot see them. | Out of reach of `setup` by nature. `audit` and the CI policy check (14) remain the answer; the guide says so. |
| N7 | Repo-level `user.name` / `user.email` | `scan` reports and `scan --fix` removes them, with undo. | Unchanged, now one category of the scan report (section 6). |

On the owner's machine (191 repos) two repos have two remotes:
`ARENA_3.0` and `ggml-kompute/kompute`. Both resolve to `[default]` and
neither is affected by B3 today. B3 is a correctness gap, not an observed
incident.

## 3. The design

### 3.1 Two engines

gitpro has two questions to answer, and each gets one engine.

```
          ┌──────────────────────────── machine ───────────────────────────┐
config ──▶│ components: config · git/ssh files · keys · guard · agents · mcp │──▶ setup, doctor, check, uninstall
          │ each: inspect() → state, plan() → changes, apply(), undo        │
          └─────────────────────────────────────────────────────────────────┘
          ┌───────────────────────────── repo ─────────────────────────────┐
config ──▶│ resolve.verdict(repo) → Verdict (state, expected, effective,    │──▶ why, scan, guard hooks, agent hook,
remotes ─▶│                          candidates, fix)                       │    prompt, VS Code, MCP, audit, policy
          └─────────────────────────────────────────────────────────────────┘
```

**Machine engine.** One list of components, each with the same four
functions. `plan()` returns the existing `files.Change` type wherever the
component writes files, so preview, backup and undo stay the code that 0.3
already tests.

| component | inspects | writes | today's code it wraps |
|---|---|---|---|
| `config` | the TOML exists, loads, validates | the TOML (first run only, through the wizard) | `setup.py`, `config.py` |
| `files` | generated git/ssh files match `plan.build` | `~/.gitconfig` block, `~/.gitconfig-identities`, `~/.gitconfig-<id>`, ssh config, `allowed_signers` | `plan.py`, `files.py` |
| `keys` | every `ssh_key` in the config exists | missing keys only (ed25519, never overwrites) | `keys.generate` |
| `guard` | global hooks installed and current | the global hooks (already part of `plan.build`) | `guard.py`, `plan.guard_paths` |
| `agents` | which supported agents are on the machine and whether their hook is installed | the user-level agent settings file, with backup | `agents.py` |
| `mcp` | which supported agents have gitpro registered | the user-level registration, with backup | `mcp.py` |

Consumers:

- `setup` = inspect all, plan all, one preview, one yes, apply all.
- `doctor` = inspect all (plus the current repo's verdict), read-only.
- `setup --check` (replaces `check`) = exit 1 when any component would change.
- `uninstall` = the reverse of every component, one preview, one yes.

`doctor_section` becomes the component's `inspect()`; the three modules that
have one today keep it as the implementation.

**Repo engine.** `resolve` already is the single judge (README §4.3). What
changes is its result: a `Verdict` with an explicit state instead of an
expected identity plus a problem list.

| state | when | commit (guard) | `why` / `scan` |
|---|---|---|---|
| `ok` | one identity matches (or a pin), and git uses it | allowed | ✓ |
| `default` | no remote, or no identity matches any remote | allowed | ✓ with the reason ("no remote", "owner X matches no identity") |
| `ambiguous` | remotes match two or more different identities and there is no pin | **blocked** (exit 3) | ⚠ lists each remote and its identity; fix: `gitpro pin --remote R` |
| `overridden` | the repo's own config sets `user.name`/`user.email` | judged as 0.3 (blocked only on mismatch) | listed; fix: `gitpro scan --fix` |
| `mismatch` | git uses something other than the expected identity (env, stale apply, wrong key) | blocked as 0.3 | ✗ with the fix, as 0.3 |

`ambiguous` is new. Two remotes that match the **same** identity are `ok`.
A repo with a pin is never `ambiguous`: the pin is the user's answer.

The pre-push check keeps comparing against the destination remote
(`remote_url=destination`), which is already right.

### 3.2 Pin chooses a remote, not git's winner

`gitpro pin` today writes whatever git resolves now (B4). In 0.4:

- `gitpro pin` in an `ok` or `default` repo: as 0.3.
- `gitpro pin` in an `ambiguous` repo without `--remote`: lists the remotes
  and their identities and asks which one; without a terminal it is an error
  that names `--remote`.
- `gitpro pin --remote R`: pins the identity that R's URL matches.
- The guard's block message and `scan` name the remote to pass: the **push
  remote** of the current branch, found the way `git push` finds it with no
  arguments: `branch.<b>.pushRemote`, then `remote.pushDefault`, then
  `branch.<b>.remote`, then `origin`. If none of those exist, the message
  lists the remotes and asks the user to choose.

## 4. `gitpro setup`

One command for the first run and for every run after.

### 4.1 First run (no config)

1. The identity questions, as the 0.3 wizard asks them (person, fallback
   identity, hosts and owners, key per identity). Discovery suggestions stay
   as defaults.
2. **One preview** of everything the machine will get, grouped by component:
   the TOML, the git/ssh file diffs, the keys to create, the guard, the agent
   hooks, the MCP registrations. Each group says what file it writes and how
   to undo it.
3. **One question**: apply all of it? (`y/N`). No answer writes nothing.
4. Apply in component order. A failure stops at that component, reports
   what was written, and names the undo for it.
5. The closing lines: the public keys to register with each account (as
   `gitpro keys` prints them; no upload, the network stays opt-in), then
   `gitpro scan` as the one next step ("see which repos need attention").

### 4.2 Later runs (config exists)

No identity questions (owner's decision, section 8). setup inspects every
component, shows only what is missing or out of date, and asks once.
"Nothing to do" is ✓ and exit 0. Adding identities stays possible with
`gitpro setup --add` (the 0.3 wizard's "adds identities" mode).

### 4.3 What setup never does

- Edit a repo: no pin, no `scan --fix`, no per-repo guard, no repo files.
  It may end by saying how many repos `scan` would flag (read-only).
- Contact the network: no key upload, no `gh`/`glab` calls.
- Overwrite a key, read a private key, or print a secret.
- Install an agent adapter marked experimental (DECISIONS Q10), unless named
  with `--agent NAME`.

### 4.4 Without a terminal

`gitpro setup -y` with an existing config converges the machine unattended,
for provisioning scripts. Without a config it is an error: the identity
questions need a person (or `-c PATH` to a prepared TOML).

## 5. Consent grammar (one for every writing command)

| form | meaning |
|---|---|
| `gitpro X` | preview only; on a terminal, then ask once |
| `gitpro X -y` | do it without asking (required without a terminal) |
| `gitpro X --json` | never asks; needs `-y` to write |

`--write` goes away as a separate concept: on a terminal the command already
asks, so "preview, then ask" is the default. Action flags that choose **what**
to do stay (`scan --fix`, `audit --rewrite`, `keys --upload`), and they follow
the same rule: they preview and ask, `-y` skips the question. `-y` is accepted
in the same place on every command, after the subcommand.

During 0.4, `--write` keeps working and means what it means today, so no
script breaks (section 9).

## 6. `gitpro scan`

scan becomes the per-repo report the owner asked for: what is wrong, and the
command that fixes it. One section per verdict state that needs attention,
each ending with its fix:

```
191 repos under ~/prayog, ~/workspace

⚠ 1 repo has two remotes for two identities; commits there are blocked
  ~/x/app   origin → work, upstream → personal
  fix: gitpro pin --remote origin ~/x/app

⚠ 2 repos set their own user.name/user.email
  … (the 0.3 overrides tree)
  fix: gitpro scan --fix

⚠ 1 repo has its own core.hooksPath; the guard does not run there
  ~/y/web   (.husky)
  fix: gitpro guard install --repo ~/y/web

  14 repos use the default identity (no remote 9, unknown owner 5)  -v lists them

✓ 173 repos: gitpro decides, no action needed
```

The sample above is illustrative layout, not measured output. As built on
2026-10-15, scan adds the ambiguous and own-hooksPath sections (and their
`ambiguous` and `own_hooks` lists in `--json`) after the 0.3 output. The
identity table stays where it is rather than moving behind `-v`, so 0.3's
output and tests do not change (§9).

`scan --fix` keeps doing what it does in 0.3 (remove repo-level overrides,
with undo). It does not pin: an ambiguous repo needs the user to choose a
remote, so scan prints the command per repo.

## 7. Command surface

The goal is that `gitpro --help` shows the commands a person uses, and the
rest stay available without being in the way. Nothing is removed in 0.4.

| group | commands | 0.4 change |
|---|---|---|
| **shown first** | `setup`, `scan`, `why`, `clone`, `pin`, `unpin`, `audit`, `lost`, `doctor`, `uninstall` | `setup` absorbs the first-run steps |
| **folded into setup** | `init`, `plan`, `apply`, `check`, `guard install` (global), `agent install`, `mcp install`, `keys --generate` | still work; print one line naming the `setup` form |
| **kept, listed under "more"** | `keys`, `auth`, `forge`, `env`, `discover`, `devcontainer`, `team`, `policy`, `report`, `completion`, `guard install --repo`, `guard status`, `guard test` | no change |
| **plumbing, hidden from `--help`** | `agent-check`, `guard check`, `prompt`, `mcp` (the server), `preflight`, `run`, `operation-status`, `operation-reconcile`, `change`, `which` | still work; the VS Code extension, hooks, MCP hosts and agents call them; documented in `docs/agent-workflows.md` and the guide's reference section |

`which` is hidden rather than folded because the VS Code extension depends
on `which --json` (capability `which.json`).

## 8. Decisions

### 8.1 Made by the owner on 2026-10-15

| | decision |
|---|---|
| D1 | **setup scope**: config, apply, guard, create missing SSH keys, agent hooks and MCP. "it should not edit existing repos on setup. scan should present what is wrong and how to fix it." |
| D2 | **existing config**: bring the machine up to date; skip the identity questions, show what is missing, one yes. |
| D3 | **two remotes, two identities**: block the commit and offer a pin. `why`, `scan` and `doctor` warn; pre-commit refuses and names `gitpro pin` with the push remote. setup does not pin. |
| D4 | **reverses DECISIONS Q4**: the guard is installed by `setup`, not opt-in. `on_mismatch = "block"`, fail-open on gitpro's own errors and the `GITPRO_SKIP` / `--no-verify` bypass stay as Q4 and README §2.9 say. `[guard] hooks = false` in the config opts out, and setup then leaves the guard alone. |

### 8.2 Taken with the recommendations (2026-10-15)

| | question | decision |
|---|---|---|
| O1 | Which remote does the pin fix name in an ambiguous repo? | The push remote, as `git push` finds it (§3.2). With none, the fix names the choice and has no argv. |
| O2 | Keys created by setup: passphrase? | `ssh-keygen` asks for one, as `keys --generate` does; `setup --no-passphrase` skips it. Without a terminal, setup refuses to create keys unless `--no-passphrase` is given. |
| O3 | Which agents does setup configure? | Claude Code, when `~/.claude` exists or `claude` is on PATH, and not when the gitpro plugin is enabled. Others only with `--agent NAME`; `--agent none` sets up none. |
| O4 | Does `--write` go away after 0.4? | It stays accepted through 0.4.x and changes nothing (setup always offers to apply). The notice comes with S5. |
| O5 | Is an ambiguous repo also blocked by the agent hook? | Yes: same verdict, same block (C5). |
| O6 | Does `doctor` stay a separate command? | Yes. |

## 9. Regression safety

- **Generated files**: for an unchanged config, `setup` writes byte-identical
  files to 0.3's `apply`. A test renders every fixture config through both
  paths and compares.
- **Old commands and flags**: every 0.3 command and flag still runs with the
  same result through 0.4.x. Tests in 0.3 that call them stay unchanged and
  must keep passing.
- **JSON**: existing `--json` fields keep their names and meaning. The
  verdict adds `state` and `candidates`; nothing is renamed.
- **Hot path**: the hooks still dispatch through `hot.py` without importing
  `rich` or `cli.py`, within the 150 ms target (README §2.9). Computing
  `ambiguous` uses the remotes the hook already reads.
- **What newly blocks**: only `ambiguous` repos without a pin. On the owner's
  machine that is 0 of 191 repos today. `gitpro scan` lists them before the
  guard is installed, so setup's preview can say how many repos would block.
- **Existing configs** load unchanged. A config with `[guard] hooks = false`
  written explicitly keeps the guard off.

## 10. Steps (one at a time, each its own PR with tests first)

| step | content | user-visible change |
|---|---|---|
| S1 | `resolve.verdict` with the `ambiguous` state; `why`, guard pre-commit, agent hook and MCP use it; `pin --remote`. | B3 and B4 fixed. Ambiguous repos block with the pin fix. |
| S2 | Machine engine: the six components behind one interface; `doctor` and `check` move onto it. | None (refactor; byte-identical output tested). |
| S3 | `setup` converges the machine: first run and later runs, one preview, one yes, `-y`. Guard on by default (D4). | B1, B2 fixed. |
| S4 | `scan` reports by verdict state with a fix per section. | §6. |
| S5 | Consent grammar: `-y` everywhere after the subcommand, `--write` folded in with its notice. | B5 fixed. |
| S6 | Command surface: help groups, fold notices, hidden plumbing; guide, README, site, skill and VS Code updated to the two-step story. | B6 fixed. |
| S7 | Release 0.4.0 through spec 20. | |

S1 stands on its own and fixes the only correctness gap, so it goes first
regardless of the rest.

**As done (2026-10-15).** The owner asked for one PR. It holds S1, S3, the
scan sections of S4 (§6) and the S6 doc changes (guide, README, site, skill,
VS Code). Deferred to later PRs: the consent grammar for the other commands
(§5, S5), the machine engine refactor (S2) and the verdict refactor behind
`resolve.verdict` (S1 adds the `ambiguous` problem code to `resolve.compare`
instead), `setup --check`, moving doctor, check and uninstall onto shared
components, and the S6 help groups and fold notices.
