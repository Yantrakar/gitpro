# 04 — `gitpro discover` and `init --discover`: identities from what is on the machine

Wave 4. Depends on 01 (`config.load_scan`, `cli.extend_command`, `jsonout`,
the minimal `forge.py`: `available`, `logged_in`, `run`, `announce`,
`no_online_error`) and 02 (`scan.find(roots, max_depth=…)`,
`scan.derive_id`).

Files this package may create or edit: new `src/gitpro/discover.py`,
`tests/test_discover.py`. Nothing else; `init --discover` is added through
`extend_command`, not by editing `cli.py`.

## 1. Purpose and user story

A new user has no config but plenty of evidence: editor "recent projects",
`~/.ssh/config` host aliases, `gh` logins, old `includeIf` rules in
`~/.gitconfig`, repos on disk, and (on request) shell history full of `git
clone` lines. `gitpro discover` reads those local artifacts and prints
`[[identity]]` blocks and `[scan] roots` to paste. `gitpro init --discover`
writes the example config with those blocks appended as comments, so the
user only uncomments.

User story: "Fresh laptop restored from backup. `gitpro init --discover
--history` gives me a config that already lists github.com/corp-org from my
history, my `github-work` ssh alias, and both my gh logins. I uncomment,
set emails, `gitpro apply`."

## 2. CLI surface

```
gitpro discover [--source S[,S…]] [--history] [--online] [--json] [-v]
gitpro init [PATH] [--discover] [--history] [--online]
```

### 2.1 Registration

`discover.register(sub, common, confirm)` adds `discover`.
`discover.extend(subs)` calls `extend_command(subs, "init", add_flags,
handler)`: `add_flags` adds `--discover`, `--history`, `--online`;
`handler(args)` returns `None` when `--discover` is absent (the existing
`cmd_init` runs), otherwise it runs `cmd_init` itself through
`cli.cmd_init(args)`, then appends the discovered section (2.4) when the
file was just created, and returns the exit code.

### 2.2 Sources

Default sources: `vscode`, `jetbrains`, `ssh`, `includeif`, `repos`.
`history` is read only with `--history` (DECISIONS Q3). `gh` and `glab`
only with `--online`. `--source` restricts the set further (it does not
enable `history` or `gh`/`glab` without their flags).

| source | what is read | what is extracted |
|---|---|---|
| `history` (`--history`) | `~/.bash_history`, `~/.zsh_history` (extended format `: ts:0;cmd`; zsh "metafied" bytes decoded with the `\x83` unmetafy rule before UTF-8 decoding), `~/.local/share/fish/fish_history` (`- cmd:` lines), `$HISTFILE` if set | lines matching `\b(git|gh|glab)\s+(repo\s+)?clone\b`: the URL → host, owner, repo; the target dir if given → candidate root. **Lines are never stored or printed**, only the sanitised URL (2.3). |
| `vscode` | `~/.config/Code/User/globalStorage/state.vscdb` (also `Code - Insiders`, `VSCodium`, `Cursor`; macOS `~/Library/Application Support/<app>/User/globalStorage/state.vscdb`), via stdlib `sqlite3` opened with the URI `file:<path>?mode=ro&immutable=1` (no copy, no lock, no journal), key `history.recentlyOpenedPathsList` in table `ItemTable` | `folderUri` entries with scheme `file` → if the folder is a repo, its `origin` raw URL; parent dirs → candidate roots. Entries with scheme `vscode-remote` (containers, SSH, WSL) are skipped and counted (`remote_entries`). |
| `jetbrains` | `~/.config/JetBrains/*/options/recentProjects.xml` (macOS `~/Library/Application Support/JetBrains/*/options/`) | `<entry key="$USER_HOME$/…">` paths → same as vscode |
| `ssh` | `~/.ssh/config`, then every file named by an `Include` directive (glob expanded relative to `~/.ssh`, read-only, depth 1), and `~/.ssh/config.d/*`; only `Host`, `HostName`, `User`, `IdentityFile`, `Port`, `Include` directives; the gitpro managed block is skipped | aliases whose `HostName` is a known forge host (`github.com`, `gitlab.com`, `bitbucket.org`, `codeberg.org`, `*.visualstudio.com`, `dev.azure.com`, `ssh.dev.azure.com`, or any host also seen in another source) → `ssh_alias`, `ssh_key`, `ssh_user`, `ssh_port` |
| `includeif` | `git config --global --show-origin --get-regexp '^includeif\.'` | `gitdir:` and `hasconfig:remote.*.url:` conditions → roots (gitdir) and host/owner patterns; the included file's `user.name`/`user.email` (via `git config -f <file> --get`) → identity name/email |
| `repos` | repos under the candidate roots found above plus `[scan].roots` from an existing config (`config.load_scan`), `scan.find(roots, exclude=…, max_depth=3)`, then `scan.inspect` | raw remote URLs → host/owner; local `user.email`/`user.name` per host/owner → name/email candidates |
| `gh`, `glab` | **`--online` only**: `forge.run(acc, ["auth", "status", "--json", "hosts"])` falling back to parsing the text of `gh auth status` when `--json` is rejected; `glab auth status`; tokens never read | logged-in accounts per host → `gh_account` / `glab_account`; the verified emails when `gh api user/emails` succeeds (needs the `user:email` scope; a 403/404 is a `⚠` with `gh auth refresh -s user:email`) |

Hosts seen in one source make an ssh alias for that host count in another:
sources are collected first, then joined.

### 2.3 Sanitising URLs

Every URL from any source passes `sanitise(url) -> str | None` before it
is stored:

1. `urllib.parse` for scheme URLs: drop `username:password@` (userinfo),
   drop the query string and fragment. For scp-like `user@host:path`, the
   part before `@` is kept only if it matches `ID_RE` (an ssh user such as
   `git`), else dropped.
2. Token denylist: if the URL (before or after step 1) contains a substring
   matching `(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{15,}|glptt-[A-Za-z0-9_-]{15,}|gldt-[A-Za-z0-9_-]{15,}|x-access-token:|oauth2:|Authorization`,
   the URL is dropped and counted in `secrets_skipped`.
3. The result is parsed with `resolve.parse_url` (01 §4); unparsable → dropped.

The owner and repo survive; nothing else from the line does.

### 2.4 Output

A summary, then TOML to paste.

```
discovered from: vscode (23 recents, 2 remote entries skipped), ssh (2 aliases), includeif (1 rule), repos (31)
history: not read (add --history to read ~/.zsh_history for git clone lines)

hosts and owners seen:
  github.com    corp-org (18 repos)  me (9)  friend (1)
  gitlab.com    grp/sub (3)
ssh aliases:
  github-work → github.com  key ~/.ssh/id_work
roots:
  ~/src (27 repos)  ~/work (4)
emails seen in repos:
  me@corp.test  (github.com/corp-org ×18)
  me@personal.test  (github.com/me ×9)

# ---- paste into ~/.config/gitpro.toml and fill in the blanks ----
[scan]
roots = ["~/src", "~/work"]

[[identity]]
id = "corp-org"
name = "Work Me"                 # seen in 18 repos
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
ssh_alias = "github-work"        # from ~/.ssh/config
ssh_key = "~/.ssh/id_work"

[[identity]]
id = "me"
name = ""                        # not seen; fill in
email = "me@personal.test"
host = "github.com"
owners = ["me", "friend"]        # friend: 1 repo, check

→ then: gitpro plan
```

With `--history` the second line becomes `history: 412 clone lines in
~/.zsh_history, ~/.bash_history (1 skipped: looked like a secret)`.

Rules for blocks: one block per (host, email) when an email is known; one
per host otherwise with `email = ""  # fill in`. Owners with ≤ 1 repo get a
`# check` comment. `gh_account = "..."` is added only with `--online`. Ids
come from `scan.derive_id(owner, host, taken)` (02; the same rule
`scan.suggest` uses). Owners already covered by an existing config's
identities get `# already in config` and no block. Nothing is written.

`init --discover` runs the same discovery and appends the TOML, every line
commented with `# `, under a header `# ---- discovered on <date> by gitpro
init --discover; uncomment what is right ----` to the newly created file.
If the file already exists: existing message, nothing appended, and the
discover output is printed instead with `→ paste what you need`.

`-v` lists each finding with its source, e.g. `vscode  ~/work/app
git@github.com:corp-org/app.git`. For `history` the `-v` line is `history
git@github.com:corp-org/app.git → ~/work/app` (the sanitised URL and the
target dir only, never the command line).

Capabilities registered: `discover`, `init.discover`.

## 3. `--json` schema

```json
{
  "ok": true, "command": "discover",
  "sources": {"history": null, "vscode": {"files": [...], "entries": 23, "remote_entries": 2},
              "jetbrains": {"files": [], "entries": 0}, "ssh": {"files": [...], "aliases": 2},
              "includeif": {"rules": 1}, "repos": {"roots": [...], "count": 31}, "gh": null, "glab": null},
  "hosts": [{"host": "github.com", "owners": [{"owner": "corp-org", "repos": 18, "emails": {"me@corp.test": 18}, "names": {"Work Me": 18}, "in_config": false}]}],
  "aliases": [{"alias": "github-work", "host": "github.com", "user": "git", "port": null, "key": "/home/me/.ssh/id_work"}],
  "roots": [{"path": "/home/me/src", "repos": 27}],
  "accounts": [{"host": "github.com", "cli": "gh", "login": "me-work", "emails": ["me@corp.test"], "verified": true}],
  "toml": "[scan]\nroots = ...",
  "warnings": []
}
```

With `--history`, `sources.history` is `{"files": [...], "lines": 412,
"secrets_skipped": 1}`.

## 4. Config keys

None read for discovery except, when a config exists: `[scan]` (through
`load_scan`, lenient) and the identities (through `config.load_all`; when
that fails the config is treated as absent with a `⚠`). Produces `[scan]
roots`, `[[identity]]` with `gh_account`/`glab_account`.

## 5. VS Code surface

None here. 15's `openConfig` command offers `Create with discovery`, which
runs `init <file> --discover`; that is 15's code.

## 6. Files created/changed

`init --discover` creates only the config file (O_EXCL, as today). Nothing
else is written: `state.vscdb` is opened `immutable=1`, so no journal or
copy is made.

## 7. Failure modes

| situation | message |
|---|---|
| a source file is unreadable/missing | silently skipped; `-v` lists `vscode: state.vscdb not found` |
| `state.vscdb` schema changed / sqlite error | `⚠ vscode: could not read recents (<error>)`; continue |
| `sqlite3` module missing | `⚠ vscode: python has no sqlite3 module; skipped` |
| `--online`, neither gh nor glab on PATH | `forge.no_online_error("discover", "account discovery", …)`, exit 2 |
| `--online`, gh present but no login | `⚠ gh has no login; account discovery skipped`, `accounts: []` |
| nothing found | `nothing discovered; roots: ~` and an empty-template identity block |
| `--source bogus` | `✗ unknown source 'bogus' (history, vscode, jetbrains, ssh, includeif, repos, gh, glab)` exit 2 |
| `--source history` without `--history` | `✗ history is read only with --history` exit 2 |

Each source has a 5 s budget; history files are read line by line with a
cap of 200 000 lines; `repos` walks at most depth 3 under each root.

## 8. Security / privacy

- Shell history is read only with `--history`. Only `clone` lines are
  looked at; each URL goes through `sanitise` (2.3); the line itself is
  never stored, printed, or placed in JSON. Dropped URLs are counted, not
  shown.
- `~/.ssh/config` and included files: only the directive names and values
  listed above; no key contents; `IdentityFile` paths are printed as given.
- `state.vscdb` is opened read-only and immutable; only the one key is read.
- `gh`/`glab`: only with `--online`; `hosts.yml` is never opened; the token
  is never printed. Nothing is sent anywhere except the `gh auth status`
  and `gh api user/emails` requests with `--online`, which the first line
  announces: `online: asking github.com via gh (account X) for your login
  and verified emails`.

## 9. Out of scope

Browser history; other editors; writing the config beyond `init`; forge
repo listing (05).

## 10. Acceptance tests (`tests/test_discover.py`)

All with `home` (temp HOME) and fixture files written into it; `fake_bin`
for `gh`.

1. `.zsh_history` with extended-format lines: two `git clone` lines for
   `git@github.com:corp-org/a.git ~/work/a` and `https://gitlab.com/grp/sub/b`
   plus a `curl` line; **without** `--history` → `sources.history is None`,
   `hosts` empty, stdout contains `add --history`; **with** `--history` →
   `hosts` has github.com/corp-org and gitlab.com/grp/sub, `roots` contains
   `~/work`, `sources.history.lines == 2`.
2. `--history` with line `git clone https://me:ghp_abcdefghijklmnopqrstuvwxyz@github.com/me/x` →
   dropped: `secrets_skipped == 1`, `me` not in owners, `ghp_` nowhere in
   stdout/stderr/JSON. Line `git clone https://me@github.com/me/y?x=1` →
   owner `me` recorded, JSON `toml` and `-v` output contain neither `me@`
   nor `?x=1`. The raw history line (`curl -H 'Authorization: Bearer
   SECRET'`) never appears in any output.
3. `fish_history` `- cmd: git clone git@github.com:me/y.git` → found with
   `--history`. A zsh history line with metafied bytes (`\x83` escape in
   the URL) decodes to the right owner.
4. A `state.vscdb` created in the test with `sqlite3` holding
   `history.recentlyOpenedPathsList` JSON with two `folderUri` entries
   (`file://…`, one a repo with origin `git@github.com:corp-org/z.git`) and
   one `vscode-remote://…` entry → counted under corp-org, root = its
   parent, `remote_entries == 1`; the file's mtime and size are unchanged
   after the run and no `-journal`/`-wal` file appeared.
5. `recentProjects.xml` with one `$USER_HOME$/proj` entry that is a repo →
   found; non-repo entry ignored.
6. `~/.ssh/config` with `Include config.d/*` and `Include work/*.conf`, a
   gitpro block, and `~/.ssh/work/corp.conf` holding `Host github-work /
   HostName github.com / IdentityFile ~/.ssh/id_work` → one alias; the
   block's hosts are not listed; `sources.ssh.files` contains both files.
7. `~/.gitconfig` with `[includeIf "gitdir:~/work/"] path = ~/.gitconfig-work`
   and that file setting name/email → identity block has that name/email
   and `~/work` is a root.
8. Repo under a root with local `user.email` override → email counted for
   its owner. A repo four levels under the root is not visited (`max_depth=3`).
9. Existing config covering corp-org → `in_config == true`, text has
   `# already in config`, no block for it.
10. `--online` with fake `gh` answering `auth status --json hosts` (JSON)
    and `api user/emails` → `accounts` entry with `verified: true`,
    `gh_account` in TOML; `fake_bin.calls("gh")` has no `auth token`
    call; first line starts with `online:`. Variant: fake rejects
    `--json` on `auth status` (exit 1, `unknown flag`) and prints the text
    form → same result.
11. `init --discover` in empty home → file created `0600`, contains the
    example and a commented discovered section; `config.load_scan` on it
    returns an empty `[scan]` without error and `config.load` fails only
    on the example's placeholders (comments are ignored).
12. `init --discover` when file exists → `already exists`, file unchanged,
    discovery printed.
13. Empty home → `nothing discovered`, exit 0; JSON has empty lists.
14. `--source bogus` → exit 2; `--source history` without `--history` → exit 2.
15. `--online` with no `gh`/`glab` on PATH → exit 2 with the `needs gh or
    glab` sentence.

## 11. Modules touched

New `discover.py` (`collect(sources, *, history, online, home) -> dict`,
`sanitise(url)`, `render_toml(d) -> str`, `register`, `extend`). Reuses
`scan.find`, `scan.inspect`, `scan.derive_id`, `config.load_scan`,
`forge.run`.

## 12. Docs text (for 17)

> `gitpro discover` reads your editor recents, `~/.ssh/config`, existing
> `includeIf` rules and the repos on disk, and prints ready-to-paste
> `[[identity]]` blocks and scan roots. Add `--history` to also read `git
> clone` lines from your shell history (only the repo URLs are kept, with
> any credentials removed; the lines themselves are never shown). `gitpro
> init --discover` creates the config with them commented in. Add
> `--online` to include your `gh`/`glab` logins. Nothing leaves the machine
> without `--online`; tokens are never read.
