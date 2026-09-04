# 13 — devcontainer integration: run `pin` on the host automatically

Wave 4. Depends on 01 (`resolve`, `jsonfile.read/edit_json` with
`jsonc=True`, `undo.write`) and 15 (the `devcontainer.ts` stub and the
`gitpro.devcontainerSetup` contribution).

Files this package may create or edit: new `src/gitpro/devcontainer.py`,
`tests/test_devcontainer.py`, `vscode/src/test/devcontainer.test.ts`;
replace `vscode/src/devcontainer.ts` (15's stub) wholesale. Nothing else.

## 1. Purpose and user story

Inside a devcontainer the host's `~/.gitconfig-*` files do not exist, so
the identity is gone unless the repo was pinned. `gitpro pin` already
solves that; people forget to run it. `devcontainer.json` has
`initializeCommand`, which runs **on the host** before the container
starts. gitpro proposes a line for it that runs `gitpro pin` when gitpro
exists on the host and does nothing otherwise, so every container start
refreshes the pin.

`devcontainer.json` is a **tracked team file** (DECISIONS Q5, Q8): the
default is print-only. `--write` edits it after a warning, with backup and
undo, and never creates it.

User story: "`gitpro devcontainer` shows me the line. I paste it (or run
`--write -y` because this is my own repo). From then on, opening the repo
in a container always has the right identity, and a colleague without
gitpro is not affected."

## 2. CLI surface

```
gitpro devcontainer [DIR] [--write] [-y] [--json]
gitpro devcontainer status [DIR] [--json]
```

### 2.1 The line

Array form, so it works on hosts without gitpro and without a shell
`||` that would hide other failures:

```json
"initializeCommand": ["sh", "-c", "command -v gitpro >/dev/null 2>&1 && gitpro pin \"$1\" || true", "sh", "${localWorkspaceFolder}"]
```

The workspace path is passed as `$1`, never interpolated into the shell
string, so paths with spaces or quotes are safe. `${localWorkspaceFolder}`
is expanded by the devcontainer CLI on the host.

Without `--write` (default):

```
~/work/app/.devcontainer/devcontainer.json  (tracked: committed to the repo)
add this key:
  "initializeCommand": ["sh", "-c", "command -v gitpro >/dev/null 2>&1 && gitpro pin \"$1\" || true", "sh", "${localWorkspaceFolder}"]
→ this file is shared with your team; gitpro does not edit it by default. Own repo? gitpro devcontainer --write ~/work/app
→ in the container, 'gitpro which' shows the pinned identity; signing and ssh keys are not pinned (see 'gitpro pin')
```

An existing `initializeCommand` (`devcontainer.propose(existing) ->
new_value`):

| existing | proposed |
|---|---|
| absent | the array above |
| string `"npm ci"` | object `{"gitpro": [array above], "npm": "npm ci"}` (object form runs entries in parallel; the key for the old value is its first word, or `cmd` if that collides) |
| array `["npm", "ci"]` | object `{"gitpro": [array], "cmd": ["npm", "ci"]}` |
| object | same object with a `gitpro` key added first |
| any value already containing `gitpro pin` | unchanged → `✓ already set up` |

(The `;`-joined string form is not used: a string `initializeCommand` is
run through the host shell and quoting the existing command safely is not
possible without evaluating it.)

No `devcontainer.json` → `✗ no devcontainer.json in <dir> (looked at
.devcontainer/devcontainer.json, .devcontainer.json); create one with the
Dev Containers extension or the devcontainer CLI first`, exit 2. gitpro
never creates the file (an image/features choice is the user's).

### 2.2 `--write`

Locates the file (`DIR/.devcontainer/devcontainer.json`, else
`DIR/.devcontainer.json`); prints the warning `⚠ <file> is tracked by git
and shared with your team` when `git ls-files --error-unmatch` succeeds
(untracked → no warning); reads with `jsonfile.read(path, jsonc=True)`;
writes with `jsonfile.edit_json(path, mutate, command="devcontainer",
jsonc=True, confirm=…)` (01): JSONC without comments is re-serialised with
the file's indentation; a file **with comments** is refused by `edit_json`
(01's rule: it will not drop comments) with `✗ <file> has comments; add
the key by hand (printed above)`, exit 2. Preview diff, confirm, backup
`~/.local/state/gitpro/backup-<ts>/devcontainer.json` (01 §5.7; never
inside the repo, so nothing new shows in `git status`), atomic write, undo script
`devcontainer-undo-<ts>.sh` via `undo.write`.

```
⚠ ~/work/app/.devcontainer/devcontainer.json is tracked by git and shared with your team
  backup  ~/.local/state/gitpro/backup-20261002-101500/devcontainer.json
  update  ~/work/app/.devcontainer/devcontainer.json   (+ initializeCommand)
write it? [y/n]
✓ written; the next container start runs gitpro pin on the host
↩ undo: sh ~/.local/state/gitpro/devcontainer-undo-20261002-101500.sh
→ also pin now: gitpro pin ~/work/app
```

### 2.3 `devcontainer status`

```
~/work/app
  devcontainer.json  .devcontainer/devcontainer.json   initializeCommand runs gitpro pin ✓
  pin                .git/git-identities  Work Me <me@corp.test>  up to date ✓
  inside container   no
```

`devcontainer.in_container() -> bool` is the one shared detection
(`REMOTE_CONTAINERS`, `CODESPACES`, `DEVCONTAINER` env, `/.dockerenv`);
15's extension uses the same list in `features.ts`. Inside a container
with no pin: `✗ not pinned and no host config: run on the host: gitpro
pin <path>` (exit 1). Pin stale (01 `pin_stale`): `⚠ pin is stale →
gitpro pin <path>` (exit 1).

Capabilities registered: `devcontainer`, `devcontainer.write`.

## 3. `--json`

`devcontainer --json`: `{"ok": true, "command": "devcontainer", "file":
"/abs/.devcontainer/devcontainer.json"|null, "tracked": bool,
"has_pin": bool, "existing_command": <value>|null, "proposed": <value>,
"has_comments": bool, "written": false}`. With `--write -y`: `"written":
true, "backup": "...", "undo": "..."`. `status --json`: `{"ok",
"command": "devcontainer status", "file", "has_pin", "pinned",
"pin_stale", "in_container", "problems": [...]}`.

## 4. Config keys

None.

## 5. VS Code surface

15 declares `gitpro.devcontainerSetup` ("gitpro: Set up devcontainer pin")
and calls `registerDevcontainer(context, cli)` from `./devcontainer`. This
package replaces the stub and exports:

```ts
export function registerDevcontainer(context: vscode.ExtensionContext, cli: Cli): void;
export function proposeLine(existing: unknown): unknown;   // mirrors devcontainer.propose
```

- Command: runs `devcontainer --json` for the active repo; shows the
  proposed value in a modal `Add initializeCommand to <file>?` with
  `Copy` (clipboard) and, when `tracked` is false or the user confirms a
  second `This file is shared with your team. Write anyway?` modal,
  `Write` → `devcontainer --write -y --json`; opens the file; logs the
  undo script. `has_comments` → only `Copy` is offered.
- When the extension is inside a container (`in_container` list) and
  `which` shows no identity, 15's status-bar fix hint reads `on the host,
  run: gitpro pin <path> — or set it up once: gitpro devcontainer`.
- When a workspace folder has a `devcontainer.json` without `gitpro pin`
  and the repo has an identity: one information message per workspace per
  session, `This repo has a devcontainer; add gitpro pin to
  initializeCommand so the container keeps your identity` with `Show` /
  `Don't ask again` (`workspaceState`). Never writes on its own.
- The printed snippet and the extension's `Show` view end with the note
  (V-030): `initializeCommand runs on the host before the container
  starts; this array form needs sh on the host (Linux, macOS, WSL). A
  colleague on Windows without WSL gets a "sh not found" error from the
  Dev Containers extension — they can remove the line; it is harmless
  for them because gitpro is Linux/macOS only.` The same note is in
  `gitpro devcontainer --help` and in 17's docs text.

## 6. Files created/changed and undo

Only with `--write`: `<repo>/.devcontainer/devcontainer.json` (or
`.devcontainer.json`), one key; backup first; undo restores the backup.
Not part of `gitpro uninstall` (repo content).

## 7. Failure modes

| situation | message | exit |
|---|---|---|
| not a repo | `✗ <dir>: not a git repository` | 2 |
| no devcontainer.json | `✗ no devcontainer.json in <dir> …` | 2 |
| JSONC unparsable / top level not an object | `✗ <file>: cannot parse (<error>); add the key by hand: …` | 2 |
| file has comments and `--write` | `✗ <file> has comments; add the key by hand` | 2 |
| `--write` without tty/`-y` | `no terminal to confirm on` | 2 |
| read-only file | OSError message | 2 |

## 8. Security / privacy

The command written is a fixed array; the path travels as an argument.
The existing command is kept verbatim; gitpro never evaluates it. Nothing
leaves the machine.

## 9. Out of scope

Running `pin` inside the container (the host config is not there),
Codespaces (no host; users set name/email in Codespaces settings),
mounting `~/.gitconfig-*` into containers, creating `devcontainer.json`.

## 10. Acceptance tests (`tests/test_devcontainer.py`, `vscode/src/test/devcontainer.test.ts`)

1. Repo without devcontainer → exit 2 with `no devcontainer.json`, `--write`
   too; nothing created.
2. Existing `{"name": "x", "image": "y"}` (committed) → default run prints
   the array line, the `shared with your team` sentence, and `tracked:
   true` in JSON; file unchanged. `--write -y` → `⚠ … tracked` on stderr,
   key added, other keys intact, indentation kept, backup equals the
   original, undo restores it; the value is the array and `json.load`
   round-trips it.
3. Untracked file → no `⚠ tracked` line; `--write -y` works.
4. `propose`: string `"npm ci"` → object `{"gitpro": [...], "npm": "npm
   ci"}`; array → `{"gitpro", "cmd"}`; object → `gitpro` key first;
   existing `gitpro pin` in a string/array/object → `already set up`,
   `has_pin: true`, no write.
5. File with `// comment` → default run parses and prints; `--write -y` →
   exit 2 `has comments`, file byte-identical, `has_comments: true` in
   JSON.
6. Unparsable file (`{"a": }`) → exit 2, file untouched.
7. `status` on a pinned, set-up repo → three lines, `--json` `has_pin`
   and `pinned` true, exit 0; `REMOTE_CONTAINERS=true` and no pin → exit
   1, message names `gitpro pin`; `in_container()` true for each env var
   in the list and for a monkeypatched `/.dockerenv` check.
8. `status` after the config email changes and `apply -y` → `pin_stale`
   true, exit 1.
9. The written array, run through `sh -c` with `$1` = a path containing a
   space and a `'`, with a fake `gitpro` on `fake_bin` → the fake logs
   `pin <that path>` exactly; without `gitpro` on PATH → exit 0, no
   output.

node:test (`devcontainer.test.ts`): `proposeLine(existing)` for
string/array/object/undefined mirrors the four cases above (expected
values listed as data in the test so both sides agree).

Integration (15's `suite.ts`, one case added by this package in
`vscode/src/test/integration/devcontainer.integration.ts`): with a temp
repo containing `.devcontainer/devcontainer.json`, command
`gitpro.devcontainerSetup` is registered and `devcontainer --json`
returns `has_pin: false`.

## 11. Modules touched

New `devcontainer.py` (`locate(repo)`, `propose(existing)`,
`in_container()`, `write(...)`, `status(repo, cfg)`, `register`),
`vscode/src/devcontainer.ts` (replaces the stub).

## 12. Docs text (for 17)

> Containers do not see the host's gitpro files. `gitpro devcontainer`
> prints the `initializeCommand` entry that runs `gitpro pin` on the host
> before every container start (and does nothing on hosts without gitpro).
> `devcontainer.json` is shared with your team, so gitpro only prints it;
> `--write` edits it with a backup and undo. `gitpro devcontainer status`
> shows the pin state.
