"""Read-only postcondition evidence, never a remote-write retry or proof of attribution."""
from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
from pathlib import Path
from typing import Optional

from . import config, operations as op, policy, resolve
from .operation_contract import DECISIONS, OperationError, Request, digest, failure, request_key
from .operation_io import Ledger, configuration_lock, file_hash, repository_lock

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["version", "command", "ok", "decision", "problems", "status", "retry_safe", "attribution"],
    "properties": {
        "version": {"const": 1}, "command": {"const": "operation-reconcile"},
        "ok": {"type": "boolean"}, "decision": {"enum": list(DECISIONS)},
        "problems": {"type": "array", "items": {"type": "object"}},
        "operation": {"type": ["string", "null"]}, "repo": {"type": ["string", "null"]},
        "idempotency_key": {"type": "string"}, "ledger_status": {"type": "string"},
        "status": {"enum": ["observed", "indeterminate"]},
        "observation": {"type": "object", "additionalProperties": False, "properties": {
            "commit": {"type": "string"}, "remote_oid": {"type": ["string", "null"]},
            "pr_url": {"type": "string"}, "desired_state_observed": {"type": "boolean"}}},
        "retry_safe": {"const": False}, "attribution": {"const": "not_proven"},
    },
}


def capture(plan: op.Plan) -> dict:
    return {"version": 1, "request": plan.request.values, "plan_token": plan.token,
            "config_path": str(plan.cfg.path), "config_how": plan.cfg.how, "snapshot": plan.snapshot,
            "common_dir": str(plan.common), "index": str(plan.index),
            "destination": plan.destination, "expected_digest": digest(dataclasses.asdict(plan.report.expected)),
            "account": dataclasses.asdict(plan.account) if plan.account else None, "project": plan.project}


def _context(intent: dict) -> op.Plan:
    req = Request.parse(intent["request"])
    env = op.child_environment()
    path = Path(intent["config_path"])
    cfg = config.load_all(path, intent["config_how"])
    env["GITPRO_CONFIG"] = str(path)
    index = Path(op.git(req.repo, env, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
    common = Path(op.git(req.repo, env, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    top = op.git(req.repo, env, "rev-parse", "--show-toplevel").strip()
    if common != Path(intent["common_dir"]) or Path(top).resolve() != req.repo or index != Path(intent["index"]):
        raise OperationError("reconciliation_context_changed", "Repository layout changed; inspect the original destination.")
    destination = op._destination(req, env)
    report = resolve.compare(req.repo, cfg, remote=req.values.get("remote"), remote_url=destination,
                             env=env, git_env=env, extra_checks=False)
    snapshot = op._snapshot(req, cfg, env, index)
    for key in ("git_config", "config", "config_file", "git_trust"):
        if snapshot[key] != intent["snapshot"][key]:
            raise OperationError("reconciliation_context_changed", "Configuration or trust changed; automatic observation "
                                 "will not borrow another identity.")
    if destination != intent["destination"] or digest(dataclasses.asdict(report.expected)) != intent["expected_digest"]:
        raise OperationError("reconciliation_context_changed", "The destination or configured identity changed.")
    plan = op.Plan(req, cfg, env, report, common, index, dict(intent["snapshot"]), destination)
    op._policies(plan)
    for key in ("policies", "signers"):
        if plan.snapshot[key] != intent["snapshot"][key]:
            raise OperationError("reconciliation_context_changed", "Applicable policy/trust files changed.")
    key_stat = intent["snapshot"].get("ssh_key_stat")
    if key_stat and op._key_stat(Path(key_stat[0])) != key_stat:
        raise OperationError("reconciliation_context_changed", "The selected SSH key changed.")
    if req.operation != "commit" and intent["account"] is not None:
        op._account(plan)
        if plan.account is None or dataclasses.asdict(plan.account) != intent["account"] or plan.project != intent["project"]:
            raise OperationError("reconciliation_context_changed", "The selected forge account changed.")
    return plan


def _commit(plan: op.Plan) -> dict:
    req, env, exp = plan.request, plan.env, plan.report.expected
    head = op.git(req.repo, env, "rev-parse", "--verify", "--quiet", "HEAD", codes=(0, 1)).strip()
    if not head:
        return {"desired_state_observed": False}
    op._oid(head)
    observation = {"commit": head, "desired_state_observed": False}
    if head == plan.snapshot["head"] or file_hash(plan.index) != plan.snapshot["index"]:
        return observation
    author_name = req.values.get("author_name", exp.name)
    author_email = req.values.get("author_email", exp.email)
    meta = op.git(req.repo, env, "show", "-s", "--format=%P%x00%cn%x00%ce%x00%an%x00%ae", head).rstrip("\n").split("\0")
    if meta != [plan.snapshot["head"] or "", exp.name, exp.email, author_name, author_email]:
        return observation
    diff = op.process(["git", "diff", "--cached", "--quiet", "--no-ext-diff", head, "--"], req.repo, env)
    op._checked(diff, "Reviewed index comparison", (0, 1))
    if diff.code:
        return observation
    body = op.git(req.repo, env, "cat-file", "commit", head).encode()
    message = body.split(b"\n\n", 1)[1].decode("utf-8")
    if message.rstrip("\n") != req.values["message"].rstrip("\n"):
        return observation
    fmt = policy._sig_format(body)
    if (fmt is not None) != exp.sign:
        return observation
    op._message_problems(plan, message, author_email or "", exp.email or "", signed=fmt is not None,
                         signing_format=fmt)
    if exp.sign:
        op._verify_signature(plan, head, fmt, exp.email or "")
    observation["desired_state_observed"] = not plan.problems
    return observation


def _pr(plan: op.Plan) -> dict:
    req = plan.request.values
    op._account(plan)
    op._authenticate(plan)
    owner = str(plan.project).split("/")[0]
    candidates = []
    for page in range(1, 11):
        rows = op._api(plan, "repos/" + str(plan.project) + "/pulls",
                       params={"state": "all", "head": owner + ":" + req["head"], "base": req["base"],
                               "per_page": "100", "page": str(page)})
        if not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
            raise OperationError("invalid_response", "GitHub returned an invalid PR listing.")
        for row in rows:
            head, base = row.get("head"), row.get("base")
            if not isinstance(head, dict) or not isinstance(base, dict):
                raise OperationError("invalid_response", "A PR is missing branch metadata.")
            if any(not isinstance(side.get("repo"), dict) for side in (head, base)):
                raise OperationError("invalid_response", "A PR is missing repository metadata.")
            matches = (head.get("ref") == req["head"] and base.get("ref") == req["base"] and
                       head["repo"].get("full_name") == plan.project and base["repo"].get("full_name") == plan.project and
                       head.get("sha") == plan.snapshot["remote_refs"][req["head"]] and
                       base.get("sha") == plan.snapshot["remote_refs"][req["base"]])
            if not matches:
                continue
            url = op._pr_url(plan, row)
            if url == plan.snapshot.get("existing_pr") or (
                    row.get("title") == req["title"] and (row.get("body") or "") == req["body"] and
                    row.get("draft") is req["draft"]):
                candidates.append(url)
        if len(rows) < 100:
            break
    else:
        raise OperationError("reconciliation_limit", "More than 1000 PRs require separate inspection.")
    if len(candidates) != 1:
        return {"desired_state_observed": False}
    return {"pr_url": candidates[0], "desired_state_observed": True}


def reconcile(key: str, *, online: bool = False) -> dict:
    req: Optional[Request] = None
    try:
        request_key(key)
        with contextlib.closing(Ledger(readonly=True)) as ledger:
            saved = ledger.inspect(key)
        intent = saved["intent"]
        if intent is None:
            raise OperationError("intent_unavailable", "This older record has no durable intent; inspect its effects "
                                 "manually. Do not repeat the write.", "needs_input")
        req = Request.parse(intent["request"])
        if saved["fingerprint"] != digest({"request": req.fingerprint, "plan": intent["plan_token"]}):
            raise OperationError("journal_corrupt", "Recorded request and plan do not match the operation key.")
        if req.operation != "commit" and not online:
            raise OperationError("network_approval", "Remote observation requires explicit --online.", "needs_approval")
        with configuration_lock(), repository_lock(Path(intent["common_dir"])):
            plan = _context(intent)
            if req.operation == "commit":
                observation = _commit(plan)
            elif req.operation == "push":
                op._transport(plan, True)
                current = op._remote_head(plan)
                observation = {"remote_oid": current, "desired_state_observed": current == plan.snapshot["head"]}
            else:
                observation = _pr(plan)
            # Account selection is derived from the unchanged config/destination, never caller-supplied credentials.
            if req.operation != "commit" and (
                    (dataclasses.asdict(plan.account) if plan.account else None) != intent["account"] or
                    plan.project != intent["project"]):
                raise OperationError("reconciliation_context_changed", "Forge account/project changed.")
        observed = observation["desired_state_observed"]
        return {"version": 1, "command": "operation-reconcile", "ok": observed,
                "decision": "ready" if observed else "indeterminate",
                "operation": req.operation, "repo": str(req.repo), "idempotency_key": key,
                "status": "observed" if observed else "indeterminate", "ledger_status": saved["state"],
                "observation": observation, "retry_safe": False, "attribution": "not_proven",
                "problems": [] if observed else [OperationError("outcome_unconfirmed",
                    "The reviewed postcondition is not uniquely observable. Inspect effects; absence is not retry permission.").problem()]}
    except OperationError as exc:
        result = failure("operation-reconcile", exc, req)
    except (OSError, ValueError, KeyError, TypeError, config.ConfigError, sqlite3.Error, KeyboardInterrupt):
        result = failure("operation-reconcile", OperationError("reconciliation_failed", "Observation did not complete; "
                       "the operation remains unconfirmed. No write was retried."), req)
    return dict(result, status="indeterminate", retry_safe=False, attribution="not_proven")
