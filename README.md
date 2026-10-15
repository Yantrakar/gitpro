<a href="https://gitpro.dev"><img src="https://gitpro.dev/icon.png" alt="gitpro" width="72" height="72"></a>

# gitpro

<!-- mcp-name: io.github.Yantrakar/gitpro -->

**The right Git identity, SSH key and signature for every repository.**

[gitpro.dev](https://gitpro.dev) · [PyPI](https://pypi.org/project/gitpro/) · [VS Code](https://marketplace.visualstudio.com/items?itemName=yantrakar.gitpro) · made by [Yantrakar](https://yantrakar.com)

Work on a company repository, commit with your work email. Switch to a personal
project, use your personal identity. Gitpro chooses by **remote URL**, not by
where you happened to clone the repository.

Useful when you have more than one GitHub/GitLab account, separate work and
personal keys, or keep finding the wrong email on your commits.

## Try it

Requires **Linux or macOS**, Git **2.36+**, Python **3.9+** and OpenSSH.
Install from [PyPI](https://pypi.org/project/gitpro/) with pipx.

```sh
pipx install gitpro
gitpro setup
```

The first run asks for your identities, their Git hosts and owners, and the SSH
key for each one (an existing key, or a new path that setup creates). **No TOML
editing is needed.** Setup then shows everything it will set up: the config, the
missing SSH keys, the Git and SSH files, the commit guard and, when Claude Code
is installed, its hook and MCP server. One yes applies all of it. Setup never
changes your repos.

Then:

```sh
gitpro scan              # repos that need a fix, with the command for each
gitpro why               # inside a repository: the expected identity and anything overriding it
```

Already have a config? `gitpro setup` brings this machine up to date with it,
and `gitpro setup --add` adds another identity without losing your existing
settings or comments. Prefer editing files? The
[user guide](docs/guide.md#configuration) covers the full TOML format.

## What you get

- **Work/personal separation:** names, emails, SSH keys and signing per host/owner.
- **Answers, not guesswork:** `gitpro why` explains mismatches and suggests a fix.
- **A safety net:** optional hooks can block wrong-identity commits and pushes.
- **Editor and container support:** see your identity in VS Code and carry it into dev containers.

Gitpro generates ordinary Git and SSH configuration; it needs no background
service. Changes can be previewed, existing hand-written files are backed up,
and key creation or uploads are never part of the setup wizard.

## Using VS Code?

Run **gitpro: Set Up Identities** from the Command Palette to open the same wizard
in a terminal. The extension also shows the current identity and helps find
repository-level overrides. [Install the extension](vscode/README.md).

## Go further when you need to

For agents, [the versioned workflow](docs/agent-workflows.md) provides structured
preflight, identity-scoped execution and durable idempotency for commits, pushes
and GitHub PRs. Persistent changes use exact proposals, human approval by default,
optional operator-scoped preauthorization, and recoverable apply. MCP execution is opt-in.

| I want to… | Start here |
|---|---|
| Understand matching, SSH keys or signing | [Configuration](docs/guide.md#configuration) |
| Find wrong identities or forgotten work | [Daily use](docs/guide.md#daily-use) · [Finding things](docs/guide.md#finding-things) |
| Use dev containers | [Container setup](docs/guide.md#dev-containers) |
| Configure team policy, CI or AI assistants | [Teams and CI](docs/guide.md#teams) · [AI assistants](docs/guide.md#ai-assistants) |
| Look up a command or undo changes | [Command reference](docs/guide.md#commands) · [Undo](docs/guide.md#undo-everything) |
| Troubleshoot or contribute | [Troubleshooting](docs/guide.md#troubleshooting) · [Development](docs/guide.md#development) |

The detailed TOML, YAML and command examples live in the guide, not on the
getting-started path.

## Problems and questions

Run `gitpro report` to draft an issue with the versions and a redacted
`gitpro why`; it shows you the draft and opens nothing until you say yes.
Or open an issue at [github.com/Yantrakar/gitpro/issues](https://github.com/Yantrakar/gitpro/issues).
Report a security problem privately with `gitpro report --security`, as the
[security policy](https://github.com/Yantrakar/gitpro/security/policy) describes,
not in a public issue.

## License

[MIT](LICENSE)
