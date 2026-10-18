# 20 — One-run release, gated stage by stage

Depends on 05 (`forge.Account`, `forge.run`). Facts below are as of main 114d173
(release 0.4.0 and #22) and the repository settings read on 2026-10-17. Written
2026-10-15 after 0.3.4; refreshed 2026-10-17. Open VSX added 2026-10-18 (section 13).

Files this package may create or edit:
- new: `scripts/release.py`, `tests/test_release_script.py`;
- edit: `.github/workflows/ci.yml` (triggers only), `.github/workflows/release.yml`,
  `.github/workflows/mcp-registry.yml` (header comment only), `docs/guide.md` (the
  developing and releasing paragraphs named in section 9), and this spec.

Nothing else. `marketplace.yml` stays as it is (section 7).

## 0. Rules for this package

1. No regression. Every existing test passes unchanged.
2. Nothing is published without a passed gate. Each publishing stage waits for an
   approval on a GitHub environment. The script approves through the same API as the
   "Review deployments" button. It never goes around a gate, and never approves after a
   failed job.
3. No secret is stored, printed or logged. PyPI and the MCP Registry keep their tokenless
   OIDC sign-in. The script fetches the gh token through `forge.run` and never prints it.
4. Tests first: section 10's tests are written and fail before the script exists.
5. One commit per numbered step in section 11. The full suite runs once, in the last step:
   `env -u FORCE_COLOR .venv/bin/python -m pytest -q -p no:cacheprovider -n auto`.
   Record the baseline on main before step 1. On main 114d173 it was 1868 passed,
   3 skipped.

## 1. What happens today

Release 0.4.0 (run 38978075243, 2026-10-16) went like this:

1. The user squash-merged #20. CI ran on main (run 38976721594), and `pages.yml`
   deployed gitpro.dev in 16 s, so the site named 0.4.0 before PyPI had it.
2. A tag `v0.4.0` was pushed by hand. It started CI again (run 38978075284) and Release.
3. Release: `build` (pytest, wheel, VSIX), then `github` and `pypi` in parallel. `pypi`
   waited for the user's approval.
4. `marketplace / publish` was skipped: the repo has no variables (`AZURE_CLIENT_ID`,
   `AZURE_TENANT_ID` unset).
5. `mcp-registry / publish` waited for the user's approval, after `pypi`.
6. The user downloaded the VSIX and uploaded it at
   https://marketplace.visualstudio.com/manage/publishers/yantrakar. The Marketplace
   listed 0.4.0 about 4 minutes later.

#22 (2026-10-17) then changed two things:
- `ci.yml` runs only on `pull_request`, with `concurrency` group
  `ci-${{ github.event.pull_request.number }}` and `cancel-in-progress: true`.
- `pages.yml` has no `push` trigger. Release calls it in a `site` job after `pypi`; it
  can also be run by hand on main for a site-only fix.

Repository settings on 2026-10-17:
- Environments (every one: deployment branch `main` only, no tag rules):
  - `pypi` and `mcp-registry`: required reviewer `sandeepshelke`, "Prevent self-review"
    off;
  - `release`: a required reviewer (section 5);
  - `github-pages`: no reviewer;
  - there is no `marketplace` environment.
- Ruleset `main` on the default branch, enforced, no bypass: no deletion, no force push,
  a pull request with 0 approvals, and the checks `test (ubuntu-latest, 3.9)`,
  `test (ubuntu-latest, 3.11)`, `test (ubuntu-latest, 3.13)`,
  `test (macos-latest, 3.13)` and `vscode`.
- No repository variables.

## 2. Goals

- CI runs before a PR is accepted, not on every push.
- A release is one run, started with one click (or by the script). Each publishing stage
  waits for its own approval on the run page.
- `scripts/release.py` drives the same run from the terminal, one yes at a time.
  With `--yes` it approves every gate unattended.
- The VSIX upload stays manual (the user's decision, 2026-10-15; the Microsoft account
  that owns the publisher has no Azure tenant). The script makes it one step.

Not goals:
- Homebrew or any other new channel. (Open VSX was one until 2026-10-18; section 13.)
- Steps on the releaser's own machine, such as upgrading their install or their agent
  skill. They stay outside this repo.
- Changing what is published, or how PyPI and the Registry sign in.

## 3. CI

`ci.yml` triggers become:

```yaml
on:
  pull_request:
  workflow_dispatch:
  workflow_call:

concurrency:
  group: ci-${{ github.event.pull_request.number || github.run_id }}
  cancel-in-progress: true
```

- `pull_request` runs CI on every PR, once per push; a newer push cancels the older run
  (as since #22).
- `workflow_dispatch` lets the user run it by hand on any branch.
- `workflow_call` lets Release run the full matrix (section 4).
- The group falls back to `github.run_id`, so a run by hand or from Release has a group
  of its own and is never cancelled by, and never cancels, another run. In a called
  workflow `github` is the caller's context, so the group is Release's run ID.
- Pushes to branches, pushes to main and tags run no CI (as since #22).

## 4. Release workflow

Trigger: `workflow_dispatch` on `main`, with no inputs. The tag push trigger is removed,
so a hand-pushed tag starts nothing. Add `concurrency: {group: release,
cancel-in-progress: false}`.

Jobs, in order; each runs only when the one before it succeeded:

| Job | Environment (gate) | Does |
|---|---|---|
| `check` | none | Fails unless `github.ref` is `refs/heads/main`. Reads the version from the first word of line 1 of `src/gitpro/VERSION` (older versions and comments follow, newest first), the only place it is written, and checks that it is X.Y.Z (`scripts/versions.py`). Fails if tag `vX.Y.Z` already exists on GitHub. Outputs `version` and `tag`. |
| `ci` | none | `uses: ./.github/workflows/ci.yml`: the whole CI matrix (Linux 3.9/3.11/3.13, macOS 3.13, VS Code). |
| `build` | none | Wheel, sdist, `smoke_wheel.py`, `npm ci`, `npm test`, `scripts/versions.py --stamp` (the committed `vscode/package.json` holds the placeholder `0.0.0`, because VS Code loads no extension without a version), `vsce package`, `SHA256SUMS`, uploaded as artifact `dist`. Its own `pytest` step is dropped, because `ci` has run the suite on the same commit. |
| `github` | `release` | Creates the annotated tag `vX.Y.Z` ("gitpro X.Y.Z") on `github.sha` with the job's token (`contents: write`), through the API (`POST git/tags`, then `POST git/refs`), so the job needs no checkout and no stored credentials. Then `gh release create` with `dist/*`, `--generate-notes` and `--verify-tag`. |
| `pypi` | `pypi` | As today. It now needs `github`, not `build`. |
| `marketplace` | `marketplace` | As today (`marketplace.yml`). It skips while the Azure variables are unset. Its `tag` input is `check`'s output, because `github.ref_name` is now `main`. |
| `openvsx` | `openvsx` | Section 13. After `github`; skipped unless the repository variable `OPENVSX` is `true`. |
| `mcp-registry` | `mcp-registry` | As today (`mcp-registry.yml`), after `pypi`, with the same `tag` input. It checks out `refs/tags/<tag>`, which `github` created, runs `scripts/versions.py --stamp` (the committed `server.json` has no version) and checks that the version equals the tag. |
| `site` | `github-pages` (no reviewer) | As today (`pages.yml`, since #22), after `pypi`. |

Facts the design relies on:
- A tag pushed with the workflow's own `GITHUB_TOKEN` starts no other workflow. GitHub
  documents this: events made with `GITHUB_TOKEN` create no new runs, except
  `workflow_dispatch` and `repository_dispatch`. So the tag doesn't start Release twice.
- PyPI's trusted publisher matches the workflow file name `release.yml` and the
  environment `pypi`. Both names stay the same.
- A job that names an environment with a required reviewer waits on the run page until
  someone approves or rejects it, for up to 30 days.

## 5. GitHub settings (the user changes these; they are not in the repo)

Done on 2026-10-17, checked through the API:
- `pypi`: required reviewer `sandeepshelke`.
- "Prevent self-review" off in `pypi` and `mcp-registry`. The person who starts the run
  approves it.
- `pypi`, `mcp-registry` and `github-pages`: deployment branch `main` (the tag rule `v*`
  was removed later, see "Still to do" 2).
- The ruleset on `main` (section 1).
- `release`: required reviewer `sandeepshelke`, "Prevent self-review" off, deployment
  branch `main` only.

Still to do:
1. Done (`release` above).
2. Done: the tag rule `v*` is removed from `pypi`, `mcp-registry` and `github-pages`.
   A run started by hand on main runs on `refs/heads/main`, so the `main` rule covers
   it, and a tag no longer starts anything.
3. If the Azure variables are ever set, create `marketplace` first, with the same
   reviewer and branch rule. A job that names an environment that doesn't exist creates
   it with no gate.

Without setting 1 the `github` job would have created the `release` environment with
no gate, so the workflow change (step 3 of section 11) had to wait for it. It is in place
since 2026-10-17.

## 6. `scripts/release.py`

Usage: `python scripts/release.py --as LOGIN [--yes] [--run ID]`

- `--as LOGIN`: the GitHub account that starts the run and approves its gates. Every gh
  call goes through `forge.run(Account("github.com", "gh", LOGIN), …)`, so the global gh
  account is never switched. The script stops if `gh api user` doesn't name LOGIN.
- `--yes`: answer yes to every question. It still stops at the first failure.
- `--run ID`: pick up an existing Release run (after Ctrl-C, or one started on the page)
  instead of starting a new one. It skips steps 1 and 2, because the tag and the PyPI
  version may already exist by then. It still checks the login and the version, and
  stops unless the run is `Release` on `main` for the local HEAD. A failed run goes
  through step 2a.
- Without `--run`, the script finds the run itself (step 2), so after Ctrl-C, a failed
  job or a gate answered no, running the same command again picks the release up.

Steps:

1. **Preflight**, all before any question. It stops with the reason when:
   - the branch isn't `main`;
   - a tracked file has changes (untracked files are allowed);
   - after `git fetch`, HEAD isn't `origin/main`;
   - `src/gitpro/VERSION` is not X.Y.Z (the same check as the `check` job);
   - `gh api user` doesn't name LOGIN;
   - `gitpro why` exits non-zero;
   - the tag check on GitHub answers anything but 200 or 404.
2. **Start or pick up.** Look up the newest `workflow_dispatch` Release run on HEAD
   (`run list … --json databaseId,headSha,createdAt,status,conclusion`).
   - If it hasn't completed (queued, in progress, waiting at a gate), say so and ask
     `Follow it? [y/N]`: never a second run for the same commit. No starts nothing.
   - Else, if part of X.Y.Z exists (tag `vX.Y.Z` locally or on GitHub, or X.Y.Z on
     PyPI): with that run, pick it up (step 2a); with none, stop with the reason, as a new
     run would fail at "The tag is new".
   - Else ask `Start release vX.Y.Z from <short sha>? [y/N]`. An older run on HEAD that
     failed or was cancelled before anything was published doesn't stop a new start.
     On yes, run
   `workflow run release.yml --ref main`. gh 2.45 doesn't print the new run's ID, so find
   it with `run list --workflow release.yml --event workflow_dispatch --json
   databaseId,headSha,createdAt`: the newest run whose `headSha` is HEAD and whose ID
   wasn't listed before the start. Comparing IDs, not times, doesn't depend on the local
   clock. It looks 12 times, 5 s apart, then stops. Print its URL.
2a. **Re-run a failed run.** A picked-up run that ended in failure or was cancelled:
   print the failed jobs and ask `Re-run its failed jobs? [y/N]`. On yes,
   `run rerun ID --failed`, then wait (12 looks, 5 s apart) until the run's
   `run_attempt` (`GET repos/Yantrakar/gitpro/actions/runs/ID`; gh 2.45's `run view` has no
   attempt field) goes up, and follow it (step 3). GitHub asks for each re-run gate's
   approval again. Every job is safe to run again (section 8). No exits 0 and writes
   nothing. A run that succeeded goes to step 4 (skipped when the Marketplace lists
   X.Y.Z) and then 5.
3. **Follow the run.** Poll `run view ID --json status,conclusion,jobs` every 15 s.
   - Print a line, led by the time since following began (`  3m15s  ✓ build: success`),
     when a job first shows up or changes state (queued, in progress, waiting, success
     ✓, skipped –, failed ✗), and when a running job starts another step.
   - After 2 minutes without such a line, print which jobs are still running or queued,
     and the step each one is on.
   - When a job fails or is cancelled, print its name, the run URL, the
     `run view ID --log-failed` hint and that running the script again offers the
     re-run, and exit 1.
   - When `GET repos/Yantrakar/gitpro/actions/runs/ID/pending_deployments` lists an
     environment:
     - if `current_user_can_approve` is false, stop and say so;
     - otherwise print what that gate publishes (section 6.1) and ask
       `Approve <env>? [y/N]`;
     - on yes, `POST` the same path with `{"environment_ids": [id], "state": "approved",
       "comment": "scripts/release.py"}`;
     - on no, exit 0 and print the run URL. The run keeps waiting; finish it on the page,
       or later with `--run ID`.
   - An environment already approved in this run isn't asked about again.
4. **VSIX (manual)**, once `github` has succeeded and before the `mcp-registry` gate is
   approved:
   - download `gitpro-X.Y.Z.vsix` from the release into `~/Downloads/gitpro-X.Y.Z/`;
   - check it against the release's `SHA256SUMS`;
   - open the publisher page (`xdg-open`, `open` on macOS, or print the URL);
   - print "Upload it with ⋯ → Update";
   - poll the Marketplace's public extension query for `yantrakar.gitpro` every 30 s
     until X.Y.Z is listed, for up to 60 minutes, saying every 5 minutes how long it has
     waited and which versions the Marketplace lists. If it isn't listed by then, exit 1 with
     what is missing and the `--run ID` to resume. `--yes` doesn't skip this wait: no one
     else can do the upload.
   - If the Marketplace already lists X.Y.Z (a resumed run, or `marketplace.yml`
     published it), the step is skipped.
   - The `pypi` gate doesn't wait for this step; only `mcp-registry` does. A run picked up
     with `--run ID` at the `mcp-registry` gate does this step first.
5. **Verify** after the run succeeds, and print one line for each:
   - PyPI lists X.Y.Z, with a wheel and an sdist;
   - the GitHub release has the wheel, the sdist, the VSIX and `SHA256SUMS`;
   - the MCP Registry has `io.github.Yantrakar/gitpro` X.Y.Z;
   - the Marketplace lists X.Y.Z;
   - Open VSX lists X.Y.Z, when the run's `openvsx` job succeeded; when it was skipped
     (the variable `OPENVSX` unset) the line is
     `– Open VSX: the openvsx job didn't run; not checked` and doesn't count;
   - gitpro.dev names X.Y.Z (`"softwareVersion": "X.Y.Z"`), checked every 30 s for up
     to 10 minutes, since GitHub Pages sends `Cache-Control: max-age=600` and a cache
     may serve the old page until then. The first miss prints one line saying so.

   It exits 0 only when every counted check passes.

Everything the script prints goes through `forge.redact` and `forge.safe`, so a token
in a gh error never reaches the terminal. Ctrl-C exits 130; the run keeps going on
GitHub and `--run ID` picks it up.

### 6.1 Gate descriptions

| Environment | Printed before the question |
|---|---|
| `release` | `creates tag vX.Y.Z and the GitHub release (wheel, sdist, VSIX, SHA256SUMS)` |
| `pypi` | `publishes gitpro X.Y.Z to PyPI (a version can never be uploaded again)` |
| `marketplace` | `publishes the VSIX to the VS Code Marketplace` |
| `openvsx` | `publishes the VSIX to Open VSX` |
| `mcp-registry` | `publishes server.json X.Y.Z to the MCP Registry (it cannot be changed later)` |

An environment the table doesn't know gets `unknown gate`, and the script stops without
approving it. `github-pages` has no reviewer, so it never waits for a gate.

## 7. Marketplace

`marketplace.yml` and its job stay. They skip while `AZURE_CLIENT_ID` is unset. If the
variables are ever set, the `marketplace` gate shows up and the script approves it like
the others. Step 4 of section 6 then finds the version already listed and goes on.

## 8. Failures and recovery

- A failed job: fix it, then run the script again (step 2a re-runs the failed jobs of the
  same run), or "Re-run failed jobs" on the page and pick it up. GitHub asks for each
  gate's approval again.
- Rejecting a gate stops that run. If `github` never ran, nothing is published and a
  new run can start. Once the tag exists, a new run refuses (`check`, "The tag is new"),
  so re-run the jobs of the same run instead. That re-run works: the `github` job keeps
  a tag that already points at the run's commit (and fails if the tag is at another
  commit), uploads to a release that exists with `gh release upload --clobber` instead of
  creating it, and the PyPI step has `skip-existing: true`.
- The refusal is real: run 39018352548, started when the tag already existed, failed at
  "The tag is new" in `check` with every other job skipped.
- `--run ID` also needs the run's commit to be the local HEAD, and the script approves
  a gate that turns pending while it waits for the VSIX.
- `mcp-registry.yml` and `marketplace.yml` keep their own `workflow_dispatch`, to publish
  an existing tag by hand.

## 9. Docs

- `docs/guide.md`, the developing paragraph: "CI runs it" stays true; CI runs on pull
  requests and in Release.
- `docs/guide.md`, the release paragraph (starting "To release, bump the version"):
  put the new version and its comment on a new first line of `src/gitpro/VERSION` (the old first line becomes line 2; the first word of line 1 is the version; the only place; the workflows stamp the rest), merge, then start Release with `python scripts/release.py --as LOGIN` or with
  "Run workflow" on the Actions page. List the four gates and the manual VSIX step.
- The header comments of `release.yml` and `mcp-registry.yml`.

## 10. Tests (`tests/test_release_script.py`)

The script's gh, git, HTTP and browser calls go through small functions that the tests
replace. No test touches the network.

- Preflight: each condition in section 6 step 1 stops the script before any question,
  and before any gh call that writes.
- The run is found by `headSha` and creation time. An older run on the same SHA is not
  picked.
- A pending gate with `current_user_can_approve: false` stops without a POST.
- `n` at a gate exits 0 with no POST. `y` posts exactly
  `{"environment_ids": [id], "state": "approved", "comment": "scripts/release.py"}`.
- `--yes` approves every known gate. An unknown environment is never approved.
- A failed job exits 1, and no gate after it is approved.
- The `mcp-registry` gate isn't approved until the Marketplace lists the version.
- A VSIX whose hash doesn't match `SHA256SUMS` stops the script.
- Nothing the script prints contains the value the fake `gh auth token` returns.
- `--run ID` skips the start and follows that run.
- A run on HEAD that hasn't completed is offered (`Follow it?`), never started again; no
  writes nothing.
- With the tag or PyPI published and a failed run on HEAD, the script re-runs its failed
  jobs after `y` (none after `n`) and follows the new attempt; with no run on HEAD it
  stops before any question.
- A succeeded run whose VSIX the Marketplace doesn't list gets step 4 before step 5.
- The `openvsx` gate is approved and named. Verify skips Open VSX when the `openvsx`
  job was skipped, and when the job succeeded passes if Open VSX lists the version and
  fails if it doesn't.
- Following prints each job and step change once, with the time, a reminder after 2
  minutes without one, and a failed job's name; the Marketplace wait reminds every 5
  minutes; the site check says once that it waits.

## 11. Steps

1. `ci.yml` (section 3): add `workflow_dispatch` and `workflow_call` and the group's
   `github.run_id` fallback. Merge it on its own PR; that PR's CI is the test.
   Merged 2026-10-17 (#24).
2. The tests in section 10, failing, then `scripts/release.py` until they pass.
   Done 2026-10-17. The tests were written after the script, not before; each check
   was then removed from the script in turn to see a test fail.
3. `release.yml` (section 4), docs (section 9), full suite. This merges only after
   setting 1 in section 5 is in place (it is). Done 2026-10-17, in the same PR as step 2.
4. Settings 2 and 3 in section 5.

The ruleset that step 4 named on 2026-10-15 is in place (section 1).

## 12. Open questions

None. The decisions are the user's, from 2026-10-15:
- CI runs before a PR is accepted, not on each push (done in #22, 2026-10-17: "we should
  be careful and avoid wasteful steps");
- each stage is clicked one by one on the run page, or run by the local script one yes
  at a time, and unattended once trusted;
- the VSIX upload stays manual.

And from 2026-10-17: the site goes live only from a release, after PyPI has it (#22).

## 13. Open VSX (added 2026-10-18)

The `openvsx` job in `release.yml` publishes the VSIX from artifact `dist` with
`npx --yes ovsx@1.2.0 publish --packagePath dist/*.vsix --trusted-publishing --skip-duplicate`.
It has `id-token: write` and nothing else, and no token is stored. `ovsx` 1.1.0 added
trusted publishing; 1.2.0 (2026-09-10) retries the token exchange on a 502, 503 or 504.
`--skip-duplicate` makes a re-run pass when the version is already there.

How Open VSX matches the run, from its source
(`GitHubTrustedPublishingProviderSupport.java`, read 2026-10-18): the repository and
owner ids, the `workflow_ref` claim without its `@ref`, and the environment if the
registration names one. `workflow_ref` is the run's top workflow, so the registration
names `release.yml`; `job_workflow_ref` (a called workflow) is only recorded. Open VSX
doesn't check the branch, which is why the job has its own environment.

Before the job can run (the Open VSX wiki, "Trusted Publishing" and "Namespace Access"):
1. The namespace `yantrakar` exists (done 2026-10-18; not yet verified). Its case must
   match `publisher` in `vscode/package.json`.
2. The first version is uploaded with an access token, because a registration for an
   extension with no active version is refused. 0.4.1 was uploaded on 2026-10-18 and is
   active; Open VSX activates a version only after its scan.
3. The user owns the namespace: creating it makes you a contributor; ownership is claimed
   in a public issue (EclipseFdn/open-vsx.org#14393, opened 2026-10-18).
4. Settings → Trusted Publishers, GitHub Actions: owner `Yantrakar`, repository `gitpro`,
   workflow `release.yml`, environment `openvsx`. The access token is then deleted.
5. The `openvsx` environment: required reviewer `sandeepshelke`, deployment branch
   `main`, like `pypi`.
6. The repository variable `OPENVSX` set to `true`.
