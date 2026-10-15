# 19 — `gitpro report`: redacted issue and incident reports

Depends on 01 (`resolve.Report.to_json`, `jsonout`, `args`), 05 (`forge.Session`,
`forge.run`), 06 (guard), 10 (agent hooks), 11 (MCP), 15 (VS Code), 16
(`doctor.check_versions`). Facts below are as of main c3b3ef7.

Files this package may create or edit:
- new: `src/gitpro/report.py`, `src/gitpro/incident.py`, `tests/test_report.py`,
  `tests/test_incident.py`, `vscode/src/report.ts`, `vscode/src/test/report.test.ts`
  (or the extension's existing test folder);
- edit: `src/gitpro/cli.py` (the `EXTENSIONS` list and the help text only),
  `src/gitpro/args.py` (`guarded` only), `src/gitpro/agents.py` (`Decision`, `_judge_one`,
  `_check`, `_fail_open`), `src/gitpro/guard.py` (`_mismatch` and its three callers, the
  hook catch-all at guard.py:191), `src/gitpro/mcp.py` (one new tool, the tool catch-all at
  mcp.py:694), `vscode/package.json`, `vscode/src/features.ts`, the docs in section 12,
  and the tests that pin tool lists and command tables (section 10.8).

Nothing else.

## 0. Rules for this package

1. **No secret ever reaches a report.** This rule overrides every other one.
   - Any path that can't prove its output clean must refuse to send, open or print the draft.
   - A refused report is a correct result. A leaked value is a bug, whatever caused it.
2. No regression. Every existing test passes unchanged, except the pinned lists named in
   10.8, which grow on purpose.
3. Tests first. Before any implementation, the planted-secret tests in 10.1 are written
   and fail.
4. One commit per numbered step in section 11.
   - After each step, run only the test files that step touches, as listed in section 11:
     `env -u FORCE_COLOR .venv/bin/python -m pytest -q -p no:cacheprovider -n auto <files>`.
   - The full suite runs once, in the last step:
     `env -u FORCE_COLOR .venv/bin/python -m pytest -q -p no:cacheprovider -n auto`.
     pytest-xdist is installed in `.venv` only; it is not a project dependency.
   - Baseline on main c3b3ef7: 1661 passed, 2 skipped in about 30 s with `-n auto`
     (about 4 min without it).
5. gitpro itself never contacts the network without `--online`.
   - The default path opens the user's own browser on a prefilled form. The user still
     has to press Submit on GitHub.
   - `[forge] online = true` in the config is not enough here: filing a public issue
     always needs `--online` on the command line.

## 1. Purpose and user story

Users of the CLI, the PyPI package, the VS Code extension and the MCP server need one
short way to tell the maintainers what went wrong, with the facts needed to act on it,
and without exposing who they are or what they work on.

- "gitpro picked the wrong identity. I run `gitpro report`, read the draft, and my
  browser opens a GitHub issue that already has the versions and the shape of `gitpro
  why`, with no names, emails, hosts or paths."
- "gitpro crashed. The error says to run `gitpro report --crash`, and the issue shows
  where in gitpro's code it failed, but nothing from my machine."
- "gitpro blocked my agent's commit, and I think wrongly. `gitpro report --denial` says
  which rule fired."
- "I found a security hole. `gitpro report --security` opens the private advisory form,
  never a public issue."

Reports go to `Yantrakar/gitpro`. The repo is a constant, `REPORT_REPO`; no config key
changes it (section 9).

## 2. CLI surface

```
gitpro report [--crash | --denial | --security] [-m TEXT] [--repo PATH]
              [--client NAME/VERSION] [--print] [--online [--as ID]] [-y] [--json]
```

- **Kind.** At most one of `--crash`, `--denial` and `--security`. With none, the kind
  is `bug`.
- **`-m TEXT`.** The user's own words, at most 2000 characters. It is scanned like every
  other section (5.3).
- **`--repo PATH`.**
  - The repo whose `why` goes in. The default is the current directory.
  - Outside a repo, the section reads `not in a git repository`, and the report goes on.
  - For `--crash` and `--security`, `why` is left out unless `--repo` is given.
- **`--client NAME/VERSION`.** Added to the versions section. Must match
  `^[a-z][a-z0-9-]{0,20}/[0-9A-Za-z.+-]{1,32}$`, else exit 2. The VS Code extension
  passes `vscode-gitpro/<extension version>`.
- **`--print`.** Prints the draft and the URL, and opens nothing.
- **`--online`.** Files the issue with `gh issue create`, as one of the user's own gh
  logins (section 6).
  - Refused with `--security` (exit 2): `✗ security reports go to the private form only;
    run gitpro report --security`.
  - `--as ID` picks the identity whose `gh_account` files it.

The flow:
1. Collect the sections (section 4).
2. Redact them (5.2).
3. Scan the result (5.3).
4. If anything is found, stop: exit 2, the kinds and sections are named, no value is
   printed, and nothing is opened or sent.
5. Print the preview (7.1).
6. Ask. The question depends on the path:
   - Default: `open the prefilled issue form in your browser? [y/N]`.
   - `--online`: `file this as a public issue on github.com/Yantrakar/gitpro as <login>?
     [y/N]`.
   - `--security`: `open the private security advisory form? [y/N]`.
7. `-y` answers yes.
8. Without a terminal and without `-y`, act as `--print`.

Exit codes:
- 0: printed, opened or filed.
- 2: a bad argument; the scan found something; `--crash` with no crash recorded;
  `--denial` with no denial recorded; a gh failure.
- 130: interrupted.

## 3. `--json`

Without `--online -y`, `--json` implies `--print`:

```json
{"ok": true, "command": "report", "kind": "bug", "title": "...", "body": "...",
 "url": "https://github.com/Yantrakar/gitpro/issues/new?title=...&body=...",
 "truncated": false, "sent": null}
```

- For `--security`, `url` is the advisory form (section 6) and the query string is empty.
- `--online -y --json` sets `"sent": {"issue": "https://github.com/Yantrakar/gitpro/issues/N"}`.
  The URL is the one `gh` printed. It must match `^https://github\.com/Yantrakar/gitpro/issues/\d+$`,
  else the run fails.
- When the scan blocks:
  `{"ok": false, "command": "report", "blocked": [{"kind": "email", "section": "message"}]}`,
  exit 2.

## 4. What a report contains (the allowlist)

The body is Markdown, built only from the fields below. A field not listed here is never
read for a report. In particular, a report never contains:
- environment variables, config file text or git config text;
- remote URLs, commit messages or file contents;
- argv values;
- exception messages, or local variables of any kind;
- `fix` and `message` strings from `problems`.

### 4.1 Versions (every kind)

- From `doctor.check_versions` (doctor.py:105-123): `gitpro`, `python`, `os`, `git`, and
  the first comma-separated part of `ssh -V` (`OpenSSH_9.6p1`). `platform.release()` is
  not included.
- The install kind: `pipx` when `sys.prefix` contains `/pipx/venvs/`, else `pip`. The
  path itself never appears.
- `client` when `--client` was given. The MCP tool passes `mcp`.

### 4.2 Redacted `why` (bug, denial; crash and security only with `--repo`)

A projection of `Report.to_json()` (resolve.py:111-124), field by field. `P(kind, v)` is
the placeholder from 5.2.

| field | in the report |
|---|---|
| `ok` | as is |
| `config.how` | as is when it is one of the values `config.find` returns (config.py:191-198: `--config`, `$GITPRO_CONFIG`, `user config`); else `other`. `config.path` is left out |
| `remotes` | the count; per remote: `P(remote, name)` (names `origin` and `upstream` kept), the URL **form** (`https`, `ssh`, `scp`, `file`, `other`), `P(host, host)`, `P(owner, owner)`, `P(repo, repo)` |
| `expected.id` | `P(id, id)`, or `[default]` |
| `expected.name`, `.email` | `P(name, …)`, `P(email, …)`. An email at `users.noreply.github.com` keeps that domain: `email-1@users.noreply.github.com` |
| `expected.ssh_key` | `P(key, path)` and whether the file exists |
| `expected.sign` | as is when it is a bool or one of gitpro's sign values; else `other` |
| `expected.file` | `gitpro include` when it is under gitpro's generated include dir; else `P(file, path)` |
| `expected.match` | `rule` as is when it is an enum value; `remote` as above; `url` and `pattern` in the form/host/owner/repo shape above |
| `effective.<key>` for name, email, signingkey, sshcommand | `P(kind, value)` with the same table, so a value equal to an expected one gets the same placeholder. `sshcommand` is always `P(sshcommand, value)`; its key paths are not shown |
| `effective.gpgsign` | the bool |
| `effective.<key>.scope` | as is when it is one of git's scopes (`system`, `global`, `local`, `worktree`, `command`); else `other` |
| `effective.<key>.origin` | `~/.gitconfig`, `$XDG_CONFIG_HOME/git/config`, `repo .git/config`, `gitpro include`; else `P(file, path)` |
| `effective.pinned` | the bool |
| `problems` | `code` only, kept when it matches `^[a-z_]{1,32}$`, else `other`. These are `resolve.PROBLEM_CODES` plus codes from `EXTRA_CHECKS` (resolve.py:30-36) |

Hosts in `KNOWN_HOSTS` keep their names: `github.com`, `gitlab.com`, `bitbucket.org`,
`codeberg.org`, `git.sr.ht`, `gitea.com`. Every other host is `P(host, …)`.

`KNOWN_HOSTS` is a constant in report.py; a test pins it.

### 4.3 Crash (`--crash`)

The most recent `last-crash.json` record (section 8):
- time, rounded to the minute, UTC;
- the gitpro version that crashed;
- the command path, e.g. `forge check`, taken from the parsed `args.command` and
  subcommand attributes, never from argv;
- the exception's class name;
- the frames.

If the record's version is not the running version, a line says so. If there is no
record: exit 2, `✗ no crash recorded`.

### 4.4 Denial (`--denial`)

The most recent `denials.jsonl` record (section 8):
- time, rounded to the minute, UTC;
- `source` (`guard` or `agent-check`);
- `agent` (an `agents.ADAPTERS` key, or null);
- `action` (`commit`, `push`, `gh pr create` and the like: `agents._what` or the guard
  hook name, no arguments);
- `reason` (section 8);
- `codes`: the problem codes, filtered as in 4.2.

`--repo`, or the current directory, adds the redacted `why` (4.2). That shows the
repo's state now, which may differ from its state at the time of the denial; the report
says so in one line.

### 4.5 Message

`-m TEXT`, under `### What happened`, after the generated sections. Control and bidi
characters are shown escaped, using `forge.safe` (forge.py:206).

### 4.6 Title

- `-m` given: `[<kind>] ` plus the first line of the message, capped at 80 characters.
- Otherwise:
  - bug: `[bug] gitpro <version>: `
  - crash: `[crash] <ExceptionClass> in <command>`
  - denial: `[denial] <reason> on <action>`

The title goes through the same scan.

## 5. Secret safety

There are three independent layers. Each one alone has to stop the planted secrets in
10.1; the tests check that (10.2).

### 5.1 Layer 1: allowlist

Section 4 is the whole input. `report.py` builds each section from typed fields. No
function in it takes a free-form dict or string from another module and copies it into
the body, except `-m`. The crash and denial records are written allowlist-only too, so
the files on disk hold nothing sensitive (section 8).

### 5.2 Layer 2: redaction by kind into stable placeholders

`Redactor`, one per report:
- `P(kind, value)` returns `<kind>-<n>`, numbered per kind in the order first seen.
  An email keeps the domain only when it is a GitHub noreply address: a value ending in
  `@users.noreply.github.com` becomes `email-<n>@users.noreply.github.com`, any other
  email `email-<n>`, so a company address does not read as a GitHub one.
- Equal values get the same placeholder: emails and hosts compared case-insensitively,
  paths after `~` expansion.
- So the maintainer can read `expected email-1, effective email-2 (from repo .git/config)`
  without seeing either email.

The kinds: `id`, `name`, `email`, `host`, `owner`, `repo`, `remote`, `key`, `file`,
`sshcommand`, `account`, `path`.

After the sections are built, the Redactor also rewrites the `-m` text: every value in
the sensitive set (5.3 a), longest first, becomes its placeholder.

### 5.3 Layer 3: the scan (blocks; never rewrites)

Runs on the final title, body and URL, after layers 1 and 2.

(a) **Known values.** The sensitive set is:
- every value the Redactor replaced;
- every identity's `id`, `name`, `email`, `ssh_key`, `signing_key`, `gh_account`,
  `glab_account`, hosts not in `KNOWN_HOSTS`, and owners from the user's config;
- `Path.home()`;
- the login name (`getpass.getuser()`);
- the machine name (`socket.gethostname()` and its first label);
- the repo's absolute path.

A value of 3 or more characters found case-insensitively, at word boundaries, is a hit.
Shorter values are skipped. The list of kinds that matched is all that is reported.

A value equal (ignoring case) to one of gitpro's own words is not searched for, and the
Redactor does not rewrite it in `-m`:
- `PUBLIC_WORDS` in report.py: `yantrakar`, `gitpro`, the `KNOWN_HOSTS`, and every fixed
  label and enum value the sections print (`versions`, `origin`, `local`, `url_form`, ...);
- this draft's own identifiers: the crash's frame function names, file stems, command words
  and exception type, and the denial's action, agent, reason and codes. These come from the
  record files gitpro wrote (section 8), already cleaned to their patterns.

Finding such a value shows nothing private: the word is in every report. Without this, a
repo named `gitpro` or an identity id `local` would block every report. A value that only
contains such a word (`Some gitpro`, `gitpro-fork`) is still searched for. A test renders
each kind of draft and fails if a word gitpro wrote is in neither list.

(b) **Patterns.** Each is a hit:
- the token regexes already in gitpro: `forge._SECRET_RE` and `forge._HEX_TOKEN_RE`
  (forge.py:190-195), `lost._TOKEN_RE` (lost.py:385);
- a URL with userinfo that `resolve.redact_url` would change;
- `-----BEGIN [A-Z ]*PRIVATE KEY-----`;
- an OpenSSH public key (`ssh-ed25519 AAAA`, `ssh-rsa AAAA`, `ecdsa-sha2-`,
  `sk-ssh-ed25519@openssh.com`);
- `AKIA[0-9A-Z]{16}`, `xox[abprs]-`;
- a run of 40 or more base64 or hex characters;
- any email address (the regex in report.py, with one exception: a placeholder
  `email-N@users.noreply.github.com`);
- an absolute path under `/home/`, `/Users/`, `/root/` or `/tmp/`;
- an IPv4 address.

(c) **Encoding.** The body is decoded from the URL (`urllib.parse.parse_qs`) and scanned
again, so an encoding step can't hide a value.

Hits give `[{"kind": ..., "section": ...}]`, where the section is one of `title`,
`versions`, `why`, `crash`, `denial` and `message`. The value is never printed, logged or
returned.

The block message: `✗ report not sent: the draft contains <kinds> in <sections>`, then:
- for `message`: `→ remove it from -m`;
- for any other section: `→ this is a gitpro bug; please report it without the draft:
  gitpro report --print -m "report blocked"`.

A false positive blocks. That is intended (rule 0.1).

### 5.4 What the user types in the browser

Text the user adds in GitHub's form is out of gitpro's reach. The preview ends with
`→ anything you add on GitHub is public; do not paste tokens, keys or private paths`.

## 6. Sending

**Browser (default).**
- The URL is
  `https://github.com/Yantrakar/gitpro/issues/new?title=<t>&body=<b>`, built with
  `urllib.parse.urlencode` (`quote_via=quote`). It has no `labels` and no `template`; the
  repo has no issue templates today (no `.github/ISSUE_TEMPLATE`).
- If the encoded URL is longer than `MAX_URL` = 7000 bytes (gitpro's own limit), the
  body is cut at a section boundary, `truncated: true` is set, and the preview says
  `→ the draft is long; the rest was printed above for you to paste`.
- The browser is opened with `webbrowser.open(url, new=2)` only when both hold:
  - on macOS, or on Linux with `DISPLAY` or `WAYLAND_DISPLAY` set;
  - `webbrowser.open` returns True.

  Otherwise the URL is printed with `→ open this URL in your browser`, and the exit code
  is still 0. This keeps a text browser from taking over an SSH terminal.

**Security.**
- Opens `https://github.com/Yantrakar/gitpro/security/advisories/new` with no query
  string. Private vulnerability reporting was turned on 2026-10-15.
- The redacted draft (versions; `why` only with `--repo`) is printed for the user to
  paste.
- Never `--online`. Never `gh`. Never an issue.

**`--online`.**
1. Without `--online` on the command line, this path is never taken (rule 0.5).
2. Choosing the account:
   - Build `forge.Session(cfg, home, ids=[args.as_] if args.as_ else None)` and keep the
     targets whose `acc.cli == "gh"` and `acc.host == "github.com"`.
   - None: `✗ report --online: no identity has a github.com gh_account; drop --online to
     use the browser`, exit 2.
   - More than one distinct `Account` without `--as`: exit 2, naming the ids.
3. `session.gate("report", "to file a public issue on Yantrakar/gitpro", "the draft
   shown above", explicit=True, args=args)` (forge.py:416). It prints the
   `online: asking …` line. Its non-None return is the exit code.
4. Run `session.run(acc, ["issue", "create", "--repo", "Yantrakar/gitpro", "--title",
   title, "--body-file", "-"])`, with the body on stdin. `forge._exec` passes a `str`
   stdin as `input` (forge.py:107-112). gh's stdout must hold one issue URL that matches
   the regex in section 3.
5. On a non-zero exit: `✗ gh issue create failed (<rc>): <forge.redact(stderr,
   session.secrets())>`, exit 2.

gitpro never reads the token itself; `gh_env` passes the gh login (forge.py:69).

## 7. VS Code, MCP and the preview

### 7.1 Preview (CLI)

The order:
1. The title.
2. The body.
3. A blank line.
4. The line `→ prefilled issue: github.com/Yantrakar/gitpro (you still press Submit)`,
   or `→ files as <login> on github.com, public`.
5. The 5.4 line.

The full URL is printed only for `--print`, `--json`, or when the browser can't open.

### 7.2 VS Code: `gitpro: Report an Issue`

- **Command.** `gitpro.reportIssue`, in `package.json` `contributes.commands`,
  registered next to `runDoctor` (features.ts:53-60).
- **Flow.**
  1. Run `gitpro report --json --print --client vscode-gitpro/<version>` in the active
     workspace folder, through `cli.ts`.
  2. If `ok` is false: an error message naming the blocked kinds and sections, and
     nothing else.
  3. Otherwise, open `body` in an untitled Markdown document, as a preview.
  4. Show a modal: `Open the prefilled GitHub issue form?`, with the actions
     `Open in Browser` and `Copy Draft`.
  5. `Open in Browser` calls `vscode.env.openExternal(vscode.Uri.parse(url, true))`.
     `Copy Draft` uses `vscode.env.clipboard`.
- **Crashes.** When a CLI call's stderr has the crash line (section 8), the extension's
  error notification adds a `Report an Issue` action, which runs the same flow with
  `--crash`.
- **Test.** Stub `openExternal` and assert that the decoded query of the URI it receives
  equals the CLI's `title` and `body`, so nothing is encoded twice.
- No `--online` from VS Code.

### 7.3 MCP: `draft_report`

- **Read-only.** The annotations are the same as the other read-only tools. It is
  enabled by default, with `execution=False`.
- **Arguments.**
  - `path` (string, optional; the repo);
  - `kind` (`bug` | `crash` | `denial`; no `security`);
  - `message` (string, maxLength 2000).
- **Result.** `{"title", "body", "url", "security_url", "truncated"}`.
- **A block** is `isError: true`, with the 5.3 kinds and sections.
- **Description.** "Draft a redacted gitpro bug report (versions, a redacted why, the
  last crash or denial) and the prefilled GitHub issue URL. It never sends or opens
  anything. Show the user the draft and the URL and let them decide; for a security
  problem, give them security_url instead of an issue."
- **Its own output.** The tool's result also goes through `mcp.redact` (mcp.py:569), as
  every tool result does.

## 8. Incident records (`src/gitpro/incident.py`)

`incident.py` imports only the stdlib and `gitpro.__version__`, because hot modules
call it. It repeats the `undo.state_dir()` rule (undo.py:17-22): `$XDG_STATE_HOME/gitpro`
or `~/.local/state/gitpro`, mode 0700. A test asserts the two agree.

Every function here is best-effort. It catches every `Exception` and returns, so a
record can never change an exit code, block a commit or slow a hook beyond one small
file write.

### 8.1 Crashes: `last-crash.json`

- **Format.** One JSON object, overwritten each time, mode 0600 (`os.open` with 0o600):
  ```json
  {"time": "2026-10-15T09:41Z", "version": "0.3.4", "command": "forge check",
   "type": "KeyError",
   "frames": [["gitpro/forge.py", 512, "check"], ["gitpro/args.py", 74, "guarded"]],
   "outside": 2}
  ```
- **Frames.** Taken from `traceback.extract_tb`:
  - only frames whose file is inside the `gitpro` package, as `gitpro/<relative path>`;
  - the count of other frames goes in `outside`;
  - at most 30 frames, innermost last.
- **`type`.** The class's `__qualname__`, kept when it matches `^[A-Za-z_][A-Za-z0-9_.]{0,80}$`,
  else `Exception`.
- **Never stored.** The exception message, locals, argv and paths outside the package.
- **Writers.**
  1. `args.guarded` (args.py:67-82) gets a last clause,
     `except Exception as e`. It:
     - calls `incident.record_crash(command, e)`;
     - prints `✗ internal error (<Type>): this is a gitpro bug` and
       `→ draft a report with: gitpro report --crash` to stderr;
     - returns 2 (today an uncaught exception exits 1 with a traceback);
     - prints the full traceback to stderr only when `GITPRO_DEBUG=1`, a new variable;
     - with `--json`, uses `jsonout.error(command, "internal error (<Type>)")`.
  2. `agents._fail_open` (agents.py:1465), in its `except Exception` branch. Its output
     and exit code don't change.
  3. guard's hook loop (guard.py:191), in its `except Exception` branch. Its output and
     exit code don't change.
  4. MCP's tool catch-all (mcp.py:694), with command `mcp <tool name>`. The
     traceback it writes to stderr and the log stays as it is (test_mcp.py:410).
- **Not covered.** A crash before `guarded` runs (`build_parser`, import errors). That
  is recorded in section 9.

### 8.2 Denials: `denials.jsonl`

- **Format.** One JSON line per denial, mode 0600. When the file passes 20 lines, it is
  rewritten with the last 20.
  ```json
  {"time": "2026-10-15T09:41Z", "version": "0.3.4", "source": "agent-check",
   "agent": "claude", "action": "commit", "reason": "committer", "codes": ["override"]}
  ```
- **`reason`.** One of:
  - `committer`, `author` (the email check);
  - `no_identity` (no match, and `[default]` has no email);
  - `pushed_to` (the push check, agents.py:1329-1332);
  - `problem` (a trigger code with matching emails);
  - `push` (guard.py:328).
- **Changes in agents.py.**
  - `agents.Decision` (agents.py:1108-1119) gains `reason: str = ""` and
    `codes: Tuple[str, ...] = ()`. The defaults keep every existing constructor call
    working.
  - `_judge_one` fills both.
  - `_check` (agents.py:1424-1455) calls `incident.record_denial(...)` on the deny path
    only. `hook = "warn"` is not a denial, so it is not recorded.
- **Changes in guard.py.** `_mismatch` gets the reason from its three callers
  (guard.py:271, 278, 328) and records only when it blocks, i.e. when `on_mismatch` is
  not `"warn"`.
- **Not recorded.** Headline, cause and fix text; emails; ids; paths; the repo.

## 9. Out of scope

- A `[report] repo` config key; reports from forks.
- Labels, issue templates and searching for duplicates.
- Filing private reports through the API (`POST /repos/{owner}/{repo}/security-advisories/reports`).
- Sending anything from VS Code or MCP.
- Telemetry of any kind. Nothing is collected unless the user runs `report`.
- Recording crashes before `guarded` runs.
- glab and GitLab.

## 10. Acceptance tests

### 10.1 Planted secrets (`tests/test_report.py`)

**The fixture.**
- A config whose values are all distinct canaries:
  - identity ids `canaryidalpha` and `canaryidbeta`;
  - names `Canary Personname`;
  - emails `canary.person@canary-corp.example`;
  - host `git.canary-corp.example`, owner `canaryowner`, repo `canaryrepo`;
  - `ssh_key = ~/.ssh/canary_key_file`;
  - `gh_account = canarylogin`.
- A repo under `HOME=/tmp/.../home/canaryuser`, with:
  - remote `https://canaryuser:ghp_CANARYCANARYCANARYCANARYCANARYCANARY12@git.canary-corp.example/canaryowner/canaryrepo.git`;
  - a repo-local `user.email` override;
  - `core.sshCommand` with a canary path.
- `GH_TOKEN=ghp_CANARYENV...` and `AWS_SECRET_ACCESS_KEY=canary...` in the environment.
- A forced crash. In the CLI: a monkeypatched function inside `gitpro` raises
  `KeyError("canary.person@canary-corp.example /tmp/.../canaryuser")`, with a canary
  local variable. In MCP: a tool that does the same.
- Forced denials from agent-check and from the guard, both with canary emails.

**The check.** For every output, assert that no canary substring appears,
case-insensitively, and none of the 5.3 (b) patterns either. The outputs:
1. the stdout and stderr of every kind with `--print`;
2. every `--json`;
3. the URL;
4. the stdin and argv that a stubbed `gh` receives;
5. the URL given to a stubbed `webbrowser.open`;
6. `last-crash.json` and `denials.jsonl`;
7. the result of the MCP tool `draft_report`;
8. the text a VS Code test passes to the stubbed `openExternal` and to the clipboard.

**The placeholders.** The draft still shows the shape: `email-1` against `email-2`, the
`override` code, `https` and `host-1`.

### 10.2 Each layer alone

Three tests, each one disabling two layers by monkeypatch. Each asserts that the
fixture's canaries are still stopped:
- layer 1 bypassed: a test hook passes the whole `to_json()` into the why section;
- layer 2: the Redactor returns values unchanged;
- layer 3: the scan only.

With layers 1 and 2 off, the scan must block and exit 2. Nothing may be sent or opened:
the stubs show no call.

### 10.3 Scan unit tests

- One positive case for each pattern and each known-value kind.
- These negatives: `email-1@users.noreply.github.com`, `github.com`, version strings, and
  `gitpro/forge.py:512`.
- The URL-decode pass catches `%40`-encoded emails.
- The block output never contains the value.
- A known value equal to a public word (`gitpro`, `Yantrakar`, `local`, `origin`,
  `github.com`) or to a `public` identifier is not a hit; `Some gitpro`, `gitpro-fork` and
  `local.host.test` are.
- A repo whose remote is `git@github.com:Yantrakar/gitpro.git` reports in every mode,
  clean, with `scp github.com owner-1/repo-1`.
- Every word of 3 or more letters that gitpro wrote in a draft (all sections but `-m`) is
  in `PUBLIC_WORDS` or the draft's identifiers.
- `P(email, …)` gives `email-N` for a company address and
  `email-N@users.noreply.github.com` for a noreply one.

### 10.4 Paths

- No `gh` call without `--online`, with `[forge] online = true` too.
- `--online` calls gh with exactly the argv in section 6, after a yes; `n` sends nothing.
- `--online --security` exits 2.
- `--security` opens the advisory URL with no query string.
- `--json` without `-y` never sends.
- The account-selection errors in section 6.
- `webbrowser.open` is not called on Linux without `DISPLAY` and `WAYLAND_DISPLAY`.
- A bad gh URL in stdout fails the run.

### 10.5 Records

- File modes are 0600, and 0700 for the directory.
- The denials file keeps 20 lines.
- A record that fails (an unwritable state dir) doesn't change the hook's exit code or
  output.
- warn mode records nothing.
- `incident.state_dir() == undo.state_dir()`.
- The hot-path import check: `python -X importtime -m gitpro agent-check` doesn't
  import `report` or `rich`.

### 10.6 Crash handler

- An unexpected exception in a command exits 2 with the two lines from 8.1, and no
  traceback unless `GITPRO_DEBUG=1`.
- `--json` gives a JSON error.
- `KeyboardInterrupt` still exits 130.
- The existing exit codes of `ConfigError`, `CalledProcessError` and `OSError` are
  unchanged.

### 10.7 Truncation

- A body over `MAX_URL` is cut at a section boundary, `truncated` is true, and the full
  draft is printed.

### 10.8 Pinned lists that change on purpose

- MCP: the default tools go from 13 to 14, and all of them from 16 to 17.
  - test_mcp.py:143-154 (annotations);
  - the llms.txt list (test_docs.py:510-514);
  - the SKILL.md list (test_docs.py:441-445).
- The commands:
  - `test_guide_names_every_command`;
  - the SKILL command table gains `report`;
  - `test_parser_has_every_package`;
  - `test_every_extension_imports`.
- `report` is not in `WRITING` (test_docs.py:25): it writes no local file of its own.
  The table row marks the network as `--online (gh)`.

## 11. Steps (one commit each)

The tests each step runs are given in brackets.

1. Tests from 10.1-10.3, failing: `xfail(strict=True)` until step 4.
   [test_report.py, test_incident.py]
2. `incident.py`, plus the writers in 8.1 and 8.2 (args, agents, guard, mcp), plus 10.5
   and 10.6.
   [test_incident.py, test_agents.py, test_guard.py, test_mcp.py,
   test_cli_core.py]
3. The Redactor and the scan in `report.py`, plus 10.3. [test_report.py]
4. The sections, the preview, `--print`, `--json` and the browser path; remove the
   xfails. [test_report.py, test_docs.py]
5. `--security` and `--online`, plus 10.4. [test_report.py, test_forge.py, test_forge_qa.py]
6. The MCP `draft_report` tool and its pinned lists. [test_mcp.py, test_docs.py]
7. The VS Code command, the crash action and its test. [`npm test` in `vscode/`]
   - This step touches only `vscode/`, and builds against the JSON in section 3, not
     against the Python code.
   - So it runs in parallel with steps 1-6, in its own worktree.
   - It is rebased onto the Python commits before step 8.
8. Docs (section 12), then the final checks:
   - the full suite, as in rule 0.4;
   - `npm test` in `vscode/`;
   - `.venv/bin/python tests/smoke_providers.py`;
   - `gitpro report --print` by hand, in this repo and outside any repo.

## 12. Docs text

- **guide.md.**
  - A `report` row in the command table.
  - A "Reporting a problem" section in Troubleshooting: the four kinds, what goes in, what
    never does, and the two record files.
  - The "Something else" bullet points to `gitpro report`.
- **Privacy text.** In the guide's privacy section (guide.md:738 area) and the site FAQ:
  - gitpro writes `last-crash.json` and `denials.jsonl` in the state dir, and what they
    hold;
  - `report` sends nothing by itself.

  The FAQ sentence "Commands only go online when you pass `--online`" stays true.
- **README.md.** "Problems and questions" names `gitpro report` and
  `gitpro report --security`.
- **SECURITY.md.** Names `gitpro report --security`.
- **SKILL.md.**
  - The command-table row: `| report | no | to open or send | --online (gh) |`.
  - The MCP tool list.
  - A rule: "when gitpro crashes or blocks wrongly, offer `gitpro report`; never file
    for the user without their yes".
- **site/llms.txt and site/index.html.** The tool count and the tool list.
- **`gitpro --help`.** The `problems and questions:` line adds `gitpro report`.
