# 17 — Skill update, README updates, plugin manifest, release version

Wave 5 (last, alone). Depends on every other package: it documents them
and sets the release version. Sections for a package that was not merged
are left out, not stubbed.

Files this package may create or edit: `README.md`, `vscode/README.md`,
`skills/gitpro/SKILL.md`, `.claude-plugin/plugin.json` (description
and version; `hooks`/`mcpServers` keys only if the loader needs them, §3;
10/11 do not touch the manifest),
`.claude-plugin/marketplace.json`, `src/gitpro/__init__.py`,
`pyproject.toml`, `vscode/package.json` (version fields only), new
`tests/test_docs.py`. Nothing else.

## 1. Purpose

Keep three documents true to the shipped behaviour and ship version
`0.2.0` everywhere at once. Each package wrote a "Docs text" section for
this one to copy; the README spec (§6, §7, §9) holds the shared tables.

## 2. `skills/gitpro/SKILL.md` changes

The skill is read by an agent before it touches git for the user. New
content, after the existing "Rules":

### Rules (additions)

- Before any `git commit`, `git push`, `gh pr create` for the user: run
  `gitpro why` in that repo (or call the `why` MCP tool). If it reports a
  problem, show the user the `→` fix line and ask before running it. Never
  run `--no-verify` or set `GITPRO_SKIP=1` unless the user says so in this
  conversation.
- Never run `gitpro guard install`, `agent install`, `mcp install`,
  `devcontainer --write`, `keys --upload`, `audit --rewrite`, `clone`
  without an explicit yes; always show the dry run first (`guard status`,
  `devcontainer`, `audit`, `clone --dry-run`).
- `devcontainer.json`, `.mcp.json`, `.claude/settings.json` are team
  files: gitpro prints what to add; `--write` only when the user owns the
  repo and says so.
- Never pass `--online`, `--history` or `--send-repo-names` without the
  user asking; say what the first `online:` line says before running it.
- `gitpro env` prints commands, not tokens. Do not try to obtain token
  values.
- Use `--json` when you need to read results; plain output when showing
  the user.

### Workflow additions

- "The repo commits as the wrong person": `gitpro why` → follow its fix →
  `gitpro which`. History already wrong: `gitpro audit` → ask → `gitpro
  audit --rewrite` (unpushed commits only).
- "Set up a new machine": `gitpro init --discover` (add `--history` only
  if the user wants shell history read) → the user edits → `gitpro plan`
  → yes → `gitpro apply -y` → `gitpro keys` → `gitpro auth` (online, say
  so) → `gitpro doctor`.
- "Lost work before wiping/moving": `gitpro lost` (offline), then
  `--online` on request.
- "Make the agent safe": `gitpro agent status`; recommend the plugin
  (hook + MCP) or `gitpro agent install`. The hook never grants; it only
  denies with a reason.

### Command table (replace)

| command | writes? | needs yes? | network |
|---|---|---|---|
| `which`, `why`, `doctor`, `prompt`, `scan` (no `--fix`), `scan --roots`, `lost`, `audit` (no `--rewrite`), `keys`, `keys --health`, `guard status`, `guard check`, `agent status`, `devcontainer`, `devcontainer status`, `policy show`, `policy lint`, `team`, `env`, `completion`, `check` | no | no | no (`--online` opt-in where present) |
| `auth`, `forge repos/forks/email` | no | no | **yes**, say so |
| `plan`, `clone --dry-run`, `init --discover` (no `--history`) | no | no | no |
| `init --discover --history` | no (reads shell history) | **yes** | no |
| `apply`, `scan --fix`, `pin`, `unpin`, `uninstall`, `guard install/uninstall`, `agent install/uninstall`, `mcp install/uninstall`, `clone`, `audit --rewrite` | yes | **yes** | no |
| `devcontainer --write`, `mcp install --scope project --write` | yes, **team file** | **yes** | no |
| `keys --upload`, `forge similar` | remote / sends names | **yes** + `--online` (+ `--send-repo-names`) | **yes** |
| `keys --generate` | creates keys, interactive | do not run | no |

### MCP section (new)

Lists the tools (11); prefer `why` → `check_commit` before committing;
tools are read-only; the `fix` field is a command to propose, not run.

### Troubleshooting additions

`doctor` first; container → `pin` on the host / `devcontainer`; husky
etc. → `guard install --repo`; "hook blocked me" → read the
`cause:`/`fix:` lines; "hook printed `exited N; not blocking`" → `gitpro
doctor` (the guard failed open).

## 3. Plugin manifest and version

- `.claude-plugin/plugin.json`: description mentions hook + MCP and that
  the hook needs `gitpro` on the plain `sh` PATH (10 §2.2). 10/11 added
  `hooks/hooks.json` and `.mcp.json` at the plugin root and no manifest
  keys (V-027); 17 checks the current plugin docs and adds `hooks` /
  `mcpServers` keys only if the loader needs them (README §13).
- Final no-skip run (README §9): with every module present, `pytest -q
  -rs` must show no skips except platform ones; `test_docs.py` includes
  a test that imports all 17 modules by the names in `cli.EXTENSIONS`
  and a CI job runs the suite with `-rs` and fails on an unexpected
  skip line.
- Version `0.2.0` in `src/gitpro/__init__.py` (01 set `0.2.0.dev0`),
  `pyproject.toml` if it carries a literal, `vscode/package.json`,
  `.claude-plugin/plugin.json`, `marketplace.json`.
- Install line everywhere: `pipx install
  git+https://github.com/Yantrakar/gitpro.git` (Q7); no PyPI.

## 4. `README.md` changes

Insert, in this order, each from the package's "Docs text":

1. Top: one paragraph on guardrails and agents (README spec §1,
   shortened) + the install line.
2. After "Daily use": `why` (01), `doctor`/`prompt`/`completion` (16),
   `guard` (06), `clone` (07), `env` (12).
3. "Finding things": `scan` roots (02), `lost` (03), `discover` (04),
   `forge` (05).
4. "Keys" additions: `keys --health`, `auth`, `keys --upload` (09).
5. "AI assistants": agent hook (10), MCP (11), `env` (12), devcontainer
   (13), `audit` (08), trailer policy.
6. "Teams": policy file, CI check, Action (`uses:
   Yantrakar/gitpro@main`), `team` (14).
7. Config reference: the full TOML from README spec §7 replaces the
   current example; every new key gets its one-line comment.
8. "Undo everything": `uninstall` scope + the per-command undo list
   (guard/agent/mcp/devcontainer are not part of `uninstall`; each prints
   `↩`).
9. Exit codes (incl. 3 = hook block) and `--json`/`--version --json`
   paragraph (README spec §5–6, short).
10. Keep the existing text for unchanged commands verbatim.

`vscode/README.md`: status bar (15), scan roots picker (02), Lost Work
view (03), devcontainer prompt (13), `Clone…` (07), capability-based
feature detection (no minimum version), settings table.

## 5. Tests (`tests/test_docs.py`)

- Every subcommand name in `cli.build_parser()` (after extensions) appears
  in `README.md` and in `SKILL.md`'s command table.
- Every config key accepted by `config.py` (walk its allowed-key sets)
  appears in the README config block; `example.toml` loads with
  `config.load_all`.
- `.claude-plugin/plugin.json`, `hooks/hooks.json`, `.mcp.json`,
  `marketplace.json` parse; `plugin.json`, `marketplace.json`,
  `vscode/package.json` and `gitpro.__version__` agree and equal
  `0.2.0`; `gitpro --version --json` reports that version.
- `SKILL.md` front matter parses (`name: gitpro`); the command table
  lists every writing subcommand from the explicit list in the test:
  `apply, scan --fix, pin, unpin, uninstall, guard install, guard
  uninstall, agent install, agent uninstall, mcp install, mcp uninstall,
  devcontainer --write, clone, audit --rewrite, keys --upload, keys
  --generate, init`.
- Every entry in `cli.EXTENSIONS` imports (no `ModuleNotFoundError`) —
  the release has no missing package.
- README install line contains `git+https://github.com/Yantrakar/gitpro.git`
  and no `pip install gitpro`/`pipx install gitpro` bare form.

## 6. Out of scope

Changelog automation, docs site, screenshots.

## 7. Modules touched

Listed at the top. No Python module changes beyond `__init__.__version__`.
