# 09 — Key health, `gitpro auth`, `keys --upload`

Wave 3. Depends on 01 only (`gh_account`/`glab_account`, `jsonout`,
`cli.extend_command`, the minimal `forge.py`: `accounts`, `run`,
`announce`, `no_online_error`; `keys.inventory` from 00).

Files this package may create or edit: new `src/gitpro/health.py`,
`tests/test_health.py`. Nothing else: `keys.py` is reused, not edited;
the new `keys` flags are added through `extend_command`.

## 1. Purpose and user story

`gitpro keys` lists keys and says "register this". It does not say whether
the key is weak, unprotected, unused, already registered, or whether ssh
actually authenticates as the right account. This package adds those checks
and an optional upload through `gh`/`glab`.

User story: "`gitpro keys --health` tells me `id_work` has no passphrase
and `id_old_rsa` is 1024-bit RSA. `gitpro auth` shows that `github-work`
authenticates as `me-work` and `github.com` as `me`. `gitpro keys --upload
--online` registers the new key on the right account."

## 2. CLI surface

```
gitpro keys [--generate] [--no-passphrase]                              # unchanged
gitpro keys --health [--online] [-c PATH] [--json]
gitpro keys --upload [--identity ID]… [--title T] [--online] [-y] [--json]
gitpro auth [--identity ID]… [--all] [-c PATH] [--json] [--timeout S]
```

Registration: `health.register` adds `auth`. `health.extend(subs)` calls
`extend_command(subs, "keys", add_flags, handler)` where `add_flags`
adds `--health`, `--upload`, `--online`, `--identity`, `--title`,
`--json`; `handler` returns `None` when neither `--health` nor `--upload`
is given (existing `cmd_keys` runs), else the health/upload exit code.

### 2.1 `keys --health`

Per key from `keys.inventory`:

| check | how | result text |
|---|---|---|
| exists | `.pub` and private file present | `missing -- run: gitpro keys --generate` (as today) |
| perms | private `0600` or `0400`; `.pub` not group/world-writable; `~/.ssh` `0700` (`0755` or `0750` → `⚠ ~/.ssh is readable by others; ssh accepts it, 0700 is safer`); world-writable `~/.ssh` → `✗` | `✗ accessible by others -- chmod 600 <path>` |
| type/size | `ssh-keygen -l -f <pub>` → `"<bits> <fp> <comment> (<TYPE>)"` | `✓ ed25519`; `✗ RSA 1024: too weak; generate ed25519` for RSA < 2048 and DSA; `⚠` for RSA 2048–3071 and ECDSA (`bits` for ECDSA is the curve size: 256/384/521, reported as `ecdsa-256` etc., not "weak"); `ed25519-sk`/`ecdsa-sk` → `· hardware key` |
| passphrase | `ssh-keygen -y -P "" -f <private>` with `stdin=DEVNULL`, 5 s timeout: exit 0 → **no passphrase**; exit ≠ 0 with `incorrect passphrase` in stderr → has a passphrase; other failure → `?` | `⚠ no passphrase -- ssh-keygen -p -f <path>` |
| agent | `ssh-add -l` lists the fingerprint | `· loaded in ssh-agent` / `· not in ssh-agent`; passphrase-protected key **not** in the agent → state `locked`: `⚠ has a passphrase and is not in ssh-agent: every git push will prompt, GUI clients and agents may fail → ssh-add <path>` |
| shared | two identities on the same host with different `gh_account`/`glab_account` share the key | `⚠ shared by work-github and personal on github.com; GitHub allows a key on one account only` |
| registered | **`--online`**: `gh ssh-key list` (authentication keys) and signing keys (see note) for the identity's `gh_account`; `glab ssh-key list --output json` (falls back to the text table); compare public key material (type + base64, ignoring the comment) | `✓ registered on github.com (me-work) as auth + signing` / `✗ not registered on github.com (me-work) → gitpro keys --upload --identity work-github --online` / `⚠ registered on github.com (me) but identity work-github maps to me-work` |

Note on signing keys (open question, README §13): `gh ssh-key list`
prints both authentication and signing keys in one table with a type
column in current gh; older versions print only authentication keys. The
parser reads the type column when present (`authentication`/`signing`)
and otherwise reports `signing: null` with `⚠ gh too old to list signing
keys`. Where the forge is GitLab, `glab ssh-key list` has a `usage_type`
field in JSON.

Output:

```
~/.ssh/id_personal   ed25519  auth + signing  →  github.com (default)
  ✓ passphrase  ✓ perms  · loaded in ssh-agent
~/.ssh/id_work       ed25519  auth + signing  →  github.com: corp-org [work-github]
  ⚠ no passphrase -- ssh-keygen -p -f ~/.ssh/id_work
  ✓ perms  · not in ssh-agent
~/.ssh/id_old        rsa 1024  auth  →  gitlab.com (default)
  ✗ RSA 1024 is too weak; generate a new key: gitpro keys --generate after changing ssh_key
registered: run with --online to check via gh/glab
2 keys fine, 1 warning, 1 problem
```

Exit 1 when any `✗`, else 0.

### 2.2 `gitpro auth`

Runs `ssh -T` per distinct (alias-or-host, key) pair that gitpro's ssh
config produces (`ssh -T -o BatchMode=yes -o ConnectTimeout=10 [-i key -o
IdentitiesOnly=yes] <ssh_user>@<host-or-alias>`), parses the greeting:

| forge | success pattern | login group |
|---|---|---|
| GitHub | `Hi (\S+)! You've successfully authenticated` | 1 |
| GitLab | `Welcome to GitLab, @(\S+)!` | 1 |
| Bitbucket Cloud | `authenticated via ssh key.*logged in as (\S+)` or `logged in as (\S+)` | 1 |
| Bitbucket Server / Data Center | `authenticated via ssh key\.` (no login) | — |
| Codeberg/Gitea/Forgejo | `Hi there, (\S+)!` | 1 |
| Azure DevOps | `remote: Shell access is not supported` | (no login) |
| other | exit 0 or 1 with no recognised line → `connected (login unknown)` |

Compared with the identity's `gh_account`/`glab_account` when set.

```
online: connecting with ssh to github.com and github-work (ssh -T); sends your public key, nothing else
  github.com    key ~/.ssh/id_personal  →  Hi me!                 ✓ matches gh_account me
  github-work   key ~/.ssh/id_work      →  Hi me-work!            ✓ matches gh_account me-work
  gitlab.com    key ~/.ssh/id_work      →  Permission denied (publickey)   ✗ key not registered on gitlab.com → gitpro keys --upload --identity gitlab-work --online
```

`--identity` limits; `--all` includes `ssh_hosts` of the default. Timeout
`--timeout` (default 30 s per host). Exit 1 when any `✗`. Host key
prompts: `BatchMode=yes` makes an unknown host key fail; gitpro prints
`✗ host key for github.com is not in ~/.ssh/known_hosts; connect once by
hand: ssh -T git@github.com (compare the fingerprint with the provider's
published one)`. It never passes `StrictHostKeyChecking=no`. A
passphrase-protected key not in the agent fails under `BatchMode`; the
result is `locked` with the `ssh-add` hint, not `denied`.

`auth` always goes online (that is its purpose); its first line says so;
no `--online` flag (README principle 4 names this exception). `ssh` not
on PATH → exit 2.

### 2.3 `keys --upload`

For each identity (or `--identity`) whose key is not registered (per the
`--online` check), after confirmation:

```
upload ~/.ssh/id_work.pub to github.com account me-work as auth key "me@corp.test laptop" and signing key? [y/n]
✓ uploaded ~/.ssh/id_work.pub to github.com (me-work): auth, signing
↩ undo: gh ssh-key delete <id> --yes   (ids printed per key)
```

Via `forge.run(acc, ["ssh-key", "add", pub, "--title", title])` and
`["ssh-key", "add", pub, "--type", "signing", "--title", title]` when the
identity signs; `glab ssh-key add <pub> --title ... [--usage-type
signing]`. The title is `--title` or the key comment or `gitpro <hostname>`.
Requires `--online` and `-y` or a TTY. Never uploads a key already present
(compares key material). Never uploads to an account the identity does not
map to. A scope error (`HTTP 404`/`403` mentioning scopes) prints `⚠ gh
token for <login> lacks the admin:public_key (and admin:ssh_signing_key)
scope; run: gh auth refresh -h <host> -s admin:public_key -s
admin:ssh_signing_key`. The key ids for undo come from the `gh ssh-key
list` call after the add (gh does not print the id on add).

Capabilities registered: `keys.health`, `keys.upload`, `auth`.

## 3. `--json`

`keys --health --json`:

```json
{"ok": false, "command": "keys", "online": false,
 "keys": [{"path": "/home/me/.ssh/id_work", "pub": "...pub", "exists": true, "type": "ed25519", "bits": 256, "fingerprint": "SHA256:...",
           "uses": ["github.com: corp-org [work-github]"], "sign": true, "passphrase": false, "perms_ok": true, "in_agent": false,
           "state": "ok"|"locked"|"weak"|"missing", "weak": false, "registered": null|{"auth": true, "signing": false|null, "account": "me-work"},
           "problems": ["no passphrase"], "fixes": ["ssh-keygen -p -f /home/me/.ssh/id_work"]}],
 "ssh_dir_mode": "0700",
 "summary": {"fine": 2, "warnings": 1, "problems": 1}}
```

`auth --json`: `{"ok", "command": "auth", "results": [{"host": "github-work",
"real_host": "github.com", "key": "...", "login": "me-work"|null,
"expected": "me-work"|null, "status": "ok"|"mismatch"|"denied"|"locked"|
"unknown_host"|"timeout"|"error", "message": "Hi me-work!..."}]}`.

`keys --upload --json` (needs `-y`): `{"ok", "command": "keys",
"uploaded": [{"key", "host", "account", "types": ["auth","signing"], "ids":
[...], "undo": "gh ssh-key delete ..."}]}`.

## 4. Config keys

`gh_account`, `glab_account` (identity and default); existing `ssh_key`,
`ssh_alias`, `ssh_hosts`, `ssh_user`, `sign`.

## 5. VS Code surface

None (15's tooltip shows the key path; 16's doctor runs `health.check_keys`).

## 6. Files created/changed

None locally. `--upload` creates keys on the forge; undo is the printed
`gh ssh-key delete` command (gitpro does not delete keys remotely).

## 7. Failure modes

| situation | message | exit |
|---|---|---|
| `ssh-keygen` missing | `✗ ssh-keygen not found` | 2 |
| `ssh-keygen -y` fails for another reason (unsupported format) | `passphrase: "?"`, `⚠ could not probe <path>: <stderr line>` | — |
| `ssh-add -l` fails (no agent) | `· no ssh-agent` info | — |
| `--online`/`--upload`, neither gh nor glab on PATH | `forge.no_online_error`, exit 2 | 2 |
| `--online`, gh present, host without login | `⚠ no gh login for <host>; registered check skipped`, `registered: null` | — |
| `auth` timeout | `✗ github.com: timed out after 30 s` | 1 |
| `--upload` without `--online` | `✗ keys --upload needs the network: … → --online` | 2 |
| `--upload` scope missing | the `⚠ … lacks the admin:public_key …` line, nothing uploaded, exit 1 | 1 |

## 8. Security / privacy

- The private key is never opened by gitpro; `ssh-keygen -y -P ""` is the
  only probe and its stdout (the public key) is discarded. The probe runs
  with `stdin=DEVNULL` and a timeout so it can never prompt or hang.
- `auth` sends what `ssh -T` sends (the public key signature handshake).
- `--online` registered check sends nothing but the list request;
  comparison is local.
- `--upload` sends the public key only, to the account the identity maps
  to, after an explicit confirmation naming the account.
- Fingerprints are printed; key material only in `--json` `pub` paths, not
  contents.

## 9. Out of scope

Rotating keys, deleting remote keys, GPG, agent forwarding, Windows
agents.

## 10. Acceptance tests (`tests/test_health.py`)

Fake executables on `fake_bin` (Python fakes, 01 §7): `ssh-keygen`
(handles `-l -f` → prints `256 SHA256:abc comment (ED25519)`, `1024
SHA256:def c (RSA)`, `256 SHA256:ghi c (ECDSA)` by file name; `-y -P ""
-f` → exit 0 for `id_nopass`, exit 255 with `incorrect passphrase supplied`
for others; logs `stdin=` to prove DEVNULL), `ssh-add` (`-l` →
fingerprint list), `ssh` (`-T` → greeting by host from argv, logs argv),
`gh` (`ssh-key list` → fixed table with a type column; `ssh-key add` →
logs; `auth status` → ok).

1. Two existing keys, one `id_nopass` → health lists `⚠ no passphrase`
   for it with the `ssh-keygen -p` fix; the other `✓ passphrase`; JSON
   `passphrase` false/true; the `ssh-keygen -y` call logged `stdin=`
   empty.
2. Key named `id_old` → RSA 1024 → `✗ … too weak`, exit 1, `weak: true`,
   `state: "weak"`. ECDSA 256 → `⚠`, not weak, `type: "ecdsa-256"`.
3. Private key mode `0644` → `✗ accessible by others`, `perms_ok: false`;
   mode `0400` → ok. `~/.ssh` mode `0755` → `⚠ … 0700 is safer`, exit 0;
   `0777` → `✗`.
4. `ssh-add -l` lists the first key's fingerprint → `in_agent` true/false
   respectively; passphrase key not in agent → `state: "locked"` and the
   `ssh-add` hint; no agent (fake exits 2) → info line, no failure.
5. Two identities on github.com sharing `ssh_key` with different
   `gh_account` → `⚠ shared by` line.
6. `--online`: fake `gh ssh-key list` contains key A's material (type
   `authentication`) but not as signing → key A `registered: {"auth":
   true, "signing": false}`; key B absent → `✗ not registered` with upload
   hint; calls show `env:GH_TOKEN=set` and no token on argv. Fake without
   a type column → `signing: null` and the `gh too old` warning.
7. `--online` with no `gh`/`glab` → exit 2 with the `needs gh or glab`
   sentence.
8. `auth`: fake `ssh -T git@github-work` prints `Hi me-work! You've
   successfully authenticated…` on stderr exit 1 → status `ok`, login
   `me-work`, matches `gh_account`; `gh_account = "other"` → `mismatch`,
   exit 1. Identity with `ssh_user = "gitlab"` → argv target
   `gitlab@<alias>`.
9. `auth`: fake prints `Permission denied (publickey)` → `denied` with
   upload hint; `Host key verification failed` → `unknown_host` with the
   known_hosts advice; a passphrase key not in the agent + `Permission
   denied` → `locked`; argv never contains `StrictHostKeyChecking=no` and
   contains `-o BatchMode=yes`.
10. `auth`: GitLab greeting `Welcome to GitLab, @jane.doe!` → login
    `jane.doe`; Bitbucket `logged in as jane` → `jane`.
11. `keys --upload --online -y --identity work-github` with key B
    unregistered → calls show `ssh-key add <pub> --title …` twice (auth,
    `--type signing`), stdout has `✓ uploaded` and `↩ undo: gh ssh-key
    delete`; already-registered key A → no add call. `--title "laptop"` →
    the title argument is `laptop`. Fake `ssh-key add` exits 1 with `HTTP
    404 … admin:public_key` → the scope warning, exit 1.
12. `keys --upload` without `--online` → exit 2; without `-y` and no tty →
    `no terminal to confirm on`, no `add` call.
13. `keys` (no flags) output unchanged from 00 §4.5 (existing test passes);
    `keys --help` lists `--health`.

## 11. Modules touched

New `health.py` (`check_keys(cfg, *, online) -> dict`, `auth(cfg, ids,
timeout) -> dict`, `upload(...)`, `parse_greeting(text) -> (forge,
login)`, `register` for `auth`, `extend` for the `keys` flags).

## 12. Docs text (for 17)

> `gitpro keys --health` checks each key: type and size, passphrase, file
> modes, ssh-agent (a passphrase key that is not loaded will prompt on
> every push), and (with `--online`) whether it is registered on the right
> account via `gh`/`glab`. `gitpro auth` runs `ssh -T` for every host and
> alias and shows which login each one gets. `gitpro keys --upload
> --online` registers missing public keys on the mapped account.
