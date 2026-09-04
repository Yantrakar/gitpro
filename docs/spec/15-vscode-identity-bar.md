# 15 — VS Code: identity status bar, capabilities, and ownership of the extension's shared files

Wave 2. Depends on 01 (`why --json`, `check --json`, `--version --json`
with `capabilities`). Everything from 02/03/07/12/13/16 that touches the
extension is **declared here** and implemented behind a stub; those
packages replace their stub file only.

Files this package may create or edit: `vscode/package.json`,
`vscode/src/extension.ts`, new `vscode/src/features.ts`,
`vscode/src/identityBar.ts`, `vscode/src/capabilities.ts`, stubs
`vscode/src/roots.ts`, `lost.ts`, `clone.ts`, `devcontainer.ts`, tests
`vscode/src/test/identityBar.test.ts`, `capabilities.test.ts`,
`vscode/src/test/integration/*` (new directory; `suite.ts` moves there and
gains one file per feature so later packages add a file, not a hunk),
`vscode/README.md` is 17's.

## 1. Purpose and user story

The status bar today shows the effective email, or a warning when none.
It does not know what the identity **should** be, which SSH key is in
play, or whether the repo is mismatched. This package makes the status bar
the always-visible guard: identity + key, red when `why` reports a
problem, one click to the fix. It also owns the extension's shared
surface so five other packages can build in parallel.

User story: "The status bar in `~/work/app` reads `$(account) work-github
· id_work`. After I accidentally `git config user.email …`, it turns red:
`$(error) old@personal.test ≠ work-github`. I click it, pick `Unset
user.email in .git/config`, and it is green again."

## 2. Shared files this package owns

### 2.1 `package.json` contributions (declared once, here)

| contribution | for | notes |
|---|---|---|
| command `gitpro.which` (status bar click) | 15 | existing id |
| command `gitpro.scanRoots` "gitpro: Choose scan roots…" + `commandPalette` + Explorer folder context menu entry | 02 | |
| view container `gitpro` (activity bar), view `gitpro.lost` "Lost Work", commands `gitpro.lostRefresh`, `gitpro.lostOpenRepo`, `gitpro.lostCopyFix`; `view/title` and `view/item/context` menus | 03 | |
| command `gitpro.clone` "gitpro: Clone…" | 07 | |
| command `gitpro.copyEnv` "gitpro: Copy env for terminal" | 12 | |
| command `gitpro.devcontainerSetup` "gitpro: Set up devcontainer pin" | 13 | |
| command `gitpro.doctor` "gitpro: Doctor" | 16 | |
| settings `gitpro.statusBar.showKey` (bool, true), `gitpro.statusBar.refreshSeconds` (int, 0), `gitpro.lost.showOnStartup` (bool, false) | 15/03 | |
| `activationEvents`: `onStartupFinished`, `onView:gitpro.lost` | | |

Commands whose CLI capability is missing are still registered (so the
palette entry exists) and show `gitpro <ver> has no '<cap>'; update: pipx install --force
git+https://github.com/sandeepshelke/gitpro.git` when invoked.

### 2.2 `features.ts`

```ts
import { registerRoots } from "./roots";
import { registerLost } from "./lost";
import { registerClone } from "./clone";
import { registerDevcontainer } from "./devcontainer";

export function registerFeatures(context: vscode.ExtensionContext, cli: Cli, caps: Capabilities, rescan: () => void): void {
  registerRoots(context, cli, rescan);
  registerLost(context, cli, caps);
  registerClone(context, cli);
  registerDevcontainer(context, cli);
  context.subscriptions.push(vscode.commands.registerCommand("gitpro.copyEnv", () => copyEnv(cli)));   // 12: ~10 lines, lives here
  context.subscriptions.push(vscode.commands.registerCommand("gitpro.doctor", () => runDoctor(cli)));  // 16: runs `doctor` into the log and shows it
}
```

`extension.ts` calls `registerIdentityBar(context, cli, caps)` and
`registerFeatures(...)` once each. Later packages never edit
`extension.ts`, `features.ts` or `package.json`.

### 2.3 Stubs

`roots.ts`, `lost.ts`, `clone.ts`, `devcontainer.ts` each export the
function named above with the documented signature and a body that
registers the command(s) with a `vscode.window.showInformationMessage("gitpro: <feature> is not built yet")`
handler (so the palette entries work and the test suite compiles). The
signatures are the contract:

```ts
export function registerRoots(context, cli, rescan: () => void): void;          // 02
export function registerLost(context, cli, caps: Capabilities): void;          // 03
export function registerClone(context, cli): void;                             // 07
export function registerDevcontainer(context, cli): void;                      // 13
```

### 2.4 `capabilities.ts`

```ts
export type Capabilities = { version: string; has(cap: string): boolean };
export async function loadCapabilities(cli: Cli): Promise<Capabilities>;   // runs `gitpro --version --json` once; on failure version "?" and has() false
export function parseCapabilities(stdout: string): Capabilities | null;
```

No minimum version (DECISIONS Q12): each feature asks `caps.has("why")`,
`caps.has("lost")`, … and degrades. `gitpro --version` without `--json`
support (an old CLI) → `version "0.1.0"`, no capabilities, status bar in
the `gitpro?` state with `update: pipx install --force
git+https://github.com/sandeepshelke/gitpro.git`.

### 2.5 Integration test layout

`vscode/src/test/integration/index.ts` (runner) loads every
`*.integration.ts` in the directory. This package writes `index.ts`,
`helpers.ts` (temp repo, `GI_GITPRO` wrapper, `GITPRO_CONFIG`,
`GIT_CONFIG_GLOBAL`) and `identityBar.integration.ts`. 02, 03, 13 add
their own file; nobody edits another's.

## 3. Status bar item (same command id `gitpro.which`)

Data source: `gitpro why --json <dir>` when `caps.has("why")`, falling
back to `gitpro which --json <dir>` when `why` exits with `no config
found` (effective email only, as today).

| state | text | background | tooltip |
|---|---|---|---|
| match | `$(account) <id> · <key basename>` (no key: `$(account) <id>`; default identity: `$(account) <email>`) | none | table: expected / effective / key / signing / remotes / `pinned` / guard (06 status, from `why --json`'s `guard` key when present) / policy (14) |
| problem | `$(error) <effective email or "no identity"> ≠ <expected id>` | `statusBarItem.errorBackground` | problems as `✗ message` lines + `→ fix` lines |
| no identity | `$(warning) no git identity` | `warningBackground` | as today + `→ gitpro scan --suggest --repo …` |
| not a repo | hidden | | |
| gitpro missing / no `why` capability | `$(account) gitpro?` | warning | install/update line |

Tooltip is a `MarkdownString` (trusted: false; values escaped). Refresh
triggers as today plus terminal close, `**/.git/git-identities` and the
config file (watcher created with `new vscode.RelativePattern(dirname,
basename)` on the path from `why --json`'s `config.path`; 01 adds that
key). Inside a container (13's env list, duplicated as a constant here)
with `no_identity`, the tooltip's fix line reads `on the host, run: gitpro
pin <path> — or set it up once: gitpro devcontainer`.

Multi-root: the directory is the active editor's folder; with no active
editor, the most recently active workspace folder, else the first. The
tooltip lists the other folders with their identity (one `why --json`
per folder, cached 30 s).

## 4. Click: quick pick of actions

- `override` problem → `Unset user.<key> in .git/config`: runs the exact
  `fix` argv from `why --json` (01 provides `fix_argv`, a list) — never a
  reconstructed command — after a modal naming it; info message with
  `Undo` when the output has an `undo` path.
- `pin_stale` → `Re-pin this repo` (`pin <dir>`).
- `not_applied` → `Apply config` (`gitpro.apply`).
- `no_identity` → `Suggest an identity…` (`scan --suggest --json --repo
  <dir>` → untitled TOML doc).
- always: `Explain (gitpro why)`, `Copy env for terminal` (if
  `caps.has("env")`), `Run doctor` (if `caps.has("doctor")`), `Open
  config`, `Show Log`.

`gitpro.openConfig` gains `Create with discovery` when the file does not
exist and `caps.has("init.discover")`: runs `init --discover <file>` in a
terminal (it may ask questions). `gitpro.apply` uses `check --json`
(`up_to_date`) instead of matching stdout text (known issue 8).

## 5. CLI changes needed

None beyond 01: `why --json` with `config.path`, `fix_argv` per problem,
and optional `guard`/`policy` keys; `--version --json`.

## 6. Failure modes

| situation | behaviour |
|---|---|
| `why --json` exits 2 with `not a git repository` | hide the item |
| `why` fails otherwise | fall back to `which --json`; log warn once per dir |
| JSON parse failure | `gitpro?` state, log error |
| `why` slow (> 5 s) | keep the previous state; log warn |
| a feature's CLI capability missing | palette entry shows the update message |
| command cancelled | nothing, log info |

## 7. Security / privacy

The extension runs only `gitpro` and `git` with argv arrays. Tooltips
show emails, key paths, remote URLs — nothing secret. `copyEnv` copies
token *commands*, not values (12). Quick actions run only argv the CLI
itself proposed.

## 8. Out of scope

SCM view decorations, inline commit-author warnings, Windows.

## 9. Acceptance tests

node:test (`identityBar.test.ts`, `capabilities.test.ts`; pure functions
over `why --json` fixtures):

1. `stateFrom(json, showKey)`: match → `$(account) work-github · id_work`;
   `showKey=false` → no key; default identity → email; `override` →
   `$(error) old@personal.test ≠ work-github`; `no_identity` → warning.
2. `tooltipFrom(json)` contains `expected`, `effective`, each `✗` and `→`
   line, `pinned`, and escapes `<`, `_`, `*`.
3. `actionsFor(json, caps)`: override → `Unset user.email in .git/config`
   whose argv equals the fixture's `fix_argv`; `pin_stale` → `Re-pin`;
   `not_applied` → `Apply config`; clean → only the always-items; `env`
   item absent when `caps.has("env")` is false.
4. `parseCapabilities('{"version":"0.2.0","capabilities":["why","lost"]}')`
   → `has("why")` true, `has("clone")` false; `parseCapabilities("gitpro
   0.1.0")` → null.
5. `pickFolder(activeEditorUri, lastActive, folders)` → active editor's
   folder, else last active, else first.
6. `inContainer(env, fileExists)` true for each of `REMOTE_CONTAINERS`,
   `CODESPACES`, `DEVCONTAINER`, `/.dockerenv`.

Integration (`identityBar.integration.ts`, env `GI_GITPRO`, `GI_TEST_REPO`,
temp `GIT_CONFIG_GLOBAL`, config via `GITPRO_CONFIG`):

7. Status bar text equals `$(account) <id> · <key>` for the test repo;
   after `git config user.email wrong@x` and a refresh, text starts with
   `$(error)` and the tooltip contains `config --unset`.
8. With `GI_GITPRO` pointing at a wrapper whose `--version --json` lists
   no `why` capability, the status bar shows `gitpro?` and `gitpro.plan`
   still runs; every command in §2.1 is registered
   (`vscode.commands.getCommands`).
9. Clicking (`gitpro.which`) shows the quick pick; selecting the unset
   action runs the `fix_argv` (asserted via the wrapper's log), after which
   the text returns to `$(account)`.
10. `gitpro.apply` with an up-to-date config shows the info message,
    never the modal.
11. Multi-root: two folders, no active editor → last-active folder's
    identity; tooltip lists both.
12. Stub commands (`gitpro.clone` etc.) exist and show the "not built
    yet" message without throwing.

pytest (01's file): `why --json` contains `config.path` and `fix_argv`.

## 10. Modules touched

See the file list at the top. `vscode/README.md` text below is copied by
17.

## 11. Docs text (for 17, `vscode/README.md`)

> The status bar shows the identity id and SSH key of the repo you are
> in. It turns red when the repo would commit as someone else (a repo-local
> `user.email`, a stale pin, an unapplied config) and one click offers the
> fix. Hover for the full picture: expected vs effective, key, signing,
> remotes, guard. Features appear as your gitpro version supports them
> (`gitpro --version --json` lists them).
