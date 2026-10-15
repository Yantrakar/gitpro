# 01 — Shared foundation: config schema, dispatch, `resolve`, `why`, helpers, fixtures

Wave 1, built alone. Every other package depends on this one. Nothing in
this package needs another package.

## 1. Purpose

Package 01 delivers everything the other packages share, so that they can
be built in parallel without editing the same files:

1. **Dispatch**: `cli.py` registers commands from a list of modules. 01
   writes the full list (one line per package); modules that do not exist
   yet are skipped, so no later package edits `cli.py` at all.
2. **Hot path**: `gitpro guard …`, `gitpro agent-check`, `gitpro prompt`
   start without importing `rich` or `cli.py`.
3. **Config schema**: every new key in README section 7, validated here;
   other packages only read dataclass fields.
4. **`resolve.py`**: expected vs effective identity for a repo. The one
   place that judges "is this repo right?".
5. **Helpers**: `files.atomic_write`, `jsonfile.edit_json`, `undo.write`,
   `jsonout`, minimal `forge` runner, `guard.HOOKS` contract, `plan.build`
   taking a `Config`.
6. **Version and capabilities**: `0.2.0.dev0` in one place; `gitpro
   --version --json` lists what this build can do.
7. **Fixtures** in `tests/conftest.py` for all packages.
8. User-visible: `gitpro why [DIR]`, `which --json`, `check --json`.

User story: "I am in `~/work/app`. `git log` shows my personal email on my
last commit. `gitpro why` tells me the repo's own `.git/config` sets
`user.email`, that the work identity would apply otherwise, and the command
that fixes it."

## 2. Command registration and dispatch (`cli.py`)

### 2.1 The pattern

`cli.py` after this package, essential parts:

Shared argparse pieces live in a stdlib-only module so hot modules can use
them without importing `cli.py`:

```python
# src/gitpro/args.py — stdlib only
from __future__ import annotations
import argparse

CAPABILITIES: set = set()          # filled by register(); see 2.3


def capability(*names: str) -> None:
    CAPABILITIES.update(names)


def common() -> argparse.ArgumentParser:    # -c/--config, hidden --home   (as today, moved here)
    ...

def confirm() -> argparse.ArgumentParser:   # -y/--yes                    (as today, moved here)
    ...
```

`cli.py` re-exports them (`from gitpro.args import CAPABILITIES,
capability, common, confirm`) so existing references keep working.

```python
# src/gitpro/cli.py
import argparse, importlib, os, sys
from gitpro.args import CAPABILITIES, capability, common, confirm   # re-exported

# The umask gitpro was started with, captured once at import (before main()
# sets 0o077). 07 passes it to `subprocess.run(["git", "clone", …],
# umask=CALLER_UMASK)` (Python ≥ 3.9) so cloned files get the user's usual
# modes. hot.py sets the same name.
CALLER_UMASK = os.umask(0o077)

# One line per package, alphabetical by module name. 01 writes the whole
# list up front; a module that is not built yet is skipped (see
# _extension_modules). No later package edits this file.
EXTENSIONS = [
    "gitpro.agents",        # 10: agent-check, agent
    "gitpro.audit",         # 08: audit
    "gitpro.clone",         # 07: clone
    "gitpro.completion",    # 16: completion
    "gitpro.devcontainer",  # 13: devcontainer
    "gitpro.discover",      # 04: discover, init --discover
    "gitpro.doctor",        # 16: doctor
    "gitpro.envcmd",        # 12: env
    "gitpro.forge",         # 05: forge (01 ships the module; 05 adds the command)
    "gitpro.guard",         # 06: guard
    "gitpro.health",        # 09: auth, keys --health/--upload
    "gitpro.lost",          # 03: lost
    "gitpro.mcp",           # 11: mcp
    "gitpro.policy",        # 14: policy, team, check --policy
    "gitpro.prompt",        # 16: prompt
    "gitpro.resolve",       # 01: why
    "gitpro.scanroots",     # 02: scan -c/--roots
]


def _extension_modules():
    """Import every listed module; skip the ones that do not exist yet.
    Only a ModuleNotFoundError *for that exact name* is skipped, so a typo
    inside a module still fails loudly."""
    for name in EXTENSIONS:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name == name:
                continue
            raise
        if hasattr(mod, "register") or hasattr(mod, "extend"):
            yield mod                         # forge.py exists from 01 but has neither until 05;
                                              # scanroots (02) has only extend


def build_parser():
    p = argparse.ArgumentParser(prog="gitpro", ...)
    p.add_argument("--version", action="store_true", help="print the version")
    p.add_argument("--json", dest="version_json", action="store_true", help=argparse.SUPPRESS)
    # dest="version_json": subcommands have their own --json (dest "json"); the two must not collide
    common_p, confirm_p = common(), confirm()
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    # existing commands, unchanged except: each gets set_defaults(func=cmd_<name>)
    sp = sub.add_parser("init", ...); ...; sp.set_defaults(func=cmd_init)
    ...
    mods = list(_extension_modules())
    for mod in mods:
        reg = getattr(mod, "register", None)
        if reg:
            reg(sub, common_p, confirm_p)
    for mod in mods:                          # second pass: flags on existing commands
        ext = getattr(mod, "extend", None)
        if ext:
            ext(sub.choices)
    return p, sub.choices


def extend_command(subs, name, add_flags, handler) -> None:
    """Let a package add flags to an existing command without editing cmd_<name>.
    add_flags(parser) adds the options. handler(args) returns an int to take over,
    or None to fall through to the original command."""
    p = subs[name]
    add_flags(p)
    orig = p.get_default("func")

    def func(args):
        rc = handler(args)
        return orig(args) if rc is None else rc
    p.set_defaults(func=func)


def main(argv=None) -> int:
    if os.name != "posix":
        print("✗ gitpro supports Linux and macOS only", file=sys.stderr); return 2
    p, subs = build_parser()
    args = p.parse_args(argv)
    if args.version:
        return cmd_version(args)
    if os.geteuid() == 0:
        return _fail("run as the user who owns these identities, not root", args)
    try:
        if args.command is None:
            return cmd_status(p, subs)
        return args.func(args)
    except config.ConfigError as e:
        return _fail(str(e), args)
    except subprocess.CalledProcessError as e:
        return _fail(f"{_sh(e.cmd)} failed ({e.returncode})", args)
    except (OSError, UnicodeError) as e:
        return _fail(str(e), args)
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr); return 130


def _fail(msg: str, args) -> int:
    if getattr(args, "json", False):
        jsonout.error(getattr(args, "command", None) or "gitpro", msg)   # stdout, nothing on stderr
    else:
        print(f"✗ {msg}", file=sys.stderr)
    return 2
```

Rules:

- `COMMANDS` is removed. The no-argument status output (`cmd_status`)
  derives its usage lines from `subs`, as it already does.
- A module's `register(sub, common, confirm)` adds its subparser(s) with
  `parents=[common]` / `[common, confirm]` as needed and calls
  `sp.set_defaults(func=<callable taking args, returning int>)`. Hot
  modules (06, 10, 16) put their options in `add_arguments(p)` so that
  `register` is `add_arguments(sub.add_parser(name, parents=[common,
  confirm]))` and `hot.main` can build the same parser standalone (2.2).
- The umask is set at import (`CALLER_UMASK`), not in `main()`; tests that
  import `cli` run with 0o077 too.
- A module's optional `extend(subs: dict[str, ArgumentParser])` adds flags
  to an existing command through `cli.extend_command`. Owners of extension
  flags (fixed here so no two packages touch one command):

| existing command | flags added | by |
|---|---|---|
| `scan` | `-c/--config`, `--roots` (root selection, excludes) | 02 |
| `init` | `--discover` | 04 |
| `keys` | `--health`, `--upload`, `--online`, `--identity`, `--json` | 09 |
| `check` | `--policy`, `--range`, `--repo` | 14 |

- **Import cost rule**: a module listed in `EXTENSIONS` may import at
  module level only the standard library and `gitpro.config`,
  `gitpro.jsonout`, `gitpro.undo`, `gitpro.files`. Everything else
  (`rich`, `gitpro.view`, `gitpro.resolve`, `gitpro.scan`, …) is imported
  inside the function that needs it. `register` must not run git or read
  files. Test 29 enforces this.
- The error path in `main()` turns `ConfigError`, `CalledProcessError`,
  `OSError`, `UnicodeError` into exit 2; with `--json` the error object goes
  to stdout and nothing to stderr. The non-POSIX refusal happens before
  parsing and cannot be JSON (documented); the root refusal can.
- `cmd_*` functions keep taking `args`. Existing ones are unchanged apart
  from `set_defaults(func=…)`.

### 2.2 Hot path (`hot.py`)

`pyproject.toml`: `gitpro = "gitpro.hot:main"`. `src/gitpro/__main__.py`
calls `hot.main()` too.

```python
# src/gitpro/hot.py — nothing but stdlib at module level
from __future__ import annotations
import importlib, os, sys

CALLER_UMASK = os.umask(0o077)      # same meaning as cli.CALLER_UMASK; cli reads this value when imported later

HOT = {                      # subcommand -> module with add_arguments(p) and main(argv) -> int
    "guard": "gitpro.guard",          # 06
    "agent-check": "gitpro.agents",   # 10
    "prompt": "gitpro.prompt",        # 16
}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in HOT and os.name == "posix" and os.geteuid() != 0:
        name = HOT[argv[0]]
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:              # a typo inside the module is not hidden
                raise
            mod = None                      # package not built yet: fall through to cli
        if mod is not None:
            return mod.main(argv)
    from gitpro.cli import main as cli_main
    return cli_main(argv)
```

Contract for a hot module (`guard`, `agents`, `prompt`):

- `add_arguments(p: argparse.ArgumentParser) -> None` adds every option
  and sub-subcommand of the command. `register(sub, common, confirm)` is
  `add_arguments(sub.add_parser("<name>", parents=[common, confirm]))`
  plus `capability(...)`.
- `main(argv) -> int` builds `argparse.ArgumentParser(prog="gitpro
  <name>", parents=[args.common()])` (plus `args.confirm()` when the
  command has `-y`), calls `add_arguments` on it, parses `argv[1:]` and
  runs. So `gitpro guard --help` through `cli.py` and `gitpro guard check …`
  through `hot.py` accept the same arguments.
- It reproduces the root-level behaviour it skips: on an error it prints
  `✗ <msg>` to stderr, or the JSON error object to stdout when `--json`
  was given, and exits 2 (the same `_fail` rules as `cli.main`; `hot`
  has a stdlib copy). Subcommands that are not hot (`guard install`,
  `guard uninstall`) may import `view`/`rich` inside their functions; the
  module stays importable without them.
- It never imports `gitpro.cli`, `gitpro.view` or `rich` at module level,
  and `args`, `config`, `resolve`, `jsonout`, `undo`, `files`, `plan`,
  `render` never import them either. Tests 29/30 enforce this with
  `python -X importtime`.

The root refusal for non-POSIX and root runs happens in `hot.main` before
dispatch, as shown (it falls to `cli.main`, which prints the message).

### 2.3 `gitpro --version [--json]`

```
$ gitpro --version
gitpro 0.2.0.dev0
$ gitpro --version --json
{"ok": true, "command": "version", "version": "0.2.0.dev0", "python": "3.12.3",
 "capabilities": ["check.json", "which.json", "why", "why.config"]}
```

`version` is read from `gitpro.__version__` (the one source; `pyproject`
is dynamic, as today). `capabilities` is the sorted `args.CAPABILITIES`
set (re-exported as `cli.CAPABILITIES`). Each `register` (and `extend`) adds names: the subcommand name, plus
`<cmd>.<feature>` entries for things a client might probe (`clone.dry-run`,
`scan.roots`, `check.json`, …). 01 registers `which.json`, `check.json`,
`why`, `why.config`. The VS Code extension (15) and the skill read this
list; there is no minimum-version rule.

Exit 0. No config needed; no git call.

Version bump (this package): `src/gitpro/__init__.py`,
`vscode/package.json`, `.claude-plugin/plugin.json`,
`.claude-plugin/marketplace.json` → `0.2.0.dev0`. 17 sets `0.2.0`. Test 28
checks all four agree.

### 2.4 `gitpro which [DIR] [--json]`

Text output as `00-existing.md` 4.6, with:

- Not a repo: `✗ <dir>: not a git repository` on stderr, exit 2 (was
  `(none)`/`(unset)`, exit 0).
- Origins under home are tilde'd in text (`[file:~/.gitconfig-identities]`).
- `core.sshcommand` is printed with that spelling (git's canonical
  lowercase), not `core.sshCommand`.

`--json`:

```json
{
  "ok": true, "command": "which",
  "repo": "/home/me/work/app",
  "remotes": {"origin": "git@github.com:corp-org/app.git"},
  "settings": {
    "user.name":       {"value": "Work Me", "origin": "file:/home/me/.gitconfig-work-github", "scope": "generated"},
    "user.email":      {"value": "me@corp.test", "origin": "file:/home/me/.gitconfig-work-github", "scope": "generated"},
    "user.signingkey": {"value": null, "origin": null, "scope": null},
    "commit.gpgsign":  {"value": null, "origin": null, "scope": null},
    "core.sshcommand": {"value": "ssh -i /home/me/.ssh/id_work -o IdentitiesOnly=yes", "origin": "file:/home/me/.gitconfig-work-github", "scope": "generated"}
  }
}
```

`remotes` holds the **raw** URLs from `git config --get-regexp
'^remote\..*\.url$'` (what `includeIf` matches), not the `insteadOf`
rewritten form. `which` works without a config file.

### 2.5 `gitpro why [DIR] [-c PATH] [--json]`

Needs a config. All is well:

```
~/work/app
  config    ~/.config/gitpro.toml (user config)
  remotes   origin git@github.com:corp-org/app.git
  expected  work-github  Work Me <me@corp.test>
            matched origin by pattern git@github.com:corp-org/**
  effective Work Me <me@corp.test>   [~/.gitconfig-work-github]
  ssh key   ~/.ssh/id_work   via core.sshcommand
  signing   on   ~/.ssh/id_work.pub
✓ identity matches
```

Repo-local override:

```
~/work/app
  config    ~/.config/gitpro.toml (user config)
  remotes   origin git@github.com:corp-org/app.git
  expected  work-github  Work Me <me@corp.test>
            matched origin by pattern git@github.com:corp-org/**
  effective Work Me <old@personal.test>   [.git/config]
  ssh key   ~/.ssh/id_work   via core.sshcommand
  signing   off
✗ user.email is set in the repo's .git/config and overrides the work-github identity
→ git -C ~/work/app config --unset user.email        (or: gitpro scan --fix --repo ~/work/app)
```

No identity, empty `[default]`:

```
~/src/foo
  config    ~/.config/gitpro.toml (user config)
  remotes   origin git@gitlab.com:someone/foo.git
  expected  (default)  no name/email in [default]; git will refuse to commit
            no identity pattern matches git@gitlab.com:someone/foo.git
  effective (unset)
✗ no identity for git@gitlab.com:someone/foo.git
→ add an identity: gitpro scan --suggest --repo ~/src/foo
```

Remote in a form no pattern covers (userinfo, missing user, port while the
identity has none):

```
✗ origin is https://me@github.com/corp-org/app.git; the work-github patterns need https://github.com/corp-org/… (no user in the URL)
→ git -C ~/work/app remote set-url origin https://github.com/corp-org/app.git
```

Pinned repos show `effective … [.git/git-identities (pin)]` and `· pinned`;
a stale pin is `pin_stale` with `→ gitpro pin ~/work/app`.

Exit codes: 0 match, 1 any problem, 2 error (not a repo, no config).
`--json` is `Report.to_json()` (section 4) plus `"config": {"path": "/abs",
"how": "user config"}` (15 watches that path).

### 2.6 `gitpro check --json`

```json
{"ok": true, "command": "check", "changes": [{"action": "update", "path": "/home/me/.gitconfig-identities", "mode": null}], "up_to_date": false}
```

Exit 1 when `up_to_date` is false, as today.

## 3. Config schema (`config.py`)

### 3.1 Dataclasses

All existing dataclasses stay frozen. New fields and tables:

```python
@dataclass(frozen=True)
class Trailers:
    co_authored_by: Optional[str] = None            # "allow" | "forbid" | "require" | None = inherit
    forbid_emails: Optional[Tuple[str, ...]] = None

@dataclass(frozen=True)
class Default:      # existing fields +
    gh_account: Optional[str] = None
    glab_account: Optional[str] = None
    clone_dir: Optional[str] = None
    trailers: Trailers = Trailers()

@dataclass(frozen=True)
class Identity:     # existing fields +
    gh_account: Optional[str] = None
    glab_account: Optional[str] = None
    clone_dir: Optional[str] = None
    policy: Optional[str] = None
    trailers: Trailers = Trailers()

@dataclass(frozen=True)
class Scan:   roots: Tuple[str, ...] = ("~",); exclude: Tuple[str, ...] = (); stale_days: int = 180
@dataclass(frozen=True)
class Guard:  hooks: bool = False; on_mismatch: str = "block"; chain_to: str = ""
@dataclass(frozen=True)
class Clone:  dir: Optional[str] = None; layout: str = "{host}/{owner}/{repo}"
@dataclass(frozen=True)
class Forge:  online: bool = False; similar: bool = False
@dataclass(frozen=True)
class Agents: hook: str = "block"; bot_emails: Tuple[str, ...] = ("*[bot]@users.noreply.github.com", "noreply@anthropic.com")
@dataclass(frozen=True)
class Policy: files: Tuple[str, ...] = ()

@dataclass(frozen=True)
class Config:
    default: Default
    identities: Tuple[Identity, ...]
    scan: Scan; guard: Guard; clone: Clone; forge: Forge; agents: Agents; policy: Policy
    path: Path
    how: str
```

Construction rules (the existing `_build(cls, table, where)` builds frozen
instances from a dict, so this is where new behaviour goes):

- Lists in TOML become tuples before `cls(**kwargs)`; nested tables
  (`trailers`) are built first with `_build_nested(Trailers, table.get
  ("trailers", {}), f"{where}.trailers")` and passed in. `git` extra
  settings stay a `dict` (mutable, as today; documented: `Identity.git` is
  the one non-frozen field and nobody mutates it).
- The per-identity `file` default is applied after construction with
  `dataclasses.replace(i, file=f"~/.gitconfig-{i.id}")` (frozen-safe).
- `config.load(path)` keeps returning `(Default, list[Identity])` for the
  existing callers and tests. New: `config.load_all(path, how) -> Config`
  and `config.load_scan(path) -> Scan` (see 3.3). `cli._load_all(args) ->
  Config` next to the existing `_load`.

### 3.2 Validation

Messages follow `<where>: <problem>`:

- `[guard]: on_mismatch must be "block" or "warn"`
- `[clone]: layout may use only {host} {owner} {repo} {id} and [A-Za-z0-9._/-]`
- `[scan]: roots must start with ~/ or /` (`~` alone accepted)
- `[scan]: stale_days must be a positive integer`
- `[default.trailers]: co_authored_by must be "allow", "forbid" or "require"`
- `identity 'x': gh_account must match [A-Za-z0-9][A-Za-z0-9._-]*`
- `identity 'x': policy must start with ~/ or /`
- `[agents]: hook must be "block", "warn" or "off"`
- `[guard]: chain_to must start with ~/ or /` (`PATH_RE`), and must not be
  gitpro's own hooks dir (`~/.config/gitpro/hooks`)
- Unknown keys use the existing `_build` format: `<where>: unknown key(s)
  foo (allowed: agents, clone, default, forge, guard, identity, policy,
  scan)`.
- Globs in `exclude`, `forbid_emails`, `bot_emails`: strings without
  control characters. `*_account`: `ID_RE`. `policy`, `clone_dir`, `files`:
  `PATH_RE`.

`example.toml` gains commented examples of every new key and fixes the
`ssh_port` comment (known issue 13).

### 3.3 Lenient scan config

`config.load_scan(path) -> Scan` parses the TOML and validates only the
`[scan]` table. It is for `scan` (02) and `lost` (03), which must keep
working on a config that `init` just created with placeholders. On any
other error it returns `Scan()` and the caller prints `⚠ <path>: <error>;
scanning ~` once.

## 4. `resolve.py`

```python
@dataclass(frozen=True)
class Match:
    identity: Optional[Identity]     # None → [default]
    remote: Optional[str]            # remote whose url matched ("origin"); None when nothing matched
    url: Optional[str]               # the raw url that matched
    pattern: Optional[str]           # the url_patterns() entry that matched
    rule: str                        # "matched origin by pattern git@github.com:corp-org/**" / "no identity pattern matches <url>"

@dataclass(frozen=True)
class Expected:
    id: Optional[str]; name: Optional[str]; email: Optional[str]
    ssh_key: Optional[str]           # absolute path, or None: [default] has no key (render writes no sshCommand for it)
    sign: bool
    file: Optional[str]              # identity.file (expanded) or ~/.gitconfig-identities
    match: Match

@dataclass(frozen=True)
class Setting:
    value: Optional[str]
    origin: Optional[str]            # as git prints it after normalisation: "file:/abs/path", "command line:", "standard input:"
    scope: Optional[str]             # "repo" | "pin" | "worktree" | "generated" | "global" | "system" | "command" | "other"

@dataclass(frozen=True)
class Effective:
    name: Setting; email: Setting; signingkey: Setting; gpgsign: Setting; sshcommand: Setting
    pinned: bool
    remotes: Dict[str, str]          # raw urls, name → url (only *.url, never pushurl)

@dataclass(frozen=True)
class Problem:
    code: str; message: str; fix: Optional[str]

@dataclass(frozen=True)
class Report:
    repo: Path; expected: Expected; effective: Effective; problems: Tuple[Problem, ...]
    def ok(self) -> bool
    def to_json(self) -> dict

def is_repo(path: Path) -> Optional[Path]                     # toplevel (or git dir for bare) or None
def remotes(repo: Path) -> Dict[str, str]
def parse_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]   # host, owner, repo — for display and clone only
def wildmatch(pattern: str, text: str) -> bool
def match_url(url: str, cfg: Config) -> Tuple[Optional[Identity], Optional[str]]  # (identity, pattern)
def expected(repo: Path, cfg: Config, *, remote: Optional[str] = None) -> Expected
def effective(repo: Path, *, env: Optional[Mapping[str, str]] = None) -> Effective
def compare(repo: Path, cfg: Config, *, remote: Optional[str] = None,
            env: Optional[Mapping[str, str]] = None) -> Report
def own_emails(cfg: Config) -> Set[str]                       # every email in [default] and identities

EXTRA_CHECKS: List[Callable[[Report], Iterable[Problem]]] = []   # appended to by 14 (policy); see 4.3
```

`remote=` is a remote **name** or a **URL**: when the value is not a key
of `remotes(repo)`, it is used as the URL to match (git passes the URL as
the remote name to `pre-push` for `git push <url>`). `env=` is the
environment to inspect for overrides (4.2); `None` means `os.environ`.
Hooks pass a copy with the variables git itself exports removed (06).

### 4.1 Matching: the same rule as git

`match_url` does what git's `includeIf "hasconfig:remote.*.url:<pattern>"`
does, with the same inputs:

- Patterns are `render.url_patterns(identity)` — the strings that
  `apply` wrote into `~/.gitconfig-identities`. No second grammar.
- The text is the **raw** remote URL from `git config --get-regexp
  '^remote\..*\.url$'` (git matches before `insteadOf` rewriting; verified
  with git 2.43: an `insteadOf` alias does not make an includeIf on the
  alias pattern apply to an https remote).
- `wildmatch` implements git's wildmatch with `WM_PATHNAME` semantics,
  verified against git 2.43 with `hasconfig`: `*` and `?` do not match
  `/`; matching is case-sensitive; the whole string must match
  (`git@github.com:corp-org/sub/app` does not match `…/sub/app.git`).
  `**` is special **only as a whole path component**: `/**/`, a leading
  `**/`, or a trailing `/**` match any number of components (including
  none, so `corp-org/**` matches `corp-org/app` and `corp-org/app.git`).
  Anywhere else `**` is two ordinary `*`, i.e. `[^/]*` — verified (V-001):
  `git@github.com:**` does **not** match `git@github.com:o/r.git`, while
  `git@github.com:*/**` does. Implementation: translate to a regex
  component by component, `re.fullmatch`.
- `render.url_patterns` follows the same rule (fixed in this package, listed
  in `00-existing.md`): for an identity with no owners it emits
  `<user>@<host>:*/**` and `<user>@<host>:*` (and the `ssh://`/`https://`
  forms with `/*/**` and `/*`), never a bare `<host>:**`.
- Later identity in the file wins (git applies includes in order and the
  later include overrides). Across remotes: any remote URL may match, as
  for git. `remote=` restricts to one remote (pre-push).
- `[default]` applies when nothing matches.

`parse_url` (host, owner, repo) is for display, `clone` (07) and
`discover` (04); it is **not** used for matching. It handles
`git@host:owner/repo(.git)`, `ssh://[user@]host[:port]/owner/repo`,
`https://[user@]host/owner/repo`, `host:path`, nested owners (everything
between host and the last segment), and returns `(None, None, basename)`
for local paths and `file://`.

### 4.2 Effective values

One git call:

```
git -C <repo> config -z --show-scope --show-origin --get-regexp '^(user\.(name|email|signingkey)|commit\.gpgsign|core\.sshcommand)$'
```

plus `git -C <repo> config -z --get-regexp '^remote\..*\.url$'` and
`git -C <repo> rev-parse --path-format=absolute --git-dir --git-common-dir
--show-toplevel`. `-z` output gives `scope\0origin\0key\nvalue\0` groups
(git prints `scope` before `origin`). Scope mapping:

| git scope | origin | gitpro scope |
|---|---|---|
| `local` | file is `<git-common-dir>/git-identities` | `pin` |
| `local` | anything else | `repo` |
| `worktree` | | `worktree` |
| `global` | file whose first line is the generated marker (`render.is_generated`, cached per path) | `generated` |
| `global` | otherwise | `global` |
| `system` | | `system` |
| `command` | `command line:` or `GIT_CONFIG_*` env | `command` |
| other | | `other` |

Origin paths: git prints global origins absolute, but a relative `file:`
origin (`.git/config`, `.git/git-identities`) is relative to the
**work-tree top level** (`--show-toplevel`), not to the git dir, even when
git is run from a subdirectory with `-C` (verified, V-003). For a bare
repo (no top level) it is relative to the git dir. gitpro resolves every
relative `file:` origin against that base, taken from the same `rev-parse`
call. Worktrees: `.git` may be a file; the common dir is used for the pin
file check.

`gpgsign` null is read as `false` when comparing. The last value git
prints for a key is the effective one (git prints every definition;
gitpro keeps the last).

Environment: `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_EMAIL`, `GIT_AUTHOR_NAME`,
`GIT_COMMITTER_NAME`, `GIT_CONFIG_COUNT` are inspected by name in `env`
(the `env=` argument, default `os.environ`) and produce `env_override`
when they differ from the expected values. Inside `pre-commit` and
`commit-msg` git **always** exports `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`
(verified, V-004), so a hook must not pass the raw environment: 06 passes
a copy with those removed and judges the author through
`Context.author_email` instead. `resolve` always runs git with `GIT_DIR`,
`GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_PREFIX`, `GIT_COMMON_DIR` removed
from the child environment and `-C <repo>`, so a call from inside a hook
about another repo is not redirected.

### 4.3 Problems

Checked in this order; all applicable ones are reported.

| code | when | fix |
|---|---|---|
| `no_identity` | expected email is None (nothing matched and `[default]` has no email) | `add an identity: gitpro scan --suggest --repo <repo>` |
| `url_form` | no pattern matched, but one **would** match after a repair: the scheme `git+ssh://` or `ssh+git://` written as `ssh://`; userinfo removed; a missing user added; the port removed; letter case changed to the identity's (tried in that order, first repair that matches wins; see 18) | `git -C <repo> remote set-url <name> <fixed url>`, or fix text with no argv when that URL would be wrong: a non-default port (set `ssh_port`, or use ssh), or user `git` on AWS CodeCommit (set `ssh_user`) |
| `override` | effective name/email scope is `repo` or `worktree` and the value differs from expected | `git -C <repo> config --unset user.<key>` + `(or: gitpro scan --fix --repo <repo>)` |
| `pin_stale` | pinned and the pin file's name/email differ from expected | `gitpro pin <repo>` |
| `global_override` | scope `global`/`system`/`other` sets name or email that beats expected (the generated file is missing from `~/.gitconfig` or not included last) | `gitpro apply` |
| `not_applied` | expected file does not exist, or `~/.gitconfig` has no gitpro block | `gitpro apply` |
| `key_mismatch` | expected `ssh_key` is set and `core.sshcommand`'s `-i <path>` (parsed with `shlex.split`, `~` expanded with `os.path.expanduser` before comparing, as ssh does) is a different path, or `core.sshcommand` is unset | `gitpro apply` (or the override command when the scope is `repo`) |
| `sign_mismatch` | `commit.gpgsign` (null = false) differs from expected `sign`, or expected sign is true and `user.signingkey` is not the expected `.pub` path | same |
| `env_override` | an env var above differs from expected, or a setting's scope is `command` | `unset GIT_AUTHOR_EMAIL GIT_COMMITTER_EMAIL` (naming the ones set) |

| `policy` | reserved for 14: a team policy rule fails for this repo | from the rule |

`key_mismatch` is never raised when the expected identity has no
`ssh_key`. After the built-in checks, `compare` calls each function in
`resolve.EXTRA_CHECKS` with the report so far and appends what it yields;
the list is empty in 01 and 14 appends its check at import time
(`resolve.EXTRA_CHECKS.append(policy.check_report)`), so `why`, `scan`
and the guard show policy problems without 01 knowing 14. `compare` runs
three git calls and no network; target < 100 ms.

### 4.4 JSON

```json
{
  "ok": false, "command": "why",
  "repo": "/home/me/work/app",
  "config": {"path": "/home/me/.config/gitpro.toml", "how": "user config"},
  "remotes": {"origin": "git@github.com:corp-org/app.git"},
  "expected": {"id": "work-github", "name": "Work Me", "email": "me@corp.test",
               "ssh_key": "/home/me/.ssh/id_work", "sign": true, "file": "/home/me/.gitconfig-work-github",
               "match": {"remote": "origin", "url": "git@github.com:corp-org/app.git", "pattern": "git@github.com:corp-org/**",
                         "rule": "matched origin by pattern git@github.com:corp-org/**"}},
  "effective": {"name": {"value": "Work Me", "origin": "file:/home/me/.gitconfig-work-github", "scope": "generated"},
                "email": {"value": "old@personal.test", "origin": "file:/home/me/work/app/.git/config", "scope": "repo"},
                "signingkey": {"value": null, "origin": null, "scope": null},
                "gpgsign": {"value": null, "origin": null, "scope": null},
                "sshcommand": {"value": "ssh -i /home/me/.ssh/id_work -o IdentitiesOnly=yes", "origin": "file:/home/me/.gitconfig-work-github", "scope": "generated"},
                "pinned": false},
  "problems": [{"code": "override", "message": "user.email is set in the repo's .git/config and overrides the work-github identity",
                "fix": "git -C /home/me/work/app config --unset user.email"}]
}
```

`ok` is `problems == []`. `fix` is one shell-quoted command, or `null`.

## 5. Shared helpers

### 5.1 `jsonout.py`

```python
def emit(command: str, obj: dict, *, ok: bool = True) -> None   # {"ok", "command", ...obj}, json.dumps(indent=2, ensure_ascii=False)
def error(command: str, message: str) -> None                  # {"ok": false, "command", "error"}
def wants_json(args) -> bool
```

`scan --json` gains `"ok": true, "command": "scan"` (added in `cmd_scan`,
nothing else changes).

### 5.2 `files.atomic_write` (public)

`files._atomic_write` is renamed `atomic_write(path: Path, text: str,
mode: int) -> None` (the old name stays as an alias for one release).
Temp file in the same directory, `fsync`, `rename`; writes through a
symlink to its target's directory; never replaces a symlink.

### 5.3 `undo.py`

```python
def state_dir() -> Path                            # $XDG_STATE_HOME/gitpro or ~/.local/state/gitpro, mkdir 0700
def write(command: str, lines: List[str], *, what: str) -> Path
def tilde(path: Path) -> str
```

`write` creates `<state>/<slug>-undo-<YYYYmmdd-HHMMSS>[-n].sh`, where
`slug` is `command` with every run of characters outside `[A-Za-z0-9]`
replaced by `-` and trimmed (`"scan"` → `scan-undo-…`, `"guard install
--repo"` → `guard-install-repo-undo-…`), with
`os.open(O_WRONLY|O_CREAT|O_EXCL, 0o700)` (the `-n` suffix is chosen by
retrying on `FileExistsError`, not by an `exists()` loop). Content:

```sh
#!/bin/sh
# puts back what 'gitpro <command>' changed on <date>   (<what>)
<line 1>
<line 2>
```

No `set -e`: every line runs; a failing line prints the shell's error
and the rest still runs. Each line is shell-quoted by the caller
(`shlex.join`). `scan.fix` switches to `undo.write("scan", lines,
what="removed repo-local user settings")`; the file name pattern and the
first two lines keep the existing wording (`# puts back what 'gitpro scan
--fix' removed on …` is produced by passing `command="scan --fix"`-style
text in `what`; the test checks the `git -C` lines and the shebang only).

### 5.4 `jsonfile.py` — JSON and JSONC settings-file editor

Used by 10 (agent settings), 11 (MCP registration), 13 (devcontainer).

```python
@dataclass(frozen=True)
class Result:
    path: Path; changed: bool; diff: str; backup: Optional[Path]; undo: Optional[Path]; text: str

def read(path: Path, *, jsonc: bool = False) -> Tuple[object, str]
    # (parsed object, original text). jsonc=True strips // and /* */ comments outside strings
    # and trailing commas before json.loads. Raises ConfigError("<path>: cannot parse (<error>)").

def edit_json(path: Path, mutate: Callable[[object], object], *, command: str, jsonc: bool = False,
              create: Optional[object] = None, confirm: Callable[[str], bool], indent: int = 2) -> Result
```

Behaviour:

1. Read (or start from `create` when the file does not exist; refuse when
   `create is None`).
2. `new = mutate(copy.deepcopy(obj))`; `mutate` returns the new object
   (or the same object mutated).
3. Serialise: `json.dumps(new, indent=<detected: the file's own indent
   when 2 or 4 spaces or a tab, else 2>, ensure_ascii=False)` + `\n`,
   keeping key order (`dict` insertion order; untouched keys keep their
   order because `mutate` edits in place). When `jsonc=True` and the file
   has comments, `edit_json` refuses to re-serialise:
   `ConfigError("<path> has comments; gitpro cannot edit it without
   losing them — add this by hand: <snippet from the caller>")`. (Textual
   insertion is not attempted; the caller prints the snippet.)
4. `diff` is a unified diff of old vs new text. If equal → `changed=False`,
   nothing written, no backup, no undo.
5. `confirm(diff)` → False → return `changed=False` with the diff (caller
   prints `✗ aborted`).
6. Re-read the file and compare with the text read in step 1; if it
   changed meanwhile → `ConfigError("<path> changed while gitpro was
   running; nothing written")`.
7. Backup to `<undo.state_dir()>/backup-<YYYYmmdd-HHMMSS>/<basename>`
   (directory 0700, file 0600; never next to the edited file, so no stray
   `.bak` lands in a repo or in a settings folder that tools watch), undo
   script via `undo.write(command, ["cp -p <backup> <path>"])` (or `rm -f
   <path>` when created), then `files.atomic_write(path, text, 0o600 if
   path is under ~/.ssh or ~/.config/gh else the original mode or 0644)`.

Writes through symlinks. When `create` is given and the parent directory
does not exist, `edit_json` creates it (`mkdir(parents=True, mode=0o700)`)
only if it is under the user's home or the current repo; otherwise
`ConfigError("<dir> does not exist")`. There is no `mkdir=` parameter.

### 5.5 Minimal `forge.py` (01 part)

Used by 03 (`repo_exists`), 04 (`accounts`), 09 (`run` for `ssh-key
list`/`add`), 12 (no calls; names only), 14/16 (status). 05 adds the
`forge` subcommands and anything else on top, in the same file (05 is the
only later editor of `forge.py`).

```python
@dataclass(frozen=True)
class Account: host: str; cli: str; login: str          # cli: "gh" | "glab"

def available(cli: str) -> bool                           # shutil.which
def accounts(cfg: Config) -> List[Account]                # from [default] and identities; no network
def gh_env(host: str, login: str) -> Dict[str, str]
    # copy of os.environ minus GH_TOKEN, GITHUB_TOKEN, GH_ENTERPRISE_TOKEN, GITHUB_ENTERPRISE_TOKEN,
    # plus GH_HOST=<host> and exactly one of: GH_TOKEN (host github.com) or GH_ENTERPRISE_TOKEN (any other host)
    # set to the output of ["gh", "auth", "token", "-u", login, "-h", host] (run with the stripped env,
    # stdin=DEVNULL, timeout 30 s). Raises ConfigError("gh has no login for <login> on <host>; run: gh auth login -h <host>")
    # when that fails. The token is never on argv, never logged, never returned to callers except inside this dict.
def logged_in(acc: Account) -> bool
    # gh: gh_env() succeeds and ["gh", "api", "user", "--jq", ".login"] == login (case-insensitive)
    # glab: ["glab", "auth", "status", "-h", host] exit 0 and its text names the login
def run(acc: Account, argv: List[str], *, timeout: int = 30, stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess
    # gh:   ["gh", *argv] with env gh_env(host, login)
    # glab: ["glab", *argv] with env minus GITLAB_TOKEN plus GITLAB_HOST=<host>   (active login; see 05 for the mismatch warning)
    # capture_output=True, text=True, never check=True, never shell
def repo_exists(acc: Account, owner_repo: str) -> Tuple[str, Optional[str]]
    # ("ok", None) | ("renamed", "new/name") | ("missing", None) | ("unknown", "<first stderr line>")
    # gh: ["repo", "view", owner_repo, "--json", "nameWithOwner"]; "missing" only when stderr contains
    # "Could not resolve to a Repository"; any other failure is "unknown". Name compare is case-insensitive.
    # glab: ["repo", "view", owner_repo, "--output", "json"] (field path_with_namespace).
def announce(what: str, accounts: List[Account], sends: str) -> None
    # prints "online: asking <host> via <cli> (account <login>[, ...]) <what>; sends <sends>" on stdout
def no_online_error(command: str, what: str, accounts, sends: str) -> int
    # prints "✗ <command> needs the network: it asks … ; sends …" + "→ re-run with --online, or set [forge] online = true"
    # on stderr and returns 2. Every command that needs --online uses this, so the exit code is 2 everywhere.
```

The token may require the user's keyring (`gh` on some systems prompts);
`gh auth token` is run with `stdin=DEVNULL` and a 30 s timeout, and a
timeout becomes `✗ gh auth token timed out (keyring locked?)`. `-u` needs
gh 2.40 (2023-11); when stderr contains `unknown shorthand flag` or
`unknown flag`, the error is `✗ gh 2.40 or newer is needed for multiple
accounts (found: <gh --version first line>)` (12 and 05 reuse this).

### 5.6 `guard.HOOKS` contract (defined here, filled by 06)

So that 08 and 14 never edit `guard.py`, the hook entry table is a fixed
list of dotted names, written by 06 and **including** the entries 08 and
14 will provide:

```python
# in guard.py (06 creates the file; this table is part of its spec)
HOOKS = {
    "pre-commit": ["gitpro.guard:identity_commit", "gitpro.policy:hook_commit"],
    "pre-push":   ["gitpro.guard:identity_push",   "gitpro.policy:hook_push"],
    "commit-msg": ["gitpro.trailers:hook_message", "gitpro.policy:hook_message"],
}
```

Each entry is resolved lazily (`importlib.import_module` + `getattr`);
an entry whose module does not import (package not built) or lacks the
attribute is skipped silently. Signature every entry implements:

```python
def hook_x(ctx: "guard.Context") -> int   # 0 = pass, 3 = block; anything else = "warn, do not block"

@dataclass(frozen=True)
class Context:
    hook: str                 # "pre-commit" | "pre-push" | "commit-msg"
    repo: Path
    args: Tuple[str, ...]     # git's hook arguments
    stdin: bytes              # pre-push: the ref lines; else b""
    cfg: Config
    report: "resolve.Report"  # resolve.compare(repo, cfg, remote=<pre-push remote name or None>)
    committer_email: str      # from `git var GIT_COMMITTER_IDENT`
    author_email: str         # from `git var GIT_AUTHOR_IDENT`
    say: Callable[[str], None]   # prints to stderr
```

`report` is built with `env=` set to a copy of `os.environ` minus
`GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_AUTHOR_DATE` (git exports them
in every commit hook, V-004); the author is judged only through
`author_email` and the own-emails rule (06 §2).

The block exit code is **3** (`guard.BLOCK = 3`). The hook scripts (06)
treat only 3 as "block"; every other non-zero status is reported and
ignored.

### 5.7 `plan.build(cfg: Config, env)`

`plan.build(cfg, env) -> (targets, stale)` takes a `Config` (callers that
have `(Default, list[Identity])` wrap them with `config.Config(...)`;
`cli._load_all` is the normal route). 06 adds hook targets inside
`build` under `if cfg.guard.hooks:`. `plan.teardown(env)` is unchanged.

### 5.8 `render.is_generated`

Accepts the marker on line 1, **or** a `#!` shebang on line 1 and the
marker on line 2 (hook scripts, 06). `files.diff`/`apply` use the same
function, so generated hook scripts are managed files like any other.

## 6. Python 3.9 rules (all packages)

`pyproject` says `>=3.9`. Every new module starts with `from __future__
import annotations`. Not allowed: `match` statements, `zip(strict=)`,
`dataclass(slots=…, kw_only=…)`, `datetime.UTC`, `X | Y` in runtime
expressions (`isinstance(x, A | B)`), `str.removeprefix` is fine (3.9),
`tomllib` without the `tomli` fallback (use `config._toml`). Test 31 runs
`python -W error -c "import ast, …"` over `src/gitpro` and fails on
`match` statements and `|` in `isinstance`; the rest is review.

## 7. Test fixtures (`tests/conftest.py`, 01 only edits this file)

```python
@pytest.fixture
def fake_bin(tmp_path, monkeypatch) -> FakeBin:
    """A directory prepended to PATH.
    fake_bin.add("gh", body) writes an executable *Python* script (shebang = sys.executable)
    that first appends one line to fake_bin.log("gh") — shlex.join(argv) + " env:GH_TOKEN=set|unset"
    + " env:GH_ENTERPRISE_TOKEN=set|unset" + " env:GITLAB_TOKEN=set|unset" + " stdin=<bytes read>" —
    then runs `body`, a Python snippet with `argv`, `stdin` (bytes) and `env` in scope that may print
    and must `sys.exit(rc)` (default 0). Never a shell string.
    fake_bin.calls("gh") -> list[list[str]] parses the log back."""

@pytest.fixture
def cfg(home, tmp_path) -> Callable[..., Path]:
    """cfg(text=None, *, apply=True): writes a config (default: two identities work-github
    (github.com, owners corp-org, key ~/.ssh/id_work, gh_account me-work) and personal
    (github.com, owners me, key ~/.ssh/id_personal) and a [default] with name/email), creates
    the empty key files + .pub, runs `apply -y` unless apply=False, returns the config path."""

def repo_with_remote(tmp_path, name, url, *, commits=0, author=("Work Me", "me@corp.test"),
                     committer=None) -> Path
    """As today, plus `commits` empty commits made with explicit GIT_AUTHOR_* / GIT_COMMITTER_*
    env (committer defaults to author), so tests do not depend on the generated config."""

@pytest.fixture
def bare_remote(tmp_path) -> Callable[[str], Path]:
    """bare_remote("origin.git") -> path to a bare repo; use its path as a remote url for pushes."""

def commit(repo, message, *, author=None, committer=None, trailers=(), date=None) -> str
    """One empty commit with explicit identities; returns the sha."""

def set_git_hooks_sandbox(monkeypatch, home)
    """GIT_CONFIG_GLOBAL unset so HOME/.gitconfig is used; GIT_CONFIG_NOSYSTEM=1 (already in `home`)."""
```

`home` stays as today. Nothing here contacts the network or the real home.

## 8. Files created/changed

None on disk by `why`, `which`, `check --json`, `--version`. Helpers write
only when a caller asks.

## 9. Failure modes

| situation | message | exit |
|---|---|---|
| dir is not a repo | `✗ <dir>: not a git repository` | 2 |
| no config (`why`) | existing `no config found (...)` | 2 |
| git < 2.36 | existing message | 2 |
| unknown config key | `_build` format listing the allowed keys | 2 |
| `--json` + any error after parsing | error object on stdout, nothing on stderr | 2 |
| non-POSIX | text on stderr (cannot be JSON: before parsing) | 2 |
| hot module missing | fall through to `cli.main` | — |

## 10. Security / privacy

Read-only. `resolve` parses `git config` output and reads origin files
only to check their first line. Env overrides are detected by name;
values printed are author/committer names and emails only. `forge.gh_env`
is the only place a token exists, in a child's environment; tests assert
it never appears on argv or in output.

## 11. Out of scope

The TUI; the extension (15); any writing command.

## 12. Acceptance tests (`tests/test_config_new.py`, `tests/test_resolve.py`, `tests/test_cli_core.py`, `tests/test_helpers.py`)

Config:

1. `[scan] roots = ["~", "/srv"]` loads as a tuple; `roots = ["src"]` →
   `[scan]: roots must start with ~/ or /`.
2. `[guard] on_mismatch = "ask"` → error naming the allowed values;
   `chain_to = "~/.config/gitpro/hooks"` → error.
3. `[clone] layout` valid/invalid/`../x` → as 3.2.
4. `[default.trailers] co_authored_by = "forbid"` → `Default.trailers
   .co_authored_by == "forbid"`; identity without the table → `None`;
   `Config` and nested objects are hashable (frozen; lists are tuples).
5. `gh_account = "bad name"` → error; `"janedoe"` ok.
6. `[agents] bot_emails = ["x\x01"]` → control-char error.
7. Unknown table `[foo]` → `unknown key(s) foo (allowed: agents, clone,
   default, forge, guard, identity, policy, scan)`.
8. `config.load` still returns `(Default, list[Identity])`; the existing
   test-suite passes unchanged.
9. `config.load_scan` on a file whose `[default]` has a placeholder email
   returns the `[scan]` table; on a file with `[scan] roots = ["x"]` raises.

wildmatch / match_url (pure; patterns from `render.url_patterns`):

10. `wildmatch("git@github.com:corp-org/**", "git@github.com:corp-org/sub/app.git")` true;
    `"git@github.com:corp-*"` vs `"git@github.com:corp-org/app.git"` false
    (`*` stops at `/`); `"**/app.git"` true; `"*app.git"` false;
    `"GIT@…"` false; `"…/app.git"` vs `"…/app"` false.
11. Two identities for github.com with owners `["a"]` and `["a","b"]` → url
    of owner `a` matches the second; owner `c` → `(None, None)`.
12. Identity with `ssh_alias = "github-work"` matches
    `git@github-work:corp-org/app.git`; https url with userinfo
    (`https://me@github.com/corp-org/app.git`) does not match, and
    `compare` reports `url_form` with the `remote set-url` fix.
13. **Differential test**: for 14 generated URL/pattern pairs (ssh, https,
    alias, port, nested group, userinfo, no `.git`, uppercase owner,
    `git@host:owner` with no repo, an owner-less identity against
    `git@github.com:o/r.git` and against `git@github.com:o/sub/r.git`),
    apply the config in the temp HOME, set the remote, and assert
    `expected(repo).email == git -C repo config user.email` (or both
    unset). Also unit: `wildmatch("git@github.com:**", "git@github.com:o/r.git")`
    false, `wildmatch("git@github.com:*/**", …)` true, `"a/**/b"` matches
    `a/b` and `a/x/y/b`.
13a. Origins: `compare` run with cwd in a subdirectory of the repo and with
    `repo` given as that subdirectory → `effective.email.origin` is the
    absolute `.git/config` path; a bare repo with `user.email` in its
    config → origin absolute under the git dir.

expected/effective/compare (use `cfg()`, `repo_with_remote`):

14. Matching repo → `expected.id == "work-github"`, `effective.email.scope
    == "generated"`, `problems == ()`, `why` exit 0 with `✓ identity
    matches` and the `config` line.
15. `git config user.email old@personal.test` → one `override`, fix
    contains `config --unset user.email`; `why` exit 1; `why --json`
    `ok: false`, `config.path` present.
16. Pinned repo, then config email changed and `apply -y` → `pin_stale`.
17. Empty `[default]` and remote on an unknown host → `no_identity`, text
    has `git will refuse to commit`.
18. `GIT_AUTHOR_EMAIL=x@y` in env → `env_override`; `compare(repo, cfg,
    env={})` with the same process env → no problem; `git -c
    user.email=x@y` is not visible to `why` (no problem) — documented.
18a. `core.sshcommand = "ssh -i ~/.ssh/id_work"` in the repo (tilde) with
    expected key `~/.ssh/id_work` → no `key_mismatch`. `Expected.ssh_key`
    is `None` for a repo that falls to `[default]`.
18b. `compare(repo, cfg, remote="git@github.com:corp-org/app.git")` (a URL
    that is not a remote name) → identity `work-github`.
18c. `resolve.EXTRA_CHECKS.append(lambda r: [Problem("policy", "x", None)])`
    → `compare` reports it; the list is restored after the test.
19. Linked worktree (`git worktree add`): `effective` origins are absolute,
    `pinned` is read from the common dir, `compare` works from the
    worktree path; `extensions.worktreeConfig` + `config --worktree
    user.email` → scope `worktree`, reported as `override`.
20. Identity with `sign = true`: after `apply`, `sign_mismatch` is absent;
    `git config commit.gpgsign false` in the repo → `sign_mismatch`;
    `git config user.signingkey /other.pub` → `sign_mismatch`; identity
    without `ssh_key` never yields `key_mismatch`.
21. `resolve.compare(other_repo)` called with `GIT_DIR` set in the
    environment to a different repo still reports `other_repo`.
22. `which` on a non-repo → stderr `not a git repository`, exit 2; `which
    --json` → five settings with `scope`, raw remote url, `ok: true`.
23. `check --json` → `up_to_date` true/false, exit 0/1.

cli / version / dispatch:

24. `gitpro --version` → `gitpro 0.2.0.dev0`; `--version --json` → object
    with `version`, `capabilities` containing `why` and `which.json`.
25. `main(["why", "--json"])` with no config → stdout JSON `ok: false`,
    `error` contains `no config found`, stderr empty, exit 2.
26. A test module registered through `EXTENSIONS` (monkeypatch the list
    with a tiny module defined in the test) appears in `--help`, is
    dispatched through `args.func`, and `extend_command(subs, "keys",
    …)` with a handler returning `None` runs the original `cmd_keys`.
    With `EXTENSIONS` monkeypatched to `["gitpro.not_built_yet"]`,
    `build_parser()` succeeds (the name is skipped); with a module that
    exists but itself does `import gitpro.also_missing`, `build_parser()`
    raises `ModuleNotFoundError` (a typo is not hidden). A module with
    only `extend` (no `register`) is still loaded and its flags appear.
27. `hot.main(["guard", …])` with no `gitpro.guard` module falls through to
    `cli.main` (exit 2 with `invalid choice`); with a stub module on
    `sys.modules` it calls the stub's `main` and `gitpro.cli` is not
    imported (`"gitpro.cli" not in sys.modules`); with a stub whose import
    raises `ModuleNotFoundError` for another name → the error propagates.
    `main(["--version", "--json"])` and `main(["why", "--json"])` both
    parse (`version_json` vs `json` do not collide).
28. `__version__`, `vscode/package.json`, `.claude-plugin/plugin.json`,
    `.claude-plugin/marketplace.json` agree.
29. Import cost: `python -X importtime -c "import gitpro.cli;
    gitpro.cli.build_parser()"` output has no line for `gitpro.resolve`,
    `gitpro.scan`, `gitpro.plan` (importing `cli` alone proves nothing:
    the extension modules load inside `build_parser`); `import
    gitpro.args`, `gitpro.config`, `gitpro.resolve`, `gitpro.hot`,
    `gitpro.jsonout`, `gitpro.undo`, `gitpro.files`, `gitpro.plan`,
    `gitpro.render` never import `rich` or `gitpro.cli` (`cli` itself is
    excluded from this list: it imports `view`/`rich` by design).
30. `python -X importtime -m gitpro --version` ≤ the baseline `import
    gitpro.cli` time + 20 ms (informational: asserted ≤ 1 s).
31. Py3.9 rule check over `src/gitpro/*.py` (AST walk).

helpers:

32. `undo.write("scan", ["git -C /r config user.email x"], what="…")` →
    file mode `0700`, line 1 `#!/bin/sh`, no `set -e`; two calls in the
    same second give `-1`; `O_EXCL` (pre-create the name → suffix used).
33. `files.atomic_write` through a symlink writes the target and keeps
    the link.
34. `jsonfile.edit_json` on `{"a": 1}` with 4-space indent adding `b` →
    diff shown to `confirm`, backup equals the original, undo restores it,
    indent kept at 4, `ensure_ascii=False` keeps `"é"`, trailing newline;
    unchanged mutate → `changed False`, no backup; file changed between
    read and write → `ConfigError`; JSONC file with `// c` and `jsonc=True`
    reads fine but editing refuses with the "by hand" message; `{"a": }`
    → `cannot parse`.
35. `forge.gh_env` with `fake_bin` gh: `GH_TOKEN` set in the returned env
    for `github.com`, `GH_ENTERPRISE_TOKEN` for `ghe.corp`, the ambient
    `GH_TOKEN=OLD` is removed first (fake logs `env:GH_TOKEN=unset` on the
    `auth token` call), the token value appears in no output and no log
    line; `repo_exists` → `missing` only on `Could not resolve to a
    Repository`, `unknown` on any other failure, `renamed` on a
    case-different name is **not** renamed.
36. `scan --fix --json` still prints `removed` and `undo`, now with `ok`
    and `command` (existing extension test passes).
37. `fake_bin.add("gh", …)` script logs `shlex.join(argv)`; an argument
    with a space and a quote round-trips through `fake_bin.calls`.

## 13. Modules touched

New: `args.py`, `resolve.py`, `jsonout.py`, `undo.py`, `jsonfile.py`,
`hot.py`, `forge.py` (minimal), `__main__.py` (switch to `hot.main`).
Changed: `cli.py` (dispatch as 2.1, `CALLER_UMASK`, `--version`, `which`,
`check --json`, `_fail`, `extend_command`, re-exports from `args`),
`config.py` (schema, `load_all`, `load_scan`), `plan.py` (`build(cfg,
env)`), `render.py` (`is_generated`, `url_patterns` `**` fix),
`files.py` (`atomic_write` public), `scan.py` (`undo.write`,
`ok`/`command`), `example.toml`, `pyproject.toml` (script entry),
`__init__.py` + `vscode/package.json` + `.claude-plugin/*.json` (version),
`tests/conftest.py` (fixtures). `guard.py` is **not** created here (06
creates it with the table from 5.6).

## 14. Docs text (for 17)

> `gitpro why [DIR]` explains which identity a repo uses, where each value
> comes from, and prints the command that fixes a mismatch. `gitpro which
> --json`, `gitpro why --json` and `gitpro --version --json` give the same
> data to tools.
