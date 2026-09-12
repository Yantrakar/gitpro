# 06 — Guard: chained git hooks (pre-commit, pre-push)

Wave 2. Depends on 01 only (`resolve`, `[guard]`, `undo`, `files.atomic_write`,
`hot.py`, the `guard.HOOKS` contract in 01 §5.6, fixtures). 08 and 14 add
hook entries later by implementing the functions the `HOOKS` table already
names; they do not edit this package's files.

Files this package may create or edit: new `src/gitpro/guard.py`,
`tests/test_guard.py`; edit `src/gitpro/render.py` (add `hook_script`,
`hook_stub`, `hooks_gitconfig`) and `src/gitpro/plan.py` (hook targets when
`cfg.guard.hooks`). Nothing else.

## 1. Purpose and user story

Config matching is right 99% of the time; the 1% (a repo-local override, a
pin that went stale, `GIT_COMMITTER_EMAIL` left in a shell, a remote added
after the clone) produces a commit with the wrong email on a work repo.
The guard is a `pre-commit` and a `pre-push` hook, installed **on request**
(`gitpro guard install`, DECISIONS Q4), globally through `core.hooksPath`,
that runs `gitpro guard check` and then **chains to the repo's own hooks**.
It blocks when the **committer** email about to be used is not the
identity the remote belongs to. Any failure of gitpro itself lets the
commit through.

User story: "I `git commit` in `~/work/app` after `export
GIT_COMMITTER_EMAIL=me@personal.test` in a side terminal. The commit is
refused with a one-line reason and the command that fixes it. `git commit
--no-verify` still works when I mean it."

## 2. What the guard judges (DECISIONS Q4, reviewer R-005)

git has two identities per commit. The **committer** is the person at the
keyboard: `git var GIT_COMMITTER_IDENT` reflects `user.email` from every
config layer and `GIT_COMMITTER_EMAIL`, and is not changed by `--author`,
`--amend`, `cherry-pick`, `rebase` or `am`. The **author** can legitimately
be someone else (a cherry-picked colleague's commit, a patch applied with
`git am`, a pair's `--author`). Verified in a scratch HOME with git 2.43:
in `pre-commit`, `GIT_AUTHOR_IDENT` follows `--author` while
`GIT_COMMITTER_IDENT` stays the local user; `-c user.email=` changes both;
`GIT_AUTHOR_EMAIL` changes only the author.

So:

- `pre-commit` blocks when `committer_email != expected.email`.
- The author is checked only when it is one of the user's **own** emails
  (`resolve.own_emails(cfg)`: every identity's email and the default's)
  and differs from `expected.email`: that is the "I am committing with my
  personal email on a work repo" case. An author email that is nobody's in
  the config (a colleague) is never a reason to block.
- `pre-push` looks at every commit about to leave. A commit is **judged
  only when its committer email is one of the user's own emails**
  (`own_emails(cfg)`); it is wrong when that email differs from
  `expected.email`. The author is judged with the same own-emails rule.
  Commits committed by anyone else (a colleague's branch merged locally,
  a commit fetched from a fork without a remote-tracking ref, an imported
  patch series) are never a reason to block (V-010): the user cannot fix
  them and they are not the user's mistake.

## 3. CLI surface

```
gitpro guard install   [--repo PATH] [-y] [--json]
gitpro guard uninstall [--repo PATH] [-y] [--json]
gitpro guard status    [--repos] [--json]
gitpro guard check <pre-commit|pre-push|commit-msg> [args…]   # called by the hook scripts; not for humans
gitpro guard test [DIR]                                       # runs the pre-commit check on DIR; exit as the hook would
```

`guard` is a hot module (01 §2.2): `guard.add_arguments(p)` adds the
sub-subcommands and options; `guard.register(sub, common, confirm)` is
`add_arguments(sub.add_parser("guard", parents=[common, confirm]))`;
`guard.main(argv)` builds `ArgumentParser(prog="gitpro guard",
parents=[args.common(), args.confirm()])`, calls `add_arguments`, parses
and dispatches. `install`/`uninstall` import `view` inside their
functions; `check`, `test`, `status` never import `cli`, `view` or `rich`.

When `[guard] hooks = true`, `gitpro plan`/`apply` include the hook files
and the gitconfig include as ordinary targets, so `apply` installs and
`uninstall` removes them. `guard install` writes the same files without
editing the TOML, and `plan.build` treats an installed guard as wanted
(V-025): the hook targets are included when `cfg.guard.hooks` **or**
`~/.gitconfig-gitpro-guard` exists with the generated marker. So `apply`
after `guard install` keeps the hooks (`up to date`), and only `guard
uninstall` (or `gitpro uninstall`) removes them. `guard install` prints
`→ 'gitpro apply' keeps these hooks; gitpro guard uninstall removes them
(or set [guard] hooks = true to make the TOML the source of truth)`.
Nothing is installed until one of those happens.

### 3.1 Installed files (global mode)

| path | content |
|---|---|
| `~/.config/gitpro/hooks/pre-commit`, `pre-push`, `commit-msg` (0700; line 1 `#!/bin/sh`, line 2 the generated marker as a comment, accepted by `render.is_generated` per 01 §5.8) | the chain script (3.2) |
| `~/.config/gitpro/hooks/<every other name in render.HOOK_NAMES>` (0700, same header) | the pass-through stub (3.3): `applypatch-msg`, `pre-applypatch`, `post-applypatch`, `prepare-commit-msg`, `post-commit`, `pre-merge-commit`, `pre-rebase`, `post-checkout`, `post-merge`, `pre-receive`, `update`, `proc-receive`, `post-receive`, `post-update`, `pre-auto-gc`, `post-rewrite`, `sendemail-validate` — the githooks(5) hooks for git 2.43 that a repo or a tool plausibly installs. **Not** forwarded: `push-to-checkout` (git skips its built-in `receive.denyCurrentBranch = updateInstead` deploy whenever that hook exists, so a stub would stop pushes from updating the checked-out tree), `reference-transaction` and `post-index-change` (run on every ref/index change, many times per command; a stub there costs a `sh` + `git rev-parse` each time and nobody installs them by hand), `fsmonitor-watchman` (git runs it only when `core.fsmonitor` names it; a stub there would break the protocol), `p4-*` (git-p4 only). A user who needs one of those keeps the repo's own `core.hooksPath` and uses repo mode (3.5). |
| `~/.config/gitpro/hooks/README` | one paragraph: what these are, how to bypass, `gitpro guard uninstall`, the list of hooks not forwarded |
| `~/.gitconfig-gitpro-guard` (managed, 0600) | `[core]\n\thooksPath = /abs/home/.config/gitpro/hooks\n` (absolute; git does not expand `~` reliably in every path context) |
| `~/.gitconfig-identities` | gains `[include] path = ~/.gitconfig-gitpro-guard` at the end of the generated section (`render.hooks_gitconfig`; `plan.build` adds it when `cfg.guard.hooks` or when `~/.gitconfig-gitpro-guard` exists, see §3) |

`render.HOOK_NAMES` is the one list (3 chain scripts + 17 stubs = 20
files); every count in this spec and in `guard status` is derived from
it, never typed.

Why a separate include file (reviewer R-024): `~/.gitconfig-identities` is
regenerated by every `apply` from the TOML. With the include, `apply`
keeps the line when the config says so or when the guard file is present,
and the guard file is the one thing `guard install` owns. `guard
uninstall` removes the include line and the file; `gitpro uninstall`
removes both too (the file is in the manifest).

Why every hook gets a stub (reviewer R-001, verified): with a global
`core.hooksPath`, git runs **only** hooks from that directory. A repo's
own `.git/hooks/post-checkout` or `prepare-commit-msg` would stop running
unless gitpro's directory forwards it. The stub forwards every hook name
to the repo's own hook and to `chain_to`.

### 3.2 Chain script (`render.hook_script(name, chain_to, gitpro_bin)`)

POSIX sh, for `pre-commit` (the `commit-msg` script is the same with
`commit-msg` substituted; `pre-push` differs as shown after):

```sh
#!/bin/sh
# Generated by gitpro -- edit the TOML config and re-run; do not edit this file.
# gitpro guard hook: pre-commit. Skip gitpro once: GITPRO_SKIP=1 git ...  (git --no-verify skips every hook)
# After the gitpro check, the repo's own hook runs (<common dir>/hooks/pre-commit), then the chain_to hook.

own_dir=$(cd -P -- "$(dirname -- "$0")" && pwd)

if [ -z "${GITPRO_SKIP:-}" ]; then
  gp=""
  if command -v gitpro >/dev/null 2>&1; then gp=gitpro
  elif [ -x "$HOME/.local/bin/gitpro" ]; then gp="$HOME/.local/bin/gitpro"
  elif [ -x __GITPRO_BIN__ ]; then gp=__GITPRO_BIN__
  fi
  if [ -n "$gp" ]; then
    rc=0; "$gp" guard check pre-commit "$@" || rc=$?
    if [ "$rc" -eq 3 ]; then exit 1; fi
    if [ "$rc" -ne 0 ]; then echo "⚠ gitpro guard: exited $rc; not blocking" >&2; fi
  fi
fi

common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
repo_hook="$common/hooks/pre-commit"
if [ -n "$common" ] && [ -x "$repo_hook" ] && [ "$(cd -P -- "$(dirname -- "$repo_hook")" && pwd)" != "$own_dir" ]; then
  "$repo_hook" "$@" || exit $?
fi

chain=__CHAIN_TO__
if [ -n "$chain" ] && [ -x "$chain/pre-commit" ] && [ "$(cd -P -- "$chain" && pwd)" != "$own_dir" ]; then
  "$chain/pre-commit" "$@" || exit $?
fi
exit 0
```

Facts the script relies on (verified in a scratch HOME, git 2.43):

- `git rev-parse --git-path hooks` returns the **global** `core.hooksPath`
  directory when one is set, so a hook that used it would call itself
  forever (R-001). `--path-format=absolute --git-common-dir` returns
  `<repo>/.git` (or the main repo's `.git` from a linked worktree), and
  `<common>/hooks/<name>` is where the repo's own hook lives. The
  `own_dir` comparison refuses to run a hook from gitpro's own directory
  even if someone points `chain_to` or a symlink there (R-003).
- A repo with a **local** `core.hooksPath` (husky `.husky`, lefthook,
  pre-commit framework): git uses the repo's value and the global guard
  does not run there at all. See 3.5.
- git passes the hook's exit code through; the script maps gitpro's
  **3** (block) to 1 and treats every other non-zero exit as "gitpro
  failed; not blocking" (README §8 rule 11). gitpro never exits 3 for
  anything except a confirmed mismatch in block mode.

`__CHAIN_TO__` is replaced with `shlex.quote(str(path))` of the expanded
`[guard].chain_to`, or `''` when unset; `__GITPRO_BIN__` with
`shlex.quote(sys.argv[0] resolved)` of the installing gitpro (R-013).
`chain_to` is validated at install: must be an absolute directory after
`~` expansion, must not resolve to `~/.config/gitpro/hooks`, must be owned
by the user and not group/world-writable (README §8 rule 2). `apply` (with
`[guard] hooks = true`) re-checks it on every render with the same refusals,
except that a chain_to deleted after install is a `⚠ [guard] chain_to … does
not exist; the hooks run without it → create it, or remove chain_to …` line:
the hooks render with `chain=''` and unrelated changes still apply. doctor
shows the same warning. `guard install` still refuses a missing dir.

The script runs `unset CDPATH` first and prefixes a relative
`--git-common-dir` with `./` before `cd -P` (stdout to /dev/null), so an
exported CDPATH cannot redirect the common dir. pre-commit removes
`<common dir>/gitpro-guard-warned` on every run before its check and
commit-msg removes it after reading it, so commit-msg only trusts a note
written by this commit's own pre-commit. Uninstall does not visit repos, so
a note left by an aborted commit stays behind (harmless; the hooks README
says it is safe to delete).

`pre-push` reads stdin once and feeds it to every hook:

```sh
tmp=$(mktemp "${TMPDIR:-/tmp}/gitpro-prepush.XXXXXX" 2>/dev/null) || tmp=""
if [ -n "$tmp" ]; then trap 'rm -f "$tmp"' EXIT; cat >"$tmp"; fi
in_tmp() { if [ -n "$tmp" ]; then "$@" <"$tmp"; else "$@"; fi; }
...  rc=0; in_tmp "$gp" guard check pre-push "$@" || rc=$?
...  in_tmp "$repo_hook" "$@" || exit $?
...  in_tmp "$chain/pre-push" "$@" || exit $?
```

When `mktemp` fails the script falls through: the gitpro check reads the
real stdin and the later hooks get an empty stdin (they still run; a
pre-push hook that needs the ref list may then do nothing). It never
exits early on a temp-file failure.

The gitpro call is always written as `rc=0; … || rc=$?; if [ "$rc" -eq 3 ];
then exit 1; fi` (V-002). The one-line form `cmd; [ $? -eq 3 ] && exit 1`
is wrong as the **last** statement of a block: `[ 0 -eq 3 ]` is false, the
`&&` list exits 1, and under `sh -e` or as the hook's final status a
clean run would fail. Verified: a hook consisting only of `true; [ $? -eq
3 ] && exit 1` makes `git commit` fail.

### 3.3 Pass-through stub (`render.hook_stub(name, chain_to)`)

Same header, no gitpro call; runs `<common>/hooks/<name>` then
`<chain_to>/<name>` with the same `own_dir` guard and `"$@"`; stdin is
forwarded through a temp file for the hooks that read stdin
(`pre-receive`, `post-receive`, `post-rewrite`, `proc-receive`), with the
same `mktemp` fall-through as `pre-push`.

git LFS (V-022): `git lfs install` normally writes `.git/hooks/pre-push`,
`post-checkout`, `post-commit`, `post-merge` that call `git lfs <name>`;
with a global `core.hooksPath` those files are not run. So the stubs for
those four names (and the `pre-push` chain script, after the gitpro check)
run `git lfs <name> "$@"` (with stdin for `pre-push`) when **all** of:
`git config filter.lfs.process` is set (any scope: `git lfs install`
without `--local` sets it globally, which also means the call runs in
every repo, as LFS itself would), `git-lfs` is on PATH, and the repo's own
hook file either does not exist or does not contain the string `git lfs`
/ `git-lfs` (so a repo whose `.git/hooks/pre-push` already calls LFS does
not run it twice). `git lfs <name>` is a no-op in repos with no LFS
objects; it is run before the repo hook.

### 3.4 `guard check` behaviour

`pre-commit`: `report = resolve.compare(repo, cfg, env=hook_env)` where
`repo` is `cwd` (git runs hooks from the top level) and `hook_env` is
`os.environ` minus `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`,
`GIT_AUTHOR_DATE` — git exports those in every `pre-commit`/`commit-msg`
run (V-004), so leaving them in would make `resolve` report
`env_override` for every `--author` commit. Committer email = `git var
GIT_COMMITTER_IDENT`; author email = `git var GIT_AUTHOR_IDENT` (both
run with the full hook environment, so they reflect `--author` and
`GIT_COMMITTER_EMAIL`). Expected = `report.expected.email`. Then, in
order:

1. `report.expected.email is None` (no identity, empty default) → not a
   guard problem; exit 0.
2. `committer_email != expected` → mismatch.
3. `author_email != expected and author_email in own_emails` → mismatch
   with cause `author is your <other identity> email`.
4. `report.problems` contains `key_mismatch`/`sign_mismatch` → `⚠` only.

Then the other entries in `guard.HOOKS["pre-commit"]` (01 §5.6:
`gitpro.policy:hook_commit`) run with the same `Context`; a missing module
is skipped; an entry that raises prints `⚠ gitpro <entry>: <error>; not
blocking`. The hook's exit is 3 if any entry returned 3 and
`on_mismatch == "block"`, else 0.

`pre-push <remote> <url>` (args from git): `resolve.compare(repo, cfg,
remote=<remote>, env=hook_env)` chooses the identity for **this** remote;
the `remote` argument may be a name or a URL (git passes the URL as the
name for anonymous pushes; `resolve.compare` accepts both, 01 §4). Then
for each stdin line `<local ref> <local sha> <remote ref> <remote sha>`,
with `local sha` not all zeros: `git log --format=%ce%x00%ae%x00%h%x00%s
<local sha> --not --remotes` and additionally `^<remote sha>` when it is
not all zeros **and** `git cat-file -e <remote sha>^{commit}` succeeds
(V-023: the remote may be ahead of the last fetch and hold a sha this
clone does not have; excluding an unknown object would make `git log`
fail. Without it the listing may include commits the remote already has,
which only makes the message longer). This always excludes what every
remote already has (R-005: remote-tracking refs can be stale, the remote
sha is exact for this push). A commit is wrong when its committer email
is in `own_emails(cfg)` and ≠ expected, or its author email is in
`own_emails(cfg)` and ≠ expected (§2, V-010). Mismatch lists the first 5
wrong commits.

`commit-msg <file>`: no identity check; the `HOOKS["commit-msg"]`
entries run (`gitpro.trailers:hook_message` from 08,
`gitpro.policy:hook_message` from 14), each skipped when missing.

`Context` (01 §5.6): `Context(hook, repo, args, stdin, cfg, report,
committer_email, author_email, say)` where `say(line)` writes to stderr.
Entries return 0 (pass), 3 (block), anything else is a warning.

Output on a mismatch (stderr):

```
✗ gitpro: this commit would be committed as me@personal.test but origin (git@github.com:corp-org/app.git) belongs to identity work-github <me@corp.test>
  cause: user.email is set in the repo's .git/config and overrides the work-github identity
  fix:   git -C ~/work/app config --unset user.email
  skip once: GITPRO_SKIP=1 git commit ...
```

The `cause`/`fix` lines come from the first `Problem` in the report; when
there is none (pure env override), cause is `GIT_COMMITTER_EMAIL is set in
the environment` and fix `unset GIT_AUTHOR_EMAIL GIT_COMMITTER_EMAIL`.
Exit **3**. With `[guard] on_mismatch = "warn"`: same text with `⚠` and
`(not blocking: [guard] on_mismatch = "warn")`, exit 0.

Pre-push variant: `✗ gitpro: 3 commits to origin (…) are committed as
me@personal.test; this remote belongs to work-github <me@corp.test>` +
commit list + `fix: gitpro audit --rewrite ~/work/app` when `gitpro.audit`
imports (08), else `fix: amend the listed commits with the right
user.email (git rebase -i <base>, then git commit --amend --no-edit on
each)`. The guard never suggests `--reset-author`: it would also change
the author of commits that are not yours (V-011).

Failure of gitpro itself inside a hook (exception, no config, config
error, git too old, timeout): `⚠ gitpro guard: <message>; not blocking` on
stderr, exit 0 from `guard.main` (and the script treats any other rc the
same way). Every git call inside the hook has a 5 s timeout. Time budget:
< 150 ms; `guard check` imports only `resolve`, `config`, `guard`, `hot`.

### 3.5 Repo mode (`--repo PATH`)

For repos that set their own `core.hooksPath` (`git config --local
core.hooksPath` → husky `.husky`, lefthook, pre-commit framework) or when
the user prefers per-repo hooks. `guard install --repo PATH`:

1. Finds the hook dir: `git -C PATH rev-parse --git-path hooks` with the
   repo's local `core.hooksPath` applied (verified: returns `.husky` for
   husky repos; for a repo without a local value under a global gitpro
   hooksPath it would return gitpro's own dir, so when the result equals
   gitpro's dir the common dir's `hooks/` is used instead).
2. **Refuses tracked and tool-owned files** (DECISIONS Q5, V-020): for
   each of `pre-commit`, `pre-push`, `commit-msg`, if `git -C PATH
   ls-files --error-unmatch <hookfile>` succeeds the file is tracked by
   the team → `✗ <file> is tracked by git; gitpro does not edit team
   files. Add this block yourself:` followed by the managed block (below),
   exit 2. The same refusal, with `… is managed by <tool>; add this block
   to the hook that tool runs:`, when the hook dir is inside a directory
   named `_` (husky 9: `.husky/_/` is regenerated by `husky` on install;
   the user's hooks live in `.husky/<hook>`, so the printed line names
   `.husky/pre-commit`), or when `git -C PATH check-ignore -q <hookdir>`
   succeeds and the dir is not `<common dir>/hooks` (an ignored hook dir
   is one a tool regenerates: lefthook's `.lefthook-local`, pre-commit's
   cache). pre-commit framework repos: `pre-commit install` writes
   `.git/hooks/pre-commit` without setting `core.hooksPath`, so the global
   guard covers them and their hook runs through the chain; `guard
   install --repo` on such a file works like any untracked hook (step 3).
   lefthook: `lefthook install` writes `.git/hooks/*`, same case.
3. For an untracked existing file: it must start with an accepted shebang
   (`#!/bin/sh`, `#!/usr/bin/env sh`, `#!/bin/bash`, `#!/usr/bin/env bash`,
   `#!/usr/bin/bash`, `#!/bin/zsh`, `#!/usr/bin/env zsh`); else `✗ <file>
   is not a shell script; add this line yourself: …`, exit 2. The file is
   backed up to `~/.local/state/gitpro/guard-backup-<ts>/<repo-hash>/<hook>`
   (not next to the hook, so no stray file lands in the repo), then the
   managed block is inserted after the shebang line (or at the top of a
   new file created with `#!/bin/sh`), and the file is made `0700`.
4. The managed block, per hook:

```sh
# BEGIN gitpro -- keep first
if [ -z "${GITPRO_SKIP:-}" ] && command -v gitpro >/dev/null 2>&1; then
  rc=0; gitpro guard check pre-commit "$@" || rc=$?
  if [ "$rc" -eq 3 ]; then exit 1; fi
fi
# END gitpro
```

   `pre-push` (the rest of the script still needs stdin, so it is kept in
   a temp file and re-opened as stdin; `mktemp` failure falls through to
   the check reading stdin directly):

```sh
# BEGIN gitpro -- keep first
if [ -z "${GITPRO_SKIP:-}" ] && command -v gitpro >/dev/null 2>&1; then
  rc=0; tmp=$(mktemp "${TMPDIR:-/tmp}/gitpro-prepush.XXXXXX" 2>/dev/null) || tmp=""
  if [ -n "$tmp" ]; then cat >"$tmp"; gitpro guard check pre-push "$@" <"$tmp" || rc=$?; exec <"$tmp"; rm -f "$tmp"
  else gitpro guard check pre-push "$@" || rc=$?; fi
  if [ "$rc" -eq 3 ]; then exit 1; fi
fi
# END gitpro
```

   The `if … rc=$?` form is the only one used anywhere (V-002; see 3.2
   for why `[ $? -eq 3 ] && exit 1` is wrong as a last statement). The
   block fails open: no gitpro → nothing; gitpro error → continue; exit 3
   → refuse; a clean run leaves the hook's status untouched.
5. Writes `~/.local/state/gitpro/guard-undo-<ts>.sh` **before** editing:
   it restores each backup (`cp`) or removes a file gitpro created. Prints
   `↩ undo: sh <script>`.

`guard uninstall --repo PATH` removes the block (and the file if gitpro
created it and nothing else is in it), via the same undo mechanism.

### 3.6 `guard status`

```
guard: installed (global, ~/.config/gitpro/hooks)  on_mismatch = block
  pre-commit ✓   pre-push ✓   commit-msg ✓ (trailers: 08 not installed → no-op; policy: not installed)
  17 pass-through hooks ✓   (not forwarded: push-to-checkout, reference-transaction, post-index-change, fsmonitor-watchman, p4-*)
  chain_to: (none)
  gitpro on PATH: ~/.local/bin/gitpro
  git lfs: filter.lfs.process set globally; stubs call 'git lfs <hook>' unless the repo hook already does
system core.hooksPath: (none)
```

The `git lfs` line appears only when `filter.lfs.process` is set. The
pass-through count is `len(render.HOOK_NAMES) - 3`.

`guard.status() -> dict` is the one status function (used by `status
--json`, 10's `agent status`, 15's tooltip, 16's `doctor`): keys as in
§4's JSON (`installed`, `hooks_dir`, `on_mismatch`, `chain_to`,
`gitpro_bin`, `hooks`, `system_hooks_path`). It runs one `git config`
call and reads the hook dir; no network; never raises (`installed:
"none"` with an `error` key on failure).

`guard status --repos` additionally scans the config roots (02's
`scan.find` when importable, else `~`) for repos with a **local**
`core.hooksPath` and lists them:

```
repos with their own core.hooksPath (global guard bypassed):
  ~/work/app   .husky   → gitpro guard install --repo ~/work/app
```

States: `guard: not installed → gitpro guard install` when the include is
absent; `guard: core.hooksPath = /other/path (not gitpro) → set [guard]
chain_to = "/other/path" and run gitpro guard install` when something else
owns it. Detection uses `git config --show-scope --show-origin --get-all
core.hooksPath` from a non-repo directory (`$HOME`), so a value from the
**system** scope (`/etc/gitconfig`, which the user cannot edit) is reported
separately: `⚠ system gitconfig sets core.hooksPath = /x; the global guard
overrides it; set chain_to to keep it` (R-026). Install refuses without
`chain_to` when any scope other than gitpro's own include sets the key.

### 3.7 `guard install` output

```
  create  ~/.config/gitpro/hooks/pre-commit
  create  ~/.config/gitpro/hooks/pre-push
  create  ~/.config/gitpro/hooks/commit-msg
  create  ~/.config/gitpro/hooks/post-checkout … (17 pass-through hooks)
  create  ~/.config/gitpro/hooks/README
  create  ~/.gitconfig-gitpro-guard
  update  ~/.gitconfig-identities        (+ include ~/.gitconfig-gitpro-guard)
install guard hooks (22 files, 1 change)?  [y/n]
✓ guard installed; test it: gitpro guard test
↩ undo: gitpro guard uninstall
→ 'gitpro apply' keeps these hooks; gitpro guard uninstall removes them (or set [guard] hooks = true to make the TOML the source of truth)
```

(Counts come from `render.HOOK_NAMES`: 20 hook files + README +
`~/.gitconfig-gitpro-guard`.)

## 4. `--json`

`guard status --json`: `{"ok": true, "command": "guard status",
"installed": "global"|"none", "hooks_dir": "...", "on_mismatch": "block",
"chain_to": null, "gitpro_bin": "...", "hooks": {"pre-commit": true, ...},
"system_hooks_path": null, "bypassed_repos": [{"repo": "...",
"hooks_path": ".husky", "repo_mode": true|false}]}` (`bypassed_repos`
only with `--repos`). `guard install/uninstall --json` → `{"ok",
"command", "changes": [...], "undo": "<path>|null"}` (requires `-y`).
`guard check` has no `--json`; it writes human text for git to show.

Capabilities registered: `guard`.

## 5. Config keys

`[guard] hooks`, `on_mismatch`, `chain_to`.

## 6. VS Code surface

None here (15 shows guard status in the status bar tooltip via `guard
status --json`).

## 7. Files created/changed and undo

Global: the files under `~/.config/gitpro/hooks/`, `~/.gitconfig-gitpro-guard`
(both generated, in the manifest), and the include line in
`~/.gitconfig-identities`. Undo: `guard uninstall` or `gitpro uninstall`.
Repo mode: the managed block in `<hooks>/<hook>`, backup in the state
dir, undo script `guard-undo-<ts>.sh`.

## 8. Failure modes

| situation | message | exit |
|---|---|---|
| hook: gitpro error | `⚠ gitpro guard: <msg>; not blocking` | 0 |
| hook: gitpro crashed / wrong rc | script prints `⚠ gitpro guard: exited <rc>; not blocking` | hook continues |
| hook: mismatch, block | the `✗ gitpro:` block | 3 → git sees 1 |
| hook: mismatch, warn | `⚠ gitpro:` block | 0 |
| install: foreign hooksPath, no chain_to | `✗ core.hooksPath is already … (<scope>)` | 2 |
| install: chain_to is gitpro's own dir / not a dir / world-writable | `✗ chain_to …` | 2 |
| apply: chain_to deleted after install | `⚠ [guard] chain_to … does not exist; …` (renders without it) | 0 |
| install --repo: hook file tracked | `✗ <file> is tracked by git; …` | 2 |
| install --repo: hook dir is ignored or `…/_/` (husky 9, lefthook, pre-commit cache) | `✗ <dir> is managed by <tool>; add this block to the hook that tool runs: …` | 2 |
| install --repo: not a shell script | `✗ <file> is not a shell script; add this block yourself: …` | 2 |
| hook: repo hook already calls `git lfs` | stub skips its own `git lfs` call (no double run) | — |
| hook: `mktemp` fails (pre-push) | check reads stdin directly; later hooks get empty stdin | hook continues |
| `guard status`: `pre-commit` framework / lefthook repo | listed as covered (they write `.git/hooks`, no `core.hooksPath`) | 0 |
| `guard test` outside a repo | `✗ <dir>: not a git repository` | 2 |

## 9. Security / privacy

Hooks run `gitpro` from PATH (or the two fallbacks) in the user's shell
environment; the script quotes everything and passes `"$@"`. gitpro never
writes into hooks it did not create except the managed block in untracked
files, after a backup. The hook never contacts the network. `GITPRO_SKIP`
(any non-empty value) is documented in the hook file itself; the hook does
not explain `--no-verify` beyond naming it.

## 10. Out of scope

Trailer checks (08), policy checks (14), Windows, enforcing on `git am`/
`rebase` beyond what `pre-commit` sees, lefthook/husky configuration (the
docs text tells the user to add the fail-open line to their tracked hook
file).

## 11. Acceptance tests (`tests/test_guard.py`)

Setup: `cfg()` with two identities; `set_git_hooks_sandbox(monkeypatch,
home)` so the temp HOME's `~/.gitconfig` applies; `fake_bin.add("gitpro",
body)` where `body` is `sys.exit(__import__("gitpro.hot").hot.main(argv[1:]))`
so hooks find a real gitpro on PATH; `bare_remote("origin")` for pushes;
`commit(repo, …, author=, committer=)` for identities.

1. `guard install -y` creates `pre-commit`, `pre-push`, `commit-msg`, one
   stub per remaining name in `render.HOOK_NAMES` (17; the set equals the
   list in 3.1 and contains `pre-merge-commit` and not
   `push-to-checkout`, `reference-transaction`, `post-index-change`, `fsmonitor-watchman`,
   `p4-changelist`) and `README` (`0700`, marker on line 2,
   `render.is_generated` true), `~/.gitconfig-gitpro-guard` (0600, absolute
   path), the include in `~/.gitconfig-identities`; `git config
   core.hooksPath` from any repo resolves to the dir; `guard status --json`
   `installed == "global"`; `guard.status()["installed"] == "global"`.
2. `guard install` without `-y` and no tty → `no terminal to confirm on`,
   nothing written.
3. `[guard] hooks = true` + `apply -y` installs the same files; `plan`
   shows them; `apply -y` again is `up to date`; `uninstall -y` removes
   them and the include; no file under `~/.config/gitpro/hooks` remains.
   Without `[guard] hooks`, `guard install -y` then `plan` is `up to date`
   and `apply -y` changes nothing (V-025); `guard uninstall -y` removes
   the files and the include, and a following `apply -y` does not bring
   them back.
4. Commit in a repo whose remote matches `work-github` and whose effective
   email is right → `git commit` succeeds; hook adds no output.
5. `git config user.email wrong@x` in that repo → `git commit` fails (rc
   1), stderr contains `✗ gitpro: this commit would be committed as
   wrong@x`, `fix:   git -C … config --unset user.email`;
   `GITPRO_SKIP=1 git commit` succeeds; `GITPRO_SKIP=0 git commit` is also
   skipped (any non-empty value); `git commit --no-verify` succeeds.
6. `GIT_COMMITTER_EMAIL=wrong@x git commit` → blocked, cause mentions the
   environment. `GIT_AUTHOR_EMAIL=wrong@x git commit` (author only, not
   an own email) → **not** blocked. `GIT_AUTHOR_EMAIL=me@personal.test`
   (the other identity's email) → blocked with `author is your personal
   email`.
7. `git commit --author="Colleague <c@other.test>"` → allowed. `git
   cherry-pick` of a commit authored by `c@other.test` → allowed. `git
   commit --amend --no-edit` on such a commit → allowed. `git merge` of a
   branch with foreign authors → allowed.
8. `on_mismatch = "warn"` → commit succeeds, stderr has `⚠ gitpro:` and
   `not blocking`.
9. Repo with its own `.git/hooks/pre-commit` that writes a marker file →
   after the gitpro check passes, the marker file exists (chain works);
   when the repo hook exits 3, the hook script exits 3 (passed through
   unchanged; asserted by running the script directly) and `git commit`
   exits **1** (git maps any failing hook to 1, V-021). Repo
   `.git/hooks/post-checkout` writing a marker → runs on `git checkout -b
   x` (pass-through stub). Repo `.git/hooks/prepare-commit-msg` that
   appends a line → the commit message has it. Repo
   `.git/hooks/pre-merge-commit` writing a marker → runs on `git merge
   --no-ff`.
9a. Static matrix for the block form, run with `sh` (and `sh -e`) on a
   script consisting of the managed block followed by `exit 0`, with a
   fake `gitpro` on PATH exiting 0 / 2 / 3 and with no `gitpro` on PATH:
   exit status 0 / 0 / 1 / 0 for both `pre-commit` and `pre-push` (stdin
   fed with one ref line; for `pre-push` a line after the block that
   `wc -l`s stdin sees 1). The same matrix for the global chain scripts.
   Also: the block in a file where it is the **last** statement (no `exit
   0`) still exits 0 on a clean run.
10. `chain_to = "<tmp>/oldhooks"` whose `pre-commit` writes a marker → both
    the chain_to hook and the repo hook run, repo first. `chain_to` set to
    `~/.config/gitpro/hooks` → `guard install` exit 2. A symlink
    `<tmp>/loop -> ~/.config/gitpro/hooks` as `chain_to` → the hook does
    not recurse (commit completes within the test timeout, marker from the
    repo hook present).
11. Fake `gitpro` on PATH replaced by scripts that exit 1 / exit 2 (with a
    `✗` line) / crash (`raise`) / are `exit 127` (missing module) → `git
    commit` succeeds in every case, stderr has `⚠ gitpro guard: exited
    <rc>; not blocking`. With no `gitpro` on PATH and no fallback → commit
    succeeds silently.
12. pre-push: two commits committed as `wrong@x` on top of the pushed
    base → `git push` to `bare_remote` fails, stderr lists `2 commits` and
    the rebase fix; after rewriting, push succeeds. The hook's stdin is
    forwarded: a repo `pre-push` hook that counts stdin lines sees 1 line.
    A push of a branch whose commits are already on another remote-tracking
    ref (fetched from a second bare remote) → not listed (`--not
    --remotes`). Pushing with `git push <url>` (URL instead of name) →
    works, identity chosen by URL. A branch carrying two commits with
    committer **and** author `pat@other.test` (not in the config) on top
    of the user's own clean commits → push succeeds (V-010); the same
    with committer `me@personal.test` (own, wrong) → blocked. Remote
    ahead: the bare remote receives a commit from a second clone, no
    fetch, then push from the first clone → the hook does not fail on the
    unknown remote sha (`cat-file -e` guard, V-023); the push is rejected
    by git as non-fast-forward, not by the hook.
13. `git config filter.lfs.process "git-lfs filter-process"` in a repo and
    a fake `git-lfs` on PATH logging its argv → `git push` logs `pre-push
    origin <url>` once; without the config no `git-lfs` call; with a repo
    `.git/hooks/pre-push` that itself runs `git lfs pre-push "$@"` → the
    log has exactly one call (stub skipped its own). Set globally
    (`git config --global filter.lfs.process …`) → the stub calls it in a
    repo with no local LFS config too.
14. Config missing (`rm` the TOML) → `git commit` succeeds with `⚠ gitpro
    guard: no config found …; not blocking`.
15. Global `core.hooksPath=/elsewhere` already set in `~/.gitconfig` and
    `chain_to` empty → `guard install` exit 2 naming scope `global`; with
    `chain_to` set → installs. System scope: `GIT_CONFIG_SYSTEM=<tmp>/sys`
    (with `GIT_CONFIG_NOSYSTEM` unset for this test) setting
    `core.hooksPath` → `guard status` prints the `⚠ system gitconfig`
    line and JSON `system_hooks_path`.
16. Repo with `git config core.hooksPath .husky` and an untracked
    `.husky/pre-commit` → `guard status --repos` lists it under bypassed;
    `guard install --repo <repo> -y` inserts the block after the shebang,
    keeping the rest; backup exists in the state dir (not in the repo),
    undo script restores it byte-for-byte; `git -C repo status --porcelain`
    shows no new files in the repo except the edited hook. With the hook
    file `git add`ed (tracked) → exit 2 with `tracked by git`, file
    unchanged. `.husky/_/` untouched; `core.hooksPath .husky/_` → exit 2
    naming `.husky/pre-commit`; a hook dir listed in `.gitignore` → exit
    2 with `managed by`.
17. Repo mode block: fake `gitpro` exiting 2 → commit proceeds; exiting 3
    → refused (`git commit` exits 1); `GITPRO_SKIP=1` → proceeds; no
    `gitpro` on PATH → proceeds; **real** gitpro, clean repo, and the hook
    file created by gitpro (block only, nothing after it) → `git commit`
    succeeds (the block's last statement does not fail the hook, V-002).
    `guard install --repo` on a repo with no hook file at all creates it
    with `#!/bin/sh` + the block, and a commit in that repo succeeds.
18. `guard test <repo>` on a mismatching repo → exit 3 with the same text;
    on a good repo → `✓ guard: would allow commits as me@corp.test
    (work-github)`, exit 0.
19. `guard check pre-commit` in a repo where `git` hangs (fake `git`
    sleeping 10 s for `var`) → finishes in < 7 s with `⚠ … timed out; not
    blocking`, exit 0.
20. Timing: `guard check pre-commit` in a good repo completes in < 1 s in
    the test (CI margin); `python -X importtime -c "import gitpro.guard"`
    shows no `rich`, `gitpro.cli`, `gitpro.view`.
21. 01's `test_hot_imports` covers the import rule; this package adds:
    `guard.HOOKS` equals the table in 01 §5.6 and every entry whose module
    is importable resolves to a callable.

## 12. Modules touched

New `guard.py` (`HOOKS` per 01 §5.6, `Context`, `BLOCK = 3`,
`identity_commit(ctx)`, `identity_push(ctx)`, `check(name, args, stdin) ->
int`, `install(...)`, `uninstall(...)`, `status() -> dict` (the one
status function; there is no `is_installed`), `add_arguments(p)`,
`register(sub, common, confirm)`, `main(argv)`), `render.py`
(`hook_script(name, chain_to, gitpro_bin)`, `hook_stub(name, chain_to)`,
`hooks_gitconfig(hooks_dir)`, `HOOK_NAMES`), `plan.py` (`build` adds hook
targets, `~/.gitconfig-gitpro-guard` and the include line when
`cfg.guard.hooks` or the guard file exists; `teardown` removes them).

## 13. Docs text (for 17)

> `gitpro guard install` adds global `pre-commit`/`pre-push` hooks that
> refuse a commit or push whose committer email is not the identity the
> remote belongs to, then run the repo's own hooks (every hook is
> forwarded, including husky-style `post-checkout`). If gitpro itself
> fails, the commit goes through. Skip the check once with `GITPRO_SKIP=1`.
> `[guard] on_mismatch = "warn"` turns the block into a warning. Repos that
> set their own `core.hooksPath` (husky, lefthook, pre-commit) are not
> covered by the global hooks: run `gitpro guard install --repo <dir>` for
> an untracked hook file, or add these lines near the top of your tracked
> hook yourself:
>
> ```sh
> if [ -z "${GITPRO_SKIP:-}" ] && command -v gitpro >/dev/null 2>&1; then
>   rc=0; gitpro guard check pre-commit "$@" || rc=$?
>   if [ "$rc" -eq 3 ]; then exit 1; fi
> fi
> ```
