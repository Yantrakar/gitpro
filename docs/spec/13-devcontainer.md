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

One multi-line **string** (`devcontainer.LINE`; shown here line by line,
in the JSON file the lines are joined with `\n`):

```sh
command true || exit 0
# runs 'gitpro pin' on hosts that have gitpro, else nothing (line 1 stops Windows cmd)
command -v gitpro >/dev/null 2>&1 || exit 0
IFS= read -r dir <<'GITPRO_PATH'; gitpro pin "$dir" || true; exit 0
${localWorkspaceFolder}
GITPRO_PATH
```

Why a string and not an array (as built; this replaces the earlier
`["sh", "-c", …, "sh", "${localWorkspaceFolder}"]` design, V-030): the
devcontainer CLI runs an array directly, so it needs `sh` on every host
and fails the container build on Windows without WSL ("sh not found"),
for colleagues who never installed gitpro. A string runs in the host's
shell: `/bin/sh -c` on Linux, macOS and WSL, `cmd.exe /c` on Windows.

- Line 1 is true in sh. In cmd.exe `command` is not a command, so the
  `|| exit 0` ends cmd with 0 (the build log shows `'command' is not
  recognized as an internal or external command`, which is harmless).
  The container starts on every OS, with or without gitpro (DECISIONS Q8).
- No gitpro on the host → exit 0, no output. A failing pin is masked by
  `|| true`: the container still starts (`devcontainer status` reports it).
- The workspace path, which the devcontainer CLI substitutes for
  `${localWorkspaceFolder}` as text, is read from a quoted here-document,
  so no character in it (spaces, quotes, `$`, backticks, backslashes) is
  parsed as shell code. `gitpro pin` and `exit 0` sit on the read line
  itself: even a path holding a newline followed by a `GITPRO_PATH` line
  cannot run anything, because the script has exited before the shell
  reaches the line after the here-document's first one.

Without `--write` (default):

```
~/work/app/.devcontainer/devcontainer.json  (tracked: committed to the repo)
add this key:
  "initializeCommand": "command true || exit 0\n# runs 'gitpro pin' on hosts that have gitpro, else nothing (line 1 stops Windows cmd)\n…"
→ this file is shared with your team; gitpro does not edit it by default. Own repo? gitpro devcontainer --write ~/work/app
→ in the container, 'gitpro which' shows the pinned identity; signing and ssh keys are not pinned (see 'gitpro pin')
```

An existing `initializeCommand` (`devcontainer.propose(existing) ->
new_value`):

| existing | proposed |
|---|---|
| absent | the line above |
| string `"npm ci"` | object `{"gitpro": LINE, "npm": "npm ci"}` (object form runs entries in parallel; the key for the old value is its first word, or `cmd` if that collides) |
| array `["npm", "ci"]` | object `{"gitpro": LINE, "cmd": ["npm", "ci"]}` |
| object | same object with a `gitpro` key added first (`gitpro-pin` when `gitpro` is taken; both taken → exit 2, add it by hand under another key) |
| any value already containing `gitpro pin` | unchanged → `✓ already set up` |

When a string or array moves into the object form, the output (and the
VS Code message, and `"merged": true` in JSON) says: `your command now
runs alongside gitpro pin (in parallel); its output appears when it
finishes`.

(The existing command is never joined into our string with `;`: quoting
it safely is not possible without evaluating it, so it keeps its own
entry in the object form, verbatim.)

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
`devcontainer-undo-<ts>.sh` via `undo.write`. The file keeps its own line
endings (a CRLF file stays CRLF) and the backup holds the original bytes,
so the undo restores the file exactly; if the write fails, the backup and
undo script are removed again.

`--write` also refuses (exit 2, the snippet printed instead, `✗ <reason>;
add the key by hand (printed above)`):

- the file is a symlink whose real path is outside the repo's real top
  level (a symlink inside the repo is followed as before);
- the file is not writable (`os.access`);
- `--json` without `-y` (the question would mix with the JSON).

Every `--write` (not `--json`) warns on stderr that the line pins only on
hosts that have gitpro: every teammate who opens the container needs
gitpro on their host to get the right identity.

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
"merged": bool, "has_comments": bool, "written": false}`. With `--write -y`: `"written":
true, "backup": "...", "undo": "..."`. `status --json`: `{"ok",
"command": "devcontainer status", "file", "has_pin", "pinned",
"pin_stale", "in_container", "problems": [...]}` (also `repo`,
`pin_file`, `pin_identity`, `container_reason`; each problem has `level`,
`about` (`devcontainer.json`, `config`, `pin`), `message`, `fix`). A config
that cannot be parsed is reported as such (`the config cannot be parsed:
…`), not as "no host config". Options may sit between the words
(`devcontainer status --json DIR`).

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
  (`devcontainer.NOTE`; V-030, updated for the string form):
  `initializeCommand runs on the host before the container starts. On
  Linux, macOS and WSL this line runs gitpro pin when gitpro is installed
  and does nothing otherwise; on Windows (cmd.exe) its first line exits 0,
  so a colleague without gitpro is not affected and the container still
  starts (the build log then shows "'command' is not recognized as an
  internal or external command", which is harmless).` The same note is in
  `gitpro devcontainer --help` and in 17's docs text.
- The nudge check can be re-run with the internal (not contributed)
  command `gitpro.devcontainerNudge [DIR]`, which resolves to why it did or
  did not show per folder (`shown`, `seen`, `off`, `no-file`, `has-pin`,
  `old-gitpro`, `no-identity`); the integration test uses it.

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
| `--write --json` without `-y` | JSON error `--write --json needs -y` | 2 |
| symlink out of the repo and `--write` | `✗ <file> is a symlink to <target>, outside the repo; …add the key by hand` | 2 |
| read-only file and `--write` | `✗ <file> is read-only (make it writable to use --write); add the key by hand` | 2 |
| object with both `gitpro` and `gitpro-pin` keys | `✗ initializeCommand already has keys "gitpro" and "gitpro-pin" …` | 2 |

## 8. Security / privacy

The command written is a fixed string. The only variable part, the
workspace path, is substituted by the devcontainer CLI and read by the
shell as data from a quoted here-document, never parsed as code; pin and
`exit 0` sit on the read line, so a path with a newline and a
`GITPRO_PATH` line cannot start a command (tested in sh, dash, bash,
busybox). On Windows the first line ends cmd.exe before anything else.
The existing command is kept verbatim; gitpro never evaluates it.
`--write` never edits a file outside the repo (symlinks are resolved and
checked). Nothing leaves the machine.

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
9. The written string, with `${localWorkspaceFolder}` replaced by a path
   containing spaces, quotes, `$HOME`, backticks, `$(id)` and a
   backslash, run through `sh -c`, `dash -c`, `bash -c` and `busybox sh
   -c` with a fake `gitpro` (exiting 3) on `fake_bin` → the fake logs
   `pin <that path>` exactly and the exit is 0; without `gitpro` on PATH →
   exit 0, no output. A path with a newline and a `GITPRO_PATH` line runs
   nothing after it. `json.load` round-trips the written file; the first
   line is `command true || exit 0`.

node:test (`devcontainer.test.ts`): `proposeLine(existing)` for
string/array/object/undefined mirrors the four cases above (expected
values listed as data in the test so both sides agree).

Integration (15's `suite.ts`, one case added by this package in
`vscode/src/test/integration/devcontainer.integration.ts`): with a temp
repo containing `.devcontainer/devcontainer.json`, command
`gitpro.devcontainerSetup` is registered and `devcontainer --json`
returns `has_pin: false`. As built it also covers Write (two modals),
`already set up`, Copy-only for a commented file, and the nudge: shown
once per folder per session, and `Don't ask again` remembered.

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
