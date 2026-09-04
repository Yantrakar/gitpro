# 12 — `gitpro env`: forge token and ssh commands for a repo

Wave 2. Depends on 01 only (`resolve`, `gh_account`/`glab_account`,
`jsonout`, `forge.gh_env` for the variable-name rule).

Files this package may create or edit: new `src/gitpro/envcmd.py`,
`tests/test_envcmd.py`. Nothing else (the VS Code command `gitpro.copyEnv`
is declared and implemented by 15, calling `env --shell sh`).

## 1. Purpose and user story

`gh`, `glab`, scripts and agents need the right account for the repo they
are in. `gh` has one active account per host; switching it globally is
wrong when two terminals work on two accounts. `gitpro env` prints shell
lines that set `GH_TOKEN`/`GH_ENTERPRISE_TOKEN`/`GITLAB_TOKEN` and, on
request, `GIT_SSH_COMMAND` and author vars — as **commands that fetch the
token**, never the token.

User story: "`eval "$(gitpro env)"` in `~/work/app` sets `GH_TOKEN` from
`gh auth token -u me-work` (gh ≥ 2.40; an older gh answers `unknown
shorthand flag` and gitpro says `✗ gh 2.40 or newer is needed for
multiple accounts`, 01 §5.5), so `gh pr create` uses my work account. With
direnv it happens on `cd`."

## 2. CLI surface

```
gitpro env [DIR] [--shell sh|fish|pwsh] [--only KEY[,KEY…]] [--unset] [-c PATH] [--json]
```

Output for `sh` (default; `bash`/`zsh` are `sh`):

```sh
# gitpro env for ~/work/app: identity work-github <me@corp.test> (origin git@github.com:corp-org/app.git)
export GH_HOST='github.com'
export GH_TOKEN="$(gh auth token -u 'me-work' -h 'github.com')"
# usage: eval "$(gitpro env)"      undo: eval "$(gitpro env --unset)"
# direnv: echo 'eval "$(gitpro env)"' >> .envrc && direnv allow
```

Rules:

- Comment lines go first and last; every line is valid `sh`.
- Keys: `gh`, `glab`, `ssh`, `author`. Default `--only gh,glab`.
- `gh`: only when the identity has `gh_account`. Variable: `GH_TOKEN`
  for `github.com`, `GH_ENTERPRISE_TOKEN` for any other host
  (`forge.gh_env` rule — gh ignores `GH_TOKEN` for GHES). `GH_HOST` is
  always set alongside.
- `glab`: `GITLAB_HOST` + `GITLAB_TOKEN="$(glab auth token -h '<host>')"`
  when `glab_account` is set and `glab auth token --help` exits 0 (probe
  only when `glab` is on PATH, 10 s timeout, `stdin=DEVNULL`); older
  glab → comment `# glab auth token not available in your glab; set
  GITLAB_TOKEN yourself`. When `GH_TOKEN`/`GITLAB_TOKEN` is already set
  in gitpro's environment, the comment `# <VAR> is already set in this
  shell; the line below replaces it` is printed (gitpro notes the
  presence only, never the value).
- `ssh` (**opt-in**: `--only ssh` or `--only gh,ssh`): `export
  GIT_SSH_COMMAND='ssh -i ~/.ssh/id_work -o IdentitiesOnly=yes'`. Not in
  the default set: with an alias, git's `core.sshcommand` already picks the
  key, and `GIT_SSH_COMMAND` in the environment overrides **every** repo's
  config, including other identities' repos in the same shell. The
  comment on the line says so: `# overrides core.sshCommand for every repo
  in this shell; unset when you cd elsewhere`.
- `author`: printed only with `--only author` (or included in `--only`
  list), or by default when the repo has an `override`/`env_override`
  problem (with the comment `# repo .git/config overrides the identity;
  these env vars win over it`).
- `--unset` prints, for the same keys the call would set: sh `unset
  GH_HOST GH_TOKEN`; fish one `set -e -g VAR` per variable; pwsh
  `Remove-Item Env:GH_HOST, Env:GH_TOKEN -ErrorAction SilentlyContinue`.
- `fish`: `set -gx GH_TOKEN (gh auth token -u 'me-work' -h 'github.com')`;
  `pwsh`: `$env:GH_TOKEN = (gh auth token -u 'me-work' -h 'github.com')`.
- No identity match and empty default: `# gitpro env: no identity for
  gitlab.com/someone; nothing to set`, exit 1 (so `eval` is a no-op).
- Not a repo: `# gitpro env: <dir> is not a git repository`, exit 2 — a
  comment, so `eval` stays safe. No config: comment, exit 2.
- Problems from `resolve.compare` are appended as `# ⚠ <message>` lines.

Capabilities registered: `env`.

## 3. `--json`

```json
{"ok": true, "command": "env", "repo": "/home/me/work/app",
 "identity": {"id": "work-github", "name": "Work Me", "email": "me@corp.test"},
 "forge": {"host": "github.com", "cli": "gh", "account": "me-work", "token_var": "GH_TOKEN"},
 "ssh_key": "/home/me/.ssh/id_work",
 "env": {"GH_HOST": "github.com"},
 "env_commands": {"GH_TOKEN": "gh auth token -u me-work -h github.com"},
 "lines": ["export GH_HOST='github.com'", "export GH_TOKEN=\"$(gh auth token -u 'me-work' -h 'github.com')\""],
 "problems": []}
```

`env` holds values gitpro knows (never tokens); `env_commands` the
commands that produce the values gitpro refuses to know.

## 4. Config keys

`gh_account`, `glab_account` (identity/default), `ssh_key`, `ssh_alias`.

## 5. VS Code surface

`gitpro.copyEnv` (owned by 15) copies the `sh` lines to the clipboard.
`terminal.integrated.env` is out of scope (static per workspace, leaks
across repos).

## 6. Files created/changed

None. Prints only.

## 7. Failure modes

| situation | output | exit |
|---|---|---|
| not a repo | comment line | 2 |
| no config | `# gitpro env: no config found (...)` | 2 |
| no identity | comment, nothing set | 1 |
| `--shell csh` | `✗ unknown shell 'csh' (sh, fish, pwsh)` on stderr | 2 |
| `--only foo` | `✗ unknown key 'foo' (gh, glab, ssh, author)` | 2 |
| identity without accounts | `# gitpro env: identity x has no gh_account/glab_account; nothing to set` | 0 |

## 8. Security / privacy

- gitpro never runs `gh auth token` here and never sees a token; the
  user's shell runs the printed command.
- Values are `shlex.quote`d; the token command uses double quotes around
  `$(…)` with the account quoted inside; the account and host are
  validated against `[A-Za-z0-9._-]+` before printing (a crafted
  `gh_account` can not inject into the command substitution).
- `--json` contains no secret values.
- **No `--values` flag** (DECISIONS; task question). A token on stdout
  lands in scrollback, logs and agent transcripts; the command form costs
  nothing and keeps the secret in `gh`'s store. Agents get `env_commands`
  from the MCP `env_for` tool and run the command in their shell.

## 9. Out of scope

Setting env automatically (direnv recipe printed instead), token creation,
`git credential` helpers, `GIT_SSH_COMMAND` by default.

## 10. Acceptance tests (`tests/test_envcmd.py`)

1. Repo matching `work-github` (`gh_account = "me-work"`) → stdout has
   the `# gitpro env for` comment, `export GH_HOST='github.com'`,
   `export GH_TOKEN="$(gh auth token -u 'me-work' -h 'github.com')"`,
   the direnv comment; **no** `GIT_SSH_COMMAND`, no author lines; exit
   0. `sh -n` on the output passes.
2. Fake `gh` on `fake_bin` printing `SECRET` for `auth token` → `SECRET`
   never appears in stdout or `--json`; `fake_bin.calls("gh")` is empty.
3. `--shell fish` → `set -gx GH_TOKEN (gh auth token -u 'me-work' -h
   'github.com')`; `--shell pwsh` → `$env:GH_TOKEN = (…)`.
4. `--unset` → `unset GH_HOST GH_TOKEN`; fish → one `set -e -g` per var;
   pwsh → `Remove-Item Env:GH_HOST, Env:GH_TOKEN …`.
5. `--only ssh` → `GIT_SSH_COMMAND` line with the `overrides
   core.sshCommand` comment and nothing else; `--only author` →
   author/committer exports only; `--only gh,ssh` → both, no author.
6. Repo with a local override → author lines appear with the `# repo
   .git/config overrides` comment and a `# ⚠` line, without `--only`.
7. Identity on `ghe.corp.test` with `gh_account` → `GH_ENTERPRISE_TOKEN`
   line, `GH_HOST='ghe.corp.test'`, JSON `token_var ==
   "GH_ENTERPRISE_TOKEN"`; `github.com` → `GH_TOKEN`.
8. Identity with `glab_account` on gitlab.com and fake `glab` where `auth
   token --help` exits 0 → `GITLAB_TOKEN="$(glab auth token -h
   'gitlab.com')"` and `GITLAB_HOST`; fake exiting 1 → comment line; no
   `glab` → no glab lines, exit 0. `GITLAB_TOKEN=x` in the environment →
   the `already set in this shell` comment; `x` never printed.
9. Not a repo → single comment line, exit 2; no identity → comment, exit
   1; `sh -c 'eval "$(cat out)"'` exits 0 for both outputs.
10. `--json` → `env_commands.GH_TOKEN == "gh auth token -u me-work -h
    github.com"`, `env` has no `GH_TOKEN` key.
11. `--shell csh` → exit 2; `--only foo` → exit 2.
12. `gh_account = "me;rm -rf x"` in the config → `✗ invalid gh_account`
    exit 2, nothing printed on stdout.
13. `--check` (the probe that runs the printed `gh auth token` line) with
    a fake `gh` whose `auth token -u` exits 1 printing `unknown shorthand
    flag: 'u'` → `✗ gh 2.40 or newer is needed for multiple accounts`,
    exit 2 (01 §5.5, V-029); the plain print path does not run `gh`.

## 11. Modules touched

New `envcmd.py` (`lines(report, cfg, shell, only, unset) -> list[str]`,
`as_json`, `register`).

## 12. Docs text (for 17)

> `eval "$(gitpro env)"` sets `GH_HOST` and `GH_TOKEN` (or
> `GH_ENTERPRISE_TOKEN`, `GITLAB_TOKEN`) via `gh auth token -u <account>`
> for the identity of the current repo. gitpro prints the command that
> fetches the token, never the token. Add `--only ssh` for
> `GIT_SSH_COMMAND`, `--only author` for author variables. `gitpro env
> --unset` reverses it; `--shell fish|pwsh` for other shells. With
> direnv: `echo 'eval "$(gitpro env)"' >> .envrc`.
