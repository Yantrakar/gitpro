# 08 — `gitpro audit`: wrong-author and agent-authored commits; trailer policy; commit-msg guard

Wave 3. Depends on 01 (`resolve`, `[agents].bot_emails`, `[*.trailers]`,
`undo`, the `guard.HOOKS` contract and `Context` in 01 §5.6). 06 is a soft
dependency: `trailers.hook_message(ctx)` is the function 06's table names
for `commit-msg`; the end-to-end hook test is `skipif` without `guard`.

Files this package may create or edit: new `src/gitpro/audit.py`,
`src/gitpro/trailers.py`, `tests/test_audit.py`, `tests/test_trailers.py`.
Nothing else; in particular **not** `guard.py` (the table already names
`gitpro.trailers:hook_message`).

## 1. Purpose and user story

Two audits of history, one policy:

- **Wrong author**: commits in a repo whose committer or (own-email)
  author is not the identity the remote belongs to. Unpushed ones can be
  rewritten; pushed ones are reported only.
- **Agent-authored**: commits made by or with an AI agent (`Co-Authored-By:`
  trailers, bot emails), so a user can see what an assistant committed
  under their name.
- **Trailer policy** per identity: `co_authored_by = allow | forbid |
  require`, `forbid_emails` globs; checked by `audit` and, through the
  `commit-msg` hook (06), at commit time.

User story: "Before opening a PR from my work repo I run `gitpro audit`. It
finds 2 unpushed commits with my personal email and prints the one rebase
command that fixes exactly those. It also shows that 5 of the 40 commits
have a `Co-Authored-By: Claude` trailer, which my employer forbids; the
commit-msg hook will stop the next one."

## 2. CLI surface

```
gitpro audit [DIR] [--range A..B] [--all] [--agents] [--authors] [--rewrite] [-c PATH] [-y] [--json] [-v]
```

Default range: `HEAD --not --remotes` (commits on no remote-tracking ref,
**as of the last fetch**; the header says so) plus a summary count for
the pushed part; `--all` audits every commit reachable from `HEAD`;
`--range` any `git rev-list` range. `--authors` / `--agents` select one
audit (default both).

Data: one `git log --format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%cI%x00%P%x00%s%x00%(trailers:only,unfold)%x1e <range>`
call; records are split on `\x1e` first, then each record on `\x00`
(a subject or trailer cannot contain either byte; `%x1e` is the record
separator because `--format` emits no trailing newline marker that is
safe). "Pushed" = commit is in the set `git rev-list --remotes` computed
once, i.e. reachable from a remote-tracking ref **as of the last fetch**.
Commits published only through a tag, `git push <url>` (no tracking ref)
or Gerrit's `refs/for/*` count as unpushed; `--help` says so.

What counts as wrong (same rule as 06 §2): committer email ≠ expected, or
author email ≠ expected **and** author email is one of the user's own
emails (`resolve.own_emails(cfg)`). A colleague's authorship is never
flagged.

Text output:

```
~/work/app  identity work-github <me@corp.test>  41 commits (38 pushed, 3 unpushed, as of last fetch)

wrong author (2 unpushed, 1 pushed)
  a1b2c3d  2026-09-30  me@personal.test   fix tests                       unpushed
  d4e5f6a  2026-09-30  me@personal.test   add login                       unpushed
  9f8e7d6  2025-02-11  me@personal.test   initial import                  pushed · report only
→ rewrite the 2 unpushed: gitpro audit --rewrite ~/work/app
  (fixes only those 2 commits and keeps every date; the commits above them get new ids but are otherwise unchanged)
  pushed commits are left alone; rewriting shared history needs a force push and everyone's agreement

agent commits (5)
  Co-Authored-By: Claude <noreply@anthropic.com>              5 commits   policy work-github: forbid  ✗
  b2c3d4e  2026-09-29  me@corp.test   refactor parser          Co-Authored-By: Claude <noreply@anthropic.com>
  ...
→ [identity.trailers] co_authored_by = "forbid" for work-github; the commit-msg guard blocks new ones (gitpro guard status)
```

`✓ all 41 commits are committed as me@corp.test; no agent trailers` when
clean. The `→ rewrite` line is only printed when every wrong-author
unpushed commit is on the current branch and the range is linear (no
merge commits, checked via `%P` having one parent); otherwise `→ 2
unpushed commits need a manual rebase (merge commits in range)`. The base
is the merge-base of HEAD and its upstream, or the newest commit reachable
from any remote-tracking ref.

### 2.1 `--rewrite`

The one writing action of this package. Rewrites **only the offending
commits** (reviewer R-032) and leaves every other commit's tree, message,
author, committer and both dates unchanged. It does not use `git rebase`
(V-011: `rebase --exec` re-runs every commit through `git commit`, which
re-stamps the committer and committer date of untouched commits and can
stop on hooks or conflicts). It replays the range with plumbing:

1. `base` = the newest commit of the range that is reachable from a
   remote-tracking ref (`git merge-base HEAD --all $(git rev-list
   --remotes)` style: the merge-base of HEAD and its upstream, else the
   first commit of `git rev-list --first-parent HEAD` that `git
   rev-list --remotes` contains). The commits to replay are `git
   rev-list --reverse --first-parent <base>..HEAD`; the range must be
   linear (every commit has exactly one parent, checked via `%P`), else
   `rewrite.possible` is false.
2. For each commit `C`, oldest first, with `parent` starting at `base`:
   read `%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI` and the raw message
   (`git log -1 --format=%B`, written to a temp file, so bytes, trailers
   and trailing newlines survive). Decide:
   - committer is replaced when `ce` is in the offending set (**exact,
     case-sensitive string match** against the set built by the audit:
     own emails ≠ expected);
   - author is replaced when `ae` is in `own_emails(cfg)` and ≠ expected;
   - otherwise both are kept as they are.
   If nothing is replaced **and** `parent` is still `C`'s original
   parent, `C` is reused unchanged (same sha). Otherwise run
   `git commit-tree C^{tree} -p <parent> -F <msgfile>` with
   `GIT_AUTHOR_NAME/EMAIL/DATE` and `GIT_COMMITTER_NAME/EMAIL/DATE` set
   from the record (replaced name/email from the expected identity, dates
   always the originals), `-c commit.gpgsign=false` is **not** passed:
   `commit-tree` signs when `commit.gpgsign` is set, which is what the
   user's identity asks for. The new sha becomes `parent`.
3. Finish with `git update-ref -m "gitpro audit --rewrite"
   refs/heads/<branch> <new tip> <old tip>` (refuses if the branch moved
   meanwhile), then `git reset --soft <new tip>`-free: the index and work
   tree are untouched because the tree of the tip is identical.

So every offending commit gets a new sha with corrected identity and its
original dates; every descendant of the first rewritten commit gets a new
sha with **identical** tree, message, author, committer and dates
(only its parent changed); commits below the first rewritten one keep
their sha. Hooks never run (plumbing), so `--no-verify` is not needed.
The replay happens with the index and work tree untouched; the only
visible change is the branch ref.

`audit` refuses `--rewrite` when `resolve.compare` still reports a
mismatch: `✗ git would still commit as <email>; fix the repo first:
<fix>` (the replacement identity is `report.expected`, and it must be
what git would use next time).

Before running: confirmation (`rewrite 2 unpushed commits? [y/n]`, `-y`
skips), then an undo script `~/.local/state/gitpro/audit-undo-<ts>.sh`
(via `undo.write`) containing `git -C <repo> update-ref -m "gitpro audit
undo" refs/heads/<branch> <old tip> <new tip>` (the `<new tip>` is filled
in after the replay by rewriting the script; until then the line uses the
old sha for both, which is a no-op) — `update-ref` with the expected old
value refuses to undo if the branch moved again. Refuses when the tree is
dirty (`✗ working tree has changes; commit or stash them first`), when a
rebase/merge/cherry-pick is in progress, when HEAD is detached, or when
any affected commit is pushed. Output: `✓ rewrote 2 commits (4 new shas:
2 fixed, 2 descendants re-parented)` + `↩ undo: sh <script>`.

### 2.2 Agent detection (`audit.classify(commit, cfg)`)

- trailer `Co-Authored-By:` (any case) whose email matches any
  `[agents].bot_emails` glob (`fnmatch`, case-insensitive) → `agent_trailer`
- author or committer email matching `bot_emails` → `agent_author`
- any other `Co-Authored-By` → `co_authored` (human pair; counted
  separately, never a policy violation unless `forbid_emails` matches)

Trailer policy resolution (`trailers.policy_for(identity, default)`):
identity `trailers.co_authored_by`, else default, else `allow`;
`forbid_emails` likewise. `require` means: every commit must carry at
least one `Co-Authored-By` (teams that want AI usage declared).
Violations are `✗` lines and exit 1.

### 2.3 `commit-msg` hook entry (`trailers.hook_message(ctx) -> int`)

Named by 06's `guard.HOOKS["commit-msg"]`; called with the `Context` from
01 §5.6 (`ctx.args[0]` is the message file path, `ctx.cfg`, `ctx.report`,
`ctx.say`). Reads the trailers with `git interpret-trailers --parse
<file>`. Applies the policy for `ctx.report.expected`'s identity:

```
✗ gitpro: Co-Authored-By: Claude <noreply@anthropic.com> is not allowed for identity work-github ([identity.trailers] co_authored_by = "forbid")
  fix:   remove the trailer from the message
  skip once: GITPRO_SKIP=1 git commit ...
```

`require` violation: `✗ gitpro: identity work-github requires a
Co-Authored-By trailer on every commit`. Returns `guard.BLOCK` (3) to
block; 0 otherwise. `[guard] on_mismatch = "warn"` is applied by 06
(the entry still returns 3; 06 turns it into a `⚠` and exit 0). The entry
never raises: any error is `ctx.say("⚠ gitpro trailers: …")` and `return 0`.
Import rule: `trailers.py` imports only stdlib and `gitpro.config` at
module level (it runs in the hot path).

Capabilities registered: `audit`, `audit.rewrite`, `trailers`.

## 3. `--json`

```json
{"ok": false, "command": "audit", "repo": "/home/me/work/app", "branch": "main",
 "identity": {"id": "work-github", "email": "me@corp.test"},
 "range": "HEAD --not --remotes", "commits": 41, "pushed": 38, "unpushed": 3,
 "wrong_author": [{"sha": "a1b2c3d...", "date": "2026-09-30T10:00:00+02:00", "author_email": "me@personal.test",
                   "committer_email": "me@personal.test", "subject": "fix tests", "pushed": false, "why": "committer"|"author"}],
 "rewrite": {"possible": true, "base": "7c6b5a4...", "commits": 2, "replayed": 4, "reason": null},
 "agents": [{"sha": "...", "kind": "agent_trailer", "who": "Claude <noreply@anthropic.com>", "pushed": true}],
 "co_authored": 0,
 "policy": {"co_authored_by": "forbid", "forbid_emails": [], "violations": 5},
 "rewritten": null}
```

`ok` = no wrong author and no policy violations. `--rewrite --json` needs
`-y`; `rewritten` becomes `{"count": 2, "undo": "<path>"}`.

## 4. Config keys

`[agents] bot_emails`; `[default.trailers]`, `[identity.trailers]`
(`co_authored_by`, `forbid_emails`).

## 5. VS Code surface

None here.

## 6. Files created/changed and undo

Only with `--rewrite`: the current branch ref (rewritten commits). Undo
script restores the ref with `update-ref` guarded by the expected value.
The audit itself writes nothing.

## 7. Failure modes

| situation | message | exit |
|---|---|---|
| not a repo | `✗ <dir>: not a git repository` | 2 |
| no identity for the repo | `⚠ no identity for <host>/<owner>; author check skipped` (agent audit still runs) | per agents |
| `--range` invalid | `✗ git rev-list failed: <stderr>` | 2 |
| `--rewrite` with pushed offenders | `✗ 1 of the wrong-author commits is pushed; rewrite only covers unpushed commits (use --range to pick them)` | 2 |
| `--rewrite` while the repo still resolves the wrong email | `✗ git would still commit as <email>; fix the repo first: <fix>` | 2 |
| dirty tree / rebase in progress / detached HEAD with `--rewrite` | `✗ working tree has changes; commit or stash them first` / `✗ a rebase is in progress` / `✗ HEAD is detached` | 2 |
| a `commit-tree` call fails midway | git's stderr, then `✗ rewrite failed; the branch was not changed` (nothing was written to the ref yet; the new objects are unreachable and get collected) | 2 |
| `update-ref` refuses (branch moved during the replay) | `✗ <branch> changed while gitpro was running; nothing written` | 2 |

## 8. Security / privacy

Offline. Prints commit subjects and emails that are already in the local
history. `--rewrite` only touches unpushed commits on the current branch
and only the offending ones.

## 9. Out of scope

Rewriting pushed history (ever); signing status of commits (`%G?` is 14);
detecting agents without trailers (impossible); GPG.

## 10. Acceptance tests (`tests/test_audit.py`, `tests/test_trailers.py`)

Setup: `cfg()` with `work-github` (`[identity.trailers] co_authored_by =
"forbid"`) and `bare_remote`; `repo_with_remote(..., commits=3)` then push;
01's `commit(repo, msg, author=, committer=, trailers=, date=)`. No guard
is installed in these tests.

1. Clean repo → `✓ all 3 commits`, exit 0, JSON `ok: true`.
2. Two unpushed commits committed as `me@personal.test` → `wrong_author`
   has 2 with `pushed: false`, `why == "committer"`; `rewrite.possible`
   true, `base` = pushed tip; text contains `gitpro audit --rewrite`.
3. Unpushed commit with `--author "Colleague <c@other.test>"` and the
   right committer → not listed. With author `me@personal.test` (own
   email) and the right committer → listed with `why == "author"`.
4. One pushed wrong-author commit (commit then push) → listed `pushed:
   true`, `report only`; `--rewrite` with no unpushed offenders → exit 2.
5. Merge commit in the unpushed range → `rewrite.possible` false, `reason`
   mentions merge commits.
6. `--rewrite -y` on a range of 5 unpushed commits: 1 clean, 2 committed
   as `me@personal.test`, 3 authored by `Colleague <c@other.test>` and
   committed as `me@personal.test`, 4 authored **and** committed by
   `c@other.test` (a colleague's commit, e.g. cherry-picked by them),
   5 clean. Afterwards: commit 1 has the same sha; commit 2 has author
   and committer `me@corp.test`; commit 3 has author still `c@other.test`
   and committer `me@corp.test`; commit 4 has author and committer still
   `c@other.test` (exact matching, a colleague is never rewritten) but a
   new sha (re-parented); commit 5 has the same tree, message, author,
   committer `me@corp.test` and the **same committer date** as before
   (`%cI` compared, the thing `rebase` would have changed); every commit's
   `%aI`/`%cI` equal the originals; the message of commit 3 (with a
   trailing blank line and a `Co-Authored-By` trailer) is byte-identical
   (`git log -1 --format=%B`); work tree and index unchanged (`git status
   --porcelain` empty, same before and after). The undo script exists;
   running it restores the old branch sha; running it again (branch
   moved) prints git's `update-ref` refusal and changes nothing; stdout
   has `✓ rewrote 2 commits`. No `rebase` appears in the git commands run
   (fake-free: assert with `GIT_TRACE` is unnecessary; check that
   `.git/rebase-merge` never exists during the run via a `commit-tree`
   count is enough: `git reflog` of the branch has exactly one new entry
   with message `gitpro audit --rewrite`).
7. `--rewrite` with a dirty tree → exit 2, ref unchanged. With the repo
   still having `user.email` overridden → exit 2 with `fix the repo
   first`.
8. Commit message with trailer `Co-Authored-By: Claude
   <noreply@anthropic.com>` → `agents` entry `agent_trailer`,
   `policy.violations == 1`, exit 1; with `co_authored_by = "allow"` →
   listed, violations 0, exit 0. A subject containing `%` and a message
   containing an empty line and `\x1e`-free unicode parse correctly.
9. Author email `dependabot[bot]@users.noreply.github.com` → `agent_author`.
10. Trailer `Co-Authored-By: Pat <pat@corp.test>` → `co_authored == 1`, not
    an agent; with `forbid_emails = ["*@corp.test"]` → violation.
11. `co_authored_by = "require"` and a commit without trailers → violation
    text `requires a Co-Authored-By trailer`.
12. `--all` counts all commits; `--range HEAD~1..HEAD` counts 1.
13. `trailers.hook_message(ctx)` with a forbidden trailer in the file →
    returns 3 and the `✗ gitpro:` block went through `ctx.say`; allowed →
    0; file with no trailers under `require` → 3; `git` missing
    (monkeypatched `subprocess.run` raising) → 0 and a `⚠` line.
14. (needs 06, `skipif` without `gitpro.guard`) `guard install -y`, `git
    commit -m "x" --trailer "Co-Authored-By: Claude <noreply@anthropic.com>"`
    → fails with the trailer message; `GITPRO_SKIP=1` succeeds.
15. `python -X importtime -c "import gitpro.trailers"` shows no `rich`,
    `gitpro.cli`, `gitpro.view`.

## 11. Modules touched

New `audit.py` (`collect(repo, cfg, range, all) -> dict`, `rewrite(...)`,
`classify`, `register`), `trailers.py` (`policy_for(identity, default)`,
`parse(file) -> list[(key, value)]`, `check(trailers, policy) ->
list[str]`, `hook_message(ctx)`).

## 12. Docs text (for 17)

> `gitpro audit` lists commits whose committer (or your own other email as
> author) is not the repo's identity: unpushed ones get `gitpro audit
> --rewrite`, which fixes only those commits, keeps every date and never
> runs `rebase` (the commits above them get new ids but are otherwise
> unchanged); pushed ones are reported only. It also lists commits made with an AI
> agent (`Co-Authored-By` bot trailers). Set `co_authored_by = "forbid" |
> "require"` under `[identity.trailers]` to enforce a policy; with the
> guard installed the `commit-msg` hook applies it at commit time.
