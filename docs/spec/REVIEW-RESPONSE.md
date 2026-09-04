# Response to REVIEW.md

One line per finding. "fixed" = the spec now says what the reviewer asked
for (or an equivalent that meets the same goal); "partly" = the goal is met
another way, the difference is stated; "rejected" = not changed, reason
given. Git facts marked [verified] were re-checked with a scratch HOME on
git 2.43.0 (README §14 lists the experiments).

## Blockers

- R-001 fixed — chain script uses `git rev-parse --path-format=absolute --git-common-dir` and `<common>/hooks/<name>` only, never `--git-path hooks`; repo mode reads the local `core.hooksPath` from `--show-scope`; recursion test added (06 §3, test 3) [verified].
- R-002 fixed — hook scripts `exec` gitpro inside `|| exit 0` guards; any exit other than 0 or `guard.BLOCK` (3) prints `exited N; not blocking` and passes; a missing gitpro passes silently; `guard.BLOCK = 3` reserved for block only (06 §3, README §5).
- R-003 fixed — global mode chains: the chain script runs `<repo>/.git/hooks/<name>` after the gitpro entries, so git-lfs and husky-installed repo hooks keep working; repo mode wraps a local `core.hooksPath` (06 §3) [verified: global hooksPath hides repo hooks; the chain restores them].
- R-004 fixed — repo mode never edits tracked or regenerated files: when the repo's `core.hooksPath` points at a tracked dir (`.husky` etc.) gitpro prints the two lines to add by hand and exits 1 (06 §3.3).
- R-005 fixed — the guard judges the **committer**; the author is judged only when it is one of the user's own emails (DECISIONS Q4; 06 §2, 08 §2, 10 §2.1, 11 `check_commit`) [verified: `--author`/amend changes `GIT_AUTHOR_IDENT` only].
- R-006 fixed — no adapter emits `allow` or `additionalContext`; `warn` = stderr only, no decision; test 16 greps every output (10 §2.1, §10).
- R-007 fixed — `resolve.match_url` matches the **raw** remote URL (no normalisation; git's `hasconfig` matches before `insteadOf` rewriting) against `render.url_patterns` with git's wildmatch rules (`*`/`?` do not cross `/`, `**` only as a whole path component, case-sensitive, full match); cases in 01 §4.1 with a differential test against git [verified; `**` rule corrected in V-001].
- R-008 fixed — 01 §2.1 defines `cli.EXTENSIONS` (pre-populated with all 17 modules, `ModuleNotFoundError` for that exact name skipped), `register(sub, common, confirm)`, `extend(subs)`, `extend_command(subs, name, add_flags, handler)` (handler returns `None` → original runs), `args.capability()`, `hot.HOT`; no later package edits `cli.py`.

## Major

- R-009 fixed — `gitpro.hot:main` console script dispatches `guard`, `agent-check`, `prompt` before importing `cli`/`rich`; import-cost rule and `importtime` tests in 06/10/16 (01 §5.2).
- R-010 fixed — `config.load` schema spelled out: frozen dataclasses with `from __future__ import annotations`, nested tables (`[identity.trailers]`, `[guard]`, `[agents]`, `[policy]`, `[scan]`) and unknown-key errors per table (01 §3).
- R-011 fixed — `effective()` parses `git config --show-scope --show-origin -z`, maps scope names and `file:`/`command line:`/`standard input:` origins; test fixtures from real output (01 §4.1) [verified format `scope\tfile:/abs\tkey\nvalue`].
- R-012 fixed — each `compare` problem code has one rule, one message and one `fix_argv`, listed in a table (01 §4.4).
- R-013 fixed — hook scripts take `chain_to` and the gitpro path through `shlex.quote`d constants validated against `PATH_RE`; no interpolation of user text (06 §3.1).
- R-014 fixed — one `undo.write(command, lines, *, what)` writer, header format fixed, 0600; `scan --fix` keeps its current script content byte-for-byte except the header (01 §5.3, 00 item 14).
- R-015 fixed — `scan` runs without a config (`[scan]` table optional, defaults unchanged), regression test in 02 (02 §2).
- R-016 fixed — `scan.effective_roots(cfg, cli_roots)` owned by 02; 03/04/11 call it through a try-import and fall back to the old defaults (02 §4, README §9).
- R-017 partly — no "Save to settings": the picker edits the TOML `[scan] roots` through 02's CLI (`scan --roots --write`) so there is one source of truth; the VS Code setting is gone (02 §5).
- R-018 fixed — `lost` kinds table rewritten with the exact git commands, empty-repo and detached-HEAD rows, and the "unpushed vs no upstream" distinction; impossible detail removed (03 §2).
- R-019 fixed — `forge.repo_exists` returns a four-state tuple `("ok"|"renamed"|"missing"|"unknown", detail)`; `missing` only on gh's `Could not resolve to a Repository`, anything else is `unknown`, and `lost` reports `unknown` instead of `gone` (01 §5.5, 03 §2).
- R-020 fixed — shell history is opt-in (`--history`, DECISIONS Q3), lines are never echoed, only host/user/alias tokens are kept, and the secret-looking token filter is specified with tests (04 §2.4).
- R-021 fixed — 04 depends on 02's `scan.find(max_depth)` and 01's `forge.accounts`; tests rewritten against `fake_bin` and `cfg()` (04 §10).
- R-022 fixed — `forge.gh_env(host, login)` sets `GH_TOKEN` for github.com and `GH_ENTERPRISE_TOKEN` for other hosts and never switches the active account; `glab` one login per host (DECISIONS Q11; 01 §5.5, 05 §2, 12 §2).
- R-023 fixed — `forge similar` kept (DECISIONS Q2) but needs `--online --send-repo-names` on every run and prints what it will send first (05 §2.3).
- R-024 fixed — the guard writes `core.hooksPath` to its own managed file `~/.gitconfig-gitpro-guard`, included from the end of the generated section of `~/.gitconfig-identities`; `plan.build` includes the hook targets when `[guard] hooks = true` or when that file exists, so `apply` after `guard install` keeps the hooks (06 §3, §3.1; V-025).
- R-025 fixed — hook scripts start with the `# Generated by gitpro` marker on line 2 and `render.is_generated` is extended to accept it after a shebang; uninstall removes them (06 §6, 00 item 15).
- R-026 fixed — a system-level `core.hooksPath` is detected via `--show-scope` and the guard refuses with a message instead of overriding it (06 §7).
- R-027 fixed — the other 06 gaps (worktrees, bare repos, `--no-verify`, `GITPRO_SKIP`, non-UTF-8 messages, pushes of deletes) each have a row in 06 §7 and a test.
- R-028 fixed — `guard.HOOKS` is a fixed table written once in 01 §5.6 naming every entry (`gitpro.guard:*`, `gitpro.policy:*`, `gitpro.trailers:*`); entries are imported lazily and a missing module is skipped; 08/14 edit nothing in `guard.py`.
- R-029 fixed — waves rebuilt from the real dependencies and Q9 order (README §9): 01; 06,10,12,15,16; 02,03,07,08,09,11; 04,05,13,14; 17.
- R-030 fixed — README §9 lists for every package the files it creates and the existing files it may edit; shared modules (`forge.py`, `scan.py`, `render.py`, `plan.py`, VS Code `package.json`/`extension.ts`) have one owner each.
- R-031 fixed — `clone` keeps the user's scheme unless the identity sets `rewrite`, reports `auth` in JSON, and restores the caller's umask (`cli.CALLER_UMASK`) for the clone so files are not 0600 (07 §2, 00 item 16).
- R-032 fixed — `audit --rewrite` replays only the unpushed commits from the merge-base with `git commit-tree`, copying author/committer/date of untouched commits byte for byte and replacing only exact-matching offending emails; refuses on dirty tree, detached HEAD, merges, pushed offenders; one `update-ref` at the end, undo via the old sha (08 §2.1; rewritten in V-011).
- R-033 fixed — `auth` greeting table per forge, BatchMode, `locked`/`unknown_host`/`timeout` states; `keys --health` passphrase check via `ssh-keygen -y -P ""` with DEVNULL stdin and timeout; registered-key check handles older `gh` (09 §2).
- R-034 fixed — tokeniser with `punctuation_chars`, recursion into `sh -c`, `cd &&`, env prefixes, conservative regex fallback on parse failure; the deny reason says "Ask the user before changing anything" and names the fix, and `GITPRO_SKIP` is described as the **user's** bypass (10 §2.1).
- R-035 fixed — plugin root is the repo root: `hooks/hooks.json` and `.mcp.json` live there; the hook command is `sh -c 'command -v gitpro >/dev/null 2>&1 || exit 0; exec gitpro agent-check --agent claude'` so plugin users without the CLI see no error (10 §2.2, 11 §2.2, README §12).
- R-036 fixed — every JSON edit goes through `jsonfile.edit_json` (backup, diff, confirm, re-read before replace, undo); tracked team files need `--write` (DECISIONS Q5); project scope for Claude uses `settings.local.json`; `claude mcp add` preferred over editing `~/.claude.json` (10 §2.2, 11 §2.2, 13 §2.2).
- R-037 fixed — protocol version negotiation, no replies to notifications, `structuredContent` gated by version, stdout redirect for tools, `-32700/-32600/-32601/-32602` paths, fail-safe per call, EOF exit (11 §2.1, §7).
- R-038 fixed — `GIT_SSH_COMMAND` is opt-in (`--only ssh`) with a warning comment; `glab_account` supported with a `glab auth token` probe; `--unset` mirrors what was set (12 §2).
- R-039 fixed — devcontainer is print-only by default; `--write` warns when tracked, needs confirmation, never creates the file; the array form `command -v gitpro … || true` cannot break container start for others (13 §2, DECISIONS Q8).
- R-040 fixed — `%G?` table with `E`/`R`/`B`/`N` handling and `allowed_signers` shipped with the policy; Action installs from `$GITHUB_ACTION_PATH`, inputs pass through `env:` not `${{ }}` in `run:` (14 §3.1, §3.4).
- R-041 fixed — prompt cache keyed on stat of every input file (`.git/config`, `config.worktree`, TOML, `~/.gitconfig`, every included gitconfig, pin file) plus an env hash; bypassed under `GIT_CONFIG_*` overrides; tests 12–13 (16 §2.2).
- R-042 fixed — the quick action runs the exact `fix_argv` from `why --json`; 15 owns `extension.ts`/`package.json`/`features.ts` and the stubs, so no other package edits them (15 §2, §4).

## Minor

- R-043 fixed — 01 §2.3 says `--version` exists and only gains `--json` (`{"version", "capabilities"}`).
- R-044 fixed — JSON keys use git's lowercase canonical form everywhere (`core.sshcommand`); text output too (01 §2.1).
- R-045 fixed — documented: non-POSIX refusal happens before parsing (text only); root refusal after parsing honours `--json` (01 §5).
- R-046 fixed — fakes are Python scripts with a `sys.executable` shebang logging `shlex.join(argv)` plus separate `env:GH_TOKEN=set|unset` and `stdin=` fields (01 §7).
- R-047 fixed — `repo_with_remote(commits=, author=, committer=)` and `commit()` pass explicit `GIT_AUTHOR_*`/`GIT_COMMITTER_*` (01 §7).
- R-048 fixed — `_build`'s existing `unknown key(s) … (allowed: …)` format wins; test 7 updated (01 §12).
- R-049 fixed — Python 3.9 rules listed in README §11 and 01 §6 (future annotations, no `match`, no `zip(strict)`, no `slots`/`kw_only`, no `datetime.UTC`, no runtime `X | Y`, `tomli` fallback); CI matrix keeps 3.9.
- R-050 fixed — `exclude` patterns match the absolute path after `~` expansion with `fnmatch`; examples given (02 §2).
- R-051 fixed — empty-repo and detached-HEAD rows added to the kinds table (03 §2).
- R-052 fixed — `discover` follows ssh `Include` with glob expansion, read-only, depth-limited; `Match` blocks are reported but not evaluated (04 §2.2).
- R-053 partly — kept exit 2 for "`--online` required but gh/glab missing" and applied it uniformly through `forge.no_online_error` in 04/05/09/14; README §5 now defines 2 as "cannot run (config, runtime or missing tool)"; 1 stays "ran and found problems". A usage error would hide the "install gh" hint behind argparse's exit code.
- R-054 fixed — VS Code clone uses `withProgress` (cancellable), `GIT_TERMINAL_PROMPT=0`, and on auth failure offers a terminal (07 §5).
- R-055 fixed — editing notes removed; 09 touches no existing module (`keys` flags via `extend_command`); README §9 agrees.
- R-056 fixed — `--title` added; the key comment is shown in the confirmation (09 §2.3).
- R-057 fixed — README §13 "Open questions" lists Cursor/Gemini field names, plugin manifest keys, `additionalContext`, each with the fallback the package uses.
- R-058 fixed — `edit_json` keeps key order, `ensure_ascii=False`, trailing newline, detected indent, and re-reads before replacing (01 §5.4 steps 3 and 6).
- R-059 fixed — the entry is recognised by `command` **containing** `gitpro agent-check` (works with the `sh -c` wrapper and absolute paths); no unknown key added (10 §2.2).
- R-060 fixed — noted in 11 §8.
- R-061 fixed — fish `set -e -g`, pwsh `Remove-Item Env:… -ErrorAction SilentlyContinue` (12 §2).
- R-062 fixed — `devcontainer.in_container()` is the one Python function; the extension carries the same constant list and a test pins both (13 §2.3, 15 §9 test 6).
- R-063 fixed — policies cached by `(path, mtime_ns, size)`; hooks return 0 immediately when no policy applies (14 §2, test 14).
- R-064 fixed — `createFileSystemWatcher(new RelativePattern(dir, base))` (15 §3).
- R-065 fixed — `add-zsh-hook precmd` (16 §2.2).
- R-066 fixed — completion generated at runtime from `build_parser()` (16 §2.3).
- R-067 fixed — values per option: `--identity`, `--only` (env keys), `--shell`, `--agent`, `--values kinds|hooks|identities` (16 §2.3).
- R-068 fixed — explicit list of writing subcommands in `test_docs.py` (17 §5).
- R-069 fixed — 00 gained the undo header/mode (item 14), `scan.suggest` id rules (item 15) and the umask note (item 16).

## Verification pass

One line per item of REVIEW.md "Verification pass". Package 01 changes were also sent to the 01 implementer by the orchestrator; the spec now matches those decisions.

- V-001 fixed — `**` matches only as a whole path component; `render.url_patterns` is to emit `*/**`-style patterns and 00 item 17 records the current bug; test 13 has a differential matrix against `git config --get-urlmatch`-style wildmatch (01 §4.1, §10).
- V-002 fixed — every hook block, the chain scripts and the docs one-liner use `rc=0; gitpro guard check … "$@" || rc=$?; if [ "$rc" -eq 3 ]; then exit 1; fi` (06 §3.2, §3.5, §13); tests 9/9a check the static form and that `git commit` exits 1.
- V-003 fixed — relative `file:` origins are resolved against `--show-toplevel` (bare: git dir), not cwd (01 §4.2).
- V-004 fixed — `resolve.effective(repo, env=)`/`compare(repo, cfg, remote=, env=)`; the guard passes the hook environment minus `GIT_AUTHOR_NAME/EMAIL/DATE` so git's own exports are not reported as `env_override` (01 §4.4, 06 §3.4, 08, 14).
- V-005 fixed — `_extension_modules` yields modules with `register` or `extend`; `scanroots` (02) is extend-only (01 §2.1, test 26).
- V-006 fixed — hot modules expose `add_arguments(p)`; `register` = `add_arguments(sub.add_parser(name, parents=[common, confirm]))`; `main` builds `ArgumentParser(prog="gitpro <name>", parents=[args.common()])` (01 §2.3, 06 §3).
- V-007 fixed — missing interfaces defined: stdlib `gitpro/args.py` (`CAPABILITIES`, `capability()`, `common()`, `confirm()`), `cli.CALLER_UMASK`/`hot.CALLER_UMASK`, `resolve.EXTRA_CHECKS`, `guard.status() -> dict`, `undo.write` slug rule, `edit_json` signature (01 §2, §5.7; README modules table).
- V-008 fixed — `key_mismatch` expands `~` in `-i`, compares resolved paths, and `Expected.ssh_key` is `None` for `[default]` without a key (01 §4.4, test 18a–c).
- V-009 fixed — stubs: 18 pass-through names plus 3 chain hooks (`pre-merge-commit` added); `reference-transaction`, `post-index-change`, `fsmonitor-watchman`, `p4-*` are not forwarded, with the reason written in 06 §3.1; counts and `guard status` output corrected.
- V-010 fixed — pre-push judges only commits whose committer (or author) email is one of the user's identities' emails (`own_emails(cfg)`); colleagues' commits that arrived without remote-tracking refs pass (06 §2, §3.4, test 12).
- V-011 fixed — `--rewrite` is a `git commit-tree` replay, oldest first, authors/dates of untouched commits preserved, exact email matching, `update-ref` with old-value check; no `rebase`, no `--reset-author` (08 §2.1, test 6; 06 fix line).
- V-012 fixed — the Action installs into `python3 -m venv "$RUNNER_TEMP/gitpro"` from `$GITHUB_ACTION_PATH` and calls the venv binary (PEP 668 on Ubuntu 24.04) (14 §3, test 13).
- V-013 fixed — tests 29/30 use `build_parser()` and exclude `cli`/`args` from the import scan (01 §10).
- V-014 fixed — `undo.write` slug = subcommand words joined with `-` (`guard install --repo` → `guard-install-repo-undo-<ts>.sh`) (01 §5.7).
- V-015 fixed — `edit_json` backups go to `<undo.state_dir()>/backup-<ts>/<basename>`; no `mkdir=` parameter; 10/13 text and examples updated (01 §5.7, 10 §2.3, 13 §3, README rule 2).
- V-016 fixed — `hot.main` skips only `ModuleNotFoundError` with `e.name == name`; other import errors propagate (01 §2.3, test 27).
- V-017 fixed — root `--json` has `dest="version_json"` so it never collides with subcommand `--json` (01 §2.1, test 27).
- V-018 fixed — this file: R-007, R-008, R-019, R-024, R-032 lines corrected to match the package files; this section added.
- V-019 fixed — 03 uses `gitpro.scan.effective_roots` (02 extends `scan`); `_roots` passes `cfg.scan if cfg else None`; test 16 patches the right name (03 §2, §10).
- V-020 fixed — `guard install --repo` refuses hook dirs that are tracked, git-ignored, or end in `/_/` (husky), with the pre-commit framework/lefthook notes (06 §3.5, test 16).
- V-021 fixed — spec and tests say `git commit` exits 1 on a block (git's rule), while `gitpro guard check` itself exits 3 (06 §2, test 9).
- V-022 fixed — with a global `core.hooksPath` git LFS's own hook files are not run, so the four LFS stubs and the `pre-push` chain script call `git lfs <name> "$@"` when `filter.lfs.process` is set and LFS is not already reached through the repo chain (runs once); the tool rows in 06 §8 say who runs first (06 §3.3, test 13).
- V-023 fixed — `^<remote sha>` is added to the pre-push range only when `git cat-file -e` succeeds; otherwise the range is `--not --remotes` (06 §3.4).
- V-024 folded into V-002 by the reviewer.
- V-025 fixed — `plan.build` includes the hook targets when `[guard] hooks = true` or `~/.gitconfig-gitpro-guard` exists, so `apply` keeps a `guard install` (06 §3, README).
- V-026 fixed — tokeniser is `shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")` with `whitespace=" \t\r"`, separators are all-punctuation tokens, redirections skipped, backticks/`$(…)` recursed to depth 4, relative `-C` joined to the `cd` dir (10 §2.1).
- V-027 fixed — 10/11 ship `hooks/hooks.json` and `.mcp.json` at the plugin root and do not edit `plugin.json`; 17 checks the plugin docs at release and adds manifest keys only if needed (10 §2.2, 11 §2, 17 §3, README §13).
- V-028 fixed — `agent status` compares `sh -lc` and `sh -c` `command -v gitpro`; install writes an absolute-path fallback; project-scope `settings.local.json` is added to `.git/info/exclude` when not ignored (10 §2.3, tests 15a–c).
- V-029 fixed — `gh auth token -u` needs gh 2.40; the message `✗ gh 2.40 or newer is needed for multiple accounts (found: …)` is specified in 01 §5.5, referenced in 12 §1 and tested in 12 test 13.
- V-030 fixed — the printed `initializeCommand` snippet, the `Show` view and `--help` carry the note that the array form needs `sh` on the host (Linux, macOS, WSL) and that a Windows colleague can remove the line (13 §5).
- V-031 fixed — the draft "rewrite via rebase" line in 08 was removed with the §2.1 rewrite.
- V-032 fixed — README §9 wave 5 and 17 §3 add a final run of the whole suite with `-rs` and no skips except platform ones.
- V-033 fixed — the Action fails fast on an empty range (`..*`, `*..`, `""`) and on a shallow clone with a message naming `fetch-depth: 0` (14 §3, test 13a).
