# Security

Report a vulnerability privately through
[GitHub security advisories](https://github.com/Yantrakar/gitpro/security/advisories/new).
`gitpro report --security` opens the same form. Please do not open a public
issue for it.

Fixes go into the latest release only.

Commands that change git or SSH configuration only preview unless given
`-y`/`--write`, and the MCP server is read-only unless started with
`--allow-execution`. A way around either is in scope.
