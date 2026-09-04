# 02 — Saved scan roots and excludes; VS Code folder picker and multi-root

Wave 3. Depends on 01 (the `[scan]` table, `config.load_scan`,
`cli.extend_command`, `jsonout`) and 15 (the VS Code skeleton with the
`roots.ts` stub and the `gitpro.scanRoots` contribution).

Files this package may create or edit: new `src/gitpro/scanroots.py`,
`tests/test_scan_roots.py`, `vscode/src/test/roots.test.ts`; edit
`src/gitpro/scan.py` (`find`, new `effective_roots`, `derive_id`),
`src/gitpro/view.py` (the scan header text only); replace
`vscode/src/roots.ts` (15's stub) wholesale. Nothing else. `cli.py`,
`extension.ts`, `package.json`, `features.ts` are not touched.

## 1. Purpose and user story

`gitpro scan` walks `~` unless told otherwise. People keep repos on other
disks (`/srv/git`, `/mnt/d/work`) and want to leave out big trees
(`~/.cache`, `~/go/pkg/mod`). The roots and excludes live in the TOML so
every scan (CLI, extension, MCP, `lost`, `discover`) uses the same set.

User story: "I set `roots = ["~", "/mnt/d/work"]` once. `gitpro scan`,
the VS Code Repo Overrides view and `gitpro lost` all cover both disks, and
none of them wastes a minute in `~/.cache`."

## 2. CLI surface

### 2.1 Registration

`scanroots.py` has no `register`; it has

```python
def extend(subs):
    from gitpro.cli import extend_command
    extend_command(subs, "scan", add_flags, handler)
```

`add_flags(parser)` adds `-c/--config` (same help as the `common` parent;
`scan` did not have it) and `--roots`. `handler(args)` returns `None` to
let the existing `cmd_scan` run, after setting `args.roots_effective`
(the result of `effective_roots`) and `args.exclude`; it returns an int
for `--roots`, which prints and exits. 01 §2.1 defines `extend_command`:
the handler runs first and the original handler runs only when it returns
`None`. `cmd_scan` (01 §13) reads `args.roots_effective` when present.
`--home` is accepted and ignored by `scan` (known issue 3 in 00).

### 2.2 Root selection

```python
# scan.py
def effective_roots(args_roots: list[str], cfg_scan: "Scan | None") -> tuple[list[Path], list[str], str]:
    """-> (roots, exclude, source) with source in {"args", "config", "default"}."""
```

Order:

1. `--repo PATH…` → exactly those, no discovery (as today); `effective_roots`
   is not consulted.
2. `DIR…` positional → those roots, `source = "args"`.
3. `[scan].roots` when a config is found and loads, `source = "config"`.
4. `~`, `source = "default"`.

The config is read with `config.load_scan(path)` (01 §3.3): it returns the
`[scan]` table only and is **lenient**: a config that fails full
validation (placeholders left after `init`, an identity missing a key)
still yields `[scan]` when that table itself is valid; a bad `[scan]`
table or TOML syntax error prints `⚠ <path>: <error>; scanning ~` and
falls through to 4. A config that cannot be found is the same as no
config. So `gitpro init && gitpro scan` exits 0 (reviewer R-015/R-016).

`exclude` applies to 2 and 3, never to `--repo`. `scan.find` gains keyword
arguments:

```python
def find(roots, *, exclude=(), max_depth=None) -> list[Path]
```

Exclude matching: each pattern is normalised once: a leading `~/` is
replaced by the home directory, a relative pattern (no leading `/`) gets
`**/` prefixed. The absolute path of every directory the walk enters is
matched against each pattern with `fnmatch.fnmatchcase` after replacing
`**` with `*` for fnmatch purposes (fnmatch's `*` already crosses `/`).
Examples, home `/home/me`:

| pattern | normalised | excludes | does not exclude |
|---|---|---|---|
| `~/.cache` | `/home/me/.cache` | `/home/me/.cache` | `/home/me/.cache2`, `/home/me/x/.cache` |
| `**/vendor` | `**/vendor` | `/srv/git/a/vendor`, `/home/me/vendor` | `/srv/git/vendor-x` |
| `node_modules` | `**/node_modules` | every `node_modules` dir | — |
| `/mnt/d/work/old*` | `/mnt/d/work/old*` | `/mnt/d/work/old`, `/mnt/d/work/old-2024` | `/mnt/d/work/x/old` |

Excluded directories are not descended into, and a repo that *is* the
excluded directory is not listed. Hidden directories and `node_modules`
are always skipped (as today), before exclude patterns are consulted, so
`skipped_excluded` counts only directories a pattern excluded. A root that
is itself excluded is skipped with `⚠ root /x is excluded by '<pattern>';
skipped`. `max_depth` (used by 04) stops the walk `N` levels below each
root; `None` means unlimited.

`scan.derive_id(owner, host, taken) -> str` is the id rule already used
by `scan.suggest` (00 §4.7, item 15), made a public function so 04 can
reuse it. Behaviour unchanged.

### 2.3 `--roots`

Prints the effective roots and excludes and exits 0:

```
roots (from ~/.config/gitpro.toml):
  ~
  /mnt/d/work
exclude:
  ~/.cache
  **/node_modules  (always)
  ~/go/pkg/mod
```

`(from command line)` / `(default)` when not from the config. With
`--json`: `{"ok": true, "command": "scan", "roots": [...abs], "exclude":
[...], "source": "config"|"args"|"default"}`.

### 2.4 Other output changes

Header line (`view.scan`): `N repos under <roots>  hidden directories,
node_modules and 2 excluded dirs skipped` (the `and N excluded dirs` part
appears only when N > 0). The scan JSON gains `"exclude": [...]`,
`"roots_source": "config"|"args"|"default"` and `"skipped_excluded": N`;
`roots` stays.

A missing root: `⚠ root does not exist: /mnt/d/work` on stderr, the scan
continues with the others; `✗ no scan root exists (looked at: /a, /b)`
exit 2 when none exists.

A dry-run footer suggestion when roots came from the command line and a
config exists: `→ to keep these roots: add to <config>: [scan] roots =
["~", "/mnt/d/work"]` (printed once; gitpro never edits the TOML).

Capabilities registered (01 §2.3): `scan.roots`.

## 3. `--json` schema

Additive to the existing `scan --json` (00 §4.7): `exclude`,
`roots_source`, `skipped_excluded` (count). `--roots --json` as in 2.3.

## 4. Config keys

`[scan] roots`, `exclude` (01 validates; `exclude` strings are stored as
written and normalised at use). `stale_days` is for 03.

## 5. VS Code surface

15 already declares in `package.json`: command `gitpro.scanRoots` ("gitpro:
Scan Roots…"), a view-title menu entry for it on `gitpro.overrides`, and
the existing `gitpro.scanDirs` setting. 15's `features.ts` calls
`registerRoots(context, cli, rescan)` from `./roots`. This package replaces
`vscode/src/roots.ts` with the real implementation and exports exactly:

```ts
export function effectiveRoots(cli: Cli): Promise<{roots: string[]; source: string}>;
export function foldersOutside(roots: string[], folders: readonly string[]): string[];
export function registerRoots(context: vscode.ExtensionContext, cli: Cli, rescan: () => void): void;
```

Behaviour:

- `gitpro.scanDirs` stays and, when non-empty, is passed as positional
  roots (overrides the TOML, as today). When it is empty, **no roots are
  passed**, so the CLI uses the TOML.
- **Scan Roots…** quick pick lists the effective roots (from `scan --roots
  --json`), each with a checkbox, plus `Add folder…` (`showOpenDialog`,
  `canSelectMany`) and `Add workspace folders`. Confirming offers two
  actions: `Use for this session` (keeps the chosen roots in memory for
  the extension's scans until reload) and `Show TOML snippet` (opens an
  untitled TOML document with `[scan]\nroots = [...]` for the user to paste).
  The extension does **not** write `gitpro.scanDirs` or the TOML (reviewer
  R-017: a settings write would silently override the TOML forever). A
  non-selectable header line shows `Config: ~/.config/gitpro.toml` with an
  `Open config` button.
- **Multi-root workspaces**: the Repo Overrides view message lists all
  roots: `3 repos under ~, /mnt/d/work · 1 skipped: owned by root`. When
  the workspace has folders outside every effective root, the view shows one
  welcome line `2 workspace folders are outside the scan roots · [Add them
  for this session]`.

Log (info): `roots: ~, /mnt/d/work (config)`.

## 6. Files created/changed and undo

CLI: none. Extension: none (session state and an untitled document only).

## 7. Failure modes

| situation | message | exit |
|---|---|---|
| no root exists | `✗ no scan root exists (looked at: /a, /b)` | 2 |
| exclude pattern excludes a root | `⚠ root /x is excluded by '<pattern>'; skipped` | continue |
| config fails full validation but `[scan]` is fine | roots from `[scan]`, no message | 0 |
| `[scan]` invalid or TOML unparsable | `⚠ <path>: <error>; scanning ~` | 0 |
| no config and no args | scan `~` silently (as today) | 0 |

## 8. Security / privacy

Read-only. Roots outside home are walked only when listed by the user.
Paths from the config are never passed to a shell.

## 9. Out of scope

Following symlinks; bare repos (unchanged); stale detection (03); the TUI
layout; writing settings.

## 10. Acceptance tests (`tests/test_scan_roots.py`, `vscode/src/test/roots.test.ts`)

pytest (use `home`, `write_config`, `repo_with_remote`, `run`):

1. Config `[scan] roots = ["<tmp>/a", "<tmp>/b"]`, repos in both → `scan
   --json` lists both repos, `roots_source == "config"`.
2. Positional `DIR` with a config present → `roots_source == "args"`, only
   that root; dry-run footer contains `[scan] roots = [`.
3. No config, no args → `roots == [str(home)]`, `roots_source == "default"`.
4. `exclude = ["**/vendor"]`, repo under `a/vendor/x` → not listed,
   `skipped_excluded == 1`; header says `1 excluded dir`.
5. `exclude = ["~/.cache"]` with a repo in `home/.cache/r` → hidden dirs are
   skipped anyway; `skipped_excluded == 0`.
6. `exclude = ["<tmp>/a/old*"]` → `<tmp>/a/old-2024/r` excluded,
   `<tmp>/a/x/old/r` listed (pattern anchoring per 2.2).
7. `--repo PATH` ignores excludes: repo under `vendor` is inspected.
8. Non-existent root among two → warning on stderr, exit 0, other root
   scanned. All roots missing → exit 2 with `no scan root exists`.
9. `scan --roots` text output lists `**/node_modules  (always)`; `--roots
   --json` has `source`.
10. `scan -c <path>` is accepted and uses that config's roots.
11. `run("init", path)` then `run("scan", "-c", path)` (placeholders still
    in the file) → exit 0, roots default, no `✗` on stderr.
12. Config with a valid `[scan]` but an identity missing `email` → roots
    from `[scan]`, exit 0; `[scan] roots = "x"` (not a list) → `⚠` line
    with the path, scans `~`, exit 0.
13. `scan.find(roots, max_depth=1)` does not list a repo two levels down.
14. Existing `test_scan.py` and `test_end_to_end.py` scan tests pass
    unchanged.

node:test (`roots.test.ts`, pure functions):

15. `effectiveRoots` with a fake `cli` returning `{"roots": [...],
    "source": "config"}` → those roots; with `scanDirs` non-empty (passed
    in) → the setting wins.
16. `foldersOutside(roots, workspaceFolders)` returns folders not under any
    root (prefix match on path segments, `/home/me/src` is not under
    `/home/me/s`).

Integration (15's `suite.ts` already runs with `GI_SCAN_DIR` as a workspace
folder): with `gitpro.scanDirs` empty, the view message contains the
folder. This package adds that one assertion to
`vscode/src/test/integration/suite.ts` only if 15 left a `// 02: roots`
marker; otherwise it is covered by 15's generic "view message shows roots"
test.

## 11. Modules touched

`scan.py` (`find(roots, *, exclude=(), max_depth=None)`, `effective_roots`,
`derive_id` public), `view.py` (header text), new `scanroots.py`
(`extend`, `add_flags`, `handler`, `print_roots`), `vscode/src/roots.ts`
(replaces the stub).

## 12. Docs text (for 17)

> Put your repo locations in the config once:
> ```toml
> [scan]
> roots = ["~", "/mnt/d/work"]
> exclude = ["~/.cache", "~/go/pkg/mod"]
> ```
> `gitpro scan`, `gitpro lost` and the VS Code view use them. `gitpro scan
> --roots` shows what is in effect. Exclude patterns match absolute
> directories; a pattern without a leading `/` or `~/` matches at any depth.
