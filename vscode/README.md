# gitpro for VS Code

Shows in the status bar which git email the current repo commits with, and
warns when git would refuse to commit (no identity). Hover for the name, the
file the email comes from, and the remotes. Click for the full
`gitpro which` output.

Commands (Command Palette, "gitpro"):

| command | runs |
|---|---|
| Plan | `gitpro plan` |
| Apply | `plan`, then `apply --yes` after you confirm |
| Pin / Unpin Identity | `gitpro pin` / `unpin` for the current repo |
| Open Config | opens `$GITPRO_CONFIG` or `~/.config/gitpro.toml`; offers to create it |
| Show Identity for This Repo | `gitpro which` |

The status bar only needs `git`. The commands need the
[gitpro](https://github.com/sandeepshelke/gitpro) CLI:

```sh
pipx install git+https://github.com/sandeepshelke/gitpro.git
```

Set `gitpro.path` if it is not on `PATH`.

In a dev container the status bar shows what git resolves inside the
container. If it warns, run `gitpro pin` in the repo on the host.

## Repo Overrides

A `user.name` or `user.email` in a repo's own `.git/config` beats gitpro. The
gitpro view in the activity bar lists those repos, grouped by the identity
they get once the setting is removed (`gitpro scan`). It scans when you press
Scan or Refresh; set `gitpro.scanDirs` to scan other directories
than your home.

- Click a repo to open its `.git/config`; hover for each setting and what
  replaces it.
- Review One by One steps through the fixable repos ("Review app (3/15)"):
  Fix, Skip, Back, or fix or skip all the rest. Your answers set the
  checkboxes, and nothing changes until the last step's confirmation. Escape
  puts the checkboxes back as they were.
- Fix Checked removes the settings of the checked repos in one go
  (`gitpro scan --fix --repo ...`) after you confirm. The notification names
  the undo script and offers Undo. Repos where nothing else sets an identity
  have no checkbox: the fix keeps them.
- Suggest Identities opens the `[[identity]]` blocks that would keep a repo's
  current identity instead. Paste the ones you want into your config, run
  Apply, then Fix.

## Log

The gitpro output channel logs every command with its directory, exit code
and time, any warnings from gitpro, what each scan found (including how
many repos it skipped and why, e.g. "30 skipped: 26 owned by root, 4 with a
broken .git link"), each review answer, and every setting a fix removes with
the undo script's path. Lines are timestamped and kept across commands. For
more, run Developer: Set Log Level... > gitpro: Debug adds each skipped repo
with its reason and checkbox changes; Trace adds gitpro's raw output.

## Development

`npm run dev` (or F5 in this folder) opens a VS Code window with the extension loaded from here, without
installing anything. It runs the checkout's `../.venv/bin/gitpro` unless `gitpro.path` is set. `npm test` runs the unit tests, `xvfb-run -a npm run test:integration` the integration
test, and `npm run package` builds the `.vsix`.

VS Code itself prints a `[DEP0169] url.parse()` deprecation warning on `code --install-extension` and in the
extension host. It comes from VS Code's request service and `@vscode/proxy-agent`, not from this extension
([microsoft/vscode#301941](https://github.com/microsoft/vscode/issues/301941)).
