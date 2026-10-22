# Reviewing a gitpro change

Read this before reviewing a pull request, whether you are a person or an
agent. The rules it points to are in docs/spec/README.md: section 2
(principles) and section 8 (safety rules). docs/spec/DECISIONS.md records
why. docs/spec/REVIEW.md is a dated review of the spec, not this checklist.

## What failure looks like here

A gitpro bug is serious when it does one of these. Look for them first.

1. **It commits as the wrong person.** A remote resolves to the wrong
   identity or key, an `includeIf` or SSH `Host` block matches the wrong
   repos, or `gitpro why` and the commit-msg hook disagree.
2. **It leaks a secret.** A token or a password in a remote URL reaches
   stdout, stderr, `--json`, a report, an MCP reply, a log or argv. gitpro
   never reads private keys, `.netrc` or `.git-credentials`, and never prints
   a token (principle 5).
3. **It loses the user's config.** A write that isn't shown first,
   confirmed, atomic, and backed up or undoable; a write that replaces a
   symlink; a write to a tracked team file without `--write` (principles
   2 and 3, safety rules 4 to 8).
4. **It goes online silently.** A network call without `--online` or its
   config key, or without the first line that names the service, account and
   what is sent (principle 4).
5. **It blocks a commit on its own error.** A hook may block only on exit 3,
   a confirmed mismatch; anything else prints a warning and lets the commit
   through (principle 9).
6. **It changes the surface.** Commands, flags, help text, exit codes,
   `--json` fields, MCP tools, hook output or files written change without a
   CHANGELOG entry and a matching update to tests/test_surface.py,
   docs/guide.md and skills/gitpro/SKILL.md.
7. **The docs lie.** README.md, docs/guide.md, site/ or the CHANGELOG claim
   something the code doesn't do.

## How to report

- Start with a count of findings. If there is nothing to fix, say so in one
  line.
- Give each finding a file and line, and what goes wrong: the input or
  state, and the wrong output or the crash. Not a guess from a name.
- Tag each finding **Important**, **Nit** or **Pre-existing** (a bug the
  change didn't introduce).
- At most three nits. Skip style the tests and linters don't enforce.
- Don't repeat a finding already raised on the pull request. If the same
  bug appears in several places, name the class once and list the places.

## Checks a change must pass

- The full test suite, offline, with a temporary HOME (principle 8).
- Python 3.9 still works: none of the forms spec README section 11 forbids.
- No new dependency without a written reason in its package file
  (principle 7).
- Site copy: factual claims only, no em dashes, no other tools named.
- The version is written only in src/gitpro/VERSION.

## Needs the maintainer, whatever the size

- .github/workflows/, releasepro.toml, action.yml and server.json:
  they publish or run on other people's repositories.
- The git hooks (src/gitpro/guard.py), the agent hook (src/gitpro/agents.py,
  hooks/hooks.json) and the MCP server (src/gitpro/mcp.py): they run inside
  git and other tools.
- Anything that changes what is written to `~/.gitconfig` or `~/.ssh/config`.

## Skip

- vscode/package-lock.json, images under site/, vscode/media/ and .github/.
- docs/spec/REVIEW.md and REVIEW-RESPONSE.md: dated records.
