# 03 — `gitpro lost`: lost and hidden work

Wave 3. Depends on 01 (`resolve`, `jsonout`, the minimal `forge.py`:
`accounts`, `repo_exists`, `announce`, `no_online_error`) and 15 (the
`lost.ts` stub and the `gitpro.lost` view contributions). 02 is a soft
dependency: it is built in the same wave, so this package imports
`gitpro.scan.effective_roots` lazily and falls back (see 2.1).

Files this package may create or edit: new `src/gitpro/lost.py`,
`tests/test_lost.py`, `vscode/src/test/lost.test.ts`; replace
`vscode/src/lost.ts` (15's stub) wholesale. Nothing else.

## 1. Purpose and user story

Work gets lost in ways git never reports in one place: a branch that was
never pushed, a stash from last year, a dirty tree in a repo you forgot, a
clone with no remote, a remote that was deleted or renamed, two clones of
the same repo that diverged, repos nobody touched for a year. `gitpro lost`
lists all of that across the scan roots. It never changes anything and
never suggests a destructive command.

User story: "Before I wipe this laptop I run `gitpro lost`. It shows 3
unpushed branches, 2 stashes, 1 repo with no remote and 1 whose remote is
gone. I push or archive them, run it again, and it says `✓ nothing lost`."

## 2. CLI surface

```
gitpro lost [DIR…] [--repo PATH]… [-c PATH] [--json] [-v]
            [--only KIND[,KIND…]] [--online] [--stale-days N]
```

### 2.1 Roots

```python
def _roots(args, cfg):
    try:
        from gitpro.scan import effective_roots          # added by 02
    except ImportError:
        effective_roots = None
    if effective_roots is not None:
        return effective_roots(args.dirs, cfg.scan if cfg else None)
    roots = args.dirs or (cfg.scan.roots if cfg else None) or [Path.home()]
    return roots, (), "args" if args.dirs else ("config" if cfg and cfg.scan.roots else "default")
```

`scan.find(roots, exclude=exclude)` when 02 is present, else `scan.find(roots)`.
`--repo PATH…` skips discovery. Linked worktrees of one repo share a
common dir (`git rev-parse --path-format=absolute --git-common-dir`); the
command groups by common dir and reports each repo once, listing all its
worktree paths under `-v`. Bare repos are skipped. Repos in `scan.find`'s
`skip_reason` list are reported under `skipped`.

### 2.2 Kinds

`--only` filters; default: all offline kinds. All detection runs `git -C
<repo>` with a 10 s timeout, in a thread pool of 4; results are collected
per repo and sorted afterwards so the output is stable.

| kind | detection |
|---|---|
| `unpushed` | each local branch from `for-each-ref --format '%(refname:short)%00%(upstream:short)%00%(upstream:track)' refs/heads`. Then `rev-list --count <branch> --not --remotes` (commits in no remote-tracking ref). Listed when count > 0. `upstream` and `ahead` (parsed from `[ahead N]`) are reported when set; `merged_gone` is reported instead (see below) when `%(upstream:track)` is `[gone]`. Remote-tracking refs reflect the last fetch; the output says `as of last fetch`. |
| `merged_gone` | branch whose upstream is `[gone]` (deleted on the remote, usually after a merged PR). Reported in its own section `upstream gone (N branches)` with the count of commits not in any remote ref: 0 → `merged, safe to delete` ; > 0 → `N commits not on any remote`. Never lists the delete command (principle: nothing destructive). |
| `stash` | `stash list --format='%gd%00%ci%00%gs'`, count > 0 |
| `dirty` | `status --porcelain=v2 -z` has entries (untracked counted separately; ignored not counted). In an empty repo (no HEAD) `status` still works. |
| `no_remote` | `git remote` prints nothing. The repo's branches are **not** also listed under `unpushed` (there is nothing to push to); the entry carries the total commit count (`rev-list --all --count`). |
| `remote_gone` | **online only**. For each distinct `host/owner/repo` parsed from the raw remote URLs (`resolve.parse_url`), `forge.repo_exists(acc, owner, repo)` (01 §5.5) → `("ok"|"renamed"|"missing"|"unknown", detail)`. `missing` and `renamed` are listed; `unknown` (no login, forge error, unsupported host) is listed under `-v` as `? unknown` and counted in `warnings`. |
| `duplicate` | two repos whose `origin` raw URLs normalise to the same `host/owner/repo` (`.git` stripped, user and scheme dropped, ssh alias → host through the config). Divergence: run `git -C A rev-list --count HEAD..B_HEAD` with `GIT_ALTERNATE_OBJECT_DIRECTORIES=<B>/.git/objects` (common dir of B) and the reverse. If both are 0 → `diverged: false`; if either command fails (missing objects, packfiles elsewhere) → `diverged: null`, `detail: "HEADs differ; could not compare"`. |
| `stale` | newest commit date across all refs (`for-each-ref --sort=-committerdate --count=1 --format=%(committerdate:unix)`) older than `stale_days` (config `[scan].stale_days`, default 180; `--stale-days` overrides). Empty repos (no refs) are not stale, they are `empty`. Printed only with `-v` or `--only stale`; always in JSON. |
| `empty` | repo with no refs at all (`for-each-ref` prints nothing) and a clean tree; listed under `-v` only. A dirty empty repo is `dirty`. |
| `detached` | HEAD detached (`symbolic-ref -q HEAD` fails) with commits not reachable from any branch or remote ref (`rev-list --count HEAD --not --branches --remotes` > 0). Listed in the `unpushed` section as `(detached HEAD)`. |

### 2.3 Text output

```
12 repos under ~, /mnt/d/work  (remote state as of last fetch)

unpushed (3 branches in 2 repos)
  ~/src/app          feature/login   4 commits, no upstream
  ~/src/app          hotfix          1 ahead of origin/hotfix
  ~/work/tool        main            2 commits, no upstream   → git -C ~/work/tool push -u origin main
upstream gone (1 branch)
  ~/src/app          pr-212          merged, safe to delete
stashes (2 in 1 repo)
  ~/src/app          stash@{0} 2026-03-01  WIP on main: fix tests
  ~/src/app          stash@{1} 2025-11-20  WIP on main: spike
dirty (1 repo)
  ~/notes            3 modified, 2 untracked
no remote (1 repo)
  ~/scratch/parser   7 commits, never pushed anywhere
duplicates (1 group)
  github.com/me/app  ~/src/app  ~/old/app   diverged: ~/old/app has 2 commits not in ~/src/app
remote gone: run with --online to check remotes via gh/glab

7 items in 5 repos · stale repos: 4 (-v lists them)
```

`✓ nothing lost under ~` when every kind is empty. The `→` fix command is
printed only for one case: a branch with no upstream in a repo with exactly
one remote (`git -C <repo> push -u <remote> <branch>`); it is quoted with
`shlex.quote`. No other fix is printed (no `branch -D`, no `stash drop`,
no `remote remove`).

With `--online` the first line is `forge.announce`'s:

```
online: asking github.com via gh (account me) whether 9 repos exist; sends owner/repo names only
```

and `remote gone` becomes a section:

```
remote gone (2 repos)
  ~/src/oldlib       git@github.com:me/oldlib.git     missing via gh
  ~/src/tool         git@github.com:me/tool.git       renamed to me/tool-cli   → git -C ~/src/tool remote set-url origin git@github.com:me/tool-cli.git
```

`--online` requires a usable account (01 `forge.accounts(cfg)`): `gh` or
`glab` on PATH and a login for the host. When no account exists for a host,
that host's repos are `unknown` with warning `⚠ no gh login for
<host>; remote check skipped`. When `gh`/`glab` are both missing,
`--online` exits 2 through `forge.no_online_error("lost", "remote check",
...)` (01 §5.5) so a script that asked for the network cannot mistake a
silent skip for "all remotes exist" (reviewer R-053; same rule in 05 and
09). `[forge] online = true` makes `--online` the default.

Exit codes: 0 nothing lost, 1 something listed (any list except `stale`,
`empty`, `skipped`), 2 error.

Capabilities registered: `lost`, `lost.online`.

## 3. `--json` schema

```json
{
  "ok": true, "command": "lost",
  "roots": ["/home/me", "/mnt/d/work"], "roots_source": "config", "repos": 12,
  "online": false,
  "unpushed": [{"repo": "/home/me/src/app", "branch": "feature/login", "commits": 4, "upstream": null, "ahead": null, "detached": false, "fix": null}],
  "merged_gone": [{"repo": "/home/me/src/app", "branch": "pr-212", "commits": 0}],
  "stashes":  [{"repo": "/home/me/src/app", "ref": "stash@{0}", "date": "2026-03-01T10:00:00+00:00", "subject": "WIP on main: fix tests"}],
  "dirty":    [{"repo": "/home/me/notes", "modified": 3, "untracked": 2}],
  "no_remote":[{"repo": "/home/me/scratch/parser", "commits": 7}],
  "remote_gone": [{"repo": "...", "remote": "origin", "url": "...", "status": "missing"|"renamed"|"unknown", "renamed_to": "me/tool-cli"|null, "detail": "...", "fix": "git -C ... remote set-url origin ..."|null, "via": "gh"}],
  "duplicates": [{"key": "github.com/me/app", "repos": ["/home/me/src/app", "/home/me/old/app"], "diverged": true|false|null, "detail": "/home/me/old/app has 2 commits not in /home/me/src/app"}],
  "stale":    [{"repo": "...", "last_commit": "2024-01-02T00:00:00+00:00", "days": 640}],
  "empty":    [{"repo": "..."}],
  "worktrees": {"/home/me/src/app": ["/home/me/src/app-wt"]},
  "skipped":  [{"path": "...", "reason": "owned by root"}],
  "warnings": []
}
```

`ok` is `true` when all lists except `stale`, `empty`, `skipped` are empty.
Lists sorted by repo path then branch/ref. `remote_gone` with status
`unknown` entries are present only with `-v` in text but always in JSON.

## 4. Config keys

`[scan] roots`, `exclude`, `stale_days`; `[forge] online`; identity
`gh_account`/`glab_account` for `--online`.

## 5. VS Code surface

15 already declares the view `gitpro.lost` ("Lost Work") in the `gitpro`
container, the commands `gitpro.lostRefresh`, `gitpro.lostOpenRepo`,
`gitpro.lostCopyFix`, their view-title/item menus, and the welcome text
`✓ nothing lost`. `features.ts` calls `registerLost(context, cli)` from
`./lost`. This package replaces `vscode/src/lost.ts` and exports:

```ts
export function lostTree(json: LostJson): KindNode[];
export function registerLost(context: vscode.ExtensionContext, cli: Cli): void;
```

Tree: kind → repo → item (branch / stash / detail). `lostOpenRepo` opens
the folder in a new window or reveals it; `lostCopyFix` copies the `fix`
command to the clipboard (the extension never runs push). No `--online`
from the extension in this package. The command runs `lost --json` with no
positional roots (the TOML decides) in the home dir, 600 s timeout.

## 6. Files created/changed

None. Read-only command.

## 7. Failure modes

| situation | message |
|---|---|
| a repo errors in one check | `⚠ <repo>: git <sub> failed: <first stderr line>`; other checks continue |
| `--online`, neither gh nor glab on PATH | `✗ lost --online needs gh or glab on PATH (remote check); install one or run without --online`, exit 2 |
| `--online`, host without a login | `⚠ no gh login for <host>; remote check skipped`, status `unknown` |
| `--only foo` | `✗ unknown kind 'foo' (unpushed, merged_gone, stash, dirty, no_remote, remote_gone, duplicate, stale, empty)` exit 2 |
| per-repo git call > 10 s | `⚠ <repo>: timed out; skipped` |
| duplicate objects not comparable | `diverged: null`, detail `HEADs differ; could not compare` |

## 8. Security / privacy

Offline by default. `--online` sends `owner/repo` names to the forge the
user is already logged into, through `gh`/`glab`, and the first line says
so. Never sends branch names, paths or contents. Stash subjects are printed
locally only. No command printed by `lost` deletes anything.

## 9. Out of scope

Pushing, deleting stashes or branches, archiving; fetching (`remote_gone`
uses the forge CLI, not `git fetch`); submodules (found as repos, reported
like any other).

## 10. Acceptance tests (`tests/test_lost.py`)

Setup: 01's fixtures `cfg`, `repo_with_remote(..., commits=N)`,
`bare_remote(name)`, `commit`, `fake_bin`.

1. Repo with branch `feature` (2 commits past main, no upstream) →
   `unpushed` has one entry, `commits == 2`, `upstream is None`; text shows
   `no upstream`; exit 1. With exactly one remote the `fix` is `git -C
   <repo> push -u origin feature`; with two remotes `fix is None`.
2. Branch pushed to `bare_remote` with `-u`, then one more commit →
   `ahead == 1`; after `git push` → not listed; exit 0 and `✓ nothing lost`.
3. Upstream deleted on the bare remote (`git push origin --delete feature`
   then `git fetch --prune`) → `merged_gone` one entry with `commits == 0`;
   not in `unpushed`; text `merged, safe to delete`; no `branch -D` anywhere
   in stdout.
4. `git stash` once → `stashes` has one entry with `ref == "stash@{0}"`.
5. Modified tracked file + one untracked → `dirty == [{"modified": 1,
   "untracked": 1}]`.
6. `git init` + one commit, no remote → `no_remote` with `commits == 1` and
   `unpushed == []` (no double listing).
7. Two clones of the bare remote, one with an extra commit → `duplicates`
   one group, `diverged == true`, `detail` names the clone with the extra
   commit. Identical clones → `diverged == false`. Clone B's `.git/objects`
   renamed away → `diverged is None`, detail `could not compare`.
8. Commit with `GIT_COMMITTER_DATE` two years ago, `--only stale` → listed
   with `days > 365`; without `-v`/`--only`, `stale` is still in JSON but
   not in text.
9. `git init` with no commits → `empty` entry; not `stale`, not `unpushed`;
   exit 0. After `touch x` → `dirty`, exit 1.
10. `git checkout --detach` + one commit → `unpushed` entry with
    `detached == true`, text `(detached HEAD)`.
11. A linked worktree (`git worktree add ../wt`) → the repo is listed once;
    `worktrees` maps the main path to the worktree path; `-v` text shows it.
12. `--only unpushed,stash` → only those keys non-empty; JSON still has all
    keys.
13. `--online` with fake `gh` that prints `{"nameWithOwner":"me/tool-cli"}`
    for `me/tool` and exits 1 with `Could not resolve to a Repository` for
    `me/oldlib` → `remote_gone` two entries, statuses `renamed` and
    `missing`, fix command for the renamed one. `fake_bin.calls("gh")`
    shows `repo view me/tool --json nameWithOwner` and `env:GH_TOKEN=unset`.
    First stdout line starts with `online:`.
14. `--online` with no `gh`/`glab` on PATH → exit 2, stderr contains
    `needs gh or glab`; JSON `ok: false`.
15. `--online` with `gh` present but `gh auth status` reporting no login for
    github.com → entry `status == "unknown"`, warning `no gh login`, exit
    per other kinds.
16. Roots from `[scan].roots` are used; with `gitpro.scan.effective_roots`
    monkeypatched away (`monkeypatch.delattr("gitpro.scan.effective_roots",
    raising=False)`) the fallback still uses `[scan].roots`; with
    `effective_roots` patched to a recording stub it is called with
    `(args.dirs, cfg.scan)`.
17. Unreadable repo (gitdir file pointing nowhere) → `skipped` entry, no
    crash.
18. Thread pool: 6 repos → output order equals sorted path order on three
    consecutive runs.

node:test (`vscode/src/test/lost.test.ts`): `lostTree(json)` groups by
kind, sorts repos, labels `feature/login · 4 commits, no upstream`;
`merged_gone` label `pr-212 · merged, safe to delete`.

## 11. Modules touched

New `lost.py` (`collect(roots, exclude, cfg, *, online, kinds, stale_days)
-> dict`, `render(report, verbose) -> str` (text renderer lives here, not
in `view.py`), `register`), `vscode/src/lost.ts` (replaces the stub).

## 12. Docs text (for 17)

> `gitpro lost` lists unpushed branches, branches whose upstream is gone,
> stashes, dirty trees, repos with no remote, diverged duplicate clones and
> (with `-v`) stale and empty repos across your scan roots. Add `--online`
> to ask GitHub/GitLab via `gh`/`glab` whether each remote still exists or
> was renamed; only `owner/repo` names are sent. It never deletes anything
> and never prints a command that would.
