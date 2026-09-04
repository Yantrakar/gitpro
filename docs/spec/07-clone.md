# 07 — `gitpro clone <url>`

Wave 3. Depends on 01 (`resolve.parse_url`, `resolve.match_url`,
`resolve.compare`, `clone_dir`, `[clone]`, `jsonout`) and 15 (the
`clone.ts` stub and the `gitpro.clone` command contribution).

Files this package may create or edit: new `src/gitpro/clone.py`,
`tests/test_clone.py`; replace `vscode/src/clone.ts` (15's stub)
wholesale. Nothing else.

## 1. Purpose and user story

`git clone` with several accounts means remembering the right ssh alias
and the right folder. `gitpro clone <url>` picks the identity from the URL
(the same rule git will use afterwards), rewrites the URL to the identity's
ssh alias when the identity asks for it, chooses the target directory from
the identity's `clone_dir` and the `[clone].layout`, runs `git clone`, and
verifies the result with `resolve.compare`.

User story: "`gitpro clone https://github.com/corp-org/app` puts the repo in
`~/work/github.com/corp-org/app`, with origin `git@github-work:corp-org/app.git`
and the work identity, and tells me so in three lines."

## 2. CLI surface

```
gitpro clone <url> [DIR] [--identity ID] [--ssh] [--no-rewrite] [--dry-run] [-y] [--json] [-- <git clone args…>]
```

Steps:

1. Parse `url` (`resolve.parse_url`; also accepts `owner/repo` shorthand
   only when `--identity` is given, host from the identity; also accepts a
   `gh`-style `host/owner/repo`).
2. Identity = `--identity ID` or `resolve.match_url`. No match → `[default]`.
3. Clone URL. The user's scheme is kept unless the identity says otherwise:
   - identity has `ssh_alias` and `rewrite = true` and `--no-rewrite` is
     absent → `<ssh_user>@<alias>:<owner>/<repo>.git` (or
     `ssh://<ssh_user>@<alias>:<port>/<owner>/<repo>.git` when `ssh_port`);
     this is exactly what git's generated `insteadOf` would do after the
     clone, so the stored remote URL and the used one agree;
   - https URL and `--ssh` given → `<ssh_user>@<host>:<owner>/<repo>.git`;
   - otherwise the URL as given. An https clone against an identity with
     an `ssh_key` is allowed: the identity's `includeIf` matches
     `https://host/owner**` too, so name/email are right; auth is the
     user's credential helper. The preview says `auth: https (credential
     helper); add --ssh to use key ~/.ssh/id_work`.
4. Target dir = `DIR` if given, else `<clone_dir or [clone].dir or cwd>` +
   `layout` with `{host}` = real host (not alias), `{owner}`, `{repo}`,
   `{id}` (identity id or `default`). Layout placeholders are the only
   substitutions; the result must stay inside its base (`..` from URL parts
   is rejected).
5. Preview and confirm (`-y` skips; `--dry-run` prints and exits 0):

```
clone git@github-work:corp-org/app.git
  into      ~/work/github.com/corp-org/app
  identity  work-github  Work Me <me@corp.test>   (owner corp-org on github.com)
  ssh       alias github-work → github.com, key ~/.ssh/id_work
clone? [y/n]
```

6. `git clone [git args] -- <url> <dir>` (the `--` guards URLs and dirs
   that start with `-`), output passed through (stderr inherited so
   progress shows), `stdin` inherited (ssh may ask for a passphrase).
   Timeout: none. The child runs with the **caller's umask** restored
   (`main()` sets `umask 077` for gitpro; the clone must not produce a
   repo whose files are unreadable to the user's group or to a container
   user): `clone.run` reads `gitpro.cli.CALLER_UMASK` (01 captures it at
   import, before the process umask is set to 0o077; `hot.CALLER_UMASK`
   is the same value) and passes `umask=CALLER_UMASK` to
   `subprocess.run` (Python ≥ 3.9; no `preexec_fn`) (00 known issue 16).
7. `resolve.compare(dir)`; print `✓ cloned into ~/work/…; identity
   work-github <me@corp.test>` or the first problem as `⚠` with its fix.
   Common case: default has no email and no identity matched → `⚠ no
   identity for github.com/someone: git will refuse to commit here → gitpro
   scan --suggest --repo <dir>`.

Exit: 0 cloned (even with a `⚠`), 1 declined, 2 git clone failed (`✗ git
clone failed (<rc>)`) or target exists (`✗ <dir> already exists`).

Always require `-y` or a TTY; the preview is the value of the command.

Capabilities registered: `clone`.

## 3. `--json`

Requires `-y` (or `--dry-run`). `{"ok": true, "command": "clone", "url":
"<as given>", "clone_url": "git@github-work:corp-org/app.git", "dir":
"/abs", "identity": "work-github"|null, "auth": "ssh-alias"|"ssh"|"https",
"dry_run": false, "problems": [...resolve problems...], "git_exit": 0}`.

## 4. Config keys

`[clone] dir`, `layout`; identity/default `clone_dir`; `ssh_alias`,
`ssh_user`, `rewrite`, `ssh_port`.

## 5. VS Code surface

15 declares the command `gitpro.clone` ("gitpro: Clone…") and
`features.ts` calls `registerClone(context, cli)` from `./clone`. This
package replaces `vscode/src/clone.ts` and exports:

```ts
export function registerClone(context: vscode.ExtensionContext, cli: Cli): void;
```

Flow: input box for the URL → `clone --json --dry-run <url>` → modal
`Clone <clone_url> into <dir> as <identity>?` → `clone --json -y <url>`
run with `GIT_TERMINAL_PROMPT=0` and `GIT_SSH_COMMAND` unset (so the
identity's `core.sshcommand` applies) plus `-- -c core.askPass=` is **not**
passed; instead the extension runs the clone through
`vscode.window.withProgress` (notification, cancellable: cancel kills the
child) and, on `git_exit != 0` with stderr containing `Permission denied`
or `could not read`, shows `Clone needs a terminal (ssh passphrase or
credentials). Run in a terminal: gitpro clone <url>` with a `Copy command`
button. On success offers `Open Folder` (reviewer R-054).

## 6. Files created/changed

The clone directory (created by git). Parent dirs created with
`os.makedirs(mode=0o755)` under the restored umask (not identity data).
Undo: `rm -r <dir>` — printed as `↩ undo: rm -r ~/work/github.com/corp-org/app`
after a successful clone. Nothing in home is touched.

## 7. Failure modes

| situation | message | exit |
|---|---|---|
| url unparsable | `✗ cannot parse '<url>' as a git URL` | 2 |
| `owner/repo` without `--identity` | `✗ 'owner/repo' needs --identity ID (which host?)` | 2 |
| target exists | `✗ <dir> already exists` | 2 |
| `--identity` unknown | `✗ no identity 'x' (have: a, b)` | 2 |
| git clone fails | git's own stderr, then `✗ git clone failed (128)` | 2 |
| layout escapes base | `✗ target leaves <base>` | 2 |
| `--json` without `-y`/`--dry-run` and no tty | `no terminal to confirm on` | 2 |

## 8. Security / privacy

`git clone` is run with an argv list; URL and dir are never shell-expanded
and come after `--`. Extra `-- <git args>` are passed verbatim before the
`--`. Network: this is the user's own `git clone`; gitpro adds no requests.

## 9. Out of scope

`gh repo clone` features (forks, upstream remote setup), bulk clone from
`forge repos` (05 prints `gitpro clone` lines), mirror/bare clones (pass
through `-- --bare` works but the layout is unchanged).

## 10. Acceptance tests (`tests/test_clone.py`)

Use `bare_remote("upstream")` as the source; URLs of the form
`git@github.com:corp-org/app.git` cannot be cloned offline, so tests pass a
fake `ssh` on `fake_bin` **or** test URL/dir computation as pure functions
and the clone step with a local path. Do both:

1. `clone.plan("https://github.com/corp-org/app", cfg)` → identity
   `work-github`, `clone_url == "git@github-work:corp-org/app.git"`, dir
   `<home>/work/github.com/corp-org/app` (identity `clone_dir = "~/work"`),
   `auth == "ssh-alias"`.
2. Identity without alias, with `ssh_key`, https url → clone url stays
   https, `auth == "https"`, preview mentions `add --ssh`; with `--ssh` →
   `git@github.com:corp-org/app.git`. Identity with `ssh_user = "gitlab"`
   → `gitlab@<alias>:…`.
3. `--no-rewrite` keeps the url; `--identity personal` forces the identity
   and its dir.
4. No match, `[clone] dir = "~/src"`, layout default → dir
   `~/src/github.com/someone/x`, identity `None`.
5. `layout = "{id}/{repo}"` → `~/src/work-github/app`; URL owner `..` →
   `target leaves` error.
6. `owner/repo` without `--identity` → exit 2; with → ok.
7. Real clone: `clone <bare> <tmp>/dest -y` → repo exists, stdout has
   `✓ cloned into`, `↩ undo: rm -r`; `--json -y` object has `git_exit ==
   0` and `dir`. Monkeypatch `subprocess.run` to capture argv → it is
   `["git", "clone", "--", url, dir]`; with `-- --depth 1` → `["git",
   "clone", "--depth", "1", "--", url, dir]`.
8. Umask: run the test with `os.umask(0o022)` before `cli.main`, clone a
   bare repo with one committed file → the file in the clone is mode
   `0644` and the directory `0755` (the clone child restored the caller's
   umask); gitpro's own state files written in the same run are still
   0600.
9. Existing target → exit 2, nothing cloned.
10. No tty, no `-y` → `no terminal to confirm on`, no clone.
11. `--dry-run` → preview printed, no directory created, exit 0; `--dry-run
    --json` has `dry_run: true`.
12. ssh alias clone with fake `ssh` (pattern from
    `test_clone_uses_identity_key_without_alias`): `clone
    git@github.com:corp-org/app.git -y` runs `git clone --
    git@github-work:corp-org/app.git <dir>`; the fake `ssh` log shows it was
    invoked for host `github-work` (the clone itself fails; assert exit 2
    and the log line).
13. After a successful local clone with a config whose default has no
    email → `⚠ no identity for` line, exit 0.

## 11. Modules touched

New `clone.py` (`plan(url, cfg, *, dir, identity, ssh, no_rewrite) ->
ClonePlan`, `run(plan, git_args, *, umask)`, `register`),
`vscode/src/clone.ts` (replaces the stub).

## 12. Docs text (for 17)

> `gitpro clone <url>` picks the identity from the URL, rewrites it to the
> identity's ssh alias when the identity has one, clones into
> `clone_dir/<host>/<owner>/<repo>` and checks the result. An https URL
> stays https unless you pass `--ssh`. Set `clone_dir` per identity or
> `[clone] dir`; change the folder shape with `[clone] layout`.
> `--dry-run` shows the plan.
