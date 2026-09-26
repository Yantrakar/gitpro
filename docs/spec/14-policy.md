# 14 — Team policy: org policy file, local and CI verification, GitHub Action, team status

Wave 4. Depends on 01 (`resolve`, `[policy]`, `identity.policy`,
`cli.extend_command`, the `guard.HOOKS` contract and `Context` in 01 §5.6),
08 (`trailers.policy_for`, `trailers.check`, commit listing helpers in
`audit.collect`). 06 is a soft dependency: the hook entries
`gitpro.policy:hook_commit/hook_push/hook_message` are named by 01's
fixed `guard.HOOKS` table; the end-to-end hook test is `skipif` without
`guard`.

Files this package may create or edit: new `src/gitpro/policy.py`,
`tests/test_policy.py`, new `action.yml` at the repo root. Nothing else
(not `guard.py`, not `cli.py`; the `check` flags come through
`extend_command`).

## 1. Purpose and user story

An organisation wants: commits to its repos authored with a company email,
signed, no AI co-author trailers (or the opposite: declared). A **policy
file** states that per remote owner. gitpro enforces it locally (the
guard's hooks and `gitpro check --policy`) and in CI (`gitpro check
--policy --range` over a PR's commits, wrapped in a GitHub Action).
`gitpro team` shows, per identity, whether this machine complies.

Buildable now: policy file format, local checks, CI check, the composite
Action, team status. Not built (§10): central policy distribution,
dashboards, audit logs.

User story: "Our org publishes `gitpro-policy.toml` in a repo. I set
`policy = "~/work/gitpro-policy.toml"` on my work identity. `gitpro check`
now also tells me my identity has `sign = false`; the guard blocks a
commit from my personal email; the PR check in CI fails on a commit from
a contractor's gmail address with a message pointing at the policy."

## 2. Policy file format

TOML; same permission rules as the config when loaded locally: owned by
the user and not group/world-writable (0600 and 0644 are both accepted;
0664 or 0666 is refused). In CI the file comes from the repository and
the permission check is skipped (`--range` mode; see §3.1 for which
version of an in-repo file is read). A policy file loaded from a repo
(`.gitpro-policy.toml`) must be a regular file inside that repo: a symlink
whose target leaves the repo is refused, and its `allowed_signers` must be
a relative path that stays inside the repo (`~/` and `/` paths are
refused there). A deeply nested or otherwise unparseable TOML file is an
invalid policy (exit 2), never a crash.

```toml
# gitpro policy — distributed by the org; users reference it from their identity
[policy]
name = "Example Corp engineering"
version = 1
contact = "it-sec@example.corp"           # shown in failure messages
allowed_signers = "allowed_signers"       # optional: path (relative to this file) of an ssh allowed_signers file

[[rule]]
host = "github.com"
owners = ["example-corp", "example-corp-*"]   # globs (case-insensitive); empty = every owner on host
allowed_email_domains = ["example.corp", "contractors.example.corp"]
allowed_emails = []                            # explicit exceptions (full addresses or globs)
require_signing = true                         # commits must be signed
signing_formats = ["ssh"]                      # "ssh" | "gpg" | "x509"; empty = any
co_authored_by = "forbid"                      # allow | forbid | require (same as [*.trailers])
forbid_emails = ["*@users.noreply.github.com"] # trailer/author globs that fail
require_verified_email = false                 # 05: forge email must be verified (needs --online)
```

Validation (`policy.load(path) -> Policy`): allowed keys only, `host`
`HOST_RE`, owners `OWNER_RE`/globs, domains `[A-Za-z0-9.-]+`, enums,
`version` int, `allowed_signers` `PATH_RE`. Several rules may match a repo
(host + owner); all apply. Loaded policies are cached by
`(path, mtime_ns, size)` so one command (a `team` run, one hook run) does
not re-parse the same file; the cache lives in the process only (R-063),
so every hook invocation, being a new process, parses its policy once.

Owner globs follow git's wildmatch with pathname rules: `*`, `?` and
`[...]` match within one path segment and never cross `/`; `**` as a whole
segment crosses segments (`corp/**/sub`). A rule also covers subgroups of
an owner it matches: `owners = ["corp"]` applies to `gitlab.com/corp/team/app`
(GitLab nested groups), and `corp/*` to `corp/team/sub/app`; `corp*sub`
does not match `corp/team/sub`.

Which policies apply to a repo: `[policy].files` (all repos) plus the
matched identity's `policy`.

## 3. CLI surface

```
gitpro check [--policy FILE]… [--range A..B] [--repo DIR] [--json]     # existing check + policy mode
gitpro team [-c PATH] [--online] [--json]
gitpro policy show [DIR] [--json]
gitpro policy lint FILE [--json]
```

Registration: `policy.register` adds `team` and `policy`; `policy.extend(subs)`
calls `extend_command(subs, "check", add_flags, handler)` adding
`--policy`, `--range`, `--repo`; `handler` returns `None` unless one of
them is given.

### 3.1 `check --policy` (local and CI)

Without `--range`: checks the **machine and the identity** for the repo
(`--repo DIR`, default cwd if a repo, else every identity with a policy):

```
policy Example Corp engineering (~/work/gitpro-policy.toml) for work-github
  email domain   me@corp.test       ✓ example.corp
  signing        required           ✗ identity work-github has sign = false → set sign = true, run gitpro apply
  signing key    ~/.ssh/id_work.pub ⚠ not checked (run with --online to verify it is registered)
  trailers       forbid             ✓ [identity.trailers] co_authored_by = "forbid"
1 problem
```

(The trailers line is `⚠ policy forbids Co-Authored-By but the identity
allows it → set [identity.trailers] co_authored_by = "forbid"` when they
disagree; the policy wins at commit time regardless.)

With `--range A..B` (CI mode): the commits in the range of `--repo` (or
cwd) are checked against the policies matching the repo's `origin`. When
no policy applies and `--policy` is not given, the check looks for
`.gitpro-policy.toml` in the repo root, then fails `✗ no policy applies
to <remote>`. A user config that does not load is a warning in this mode
(`⚠ config not used: …`) and the check goes on with the repo's policy.

**The base version judges a range.** A policy file inside the repo —
`.gitpro-policy.toml`, a `--policy FILE` whose path is inside the repo, or
an identity's `policy` that points into it — and a relative
`allowed_signers` it names are read from the range's base commit (`git
show <base>:<path>`), never from the working tree, so a pull request
cannot widen the policy that judges it (add `gmail.com`, list its own
key). The base is the one commit the range excludes (`A` of `A..B`, the
merge base of `A...B`); a range with no single base (`HEAD`, `A B ^C ^D`)
is an error (exit 2) unless the policy comes from outside the repo.
The base must be a commit of a protected branch: the Action's default
range (`pull_request` `base.sha..head.sha`) is; a custom range whose base
the PR author can choose is not, since the base's policy is the one that
judges. `--policy FILE` outside the repo is read as given; a path counts
as inside when its resolved path is in the resolved repo (any symlinked
spelling) or its plain spelling is (a symlink in the checkout pointing
out is then refused from the base tree). Changes made by merge commits
count (diffed against their first parent). When the range changes
the policy file or its `allowed_signers` (a commit in the range touches
it, or the checkout differs from the base), the output says `⚠ this range
changes <file>; it is judged by the base version (<base>)`; when the base
has no policy file yet, `⚠ <file>: policy added in this range; not
enforced until merged` and no repo policy applies (exit 0 unless another
policy applies). The warnings go to stderr and to `warnings` in the JSON.
Protect the policy with CODEOWNERS (`/.gitpro-policy.toml
@example-corp/security`, the same for `allowed_signers`) and a branch
protection rule requiring code owner review, so changes to it are
reviewed before they start to judge.

```
policy Example Corp engineering for github.com/example-corp/app, 4 commits in origin/main..HEAD
  a1b2c3d  2026-10-01  pat@gmail.com         ✗ email domain gmail.com not allowed (example.corp, contractors.example.corp)
  b2c3d4e  2026-10-01  me@example.corp       ✗ not signed (require_signing)
  c3d4e5f  2026-10-01  me@example.corp       ✗ Co-Authored-By: Claude <noreply@anthropic.com> forbidden
  d4e5f6a  2026-10-02  me@example.corp       ✓ signed (ssh, good signature)
3 of 4 commits violate the policy; contact it-sec@example.corp
```

Author/committer: both emails of each commit are checked against
`allowed_email_domains`/`allowed_emails`; a violation names which one.

Signing check: `git log --format=%H%x00%G?%x00%GK%x00%GS` with
`-c gpg.ssh.allowedSignersFile=<file>` when the policy ships
`allowed_signers`, else the user's setting. `%G?` classification:

| `%G?` | meaning | result |
|---|---|---|
| `G` | good signature | signed |
| `U` | good, unknown validity | signed |
| `X` | good, expired | signed, `⚠ expired` |
| `Y` | good, expired key | signed, `⚠ expired key` |
| `R` | good, revoked key | **violation** `signed with a revoked key` |
| `E` | cannot check (missing key / no allowed signers) | signed with `⚠ unverifiable here`; a **violation** when the policy ships `allowed_signers` and requires signing: `signed by a key that is not in the policy's allowed_signers` for ssh, `gpg signature cannot be checked: the policy's allowed_signers lists ssh keys only` for gpg/x509 |
| `B` | bad signature | violation |
| `N` | no signature | violation |

`signing_formats` is checked from the signature header of each commit
(`gpgsig` starting `-----BEGIN SSH SIGNATURE-----` is ssh, `PGP` gpg,
`SIGNED MESSAGE` x509). Every `E` counts as unverifiable; the summary
then adds `⚠ N signature(s) could not be verified in this environment`
(JSON `unverifiable`).

Signer and committer: git does not bind an ssh signature's principal to
the committer email — a key listed for `alice@` verifies a commit whose
committer claims `bob@` as `G`. Nor is `%GS` the principals field: `ssh-keygen
-Y find-principals` prints one principal per line and git records only
the first one that verifies, so `%GS` never shows the rest of a list or
its `!` exclusions. gitpro therefore ignores `%GS` here and reads the
shipped `allowed_signers` itself (in `--range` mode the base copy): every
line whose key fingerprint (`SHA256:` + unpadded base64 of the SHA-256 of
the decoded key blob) equals `%GK` contributes its principal patterns
(option fields such as `namespaces="git"` and `valid-after=` and quoted
principals are parsed; lines whose `namespaces` exclude `git`, and
`cert-authority` lines, are skipped). On that union, OpenSSH pattern-list
rules apply: any matching `!pattern` denies, otherwise any matching
pattern allows (`*`/`?` globs; compared case-insensitively). When the
policy ships `allowed_signers`, an ssh signature (`G`/`U`) whose committer
email is not allowed this way is a violation with rule `allowed_signers`:
`signed by <principals> (allowed_signers), but committed as <email>`.
When no line matches `%GK` (a key certified by a `cert-authority` line),
`%GS` alone is compared. `valid-after`/`valid-before` are left to
ssh-keygen's own verification and not re-checked per line. gpg/x509 signatures are
checked against this machine's keyring only; their signer identity is
not compared with the committer (a limit of this package).

Exit 1 on any violation, 0 clean, 2 errors.

### 3.2 Hook entries (01 §5.6 contract)

`policy.hook_commit(ctx)`, `hook_push(ctx)`, `hook_message(ctx)` are
named in `guard.HOOKS`. Each: load applicable policies for `ctx.repo` (none
→ `return 0` immediately, no further work); apply the email rules to
`ctx.committer_email`/`ctx.author_email` (commit), to every commit being
pushed (push, via the same listing 06 uses), the trailer rules to the
message file (commit-msg; `trailers.check` from 08). Signing cannot be
checked pre-commit (the signature does not exist yet); `hook_push` checks
`%G?` for pushed commits when `require_signing`. Messages use the 06
block with `policy: <name> (<contact>)` as cause; return `guard.BLOCK`
(3) on violation, 0 otherwise, never raise (errors → `ctx.say("⚠ gitpro
policy: …")`, 0). Import rule as 08: stdlib + `gitpro.config` +
`gitpro.trailers` at module level.

### 3.3 `gitpro team`

```
team status (this machine)
  identity      policy                       email  signing  key registered  verified      trailers
  work-github   Example Corp engineering     ✓      ✗        ⚠ (--online)    ⚠ (--online)  ✓
  gitlab-work   (none)                       ·      ·        ·               ·             ·
guard: installed (block)     agent hook: claude ✓
1 identity violates its policy → gitpro check --policy
```

`--online` fills the registered-key (09) and verified-email (05) columns
(`verified` is `·` when no rule has `require_verified_email`). It opens
one `forge.Session` for the identities (and `[default]` when it has an
account) and its gate (`explicit=True`) prints the single announce line
for every forge call of the run — the key lists and `user/emails` — first
on stdout, on stderr under `--json` (so the JSON stays valid);
`health.check_keys` runs with `announce=False`. A result the forge did not
give (an account that did not answer, a token without the email scope) is
unknown: `⚠ could not check (<reason>)`, never "not verified"; the reasons
are `⚠` lines on stderr and `warnings` in the JSON. Without gh/glab, or
with no identity that has a `gh_account`/`glab_account`, exit 2.
`guard`/`agents` rows come from `gitpro.guard`/`gitpro.agents` when
importable, else `· not installed`.

### 3.4 GitHub Action (`action.yml` at the repo root, composite)

`action.yml` is the source of truth; its header comment is the usage
example. Inputs: `policy` (default `.gitpro-policy.toml`), `range`
(default `${{ github.event.pull_request.base.sha }}..${{
github.event.pull_request.head.sha }}`), `comment` (default `"false"`)
and `working-directory` (default `.`: the checkout to check). Steps:

1. `python3 -m venv "$RUNNER_TEMP/gitpro"` and `pip install --quiet
   "$GITHUB_ACTION_PATH"`.
2. With `GITPRO_POLICY`, `GITPRO_RANGE`, `GITPRO_WORKDIR` in `env:`: `cd`
   to the working directory, refuse an empty range and a shallow clone
   (`::error::`, exit 2), then `gitpro check --policy="$GITPRO_POLICY"
   --range="$GITPRO_RANGE" --json | tee "$RUNNER_TEMP/gitpro-policy.json"`
   (the `=` form: an input starting with `-` cannot be read as an option).
3. `if: inputs.comment == 'true' && failure()`, `actions/github-script`:
   skips with a notice outside pull requests and on fork PRs (their
   `GITHUB_TOKEN` is read-only; the check step still fails the job); posts
   `gitpro did not produce a result; see the check step` when the JSON
   file is missing or unreadable; otherwise one line per violation (at
   most 50). Commit-derived text (messages, warnings, contact) goes into
   code spans with backticks and newlines replaced and every `@` followed
   by a zero-width space, so a commit cannot inject markdown, links or
   @mentions. A failed `createComment` (missing permission) is a notice,
   not a second failure.

The policy that judges a PR is the base version (§3.1): recommend
CODEOWNERS on `.gitpro-policy.toml` and `allowed_signers`, and keep the
range base a protected-branch commit (the default `base.sha..head.sha`
is safe; the action.yml header says so).

Installation from the Action's own checkout (`$GITHUB_ACTION_PATH`,
DECISIONS Q7) into a venv under `$RUNNER_TEMP` (V-012): the runner's
`python3 -m pip install` without a venv is refused on Ubuntu 24.04 images
(PEP 668 "externally-managed-environment") and would otherwise pollute
the runner's Python; the venv's `gitpro` is called by absolute path, so
nothing needs to be on PATH. The version used is the ref in `uses:
sandeepshelke/gitpro@<ref>`. Inputs reach the shell through `env:`,
never interpolated into `run:` (a crafted `range` input cannot inject).
The default range is empty on non-`pull_request` events (`push`,
`workflow_dispatch`: the two `sha` fields are unset) and the step fails
with a clear `::error::` instead of letting `git rev-list ..` mean
`HEAD..HEAD` (V-033); the same for a shallow clone, which would make the
range incomplete. The workflow must check out with `fetch-depth: 0`. No config file is
needed in CI mode: identity matching is skipped, rules match on the
remote URL alone. The policy file's permission check is skipped in
`--range` mode. License: MIT, same as the repo (Q1).

GitLab CI (merge request pipelines), ready to paste into `.gitlab-ci.yml`.
The base version of the policy judges here too. With
`CI_MERGE_REQUEST_DIFF_BASE_SHA` (always set) the base is the MR's fork
point, so the policy as of the fork point judges it — a policy tightened
on the target branch since then is not applied. Merged results pipelines
also set `CI_MERGE_REQUEST_TARGET_BRANCH_SHA` (the target branch tip):
the snippet prefers it, so the current target-branch policy judges. Both
are commits of the protected target branch, as the base must be. gitpro
refuses to run as root and most CI images run as root, so the job runs it
as an ordinary user; `pipefail` keeps `tee` from hiding the exit code:

```yaml
gitpro-policy:
  image: python:3.12
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  variables:
    GIT_DEPTH: "0"                # the whole history; a shallow clone is refused
  script:
    - git fetch --quiet origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
    - useradd --create-home ci && chown -R ci "$CI_PROJECT_DIR"
    - su ci -c 'python3 -m venv ~/gitpro && ~/gitpro/bin/pip install --quiet "git+https://github.com/sandeepshelke/gitpro.git@<ref>"'
    - su ci -s /bin/bash -c 'set -o pipefail; ~/gitpro/bin/gitpro check --policy .gitpro-policy.toml --range "${CI_MERGE_REQUEST_TARGET_BRANCH_SHA:-$CI_MERGE_REQUEST_DIFF_BASE_SHA}..$CI_COMMIT_SHA" --json | tee gitpro-policy.json'
  artifacts:
    when: always
    paths: [gitpro-policy.json]
```

Protect `.gitpro-policy.toml` with a CODEOWNERS entry and "require code
owner approval" on the protected branch.

Capabilities registered: `policy`, `check.policy`, `team`.

## 4. `--json`

`check --policy --range`:

```json
{"ok": false, "command": "check", "mode": "policy", "repo": "/w/app", "remote": "git@github.com:example-corp/app.git",
 "policies": [{"name": "Example Corp engineering", "file": "/w/app/.gitpro-policy.toml", "rules": 1}],
 "range": "origin/main..HEAD", "commits": 4,
 "violations": [{"sha": "a1b2c3d...", "author_email": "pat@gmail.com", "committer_email": "...", "rule": "allowed_email_domains", "which": "author", "message": "email domain gmail.com not allowed (example.corp, contractors.example.corp)"}],
 "signing": [{"sha": "...", "status": "G", "signed": true, "warning": null}],
 "contact": "it-sec@example.corp"}
```

Machine mode: `"mode": "machine", "identities": [{"id", "policy",
"checks": [{"name": "signing", "ok": false, "message", "fix"}]}]`.
`team --json`: `{"ok", "command": "team", "identities": [...same...],
"guard": {...06 status...}|null, "agents": {...10 status...}|null,
"warnings": [...forge warnings, --online...]}`. `check --policy --range`
also carries `"unverifiable": N` and `"warnings": [...]` (§3.1).
`policy show --json`: the matching rules. `policy lint --json`: `{"ok",
"errors": [...]}`.

## 5. Config keys

`[policy] files`; identity `policy` (01 validates paths).

## 6. VS Code surface

None in this package. 15's tooltip shows `policy: <name> ✓/✗` when `why
--json` carries a `policy` problem. The hook point is
`resolve.EXTRA_CHECKS` (01 §4.3): `policy.py` does
`resolve.EXTRA_CHECKS.append(check_report)` at import time, where
`check_report(report) -> list[Problem]` yields problems with code
`policy`. `cli.build_parser` imports `gitpro.policy` through
`EXTENSIONS`, so every command that calls `resolve.compare` after parsing
(`why`, and the other commands built on `resolve.compare`) sees policy
problems; `scan` does not call `compare`. Plain `gitpro check` (no
`--policy`/`--range`/`--repo`) stays the "would `apply` change anything"
check of 00 §4.4 and does not look at policies; the hot path (`guard
check`) gets them through `HOOKS` entries instead and does not import
`policy` for `compare`. There is no try-import in `resolve`.

## 7. Files created/changed

None. All commands are read-only. `action.yml` is repo content.

## 8. Failure modes

| situation | message | exit |
|---|---|---|
| policy file missing | `✗ policy <path>: not found (identity work-github)` | 2 |
| policy file group/world-writable (local mode) | same message as config | 2 |
| policy file not valid TOML (also: nested too deeply) | `✗ <path>: invalid TOML: …` | 2 |
| repo policy or its `allowed_signers` leaves the repo (symlink, `~/`, `/`) | `✗ … leaves the repo (a symlink?); refused` / `allowed_signers must be a path inside the repo` | 2 |
| `--range` without a single base and an in-repo policy | `✗ the range … has no single base commit to read <file> from; …` | 2 |
| `--range` changes the policy / `allowed_signers` | `⚠ this range changes <file>; it is judged by the base version (<base>)` | (as judged) |
| `--range` adds the policy | `⚠ <file>: policy added in this range; not enforced until merged` | 0 |
| `--range` with a broken user config | `⚠ config not used: …`, the repo policy applies | (as judged) |
| invalid policy | `✗ <path>: <where>: <problem>` | 2 |
| `--range` with no policy | `✗ no policy applies to <remote>; pass --policy FILE or add .gitpro-policy.toml` | 2 |
| `allowed_signers` path missing | `✗ <policy>: allowed_signers <path> not found` | 2 |
| no commits in range | `✓ no commits in <range>` | 0 |
| `team --online` without gh/glab | `✗ team --online needs gh or glab on PATH …` | 2 |
| `team --online`, no identity has a forge account | `✗ team: no identity has a gh_account or glab_account; nothing to ask` | 2 |

## 9. Security / privacy

Policy files are data, never executed. CI mode reads commit metadata only.
`require_verified_email` needs `--online` and goes through 05's rules. The
Action uses the default token only for the optional comment step, and
puts commit-derived text into code spans there (§3.4). In `--range` mode
an in-repo policy and its `allowed_signers` come from the range base, so
the reviewed, merged version judges a PR (§3.1); the remaining trust is in
who may merge changes to the policy file (CODEOWNERS).

## 10. Out of scope / future

Central policy distribution and updates (today: the org commits the file
to a repo; users point `policy` at their checkout), per-org dashboards,
audit logs across machines, forge-side enforcement (branch protection),
seat management. Nothing in this package depends on them.

## 11. Acceptance tests (`tests/test_policy.py`)

Fixture: `policy_file(tmp_path, text)` with mode 0600; `cfg()` with
`work-github` (`policy = <file>`) and `repo_with_remote` on
`github.com/example-corp`. Signed commits: generate an ed25519 key with
`ssh-keygen` in the test (real `ssh-keygen`; `skipif` absent), `git -c
gpg.format=ssh -c user.signingkey=<pub> -c commit.gpgsign=true commit`,
and an allowed_signers file.

1. `policy lint` on the example → ok; unknown key → error names it;
   `co_authored_by = "maybe"` → error; `allowed_signers` pointing
   nowhere → error.
2. `policy show <repo>` → one matching rule; a repo on another owner →
   `no policy applies`.
3. Machine mode: identity with `sign = false` and policy
   `require_signing` → `✗ … sign = true`, exit 1, JSON check `signing.ok
   false`; with `sign = true` and key present → `✓`.
4. Email domain: identity email `me@corp.test` vs domains
   `["example.corp"]` → `✗`; `allowed_emails = ["me@corp.test"]` → `✓`.
5. Range mode: 4 commits as in the example → 3 violations with the
   expected `rule` values and `which`; exit 1; JSON has `contact`; the
   signed commit has `signing.status == "G"`.
6. Range mode without a config file (CI) and `--policy` → works, matches
   by remote only; policy file mode 0644 accepted (in every mode);
   group/world-writable (0664) refused in machine mode, accepted in
   `--range` mode.
7. `.gitpro-policy.toml` committed in the range base and no `--policy` →
   found; a policy the range widens is judged by the base version; a
   policy the range adds is not enforced (warning).
8. No policy applies and none found → exit 2.
9. `%G?` table: a commit signed with a key **not** in the allowed signers
   (`E`) → signed with `⚠` when the policy has no `allowed_signers`,
   violation when it has; `N` → violation; a tampered signature (`B`,
   produced by editing the commit object with `git replace`/`cat-file`
   round trip) → violation; `signing_formats = ["gpg"]` with an ssh
   signature → violation `format ssh not allowed`.
10. Hook entries: `policy.hook_commit(ctx)` with `committer_email =
    "x@gmail.com"` → 3 and the `✗ gitpro:` block via `ctx.say` names
    `policy: Example Corp engineering (it-sec@example.corp)`; allowed
    email → 0; repo with no applicable policy → 0 and no git call
    (counter); `hook_message` with a forbidden trailer → 3.
11. (needs 06, `skipif`) `guard install -y`, policy forbids gmail, `git -c
    user.email=x@gmail.com commit` → blocked with the policy line;
    `GITPRO_SKIP=1` passes.
12. `team` → identity row with `✗` under signing, exit 1; `--json` has
    `guard`/`agents` keys (null when those modules are absent, via
    `sys.modules` monkeypatch).
13. `action.yml`: exists, contains `using: composite`, `python3 -m venv
    "$RUNNER_TEMP/gitpro"`, `bin/pip" install --quiet
    "$GITHUB_ACTION_PATH"`, `bin/gitpro" check --policy`,
    `is-shallow-repository`, and no `${{ inputs.` inside a `run:` block
    (regex over the text). The install and check steps are also run
    locally by the test as a shell script with `RUNNER_TEMP=<tmp>`,
    `GITHUB_ACTION_PATH=<repo root>` and a `GITPRO_RANGE` of `..` → exit
    2 with `commit range is empty`; with a real range in a test repo →
    JSON on stdout (this is the "verify on a runner" stand-in; the first
    release additionally runs the Action once in the repo's own CI).
13a. `resolve.EXTRA_CHECKS` contains `policy.check_report` after `import
    gitpro.policy`; `why --json` on a repo violating a rule has a problem
    with `code == "policy"`.
14. Policy cache: load twice → one parse (monkeypatch `tomllib.loads`
    counter); touch the file with new content → re-parsed.

## 12. Modules touched

New `policy.py` (`load`, `applicable(repo_or_url, cfg)`, `check_machine`,
`check_range`, `check_repo`, `hook_commit/hook_push/hook_message`,
`team`, `register`, `extend`), `action.yml` (new), `tests/test_policy.py`.

## 13. Docs text (for 17)

> Teams describe their rules once in a policy TOML (allowed email domains
> per owner, signing required, trailer rules). Point an identity's
> `policy` at it: `gitpro check --repo .` (in a repo) or `gitpro check
> --policy FILE` (every identity) reports what this machine violates, and
> `gitpro why` shows the policy problems of a repo next to the identity
> ones. The guard blocks offending commits, and `gitpro check --policy
> FILE --range base..head` verifies a PR in CI (`uses:
> sandeepshelke/gitpro@main`; the policy file is read from the PR's base,
> so protect it with CODEOWNERS). `gitpro team` is the one-screen summary
> and ends with the `gitpro check --policy …` command for the details.
> Plain `gitpro check` is unchanged: it only says whether `gitpro apply`
> would change anything.
