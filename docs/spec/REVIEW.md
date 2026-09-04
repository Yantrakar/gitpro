# Independent review of the gitpro spec (`docs/spec/`)

Reviewer: independent, adversarial review. Date: 2026-10-02.
Scope: `README.md`, `00-existing.md`, work packages `01`–`17`, checked
against the code at HEAD (`src/gitpro`, `tests/`, `vscode/`,
`skills/gitpro`, `.claude-plugin/`).

## How this review was done

- I read every spec file and every module in `src/gitpro`, the tests, the
  VS Code extension sources, the skill and the plugin manifests.
- `00-existing.md` was checked against the code line by line. It is
  accurate: the commands, flags, exit codes, file layout and known issues
  match HEAD. The few small gaps are listed under Minor.
- The existing suite passes (`.venv/bin/pytest -q`: 55 passed).
- I tested git behaviour the spec relies on with git 2.43.0, a temporary
  `HOME` under the session scratchpad, `GIT_CONFIG_NOSYSTEM=1`, and no
  network. The results are listed in the appendix and are referred to as
  **[verified]**. Claims about other tools (Claude Code, MCP, gh, glab,
  husky, git-lfs, devcontainers) that I could not test offline are marked
  **[believed]** or **[uncertain]**. They need checking before anyone
  builds on them.
- Not done: running gitpro against the real home directory, reading
  `~/.ssh` or `~/.gitconfig`, any network access, editing any file other
  than this one.

## Summary

| severity | count |
|---|---|
| Blocker | 8 |
| Major | 34 |
| Minor | 27 |
| Question for the product owner | 12 |

The core idea is sound: `resolve` as the single judge of "is this repo
right", the `--json` contract, and undo for every write. The spec also
works hard on privacy. The weak spots are where gitpro starts running code
inside other tools' processes:

- **06 (git hooks).** The script as written hangs every commit
  ([verified]). It turns gitpro crashes into blocked commits. It switches
  off every repo hook that isn't chained, git-lfs's included. It blocks
  pushes that contain teammates' commits.
- **10 (agent hook).** Warn mode tells Claude Code to *approve* the
  command, which skips the user's permission prompt.
- **01 (`resolve`).** It does not match URLs the way git does
  ([verified]), so every guard built on it reaches the wrong verdict on
  some remotes.

All three are cheap to fix in the spec now and expensive to fix after ten
agents have built against them.

---

## Blockers

### R-001 — Chain script runs itself forever; every commit hangs [verified]

- **Where:** `06-guard.md` §2 "Installed files", the chain script.
- **Problem:** The script finds the repo's own hook with
  `repo_hook=$(git rev-parse --git-path hooks)/pre-commit`. When
  `core.hooksPath` is set, `--git-path hooks` returns the `core.hooksPath`
  directory. Global mode sets `core.hooksPath` to the gitpro hooks dir, so
  `repo_hook` is the gitpro script itself, and `exec "$repo_hook"`
  re-runs it with no end.
- **Failure:** I reproduced it with the script's logic and a global
  `core.hooksPath`. `git commit` never returned: 1,541 runs in 3 s before
  `timeout` killed it. Acceptance tests 4 and 8 would hang too.
- **Fix:**
  - Use `"$(git rev-parse --path-format=absolute --git-common-dir)/hooks/<name>"`
    (git ≥ 2.31, within the 2.36 minimum). That is `.git/hooks` of the
    main repo, also from linked worktrees.
  - Refuse to chain to a path whose physical directory
    (`cd -P … && pwd`) is the script's own directory.
  - Apply the same check to `chain_to` (R-013).
  - Add a test-wide timeout (for example `timeout=20` in the `git()`
    helper) so a regression fails instead of hanging CI.

### R-002 — Hooks do not fail open: any gitpro failure blocks the commit

- **Where:** `06-guard.md` §2, `gitpro guard check pre-commit "$@" || exit $?`,
  and the repo-mode block. README §2.8 and §8.11 promise fail-open.
- **Problem:** The script blocks on *any* nonzero exit. These all block
  the commit:

  | cause | exit |
  |---|---|
  | uncaught Python exception | 1 (same as a confirmed mismatch) |
  | broken pipx venv after a Python upgrade | 126 or 127 |
  | an older gitpro on PATH with no `guard` subcommand (argparse) | 2 |
  | killed by the hook timeout or a signal | 128+n |

  The "fail open" handling in §2 covers only errors that `guard check`
  catches itself.
- **Failure:** The user upgrades Homebrew Python. pipx's gitpro shim now
  fails with "bad interpreter" (126), and every commit in every repo
  fails with a message that does not mention gitpro.
- **Fix:**
  - Give "block" its own exit code (for example `3`, defined in
    `guard.py` and documented). The script blocks only on that code and
    turns any other nonzero code into
    `⚠ gitpro guard: exited <rc>; not blocking`.
  - `guard check` wraps everything in `try/except BaseException` (except
    `KeyboardInterrupt`) and exits 0 with a warning.
  - The repo-mode line in R-004 follows the same rule.
  - Tests: a fake `gitpro` that exits 1, 2 and 127 must all let the
    commit through.

### R-003 — A global `core.hooksPath` turns off every other repo hook, git-lfs's included

- **Where:** `06-guard.md` §2 (global mode), §9 ("`post-checkout` hooks"
  out of scope).
- **Problem:** With `core.hooksPath` set, git looks for *every* hook
  there. gitpro chains only `pre-commit`, `pre-push` and `commit-msg`.
  Every other hook a repo or tool installed in `.git/hooks` silently stops
  running:
  - `prepare-commit-msg` (ticket-id inserters, commitizen)
  - `pre-merge-commit`, `post-checkout`, `post-merge`, `post-commit`
  - `post-rewrite`, `pre-rebase`, `reference-transaction`, `pre-auto-gc`
- **Failure:**
  - **git-lfs** [believed]: its `pre-push` (installed per repo by
    `git lfs install`) is the step that uploads LFS objects. gitpro's
    pre-push chains `.git/hooks/pre-push`, so repos that already have it
    keep working. In a *new* clone, `git lfs install` writes to
    `git rev-parse --git-path hooks`, which is now the gitpro dir. It
    finds gitpro's `pre-push` there and refuses ("Hook already exists"),
    so the clone never gets LFS hooks. Pushes then upload commits that
    hold LFS pointers without the objects, and teammates' checkouts fail
    to smudge. That is data loss on the remote.
  - **lefthook** [believed] respects `core.hooksPath` and would write its
    hooks into, or over, the gitpro dir.
  - **pre-commit framework** [believed, confirm]: `pre-commit install`
    refuses to run when `core.hooksPath` is set ("Cowardly refusing to
    install hooks with `core.hooksPath` set").
  - The spec says husky, lefthook and pre-commit "set core.hooksPath" in
    the repo. As far as I know, only husky does. The other two are hurt
    by the *global* setting, not bypassed by a local one.
- **Fix (pick one and spell it out):**
  1. Generate a pass-through stub for every client hook name in
     githooks(5): it runs the repo hook if present, else exits 0. Keep
     the gitpro check only in `pre-commit`, `commit-msg` and `pre-push`.
     For repos that use LFS (`git config --get filter.lfs.process`), the
     stubs for `pre-push`, `post-checkout`, `post-commit` and `post-merge`
     run `git lfs <hook> "$@"` as LFS's own scripts do. Document that
     `lefthook install` and `pre-commit install` need
     `gitpro guard uninstall` first, or run with `core.hooksPath` unset
     for that one command.
  2. Or make repo mode (per-repo `.git/hooks` with chaining) the default
     and keep global mode opt-in, with these interactions printed at
     install.

  Either way:
  - `guard status` lists repos whose `.git/hooks` holds hook types that
    gitpro does not chain.
  - Tests cover one non-chained hook (`post-checkout`) still running.

### R-004 — Repo mode edits tracked or regenerated husky files

- **Where:** `06-guard.md` §2 "Repo mode", §7, test 13.
- **Problem:** "`<hooks>/pre-commit` gets a managed block" with
  `<hooks>` = the repo's `core.hooksPath`:
  - husky v9 sets `core.hooksPath=.husky/_`. That dir is generated and
    gitignored, and `npm install` (the `prepare` script) rewrites it, so
    the block silently disappears.
  - If the implementer writes `.husky/pre-commit` instead (as test 13
    says), that is a tracked team file. The block gets committed and
    pushed. The backup `.husky/pre-commit.bak.<ts>` lands in the work
    tree too.
  - The line `[ -n "$GITPRO_SKIP" ] || gitpro guard check … || exit $?`
    exits 127 on every teammate who doesn't have gitpro, so all their
    commits are blocked. This conflicts with README §8.8 ("touch only the
    named repos").
- **Fix:**
  - Refuse paths tracked by git (`git ls-files --error-unmatch <file>`).
  - Refuse husky's `_` directory.
  - Keep backups in the state dir, not next to the file.
  - Make the inserted line fail open:
    `command -v gitpro >/dev/null 2>&1 && { gitpro guard check pre-commit "$@"; [ $? -eq 3 ] && exit 1; }`
    (R-002).
  - For husky, consider the per-user startup file
    (`~/.config/husky/init.sh`, husky v9 [uncertain]) instead of repo
    files, or only print instructions.
  - Change test 13 to assert the refusal on a tracked file.

### R-005 — The guard blocks legitimate commits and pushes authored by other people [partly verified]

- **Where:** `06-guard.md` §2 "`guard check` behaviour" (pre-commit and
  pre-push). The same logic is reused by `10` §2 step 5, `11`
  `check_commit` and `14` guard integration.
- **Problem:** The guard compares the commit's **author** email with the
  expected email. The author is meant to differ in normal work:
  - **pre-push, update case:** the range is
    `<remote_sha>..<local_sha>`, limited by `--not --remotes` only when
    the remote sha is all zeros. After `git merge origin/main` on a
    feature branch, the range holds every teammate commit from main. The
    push is blocked, listing their emails. That is the default
    `on_mismatch = "block"`, in every team repo.
  - **cherry-pick, `git am`, applying a colleague's patch:** these keep
    the original author. The pre-push check flags them.
  - **`git commit --amend` on a colleague's commit, or `--author=`
    (pairing, committing for someone)** [verified]: in git 2.43 the
    pre-commit hook already sees the final author. `git var
    GIT_AUTHOR_IDENT` and `$GIT_AUTHOR_EMAIL` both show the `--author`
    value, and for `--amend` the original author. So pre-commit blocks
    these too.
- **Failure:** Users meet false blocks in their first week and turn the
  guard off, which defeats the product's main guardrail.
- **Fix:**
  - Judge "am I using the wrong identity" by the **committer** ident
    (`git var GIT_COMMITTER_IDENT`; for pre-push, `%ce`). The committer
    is always the local user.
  - Flag the *author* only when it is one of the user's **own**
    configured emails (any identity or the default) other than the
    expected one. A teammate's address is not the user's mistake.
  - In pre-push, always add `--not --remotes` (both cases), and for the
    update case also exclude commits reachable from the remote sha.
  - Spell out these cases in tests: merge from main, cherry-pick of a
    teammate's commit, `--author` on behalf of a pair, and amend of a
    teammate's commit. None may block.
  - Update 10, 11 and 14, which copy "the mismatch as in 06".

### R-006 — Agent hook warn mode auto-approves the command (skips Claude Code permission prompts)

- **Where:** `10-agent-hooks.md` §2 ("Warn mode:
  `permissionDecision: "allow"` with `additionalContext`").
- **Problem:** For Claude Code `PreToolUse`, `permissionDecision: "allow"`
  means "approve this tool call and skip the permission system"
  [believed, high confidence; it is the documented meaning]. In warn mode,
  any `git push`, `gh api -X DELETE …` or `gh repo create` that gitpro
  judges identity-relevant *and* mismatched would run with no prompt,
  even if the user's settings say to ask. An identity checker must never
  widen what the agent may do.
- **Failure:** A user sets `[agents] hook = "warn"` to cut noise. Claude
  runs `git push --force` into a repo with an override. gitpro's warning
  becomes an approval, and the user's "ask before push" rule is skipped.
- **Fix:**
  - gitpro never outputs `"allow"`.
  - Warn mode either prints only to stderr with exit 0 (no JSON), or uses
    `permissionDecision: "ask"` with the reason, so the user decides.
  - Whether `additionalContext` works for `PreToolUse` is [uncertain]
    (it is documented for other events). Verify it before relying on it.
  - The allow path already prints nothing. Keep that.
  - Add a test: no adapter output in any mode contains `"allow"` (or the
    Cursor equivalent `"permission": "allow"`).

### R-007 — `match_url` does not match URLs the way git does, so every guard reaches the wrong verdict on some remotes [verified]

- **Where:** `01-config-resolve-why.md` §4 rules for `parse_url`,
  `match_url`, `expected`, `effective`.
- **Problem:** The spec says `match_url` "mirrors git's rule exactly", but
  it matches on a parsed `(host, owner)` with `fnmatch`. git matches the
  **raw configured URL** against the `includeIf` patterns that
  `render.url_patterns` generated, using wildmatch, case-sensitive. These
  differ:

  | case | git [verified] | spec |
  |---|---|---|
  | owners `["grp"]`, remote `git@gitlab.com:grp/sub/app` | matches (`grp/**`) | no match: owner `grp/sub` ≠ `grp` |
  | owner glob `corp-*` vs `corp-a/sub` | matches | depends on fnmatch, where `*` crosses `/` |
  | `GRP` vs `grp` | no match | depends on implementation |
  | `https://jane@github.com/o/r` | **no match** (patterns have no userinfo) | matches |
  | `gitlab.com:grp/app` (scp form without `user@`) | **no match** | matches |
  | different `ssh_user` (`me@host:o/r`) | no match | matches |
  | `ssh://…:2222/…` when the identity has no `ssh_port` | no match | matches |

  The rules also say remotes come from `git remote -v`. That prints URLs
  **after** `url.insteadOf` rewriting [verified]: an https remote shows as
  `git@github-work:…`. But `hasconfig:remote.*.url` matches the raw
  `remote.<name>.url` value [verified]. Only `url` counts, not `pushurl`.
- **Failure:**
  - Nested GitLab group: git applies the work identity, while `resolve`
    expects `[default]`. The guard then blocks every correct commit with
    a "mismatch".
  - `https://jane@…` remote: `resolve` expects the work identity, but git
    applies the default. `why` says "run gitpro apply", which changes
    nothing, so the user is stuck.
- **Fix:**
  - `match_url` evaluates `render.url_patterns(identity)` against each raw
    URL with a small wildmatch (`*` does not cross `/`; `/**` and `**/`
    as git defines them; case-sensitive). It then matches git by
    construction. Keep `parse_url` for display and `clone`.
  - Read remotes with `git config --get-regexp '^remote\..*\.url$'`, not
    `git remote -v`. Show raw URLs in `which --json` and `why --json`.
  - Add a problem code for "remote matched no pattern because of its form"
    (userinfo, missing user, port), with a fix (`git remote set-url …`).
  - Add a **differential test**: for a table of URLs, write the generated
    includeIf config into a temp `HOME`, ask real `git config user.email`,
    and assert `resolve.expected` agrees. This one test protects 06, 08,
    10, 11, 12, 14, 15 and 16.

### R-008 — The CLI registration and dispatch pattern is undefined, though every package copies it

- **Where:** `README.md` §4.2 and §9 ("one `register(...)` call … plus
  one `COMMANDS` entry"); `01-config-resolve-why.md` §7.
- **Problem:** Two dispatch mechanisms are mixed:
  - 01's `register()` uses `p.set_defaults(func=cmd_why)`.
  - `main()` dispatches through `COMMANDS[args.command]` (cli.py:328).
  - The README says `COMMANDS` is "used by the no-argument status output",
    but `cmd_status` walks `subs.values()` (the subparser choices) and
    never reads `COMMANDS`.

  If 01's implementer does not switch `main()` to `args.func`, every
  package that only sets `func` fails with `KeyError`. If they do, the
  `COMMANDS` entries the README demands are dead code. Either way ~15
  packages each edit the same `COMMANDS` dict literal and the same
  alphabetical block. Some packages also extend *existing* parsers (09
  `extend_keys_parser`, 14 `extend_check_parser`, 04 adds `init
  --discover`, 02 changes `scan`). That is a second, undocumented pattern.
- **Failure:** Parallel agents build two different conventions, and
  merges conflict or fail at runtime. This is the one interface every
  package touches.
- **Fix (in 01):**
  - `main()` dispatches `args.func(args)`. Remove `COMMANDS`, or derive
    it from the parser.
  - `build_parser()` iterates a module list
    (`EXTENSIONS = ["audit", "agents", …]`) and calls `register` in each
    importable module, wrapped in `try: import`. Each package then adds
    one string to one list, or nothing at all with entry points.
  - Document `extend_<cmd>_parser(p)` as the official way to add flags to
    existing commands. Name the parsers it may touch (`keys`, `check`,
    `init`, `scan`) and say who owns each flag.
  - `register` must not import heavy modules (R-009).

---

## Major

### R-009 — Hot-path commands cannot meet their latency budgets through the `gitpro` entry point [measured]

- **Where:** README §2.8 (< 150 ms); `06` §2 ("imports only resolve,
  config, guard"); `10` §7 (< 150 ms allow path); `16` §2 prompt
  (< 80 ms).
- **Problem:** Measured at HEAD, with only today's 10 modules:
  - `gitpro --version` takes 80–90 ms.
  - `import gitpro.cli` takes ~68 ms, mostly `rich`.

  The hooks run `gitpro guard check …`, which goes through `cli.main` →
  `build_parser` → every package's `register`. The spec gives no way for
  `guard check` to avoid importing `cli`, `view` and `rich`. Adding
  17 modules and 2–3 git calls (01's "two git calls" is really about
  three, see R-012) puts `guard check` near or over 150 ms. The 80 ms
  prompt budget is gone before any work starts. `agent-check` runs on
  **every** Bash tool call the agent makes.
- **Fix:**
  - Put a fast path in `gitpro/__main__.py` and the console-script
    target: if `argv[1]` is in `{"guard", "agent-check", "prompt"}`,
    import only the small module (`gitpro.hot`) and never `cli` or
    `rich`. Rule: `config`, `resolve`, `guard`, `agents` and `prompt`
    must not import `rich` or `view` at module level.
  - Add a regression test with `python -X importtime` (no `rich` in the
    import list of `gitpro guard check`), not a wall-clock assertion.

### R-010 — Frozen dataclasses and nested tables are underspecified in `config.load`

- **Where:** `01` §3.
- **Problem:**
  - `Default` and `Identity` become `frozen=True`, but `config.load()`
    today mutates them (`i.file = i.file or f"~/.gitconfig-{i.id}"`),
    which raises `FrozenInstanceError`.
  - `_build` uses `cls(**table)`. Nothing converts `[identity.trailers]`
    to `Trailers`, TOML arrays to the declared `tuple[str, ...]`, or
    `[scan]` and the other tables to their dataclasses, so
    `Identity.trailers` would be a `dict`.
  - `Config.how` ("user config", "env", "-c") cannot be computed by
    `load_all(path)`, which only gets a path.
  - `Default.git` stays a `dict` inside a frozen class, so it is mutable
    and unhashable.
- **Fix:**
  - Specify `dataclasses.replace` for the `file` default.
  - Add a `_build_nested(cls, table, where)` that validates and converts
    lists to tuples.
  - Change the signature to `load_all(path, how)`, or return `how` from
    `config.find`.
  - Freeze `git` as a mapping copy, or leave `Default`/`Identity`
    unfrozen and say so.

### R-011 — `effective()` origins and scopes are wrong for real git output [verified]

- **Where:** `01` §4 (`Setting.origin`, scope derivation); README §6
  ("Paths are absolute").
- **Problem:**
  - `git config --show-origin` prints `file:.git/config` (relative) in
    the main worktree, and absolute paths in linked worktrees and for
    includes [verified]. The pin shows as `file:.git/git-identities`
    [verified]. The spec's example is absolute, and the README says paths
    are absolute.
  - The path heuristics miss these:
    - `~/.config/git/config` (XDG global)
    - `$GIT_CONFIG_GLOBAL` pointing elsewhere
    - `.git/config.worktree` with `extensions.worktreeConfig` [verified]
    - `GIT_CONFIG_COUNT`/`-c` (`command line:`)
    - included files that are neither generated nor in `.git`
  - Identity files carry the `# Generated by gitpro` marker on line 1 —
    fine — but `include.path` files written by the user aren't covered.
- **Fix:**
  - Use `--show-scope --show-origin` (git ≥ 2.26). git then reports
    `local`, `global`, `system`, `worktree` and `command`. Refine only
    `local` + `git-identities` → `pin` and global + generated → `generated`.
  - Normalise origins to absolute paths in `to_json`, relative to
    `git rev-parse --git-dir`.
  - Test linked worktrees and `config.worktree`.

### R-012 — Several `compare` problem rules are ambiguous

- **Where:** `01` §4 problems table and "two git calls".
- **Problems:**
  1. `sign_mismatch` compares `commit.gpgsign` (often unset → `null`) with
     `sign: bool`. Say that null means false.
  2. The glossary's "mismatch" includes the signing key, but no rule
     checks `user.signingkey` against the expected `ssh_key.pub`.
  3. `key_mismatch` when the identity has no `ssh_key` (none expected,
     but `core.sshCommand` set by a global file) is undefined.
  4. `compare` needs `config --show-origin`, the remote URLs (R-007), and
     `rev-parse` for `is_repo` and the pin path: three or more calls, not
     two.
  5. git calls pre-push with `$1 = remote name` and `$2 = URL`, but `$1`
     can be a URL when the user pushes to a URL directly. `remote=` must
     accept a URL.
  6. Inside hooks git exports `GIT_DIR`, `GIT_INDEX_FILE`, and in
     worktrees `GIT_WORK_TREE`. `resolve` calls with `git -C <other repo>`
     (10's `cd X &&`, 11's tools) inherit them and query the wrong repo.
     `resolve` should drop `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
     `GIT_PREFIX` and `GIT_COMMON_DIR` when it targets an explicit path.
- **Fix:** Add these rules to §4 and one test each.

### R-013 — `chain_to` and `__GITPRO_BIN__` are pasted into a shell script without validation

- **Where:** `01` §3 validation list (no `chain_to` rule); `06` §2
  (`__CHAIN_TO__`, `__GITPRO_BIN__` substitution).
- **Problem:**
  - `chain_to` is free text substituted into
    `chain="${GITPRO_CHAIN_TO:-__CHAIN_TO__}"`. A value containing `"`,
    `$(` or a backtick becomes code. The config is user-owned, so this is
    a correctness issue more than privilege escalation, but README §8.3
    forbids it.
  - `__GITPRO_BIN__` (pipx path) can contain spaces.
  - `chain_to` equal to the gitpro hooks dir loops (R-001).
- **Fix:**
  - Validate `chain_to` with `PATH_RE` and reject the gitpro hooks dir.
  - Substitute with `shlex.quote`, or write the values to a sidecar file
    that the script reads.
  - Test a path with a space and a quote.

### R-014 — `undo.write` contradicts itself and changes `scan --fix`'s undo script

- **Where:** `01` §6.
- **Problem:** "File name and content stay byte-identical to today" and
  "`set -e` is new" cannot both hold. Also:
  - Today's script (`scan.fix`) is written with `write_text`, mode 0600
    under umask 077, and has a header comment
    (`# puts back what 'gitpro scan --fix' removed on <date>`). The spec
    says 0700 and does not mention the header.
  - `set -e` changes behaviour: today a failing line (repo deleted since)
    does not stop the rest of the restore. With `set -e` a partial restore
    stops at the first missing repo.
  - The name collision handling (`-n` suffix) is a racy `while exists()`
    loop.
- **Fix:**
  - Decide: keep the header, mode 0600 (the user runs `sh <path>`, so
    exec permission is not needed), and *no* `set -e` for multi-repo
    restores, or `set -e` with each line written as `cmd || echo "⚠ …"`.
  - Create the file with `os.open(O_CREAT|O_EXCL, 0o600)`.
  - Update the "byte-identical" sentence.

### R-015 — `scan` gains a config dependency and regresses straight after `gitpro init`

- **Where:** `02` §2 and §7.
- **Problem:**
  - `scan` today runs without a config (it uses `Path.home()`). 02 makes
    it read `[scan] roots`. `gitpro init` writes a template whose emails
    are placeholders (`*.example`), which `config.load` rejects
    (`PLACEHOLDER_RE`). So `gitpro scan`, which is exactly what a new user
    runs next, and the VS Code scan view would fail with exit 2.
  - Using `cli._load` would also add the git ≥ 2.36 check to a read-only
    scan.
- **Fix:**
  - `scan` reads only the `[scan]` table through a lenient loader
    (`config.load_scan(path)`). It falls back to `["~"]` with a `⚠` when
    the config is missing or invalid.
  - Add a test: run `init`, then `scan`, and expect exit 0.

### R-016 — No shared "effective roots" function; 02, 03, 04 and 11 all edit or need `scan.find`

- **Where:** `02` §11, `03` §11 (`lost.collect(roots, exclude, …)`), `04`
  (depth ≤ 3 walk), `11` (`scan`/`lost` tools take `roots`).
- **Problem:**
  - 02 changes `scan.find`'s signature (exclude). 04 wants a depth limit
    in the same function. Both are wave 1, so they conflict.
  - No package owns the public function "roots from flags, then
    `[scan]`, then default, with excludes and tilde expansion". 03, 11 and
    16 each re-implement it.
- **Fix:**
  - 02 owns `scan.effective_roots(args_roots, cfg) -> tuple[list[Path], list[str]]`
    and `scan.find(roots, *, exclude=(), max_depth=None)` with both
    parameters from day one.
  - 04 uses `max_depth`.
  - Document it in 02 §11 as an API for others.

### R-017 — VS Code "Save to settings" splits the source of truth for scan roots

- **Where:** `02` §5.
- **Problem:** Copying TOML roots into `gitpro.scanDirs` makes the VS Code
  setting win from then on. The user later edits `[scan] roots` in the
  TOML and the extension ignores it. That contradicts 02's own user story
  ("set roots once").
- **Fix:**
  - The extension passes no roots unless `gitpro.scanDirs` is non-empty.
  - The picker offers "Use for this session" or "Show TOML snippet to
    paste" (principle 2.1: suggest, user picks), not a write to settings.

### R-018 — `lost` reports false positives and has an impossible detail

- **Where:** `03` §2 kinds table, §9, test 9.
- **Problems:**
  1. A branch whose upstream is `[gone]` (deleted after a squash merge)
     still has commits not in any remote-tracking ref (squash changes the
     sha), so it counts as `unpushed` forever. "✓ nothing lost" is never
     reached for anyone who uses squash-merge PRs. Report these as
     `merged_gone` (info) or check `git cherry` / patch-id against the
     default branch.
  2. A `no_remote` repo's branches also match the `unpushed` rule
     (nothing is in `--remotes`), so the repo is listed twice.
  3. `duplicate … diverged: has 2 commits not in …` cannot be computed
     between two separate clones without shared objects. Specify how
     (read-only `GIT_ALTERNATE_OBJECT_DIRECTORIES=<other>/objects git
     rev-list A --not B`, or compare reflogs and shas only and say "differ").
  4. Linked worktrees share one repo. §9 says worktrees are out of scope,
     but they would show as duplicates and their branches and stashes
     would be listed N times. Dedupe by `rev-parse --git-common-dir`.
  5. All results are only as fresh as the last fetch. Say so on the
     summary line ("as of last fetch").
  6. Scanning `~` with 6 git calls per repo across hundreds of repos needs
     a thread pool (I/O bound). Specify `concurrent.futures`, a bounded
     pool, and stable output order.
  7. Test 9's `xfail` follow-up names no owner package.
- **Fix:** As above, with a test each for 1, 2 and 4.

### R-019 — `repo_exists` treats any `gh` failure as "repo gone"

- **Where:** `05` §7 (`repo_exists -> "ok"|"404"|"renamed"`), `03`
  `remote_gone`.
- **Problem:**
  - `gh repo view` exits 1 for a network error, an auth error, a rate
    limit or SSO enforcement, as well as for a missing repo.
  - GitHub also returns 404 for a private repo that the queried account
    cannot see. That is exactly the multi-account case gitpro exists for.
  - An owner that differs only in case gets reported as "renamed".
- **Failure:** `lost --online` tells the user their employer's private
  repo is "gone", and they delete the only local clone with unpushed
  work.
- **Fix:**
  - Return `"unknown"` with the gh stderr line for anything other than a
    clear "Could not resolve to a Repository".
  - Label the result "not found or no access for account X".
  - Compare owner and repo case-insensitively.
  - Never print a destructive suggestion.

### R-020 — `discover` can leak credentials from shell history

- **Where:** `04` §2 and §8.
- **Problems:**
  - Stripping only `://user:pass@` misses tokens in the username field:
    `https://ghp_xxx@github.com/…`, `https://oauth2:glpat-xxx@gitlab…`,
    `https://x-access-token:…@`.
  - The denylist lacks `github_pat_`, `gho_`, `ghu_`, `ghs_`, `ghr_`,
    `glptt-` and `gldt-`.
  - `-v` prints raw history lines, which can hold
    `git -c http.extraheader="Authorization: …"` or `GH_TOKEN=… gh …`.
  - zsh history uses "metafied" bytes. Read it with
    `errors="replace"` and unmetafy.
  - Copying VS Code's `state.vscdb` copies other extensions' secrets
    cache. Open it read-only in place with
    `file:…?mode=ro&immutable=1`.
  - `vscode-remote://` URIs should be skipped.
- **Fix:**
  - Rebuild each URL from parsed parts with **all** userinfo and query
    strings dropped, and print only that URL plus the directory, never
    the source line.
  - Add a test with each token shape above.

### R-021 — 04's tests and dependencies are not buildable as written

- **Where:** `04` §10 tests 2 and 11, README §9 wave table.
- **Problems:**
  - Test 11 loads `example.toml` with `config.load`. It fails because
    `PLACEHOLDER_RE` rejects the `*.example` addresses.
  - Test 2 still contains an open "decide:".
  - 04 is wave 1, but `--online` uses 05's `forge.py` (wave 2).
  - 04 says "gh auth token is never called", while 05's `forge.run`
    calls it, and 04 imports it.
  - The id derivation rules live inline in `scan.suggest`. Reusing them
    means extracting a function from `scan.py`, which 02 also edits.
  - `gh auth status --json hosts` exists only in recent gh versions
    [uncertain]. Need a fallback that parses text.
- **Fix:**
  - Test 11: load with placeholders allowed (a `strict=False` parameter
    in 01), or check structure only.
  - Decide test 2.
  - Move `--online` to wave 2 or make it `xfail` without 05.
  - Change the sentence to "gitpro never prints or stores a token".
  - 02 extracts `scan.derive_id(...)`.

### R-022 — Multi-account `gh` handling picks the wrong account in common setups

- **Where:** `05` §7 `run`, `logged_in`; `09` and `12` reuse it.
- **Problems:**
  - For GitHub Enterprise Server hosts, gh reads `GH_ENTERPRISE_TOKEN`,
    not `GH_TOKEN` [believed]. Setting `GH_TOKEN` with
    `GH_HOST=ghe.corp` is ignored, and the query runs as gh's active
    account. The wrong account answers silently.
  - An inherited `GH_TOKEN`/`GITHUB_TOKEN` (CI, `.envrc`, a previous
    `eval "$(gitpro env)"`) wins over the stored login. Whether
    `gh auth token -u X` then returns the stored token or the env token is
    [uncertain]. Strip `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`
    and `GITHUB_ENTERPRISE_TOKEN` from the env before calling `gh auth
    token`, and set exactly one of them for the child.
  - Nothing checks which account answered. Run `gh api user --jq .login`
    once per account and refuse on a mismatch.
  - `logged_in()` uses `gh auth status -h host`, which succeeds if *any*
    account is logged in on that host. It should check the specific login.
  - `gh auth token` may trigger an OS keyring prompt (macOS Keychain) in
    a non-interactive context. Document it and set a timeout.
  - `forge email` needs the `user:email` scope, which gh does not request
    by default. Expect "insufficient scope" and print
    `gh auth refresh -h github.com -s user:email`.
  - `glab repo list` defaults to 20–100 per page. Specify pagination
    (`--per-page 100 --page N`, or `glab api --paginate`).
  - `--orgs` is used in the examples but missing from the §2 synopsis.
- **Fix:** As listed. Add the "which account answered" check to tests,
  using the fake `gh`.

### R-023 — `forge similar` sends possibly confidential names to GitHub search

- **Where:** `05` §2 "forge similar", §9.
- **Problem:**
  - Local repo names include employer-internal repos (codenames, unreleased
    products) cloned from internal hosts. Sending them as `gh search repos`
    queries discloses them to GitHub under the user's personal account.
    The `online:` line says "no paths, emails or contents" but the names
    *are* the sensitive part.
  - GitHub search is limited to ~30 requests per minute for authenticated
    users [believed], so 12+ names hit the limit quickly.
- **Fix:** If the feature stays (see Q2):
  - Only send names of repos whose remote is on that same forge, or that
    have no remote.
  - Add `--name PATTERN` to choose names.
  - Show the full list and confirm (not just a flag).
  - Throttle and handle HTTP 403 rate-limit responses.

### R-024 — The guard's `core.hooksPath` lives in a file that `apply` regenerates

- **Where:** `06` §2 ("`~/.gitconfig-identities` … 06 adds the key when
  `guard.hooks`") and `guard install` ("same change without editing the
  TOML").
- **Problems:**
  - `guard install` adds `core.hooksPath` to a generated file while
    `[guard] hooks = false`. The next `gitpro apply` (any config change)
    rewrites the file from the TOML and **silently removes the guard**.
    `gitpro check` reports drift in the meantime.
  - `plan.build` needs the guard settings, a signature change
    (`build(d, ids, env)` → needs `Config`) that 01 doesn't define. It
    affects `cmd_plan`, `cmd_apply`, `cmd_check`, `cmd_status`,
    `cmd_uninstall`, and 16's `applied` check.
- **Fix:**
  - Pick one owner. Either `guard install` refuses without
    `[guard] hooks = true` and prints the TOML line (principle 1), or the
    hooksPath goes into its own generated file
    (`~/.gitconfig-gitpro-guard`) included from the managed block, so
    `apply` leaves it alone.
  - 01 defines `plan.build(cfg: Config, ids, env)` (or adds a `guard`
    parameter) so that 06 does not change a shared signature.

### R-025 — Hook files fail `render.is_generated`, so uninstall never removes them

- **Where:** `06` §2 ("first line `#!/bin/sh`, second line the generated
  marker"); README §8.6; `render.is_generated` (matches only at the start
  of the file); `plan.teardown`.
- **Problem:** `teardown` and stale-file cleanup remove only files that
  pass `is_generated`. Hook files have the shebang on line 1, so they
  fail. `gitpro uninstall` leaves the hooks dir in place, and stale-file
  cleanup never removes old hooks.
- **Fix:**
  - 01 extends `is_generated` to accept a `#!` line followed by the
    marker on line 2, and owns that change.
  - Add a test: `uninstall -y` after `guard install` leaves no files.

### R-026 — The guard overrides a system-level `core.hooksPath`

- **Where:** `06` §2 and §7 (only global `core.hooksPath` is detected).
- **Problem:** Some companies set `core.hooksPath` in `/etc/gitconfig`
  (secret scanning, commit policy). A global include overrides the system
  value, so installing the guard turns off the company hooks without
  telling anyone.
- **Fix:**
  - Detect with `git config --show-scope --get-all core.hooksPath` across
    all scopes.
  - If the system scope sets it, refuse unless
    `chain_to = <that path>`, and print why.

### R-027 — Other gaps in 06

- **Where:** `06` §2, §6, §7.
- **Gaps:**
  - The pre-push stdin temp file needs `mktemp` and a
    `trap 'rm -f "$tmp"' EXIT`. Specify it in the script.
  - The comment "or core.hooksPath set in the repo" is wrong: with a
    repo-local hooksPath the global script never runs.
  - `guard status` "repos with their own core.hooksPath" means walking
    every repo under the scan roots on each `status` call (and on 16's
    `doctor` and 15's tooltip). Make the bypass scan explicit
    (`guard status --repos`) or cache it.
  - Shebang detection for repo mode accepts only `#!/bin/sh` and
    `#!/usr/bin/env bash`. Also accept `#!/bin/bash`, `#!/usr/bin/env sh`
    and `#!/usr/bin/env zsh`. Refuse others, as specified.
  - `guard install`'s undo is `gitpro guard uninstall`. If gitpro is
    broken or removed, that does not work. Principle 2 asks for an undo
    script or a backup. Write
    `~/.local/state/gitpro/guard-undo-<ts>.sh` (remove the files and
    restore the gitconfig backup).
  - `[ -z "$GITPRO_SKIP" ]` means `GITPRO_SKIP=0` also skips. Document
    "any non-empty value".
- **Fix:** As listed.

### R-028 — `guard.HOOKS` is a merge hotspot with unclear lazy-import rules

- **Where:** `06` §11 (`HOOKS: dict[str, Callable]`), `08` §11 (one dict
  entry), `14` §12 (two entries).
- **Problem:** Three packages in waves 2 and 3 edit the same dict
  literal. 06 also ships a placeholder `commit-msg` entry that 08 must
  replace. Nothing says how an entry imports its module lazily (to keep
  R-009), or how several checks combine (policy after identity, both on
  pre-commit).
- **Fix:**
  - Make `HOOKS` a `dict[str, list[str]]` of dotted function paths
    (`"pre-commit": ["gitpro.guard:identity", "gitpro.policy:check_commit"]`),
    resolved with `importlib` at call time. A missing module is skipped.
  - Or have 06 define a fixed list with all three entries up front
    (identity, trailers, policy), each guarded by `try: import`. Then 08
    and 14 never touch `guard.py`.

### R-029 — The wave plan has hidden dependencies

- **Where:** README §9, plus the package headers.
- **Problems:**

  | package | wave | needs | from |
  |---|---|---|---|
  | 09 | 1 | `fake_bin` fixture | 01, wave 1 but unmerged |
  | 09 | 1 | `forge.run` | 05, wave 2 |
  | 04 | 1 | `forge.run` (R-021) | 05, wave 2 |
  | 15 | 2 | CLI version 0.2.0 for `MIN_CLI = "0.2.0"` | 17, wave 3 (the bump) |

  Until 17 merges, 15's extension shows `gitpro?` for every build, and its
  integration test 6 cannot pass. 01 §2.3 even prints `gitpro 0.1.0`.

  15 also hosts the VS Code commands of 07 (clone), 12 (copyEnv) and 16
  (doctor) in its `identityBar.ts`, so those packages can't finish their
  VS Code parts without 15.

  "Wave 1 can stub against the dataclass fields" is fragile, because 01's
  dataclasses change (R-010).
- **Fix:**
  - 01 bumps the version to `0.2.0.dev0` and fixes the conftest fixtures
    first, as a tiny "01a" merged before anything else.
  - The extension uses feature detection (`gitpro why --help` exit code)
    rather than version comparison, or `MIN_CLI = "0.2.0.dev0"`.
  - Move 09's `--online` and `--upload` parts to wave 2.
  - Give 07, 12 and 16 their own small `.ts` files with one `register`
    call each.

### R-030 — Shared modules have no owner

- **Where:** `11` §11 (jsonfile.py "owned by 10, copied by 11"), `13`
  (JSONC editing plus backup/undo), `06`/`10`/`11`/`13` (all need atomic
  write, backup and undo), `vscode/package.json` (edited by 02, 03, 13
  and 15; not listed in the conflict rules).
- **Problem:**
  - Two wave-2 packages are told to carry a copy of the same helper and
    "remove it on merge". The first one to merge decides the API.
  - `files._atomic_write` is private but needed by four packages.
  - `package.json` `contributes.commands`, `configuration` and `menus`
    are JSON arrays that every VS Code package appends to, so they will
    conflict.
- **Fix:**
  - 01 ships `jsonfile.py`: `edit_json(path, mutate, *, command,
    jsonc=False) -> Result`, with preview diff, backup, atomic write and
    undo script.
  - Make `files.atomic_write` public.
  - Add `package.json` to the conflict rules: one package (15) owns the
    `contributes` sections, and others list their entries in their spec
    for 15 to add, or merge in a fixed order.

### R-031 — `gitpro clone` creates private checkouts and changes auth method

- **Where:** `07` §2 steps 3 and 6, §6.
- **Problems:**
  - `main()` sets `os.umask(0o077)`, and `git clone` inherits it. Every
    file in the checkout ends up 0600 and every dir 0700. git does not
    store those modes, but local tools see them: a Docker `COPY` into an
    image that runs as a non-root user cannot read the files, and a
    shared or served directory breaks. The "parent dirs 0755" claim is
    also false under that umask. Fix: restore the user's original umask
    for the `git clone` child (`preexec_fn` or save it before `main`
    changes it).
  - Step 3 rewrites `https://` to ssh for identities with an `ssh_key`
    but no alias. Users who authenticate over https (credential helper,
    `gh auth setup-git`) get a different auth path. A host not yet in
    `known_hosts` fails, and in VS Code (no TTY) it fails with "Host key
    verification failed". Keep the user's scheme unless `rewrite` is on.
    `--ssh` could request the rewrite.
  - The ssh URL is hard-coded as `git@…`. It must use `ssh_user`.
  - Pass `git clone [args] -- <url> <dir>`, so a URL starting with `-`
    can never become an option.
  - `--dry-run` is used by 07 §5 and by 15, but it is missing from the §2
    synopsis.
- **Fix:** As listed. Add a test: the checkout file mode follows the
  caller's umask (e.g. 022 → 0644).

### R-032 — `audit --rewrite` rewrites more than the offending commits

- **Where:** `08` §2 (`rebase --exec "git commit --amend --no-edit
  --reset-author"`), and the `06` pre-push fix text.
- **Problems:**
  - `--exec` runs after *every* commit in the range. `--reset-author`
    therefore takes over colleagues' and agents' commits in the same
    unpushed range, and resets every author date to now.
  - The amend runs the hooks: gitpro's own `commit-msg` (a
    `co_authored_by = "forbid"` policy stops the rebase halfway at the
    first commit with a Claude trailer) and the repo's lint hooks, once
    per commit.
  - The undo `update-ref refs/heads/<b> <old>` has no old-value check. If
    the user has committed since, the undo discards that work.
  - "Pushed" is measured against remote-tracking refs, which may be stale
    (no fetch). Say so in the confirmation.
  - `%(trailers:only,unfold)` output contains newlines inside the
    `%x00`-separated record. That is fine, but the parser must split on
    `%x1e` first and then on NUL. State this.
  - `co_authored_by = "require"` applies to every commit in the range,
    including teammates' commits. Limit it to commits authored by the
    user's own emails (as in R-005).
- **Fix:**
  - Amend only the offending commits:
    ```
    --exec 'case "$(git log -1 --format=%ae)" in <bad emails>) git commit --amend --no-edit --no-verify --reset-author;; esac'
    ```
    or better, set `GIT_AUTHOR_*` and `--date` to keep the original date
    (`--no-verify` is justified because the tree is unchanged).
  - Undo with `git update-ref refs/heads/<b> <old> <new>` so it refuses
    if the branch moved.
  - Add tests: a colleague's commit in range keeps its author and date,
    and a forbidden-trailer commit in range does not stop the rebase.

### R-033 — `gitpro auth` and `keys --health` misreport common states

- **Where:** `09` §2 tables, §7.
- **Problems:**
  - `ssh -T -o BatchMode=yes` with a passphrase-protected key that is not
    in the agent fails with `Permission denied (publickey)`. 09 reports
    that as `denied`, with "key not registered → gitpro keys --upload".
    The user then tries to upload a key that is already registered, and
    GitHub answers "key is already in use". Fix: when the key has a
    passphrase (from the health probe) and is not in `ssh-add -l`, report
    `locked: add it to the agent (ssh-add <key>)`, not `denied`.
  - The Bitbucket pattern `authenticated via ssh key.*logged in as` mixes
    two generations of the greeting. Current Bitbucket prints
    "authenticated via ssh key." with no "logged in as", and older output
    was "logged in as <user>." [uncertain]. Match either line, and allow
    "login unknown".
  - `gh ssh-key list` needs the `read:public_key` scope. Signing keys need
    `read:ssh_signing_key`, and upload needs the `admin:` variants. gh does
    not request these by default. No failure mode covers "insufficient
    scope". Add one with `gh auth refresh -h <host> -s <scope>`.
  - `gh ssh-key list --type signing` [uncertain]: `--type` exists on
    `gh ssh-key add`, and I am not sure `list` has it. The listing may
    instead have a type column. Same for `glab ssh-key add --usage-type`
    [uncertain]. 09 says "verify".
  - The permission check says "private 0600, ~/.ssh 0700" and marks
    anything else `✗` (exit 1). A 0400 key is stricter and fine, and a
    0755 `~/.ssh` is accepted by ssh. Use "no group or other write; no
    group or other read for private keys", and make `~/.ssh` 0755 a `⚠`
    at most.
  - `ssh-keygen -l` prints ECDSA curves as bits (256/384/521), not
    "nistp256". Map the bits.
- **Fix:** As listed. Add a test for the passphrase-plus-no-agent case.

### R-034 — Agent command parsing is easy to slip past; the deny message hands the agent the bypass

- **Where:** `10` §2 step 2, the deny reason text, §12.
- **Problems:**
  - `shlex.split` treats a newline as whitespace [verified]:
    `"ls\ngit commit -m x"` becomes one command, `ls git commit -m x`,
    which is allowed. Agents often send multi-line commands.
  - Splitting on `;`, `&&`, `||` and `|` *before* tokenising breaks quoted
    messages that contain those characters. Splitting *after* `shlex`
    loses the operators.
  - These forms slip past:
    - `bash -c "git commit …"`, `sh -c`, `eval`
    - subshells and groups: `( … )`, `{ …; }`
    - `xargs git …`, `time git`, `exec git`
    - `/usr/bin/git`, `\git`
    - aliases such as `g commit`
    - `git -c alias.x=commit x`
  - The deny reason says `Bypass: GITPRO_SKIP=1`. The agent reads the
    reason, so it learns the bypass. Since `agent-check` reads its own
    environment, not the command's, a retry as
    `GITPRO_SKIP=1 git commit` is denied again. The message promises a
    bypass that doesn't work for the agent, but does work for the git
    guard when the agent retries.
- **Fix:**
  - Tokenise with `shlex.shlex(punctuation_chars=True)`, treat newlines as
    separators, and recurse into `sh -c`/`bash -c` arguments.
  - Fall back conservatively: if the command contains the word `git`,
    `gh` or `glab` anywhere and parsing was uncertain, run the check
    (which is still read-only and cheap).
  - Take the bypass text out of the agent-facing reason ("ask the user").
    Say in docs that the user turns the check off with `[agents] hook`.
  - State the goal honestly in §1: this catches accidents, and the git
    guard is the enforcement layer.

### R-035 — Plugin hook and MCP files are in the wrong place and break plugin users without the CLI

- **Where:** `10` §2 and §11 (`.claude-plugin/hooks/hooks.json`), `11` §2
  and §11 (`.claude-plugin/.mcp.json`), `17` §3 (`"./hooks/hooks.json"`),
  `10` test 14.
- **Problems:**
  - In Claude Code plugins, components (`skills/`, `commands/`,
    `hooks/hooks.json`, `.mcp.json`) live at the **plugin root**. Only
    `plugin.json` (and `marketplace.json`) go inside `.claude-plugin/`
    [believed, high confidence]. This repo already follows that:
    `skills/` is at the root. 10 and 11 put the files inside
    `.claude-plugin/`, and 17 points at `./hooks/hooks.json`. Those
    disagree, and the 10/11 paths would not load.
  - The plugin hooks file has a top-level `{"hooks": {…}}` wrapper
    [believed]. Confirm it.
  - Installing the plugin installs the hook for users who have only the
    skill and no `gitpro` CLI. Each Bash call then reports a hook error
    (exit 127; non-blocking but noisy on every tool call).
  - Test 14 detects a "plugin install" from a file in the repo. Plugin
    installation lives under `~/.claude/plugins/` and in `enabledPlugins`
    in settings, not in the repo.
  - User-scope install plus the plugin runs the check twice.
- **Fix:**
  - Put the files at `hooks/hooks.json` and `.mcp.json` at the repo root,
    and fix 10, 11 and 17 to agree.
  - Make the hook command fail open when the CLI is missing:
    `sh -c 'command -v gitpro >/dev/null 2>&1 || exit 0; exec gitpro agent-check --agent claude'`.
  - `agent status` detects the plugin from `enabledPlugins` and warns
    about duplicates.
  - The root `.mcp.json` doubles as Claude Code's *project* MCP config
    when someone works on gitpro itself. That is acceptable; note it.

### R-036 — The installers write files that other people or other processes own

- **Where:** `10` §2 `--scope project`, `11` §2 `mcp install` table.
- **Problems:**
  - Claude Code project scope: `.claude/settings.json` is a team-shared,
    tracked file. Installing there commits a hook that runs `gitpro` for
    every teammate (see R-035 for what happens without the CLI). Use
    `.claude/settings.local.json` (personal, gitignored by Claude Code)
    for project scope, or require `--shared`.
  - `~/.claude.json` (11, user-scope MCP) is rewritten constantly by a
    running Claude Code (project state, counters). gitpro reads it,
    mutates it and writes it back, which risks a lost update in either
    direction. Prefer `claude mcp add --scope user gitpro -- gitpro mcp`
    when the `claude` CLI is on PATH. Otherwise warn "close Claude Code
    first" and re-read and compare just before the atomic replace.
  - `.mcp.json` in a project is shared too. The same rule applies.
  - Cursor (`mcpServers` wrapper) and Gemini (`mcpServers`; `type` may
    not be accepted) entries are underspecified; the table says "same".
- **Fix:** As listed, with the exact JSON path per agent in the table.

### R-037 — MCP server robustness

- **Where:** `11` §2 and §7.
- **Problems:**
  - Tools reuse command code that prints through `view`/`rich` to stdout.
    Any stray print corrupts the JSON-RPC stream. Child processes inherit
    the server's stdin (the client's pipe), so a `git` or `ssh` child that
    reads stdin would eat protocol messages. Fix:
    - wrap each tool in `contextlib.redirect_stdout(sys.stderr)`;
    - run every subprocess with `stdin=DEVNULL` and captured output;
    - add a test that runs a tool which warns and checks stdout is still
      valid JSON-RPC.
  - "Anything else → -32601" must exclude **notifications** (no `id`:
    `notifications/cancelled`, `notifications/roots/list_changed`). A
    server must not reply to them.
  - Protocol versions: 2025-11-25 exists (and possibly newer by build
    time) [believed]. Keep the "echo if supported, else newest" rule, and
    put the list in one constant that is easy to update. Send
    `structuredContent` only for 2025-06-18 and later.
  - A `scan`/`lost` call with `roots: ["/"]` blocks the single-threaded
    server for minutes, and the client times out. Cap roots to the
    user's `[scan] roots` (or under `$HOME`) unless given explicitly, and
    report progress or limits.
  - Newer MCP guidance reports input validation errors as tool results
    (`isError: true`) so the model can correct itself [uncertain].
    Consider it for schema failures.
- **Fix:** As listed.

### R-038 — `gitpro env` leaks one repo's identity into other repos and ignores `glab_account`

- **Where:** `12` §2 rules, §8.
- **Problems:**
  - `eval "$(gitpro env)"` exports `GIT_SSH_COMMAND` (and with `--only
    author` the `GIT_AUTHOR_*` variables) into the *shell*. After `cd` to
    a personal repo, git still uses the work key and author. That is the
    exact failure gitpro exists to prevent, and 06 and 10 then report
    `env_override`.
  - git already chooses the key through config. Recommend printing
    `GIT_SSH_COMMAND` only with `--only ssh`, and lead the docs with the
    direnv recipe (scoped per directory).
  - The rule text contradicts itself: "only when … no `ssh_alias`" and
    then "decision: print it whenever `ssh_key` is set".
  - GHES: `GH_TOKEN` is ignored for enterprise hosts (R-022). Print
    `GH_ENTERPRISE_TOKEN` for non-github.com hosts.
  - glab: `GITLAB_TOKEN="$(glab auth token)"` ignores `glab_account`
    (glab has one login per host), so two GitLab identities on gitlab.com
    get the same token. Whether `glab auth token` exists in current glab
    is [uncertain]. If `GITLAB_TOKEN` is already set (from the previous
    repo's eval), glab probably returns it [believed]. Emit `unset
    GITLAB_TOKEN;` before the command, and warn when `glab_account`
    differs from the logged-in user.
  - A key path containing a space breaks `GIT_SSH_COMMAND='ssh -i …'`.
    Quote it inside the string.
- **Fix:** As listed. Add a test for `cd` from a work repo to a personal
  repo in one shell with the direnv recipe.

### R-039 — devcontainer integration writes a team file and can break container start for others

- **Where:** `13` §2, §6, §8.
- **Problems:**
  - `devcontainer.json` is tracked and shared. Adding a host-side command
    imposes gitpro on every teammate.
  - On a **Windows host** (Docker Desktop with VS Code), a string
    `initializeCommand` runs in `cmd.exe` [believed]. There, `|| true` and
    `;` are not valid (`true` isn't a command), so the command fails and
    the container does not start.
  - `${localWorkspaceFolder}` is unquoted inside a shell string. A path
    with a space (common on macOS) splits into two arguments, `|| true`
    hides the failure, and the pin never happens.
  - Converting the user's **array** form to the **object** form turns one
    sequential command into parallel commands. Support for object-form
    `initializeCommand` in older tooling and Codespaces is [uncertain].
  - Creating a minimal `devcontainer.json` (`name` +
    `initializeCommand`, no image) makes VS Code offer "Reopen in
    Container" for a config that cannot build. That costs something and
    adds nothing.
  - Textual insertion only covers *adding* the key. Changing an existing
    `initializeCommand` in a commented file is unspecified: it either
    loses comments or isn't implemented.
  - VS Code launched from a desktop launcher has no `~/.local/bin` on
    PATH, so `gitpro pin` is "not found" and silently skipped every time.
  - `.devcontainer/<name>/devcontainer.json` (multiple configs) is not
    located.
- **Fix:**
  - Never create the file.
  - Default to printing the snippet. Make `--write` warn that the file is
    tracked and shared.
  - Use the array form `["sh", "-c", "command -v gitpro >/dev/null && gitpro pin \"$1\" || true", "sh", "${localWorkspaceFolder}"]`
    when the existing value is absent. Document that Windows hosts need
    their own setup.
  - Specify textual replacement with a JSONC tokenizer, or refuse when
    comments exist and print the line.
  - Locate the multi-config layout.

### R-040 — Policy checks: signing is easy to satisfy, and the Action has supply-chain and injection gaps

- **Where:** `14` §2 format, §3 signing check and Action.
- **Problems:**
  - "Signing check" uses the policy's `allowed_signers` "when provided",
    but `allowed_signers` is not a key in the policy format, and
    `policy.load` allows only listed keys. As written it is a validation
    error.
  - Counting `%G?` = `E` as **signed** in CI (no allowed signers) means
    any commit with a signature header passes `require_signing`, whether
    or not anyone can verify it. That should be stated as "has a
    signature (unverified)", or verification should be required (the
    policy ships `allowed_signers`).
  - `X`, `Y` and `R` (expired or revoked) are not classified.
  - The Action runs `pipx install "gitpro…"` from PyPI. If the PyPI name
    `gitpro` is not owned by this project (unknown; see Q7), every CI
    using the Action installs someone else's code. Install from the
    action's own checkout:
    `pipx install "${{ github.action_path }}"`.
  - `${{ inputs.policy }}` and `${{ inputs.range }}` are pasted into
    `run:` scripts, the classic Actions script-injection pattern. Pass
    them via `env:` and quote them in bash.
  - The `github-script` step is "optional" but has no `if:` and no
    `comment` input. On fork PRs the token is read-only, so posting a
    comment fails.
  - For non-PR events `github.event.pull_request.*` is empty and the
    range becomes `..`.
  - §1 says "future, section 13", but the future section is §10.
- **Fix:** As listed. Add a test that a commit with a corrupted signature
  fails when `allowed_signers` is provided.

### R-041 — Prompt cache can show `✓` for a repo that is now wrong

- **Where:** `16` §2 prompt cache.
- **Problem:** The cache key is the mtime of `.git/config`, the TOML and
  `~/.gitconfig-identities`. It misses:
  - `GIT_AUTHOR_EMAIL` and other env changes (`env_override`)
  - `~/.gitconfig`
  - per-identity files `~/.gitconfig-<id>`
  - the pin file `.git/git-identities`
  - `config.worktree`
  - edits within the same mtime tick

  The status a user trusts then lags reality, and the guardrail shows
  `✓`.
- **Fix:**
  - Key on `(path, size, mtime_ns)` of all of those files, plus a hash of
    the relevant env vars.
  - Never cache a result computed under an env override.
  - Add tests per input.

### R-042 — 15's quick action and its edit to `extension.ts`

- **Where:** `15` §2 quick pick, §4.
- **Problems:**
  - "Unset user.email in .git/config" runs `scan --fix --yes --repo`,
    which removes *every* repo-local override that scan knows (name and
    email), not just the one in the label.
  - 15 also deletes the old status-bar code from `extension.ts` while 02,
    03 and 13 add lines there in the same wave. That is a guaranteed
    conflict.
- **Fix:**
  - Run the exact `fix` command from `why --json` (a `git config --unset`
    argv), or label it "Remove repo-local name/email".
  - Land 15's `extension.ts` removal first (it can be a small preparatory
    merge), or keep the old code path behind the new registration.

---

## Minor

- **R-043** `01` §2.3. `--version` already exists (argparse
  `action="version"`). Only the output format needs checking.
- **R-044** `01` §2.1. `which --json` uses the lowercase key
  `core.sshcommand` while text output and other keys use camelCase. Pick
  one and document it. git's canonical form is lowercase.
- **R-045** `01` §5. The JSON error path can't cover the non-POSIX refusal
  (before parsing). The root refusal (after parsing) can. Document both.
- **R-046** README §10 `fake_bin`. "Logs its argv … shell quoted" is hard
  in POSIX sh (no `printf %q`). Generate the fakes as Python scripts with
  a `sys.executable` shebang that log with `shlex.join`. Log
  `env:GH_TOKEN=set` in a separate field.
- **R-047** README §10 `repo_with_remote(commits=)`. "Authored as the
  repo's effective identity" fails when the temp home has no identity
  (git refuses to commit). Pass explicit `GIT_AUTHOR_*` and
  `GIT_COMMITTER_*`.
- **R-048** `01` §12 test 7. The expected message format for an unknown
  top-level key differs from today's text
  (`"{path}: unknown top-level keys ['foo']"` vs `_build`'s
  `unknown key(s) … (allowed: …)`). Say which format wins.
- **R-049** Python 3.9. Spec snippets use `str | None` in dataclass
  fields. That works only with `from __future__ import annotations`,
  which every existing module has. Make it a rule for new modules.
  Also ban:
  - `match`
  - `zip(strict=)`
  - `dataclass(slots=, kw_only=)`
  - `datetime.UTC`
  - `X | Y` at runtime (`isinstance`, type aliases)
  - `tomllib` without the `tomli` fallback

  CI already tests 3.9. Keep it in the matrix for every package.
- **R-050** `02` §2. `exclude` uses fnmatch on what, exactly? A bare `src`
  would exclude every directory named `src`. Specify that patterns match
  absolute paths after tilde expansion, and give examples.
- **R-051** `03`. Empty repos (no commits; `log -1` fails) and detached
  HEAD are not covered in the kinds table.
- **R-052** `04`. ssh config `Include` directives (and `Match` blocks) are
  not followed. Say whether `discover` follows `Include` (it should, with
  glob expansion, read-only).
- **R-053** `05` §8. "no `--online`" exits 2, but README §5 defines 2 as
  a config or runtime error. "Refused because a flag is missing" fits 1
  or a usage error better. Pick one and apply it the same way in
  04, 05 and 09.
- **R-054** `07`. VS Code clone runs with no progress or cancel and no
  `GIT_TERMINAL_PROMPT=0` / `GIT_SSH_COMMAND` BatchMode. A credential
  prompt can't be answered from the extension, and the clone sits until
  ssh gives up.
- **R-055** `09` §2. The "used by config" row and §11 still contain
  editing notes ("(cannot happen) — instead:", "decision:" mid-sentence).
  README §9 says 09 touches `keys.py`, 09 §11 says it doesn't, and 09
  says `cli.py` gets two lines. Clean this up.
- **R-056** `09` §2 upload title. The key comment (often `user@hostname`
  of a work laptop) becomes the key title on a personal account. Show it
  in the confirmation (it already does) and allow `--title`.
- **R-057** `10` §2. "Open question in README" refers to a section that
  doesn't exist. README has no open-questions list. Add one (Cursor and
  Gemini field names, the plugin manifest format, `additionalContext`).
- **R-058** `10` §2. Re-serialising `settings.json`: specify
  `ensure_ascii=False`, key order preserved, trailing newline kept. Also
  re-read and compare just before replace (Claude Code may edit it
  concurrently).
- **R-059** `10` §2. Recognising the entry by "command starts with
  `gitpro agent-check`" misses the wrapped form from R-035 and absolute
  paths. Add a marker (for example a `"gitpro": true` key, if Claude Code
  tolerates unknown keys [uncertain], or a fixed command suffix
  `# gitpro`).
- **R-060** `11` §2. The `identities` tool exposes all emails to any MCP
  client. That is fine locally; note it in §8.
- **R-061** `12` §2. `--unset` for fish uses `set -e`. Use `set -e -g`
  for a global unset in functions. pwsh unset is
  `Remove-Item Env:GH_TOKEN`; specify it.
- **R-062** `13` §2. `devcontainer status` "inside container" detection
  lists env vars. Make it the same function as the extension's
  (documented once), or the two will drift.
- **R-063** `14` §3. Running the policy check in the pre-commit hook
  loads and validates the policy TOML on every commit. Cache it by mtime,
  or it eats the R-009 budget.
- **R-064** `15` §2. The `fs.watch` config watcher on a path from
  `why --json` needs re-arming when an editor replaces the file
  atomically (rename). Use VS Code's `createFileSystemWatcher` with a
  `RelativePattern`.
- **R-065** `16` §2. The zsh snippet `precmd() { … }` replaces any
  existing `precmd` (oh-my-zsh, starship). Use
  `autoload -Uz add-zsh-hook; add-zsh-hook precmd …`.
- **R-066** `16` §2 completion. "Static text generated … at build time"
  vs "walks `build_parser()`" (runtime). Pick one: runtime generation is
  simpler and keeps the output current.
- **R-067** `16`. `--values kinds` returns `lost` kinds, but `env --only`
  has different kinds (`gh,glab,ssh,author`). Give completion values per
  option.
- **R-068** `17` §5. "Writing subcommands = parsers with `--yes`"
  misclassifies `keys` (gains `-y` for `--upload`) and `audit` (only with
  `--rewrite`). Use an explicit list in the test.
- **R-069** `00-existing.md`. Accurate against HEAD (commit 487953a).
  Small additions:
  - the undo script's header comment and its 0600 mode (relevant to
    R-014);
  - `scan.suggest`'s inline id rules (relevant to R-021);
  - `main()` sets `umask 077` for all children (relevant to R-031).

---

## Questions for the product owner

- **Q1 — Paid tier.** The only mention is `14` §10 ("listed so the paid
  tier has a shape"): central policy distribution, dashboards, audit
  logs, seats.
  - Is all of 14 (policy file, CI check, Action, `team`) meant to stay
    free under MIT?
  - Should any free package leave hooks for paid features? Nothing in the
    spec prepares for them, and I think that is correct.
  - Please confirm the split before 14 is built.
- **Q2 — Keep `forge similar`?** It is the one feature that sends local
  information off the machine (R-023), its value is speculative, and
  GitHub search rate limits make it slow. I suggest cutting it from this
  release.
- **Q3 — Shell history in `discover`.** Should reading shell history be
  opt-in (`--history`) rather than default? It is the most sensitive
  local source (R-020).
- **Q4 — Guard defaults.** For the first release, should
  `on_mismatch` default to `warn`, with pre-push off by default, until
  the false-positive rules (R-005) have been used in the field? And is
  global mode (R-003) or per-repo mode the default?
- **Q5 — Writing to team files.** Should gitpro ever write tracked,
  shared files: `devcontainer.json`, `.husky/*`, `.claude/settings.json`,
  `.mcp.json`? I recommend print-only for all of them (R-004, R-036,
  R-039).
- **Q6 — Who may bypass the agent hook?** Should the agent ever be told
  how to bypass it, or only the user (R-034)? And in warn mode, should a
  mismatch prompt the user (`ask`) or only annotate (R-006)?
- **Q7 — PyPI name.** Do you own `gitpro` on PyPI? If not, the Action's
  `pipx install gitpro` and the docs install line point at someone
  else's package (R-040). Decide the published name now.
- **Q8 — Windows hosts.** Windows is out of scope everywhere, but a
  committed `devcontainer.json` reaches teammates on Windows (R-039). Is
  "breaks for Windows teammates" acceptable, or is 13 print-only?
- **Q9 — Scope for 0.2.0.** 17 packages is a lot for one release of an
  identity manager. Suggested core: 01, 06 (fixed), 10, 12, 15 and
  16 (doctor, prompt). Second: 02, 03, 07, 08, 09, 11. Defer: 04, 05,
  13, 14. Which do you want?
- **Q10 — Agents beyond Claude Code.** Cursor and Gemini hook fields are
  unverified (`10` §2). Ship only Claude Code in 0.2.0 and add others when
  someone can test them?
- **Q11 — GitLab multi-account.** glab has one login per host, so
  `glab_account` can only warn, and `gitpro env` can't pick between two
  gitlab.com accounts (R-038). Is that acceptable?
- **Q12 — One version for everything.** CLI, extension, plugin and
  marketplace all at `0.2.0`, with feature detection instead of
  `MIN_CLI` (R-029)?

---

## Verdict per work package

| WP | verdict | must fix before build |
|---|---|---|
| 00 existing | **ready** | (R-069 notes only) |
| 01 config/resolve/why | **ready after fixes**; land first, small | R-007, R-008, R-010, R-011, R-012, R-013, R-014, R-029 (version, fixtures), R-030 (jsonfile, atomic write, `is_generated`) |
| 02 scan roots | **ready after fixes** | R-015, R-016, R-017 |
| 03 lost | **ready after fixes** | R-018, R-019 |
| 04 discover | **ready after fixes** | R-020, R-021; Q3 |
| 05 forge | **ready after fixes** (drop or rework `similar`) | R-019, R-022, R-023; Q2 |
| 06 guard | **rework** | R-001 to R-005, R-024 to R-028 |
| 07 clone | **ready after fixes** | R-031 |
| 08 audit | **ready after fixes** | R-005 (shared rule), R-032 |
| 09 keys health/auth | **ready after fixes** | R-033; move online parts to wave 2 (R-029) |
| 10 agent hooks | **rework** | R-006, R-034, R-035, R-036 |
| 11 MCP | **ready after fixes** | R-035, R-036, R-037 |
| 12 env | **ready after fixes** | R-022, R-038 |
| 13 devcontainer | **rework** (or reduce to print-only) | R-039; Q5, Q8 |
| 14 policy | **rework** (after 06) | R-040, R-005, R-028, R-063; Q1, Q7 |
| 15 VS Code identity bar | **ready after fixes** | R-029 (version gate), R-030 (package.json), R-042 |
| 16 doctor/prompt/completion | **ready after fixes** | R-009, R-041 |
| 17 skill/docs | **ready after fixes** (finalise last) | R-035 (plugin paths), R-068 |

---

## Appendix: experiments (git 2.43.0, temporary HOME, no network)

| # | experiment | result |
|---|---|---|
| E1 | 06 chain script logic, global `core.hooksPath` = hooks dir, `timeout 3 git commit` | exit 124 (hung); the hook ran 1,541 times. `git rev-parse --path-format=absolute --git-common-dir` + `/hooks` gives `.git/hooks` (R-001). |
| E2 | `git rev-parse --git-path hooks` with global or local `core.hooksPath` | returns the hooksPath dir (`.husky` for a local one) (R-001). |
| E3 | pre-commit and commit-msg hooks printing `git var GIT_AUTHOR_IDENT` and `$GIT_AUTHOR_EMAIL` for `commit --author=Other`, `commit --amend` of that commit, `git -c user.email=… commit` | pre-commit already sees `Other` for `--author` and `--amend`, and the `-c` email (R-005). |
| E4 | includeIf `hasconfig:remote.*.url:git@gitlab.com:grp/**` vs `grp/sub/app` | matches (nested groups) (R-007). |
| E5 | same pattern vs `GRP/app` | no match (case-sensitive). |
| E6 | `https://github.com/o/**` vs `https://jane@github.com/o/r` | no match (R-007). |
| E7 | `git@gitlab.com:grp/**` vs `gitlab.com:grp/app` | no match (R-007). |
| E8 | `https://github.com/corp-*/**` vs `https://github.com/corp-a/sub/r` | matches. |
| E9 | `url.<alias>.insteadOf https://github.com/corp/`; `git remote -v` vs `remote.origin.url`; includeIf on alias pattern vs https pattern | `remote -v` shows the rewritten alias URL. The includeIf on the alias pattern does **not** apply; the one on the raw https pattern does (R-007). |
| E10 | `--show-origin` in main worktree, linked worktree, pin | `file:.git/config` (relative) in main; absolute in linked worktrees; `file:.git/git-identities` for the pin (R-011). |
| E11 | `extensions.worktreeConfig` + `config --worktree` | worktree scope appears in `--show-scope` (R-011). |
| E12 | `python -c "import shlex; shlex.split('ls\ngit commit -m x')"` | `['ls', 'git', 'commit', '-m', 'x']` (R-034). |
| E13 | `gitpro --version` wall time; `python -X importtime -c "import gitpro.cli"` | 80–90 ms; 68 ms cumulative import, mostly `rich` (R-009). |
| E14 | `.venv/bin/pytest -q` | 55 passed. |

---

## Verification pass

Date: 2026-10-02. Same rules as the first pass: read-only except this
section, git 2.43.0, temporary `HOME` under the session scratchpad,
`GIT_CONFIG_NOSYSTEM=1`, no network, gitpro never run against the real
home. Inputs: the revised `README.md` (§9 waves and file ownership),
`DECISIONS.md`, `REVIEW-RESPONSE.md`, and all of `01`-`17`. 01, 06 and 10
were read in full. The other packages were spot-checked for the fixes the
response claims and for interfaces they expect from earlier waves.
Experiments V-E1 to V-E8 are at the end of this section.

### Summary

- 22 of the 42 Blockers and Majors are fixed in the spec text. 14 are
  partly fixed. None is unfixed. The other 6 are R-010, R-018, R-020,
  R-031, R-033 and R-041: the text now says what was asked, but I did not
  re-test them.
- The revision is a big improvement. The chain script no longer recurses
  and it fails open ([verified], V-E4). No adapter emits "allow". Shared
  modules each have one owner. Within a wave, no two packages edit the
  same file.
- **New: 2 Blockers, 10 Majors, 20 Minors** (V-001 to V-033; V-024
  was folded into V-002).
  - **01 has 1 Blocker and 6 Majors.** The implementer needs them now:
    V-001, V-003 to V-008.
  - 06 has one Blocker, V-002, which also reaches the docs text.
- The response says the spec does things it does not do in four places.
  The spec text is the one to follow (V-018).

### 1. Blockers and Majors from the first pass

| ID | status | note |
|---|---|---|
| R-001 | fixed | Chain uses `--git-common-dir` + `/hooks`; `own_dir` guard; symlinked `chain_to` loop does not recurse [verified V-E4]. |
| R-002 | fixed | rc 0/1/2/127 from gitpro → commit succeeds with a warning; rc 3 → commit refused [verified V-E4]. 06 test 9 expects `git commit` to exit 3; git exits 1 (V-021). |
| R-003 | fixed | Repo hook runs after gitpro, also in linked worktrees [verified V-E4]. New cost problem from the stubs: V-009. |
| R-004 | partly | Tracked hook files are refused. Husky v9's `.husky/_` is untracked **and ignored**, so step 3 would edit it although the text says "never touched" (V-020). |
| R-005 | partly | Judging the committer is right. But inside hooks git always exports `GIT_AUTHOR_*`, so 01's `env_override` fires on `--author`, amend and cherry-pick, which is exactly the case R-005 was about (V-004, [verified V-E3]). pre-push still blocks foreign committers that are not in remote-tracking refs (V-010). |
| R-006 | fixed | No allow / `additionalContext` path; warn = stderr only; test 16. |
| R-007 | partly | Raw URL and full match are right [verified again]. The `**` rule in 01 §4.1 is wrong, so `match_url` would still disagree with git, and owner-less identities never match scp remotes even in git itself (V-001, [verified V-E1]). The response's "normalises like hasconfig" is wrong; 01's text (raw URL) is right. |
| R-008 | partly | `EXTENSIONS`, `register`, `extend`, `extend_command` and `capability` are defined. But `_extension_modules` drops modules that have no `register`, so 02's extend-only `scanroots` never runs (V-005). The hot-module parser contract cannot be implemented as written (V-006). |
| R-009 | partly | `hot.py` is right in shape. A hot module's parser cannot reach `common`, `capability` or `CAPABILITIES` without importing `cli` (V-006). |
| R-010 | fixed (not re-tested) | Schema text is complete. |
| R-011 | partly | The `-z` format and scope table are right [verified V-E2]. Relative `file:` origins are relative to the **work-tree top level** (bare: git dir), not the git dir, so the pin is never recognised (V-003). |
| R-012 | partly | Table is clear. Two rules misfire: `env_override` in hooks (V-004) and `key_mismatch` (V-008). |
| R-013 | fixed | `shlex.quote` + `PATH_RE`. |
| R-014 | fixed | Minor naming detail, V-014. |
| R-015 | fixed | `config.load_scan` is lenient; `init && scan` exits 0. |
| R-016 | fixed in 02 | 03 still says `scanroots`, and its test 16 patches the wrong module (V-019). |
| R-017 | partly, accepted | One source of truth (TOML via `scan --roots --write`) meets the goal. |
| R-018 | fixed (not re-tested) | Kinds table is sound on reading; `duplicate` handles missing objects. |
| R-019 | fixed | 01 §5.5 uses `("ok"|"renamed"|"missing"|"unknown", detail)`. The response says True/False/None; follow 01. |
| R-020 | fixed (not re-tested) | `--history` opt-in. |
| R-021 | fixed | 04 is in wave 4, after 02's `find(max_depth)`. |
| R-022 | fixed | `GH_TOKEN` vs `GH_ENTERPRISE_TOKEN` rule is right. `gh auth token -u` needs gh 2.40+ (V-029). |
| R-023 | fixed | `--online --send-repo-names` on every run. |
| R-024 | partly | The separate include file works. `guard install` without the TOML key is honest, but `check`/`status` then report drift and the next `apply` removes the guard (V-025). The file is `~/.gitconfig-gitpro-guard` (06), not the path in the response. |
| R-025 | fixed | Marker on line 2 accepted by `is_generated`. |
| R-026 | fixed | System `core.hooksPath` detected, guard refuses. |
| R-027 | partly | Rows added. New gaps: V-020, V-022, V-023. |
| R-028 | fixed | Fixed table in 01 §5.6, lazy, missing skipped. |
| R-029 | fixed | Waves are consistent with the dependencies, apart from same-wave soft dependencies (V-032) and three interfaces nobody provides (V-007). |
| R-030 | fixed | No two packages in one wave edit the same file (checked all four multi-package waves). |
| R-031 | fixed (not re-tested) | Clone needs `cli.CALLER_UMASK`, which 01 does not define (V-007). |
| R-032 | partly | Only offending commits are matched. But `--reset-author` replaces a colleague's authorship when only the committer was wrong. The `case` test matches substrings. Descendant commits are not byte-identical, and test 6 cannot pass as written ([verified V-E6], V-011). |
| R-033 | fixed (not re-tested) | BatchMode and `locked` states specified. |
| R-034 | partly, accepted | Good enough with the git guard as the second line. Tokeniser details in V-026. |
| R-035 | fixed | Repo-root `hooks/hooks.json` and `.mcp.json`; the `command -v` wrapper is right. The manifest keys are better omitted (V-027). |
| R-036 | partly | `edit_json` everywhere. Backups land next to work-tree files, and a `settings.local.json` that gitpro creates is not gitignored (V-015, V-028). |
| R-037 | fixed | MCP robustness rows present. |
| R-038 | fixed | `GIT_SSH_COMMAND` opt-in with warning. |
| R-039 | fixed | Print-only default, array form. Windows note in V-030. |
| R-040 | partly | `%G?` table and `env:` inputs fixed. Installing with `python3 -m pip install` on GitHub's current Ubuntu runners is likely refused (PEP 668) [believed] (V-012). |
| R-041 | fixed (not re-tested) | Stat-keyed cache, bypass on `GIT_CONFIG_*`. |
| R-042 | fixed | Quick action runs `fix_argv`; 15 owns `extension.ts`/`package.json`. |

### 2. Rejected / partly findings: do I accept the reasoning?

- **R-017 (partly):** accepted. Writing the TOML through 02's CLI keeps
  one source of truth, which was the point.
- **R-053 (partly):** accepted. Exit 2 is now defined in README §5 as
  "cannot run", and `forge.no_online_error` makes it the same everywhere.
  Keeping the install hint matters more than matching argparse.
- **R-024 (marked fixed; I rate it partly):** the reasoning is fair, but
  the user experience is not. See V-025.
- **R-034 (marked fixed; I rate it partly):** accepted as good enough.
  The agent hook is a convenience layer and the git guard is the
  enforcement.
- **R-005, R-007, R-008, R-011, R-032 are marked "fixed" but are partly
  fixed:** the new findings below say what is left.

### 3. New findings

#### Blockers

**V-001 (01) - `wildmatch` treats `**` the way git does not; owner-less identities never match scp remotes [verified V-E1]**

- Problem: 01 §4.1 says "`**` matches anything including `/`". In git's
  wildmatch (`WM_PATHNAME`), `**` is special **only as a whole path
  component**: preceded by start-of-pattern or `/`, and followed by
  end-of-pattern or `/`. Anywhere else it behaves like `*` and does not
  cross `/`. Measured with `includeIf hasconfig:remote.*.url`:
  - `git@github.com:**` does **not** match `git@github.com:o/r.git`,
    but does match `git@github.com:r.git`;
  - `git@github.com:o**` does not match `o/r.git`;
  - `git@github.com:*/**` and `https://github.com/**` match nested paths.
- Consequences:
  - `render.url_patterns` emits `git@host:**` (and `git@alias:**`) for an
    identity with no `owners`. So **today, in git itself**, such an
    identity never applies to scp-form remotes. This is an existing
    product bug.
  - A `match_url` built to 01's text would report "ok" where git applies
    nothing. That is precisely the disagreement R-007 was about.
- Fix:
  1. In `wildmatch`, `**` is special only when it is a whole component.
     Otherwise translate it as `[^/]*`.
  2. For an owner-less identity, `render.url_patterns` should emit
     `user@h:*/**` (and keep `user@h:*` for single-segment paths if
     wanted).
  3. Add owner-less ssh/alias/https cases and the `:o**` case to the
     differential test 13, so git itself is the reference.
- `apply` will rewrite the generated gitconfig. Note it in 00 as a fixed
  bug.

**V-002 (06) - Repo-mode block and the docs one-liner block every clean commit when they are the last line, and fail closed under `sh -e` [verified V-E7]**

- Problem: the managed block (06 §3.5 step 4) is
  `… || { gitpro guard check pre-commit "$@"; [ $? -eq 3 ] && exit 1; }`.
  The pre-push variant ends the same way, and so does the line in
  §13 "Docs text".
- When gitpro returns 0, `[ 0 -eq 3 ]` is false, so the group's status is
  1. If that is the last command of the script, the hook exits 1:
  - Repo mode on a repo **without** an existing hook creates a file
    whose only content is this block. Measured: `git commit` exits 1
    with a clean gitpro, for every commit and every push.
  - A user who appends the docs line at the end of a husky hook gets
    the same result.
  - Husky v9 runs hooks with `sh -e`. There, a gitpro error (rc 1 or 2)
    aborts the hook, so it fails closed, which breaks the fail-open
    promise.
- Fix: use the `if` form that the chain script already uses (06 line 123)
  everywhere. This form exits 0 on rc 0, 1 and 2 and when gitpro is
  missing, and exits 1 on rc 3, in dash and bash, with and without `-e`,
  first or last line (verified):
  ```sh
  if [ -z "${GITPRO_SKIP:-}" ] && command -v gitpro >/dev/null 2>&1; then rc=0; gitpro guard check pre-commit "$@" || rc=$?; if [ "$rc" -eq 3 ]; then exit 1; fi; fi
  ```
  Write the pre-push variant the same way, with `mktemp` failure falling
  through instead of running `exec <""`.
- Add a test: repo mode on a repo with no hook, then a clean commit
  succeeds. Add a static test that every generated or printed hook line
  passes the four-case matrix.

#### Majors

**V-003 (01) - Relative config origins are resolved against the wrong directory [verified V-E2]**

- Measured:
  - `git config --show-origin` prints `file:.git/config` and
    `file:.git/git-identities` relative to the **work-tree top level**,
    also when run with `-C sub/deep`;
  - in a bare repo it prints `file:config`, relative to the git dir;
  - linked worktrees and `GIT_DIR` give absolute paths.
- 01 §4.2 resolves against `--git-dir`, which gives
  `/r/.git/.git/git-identities`. So the pin is never recognised: its
  scope becomes `repo`, the problem shows as `override`, and the
  suggested fix (`config --unset`) edits the wrong file.
- Fix: resolve against `--show-toplevel` when there is a work tree, and
  against the git dir otherwise. Add a test that runs `effective()` from
  a subdirectory and from a bare repo with a pin.

**V-004 (01, 06, 08, 14) - `env_override` fires inside every commit hook [verified V-E3]**

- Inside `pre-commit` and `commit-msg`, git always exports
  `GIT_AUTHOR_NAME` and `GIT_AUTHOR_EMAIL`: for plain commits, for
  `--author`, for amend. `GIT_COMMITTER_*` appear only if the user set
  them.
- 01 §4.2 reads these variables from the current environment, so in a
  hook `ctx.report` carries `env_override` whenever the author differs
  from expected, which is exactly the colleague case.
- Effects:
  - 06 takes the cause from the first problem and prints
    `unset GIT_AUTHOR_EMAIL` as the fix;
  - 08/14 entries that use `ctx.report.ok()` would block;
  - in the agent hook the variables come from the agent's environment,
    which is correct there.
- Fix: in 01, `compare(..., env=None)` takes the environment explicitly.
  `guard.Context` builds the report with `GIT_AUTHOR_*` removed and
  judges the author only through `ctx.author_email` and the own-emails
  rule. State this in 01 §4.2 and 06 §2, and add a guard test for
  `--author` with a colleague and a correct committer, which must pass.

**V-005 (01) - `_extension_modules` skips extend-only modules; `scan -c/--roots` would never exist**

- 01 §2.1 yields a module only `if hasattr(mod, "register")`, and the
  second pass calls `extend` only on yielded modules. 02 says
  "`scanroots.py` has no `register`; it has `extend`".
- Fix: yield modules that have `register` **or** `extend`; call
  `register` only when present. Add a test with an extend-only fake
  module.

**V-006 (01) - The hot-module parser contract cannot be implemented as written**

- 01 §2.2 says the hot module's parser "is built by the same function
  `register` uses, called with a private `argparse.ArgumentParser`
  instead of a subparsers object". But:
  - `register(sub, common, confirm)` calls `sub.add_parser`, which an
    `ArgumentParser` does not have;
  - the hot path has no `common`/`confirm` parents (`-c`, hidden
    `--home`, `-y`) without importing `cli`;
  - `cli.capability()` lives in `cli`, which imports `view`/`rich` at
    module level, so any shared parser code that registers capabilities
    breaks the import rule (test 30).
- 06 already invented `guard.make_parser()`; 10 and 16 have nothing
  comparable.
- Fix, now in 01:
  - add a stdlib-only `gitpro/args.py` with `common()`, `confirm()`,
    `CAPABILITIES` and `capability()`. `cli` re-exports them;
  - each hot module exposes `add_arguments(p)`. Its `register` does
    `add_arguments(sub.add_parser(name, parents=[common, confirm]))`,
    and its `main` does
    `p = ArgumentParser(prog="gitpro <name>", parents=[args.common()]); add_arguments(p)`.
  - Note also: `hot.main` sends **all** `guard` argv, including
    `install`/`uninstall`, to `guard.main`, which must then reproduce
    cli's root refusal, `--json` errors and `_fail` (or forward
    non-check subcommands to `cli`).

**V-007 (01) - Interfaces later packages use that 01 does not provide**

| user | needs | status in 01 |
|---|---|---|
| 07 §2 | `cli.CALLER_UMASK` (and "hot likewise") | absent. `main` calls `os.umask(0o077)` without saving the return value. Add `CALLER_UMASK = os.umask(0o077)` in both. 07 can use `subprocess.run(..., umask=CALLER_UMASK)` (3.9+) instead of `preexec_fn`. |
| 14 §6 | problem code `policy` and hook point `resolve.EXTRA_CHECKS` | absent. 14 also describes a second mechanism (a try-import of `policy.check_repo` inside `compare`). Pick one in 01 now. |
| 06 §4 pre-push | `compare(remote=…)` accepting a URL (`git push <url>` passes the URL as the "name") | 01 says "remote name" only. Accept a name or a URL. |
| 10 §2.3 / 16 | `guard.is_installed()` (10) vs `guard.status()` (16) | 06 §12 lists `status` only. Name one in 06. |
| 16 §2 | `config.find` | exists today (`find(explicit, home)`); fine. |

**V-008 (01) - `key_mismatch` false positives**

- `render.identity_gitconfig` writes `core.sshCommand = ssh -i ~/.ssh/…`
  (a tilde path, `_shell_path`). A comparison against the expanded
  `ssh_key` path differs unless the parsed `-i` value goes through
  `expanduser`.
- `managed_gitconfig` never writes `sshCommand` for `[default]`, yet a
  `[default] ssh_key` makes `Expected.ssh_key` non-None. Every repo on the
  default identity then gets `key_mismatch`.
- Fix: expand `~` in the parsed value. `Expected.ssh_key` is None for
  `[default]` (or whatever render actually writes: derive Expected from
  the same function that renders).

**V-009 (06) - Pass-through stubs for high-frequency hooks make git much slower [measured V-E5]; list incomplete**

- A `reference-transaction` stub runs for every ref transaction, twice
  (prepared, committed). Fetching 300 tags went from 0.055 s to 4.18 s
  (75x). 20 commits went from 0.088 s to 0.45 s. `post-index-change`
  runs on every index write.
- `pre-merge-commit` is missing from the list.
- The text says "21 pass-through hooks" but lists 24.
- Fix:
  - do not install stubs for `reference-transaction`,
    `post-index-change`, `fsmonitor-watchman` and the `p4-*` hooks;
  - document that these repo hooks are not chained (they are rare in
    practice), or install those stubs only with `guard install --all`;
  - add `pre-merge-commit`;
  - derive the count from `render.HOOK_NAMES` in text and tests.

**V-010 (06) - pre-push still blocks foreign committers**

- Problem: pre-push lists `%ce` for every commit in
  `<local> --not --remotes ^<remote sha>` and blocks when the committer
  differs from expected. Commits fetched without a remote-tracking ref
  carry other people's committer emails and are blocked:
  - `gh pr checkout` of a fork PR;
  - `git pull <url> <branch>`;
  - `git fetch <url>`;
  - merging a contributor's branch.
- Fix: in pre-push, block only commits whose committer is one of the
  user's own emails and differs from expected. This is the same
  own-emails rule as for authors. A commit with a foreign committer is
  not the user's mistake. Add a test: push a branch containing a commit
  with committer `pat@other.test`, which passes.

**V-011 (08) - `audit --rewrite` replaces colleagues' authorship and does not keep the other commits identical [verified V-E6]**

- Measured with the exact `case` script from 08 §2.1:
  - First offender with author `Pat <pat@other.test>` and wrong
    committer: after the rewrite the author is `Me <me@corp.test>`.
    `--reset-author` throws the colleague's authorship away.
  - The pattern `*'me@personal.test'*` is a substring match: an author
    `notme@personal.test` was also reset.
  - A later offender is re-picked first, and the pick already sets the
    committer to the current user with **today's** committer date. The
    "keeps dates" promise breaks for every offender after the first.
  - Clean descendant commits (commit 4 in test 6) get new committer dates
    and SHAs, so test 6's "same dates" is wrong.
- Fix: do not use `rebase --exec`. By the time the exec runs, the pick
  has already replaced the committer and the committer date, so the exec
  can neither detect later offenders by `%ce` nor restore the dates.
  - Replay instead, without a working-tree checkout. For each commit
    `C` in `base..HEAD`, oldest first:
    `git commit-tree <C^{tree}> -p <new parent> -F <message file>`, with
    `GIT_AUTHOR_NAME/EMAIL/DATE` and `GIT_COMMITTER_NAME/EMAIL/DATE`
    copied from `C`.
  - Replace only the offending field: the committer when the committer
    email is in the offending set (exact comparison, not a substring).
    Replace the author only when the author email is one of the user's
    own emails and is not the expected one.
  - Finish with `update-ref refs/heads/<b> <new> <old>`. The undo script
    is the reverse `update-ref`.
  - Correct the text: descendants of the first rewritten commit get new
    SHAs, with identical tree, message, identities and dates.
  - Test 6 must check the author **and** the committer of a colleague's
    commit, and the committer dates of clean descendants.
- Also delete the leftover draft line in the 08 §2.1 code block
  (`… rebase --committer-date-is-author-date? no —`).

**V-012 (14) - The Action's install step likely fails on GitHub's Ubuntu 24.04 runners [believed]**

- Problem: `python3 -m pip install --quiet "$GITHUB_ACTION_PATH"` into
  the system Python. On Debian/Ubuntu 23.04+ that interpreter is marked
  externally managed (PEP 668), and pip refuses. Even where it works, the
  `gitpro` script may land in `~/.local/bin`, which is not on `PATH` in
  the next step.
- Fix: `python3 -m venv "$RUNNER_TEMP/gitpro" && "$RUNNER_TEMP/gitpro/bin/pip" install --quiet "$GITHUB_ACTION_PATH"`,
  then call `"$RUNNER_TEMP/gitpro/bin/gitpro"`; or require
  `actions/setup-python` first. Verify on a real runner before 14 is
  built.

#### Minors

- **V-013 (01) Test 29 passes trivially.** `import gitpro.cli` does not
  run `build_parser`, so the exact-name skip is not exercised. Call
  `build_parser()` in the test. `cli.py` imports `view`, `plan` and
  `scan` at module level today, which is fine (cli is not hot), but
  test 30 must not include `cli`.
- **V-014 (01) `undo.write(command, …)` names the file after `command`.**
  `command="scan --fix"` would put a space in the filename. Use a
  separate `slug` (`scan`, `agent`, `audit`, `devcontainer`, `guard`) or
  slugify. Keep the existing `scan-undo-<ts>.sh` name (00 item 14).
- **V-015 (01) `edit_json` backups land in work trees.** Writing
  `<file>.bak.<ts>` next to project `.mcp.json`, `devcontainer.json` or
  `.claude/settings.local.json` leaves an untracked file that
  `git add .` picks up. Put backups in `~/.local/state/gitpro/backup-<ts>/`,
  as 06 already does. Also, the `mkdir=` behaviour mentioned in 10/11
  is not in the signature.
- **V-016 (01) `hot.main` catches any `ImportError`.** A broken
  `guard.py` then silently falls through to `cli`. This is harmless only
  because `cli` re-imports it and fails loudly. Use the same exact-name
  rule as `_extension_modules`.
- **V-017 (01) Top-level `--json` and subcommand `--json` share
  `dest="json"`.** Subparser defaults overwrite the parent's value.
  Use `dest="version_json"` on the root parser.
- **V-018 (response) The response text disagrees with the spec in four
  places:**
  - R-007 says "normalises like hasconfig" (01: raw URL);
  - R-019 says True/False/None (01: 4-state tuple);
  - R-024 names `~/.config/git/gitpro-guard.gitconfig` (06:
    `~/.gitconfig-gitpro-guard`);
  - R-008 says `register(sub, parents)` (01: `register(sub, common, confirm)`).

  Implementers should follow the package files.
- **V-019 (03) The roots code and its test disagree.** 03 §2.1 imports
  `gitpro.scan.effective_roots`, but the prose and README §9 say
  `scanroots`, and test 16 patches `sys.modules["gitpro.scanroots"]`,
  so it tests nothing. `effective_roots(args.dirs, cfg.scan)` raises
  when `cfg` is None. Use `cfg.scan if cfg else None`, and in the test
  patch `gitpro.scan.effective_roots` away.
- **V-020 (06) Husky v9.** `core.hooksPath = .husky/_` is untracked and
  in `.husky/_/.gitignore`, so it passes the "tracked" refusal and step 3
  would edit husky's regenerated wrapper. Refuse when the hooks dir is
  ignored (`git check-ignore -q`) or is `.husky/_`, and print the line to
  add to `.husky/pre-commit` instead.
- **V-021 (06) Test 9 says `git commit` "fails with 3".** git exits 1
  when pre-commit fails, whatever the hook's code [verified V-E4].
- **V-022 (06) Other hook managers.**
  - The LFS step runs `git lfs pre-push` and the repo's own
    `.git/hooks/pre-push` (from `git lfs install --local`) may run it
    again.
  - The `filter.lfs.process` test is usually true globally, not per
    repo.
  - With a global `core.hooksPath`, the following are not described
    [believed]: `pre-commit install` refuses ("Cowardly refusing"), and
    `git lfs install` / lefthook write into or complain about gitpro's
    hooks dir.

  Add rows to §7 and a `guard status` note.
- **V-023 (06) `^<remote sha>` after a force push.** The remote's old
  sha may be absent locally, so `git log` fails and the check is
  silently skipped. Drop `^<remote sha>` when `git cat-file -e` fails
  (the `--not --remotes` part still bounds the range).
- **V-025 (06) `guard install` without `[guard] hooks = true`.**
  `gitpro check`/status (15, 16) then show drift, and the next `apply`
  uninstalls the guard. Either offer to add the key (the user's own TOML,
  not a team file; reuse 02's TOML writer), or make `plan` treat an
  installed guard as wanted until `guard uninstall`.
- **V-026 (10) Tokeniser details [verified V-E8].**
  - "newlines are added to the separator set" needs
    `punctuation_chars='();<>|&\n'` **and** `whitespace=' \t\r'`.
  - Punctuation then merges into tokens like `;\n`, `)\n`, `&&\n` and
    `>&` (from `2>&1`). The splitter must treat any all-punctuation
    token as a separator, and treat `>`/`>&` as redirections, not
    command separators.
  - Backticks are not recursed (`` `git commit` `` slips through).
    `$(git push)` happens to split correctly.
  - A relative `git -C` path must be joined to the `cd` directory, not
    to `cwd`.
- **V-027 (10, 11) Manifest keys.** Claude Code loads `hooks/hooks.json`
  and `.mcp.json` from the plugin root by convention. A manifest
  `hooks` key pointing at the default file is reported to cause a
  "duplicate hooks file" load error [believed]. Default: omit both keys.
  Then 10 and 11 do not edit `plugin.json` at all, and test 15 checks
  only the files.
- **V-028 (10) Two install gaps.**
  - The `command -v gitpro` wrapper is a silent no-op when Claude Code's
    hook `PATH` lacks `~/.local/bin` (pipx). `agent status` should run
    the wrapper's `command -v` with the hook environment it can see, or
    install with an absolute path fallback as 06 does with
    `__GITPRO_BIN__`.
  - A `.claude/settings.local.json` that gitpro **creates** is not
    gitignored. Check `git check-ignore`, and if needed add it to
    `.git/info/exclude`.
- **V-029 (01, 12) `gh auth token -u` exists only in gh 2.40+.** Older
  gh prints "unknown shorthand flag", which 01 turns into "gh has no
  login …". Detect that message and say "gh 2.40 or newer is needed".
- **V-030 (13) The array `initializeCommand` needs `sh` on the host.**
  A Windows colleague's container start fails [believed]. State this
  in the printed hint.
- **V-031 (08) Leftover draft line** in the 08 §2.1 code block (see
  V-011).
- **V-032 (wave plan) Same-wave soft dependencies:** 03→02 and 11→03/08
  in wave 3, 16→06/10 in wave 2. Their `skipif` tests never run in the
  wave where the code is written. Add a final no-skip run (17's job, or
  a CI job that fails on skips once all modules exist).
- **V-033 (14) Action inputs.** The default `range` is `..` on non-PR
  events. Fail with a clear message when either sha is empty. Check
  `git rev-parse --is-shallow-repository` and say "needs fetch-depth: 0"
  instead of a confusing range error.

(V-024 was folded into V-002.)

### 4. Wave and file-ownership check

- **Same file in one wave:** none.
  - Wave 2: 06 render/plan/guard; 10 agents/hooks.json/plugin.json;
    12 envcmd; 15 vscode; 16 doctor/prompt/completion.
  - Wave 3: 02 scan/view/scanroots/roots.ts; 03 lost/lost.ts;
    07 clone/clone.ts; 08 audit/trailers; 09 health;
    11 mcp/.mcp.json/plugin.json.
  - Wave 4: 04 discover; 05 forge; 13 devcontainer; 14 policy/action.yml.
  - `plugin.json` is touched by 01, 10, 11 and 17 in four different waves
    (and by none of them if V-027 is taken).
- **Needed but not provided by an earlier wave:** `cli.CALLER_UMASK`,
  `resolve.EXTRA_CHECKS`/`policy` code, URL-accepting `compare(remote=)`,
  and the hot-parser helpers (V-006, V-007). All are 01's to add now.
- **Soft dependencies inside one wave:** V-032.

### 5. Updated verdict per work package

| package | verdict | remaining |
|---|---|---|
| 01 | **not ready: fix before continuing** | Blocker V-001. Majors V-003, V-004, V-005, V-006, V-007, V-008. Minors V-013 to V-017, V-029. All are spec-level and cheap now. |
| 02 | ready | needs V-005 in 01 |
| 03 | ready after minor fixes | V-019 |
| 04 | ready | - |
| 05 | ready | - |
| 06 | **ready after fixes** | Blocker V-002 (repo mode + docs line). Majors V-009, V-010, and V-004's guard side. Minors V-020 to V-023, V-025. |
| 07 | ready once 01 adds `CALLER_UMASK` | V-007 |
| 08 | **ready after fixes** (audit read-only part ready; `--rewrite` needs rework) | V-011, V-031 |
| 09 | ready | - |
| 10 | ready | V-026, V-027, V-028 (minor) |
| 11 | ready | V-027 (minor) |
| 12 | ready | V-029 (minor) |
| 13 | ready | V-030 (minor) |
| 14 | ready after fixes | V-012 (verify on a runner), V-007 (`EXTRA_CHECKS`), V-033 |
| 15 | ready | - |
| 16 | ready | `guard.status` name (V-007) |
| 17 | ready (last) | copy corrected docs text from 06 (V-002) |

### 6. Experiments (git 2.43.0, temporary HOME, no network)

| # | experiment | result |
|---|---|---|
| V-E1 | `includeIf hasconfig:remote.*.url:<pattern>` vs remotes `git@github.com:o/r.git`, `…:r.git`, `…:g/s/r.git` | `git@github.com:**` → no match for `o/r.git`, match for `r.git`. `git@github-work:**` → same. `git@github.com:*/**` → matches `o/r.git` and `g/s/r.git`, not `r.git`. `https://github.com/**`, `ssh://git@github.com/**`, `git@github.com:o/**` (vs `o/a/b.git`), `**/r.git`, `git@github.com:**/r.git` → match. `git@github.com:o**` vs `o/r.git` → no match. `git@github.com:o/**` matches `…:o/` but not `…:o`. |
| V-E2 | `git config -z --show-scope --show-origin` in the top level, in `-C sub/deep`, in a bare repo, a linked worktree, with `-c` and `GIT_CONFIG_COUNT` | `local\0file:.git/config\0user.email\nX\0` (relative to the **top level**, also from a subdir). Bare: `file:config` (relative to the git dir). Linked worktree and `GIT_DIR`: absolute. `-c`/env: `command\0command line:\0…`. Keys lowercase. |
| V-E3 | pre-commit and commit-msg printing `env | grep ^GIT_` for a plain commit, `--author=Pat`, `--amend`, and with an exported `GIT_COMMITTER_EMAIL` | `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL` exported **every time** (Pat's for `--author`/amend). `GIT_COMMITTER_*` only when the user exported it. `git var GIT_COMMITTER_IDENT` = the local user under `--author`/amend. |
| V-E4 | Revised 06 chain script with a fake gitpro (rc 0, 1, 2, 3, 127), global hooksPath, `chain_to` symlinked to the hooks dir, a repo hook, a linked worktree | rc 0/1/2/127 → commit exit 0 with a warning. rc 3 → commit exit **1**. Repo hook runs. Symlink loop → no recursion. Worktree OK. Repo hook exiting 3 → commit exit 1. |
| V-E5 | 300 tags fetched from a local remote, with and without a `reference-transaction` pass-through stub; 20 commits with stub + pre-commit chain | fetch 0.055 s → 4.18 s. 20 commits 0.088 s → 0.45 s. |
| V-E6 | 08 §2.1 `rebase --no-verify --exec '<case script>' <base>` on (a) one offender authored by Pat with a wrong committer; (b) a chain c2 (offender), c3 (Pat author, wrong committer), c4 (clean), c5 (`notme@personal.test`) | (a) author becomes `Me <me@corp.test>`; Pat lost. (b) c2 fixed with dates kept. c3, c4 and c5 get committer date = now. c5's author is reset because of the substring match. |
| V-E7 | 06 §3.5 managed block as a new `.git/hooks/pre-commit`; the §13 docs line as the last/middle line under `dash -e` and `bash -e`; fake gitpro rc 0/1/3 | New-file block: hook exits 1 for rc 0, 1 and 3, and `git commit` exits 1 with a clean gitpro. pre-push block: exits 1 for rc 0. Docs line as the last line: exit 1 for rc 0. Under `-e` in the middle: rc 1 → exit 1 (fails closed). The `if … rc=$?; if [ "$rc" -eq 3 ]` form: 0/0/1 in every case, and 0 without gitpro. |
| V-E8 | `shlex.shlex(posix=True, punctuation_chars=…)`, `whitespace_split=True` on agent commands | Default whitespace drops `\n` (`git commit -m x\ngit push` becomes one command). With `punctuation_chars='();<>|&\n'`, `whitespace=' \t\r'`: tokens `;\n`, `)\n`, `&&\n`, `>&` appear. `$(git push)` splits into `$ ( git push )`. Backticks stay inside one token. A heredoc inside `"$( … )"` stays one token. |
