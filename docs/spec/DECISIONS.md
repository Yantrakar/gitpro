# Decisions on the review's product-owner questions

Made by the orchestrating agent on 2026-10-02 while the product owner was away,
choosing the conservative option each time. Each one is easy to revisit; the
spec follows these until the product owner says otherwise.

| Q | Decision | Why |
|---|---|---|
| Q1 paid tier | Everything in this repo stays MIT and free, including 14 (policy, `check --policy`, the Action). Paid ideas (hosted GitHub App, team dashboard) stay in 14's "future, needs hosting" section only. | The owner asked for ideas on a paid tier, not for a licence change. Nothing built now is lost if a paid tier comes later. |
| Q2 `forge similar` | Keep. Off by default; needs `--send-repo-names` on every run (or `[forge] similar = true`), and the first line says exactly what is sent to which service. | The owner asked for "discovery of similar work on github public". |
| Q3 shell history in `discover` | Opt-in: `--history`. Without it, discover reads editor recents, ssh config host aliases, includeIf rules and repos on disk only. Output never echoes history lines, only the clone URLs found. | History can hold secrets; local-only still deserves consent. |
| Q4 guard defaults | Nothing installs until the user runs `guard install` (or sets `[guard] hooks = true`). Once installed: `on_mismatch = "block"`, pre-commit and pre-push both on, global mode (core.hooksPath) with chaining fixed per review R-001/R-003; per-repo mode available. The guard judges the committer (the user's own identity), per R-005. Any gitpro failure fails open. | The owner asked for a guard; an installed guard that only warns is easy to ignore. Opt-in install keeps it safe. |
| Q5 tracked team files | gitpro never writes tracked files (devcontainer.json, `.husky/*`, `.claude/settings.json` in a repo, `.mcp.json` in a repo) by default; it prints the snippet. An explicit `--write` exists only for devcontainer.json and project `.mcp.json`, with backup and undo. User-level files (`~/.claude/settings.json`) follow the normal preview/confirm/backup/undo rules. | Writing files a whole team commits is a team decision. |
| Q6 agent hook bypass | The agent is told to ask the user; the message does not teach a bypass. The user bypasses with `GITPRO_SKIP=1` in their own shell, `[agents] hook = "warn"` or `"off"`. Warn mode emits no permission decision (the normal permission prompt still happens) plus a visible message. "block" emits deny. Never emit allow. | R-006: allow would skip the user's own permission prompt. |
| Q7 PyPI | Not assumed. Install docs keep `pipx install git+https://github.com/sandeepshelke/gitpro.git`; the Action installs from its own checkout (`pip install "$GITHUB_ACTION_PATH"`). | Owner has not claimed the PyPI name. |
| Q8 devcontainer & Windows | Print-only by default; `--write` warns that the snippet needs `gitpro` on every teammate's host and uses a form that does not fail the container build when gitpro is missing on any OS. | Do not break teammates. |
| Q9 scope | All packages are built (the owner asked for all), in the review's order: 01, 06, 10, 12, 15, 16 first; then 02, 03, 07, 08, 09, 11; then 04, 05, 13, 14; 17 last. | Owner's instruction "implement all". |
| Q10 other agents | Claude Code adapter fully built and tested. Cursor / Gemini adapters exist in the adapter table, marked experimental, not installed unless named explicitly. | Their hook formats are unverified offline. |
| Q11 glab one login per host | Accepted and documented. | glab limitation. |
| Q12 versions | One version for CLI, extension and plugin. The extension detects features with `gitpro --version` and a `capabilities` list in `gitpro --version --json`, not a hard minimum. | Simpler and robust to mixed installs. |
