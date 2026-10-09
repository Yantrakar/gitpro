"""Postcondition observation never repeats writes or promotes uncertainty into attribution."""
from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
from typing import Callable

import pytest

from conftest import git, run
from gitpro import mcp, operation_reconcile as reconcile, operations
from gitpro.operation_contract import digest
from gitpro.operation_io import Ledger, repository_lock, state_dir
from test_operations import fake_pr, op_request, ready


def failed_finish(self: Ledger, key: str, result: dict) -> None:
    raise OSError("lost durable response")


def test_commit_observation_retains_unknown_ledger(op_request: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    before = ready(op_request)
    monkeypatch.setattr(Ledger, "finish", failed_finish)
    result = operations.execute(op_request, before["plan_token"], "lost-commit", authorized=True)
    assert result["status"] == "indeterminate", result
    ledger_bytes = (state_dir() / "ledger.sqlite3").read_bytes()
    observed = reconcile.reconcile("lost-commit")
    assert observed["status"] == "observed", observed
    assert observed["ledger_status"] == "running" and observed["retry_safe"] is False
    assert observed["attribution"] == "not_proven"
    assert (state_dir() / "ledger.sqlite3").read_bytes() == ledger_bytes
    assert operations.status("lost-commit")["problems"][0]["code"] == "outcome_unknown"
    assert git(Path(op_request["repo"]), "rev-list", "--count", "HEAD") == "2"


def test_unexecuted_intent_does_not_authorize_retry(op_request: dict) -> None:
    plan = operations.build(operations.Request.parse(op_request))
    with contextlib.closing(Ledger()) as ledger:
        ledger.claim("unexecuted", digest({"request": plan.request.fingerprint, "plan": plan.token}),
                     intent=reconcile.capture(plan))
    result = reconcile.reconcile("unexecuted")
    assert result["decision"] == "indeterminate" and result["retry_safe"] is False
    assert not result["observation"]["desired_state_observed"]


def test_changed_config_and_active_operation_stop_observation(op_request: dict,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    plan = ready(op_request)
    monkeypatch.setattr(Ledger, "finish", failed_finish)
    operations.execute(op_request, plan["plan_token"], "changed-context", authorized=True)
    repo = Path(op_request["repo"])
    with repository_lock(repo / ".git"):
        result = reconcile.reconcile("changed-context")
        assert result["problems"][0]["code"] == "operation_busy"
    git(repo, "config", "user.email", "another@account.test")
    result = reconcile.reconcile("changed-context")
    assert result["problems"][0]["code"] == "reconciliation_context_changed", result


def test_push_observation_reads_exact_original_destination(op_request: dict, bare_remote: Callable,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Path(op_request["repo"])
    target, other = bare_remote("target.git"), bare_remote("fetch-only.git")
    git(repo, "-c", "user.name=Default Me", "-c", "user.email=me@default.test", "commit", "--amend",
        "--author=Default Me <me@default.test>", "--no-edit")
    git(repo, "remote", "set-url", "origin", str(other))
    git(repo, "remote", "set-url", "--push", "origin", str(target))
    req = {"version": 1, "operation": "push", "repo": str(repo), "remote": "origin", "branch": "reviewed"}
    plan = ready(req, online=True)
    monkeypatch.setattr(Ledger, "finish", failed_finish)
    operations.execute(req, plan["plan_token"], "lost-push", authorized=True, online=True)
    assert reconcile.reconcile("lost-push")["decision"] == "needs_approval"
    result = reconcile.reconcile("lost-push", online=True)
    assert result["status"] == "observed", result
    assert result["observation"]["remote_oid"] == git(target, "rev-parse", "refs/heads/reviewed")
    assert not git(other, "show-ref")
    git(target, "update-ref", "-d", "refs/heads/reviewed")
    absent = reconcile.reconcile("lost-push", online=True)
    assert absent["decision"] == "indeterminate" and absent["retry_safe"] is False


def pr_data(req: dict, intent: dict) -> dict:
    project = intent["project"]
    return {"html_url": f"https://github.com/{project}/pull/42", "title": req["title"], "body": req["body"],
            "draft": req["draft"], "state": "closed",
            **{side: {"ref": req[side], "sha": intent["snapshot"]["remote_refs"][req[side]],
                      "repo": {"full_name": project}} for side in ("head", "base")}}


@pytest.mark.parametrize("response", ["unique", "none", "multiple", "edited", "paged", "limit"])
def test_pr_observation_includes_closed_prs_and_never_posts(op_request: dict, fake_bin: object, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch, response: str) -> None:
    req, marker = fake_pr(op_request, fake_bin, tmp_path, response="uncertain")
    preflight = ready(req, online=True)
    result = operations.execute(req, preflight["plan_token"], "uncertain-pr", authorized=True, online=True)
    assert result["status"] == "indeterminate" and marker.read_text() == "1"
    with contextlib.closing(Ledger(readonly=True)) as ledger:
        intent = ledger.inspect("uncertain-pr")["intent"]
    row = pr_data(req, intent)
    pages = []

    def api(plan: operations.Plan, endpoint: str, *, params: dict) -> list:
        assert endpoint == "repos/corp-org/app/pulls" and params["state"] == "all"
        pages.append(params["page"])
        if response == "none":
            return []
        if response == "multiple":
            return [row, dict(row, html_url="https://github.com/corp-org/app/pull/43")]
        if response == "edited":
            return [dict(row, title="subsequently edited")]
        if response == "limit" or (response == "paged" and params["page"] == "1"):
            unrelated = copy.deepcopy(row)
            unrelated["head"]["sha"] = "f" * 40
            return [unrelated] * 100
        return [row]

    monkeypatch.setattr(operations, "_api", api)
    observed = reconcile.reconcile("uncertain-pr", online=True)
    if response in ("unique", "paged"):
        assert observed["status"] == "observed", observed
        assert observed["observation"]["pr_url"].endswith("/pull/42")
        assert observed["retry_safe"] is False
    else:
        assert observed["decision"] == "indeterminate", observed
    if response == "paged":
        assert pages == ["1", "2"]
    if response == "limit":
        assert len(pages) == 10 and observed["problems"][0]["code"] == "reconciliation_limit"
    assert marker.read_text() == "1"
    assert len([call for call in fake_bin.calls("gh") if "POST" in call]) == 1


def test_legacy_record_and_corrupt_intent_are_explicit(op_request: dict) -> None:
    with contextlib.closing(Ledger()) as ledger:
        ledger.claim("legacy", "old")
    assert reconcile.reconcile("legacy")["problems"][0]["code"] == "intent_unavailable"
    plan = ready(op_request)
    assert operations.execute(op_request, plan["plan_token"], "corrupt", authorized=True)["ok"]
    with contextlib.closing(Ledger()) as ledger, ledger.db:
        ledger.db.execute("UPDATE operation_intents SET intent='{}' WHERE key='corrupt'")
    assert reconcile.reconcile("corrupt")["problems"][0]["code"] == "journal_corrupt"


def test_reconciliation_cli_mcp_parity(op_request: dict, capsys: pytest.CaptureFixture) -> None:
    plan = ready(op_request)
    assert operations.execute(op_request, plan["plan_token"], "parity", authorized=True)["ok"]
    expected = reconcile.reconcile("parity")
    capsys.readouterr()
    assert run("operation-reconcile", "--idempotency-key", "parity") == 0
    assert json.loads(capsys.readouterr().out) == expected
    inp = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "reconcile_operation", "arguments": {"idempotency_key": "parity"}}}) + "\n")
    out = io.StringIO()
    assert mcp.serve(inp, out) == 0
    assert json.loads(out.getvalue())["result"]["structuredContent"] == expected
    assert run("operation-reconcile", "--schema") == 0
    assert json.loads(capsys.readouterr().out)["result"] == reconcile.SCHEMA


@pytest.mark.parametrize("mode", ["invalid_key", "unknown_key", "read_error"])
def test_reconciliation_errors_never_signal_retry_permission(home: Path, monkeypatch: pytest.MonkeyPatch,
                                                           mode: str) -> None:
    if mode == "read_error":
        def fail(*args: object, **kwargs: object) -> None:
            raise OSError("unreadable journal")
        monkeypatch.setattr(reconcile, "Ledger", fail)
    result = reconcile.reconcile("invalid key" if mode == "invalid_key" else "unknown")
    assert result["ok"] is False and result["status"] == "indeterminate"
    assert result["retry_safe"] is False and result["attribution"] == "not_proven"
    assert set(reconcile.SCHEMA["required"]) <= result.keys()
