# 05 — Forge view (read-only via gh/glab) and opt-in similar-work search

Wave 4. Depends on 01 (the minimal `forge.py`: `Account`, `available`,
`accounts`, `gh_env`, `logged_in`, `run`, `repo_exists`, `announce`,
`no_online_error`; `[forge]`, `gh_account`/`glab_account`, `jsonout`) and
02 (`scan.find` with excludes, for "cloned" detection). 07 is a soft
dependency (the `→ gitpro clone` line).

Files this package may create or edit: `src/gitpro/forge.py` (05 is the
only package that edits it after 01: it adds `register` and the command
functions, and keeps every 01 signature unchanged), new
`tests/test_forge.py`. Nothing else.

## 1. Purpose and user story

The forge knows things the disk does not: which repos each account owns,
which forks have an upstream that moved, whether the identity's email is
verified on the account. All of it is readable through the user's own `gh`
/ `glab` login. gitpro wraps those calls, maps results to identities, and
never writes.

User story: "`gitpro forge repos` shows that my work account has 14 repos,
3 of which I never cloned, and that one personal fork's upstream was renamed.
`gitpro forge email` confirms each identity's email is a verified email on
its account."

## 2. CLI surface

```
gitpro forge repos   [--identity ID]… [--not-cloned] [--orgs] [--online] [-c PATH] [--json]
gitpro forge forks   [--identity ID]… [--online] [-c PATH] [--json]
gitpro forge email   [--identity ID]… [--online] [-c PATH] [--json]
gitpro forge similar [--identity ID]… [--name PATTERN]… [--online] [--send-repo-names] [-c PATH] [-y] [--json]
```

Every subcommand goes online. Without `--online` (or `[forge] online =
true`) it exits 2 through `forge.no_online_error` (01 §5.5):

```
✗ forge repos needs the network: it asks github.com via gh (account me-work) for the repo list of me-work; nothing else is sent
→ re-run with --online, or set [forge] online = true
```

With `--online`, the first line is `forge.announce`'s `online:` form with
the same content. When neither `gh` nor `glab` is on PATH, the same
`no_online_error` path exits 2 (`needs gh or glab on PATH`).

### 2.1 Accounts and tokens (01 §5.5, repeated)

`forge.accounts(cfg)` lists one `Account(host, cli, login)` per identity
with `gh_account`/`glab_account` (and `[default]`). `forge.run(acc, argv)`
runs `gh` with `GH_HOST=<host>` and the token for `login` in `GH_TOKEN`
(for `github.com`) or `GH_ENTERPRISE_TOKEN` (any other host) through
`forge.gh_env`; the token comes from `gh auth token -u <login> -h <host>`
and is passed in the child's environment only, never argv, output or
logs. `gh auth switch` is never run. For `glab`, `GITLAB_HOST=<host>` and
the active login; when `glab auth status` reports a different login than
`glab_account`, a `⚠ glab is logged in to <host> as <x>, identity <id>
expects <y>; results are for <x>` is printed (DECISIONS Q11: one login
per host).

### 2.2 `forge repos`

For each account: `gh repo list <login> --limit 1000 --json
nameWithOwner,isFork,isArchived,sshUrl,pushedAt`; with `--orgs`, also `gh
api user/orgs --paginate --jq '.[].login'` and `gh repo list <org> …` per
org. For glab: `glab repo list --mine --per-page 100 --page N --output
json` until a page is short (glab has no `--limit`). Cloned status comes
from a local scan (02 roots and excludes) matching normalised
`host/owner/repo` of the raw `origin` URL.

```
online: asking github.com via gh (accounts me-work, me) and gitlab.com via glab (jane.doe) for repo lists; sends nothing else

work-github  me-work@github.com   14 repos · 11 cloned · 3 not cloned · 1 archived
  not cloned:
    corp-org/infra         pushed 2026-09-20   → gitpro clone git@github.com:corp-org/infra.git
    corp-org/old-site      pushed 2023-01-05   archived
    me-work/dotfiles       pushed 2026-08-01   → gitpro clone git@github.com:me-work/dotfiles.git
personal     me@github.com         9 repos · 9 cloned
gitlab-work  jane.doe@gitlab.com   3 repos · 2 cloned · 1 not cloned
  not cloned:
    grp/sub/tool           pushed 2026-05-11   → gitpro clone git@gitlab.com:grp/sub/tool.git
```

`--not-cloned` lists only those. `→ gitpro clone …` when `gitpro.clone`
imports (07), else `→ git clone …`.

### 2.3 `forge forks`

Repos with `isFork: true`: `gh repo view <nameWithOwner> --json
parent,nameWithOwner` → parent `nameWithOwner`. Compared with the local
clone's `upstream` remote (raw URL, if any): missing upstream, or upstream
URL whose `owner/repo` differs from the parent (moved/renamed), or parent
`null` (upstream deleted).

```
forks of me (github.com via gh)
  me/requests     parent psf/requests        local ~/src/requests  upstream ok
  me/oldtool      parent (deleted)           local ~/src/oldtool   → upstream is gone; keep or archive
  me/libfoo       parent neworg/libfoo       local ~/src/libfoo    upstream remote points to oldorg/libfoo
                                             → git -C ~/src/libfoo remote set-url upstream git@github.com:neworg/libfoo.git
```

### 2.4 `forge email`

`gh api user/emails --jq '.[] | select(.verified) | .email'` (needs the
`user:email` scope; on a 403/404 or an error body mentioning scopes:
`⚠ gh token for <login> lacks the user:email scope; run: gh auth refresh
-h <host> -s user:email`, `verified: null`) and `glab api user --jq .email`
(plus `glab api user/emails` where available). Compares with each
identity's `email`.

```
work-github  me@corp.test        verified on github.com (me-work)       ✓
personal     me@personal.test    not an email of github.com account me  ✗  → add it at https://github.com/settings/emails or fix the identity
gitlab-work  jane@corp.test      verified on gitlab.com (jane.doe)      ✓
```

Exit 1 when any `✗`.

### 2.5 `forge similar`

Strictly opt-in. Searches the forge for public repos whose names match the
user's local repo names, to find "I already published this under another
account" and "someone forked my work". Requires **both** `--online` and
`--send-repo-names` on every run (or `[forge] similar = true`), else exit 2:

```
✗ forge similar sends your local repo names (12 names from ~, /mnt/d/work) to github.com via gh as search queries
→ re-run with --online --send-repo-names to allow that
```

What is sent (reviewer R-022/R-023): only the repo names (last path
segment of the raw `origin` URL, or the directory name for repos with no
remote) of local repos that either have **no remote** or whose remote host
is the forge being asked. Repos whose remote points at another host are
never sent. `--name PATTERN` (fnmatch, repeatable) restricts the list
further. Before any request the full list is printed and confirmed:

```
online: sending 12 repo names to github.com via gh (account me) as `gh search repos <name>`; no paths, emails or contents
  app, parser, notes, dotfiles, infra, tool, …(6 more)
send these 12 names?  [y/n]
```

(`-y` skips the question; with `--json` `-y` is required.) Then one `gh
search repos <name> --limit 10 --json nameWithOwner,isFork` per name with a
0.5 s pause between calls; on `HTTP 403`/`rate limit` in stderr the run
stops with `⚠ github.com: rate limited after N names; the rest were not
sent` and reports what was sent.

```
  app        3 public matches: corp-org/app (yours, cloned), me/app (yours), other/app
  parser     1 public match:   someone/parser   not yours
  notes      0
```

`--json` carries `sent: ["app", "parser", ...]` so the record of what left
the machine is explicit.

Capabilities registered: `forge`, `forge.similar`.

## 3. `--json` schema

```json
{"ok": true, "command": "forge repos", "online": true,
 "sent": {"github.com": ["repo list me-work", "repo list me"], "gitlab.com": ["repo list --mine"]},
 "identities": [{"id": "work-github", "host": "github.com", "cli": "gh", "account": "me-work",
   "repos": [{"name_with_owner": "corp-org/infra", "ssh_url": "git@github.com:corp-org/infra.git", "fork": false, "archived": false,
              "pushed_at": "2026-09-20T10:11:12Z", "cloned": null, "clone_cmd": "gitpro clone git@github.com:corp-org/infra.git"}]}],
 "warnings": []}
```

`forge forks`: `forks: [{"name_with_owner","parent","local","upstream_url","status": "ok"|"missing"|"moved"|"parent_deleted","fix"}]`.
`forge email`: `emails: [{"id","email","host","account","verified": true|false|null,"fix"}]`.
`forge similar`: `sent: [names]`, `not_sent: N`, `matches: [{"name","results":[{"name_with_owner","yours": bool,"cloned": path|null}]}]`.

## 4. Config keys

`[forge] online`, `similar`; identity/default `gh_account`,
`glab_account`; `[scan] roots`, `exclude` for cloned detection.

## 5. VS Code surface

None.

## 6. Files created/changed

None. No cache.

## 7. `forge.py` after this package

01 §5.5 defines and ships: `Account`, `available`, `accounts`, `gh_env`,
`logged_in`, `run(acc, argv, *, timeout=30, stdin=DEVNULL)`,
`repo_exists(acc, owner, repo) -> (status, detail)` with status
`"ok"|"renamed"|"missing"|"unknown"`, `announce`, `no_online_error`. 05
adds, without changing those: `register(sub, common, confirm)`,
`repos(cfg, accounts, *, orgs)`, `forks(...)`, `emails(...)`,
`similar(cfg, accounts, names, *, confirm)`, `glab_pages(acc, argv)`.
`forge.py` keeps 01's import rule (module level: stdlib + `gitpro.config`,
`gitpro.jsonout`); `scan` and `resolve` are imported inside functions.

## 8. Failure modes

| situation | message | exit |
|---|---|---|
| no `--online` | `✗ … needs the network …` (`no_online_error`) | 2 |
| gh/glab missing | `✗ forge repos needs gh or glab on PATH …` | 2 |
| not logged in | `✗ gh has no login for me-work on github.com; run: gh auth login -h github.com` | 2 |
| scope missing (`email`) | `⚠ … lacks the user:email scope; run: gh auth refresh -h <host> -s user:email` | continue |
| rate limited / API error | `⚠ github.com: <gh stderr line>; <identity> skipped` (`similar`: stops) | continue |
| no account mapped | `⚠ identity x has no gh_account/glab_account; skipped` | continue |
| timeout 30 s | `⚠ gh timed out; <identity> skipped` | continue |
| glab login differs | `⚠ glab is logged in to <host> as <x>, identity <id> expects <y>` | continue |

## 9. Security / privacy

Read-only API calls only (`repo list`, `repo view`, `api user/orgs`, `api
user/emails`, `search repos`). Tokens: obtained via `gh auth token` into a
child's env, never stored, never printed. Nothing is sent that is not
listed on the `online:` line. `forge similar` is the only command that
sends local information (repo names), needs its own flag, prints the full
list and asks before sending.

## 10. Out of scope

Creating repos, forks, keys (09 does `keys --upload` with the same
`forge.run`), org membership management, Bitbucket/Gitea (hosts without
gh/glab are skipped with a warning), caching.

## 11. Acceptance tests (`tests/test_forge.py`)

Fake `gh` (`fake_bin.add("gh", body)`) that switches on `argv[:2]`: `auth
status` → exit 0 (JSON hosts); `auth token` → prints `FAKE_TOKEN`; `repo
list` → fixed JSON; `repo view` → JSON by name; `api user/emails` → JSON;
`api user/orgs` → JSON; `search repos` → JSON. The fake logs
`env:GH_TOKEN=set|unset env:GH_ENTERPRISE_TOKEN=set|unset` per call.

1. `forge repos` without `--online` → exit 2, stderr names gh and the
   account, `fake_bin.calls("gh") == []`.
2. `forge repos --online` with two identities (gh_account `me-work`, `me`)
   and one cloned repo under `[scan].roots` → text shows `1 cloned · 1 not
   cloned` for the matching identity; JSON `cloned` is the local path or
   null; calls show `repo list me-work` and `repo list me`, every call
   with `env:GH_TOKEN=set`, and `FAKE_TOKEN` appears nowhere in stdout,
   stderr or JSON.
3. Identity on `ghe.corp.test` → its calls log `env:GH_ENTERPRISE_TOKEN=set
   env:GH_TOKEN=unset`.
4. `[forge] online = true` → no `--online` needed.
5. `--orgs` → `api user/orgs` is called, then `repo list <org>`.
6. `forge forks --online`: fake returns one fork with parent
   `neworg/libfoo` while the local clone's `upstream` is
   `git@github.com:oldorg/libfoo.git` → status `moved`, fix command with
   `remote set-url upstream`.
7. Fork with `parent: null` → `parent_deleted`.
8. `forge email --online`: fake returns verified emails `[me@corp.test]` →
   identity with that email `✓`, identity with another email `✗`, exit 1.
9. `forge email` when fake `api user/emails` exits 1 with `HTTP 403 …
   user:email scope` → warning with `gh auth refresh -h github.com -s
   user:email`, `verified: null`, exit 0.
10. `forge similar --online` without `--send-repo-names` → exit 2, no
    `search` call logged. With both and `-y`: local repos `app` (origin
    github.com), `parser` (no remote), `tool` (origin gitlab.com) → `sent
    == ["app", "parser"]`, `not_sent == 1`, calls have `search repos app`
    and `search repos parser` but not `tool`; `yours` true for `me/app`.
    Without `-y` and no tty → `no terminal to confirm on`, no `search`
    call. `--name 'par*'` → only `parser` sent.
11. Fake `search repos` exits 1 with `HTTP 403: API rate limit exceeded`
    on the second call → warning `rate limited after 1 names`, `sent` has
    one entry, exit 0.
12. gh missing from PATH → exit 2 with the `needs gh or glab` sentence.
13. `forge.repo_exists` (01's function, unchanged by 05) with fake `repo
    view` exit 1 `Could not resolve to a Repository` → `("missing", …)`;
    `nameWithOwner` different → `("renamed", "me/new")`; fake exit 1 with
    `error connecting` → `("unknown", …)`.
14. glab: fake `glab` with `auth status` printing `Logged in to gitlab.com
    as jane.doe`; identity `glab_account = "other"` → warning about the
    active login. `repo list` returns 100 items on page 1 and 3 on page 2
    → 103 repos, calls show `--page 1` and `--page 2`.

## 12. Modules touched

`forge.py` (adds `register` with the `forge` subparser and its four
sub-subcommands, the command functions, `glab_pages`). Reads `scan.find`
for cloned detection.

## 13. Docs text (for 17)

> `gitpro forge repos|forks|email|similar --online` read your accounts
> through `gh`/`glab`: repos you have not cloned, forks whose upstream moved,
> whether each identity's email is verified. Every call is read-only and the
> first line says what is sent. `forge similar` additionally needs
> `--send-repo-names` on every run because it sends your local repo names
> as searches; it shows the list and asks first.
