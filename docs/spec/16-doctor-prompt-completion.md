# 16 — `doctor`, `prompt`, `completion`

Wave 2. Depends on 01 only (`resolve`, `check --json` logic, `hot`,
`build_parser`, `lint`). Optional, via try-import at run time: 06, 09, 10,
11, 14 for extra doctor sections.

Files this package may create or edit: new `src/gitpro/doctor.py`,
`prompt.py`, `completion.py`, `tests/test_doctor.py`, `tests/test_prompt.py`,
`tests/test_completion.py`. Nothing else (the VS Code `gitpro.doctor`
command is 15's).

## 1. Purpose and user story

- **`gitpro doctor`**: one command that runs every check gitpro has and
  prints a short, ordered list of what to fix.
- **`gitpro prompt`**: a shell prompt segment (`✓ work-github` /
  `✗ old@personal.test`) cheap enough to run on every prompt.
- **`gitpro completion bash|zsh|fish`**: tab completion for subcommands,
  flags, identity ids and option values.

User story: "A colleague says gitpro 'does nothing' on her machine.
`gitpro doctor` shows: git 2.34 is too old, `~/.gitconfig` sets
`user.email` after the include, and `id_work` has no passphrase. Three
lines, three fixes."

## 2. CLI surface

### 2.1 `gitpro doctor [DIR] [-c PATH] [--online] [--json] [-v]`

```
gitpro 0.2.0  python 3.12.3  git 2.43.0  linux
config    ~/.config/gitpro.toml (user config)  3 identities        ✓
applied   up to date                                               ✓
files     ~/.gitconfig block last ✓  ~/.ssh/config block first ✓   ✓
lint      ~/.gitconfig also sets user.email directly               ⚠ → remove it from ~/.gitconfig
keys      3 keys · 1 without passphrase                            ⚠ → gitpro keys --health
ssh       ~/.ssh 0700 ✓  config.d/git-identities 0600 ✓           ✓
guard     installed, block                                         ✓
agents    claude hook installed · mcp registered                   ✓
repo      ~/work/app → work-github                                 ✗ user.email set in .git/config → git -C ~/work/app config --unset user.email
policy    Example Corp engineering: signing required, sign=false   ✗ → set sign = true
env       GIT_AUTHOR_EMAIL is set in this shell                    ⚠ → unset GIT_AUTHOR_EMAIL
2 problems, 3 warnings → fix the ✗ lines first
```

`doctor.SECTIONS` is a list of `(name, fn, needs)` where `needs` is the
module name to try-import (`None` for built-ins). Each `fn(ctx) ->
Section` is wrapped: an exception becomes `⚠ <section>: check failed
(<error>)` and the rest continues; a missing optional module → `·
<section>: not installed (optional)`.

| section | source | needs |
|---|---|---|
| versions | `git --version` ≥ 2.36 (`✗` + install hint, as `_load`), `ssh -V`, python | — |
| config | `config.find` + `load_all`; errors are `✗` with the ConfigError text | — |
| applied | `plan.build` vs disk (`check` logic) | — |
| files | block positions (gitpro block last in `~/.gitconfig`, first in `~/.ssh/config`), generated files present | — |
| lint | `lint.warnings` | — |
| keys | `health.check_keys(online=False)` summary; `--online` adds registered | `gitpro.health` (09) |
| ssh | `~/.ssh` mode, `config.d` file mode, `IdentitiesOnly`, `known_hosts` has the hosts (`ssh-keygen -F`), old tool files | — |
| guard | `guard.status()` | `gitpro.guard` (06) |
| agents | `agents.status()` + `mcp` registration | `gitpro.agents`, `gitpro.mcp` (10, 11) |
| repo | `resolve.compare(DIR or cwd)` when inside a repo | — |
| policy | `policy.check_machine` | `gitpro.policy` (14) |
| env | `GIT_AUTHOR_*`, `GIT_COMMITTER_*`, `GIT_SSH_COMMAND`, `GIT_CONFIG_GLOBAL`, `GH_TOKEN`/`GH_ENTERPRISE_TOKEN`/`GITLAB_TOKEN` **set or not** (never values); `core.sshCommand` in `~/.gitconfig` outside the block | — |
| forge | `--online`: `health.auth` summary | `gitpro.health` |

`--online` without gh/glab and ssh → the keys/forge sections say `⚠
needs gh or glab`; doctor itself does not exit 2 (it is a report). Exit 1
when any `✗`, else 0. `-v` prints detail lines.

### 2.2 `gitpro prompt [DIR] [--format FMT] [--max-ms N] [--install-help]`

Hot path (`hot.HOT`): `prompt.main(argv)` with module-level imports
stdlib + `gitpro.config` + `gitpro.resolve` only. Prints one line and
exits 0 always. Empty output outside a repo. Formats: `{id}`, `{email}`,
`{key}`, `{state}` (`ok`|`mismatch`|`none`), `{sym}` (`✓`/`✗`/`⚠`);
default `"{sym} {id}"` → `✓ work-github`, `✗ old@personal.test` on
mismatch (effective email, so the user sees what git would use), `⚠ no
identity`.

Cache: `~/.local/state/gitpro/prompt-cache.json` (0600), keyed by the
repo's common dir. Each entry stores the result plus a **key**: the
`(path, size, mtime_ns)` of every input that can change the answer —
`.git/config`, the worktree's `config.worktree` if present, the TOML
config, `~/.gitconfig`, `~/.gitconfig-identities`, every
`~/.gitconfig-<id>` the identities include, the pin file
`.git/git-identities` — plus a hash of the env variables that affect
identity (`GIT_AUTHOR_*`, `GIT_COMMITTER_*`, `GIT_CONFIG_GLOBAL`,
`GIT_CONFIG_*`, `HOME`). A hit = every stat matches; then no git call. The
cache is **bypassed** (not written either) when any `GIT_CONFIG_COUNT`,
`GIT_CONFIG_GLOBAL` or `GIT_CONFIG_SYSTEM` is set, since those make the
answer depend on things gitpro cannot stat cheaply.

Budget: `--max-ms 80` default. Over budget (measured around the git calls,
`time.monotonic`) → print the stale cached value if any, else nothing;
the computation is not aborted mid-way (no threads), it just stops
printing after the budget: the result of the slow run is still cached so
the next prompt is fast. Any exception → empty output, exit 0.

`--install-help` prints (never edits rc files):

```
bash:     PS1='$(gitpro prompt) '"$PS1"
zsh:      autoload -Uz add-zsh-hook; _gitpro_prompt() { psvar[1]=$(gitpro prompt); }; add-zsh-hook precmd _gitpro_prompt; PS1='%1v '"$PS1"
fish:     function fish_prompt; gitpro prompt; echo -n ' '; prompt_pwd; echo -n ' > '; end
starship: [custom.gitpro] command = "gitpro prompt" when = "git rev-parse --git-dir" shell = ["sh"]
```

### 2.3 `gitpro completion bash|zsh|fish [--values identities|kinds|hooks|shells]`

Generated **at run time** from `cli.build_parser()`: `completion.generate(parser,
shell)` walks the subparsers (including those added by `extend_command`)
so every installed package's commands and flags appear. Option values:
for each option with `choices`, the choices; for `--identity`, `--only`,
`<hook>` positionals, a dynamic call `gitpro completion --values
identities|kinds|hooks` (reads the config, prints one per line, fails
silently to nothing; `kinds` are 03's lost kinds when importable, else
empty). Install line on stderr: `# add to ~/.bashrc: eval "$(gitpro
completion bash)"`.

Capabilities registered: `doctor`, `prompt`, `completion`.

## 3. `--json`

`doctor --json`:

```json
{"ok": false, "command": "doctor", "versions": {"gitpro": "0.2.0", "python": "3.12.3", "git": "2.43.0", "os": "linux"},
 "sections": [{"name": "repo", "status": "ok"|"warning"|"error"|"skipped"|"missing", "summary": "~/work/app → work-github", "problems": [{"message": "...", "fix": "..."}], "details": []}],
 "summary": {"problems": 2, "warnings": 3}}
```

`prompt` has no `--json`. `completion` prints scripts.

## 4. Config keys

None.

## 5. VS Code surface

`gitpro.doctor` is declared and implemented by 15 (`features.ts`: runs
`doctor` into the log and shows it); nothing here.

## 6. Files created/changed

`prompt` writes `~/.local/state/gitpro/prompt-cache.json` (0600, atomic
write; safe to delete; `doctor -v` mentions it). Nothing else.

## 7. Failure modes

| situation | behaviour |
|---|---|
| no config | doctor: `config ✗ no config found … → gitpro init`; sections needing the config are `· skipped`; exit 1 |
| a section throws | `⚠ <section>: check failed (<error>)`; `-v` adds the last traceback line |
| prompt in a broken repo / any error | empty output, exit 0 |
| prompt cache unreadable or wrong shape | ignored, recomputed, rewritten |
| `completion` for an unknown shell | `✗ unknown shell 'x' (bash, zsh, fish)`, exit 2 |

## 8. Security / privacy

`doctor` reports whether `GH_TOKEN` etc. are set, never values. `--online`
is the only network path and reuses 09's announcement. The prompt cache
stores repo paths, identity ids and emails (no secrets), mode 0600.

## 9. Out of scope

Auto-fixing from doctor (it prints commands), powerline modules, Windows
shells.

## 10. Acceptance tests (`tests/test_doctor.py`, `tests/test_prompt.py`, `tests/test_completion.py`)

doctor:

1. Fresh home, no config → `config ✗` with `gitpro init`, exit 1, JSON
   `status == "error"`; later sections `skipped`.
2. `cfg()` applied → `config ✓`, `applied up to date ✓`, `files ✓`, exit 0;
   optional sections whose module is absent (monkeypatch
   `sys.modules["gitpro.guard"] = None`) print `· guard: not installed
   (optional)` and `status == "missing"`.
3. Change the config without apply → `applied ✗ 1 pending → gitpro apply`.
4. `~/.gitconfig` with `user.email` set outside the block → `lint ⚠`.
5. Block moved to the top of `~/.gitconfig` → `files ✗ … not last → gitpro
   apply`.
6. `GIT_AUTHOR_EMAIL` set → `env ⚠ → unset GIT_AUTHOR_EMAIL`; `GH_TOKEN=SECRET`
   → `GH_TOKEN is set` and `SECRET` nowhere in stdout or JSON.
7. In a repo with an override → `repo ✗` with the unset command.
8. A section that raises: monkeypatch the entry in `doctor.SECTIONS` for
   `ssh` to a function raising `RuntimeError("boom")` → `⚠ ssh: check
   failed (boom)`, other sections still printed, exit from the rest.
9. git too old (fake `git` on `fake_bin` printing `git version 2.30.0`
   for `--version`, exec-ing the real git otherwise) → `versions ✗` with
   the install hint.

prompt:

10. Matching repo → `✓ work-github`; override → `✗ old@personal.test`; no
    identity → `⚠ no identity`; outside a repo → empty; exit 0 always.
11. `--format "{id}|{state}|{key}"` → `work-github|ok|id_work`.
12. Cache: second call makes zero `git` calls (monkeypatched
    `subprocess.run` counter); after `git config user.email x` the result
    updates (mtime/size changed); after editing the TOML (`cfg(text)`
    with another email) likewise; after `pin` likewise.
13. `GIT_CONFIG_GLOBAL` set → the cache file is not written and git is
    called each time.
14. `--max-ms 0` with a cold cache → empty output, exit 0; second call
    (now cached) → the value.
15. `--install-help` prints the four snippets; `zsh` line contains
    `add-zsh-hook precmd`.
16. `python -X importtime -c "import gitpro.prompt"` shows no `rich`,
    `gitpro.cli`, `gitpro.view`.
17. Cache file with garbage → ignored, result still correct, file
    rewritten valid, mode 0600.

completion:

18. `completion bash` output contains every subcommand name from
    `build_parser()` after extensions (walk it in the test) and passes
    `bash -n`; `zsh` output starts with `#compdef gitpro` and passes `zsh
    -n` when zsh is on PATH; `fish` output contains `complete -c gitpro`.
19. Options with `choices` (`--shell`, `--agent`, `--format`) appear with
    their values in the bash script.
20. `completion --values identities` → ids one per line; no config →
    empty, exit 0; `--values kinds` → 03's kinds when importable else
    empty; `--values hooks` → `pre-commit pre-push commit-msg`.
21. Unknown shell → exit 2.

## 11. Modules touched

New `doctor.py` (`SECTIONS`, `run(cfg_path, dir, online) -> dict`,
`register`), `prompt.py` (`segment(dir, fmt, max_ms) -> str`, `cache_key`,
`main(argv)`, `register`), `completion.py` (`generate(parser, shell)`,
`values(kind)`, `register`).

## 12. Docs text (for 17)

> `gitpro doctor` runs every check (git version, config, applied state,
> file order, lint, keys, ssh, guard, agent hooks, the current repo,
> policy, environment) and lists what to fix. `gitpro prompt` prints
> `✓ work-github` or `✗ wrong@email` for your shell prompt in under 80 ms
> (`gitpro prompt --install-help` shows the shell lines). `eval "$(gitpro
> completion bash)"` (or `zsh`, `fish`) adds tab completion.
