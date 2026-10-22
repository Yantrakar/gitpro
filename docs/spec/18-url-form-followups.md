# 18 — url_form follow-ups from the provider sandbox test

Four gaps found by a sandbox test of remote URL forms from common git hosts.
Each is a change to the `url_form` problem in `resolve.compare` (package 01,
section "PROBLEM_CODES") or to the advisory checks in `lint.py`. **URL
matching does not change.** `render.url_patterns` and `resolve.match_url`
keep mirroring git's `includeIf hasconfig:remote.*.url`: the raw URL,
case-sensitive, `*` stops at `/` (01; REVIEW.md R-007). An unusual URL form
is still not matched; it is reported, with a fix where a correct one exists.

Files this package may edit: `src/gitpro/resolve.py` (`_url_fixes` and the
`url_form` block of `compare` only), `src/gitpro/lint.py`,
`tests/test_resolve.py`, a new `tests/test_lint.py` (no test file calls
`lint.warnings` directly today; `tests/test_doctor.py` covers it through
`doctor`), `docs/spec/01-config-resolve-why.md` (the
`url_form` row and the `why` example only), `docs/guide.md` (the identity
key table only). Nothing else. No change to `render.py`, `config.py`,
`OWNER_RE`, the VS Code extension, MCP, guard or audit.

## 0. Rules for this package

1. One item per commit, in the order 4, 2, 3, 1 (section 6). Before the
   first change, run the full suite as a baseline and record the counts.
2. No regression. Every existing test passes unchanged, except the two
   assertions this spec names in section 4.5, which change on purpose.
3. Each item adds tests for the new case **and** for the cases next to it
   that must stay as they are.
4. Where a URL form is involved, add it to the differential test against
   real git (`DIFF_URLS` in `tests/test_resolve.py`), to show that git also
   does not match it. That pins that matching is unchanged.
5. Full suite after every item:
   `env -u FORCE_COLOR .venv/bin/python -m pytest -q -p no:cacheprovider`.
   Baseline on main at 6fb0442: 1627 passed, 2 skipped.

## 1. Current code (as of main 6fb0442)

`resolve._url_fixes(url, cfg) -> List[Tuple[str, str, Identity, str]]`
builds candidate URLs, in order, and returns the first one that
`match_url` matches, as `[(fixed url, reason, identity, pattern)]`:

1. `https?://user@rest` → `https?://rest`, reason `no user in the URL`.
2. `ssh://host/...` with no user → `ssh://<u>@host/...` for each `u` in the
   distinct `ssh_user` of all identities (or `["git"]`), reason
   `user <u>@ in the URL`. Same for scp form `host:path` with no user →
   `<u>@host:path`.
3. `(ssh|https?)://host:<port>/...` → port removed, reason
   `no port in the URL`.

`compare` (block `if exp.match.identity is None:`) takes the first
candidate remote URL with a repair and appends:

```python
argv = ["git", "-C", str(top), "remote", "set-url", name, fixed] \
    if name and remote_url is None else None
need = pat.replace("**", "…")
out.append(_p("url_form", f"{name or 'the remote'} is {redact_url(url)}; the {i.id} patterns need {need} "
                          f"({reason})", argv))
```

`_p(code, message, argv, fix=None)` sets `fix` to the shell-quoted argv
when `fix` is None, and `fix_argv` to `tuple(argv)` or None. A problem
with `fix` text and `fix_argv = None` is already supported by every
consumer (checked):

- `resolve._shown_fix` returns `p.fix` when there is no argv; `render_text`
  prints `→ <fix>` when `p.fix or p.fix_argv`.
- `doctor._problem` uses `p.fix`.
- `guard.py:262-264` and `audit.py:495` print `_shown_fix(cause)`.
- VS Code `labelFor` (`vscode/src/identityBar.ts`) offers no button when
  `fix_argv` is empty; the problem still shows. No VS Code change needed.

`env_override` already returns such a problem (fix text, no argv).

## 2. Item 1: owner (or host) letter case differs

### Facts (verified 2026-10-15)

- git's `hasconfig:remote.*.url` glob is case-sensitive: an identity with
  owner `acme` does not match `git@github.com:ACME/r.git` in real git.
  gitpro agrees (spec 01 differential test already has an uppercase-owner
  URL).
- GitHub and GitLab resolve owner and repo case-insensitively over HTTPS:
  `git ls-remote https://github.com/YANTRAKAR/GITPRO.git HEAD` returns the
  same sha as `https://github.com/Yantrakar/gitpro.git`, and
  `https://gitlab.com/GITLAB-ORG/CLI.git` the same as
  `https://gitlab.com/gitlab-org/cli.git`.
- Not verified: SSH on those hosts, or any other host. A host that is
  case-sensitive would treat `ACME` and `acme` as different owners.

### Current behaviour

`git@github.com:ACME/r.git` with an identity for owner `acme`: no pattern
matches, none of the three repairs applies, so no `url_form` problem.
`gitpro why` reports the default identity with no explanation, and git
commits as the default identity.

### Required behaviour

Add a fourth repair, tried **after** the three existing ones (so every URL
that gets a repair today gets the same one):

- For each identity `i` (config order) and each pattern `pat` in
  `render.url_patterns(i)` (list order), let `prefix` be `pat` up to its
  first `*` (the whole pattern if it has none).
- If `url.lower().startswith(prefix.lower())` and not
  `url.startswith(prefix)`, the candidate is `prefix + url[len(prefix):]`.
- The candidate is accepted only if `match_url(candidate, cfg)` returns an
  identity (as for the other repairs). First accepted candidate wins.
- Reason: `letter case differs; git matches case-sensitively`.
- Fix: the usual `git -C <top> remote set-url <name> <candidate>` argv.

Examples (identity `gh-corp`, host `github.com`, owners `["corp-org"]`):

| remote URL | url_form fix URL |
|---|---|
| `git@github.com:CORP-ORG/app.git` | `git@github.com:corp-org/app.git` |
| `https://github.com/Corp-Org/app.git` | `https://github.com/corp-org/app.git` |
| `git@GitHub.com:corp-org/app.git` | `git@github.com:corp-org/app.git` |
| `git@github.com:corp-org/app.git` | no problem (matches; unchanged) |
| `git@github.com:other/app.git` | no url_form (unchanged) |
| `https://me@github.com/CORP-ORG/app.git` | no url_form: needs two repairs (out of scope, section 7) |

Owner globs: with owners `["team-*"]`, `git@github.com:TEAM-x/r.git` →
`git@github.com:team-x/r.git` (prefix `git@github.com:team-`).

### Tests

- New: the first three table rows, plus the `team-*` case, each checking
  `fix_argv[-1]`, the reason text in `message`, and `p.code == "url_form"`.
- Unchanged, pinned: the existing `test_url_form`,
  `test_url_form_missing_user_and_port` (first half),
  `test_url_form_https_port` as revised in 4.5; a matching URL has no
  problems; `git@github.com:other/app.git` has no `url_form`.
- Differential: `DIFF_URLS` already has `git@github.com:Corp-Org/app.git`.
  Add `https://github.com/Corp-Org/app.git` (git and gitpro both give the
  default, unset email).

## 3. Item 2: `git+ssh://` and `ssh+git://` schemes (Launchpad)

### Facts (verified 2026-10-15)

- git treats `git+ssh://` and `ssh+git://` exactly as `ssh://`. With
  `GIT_SSH_COMMAND` set to a script that prints its arguments,
  `git ls-remote` on `ssh://me@example.test/~me/r`,
  `git+ssh://me@example.test/~me/r` and `ssh+git://me@example.test/~me/r`
  each ran `me@example.test git-upload-pack '~me/r'`.
- `hasconfig:remote.*.url` matches the raw URL, so a `git+ssh://` remote
  does not match gitpro's `ssh://` patterns (probe: real git gives the
  default identity).
- Launchpad documents `git+ssh://` URLs:
  https://ubuntu.com/docs/launchpad/user/explanation/working-with-code/git-hosting/
  shows `[url "git+ssh://USER@git.launchpad.net/"] insteadof = lp:`,
  `git+ssh://git.launchpad.net/REPOSITORY` and
  `https://git.launchpad.net/~user/example-project`. Its SSH user is the
  Launchpad user name, so a Launchpad identity needs `ssh_user = "<name>"`.

### Current behaviour

`git+ssh://me@git.launchpad.net/~me/r` with an identity (host
`git.launchpad.net`, owners `["~me"]`, `ssh_user = "me"`): no match and no
repair, so no `url_form`.

### Required behaviour

If the URL starts with `git+ssh://` or `ssh+git://` (lower case, as git
spells them), let `base = "ssh://" + url[len(scheme):]`. Before repairs 1
to 3, try:

1. `base` itself, reason `ssh:// instead of <scheme>` (for example
   `ssh:// instead of git+ssh://`).
2. Then the user and port repairs (2 and 3 of section 1, with item 4's
   rules for the port) applied to `base`; reason
   `ssh:// instead of <scheme>, <that repair's reason>`.

First accepted candidate wins, as now. The fix is the usual set-url argv to
the accepted candidate (or, for a port repair that item 4 turns into text,
that text).

Examples (identity `lp`: host `git.launchpad.net`, owners `["~me"]`,
`ssh_user = "me"`):

| remote URL | url_form fix URL |
|---|---|
| `git+ssh://me@git.launchpad.net/~me/r` | `ssh://me@git.launchpad.net/~me/r` |
| `ssh+git://me@git.launchpad.net/~me/r` | `ssh://me@git.launchpad.net/~me/r` |
| `git+ssh://git.launchpad.net/~me/r` | `ssh://me@git.launchpad.net/~me/r` (scheme, then user) |
| `ssh://me@git.launchpad.net/~me/r` | no problem (matches; unchanged) |
| `https://git.launchpad.net/~me/r` | no problem (matches; unchanged) |
| `git+ssh://me@git.launchpad.net/~other/r` | no url_form |

### Tests

- New: each table row.
- Differential: add an `lp` identity to `DIFF_CONFIG` (host
  `git.launchpad.net`, owners `["~me"]`, `ssh_user = "me"`) and these URLs
  to `DIFF_URLS`: `git+ssh://me@git.launchpad.net/~me/r` (both default),
  `ssh://me@git.launchpad.net/~me/r` and `https://git.launchpad.net/~me/r`
  (both `lp`). Adding an identity must not change the result of any URL
  already in `DIFF_URLS`; the test checks this anyway.

## 4. Items 3 and 4: repairs that must not suggest a wrong URL

Both items are repairs that today produce a `set-url` that points at the
wrong place. In both, the remote is still reported as `url_form`, but with
fix **text** and `fix_argv = None`, built with
`_p("url_form", message, None, fix=text)`.

`_url_fixes` therefore needs to return, per accepted candidate, either a
fixed URL (as now) or a message and fix text with no URL. The exact shape
is the implementer's choice; `compare` must build the problems below.

### 4.1 Item 4 facts

- `render.url_patterns` writes `ssh://<user>@<host>:<ssh_port>/…` only when
  the identity sets `ssh_port`, and **never** writes a port in an `https://`
  pattern.
- Removing a port from a URL makes git connect to the scheme's default
  port. Default ports: ssh 22, https 443, http 80. Only for that port is
  the URL without it the same server.
- Today `https://git.self.test:3000/team/r.git` gets the fix
  `https://git.self.test/team/r.git`, which connects to port 443, not 3000.

### 4.2 Item 4 required behaviour (port repair)

When repair 3 (port removed) yields a URL that `match_url` accepts for
identity `i`, with the removed port `P` and scheme `s`:

- `P` is the default port of `s` (ssh 22, https 443, http 80): unchanged.
  set-url argv to the URL without the port, reason `no port in the URL`.
- `s` is `ssh` and `P` is not 22: no argv.
  - message: `f"{name or 'the remote'} is {redact_url(url)}; it looks like {i.id}, whose patterns have no port {P}"`
  - fix: `f"set ssh_port = {P} on identity {i.id} in the gitpro config, then run: gitpro apply"`,
    and when `i.ssh_port` is set, append
    `f" (it is {i.ssh_port} now; an identity has one ssh_port)"`.
- `s` is `https` or `http` and `P` is not its default: no argv.
  - message: `f"{name or 'the remote'} is {redact_url(url)}; it looks like {i.id}, but gitpro's https patterns never carry a port"`
  - fix: `f"switch {name or 'the remote'} to an ssh URL for {i.id}"`

gitpro never edits the user's TOML ("Suggest, user picks", README); the
fix is text the user acts on.

### 4.3 Item 3 facts (AWS CodeCommit)

From https://docs.aws.amazon.com/codecommit/latest/userguide/setting-up-ssh-unixes.html
(not tested live):

- The documented `~/.ssh/config` entry is
  `Host git-codecommit.*.amazonaws.com`, `User APKAEIBAERJR2EXAMPLE` (the
  SSH key ID), `IdentityFile ~/.ssh/codecommit_rsa`.
- The documented clone URL is
  `ssh://git-codecommit.us-east-2.amazonaws.com/v1/repos/MyDemoRepo`, with
  no user.
- The troubleshooting section documents the user in the URL instead:
  `ssh://Your-SSH-Key-ID@git-codecommit.us-east-2.amazonaws.com/v1/repos/MyDemoRepo`.

So CodeCommit's SSH user is the SSH key ID, never `git`. Today, for an
identity with host `git-codecommit.us-east-2.amazonaws.com` and the default
`ssh_user = "git"`, repair 2 suggests
`ssh://git@git-codecommit.us-east-2.amazonaws.com/v1/repos/r`, which the
server rejects. When the identity sets `ssh_user` to the key ID, repair 2
already suggests the documented `ssh://<key id>@…` form; that is correct
and stays.

A CodeCommit host, for this package: `host` matches
`^git-codecommit\.[a-z0-9-]+\.amazonaws\.com$`, compared case-insensitively.
Other CodeCommit endpoint names (FIPS, other partitions) are not verified
and are out of scope.

### 4.4 Item 3 required behaviour

**url_form.** When repair 2 (user added) is accepted with user `git`, and
the accepted identity `i` has a CodeCommit host: no argv.

- message: `f"{name or 'the remote'} is {redact_url(url)}; it looks like {i.id}, but CodeCommit's SSH user is the SSH key ID, not git"`
- fix: `f'set ssh_user = "<your SSH key ID>" on identity {i.id} in the gitpro config, then run: gitpro apply'`

Repair 2 with any other user, on any host, is unchanged.

**lint.** `lint.warnings` adds, for each identity with a CodeCommit host,
`ssh_user == "git"`, and `ssh_key` or `ssh_alias` set (SSH is in use; an
HTTPS-only CodeCommit identity has no use for `ssh_user`):

`f"identity {i.id!r}: CodeCommit's SSH user is the SSH key ID, not git; set ssh_user to it"`

No warning when `ssh_user` is anything else, or for any other host.

**guide.** In `docs/guide.md`, the identity table row for `ssh_user`,
`ssh_port` gains: `AWS CodeCommit's SSH user is the SSH key ID.`

### 4.5 Tests for items 3 and 4

Intended changes to existing tests (the only ones this package changes):

- `test_url_form_https_port` (QA01-7): `https://github.com:8443/corp-org/app.git`
  now gives `fix_argv is None`, the https message above and fix
  `switch origin to an ssh URL for <id>`. Today it pins the wrong fix
  `https://github.com/corp-org/app.git`.
- `test_url_form_missing_user_and_port`, second half:
  `ssh://git@github.com:2200/corp-org/app.git` now gives `fix_argv is None`
  and fix `set ssh_port = 2200 on identity <id> in the gitpro config, then run: gitpro apply`.
  The first half (`ssh://github.com/…` → `ssh://git@github.com/…`) is
  unchanged.

New tests:

- Default ports keep the argv: `ssh://git@github.com:22/corp-org/app.git`
  → `ssh://git@github.com/corp-org/app.git`;
  `https://github.com:443/corp-org/app.git` →
  `https://github.com/corp-org/app.git`; reason `no port in the URL`.
- ssh non-default port with `ssh_port` already set to another value: the
  `(it is Q now; …)` suffix.
- An identity with `ssh_port = 2222` and URL
  `ssh://git@gitlab.corp.test:2222/grp/sub/app.git` still matches with no
  problem (unchanged).
- `gitpro why` text output for a no-argv url_form shows `✗ <message>` and
  `→ <fix text>` (via `render_text`).
- CodeCommit, identity host `git-codecommit.us-east-2.amazonaws.com`,
  owners `["v1/repos"]`:
  - `ssh_user` default, URL `ssh://git-codecommit.us-east-2.amazonaws.com/v1/repos/r`
    → url_form, no argv, the CodeCommit message and fix.
  - `ssh_user = "APKAEIBAERJR2EXAMPLE"`, same URL → argv to
    `ssh://APKAEIBAERJR2EXAMPLE@git-codecommit.us-east-2.amazonaws.com/v1/repos/r`
    (unchanged behaviour, pinned).
  - `https://git-codecommit.us-east-2.amazonaws.com/v1/repos/r` matches
    with no problem (unchanged).
  - A non-CodeCommit host, `ssh://github.com/corp-org/app.git` → argv
    `ssh://git@github.com/corp-org/app.git` (unchanged).
- lint: the warning for a CodeCommit identity with `ssh_key` and default
  `ssh_user`; none with `ssh_user` set; none without `ssh_key` and
  `ssh_alias`; none for a github.com identity.
- Differential: `DIFF_URLS` already has `ssh://git@github.com:22/corp-org/app.git`.
  Add `https://github.com:8443/corp-org/app.git` (default in git and gitpro).

## 5. Spec 01 update

In `docs/spec/01-config-resolve-why.md`, the `url_form` row of the
PROBLEM_CODES table now reads:

| `url_form` | no pattern matched, but one **would** match after a repair: the scheme `git+ssh://` or `ssh+git://` written as `ssh://`; userinfo removed; a missing user added; the port removed; letter case changed to the identity's (tried in that order, first repair that matches wins; see 18) | `git -C <repo> remote set-url <name> <fixed url>`, or fix text with no argv when that URL would be wrong: a non-default port (set `ssh_port`, or use ssh), or user `git` on AWS CodeCommit (set `ssh_user`) |

## 6. Order of work

1. Item 4 (port), then run the suite.
2. Item 2 (`git+ssh`), which reuses item 4's port rule.
3. Item 3 (CodeCommit: url_form text, lint, guide).
4. Item 1 (letter case), last because it is tried last.
5. Spec 01 row (section 5), in the commit of the item that completes it or
   a final docs commit.

Commit headers (Conventional Commits, 50 characters at most), for example
`fix(resolve): no set-url for a non-default port`.

## 7. Out of scope (known limits)

- URLs that need two repairs (for example userinfo and letter case
  together), except the scheme rewrite of item 2, which combines by design.
- Launchpad's `lp:` shorthand (`url.<base>.insteadOf = lp:`): the remote
  URL git matches is the raw `lp:…`, which no pattern covers. Not
  reported.
- CodeCommit endpoints other than `git-codecommit.<region>.amazonaws.com`,
  and `codecommit::` URLs of git-remote-codecommit.
- A live test against Launchpad or CodeCommit; the facts above come from
  their documentation.
- Whether hosts other than GitHub and GitLab treat owner case as the same.
