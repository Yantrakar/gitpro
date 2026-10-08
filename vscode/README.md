# gitpro for VS Code

Shows in the status bar which git identity and SSH key the current repo
commits with, turns red when it is the wrong one, and offers the fix. It also
finds repo-level overrides and lost work, clones with the right identity and
keeps the identity inside dev containers.

It needs the [gitpro](https://github.com/sandeepshelke/gitpro) CLI:

```sh
pipx install git+https://github.com/sandeepshelke/gitpro.git
```

Set `gitpro.path` if it is not on `PATH`.

## Status bar

For the repo of the file you are editing, the status bar shows one of:

| text | meaning |
|---|---|
| `$(account) work-github · id_ed25519_work` | the right identity, and the SSH key git uses (turn the key off with `gitpro.statusBar.showKey`) |
| `$(error) jane@personal.dev ≠ work-github` (red) | git commits with that email, but the remote belongs to identity `work-github`, or another problem (signing, policy) |
| `$(warning) no git identity` | no identity matches and `[default]` has none: git refuses to commit |
| `$(account) gitpro?` | gitpro was not found, or is too old to explain the identity; the tooltip says how to install or update it |

Hover for the details from `gitpro why`: the expected identity and the rule
that matched, the effective name and email with the file they come from, the
SSH key, signing, the remotes, whether the repo is pinned, the guard and
policy state, each problem with its `→` fix, and the identities of your other
workspace folders. The status bar refreshes on file, editor, focus and
terminal changes; `gitpro.statusBar.refreshSeconds` adds
a timer.

Click it for a menu:

- One fix per problem, as gitpro proposed it: **Apply config**, **Re-pin this
  repo**, **Unset user.email in .git/config**, **Set origin URL to …**,
  **Run …** or **Suggest an identity…**. Each one asks "Run this command?"
  with the exact command first. When the fix leaves an undo script, the
  notification offers **Undo**.
- **Explain (gitpro why)**: the full explanation in the log.
- **Copy env for terminal**: `gitpro env` for this repo, to paste into a
  terminal. It copies the commands that fetch tokens, never a token.
- **Run doctor**, **Open config**, **Show Log**.

In a dev container the status bar shows what git resolves inside the
container. If it warns, run `gitpro pin` in the repo on the host, or use
[Set up devcontainer pin](#dev-containers).

## Commands

Command Palette, "gitpro":

| command | runs |
|---|---|
| Show Identity for This Repo | `gitpro which` |
| Plan | `gitpro plan` |
| Apply | `plan`, then `apply --yes` after you confirm |
| Pin Identity to This Repo / Unpin Identity from This Repo | `gitpro pin` / `unpin` for the current repo |
| Open Config | opens `$GITPRO_CONFIG` or `~/.config/gitpro.toml`; offers to create it |
| Refresh Status | refreshes the status bar |
| Scan Repos for Overrides, Fix Checked, Review One by One, Suggest Identities | the [Repo Overrides](#repo-overrides) view |
| Choose Scan Roots… | the [scan roots](#scan-roots) picker |
| Find Lost Work | fills the [Lost Work](#lost-work) view |
| Clone… | [`gitpro clone`](#clone) |
| Copy env for terminal | `gitpro env --shell sh` for this repo, copied to the clipboard |
| Set up devcontainer pin | [`gitpro devcontainer`](#dev-containers) |
| Doctor | `gitpro doctor` into the log |

## Repo Overrides

A `user.name` or `user.email` in a repo's own `.git/config` beats gitpro. The
gitpro view in the activity bar lists those repos, grouped by the identity
they get once the setting is removed (`gitpro scan`). It scans when you press
Scan or Refresh, under your [scan roots](#scan-roots).

- Click a repo to open its `.git/config`; hover for each setting and what
  replaces it.
- Review One by One steps through the fixable repos ("Review app (3/15)"):
  Fix, Skip, Back, or fix or skip all the rest. Your answers set the
  checkboxes, and nothing changes until the last step's confirmation. Escape
  puts the checkboxes back as they were.
- Fix Checked removes the settings of the checked repos in one go
  (`gitpro scan --fix --repo ...`) after you confirm. The notification names
  the undo script and offers Undo. Repos where nothing else sets an identity
  have no checkbox: the fix keeps them.
- Suggest Identities opens the `[[identity]]` blocks that would keep a repo's
  current identity instead. Paste the ones you want into your config, run
  Apply, then Fix.

A malformed scan response is reported as a failed scan, with no fixable
results; restoring the CLI and scanning again recovers without reloading VS Code.

## Scan roots

The Repo Overrides view scans the `[scan] roots` from your gitpro config
(else your home directory). `gitpro.scanDirs`, when set, wins over them.
**Choose Scan Roots…** (in the view's title bar, the Command Palette and a
folder's context menu in the Explorer) opens a picker with the
roots in effect and these actions:

- **Open Config**: edit `[scan] roots` in the TOML.
- **Add Folder…**: pick another directory.
- **Add Workspace Folders**: shown when some of your workspace folders lie
  outside the roots.
- **Use for This Session**: scan the checked roots until VS Code reloads.
- **Show TOML Snippet**: the `[scan] roots = [...]` lines to paste into your
  config.

The extension never writes `gitpro.scanDirs` or the TOML itself. When a
workspace folder is outside the roots, a notice says "N workspace folders are
outside the scan roots" and offers **Add Them for This Session**.

## Lost Work

The Lost Work view lists work that exists only on this machine, from
`gitpro lost --json --offline`: unpushed branches, branches whose upstream is
gone, stashes, dirty trees, repos with no remote and diverged duplicate
clones, grouped by kind and repo. It never goes online, whatever
`[forge] online` says, and never pushes or deletes anything.

- Right-click a repo for **Open Repo**, or an item for **Copy Fix Command**,
  which copies the suggested command
  (for example `git -C ~/src/dotfiles push -u origin master`), quoted for a
  POSIX shell (sh, bash, zsh), not PowerShell or cmd.
- "Unpushed" means not on a remote-tracking branch as of your last fetch.
- Set `gitpro.lost.showOnStartup` to look when VS Code starts and show the
  view if it finds anything.

## Clone

**Clone…** asks for a URL (`git@host:owner/repo.git`, `https://host/owner/repo`
or `host/owner/repo`), shows gitpro's plan ("Clone … into ~/work/github.com/…
as work-github?") and clones only when you pick **Clone**. Progress shows in a
notification you can cancel. Cancel first asks gitpro to stop (SIGTERM),
so it can stop git and ssh and remove the partial clone. On POSIX, gitpro
gives git's process group 2 seconds to stop before killing any surviving
helpers, even if git itself has already exited. The extension escalates to
SIGKILL after 3 seconds if its child still has not closed its output. The
extension waits up to 10 seconds for gitpro to finish. If the directory is
still there afterwards, it tells you to remove it by hand. When
git needs a terminal (an ssh passphrase or credentials), the error offers
**Copy command** so you can run `gitpro clone` in a terminal instead. When it
is done, **Open Folder** opens the new repo.

## Dev containers

When a repo has a `devcontainer.json` without `gitpro pin`, the extension
says once per session: "This repo has a devcontainer; add gitpro pin to
initializeCommand so the container keeps your identity", with **Show** and
**Don't ask again**.

**Show** (or the command **Set up devcontainer pin**) shows the
`initializeCommand` entry from `gitpro devcontainer` and offers **Write** or
**Copy**. `devcontainer.json` is usually committed and shared with your team,
so **Write** asks again and explains that teammates need gitpro on their host
for the entry to do anything. If the file has comments, writing would lose
them, so only **Copy** is offered. Writing goes through
`gitpro devcontainer --write -y`, which keeps a backup and an undo script.

## Which gitpro is installed

The extension runs `gitpro --version --json` once and turns on each feature
when the CLI lists its capability (`why`, `lost`, `clone`, `devcontainer`,
`env`, `doctor`, `scan.roots` and so on). There is no minimum version: with
an older gitpro, the features it lacks say "gitpro X has no 'cap'; update:"
followed by the install command, and the rest keeps working.

## Settings

| setting | default | |
|---|---|---|
| `gitpro.path` | `gitpro` | Path to the `gitpro` command. |
| `gitpro.scanDirs` | `[]` | Directories the Repo Overrides view scans. Empty: the `[scan] roots` from your config, else your home directory. |
| `gitpro.statusBar.showKey` | `true` | Show the SSH key's file name next to the identity. |
| `gitpro.statusBar.refreshSeconds` | `0` | Also refresh the status bar every this many seconds. `0`: only on file, editor, focus and terminal changes. |
| `gitpro.lost.showOnStartup` | `false` | Look for lost work when VS Code starts, and show the Lost Work view if it finds any. |

## Log

The gitpro output channel logs every command with its directory, exit code
and time, any warnings from gitpro, what each scan found (including how
many repos it skipped and why, e.g. "30 skipped: 26 owned by root, 4 with a
broken .git link"), each review answer, and every setting a fix removes with
the undo script's path. Lines are timestamped and kept across commands. For
more, run Developer: Set Log Level... > gitpro: Debug adds each skipped repo
with its reason and checkbox changes; Trace adds gitpro's raw output.

URLs in the logged commands have any password, token or secret query value
(`access_token=`, `private_token=`, `sig=` and the like) replaced with `***`.
The commands themselves run with the real URL.

A command that is cancelled or runs past its time limit gets SIGTERM, then
SIGKILL 3 seconds later if needed. On POSIX these signals reach its process
group; on Windows only the child is stopped. When VS Code closes, the extension stops any gitpro still running,
such as a clone, in the same way, with 2 seconds' grace.

## Development

`npm run dev` (or F5 in this folder) opens a VS Code window with the extension loaded from here, without
installing anything. It runs the checkout's `../.venv/bin/gitpro` unless `gitpro.path` is set. `npm test` runs the unit tests, `xvfb-run -a npm run test:integration` the integration
test, and `npm run package` builds the `.vsix`.

The editor integration tests need the Python CLI as well as `npm ci`. From
this folder, prepare it with `python -m venv ../.venv` and
`../.venv/bin/python -m pip install -e ..`, as CI does. Alternatively, set
`GITPRO` to an installed CLI's absolute path. Tests use throwaway HOME and
repositories; no real Git configuration or remote repositories are changed.

VS Code itself prints a `[DEP0169] url.parse()` deprecation warning on `code --install-extension` and in the
extension host. It comes from VS Code's request service and `@vscode/proxy-agent`, not from this extension
([microsoft/vscode#301941](https://github.com/microsoft/vscode/issues/301941)).
