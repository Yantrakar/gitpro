"""Agent preflight and explicitly authorized, identity-scoped commit/push/PR execution.

Existing human commands and fail-open hooks retain their contracts. These operation endpoints
fail closed and never edit identity configuration or switch a globally active forge account.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import re
import shlex
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from urllib.parse import quote, urlsplit

from . import config, forge, jsonout, policy, resolve, trailers
from .config import Config, ConfigError
from .operation_contract import (MAX_REQUEST, REQUEST_SCHEMA, RESULT_SCHEMA, VERSION,
                                 OperationError, Request, decode, digest, failure, request_key)
from .operation_io import (Ledger, Process, configuration_lock, file_hash, locked_index, process,
                           repository_lock, state_dir, termination_signals)

OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
MAX_COMMITS = 1000
TOKENS = (*forge._GH_TOKEN_VARS, *forge._GLAB_TOKEN_VARS)


def child_environment() -> Dict[str, str]:
    drop = {*resolve._REDIRECT_VARS, *TOKENS, "GIT_SSH_COMMAND", "GIT_SSH", "GIT_SSH_VARIANT",
            "GITPRO_SKIP", "GH_HOST", "GH_REPO", "GITLAB_HOST", "GIT_ASKPASS", "SSH_ASKPASS"}
    env = {k: v for k, v in os.environ.items()
           if k not in drop and not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_TRACE"))}
    # Keep inherited Git safety/config settings: removing all GIT_CONFIG_* would also remove policy.
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GCM_INTERACTIVE="never",
               GH_PROMPT_DISABLED="1", GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true",
               SSH_ASKPASS_REQUIRE="never", GIT_NO_LAZY_FETCH="1")
    return env


def _checked(result: Process, label: str, codes: Sequence[int] = (0,)) -> str:
    if result.stopped:
        raise OperationError(result.stopped, f"{label} did not complete.")
    if result.code not in codes:
        # Child output may contain arbitrary credentials from helpers/hooks. Do not echo it.
        raise OperationError("check_failed", f"{label} failed (exit {result.code}).")
    return result.out


def git(repo: Path, env: Dict[str, str], *args: str, codes: Sequence[int] = (0,),
        text: Optional[str] = None) -> str:
    return _checked(process(["git", *args], repo, env, text=text), "git " + args[0], codes)


def _config(repo: Path, env: Dict[str, str], key: str) -> str:
    return git(repo, env, "config", "--get", key, codes=(0, 1)).strip()


def _oid(value: str) -> str:
    if not OID.fullmatch(value):
        raise OperationError("invalid_response", "Git/forge returned an invalid object ID.")
    return value


def _ref(repo: Path, env: Dict[str, str], value: str) -> None:
    result = process(["git", "check-ref-format", "refs/heads/" + value], repo, env)
    if result.code or result.stopped:
        raise OperationError("invalid_ref", "Invalid branch name.", "needs_input")


@dataclass
class Plan:
    request: Request
    cfg: Config
    env: Dict[str, str]
    report: resolve.Report
    common: Path
    index: Path
    snapshot: dict
    destination: Optional[str] = None
    account: Optional[forge.Account] = None
    project: Optional[str] = None
    checks: List[dict] = field(default_factory=list)
    problems: List[dict] = field(default_factory=list)
    policies: list = field(default_factory=list)

    def problem(self, code: str, message: str, decision: str = "blocked",
                argv: Optional[Sequence[str]] = None) -> None:
        self.problems.append({"code": code, "message": forge.redact(message), "decision": decision,
                              "action": {"kind": "request_approval" if decision == "needs_approval" else "inspect",
                                         "argv": list(argv) if argv else None}})

    def check(self, code: str, message: str, status: str = "pass") -> None:
        self.checks.append({"code": code, "status": status, "message": message})

    @property
    def decision(self) -> str:
        for decision in ("blocked", "needs_approval", "needs_input", "indeterminate"):
            if any(p["decision"] == decision for p in self.problems):
                return decision
        return "ready"

    @property
    def token(self) -> str:
        return digest({"request": self.request.values, "snapshot": self.snapshot,
                       "identity": dataclasses.asdict(self.report.expected)})

    def result(self) -> dict:
        exp = self.report.expected
        return {"version": VERSION, "command": "preflight", "ok": self.decision == "ready",
                "decision": self.decision, "operation": self.request.operation, "repo": str(self.request.repo),
                "problems": self.problems, "checks": self.checks,
                "identity": {"id": exp.id or "default", "name": exp.name, "email": exp.email,
                             "sign": exp.sign, "ssh_key": exp.ssh_key,
                             "account": dataclasses.asdict(self.account) if self.account else None},
                "destination": resolve.redact_url(self.destination),
                "head": self.snapshot["head"], "plan_token": self.token if self.decision == "ready" else None,
                "effects": {"persistent_configuration": False, "network": self.request.operation != "commit",
                            "child_identity_isolated": True},
                "authorization": "A ready result is not permission to execute; retain host/user approval."}


def _destination(req: Request, env: Dict[str, str]) -> Optional[str]:
    remote = req.values.get("remote")
    if remote is None:
        return None
    if remote not in git(req.repo, env, "remote").splitlines():
        raise OperationError("unknown_remote", "Supply a configured remote name.", "needs_input")
    args = ["remote", "get-url", "--all"]
    if req.operation == "push":
        args.append("--push")
    urls = git(req.repo, env, *args, remote).splitlines()
    if len(urls) != 1:
        raise OperationError("ambiguous_destination", "Exactly one destination URL is required.", "needs_input")
    url = urls[0]
    if resolve.redact_url(url) != url or "?" in url or "\n" in url:
        raise OperationError("credential_url", "Use a remote without embedded credentials or query parameters.",
                             "needs_approval")
    if git(req.repo, env, "ls-remote", "--get-url", url).strip() != url:
        raise OperationError("chained_rewrite", "The resolved destination would be rewritten again; review remote URLs.",
                             "needs_approval")
    return url


def _snapshot(req: Request, cfg: Config, env: Dict[str, str], index: Path) -> dict:
    head = git(req.repo, env, "rev-parse", "--verify", "--quiet", "HEAD", codes=(0, 1)).strip()
    branch = git(req.repo, env, "symbolic-ref", "--quiet", "HEAD", codes=(0, 1)).strip()
    if index.is_symlink():
        raise OperationError("symlink_index", "The staged index must be a regular file.", "blocked")
    listing = git(req.repo, env, "config", "--null", "--list", "--show-origin")
    values = _config_values(listing)
    trust = {}
    for key in ("gpg.ssh.allowedsignersfile", "gpg.ssh.revocationfile"):
        name = values.get(key)
        if name:
            path = Path(name).expanduser()
            path = path if path.is_absolute() else req.repo / path
            trust[str(path)] = file_hash(path)
    return {"head": _oid(head) if head else None, "branch": branch, "index": file_hash(index),
            "git_config": digest(listing),
            "config": digest(dataclasses.asdict(cfg)), "config_file": file_hash(cfg.path), "git_trust": trust}


def _config_values(listing: str) -> Dict[str, str]:
    """{key: value} from `git config --null --list --show-origin`, the last value winning as with
    `git config --get`. Keys are as git lists them: section and name lower case, subsection as written."""
    fields = listing.split("\0")
    values = {}
    for entry in fields[1::2]:
        key, _, value = entry.partition("\n")
        values[key] = value
    return values


def _policies(plan: Plan) -> None:
    exp = plan.report.expected
    srcs = policy.sources(plan.cfg, exp.match.identity)
    local = plan.request.repo / policy.REPO_POLICY
    if local.exists():
        srcs.append((str(local), "repository"))
    loaded = [policy.load(p, check_perms=who != "repository",
                          inside=plan.request.repo if who == "repository" else None) for p, who in srcs]
    urls = [plan.destination] if plan.destination else list(plan.report.effective.remotes.values())
    plan.policies = policy.match(loaded, urls, plan.cfg)
    plan.snapshot["policies"] = {str(p.path): file_hash(Path(p.path)) for p in loaded}
    plan.snapshot["signers"] = {p.allowed_signers: file_hash(Path(p.allowed_signers))
                              for p in loaded if p.allowed_signers}


def _policy_base(plan: Plan, base: Optional[str]) -> None:
    """Never let a checkout's modified/deleted policy weaken the policy already in its base."""
    if not base:
        return
    paths = {str(plan.request.repo / policy.REPO_POLICY), *plan.snapshot["policies"], *plan.snapshot["signers"]}
    for raw in sorted(paths):
        path = Path(os.path.abspath(os.path.expanduser(raw)))
        try:
            rel = path.relative_to(plan.request.repo).as_posix()
        except ValueError:
            continue
        rows = git(plan.request.repo, plan.env, "ls-tree", "-z", base, "--", rel).split("\0")
        entry = next((row for row in rows if row.partition("\t")[2] == rel), None)
        if entry is None:
            continue
        mode = entry.split(" ", 1)[0]
        if mode not in ("100644", "100755"):
            raise OperationError("policy_base_invalid", "A policy/trust file in the base is not a regular file.", "blocked")
        base_text = git(plan.request.repo, plan.env, "cat-file", "blob", base + ":" + rel)
        if not path.is_file() or path.is_symlink() or path.read_text(encoding="utf-8") != base_text:
            plan.problem("policy_changed", "A repository policy/trust file differs from the committed base. "
                         "Review that change separately; it cannot authorize its own operation.", "needs_approval")


def _message_problems(plan: Plan, message: str, author: str, committer: str, *, signed: bool,
                      signing_format: Optional[str], own_commit: bool = True, merge: bool = False) -> None:
    exp = plan.report.expected
    found = trailers.split_lines(git(plan.request.repo, plan.env, "interpret-trailers", "--parse",
                                     "--no-divider", text=message))
    tp = trailers.policy_for(exp.match.identity, plan.cfg.default)
    if merge and tp.co_authored_by == "require":
        tp = dataclasses.replace(tp, co_authored_by="allow")
    if own_commit:
        for msg in trailers.check(found, tp):
            plan.problem("trailer_policy", msg)
    for pol, rules in plan.policies:
        for email in (author, committer):
            violation = policy.email_problem(rules, email)
            if violation:
                plan.problem("policy_" + violation[0], violation[1])
        for code, msg in policy.trailer_problems(pol, rules, found, merge):
            plan.problem("policy_" + code, msg)
        if any(r.require_signing for r in rules) and not signed:
            plan.problem("policy_signing", "The applicable policy requires a signed commit.")
        if signed and any(r.signing_formats and signing_format not in r.signing_formats for r in rules):
            plan.problem("policy_signing_format", "The signature format is not permitted by policy.")


def _account(plan: Plan, required: bool = True) -> None:
    exp = plan.report.expected
    identity = exp.match.identity
    host, owner, name = resolve.parse_url(plan.destination or exp.match.url or "")
    if identity:
        host = identity.host
        login = identity.gh_account
    else:
        login = plan.cfg.default.gh_account if host == forge.GITHUB else None
    if not login or not host or not owner or not name:
        if required:
            raise OperationError("missing_account", "Configure a GitHub account for the destination identity.",
                                 "needs_input")
        return
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or not re.fullmatch(r"[A-Za-z0-9-]+", login):
        raise OperationError("invalid_account", "Invalid GitHub host/account.", "needs_input")
    if "/" in owner or not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise OperationError("unsupported_forge", "This operation supports GitHub repositories only.", "needs_input")
    plan.account = forge.Account(host, "gh", login)
    plan.project = owner + "/" + name.removesuffix(".git")


def _authenticate(plan: Plan) -> None:
    if plan.account is None:
        raise OperationError("missing_account", "A configured GitHub account is required.", "needs_input")
    # Reuse the existing credential resolver; take only its selected credential, never its ambient env.
    scoped = forge.gh_env(plan.account.host, plan.account.login, base_env=plan.env)
    for key in ("GH_HOST", "GH_TOKEN", "GH_ENTERPRISE_TOKEN"):
        if key in scoped:
            plan.env[key] = scoped[key]
    login = _checked(process(["gh", "api", "user", "--jq", ".login"], plan.request.repo, plan.env),
                     "GitHub account verification").strip()
    if login.casefold() != plan.account.login.casefold():
        raise OperationError("account_mismatch", "The credential does not belong to the configured account.", "blocked")
    plan.check("forge_account", "The selected credential belongs to the configured GitHub account.")


def _api(plan: Plan, endpoint: str, *, params: Optional[dict] = None) -> object:
    argv = ["gh", "api", "--method", "GET", endpoint]
    for key, value in (params or {}).items():
        argv += ["-f", f"{key}={value}"]
    text = _checked(process(argv, plan.request.repo, plan.env), "GitHub read")
    try:
        return json.loads(text)
    except ValueError:
        raise OperationError("invalid_response", "GitHub returned an invalid JSON response.") from None


def _transport(plan: Plan, online: bool) -> None:
    url = plan.destination or ""
    host, _, _ = resolve.parse_url(url)
    if url.startswith("https://"):
        _account(plan)
        headers = git(plan.request.repo, plan.env, "config", "--get-regexp",
                      r"^http(\..*)?\.extraheader$", codes=(0, 1))
        if headers:
            raise OperationError("http_header_override", "Configured HTTP headers could override the selected account. "
                                 "Review/remove them before scoped HTTPS execution.", "needs_approval")
        if online:
            _authenticate(plan)
        # Empty helper clears helpers that could borrow another identity.
        plan.env.update(GIT_CONFIG_COUNT=str(int(plan.env.get("GIT_CONFIG_COUNT", "0")) + 2))
        n = int(plan.env["GIT_CONFIG_COUNT"]) - 2
        plan.env.update({f"GIT_CONFIG_KEY_{n}": "credential.helper", f"GIT_CONFIG_VALUE_{n}": "",
                         f"GIT_CONFIG_KEY_{n + 1}": "credential.helper",
                         f"GIT_CONFIG_VALUE_{n + 1}": "!gh auth git-credential"})
    elif host and (url.startswith("ssh://") or ("://" not in url and ":" in url)):
        key = plan.report.expected.ssh_key
        if not key or not Path(key).is_file():
            raise OperationError("missing_ssh_key", "The destination identity needs an existing SSH key.", "needs_input")
        plan.env["GIT_SSH_COMMAND"] = (f"ssh -F /dev/null -i {shlex.quote(key)} -o IdentitiesOnly=yes "
                                       "-o BatchMode=yes -o StrictHostKeyChecking=yes -o UpdateHostKeys=no")
        identity = plan.report.expected.match.identity
        if identity and identity.ssh_alias and host == identity.ssh_alias:
            plan.env["GIT_SSH_COMMAND"] += (" -o " + shlex.quote("HostName=" + identity.host) +
                                             " -p " + str(identity.ssh_port))
        plan.snapshot["ssh_key_stat"] = _key_stat(Path(key))
        if online:
            _ssh_account(plan, host)
    elif url.startswith("file://") or Path(url).is_absolute():
        pass
    else:
        raise OperationError("unsupported_transport", "Use SSH, GitHub HTTPS, or an absolute local remote.",
                             "needs_input")


def _ssh_account(plan: Plan, host: str) -> None:
    from .health import parse_greeting
    identity = plan.report.expected.match.identity
    person = identity or plan.cfg.default
    gh_account = person.gh_account
    glab_account = person.glab_account
    if not gh_account and not glab_account:
        plan.check("ssh_account", "No forge login configured; only the configured SSH key and destination access are checked.",
                   "skip")
        return
    url = plan.destination or ""
    user, port = "git", None
    if url.startswith("ssh://"):
        parsed = urlsplit(url)
        user, port = parsed.username or "git", parsed.port
    else:
        match = resolve._SCP_RE.match(url)
        if match:
            user = match[1] or "git"
    argv = shlex.split(plan.env["GIT_SSH_COMMAND"]) + ["-T"]
    if port:
        argv += ["-p", str(port)]
    result = process([*argv, user + "@" + host], plan.request.repo, plan.env)
    if result.stopped or result.code not in (0, 1):
        raise OperationError("ssh_account_unknown", "The selected SSH key's forge account could not be checked.")
    provider, login = parse_greeting(result.out + result.err)
    expected = gh_account if provider == "github" else glab_account if provider == "gitlab" else None
    if not login or not expected:
        raise OperationError("ssh_account_unknown", "The SSH server did not identify a configured forge account.")
    if login.casefold() != expected.casefold():
        raise OperationError("ssh_account_mismatch", "The selected key authenticates as another forge account.", "blocked")
    plan.check("ssh_account", "The selected SSH key authenticates as the configured forge account.")


def _key_stat(path: Path) -> list:
    st = path.stat()
    return [str(path), st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode]


def _verify_signature(plan: Plan, oid: str, fmt: Optional[str], committer: str) -> None:
    tmp: List[str] = []
    try:
        pre, ships = policy._signers_args(plan.policies, tmp)
        text = _checked(process(["git", *pre, "show", "-s", "--format=%G?%x00%GK%x00%GS", oid],
                                plan.request.repo, plan.env), "Signature verification")
        fields = text.rstrip("\n").split("\0")
        if len(fields) != 3:
            raise OperationError("invalid_response", "Could not read the verified signature.")
        status, key, signer = fields
        status = policy.classify(status, fmt, signer)
        if status not in ("G", "U"):
            raise OperationError("signature_unverified", "The signature could not be verified as valid.")
        signers = [p.allowed_signers for p, _ in plan.policies if p.allowed_signers]
        for path in signers:
            Path(path).read_text(encoding="utf-8")  # A missing/unreadable trust file is not an empty policy.
        principals = policy.signer_principals(signers).get(key)
        for _, rules in plan.policies:
            for code, msg in policy.signing_problems(rules, status, fmt, ships, signer=signer,
                                                      committer=committer, principals=principals):
                plan.problem("policy_" + code, msg)
    finally:
        for path in tmp:
            Path(path).unlink()


def _history(plan: Plan, base: Optional[str], head: str) -> None:
    repo, env = plan.request.repo, plan.env
    if base:
        available = process(["git", "cat-file", "-e", base + "^{commit}"], repo, env)
        if available.code or available.stopped:
            raise OperationError("missing_history", "Fetch the destination history before retrying preflight.",
                                 "needs_input")
        ancestor = process(["git", "merge-base", "--is-ancestor", base, head], repo, env)
        if ancestor.stopped or ancestor.code not in (0, 1):
            raise OperationError("check_failed", "Could not compare destination history.")
        if ancestor.code:
            plan.problem("non_fast_forward", "The destination is not an ancestor of the proposed head.")
            return
    args = [head] + ([f"^{base}"] if base else [])
    commits = git(repo, env, "rev-list", f"--max-count={MAX_COMMITS + 1}", *args, "--").splitlines()
    if len(commits) > MAX_COMMITS:
        raise OperationError("history_limit", "More than 1000 outgoing commits; review/push a smaller range.",
                             "needs_input")
    own = {e.casefold() for e in resolve.own_emails(plan.cfg)}
    expected = (plan.report.expected.email or "").casefold()
    signing_policy = any(r.signing() for _, rules in plan.policies for r in rules)
    for oid in commits:
        _oid(oid)
        raw = git(repo, env, "show", "-s", "--format=%ce%x00%ae%x00%B", oid)
        parts = raw.split("\0", 2)
        if len(parts) != 3 or "\0" in parts[-1]:
            raise OperationError("invalid_response", "Could not read an outgoing commit.")
        committer, author, message = parts
        if any(e.casefold() in own and e.casefold() != expected for e in (committer, author)):
            plan.problem("outgoing_identity", f"Commit {oid} uses another configured identity.")
        body = git(repo, env, "cat-file", "commit", oid).encode()
        fmt = policy._sig_format(body)
        merge = sum(line.startswith(b"parent ") for line in body.split(b"\n\n", 1)[0].splitlines()) > 1
        _message_problems(plan, message, author, committer, signed=fmt is not None, signing_format=fmt,
                          own_commit=committer.casefold() in own, merge=merge)
        if signing_policy and fmt is not None:
            _verify_signature(plan, oid, fmt, committer)
    plan.check("outgoing_history", f"Checked {len(commits)} outgoing commits against the destination identity/policy.")
    _policy_base(plan, base)


def _remote_head(plan: Plan) -> Optional[str]:
    ref = "refs/heads/" + plan.request.values["branch"]
    result = process(["git", "ls-remote", "--exit-code", str(plan.destination), ref],
                     plan.request.repo, plan.env)
    text = _checked(result, "Destination ref lookup", (0, 2))
    if result.code == 2 and not text.strip():
        return None
    rows = [line.split("\t") for line in text.splitlines()]
    if len(rows) != 1 or rows[0][1:] != [ref]:
        raise OperationError("invalid_response", "Could not resolve exactly one destination ref.")
    return _oid(rows[0][0])


def _commit_checks(plan: Plan) -> None:
    req, exp, env = plan.request, plan.report.expected, plan.env
    _policy_base(plan, plan.snapshot["head"])
    for p in plan.report.problems:
        if p.code != "key_mismatch":
            plan.problem(p.code, p.message, "needs_input" if p.code == "no_identity" else "needs_approval",
                         p.fix_argv)
    if not exp.name or not exp.email:
        plan.problem("missing_identity", "Configure a name and email before executing.", "needs_input")
    author = req.values.get("author_email", exp.email or "")
    _, bad_author = resolve.identity_conflicts(exp.email, resolve.own_emails(plan.cfg), author, exp.email)
    if bad_author:
        plan.problem("author_identity", "The proposed author is another of your configured identities.")
    markers = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
    paths = git(req.repo, env, "rev-parse", "--path-format=absolute",
                *(a for m in markers for a in ("--git-path", m))).splitlines()
    if len(paths) != len(markers):
        raise OperationError("check_failed", "Could not locate Git's in-progress markers.")
    for path in paths:
        if Path(path).exists():
            plan.problem("operation_in_progress", "Finish the existing Git operation first.")
    changes = process(["git", "diff", "--cached", "--quiet", "--exit-code", "--no-ext-diff"], req.repo, env)
    if changes.stopped or changes.code not in (0, 1):
        raise OperationError("check_failed", "Could not inspect staged changes.")
    if changes.code == 0:
        plan.problem("nothing_staged", "Stage the intended changes before preflight.", "needs_input")
    if git(req.repo, env, "ls-files", "--unmerged"):
        plan.problem("unmerged_index", "Resolve staged conflicts first.")
    fmt = _config(req.repo, env, "gpg.format") or "openpgp"
    fmt = "gpg" if fmt == "openpgp" else fmt
    if exp.sign:
        if not exp.signingkey:
            plan.problem("missing_signing_key", "The configured signing key is missing.", "needs_input")
        elif fmt == "ssh" and Path(exp.signingkey).is_file():
            plan.snapshot["signing_key_stat"] = _key_stat(Path(exp.signingkey))
            signers = [p.allowed_signers for p, _ in plan.policies if p.allowed_signers]
            if signers:
                public = Path(exp.signingkey)
                if public.suffix != ".pub":
                    public = public.with_name(public.name + ".pub")
                if not public.is_file():
                    plan.problem("missing_public_key", "A public signing key is needed to check policy principals.",
                                 "needs_input")
                else:
                    parts = public.read_text(encoding="utf-8").split()
                    fingerprint = policy.ssh_fingerprint(parts[1]) if len(parts) >= 2 else None
                    principals = policy.signer_principals(signers).get(fingerprint or "")
                    if not principals:
                        plan.problem("signer_unknown", "The public key has no directly verifiable policy principal.",
                                     "indeterminate")
                    elif not policy.principal_ok(principals, exp.email):
                        plan.problem("policy_allowed_signers", "The policy signing key does not permit this committer email.")
                    plan.snapshot["public_signing_key"] = file_hash(public)
    _message_problems(plan, req.values["message"], author, exp.email or "", signed=exp.sign, signing_format=fmt)
    plan.check("staged_commit", "Only the reviewed staged index is committed; existing Git hooks still run.")


def _pr_checks(plan: Plan) -> None:
    req = plan.request
    _account(plan)
    _authenticate(plan)
    prefix = "repos/" + str(plan.project)
    head, base = req.values["head"], req.values["base"]
    if head == base or plan.snapshot["branch"] != "refs/heads/" + head:
        raise OperationError("branch_mismatch", "head must be this worktree's branch and differ from base.", "blocked")
    refs = {}
    for name in (base, head):
        data = _api(plan, prefix + "/git/ref/heads/" + quote(name, safe=""))
        if not isinstance(data, dict) or not isinstance(data.get("object"), dict):
            raise OperationError("invalid_response", "GitHub did not return the requested branch.")
        refs[name] = _oid(str(data["object"].get("sha", "")))
    if refs[head] != plan.snapshot["head"]:
        raise OperationError("head_not_pushed", "Push the current head before creating the PR.", "needs_input")
    prs = _api(plan, prefix + "/pulls", params={"state": "open", "head": plan.project.split("/")[0] + ":" + head,
                                               "base": base, "per_page": "100"})
    if not isinstance(prs, list) or len(prs) > 1 or (prs and not isinstance(prs[0], dict)):
        raise OperationError("invalid_response", "Could not identify a unique existing PR.")
    plan.snapshot.update(remote_refs=refs, existing_pr=_pr_url(plan, prs[0]) if prs else None)
    # GitHub allows a base that has advanced independently; inspect commits unique to head.
    base_oid = refs[base]
    merge = git(req.repo, plan.env, "merge-base", base_oid, refs[head]).strip()
    _history(plan, _oid(merge), refs[head])
    _policy_base(plan, base_oid)
    plan.check("pr_destination", "The configured GitHub account, pushed head, base and existing PR were checked.")


def _pr_url(plan: Plan, data: object) -> str:
    if not isinstance(data, dict) or not plan.account:
        raise OperationError("invalid_response", "GitHub did not return a pull request.")
    url = data.get("html_url")
    prefix = "https://" + plan.account.host + "/" + str(plan.project) + "/pull/"
    if not isinstance(url, str) or not url.startswith(prefix) or not url[len(prefix):].isdigit():
        raise OperationError("invalid_response", "GitHub did not return a valid pull request URL.")
    return url


def build(req: Request, cfg_path: Optional[Path] = None, *, online: bool = False) -> Plan:
    state_dir()
    env = child_environment()
    resolve.require_git()
    if not req.repo.is_dir():
        raise OperationError("not_repository", "repo must be an existing worktree.", "needs_input")
    top = git(req.repo, env, "rev-parse", "--show-toplevel").strip()
    if Path(top).resolve() != req.repo:
        raise OperationError("not_worktree_root", "Supply the canonical worktree root.", "needs_input")
    path, how = config.find(cfg_path, Path.home())
    cfg = config.load_all(path, how)
    env["GITPRO_CONFIG"] = str(cfg.path)
    common = Path(git(req.repo, env, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    index = Path(git(req.repo, env, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
    destination = _destination(req, env)
    rep = resolve.compare(req.repo, cfg, remote=req.values.get("remote"), remote_url=destination,
                          env=env, git_env=env, extra_checks=False)
    snapshot = _snapshot(req, cfg, env, index)
    plan = Plan(req, cfg, env, rep, common, index, snapshot, destination)
    for key in ("branch", "base", "head"):
        if key in req.values:
            _ref(req.repo, env, req.values[key])
    _policies(plan)
    if req.operation == "commit":
        _commit_checks(plan)
    else:
        if not snapshot["head"]:
            plan.problem("missing_head", "Create a commit first.", "needs_input")
        if not rep.expected.email:
            plan.problem("missing_identity", "Configure an identity for the destination.", "needs_input")
        if req.operation == "push":
            _transport(plan, online)
        else:
            _account(plan)
        if not online:
            plan.problem("network_approval", "Destination checks and execution require explicit --online.",
                         "needs_approval")
            plan.check("destination", "Not checked offline.", "unknown")
        elif not plan.problems:
            if req.operation == "push":
                remote_head = _remote_head(plan)
                plan.snapshot["remote_head"] = remote_head
                _history(plan, remote_head, snapshot["head"])
                plan.check("destination", "The exact destination ref was read; execution uses a compare-and-swap lease.")
            else:
                _pr_checks(plan)
    if any(r.require_verified_email for _, rules in plan.policies for r in rules):
        if not online:
            plan.problem("verified_email_unknown", "Email verification requires explicit --online.", "needs_approval")
        else:
            if plan.account is None:
                _account(plan)
                _authenticate(plan)
            elif not any(c["code"] == "forge_account" for c in plan.checks):
                _authenticate(plan)
            emails = _api(plan, "user/emails")
            if not isinstance(emails, list):
                raise OperationError("invalid_response", "Could not check verified email.")
            if not any(isinstance(e, dict) and e.get("verified") is True and
                       str(e.get("email", "")).casefold() == (rep.expected.email or "").casefold() for e in emails):
                plan.problem("email_not_verified", "The configured email is not verified on this account.")
    # Detect changes even during preflight; don't mint a token for a mixed snapshot.
    current = _snapshot(req, cfg, child_environment(), index)
    if any(current[k] != snapshot[k] for k in current):
        raise OperationError("state_changed", "Repository/configuration changed during preflight; inspect again.")
    plan.check("policy", "Applicable local policies were evaluated explicitly; checker errors stop execution.")
    return plan


def preflight(raw: object, cfg_path: Optional[Path] = None, *, online: bool = False) -> dict:
    req = None
    try:
        req = Request.parse(raw)
        return build(req, cfg_path, online=online).result()
    except OperationError as exc:
        return failure("preflight", exc, req)
    except ConfigError as exc:
        return failure("preflight", OperationError("configuration_error", forge.redact(str(exc)), "needs_input"), req)
    except (OSError, ValueError) as exc:
        return failure("preflight", OperationError("check_failed", f"Preflight failed ({type(exc).__name__})."), req)
    except KeyboardInterrupt:
        return failure("preflight", OperationError("cancelled", "Preflight cancelled; no operation was started."), req)


def _commit(plan: Plan) -> dict:
    req, exp = plan.request, plan.report.expected
    if exp.name is None or exp.email is None:
        raise OperationError("missing_identity", "A configured committer name and email are required.", "needs_input")
    env = dict(plan.env)
    env.update(GIT_AUTHOR_NAME=req.values.get("author_name", exp.name),
               GIT_AUTHOR_EMAIL=req.values.get("author_email", exp.email),
               GIT_COMMITTER_NAME=exp.name, GIT_COMMITTER_EMAIL=exp.email)
    # Do not attach forge tokens to local commits or their hooks.
    for key in TOKENS:
        env.pop(key, None)
    with locked_index(plan.index, plan.snapshot["index"]) as private:
        env["GIT_INDEX_FILE"] = str(private)
        tree = git(req.repo, env, "write-tree").strip()
        signing = ["-c", f"commit.gpgsign={'true' if exp.sign else 'false'}"]
        if exp.sign and exp.signingkey:
            signing += ["-c", "user.signingkey=" + exp.signingkey]
        result = process(["git", *signing, "commit", "--cleanup=verbatim", "--file=-"], req.repo, env,
                         text=req.values["message"])
        head = git(req.repo, env, "rev-parse", "--verify", "HEAD").strip()
        if result.stopped or result.code:
            if head != plan.snapshot["head"] or result.stopped:
                raise OperationError("outcome_unknown", "Commit execution was interrupted or changed HEAD. Inspect history.")
            raise OperationError("execution_failed", "Git refused the commit; HEAD is unchanged.", "blocked")
        meta = git(req.repo, env, "show", "-s", "--format=%T%x00%P%x00%cn%x00%ce%x00%an%x00%ae", head).strip().split("\0")
        expected = [tree, plan.snapshot["head"] or "", exp.name, exp.email,
                    env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"]]
        if meta != expected or file_hash(plan.index) != plan.snapshot["index"]:
            raise OperationError("outcome_unknown", "The resulting commit/index differs from the reviewed operation.")
        message = git(req.repo, env, "show", "-s", "--format=%B", head)
        body = git(req.repo, env, "cat-file", "commit", head).encode()
        committed_message = body.split(b"\n\n", 1)[1].decode("utf-8")
        if committed_message.rstrip("\n") != req.values["message"].rstrip("\n"):
            raise OperationError("outcome_unknown", "A hook changed the reviewed commit message; inspect the new commit.")
        fmt = policy._sig_format(body)
        _message_problems(plan, message, env["GIT_AUTHOR_EMAIL"], exp.email,
                          signed=fmt is not None, signing_format=fmt)
        if exp.sign:
            _verify_signature(plan, head, fmt, exp.email)
        if plan.problems or (fmt is not None) != exp.sign:
            raise OperationError("outcome_unknown", "A hook changed the resulting commit's policy compliance; inspect history.")
        return {"commit": _oid(head), "tree": _oid(tree)}


def _push(plan: Plan) -> dict:
    req = plan.request
    old, head = plan.snapshot["remote_head"], plan.snapshot["head"]
    if old == head:
        return {"remote_oid": head, "already_present": True}
    ref = "refs/heads/" + req.values["branch"]
    # Fast-forward ancestry was checked above; the explicit lease prevents races, not history rewriting.
    argv = ["git", "-c", f"remote.{req.values['remote']}.mirror=false", "push", "--porcelain", "--atomic",
            "--no-follow-tags", "--recurse-submodules=no", f"--force-with-lease={ref}:{old or ''}",
            req.values["remote"], f"{head}:{ref}"]
    result = process(argv, req.repo, plan.env)
    current = _remote_head(plan)
    if current == head:
        return {"remote_oid": head, "already_present": False}
    if result.stopped or result.code == 0 or current != old:
        raise OperationError("outcome_unknown", "The push outcome could not be verified. Inspect the remote ref.")
    raise OperationError("execution_failed", "The remote rejected the push and its ref is unchanged.", "blocked")


def _create_pr(plan: Plan) -> dict:
    if plan.snapshot.get("existing_pr"):
        return {"pr_url": plan.snapshot["existing_pr"], "already_present": True}
    req = plan.request
    body = {k: req.values[k] for k in ("title", "body", "base", "head", "draft")}
    result = process(["gh", "api", "--method", "POST", "repos/" + str(plan.project) + "/pulls", "--input", "-"],
                     req.repo, plan.env, text=json.dumps(body))
    if result.stopped or result.code:
        raise OperationError("outcome_unknown", "PR creation did not return a confirmed result. Inspect GitHub before retrying.")
    try:
        data = json.loads(result.out)
    except ValueError:
        raise OperationError("outcome_unknown", "PR creation returned an unreadable result; inspect GitHub.") from None
    return {"pr_url": _pr_url(plan, data), "already_present": False}


def execute(raw: object, token: str, key: str, cfg_path: Optional[Path] = None, *,
            online: bool = False, authorized: bool = False) -> dict:
    req = None
    claimed = False
    ledger = None
    result: dict
    try:
        req = Request.parse(raw)
        request_key(key)
        if not authorized:
            raise OperationError("approval_required", "Execution requires explicit host/user authorization.",
                                 "needs_approval")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
            raise OperationError("invalid_plan", "Supply the ready preflight's plan_token.", "needs_input")
        fingerprint = digest({"request": req.fingerprint, "plan": token})
        ledger = Ledger()
        previous = ledger.get(key, fingerprint)
        if previous is not None:
            ledger.close()
            return previous
        with contextlib.ExitStack() as locks:
            locks.enter_context(configuration_lock())
            plan = build(req, cfg_path, online=online)
            locks.enter_context(repository_lock(plan.common))
            previous = ledger.get(key, fingerprint)
            if previous is not None:
                ledger.close()
                return previous
            if plan.decision != "ready":
                result = dict(plan.result(), command="run", status="blocked")
            else:
                fresh = build(req, cfg_path, online=online)
                if fresh.decision != "ready" or fresh.token != token:
                    raise OperationError("stale_plan", "The reviewed state changed. Run preflight and review again.", "blocked")
                from .operation_reconcile import capture
                ledger.claim(key, fingerprint, intent=capture(fresh))
                claimed = True
                outcome = {"commit": _commit, "push": _push, "pr-create": _create_pr}[req.operation](fresh)
                result = {"version": VERSION, "command": "run", "ok": True, "decision": "ready",
                          "status": "succeeded", "operation": req.operation, "repo": str(req.repo),
                          "problems": [], "outcome": outcome}
    except OperationError as exc:
        result = failure("run", exc, req)
        if claimed:
            result["status"] = "failed" if exc.code == "execution_failed" else "indeterminate"
            if result["status"] == "indeterminate":
                result["decision"] = "indeterminate"
    except (OSError, ConfigError, ValueError, sqlite3.Error) as exc:
        result = failure("run", OperationError("operation_failed", f"Operation failed ({type(exc).__name__}); "
                                               "inspect status and effects before retrying."), req)
        result["status"] = "indeterminate" if claimed else "blocked"
    except KeyboardInterrupt:
        result = failure("run", OperationError("cancelled", "Operation cancelled. Inspect its effects before retrying."), req)
        result["status"] = "indeterminate" if claimed else "blocked"
    result.update(idempotency_key=key, replayed=False)
    if ledger is not None:
        try:
            if claimed:
                ledger.finish(key, result)
        except (OSError, sqlite3.Error):
            result = failure("run", OperationError("journal_failed", "Could not persist the outcome. "
                                                  "The operation may have succeeded; do not repeat it."), req)
            result.update(status="indeterminate", idempotency_key=key, replayed=False)
        finally:
            ledger.close()
    return result


def status(key: str) -> dict:
    try:
        request_key(key)
        with contextlib.closing(Ledger(readonly=True)) as ledger:
            result = ledger.get(key)
        if result is None:
            raise OperationError("unknown_request", "No operation has this key.", "needs_input")
        return dict(result, command="operation-status")
    except OperationError as exc:
        return failure("operation-status", exc)
    except (OSError, ValueError, sqlite3.Error):
        return failure("operation-status", OperationError("journal_failed", "Could not read the operation ledger."))


def _read_request(path: str) -> object:
    if path == "-":
        return decode(sys.stdin.read(MAX_REQUEST + 1))
    with open(path, encoding="utf-8") as stream:
        return decode(stream.read(MAX_REQUEST + 1))


def _emit(result: dict) -> int:
    jsonout.emit(result["command"], result)
    return 0 if result["ok"] else (3 if result["decision"] == "blocked" else 2)


def _command(args: argparse.Namespace) -> int:
    if args.command == "operation-reconcile":
        from .operation_reconcile import SCHEMA, reconcile
        if args.schema:
            jsonout.emit("operation-reconcile-schema", {"version": VERSION, "result": SCHEMA})
            return 0
        return _emit(reconcile(args.idempotency_key, online=args.online))
    if getattr(args, "schema", False):
        jsonout.emit("preflight-schema", {"version": VERSION, "request": REQUEST_SCHEMA, "result": RESULT_SCHEMA})
        return 0
    if args.command == "operation-status":
        return _emit(status(args.idempotency_key))
    try:
        if not args.request:
            raise OperationError("missing_input", "Supply --request FILE (or - for stdin).", "needs_input")
        raw = _read_request(args.request)
    except (OSError, UnicodeError):
        return _emit(failure(args.command, OperationError("invalid_request", "Could not read the request.", "needs_input")))
    except OperationError as exc:
        return _emit(failure(args.command, exc))
    if args.command == "preflight" or not (args.yes or args.write):
        return _emit(preflight(raw, args.config, online=args.online))
    return _emit(execute(raw, args.plan_token, args.idempotency_key, args.config,
                         online=args.online, authorized=args.yes or args.write))


def command(args: argparse.Namespace) -> int:
    with termination_signals():
        return _command(args)


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser,
             confirm: argparse.ArgumentParser) -> None:
    from .args import capability
    for name in ("preflight", "run"):
        p = sub.add_parser(name, parents=[common, confirm] if name == "run" else [common],
                           help="structured agent " + ("operation checks" if name == "preflight" else "execution"))
        p.add_argument("--request", metavar="FILE", help="versioned JSON request (- reads stdin)")
        p.add_argument("--online", action="store_true", help="authorize destination/account network checks")
        p.add_argument("--json", action="store_true", help="JSON is always emitted")
        if name == "preflight":
            p.add_argument("--schema", action="store_true", help="print the request and result JSON Schemas")
        else:
            p.add_argument("--plan-token", default="", help="token from the reviewed, ready preflight")
            p.add_argument("--idempotency-key", default="", help="stable key for this operation and its retries")
        p.set_defaults(func=command)
    p = sub.add_parser("operation-status", help="read a durable operation result without executing")
    p.add_argument("--idempotency-key", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=command)
    p = sub.add_parser("operation-reconcile", help="observe recorded intent without repeating a write")
    p.add_argument("--idempotency-key", default="")
    p.add_argument("--online", action="store_true", help="authorize remote/account reads")
    p.add_argument("--schema", action="store_true", help="print the observation result JSON Schema")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=command)
    capability("preflight.v1", "run.v1", "operation-status.v1")
    capability("operation-reconcile.v1")
