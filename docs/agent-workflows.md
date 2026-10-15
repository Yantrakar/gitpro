# Agent workflows (contract version 1)

Gitpro provides a shared CLI/MCP **preflight -> execute -> verify** path using
existing configuration. A separate **propose -> authorize -> apply** path
handles persistent configuration and bounded repairs. Operation execution never
repairs configuration implicitly, switches the active forge account, stages
files, or grants permission on behalf of an agent host.
Authorization defaults to human approval; a host operator can instead supply
an explicit, narrowly scoped policy for unattended changes.

## Preview by default

Capability `dry-run-defaults.v1` announces preview-first CLI behavior. Use standard
`-y`/`--yes` for explicitly authorized noninteractive execution, or `--write` to
enable a command's interactive confirmation. No new `--run` flag is needed.
Existing action flags such as `--fix`, `--rewrite`, `--generate` and `--upload`
remain explicit requests for those actions, with their existing safeguards;
`--online` alone never grants write permission.

Without an execution flag, `apply`, `uninstall`, `init`, `pin`, `unpin`, `clone`,
and agent/MCP/guard installation/removal only preview. `setup` collects and
displays configuration but cannot save until launched with `--write`.
`gitpro run --request FILE` returns a preflight; add `-y`, the reviewed token and
idempotency key to execute. `change apply` rebuilds a proposal without authorizing
it; `change recover` shows recorded status. These reads can still report missing
inputs or stale plans rather than pretending a write would succeed.

`change approve` previews by default. **`change approve --write` still requires
real terminal review and confirmation**; neither `--write` nor a plan ID grants
approval by itself. MCP remains read-only by default; explicitly enabled write
tools retain their host authorization and human/policy approval requirements.

Migration: scripts that used bare `init`, `pin`, `unpin` or installer commands
must add an explicit execution flag. Regenerate old managed devcontainer pin
snippets through `devcontainer --write -y`; handwritten pin commands need `-y`
added after review. No live configuration is migrated automatically.

## Supported operations

| Operation | Scope |
|---|---|
| `commit` | The current staged index, with an explicit message. Ordinary Git hooks run. No amend, merge, cherry-pick, rebase, or automatic staging. An explicit colleague author is preserved; the committer remains the configured identity. |
| `push` | Current HEAD to one explicitly named branch on one configured remote. Checks the actual push URL, outgoing identities/policies, and fast-forward ancestry. No deletion, tags, submodule pushes, or history rewriting. |
| `pr-create` | GitHub, including configured enterprise hosts: one already-pushed branch in the same repository as the base. Explicit title/body; no implicit push, fork, or cross-repository PR. |

Transport support: SSH with an existing identity key, GitHub HTTPS through the
configured `gh_account`, and absolute local/file remotes. SSH uses only the
selected key and requires an already trusted host key; it does not load arbitrary
SSH config, ProxyCommand or ProxyJump directives. Configured gitpro host aliases
are mapped to their real host and port. Encrypted keys must already be available
through the SSH agent. When a forge login is configured, an online SSH probe must
identify that account; otherwise preflight explicitly reports that account
identity was not checked (the selected key and repository access still are).
Other SSH arrangements, GitLab HTTPS, cross-repository PRs,
and missing local history require a separate, explicitly authorized human/agent
workflow; they are not silently approximated.
HTTP extra-header overrides are refused because they can select a different
account independently of the scoped credential helper.

## Contract and authorization

`gitpro preflight --schema` prints the request and result JSON Schemas.
Requests have `version: 1`, an absolute worktree-root `repo`, an `operation`, and
only that operation's fields. Unknown fields, duplicate JSON members, unsupported
versions, arbitrary commands and environment dictionaries are rejected.
MCP rejects duplicate members anywhere in the JSON-RPC message before dispatch,
including nested operation/change requests, and remains available for the next
request.

| Decision | Meaning |
|---|---|
| `ready` | The required preconditions were checked for this operation. It is not execution permission or a guarantee the external write will succeed. |
| `blocked` | A known policy/identity violation, conflicting operation or invalid state. |
| `needs_input` | Supply missing configuration, stage changes, fetch required history, or correct the request. |
| `needs_approval` | Persistent repair or an explicitly permitted network check is required. Operation execution does not perform repairs; use the separate change workflow. |
| `indeterminate` | A required check failed, or an operation's effects cannot be established. Do not assume success or retry a write blindly. |

Results have stable `problems[].code`, human-readable messages, checks performed,
the resolved identity and destination, and a `plan_token` only when ready.
Exit 0 means success/ready, 3 means blocked, and 2 means another non-ready result.
Every new command emits one JSON object; `--json` is accepted for consistency.

The token detects stale state; it is **not a signature, approval token or security
boundary**. CLI execution requires `-y`. MCP execution must be enabled by the
server operator, and the agent host must still authorize each write. Neither
the request nor the token can authorize persistent configuration changes.

## Example: staged commit

Save the request as `request.json` (replace the repository path):

```json
{
  "version": 1,
  "operation": "commit",
  "repo": "/absolute/path/to/repository",
  "message": "fix: handle missing configuration"
}
```

```sh
gitpro preflight --request request.json --json
# Review the decision, identity and intended operation; obtain host/user approval.
gitpro run --request request.json --plan-token TOKEN_FROM_PREFLIGHT \
  --idempotency-key task-42-commit-1 -y --json
gitpro operation-status --idempotency-key task-42-commit-1 --json
```

`--request -` reads stdin. The maximum request is 128 KiB. Add `author_name` and
`author_email` together only when the commit really has a different author.
A local override is reported for approval, not silently repaired. Ambient author,
committer, repository-redirection and credential variables are not borrowed;
inherited Git safety settings remain in force.

Push request:

```json
{
  "version": 1,
  "operation": "push",
  "repo": "/absolute/path/to/repository",
  "remote": "origin",
  "branch": "feature/agent-contract"
}
```

PR request:

```json
{
  "version": 1,
  "operation": "pr-create",
  "repo": "/absolute/path/to/repository",
  "remote": "origin",
  "base": "main",
  "head": "feature/agent-contract",
  "title": "Add the agent contract",
  "body": "Describe the reviewed changes.",
  "draft": true
}
```

Push and PR preflight/execution both require `--online`. This permits reading
the destination refs and, when relevant, verifying the configured GitHub
account. Push sends commits; PR creation sends the title, body and branch names
to that destination. No automatic fetch or account switching occurs. The online
flag alone never authorizes execution. Policies requiring verified email also
need online verification.

## Idempotency and atomicity

Reuse **the same request, original plan token and idempotency key** after a lost
response. Keys are unique within the user's operation ledger. A completed
request returns its stored result, without repeating the write, even though HEAD
has since changed. Reusing a key for a different request or plan is a conflict.
A new intended operation needs a new key and preflight.

The private ledger lives at
`$XDG_STATE_HOME/gitpro/operations/ledger.sqlite3` (default
`~/.local/state/gitpro/operations/ledger.sqlite3`). SQLite commits a running
record before starting a write, and durably records the result afterward.
A nonempty `XDG_STATE_HOME` must be absolute; a relative value returns
`invalid_state_path` before any journal, lock or operation write. To inspect an
older journal created with a relative value, set `XDG_STATE_HOME` to that existing
directory's absolute path rather than starting new operations with new keys.
The ledger's directory ancestry is fsynced before execution; a failed flush
prevents the write rather than silently weakening durability.
The same transaction records immutable operation intent, including the request's
commit message or PR title/body, configuration fingerprints and destination.
Treat the ledger as private content; authentication tokens from the environment
are never copied into that intent.
Do not delete the ledger while operations or retries are possible.
An unfinished running record means **running or outcome unknown**: inspect Git
history, remote refs or the PR before taking further action. Gitpro does not
invent exactly-once delivery for remote APIs.

| Boundary | Guarantee |
|---|---|
| Local agent coordination | A nonblocking lock serializes gitpro operations sharing a common Git directory, including linked worktrees. Separate repositories and their credential environments are independent. Shared configuration locks allow concurrent scoped operations but exclude approved persistent changes. |
| Staged content | A native index lock protects the reviewed index while Git/hooks use a private copy. Ordinary competing Git index writes fail instead of entering this commit. The original index is not rewritten. |
| Commit | Git performs its normal ref update and runs hooks. The resulting parent, tree, author, committer, policy compliance and required signature are checked. Unexpected hook effects are reported, not silently rolled back. |
| Push | One-ref atomic push, verified fast-forward ancestry and an exact expected-old-ref lease. A changed destination is not overwritten. Unsupported server atomic pushes fail rather than falling back. |
| PR | Detect an existing open head/base PR first. Record a successful returned PR URL. An interrupted or malformed write response is indeterminate, never automatically repeated. |
| Cross-system transaction | None. The journal, Git refs, hooks and forge cannot participate in one transaction. Failure after a side effect requires reconciliation, not an automatic rollback or retry. |

Preflight fingerprints HEAD, branch, staged index, effective Git configuration,
gitpro configuration, policy/signers content, relevant key metadata and checked
remote state. Execution revalidates under the cooperative repository lock.
Repository policy/trust files that differ from the committed base are reported
for separate approval rather than letting a policy change authorize itself.
This is a conservative restriction on policy-editing workflows, not the CI
policy engine's full protected-base policy migration workflow.
These locks do not serialize arbitrary external programs or turn a changing
filesystem into a snapshot. Run agents in separate worktrees and avoid concurrent
configuration edits. Never run untrusted repository hooks: the executor is not a
sandbox or credential firewall against the command you explicitly authorized.
Cooperating processes must use the same state directory. Legacy writing commands
such as `apply`, `setup`, `pin` and `scan --fix` do not participate in this new
configuration lock; do not run them concurrently with agent operations/changes.

Child processes have bounded runtime/output, no inherited interactive stdin, and
process-group cleanup, including CLI/MCP SIGTERM cancellation. A stopped write can have effects even when no success
response was returned. `operation-status` is read-only and never restarts it.
Gitpro reports failures explicitly without copying arbitrary helper output or
tokens into its JSON, logs or ledger.

## Read-only reconciliation

Use `gitpro operation-reconcile --idempotency-key KEY` after inspecting an
interrupted or indeterminate operation. `--schema` exports its separate result
contract. Push/PR inspection additionally requires explicitly permitted `--online`
destination/account reads. No write is retried and no recorded outcome is changed.

| Operation | Evidence checked |
|---|---|
| Commit | Current HEAD's exact parent, author, committer, message, staged tree and applicable signing/message policy, with the original index unchanged. Later commits/index edits can make the result unconfirmable. |
| Push | The original resolved destination branch currently points to the intended commit. An absent/different ref is not permission to push again. |
| PR | A unique matching PR, including closed/merged PRs, with original repository, refs/SHAs, title/body/draft. A preflight-reused PR can match its recorded URL. Pagination is bounded to 10 pages of 100 results; incomplete searches are not treated as absence. |

The original configuration, repository layout, identity and trust inputs must
still match. Gitpro reuses scoped credentials for network reads, not arbitrary
credentials supplied by the caller.

`status: "observed"` means the requested postcondition is visible now, **not that
this operation caused it**. Results explicitly retain `retry_safe: false` and
`attribution: "not_proven"`. A running/indeterminate ledger entry stays that way;
reconciliation never converts it into a completed operation or enables a retry.
Missing, ambiguous or edited evidence returns an actionable non-ready result.
Older journal records without captured intent report `intent_unavailable` and
need manual inspection rather than reconstructed guesses.

## Approved persistent changes

`gitpro change propose --schema` exports the separate version-1 request and result
schemas plus the operator approval-policy schema. Proposals read configuration and
render exact target files; they do not
write targets or record approval. The result contains the normalized `request`,
content-addressed `plan_id`, paths, actions, content hashes, modes and zero-context
diffs. A valid proposal returns exit 0 with `decision: "needs_approval"` (or
`"ready"` when an operator policy covers it); a no-op
has `status: "no_changes"` and needs no apply. Errors use the same decision and
exit-code conventions as operations.

| Request `kind` | Required fields and scope |
|---|---|
| `configure` | `toml`: the complete explicitly supplied TOML document. Creates/replaces the source and renders its managed Git/SSH/guard/signers files in one reviewed change. Does not invent account details, create keys or log in. |
| `regenerate` | Render existing configuration without editing its TOML source. Useful for missing/stale generated files. |
| `repo-identity` | `repo`: absolute worktree root. Remove only conflicting `user.name`/`user.email` entries directly in the common `.git/config`, when the underlying settings already match the configured identity. Requires generated files to be up to date. |
| `undo` | `change_id`: a successfully applied change. Propose restoring its exact old bytes/modes. This is a new change requiring its own human approval or an explicit policy grant for `undo`; later edits are never overwritten. |

Except for `undo`, `config_path` optionally selects an absolute `.toml` source
inside the current home directory. Otherwise selection uses `-c`, `GITPRO_CONFIG`,
then `~/.config/gitpro.toml`. Requests are limited to 128 KiB, input files to 1 MiB,
and plans/backups to 4 MiB and 128 input files. Only trusted regular files and
non-symlink paths are supported; writable-by-others targets/parents are refused
(system sticky temporary-directory ancestors are allowed). Existing files that
change must be owned by the current user. Symlink-based dotfile setups require
a separate human workflow. Private SSH key files are not change targets.
Managed configuration sources, generated outputs and stale-output removal cannot
target `.git` entries or directories that contain Git metadata, including bare,
separate and linked-worktree Git directories. Keep custom identity files outside
those directories; only `repo-identity` can perform the supported local
name/email repair. Previously recorded approvals are revalidated against this
restriction before forward application; existing rollback/undo backups remain
available.

`repo-identity` deliberately excludes local includes, pins, worktree-specific
configuration, signing/transport repairs and repository policy edits. It never
changes remote URLs, staged content or history. A configure proposal may update
gitpro's **home-level generated** allowed-signers file; it does not edit a
repository's protected policy/trust files. File-level success does not establish
account access or operation readiness: run the operation's preflight afterward.

Example repair request, saved as `change-request.json`:

```json
{
  "version": 1,
  "kind": "repo-identity",
  "repo": "/absolute/path/to/repository"
}
```

```sh
gitpro change propose --request change-request.json > proposal.json
# The human runs this in their own terminal and reviews the freshly computed diff:
gitpro change approve --write --proposal proposal.json
# With host/user permission, the agent can now use the recorded exact approval:
gitpro change apply --plan-id PLAN_ID -y
gitpro change status --plan-id PLAN_ID
```

Approval requires terminal stdin/stdout/stderr and a default-no confirmation.
There is no `-y` bypass, client `approved: true`, or MCP approval tool. The displayed
diff is rebuilt from the normalized request, not trusted from the proposal's
display fields, and inputs are rechecked after confirmation. Approval records
only the reviewed plan; it does not apply it. Do not automate the approval prompt.
A TTY and same-user SQLite record are **not cryptographic proof of human consent**:
a same-UID agent with unrestricted shell access can bypass these guardrails.
The host/operator must enforce the approval channel and authorize each execution.

### Scoped operator preauthorization

Capability `approval-policy.v1` allows the operator to replace the per-plan human
prompt with **bounded, recorded preauthorization**, not a blanket safety bypass.
There is no automatic policy discovery or agent-supplied `approved: true`.
The operator explicitly selects a protected, absolute policy path:

```json
{
  "version": 1,
  "expires_at": "2030-01-01T00:00:00Z",
  "rules": [
    {
      "id": "worktree-identity-repair",
      "kinds": ["repo-identity"],
      "config_paths": ["/home/runner/.config/gitpro.toml"],
      "repo_roots": ["/home/runner/work/project"],
      "write_paths": ["/home/runner/work/project/.git/config"],
      "write_roots": []
    }
  ]
}
```

Replace these paths and expiry with the intended deployment scope. A linked
worktree's common Git config may be **outside** its worktree: scope `repo_roots`
to the worktree and `write_paths` separately to that common config.

Every rule requires all six fields shown. **One rule** must cover the change
kind, exact source config path, repository when applicable, and **every changed
file**; permissions are never assembled from multiple rules. `write_paths` are
exact files; `repo_roots` and `write_roots` include descendants by path ancestry,
not string prefixes or globs. `undo` uses the original source/repository context.
Empty `rules` denies all automatic grants. Policies are bounded to 64 KiB,
32 rules and 128 paths per scope array; duplicate fields/IDs/paths, unsupported
kinds, relative/noncanonical paths and filesystem-root scopes are rejected.

Prefer exact file paths and minimal kinds. **Path scopes do not constrain
individual TOML values**: authorizing `configure` permits the complete proposed
configuration within those file scopes. Broad home-directory write roots grant
broad authority. Policies cannot authorize their own replacement; changes cannot
overwrite their journal or coordination files.

```sh
gitpro change propose --request change-request.json \
  --approval-policy /absolute/operator/policy.json > proposal.json
gitpro change apply --plan-id PLAN_ID --proposal proposal.json \
  --approval-policy /absolute/operator/policy.json -y
gitpro change status --plan-id PLAN_ID
```

Propose only reports coverage. Apply rebuilds the request, checks its exact plan
ID, records the grant, and revalidates before writing. Uncovered plans still
require human approval. Neither policy coverage nor `-y` bypasses the agent
host's execution permission, stale-plan rejection, identity/policy checks,
locking, journaling or credential isolation.

Results/status expose `authority`: `kind: "human"` or `"policy"`, with the policy
path, byte hash, file fingerprint and matching rule ID for policy grants. A
pending policy grant is usable only with the same unchanged, unexpired policy.
Changing/removing the policy revokes that grant; explicitly supplying the
proposal again can obtain a fresh grant under a currently covering policy.
Completed results replay without the policy, but interrupted attempts cannot be
reauthorized into a forward retry. Recovery retains the original permission to
roll back even after policy removal/expiry and still requires host authorization.

Policy files/parents must have trusted ownership and must not be symlinks or
writable by other users. **These checks are not isolation from a same-UID agent.**
The operator must protect policy contents and CLI/MCP startup arguments outside
the agent's writable workspace, using host-enforced separation or read-only
mounts. An unrestricted same-user shell can rewrite a user-owned policy, choose
another CLI policy or run Git directly. Never describe this as a security boundary
unless the deployment actually enforces one.

### Change idempotency, failure and recovery

The plan ID is the apply idempotency key. Repeating apply returns the original
durable result without repeating writes, including after later external edits.
Status describes that recorded attempt, **not current filesystem readiness**.
Changed file bytes, modes, identities or parent topology invalidate an unused
approval. Propose again and obtain a fresh approval; do not substitute a new ID
to hide an unknown outcome.

Before writes, the private SQLite ledger stores the immutable approved document,
exact old-file backups, and an `applying` record. Per-file write intent and owned
native lock identity are recorded before replacement. Each file is replaced
atomically and fsynced, including its parent directory. Contents/modes and
unchanged inputs are checked afterward. This is **not atomic multi-file
visibility**, nor a filesystem-wide compare-and-swap against arbitrary editors.
The exclusive configuration lock protects cooperating agent operations only.

An ordinary failure attempts to restore files written by this change, without
overwriting files that diverged from both the recorded before/after contents and
modes. `rolled_back` means file contents/modes were restored; `indeterminate`
means conflicts, interruption or failed journaling need inspection. File timestamps,
inodes, ACLs and extended attributes are not restored. Private parent directories
created for the change can remain empty after rollback/undo.

After a crash or unknown result, `change apply` refuses to resume forward writes.
Inspect `change status` and the listed files, then explicitly authorize:

```sh
gitpro change recover --plan-id PLAN_ID -y
```

Recovery uses the original approval's rollback permission, never a new forward
attempt. It removes only recorded orphan native locks whose device/inode/change-time
metadata still match. Unknown or externally replaced locks are left alone.
A kill between creating a lock and recording its identity can leave a lock that
needs human inspection; recovery does not guess its owner. Changed/locked files
are preserved and reported, not force-restored. Backups remain in the private
ledger at the returned `backup_store` path; retain that ledger for retries and undo.
Proposals with credential-bearing URLs in their request/diff are refused, rather
than returning redacted text that could not reproduce the reviewed plan.
Diffs omit unchanged context but **changed configuration lines are shown exactly**.
Keep literal credentials out of TOML/Git settings and treat proposal files and
optional MCP logs as private; the backup database necessarily contains old bytes.

## MCP

The default `gitpro mcp` adds read-only `preflight`, `operation_status`, `reconcile_operation`,
`propose_change`, `change_status` and `draft_report` tools. `run_operation`, `apply_change` and
`recover_change` are neither listed nor callable unless the operator starts:

```sh
gitpro mcp --allow-execution
# Optional operator-scoped preauthorization:
gitpro mcp --allow-execution --approval-policy /absolute/operator/policy.json
```

Installers and the plugin continue to register the read-only server.
Changing that registration requires user approval. `preflight` takes `request`
and optional `online`; `run_operation` additionally takes `plan_token` and
`idempotency_key`. `operation_status` takes `idempotency_key`;
`reconcile_operation` also accepts optional `online`. CLI and MCP call the same
evaluator and executor.
`propose_change` takes a change `request`; `change_status`, `apply_change` and
`recover_change` take `plan_id`. `apply_change` additionally accepts the proposal's
normalized `request` to obtain a grant from the operator's startup policy. Clients
cannot select a policy path or grant approval. Apply requires recorded human
approval or a covering policy; recover uses the original rollback permission.
Both require host/operator execution authorization.

Existing `why`, `check_commit`, `env_for` and fail-open agent/Git hooks retain
their existing compatibility behavior. They are not substitutes for this strict,
operation-aware preflight and do not inherit execution authority.
The committer/own-author identity rule is shared with those hooks and diagnostics;
strict operation checks additionally enforce message, signing, destination and
execution-state requirements.
