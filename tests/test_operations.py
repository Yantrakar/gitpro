"""Agent operation contracts, isolated execution, atomic refs and retry behavior; offline fixtures only."""
from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import pytest

from conftest import git, repo_with_remote, run
from gitpro import mcp, operation_io, operations
from gitpro.operation_contract import OperationError, Request, decode, digest
from gitpro.operation_io import (Ledger, Process, configuration_lock, locked_index, process,
                                 repository_lock, state_dir)


@pytest.fixture
def op_request(cfg: Callable, tmp_path: Path) -> dict:
    cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git", commits=1)
    (repo / "change.txt").write_text("a staged change\n")
    git(repo, "add", "change.txt")
    return {"version": 1, "operation": "commit", "repo": str(repo), "message": "A reviewed commit"}


def ready(op_request: dict, **kwargs: object) -> dict:
    result = operations.preflight(op_request, **kwargs)
    assert result["decision"] == "ready", result
    return result


def execute(op_request: dict, key: str = "test-op", **kwargs: object) -> dict:
    plan = ready(op_request, **kwargs)
    return operations.execute(op_request, plan["plan_token"], key, authorized=True, **kwargs)


def test_commit_is_scoped_and_idempotent(op_request: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Path(op_request["repo"])
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "wrong@ambient.test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "wrong@ambient.test")
    monkeypatch.setenv("GH_TOKEN", "sensitive-ambient-token")
    before_env = dict(os.environ)
    before_config = (repo / ".git/config").read_bytes()
    before_index = (repo / ".git/index").read_bytes()
    plan = ready(op_request)
    assert "sensitive-ambient-token" not in json.dumps(plan)
    result = operations.execute(op_request, plan["plan_token"], "commit-once", authorized=True)
    assert result["status"] == "succeeded", result
    assert git(repo, "show", "-s", "--format=%an <%ae> / %cn <%ce>") == \
        "Work Me <me@corp.test> / Work Me <me@corp.test>"
    assert (repo / ".git/index").read_bytes() == before_index
    assert (repo / ".git/config").read_bytes() == before_config
    assert dict(os.environ) == before_env
    replay = operations.execute(op_request, plan["plan_token"], "commit-once", authorized=True)
    assert replay["status"] == "succeeded" and replay["replayed"]
    assert replay["outcome"] == result["outcome"]
    assert git(repo, "rev-list", "--count", "HEAD") == "2"
    assert operations.status("commit-once")["outcome"] == result["outcome"]


def test_execution_excluded_by_configuration_change_but_finished_requests_replay(op_request: dict) -> None:
    plan = ready(op_request)
    with configuration_lock(exclusive=True):
        result = operations.execute(op_request, plan["plan_token"], "configuration-lock", authorized=True)
        assert result["problems"][0]["code"] == "operation_busy"
    result = operations.execute(op_request, plan["plan_token"], "configuration-lock", authorized=True)
    assert result["status"] == "succeeded", result
    with configuration_lock(exclusive=True):
        replay = operations.execute(op_request, plan["plan_token"], "configuration-lock", authorized=True)
        assert replay["replayed"] and replay["outcome"] == result["outcome"]


def test_explicit_colleague_author_is_preserved(op_request: dict) -> None:
    op_request.update(author_name="A Colleague", author_email="colleague@elsewhere.test")
    result = execute(op_request)
    assert result["ok"], result
    assert git(Path(op_request["repo"]), "show", "-s", "--format=%ae %ce") == "colleague@elsewhere.test me@corp.test"


def test_own_other_author_is_blocked(op_request: dict) -> None:
    op_request.update(author_name="Personal Me", author_email="me@personal.test")
    result = operations.preflight(op_request)
    assert result["decision"] == "blocked"
    assert result["plan_token"] is None


def test_persistent_override_needs_approval_not_silent_repair(op_request: dict) -> None:
    repo = Path(op_request["repo"])
    git(repo, "config", "user.email", "wrong@other.test")
    result = operations.preflight(op_request)
    assert result["decision"] == "needs_approval"
    assert git(repo, "config", "user.email") == "wrong@other.test"


@pytest.mark.parametrize("change", ["index", "head", "config", "policy"])
def test_stale_plan_never_executes(op_request: dict, change: str) -> None:
    repo = Path(op_request["repo"])
    plan = ready(op_request)
    if change == "index":
        (repo / "another").write_text("changed")
        git(repo, "add", "another")
    elif change == "head":
        git(repo, "-c", "user.name=Work Me", "-c", "user.email=me@corp.test",
            "commit", "--allow-empty", "-m", "someone else committed")
    elif change == "config":
        git(repo, "config", "core.abbrev", "10")
    else:
        (repo / ".gitpro-policy.toml").write_text('[policy]\nname = "new"\nversion = 1\n')
    before = git(repo, "rev-parse", "HEAD")
    result = operations.execute(op_request, plan["plan_token"], "stale", authorized=True)
    assert not result["ok"]
    assert git(repo, "rev-parse", "HEAD") == before


def test_idempotency_key_cannot_change_request(op_request: dict) -> None:
    plan = ready(op_request)
    assert operations.execute(op_request, plan["plan_token"], "fixed", authorized=True)["ok"]
    changed = dict(op_request, message="Different request")
    result = operations.execute(changed, plan["plan_token"], "fixed", authorized=True)
    assert result["problems"][0]["code"] == "idempotency_conflict"


def test_running_record_never_repeats_write(op_request: dict) -> None:
    plan = ready(op_request)
    fingerprint = digest({"request": Request.parse(op_request).fingerprint, "plan": plan["plan_token"]})
    ledger = Ledger()
    try:
        ledger.claim("interrupted", fingerprint)
    finally:
        ledger.close()
    result = operations.execute(op_request, plan["plan_token"], "interrupted", authorized=True)
    assert not result["ok"] and result["problems"][0]["code"] == "outcome_unknown"
    assert git(Path(op_request["repo"]), "rev-list", "--count", "HEAD") == "1"


def test_execution_requires_authorized_invocation(op_request: dict) -> None:
    plan = ready(op_request)
    result = operations.execute(op_request, plan["plan_token"], "no-consent")
    assert result["decision"] == "needs_approval"
    assert not state_dir().exists()


def test_native_index_and_repository_locks(op_request: dict) -> None:
    repo = Path(op_request["repo"])
    plan = ready(op_request)
    with repository_lock(repo / ".git"):
        result = operations.execute(op_request, plan["plan_token"], "busy", authorized=True)
        assert result["problems"][0]["code"] == "operation_busy"
    with locked_index(repo / ".git/index", operations.file_hash(repo / ".git/index")):
        result = operations.execute(op_request, plan["plan_token"], "index-busy", authorized=True)
        assert result["problems"][0]["code"] == "index_busy"
    assert not (repo / ".git/index.lock").exists()
    assert git(repo, "rev-list", "--count", "HEAD") == "1"


def test_existing_hooks_run_and_failure_is_not_bypassed(op_request: dict) -> None:
    repo = Path(op_request["repo"])
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o700)
    result = execute(op_request)
    assert not result["ok"], result
    assert result["status"] == "failed"
    assert git(repo, "rev-list", "--count", "HEAD") == "1"
    assert not (repo / ".git/index.lock").exists()


def test_hook_cannot_change_staged_content_silently(op_request: dict) -> None:
    repo = Path(op_request["repo"])
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'hook change' > change.txt\ngit add change.txt\n")
    hook.chmod(0o700)
    result = execute(op_request)
    assert result["status"] == "indeterminate", result
    assert git(repo, "rev-list", "--count", "HEAD") == "2"


def test_local_push_uses_actual_pushurl_and_cas(op_request: dict, bare_remote: Callable) -> None:
    repo = Path(op_request["repo"])
    target = bare_remote("destination.git")
    fetch_only = bare_remote("different.git")
    # Default owns local paths; keep the outgoing commit consistent with that identity.
    git(repo, "-c", "user.name=Default Me", "-c", "user.email=me@default.test", "commit", "--amend",
        "--author=Default Me <me@default.test>", "--no-edit")
    git(repo, "remote", "set-url", "origin", str(fetch_only))
    git(repo, "remote", "set-url", "--push", "origin", str(target))
    push = {"version": 1, "operation": "push", "repo": str(repo), "remote": "origin", "branch": "reviewed"}
    assert operations.preflight(push)["decision"] == "needs_approval"
    plan = ready(push, online=True)
    result = operations.execute(push, plan["plan_token"], "push-once", online=True, authorized=True)
    assert result["ok"], result
    assert git(target, "rev-parse", "refs/heads/reviewed") == git(repo, "rev-parse", "HEAD")
    assert not git(fetch_only, "show-ref")
    replay = operations.execute(push, plan["plan_token"], "push-once", online=True, authorized=True)
    assert replay["replayed"]


def test_push_rejects_wrong_outgoing_identity(op_request: dict, bare_remote: Callable) -> None:
    repo = Path(op_request["repo"])
    target = bare_remote("default.git")
    git(repo, "remote", "set-url", "--push", "origin", str(target))
    push = {"version": 1, "operation": "push", "repo": str(repo), "remote": "origin", "branch": "main"}
    result = operations.preflight(push, online=True)
    assert result["decision"] == "blocked"
    assert any(p["code"] == "outgoing_identity" for p in result["problems"])


@pytest.mark.parametrize("updates", [
    {"version": True}, {"version": 2}, {"operation": "shell"}, {"repo": "."}, {"message": ""},
    {"approved": True}, {"env": {"GH_TOKEN": "never"}}, {"remote": "origin"},
    {"author_email": "alone@test"}, {"author_name": "X\nY", "author_email": "x@test"},
])
def test_strict_request_contract(op_request: dict, updates: dict) -> None:
    result = operations.preflight(dict(op_request, **updates))
    assert result["decision"] == "needs_input" and not result["ok"]


def test_duplicate_json_members_rejected() -> None:
    with pytest.raises(OperationError, match="Duplicate"):
        decode('{"operation": "commit", "operation": "push"}')


def test_cli_mcp_contract_parity(op_request: dict, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(op_request))
    capsys.readouterr()
    assert run("preflight", "--request", str(path), "--json") == 0
    cli_result = json.loads(capsys.readouterr().out)
    server = mcp.Server(io.StringIO(), None)
    response = server.call({"name": "preflight", "arguments": {"request": op_request}})
    assert response["structuredContent"] == cli_result
    names = {t["name"] for t in server.dispatch("tools/list", {})["tools"]}
    assert "run_operation" not in names and "preflight" in names
    with pytest.raises(mcp.RpcError):
        server.call({"name": "run_operation", "arguments": {}})
    enabled = mcp.Server(io.StringIO(), None, allow_execution=True)
    args = {"request": op_request, "plan_token": cli_result["plan_token"], "idempotency_key": "mcp-once"}
    result = enabled.call({"name": "run_operation", "arguments": args})["structuredContent"]
    assert result["ok"], result


def test_two_agents_use_different_identities_without_global_switch(op_request: dict, tmp_path: Path) -> None:
    personal = repo_with_remote(tmp_path, "personal", "git@github.com:me/personal.git", commits=1,
                                author=("Personal Me", "me@personal.test"))
    (personal / "new").write_text("personal")
    git(personal, "add", "new")
    other = dict(op_request, repo=str(personal))
    plans = [ready(r) for r in (op_request, other)]
    env = dict(os.environ)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(operations.execute, r, p["plan_token"], f"agent-{i}", authorized=True)
                 for i, (r, p) in enumerate(zip((op_request, other), plans))]
        results = [t.result(timeout=30) for t in tasks]
    assert all(r["ok"] for r in results), results
    assert git(Path(op_request["repo"]), "show", "-s", "--format=%ce") == "me@corp.test"
    assert git(personal, "show", "-s", "--format=%ce") == "me@personal.test"
    assert dict(os.environ) == env


def test_bounded_process_timeout_and_output(tmp_path: Path) -> None:
    env = dict(os.environ)
    timeout = process([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, env, timeout=0.1)
    assert timeout.stopped == "timeout"
    overflow = process([sys.executable, "-c", "print('x' * 2000000)"], tmp_path, env)
    assert overflow.stopped == "output_limit" and len(overflow.out) <= 1024 * 1024


def test_indeterminate_check_is_not_ready(op_request: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    original = operations.process

    def broken(argv: list, *args: object, **kwargs: object) -> Process:
        if "interpret-trailers" in argv:
            return Process(1, "", "sensitive output must not be returned")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(operations, "process", broken)
    result = operations.preflight(op_request)
    assert result["decision"] == "indeterminate"
    assert "sensitive output" not in json.dumps(result)


def fake_pr(op_request: dict, fake_bin: object, tmp_path: Path, *, response: str = "ok") -> tuple:
    repo = Path(op_request["repo"])
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "-m", "topic")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "proposed"], check=True)
    head = git(repo, "rev-parse", "HEAD")
    marker = tmp_path / "created-pr"
    fake_bin.add("gh", f"""
import json
from pathlib import Path
marker = Path({str(marker)!r})
if argv[:2] == ["auth", "token"]:
    assert env.get("GH_TOKEN") is None
    assert env.get("GITLAB_TOKEN") is None
    print("ghp_this_is_a_fake_account_specific_token")
elif argv[:2] == ["api", "user"]:
    assert env.get("GH_TOKEN") == "ghp_this_is_a_fake_account_specific_token"
    print("me-work")
elif argv[:4] == ["api", "--method", "GET", "user/emails"]:
    print('[{{"email":"me@corp.test","verified":true}}]')
elif "GET" in argv and "/git/ref/heads/" in argv[3]:
    print(json.dumps(dict(object=dict(sha={base!r} if argv[3].endswith("/main") else {head!r}))))
elif "GET" in argv and argv[3].endswith("/pulls"):
    print(json.dumps([{{"html_url": "https://github.com/corp-org/app/pull/42"}}] if marker.exists() else []))
elif "POST" in argv:
    payload = json.loads(stdin)
    assert payload["head"] == "topic" and payload["base"] == "main"
    marker.write_text(str(int(marker.read_text()) + 1) if marker.exists() else "1")
    if {response!r} == "uncertain":
        sys.exit(1)
    print('{{"html_url":"https://github.com/corp-org/app/pull/42"}}')
else:
    sys.exit(2)
""")
    req = {"version": 1, "operation": "pr-create", "repo": str(repo), "remote": "origin",
           "base": "main", "head": "topic", "title": "A reviewed PR", "body": "Description", "draft": True}
    return req, marker


def test_pr_creation_uses_selected_account_and_replays(op_request: dict, fake_bin: object, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    req, marker = fake_pr(op_request, fake_bin, tmp_path)
    monkeypatch.setenv("GH_TOKEN", "wrong-ambient-account")
    monkeypatch.setenv("GITLAB_TOKEN", "unrelated-secret")
    before = dict(os.environ)
    plan = ready(req, online=True)
    result = operations.execute(req, plan["plan_token"], "pr-once", online=True, authorized=True)
    assert result["ok"], result
    assert result["outcome"]["pr_url"].endswith("/pull/42")
    assert marker.read_text() == "1"
    replay = operations.execute(req, plan["plan_token"], "pr-once", online=True, authorized=True)
    assert replay["replayed"] and marker.read_text() == "1"
    assert dict(os.environ) == before
    assert "ghp_" not in json.dumps(result)
    assert b"ghp_" not in (state_dir() / "ledger.sqlite3").read_bytes()
    assert not any("switch" in call for call in fake_bin.calls("gh"))


def test_existing_pr_is_not_created_again(op_request: dict, fake_bin: object, tmp_path: Path) -> None:
    req, marker = fake_pr(op_request, fake_bin, tmp_path)
    marker.write_text("1")
    result = execute(req, "existing-pr", online=True)
    assert result["ok"] and result["outcome"]["already_present"]
    assert marker.read_text() == "1"
    assert not any("POST" in call for call in fake_bin.calls("gh"))


def test_uncertain_remote_write_is_durable_and_not_retried(op_request: dict, fake_bin: object, tmp_path: Path) -> None:
    req, marker = fake_pr(op_request, fake_bin, tmp_path, response="uncertain")
    plan = ready(req, online=True)
    result = operations.execute(req, plan["plan_token"], "uncertain-pr", online=True, authorized=True)
    assert result["status"] == "indeterminate", result
    replay = operations.execute(req, plan["plan_token"], "uncertain-pr", online=True, authorized=True)
    assert replay["status"] == "indeterminate" and replay["replayed"]
    assert marker.read_text() == "1"


def test_account_verification_failure_blocks_before_post(op_request: dict, fake_bin: object, tmp_path: Path) -> None:
    req, _ = fake_pr(op_request, fake_bin, tmp_path)
    fake_bin.add("gh", """
if argv[:2] == ["auth", "token"]: print("fake-token")
elif argv[:2] == ["api", "user"]: print("someone-else")
else: sys.exit(1)
""")
    result = operations.preflight(req, online=True)
    assert result["decision"] == "blocked", result
    assert result["problems"][0]["code"] == "account_mismatch"
    assert not any("POST" in call for call in fake_bin.calls("gh"))


def test_same_key_concurrency_performs_one_commit(op_request: dict) -> None:
    plan = ready(op_request)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(operations.execute, op_request, plan["plan_token"], "concurrent", authorized=True)
                   for _ in range(2)]
        results = [f.result(timeout=30) for f in futures]
    assert any(r["ok"] for r in results), results
    assert git(Path(op_request["repo"]), "rev-list", "--count", "HEAD") == "2"
    assert operations.status("concurrent")["ok"]


def test_linked_worktrees_share_lock_but_have_independent_indexes(op_request: dict, tmp_path: Path) -> None:
    repo = Path(op_request["repo"])
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "second", str(linked)], check=True)
    (linked / "linked.txt").write_text("only the linked tree")
    git(linked, "add", "linked.txt")
    other = dict(op_request, repo=str(linked))
    plan = ready(other)
    with repository_lock(repo / ".git"):
        blocked = operations.execute(other, plan["plan_token"], "linked-busy", authorized=True)
        assert blocked["problems"][0]["code"] == "operation_busy"
    result = operations.execute(other, plan["plan_token"], "linked-ok", authorized=True)
    assert result["ok"], result
    assert git(linked, "show", "--format=", "--name-only", "HEAD") == "linked.txt"
    assert git(repo, "diff", "--cached", "--name-only") == "change.txt"


def test_two_agents_in_linked_worktrees_keep_different_identities(op_request: dict, tmp_path: Path) -> None:
    repo = Path(op_request["repo"])
    linked = tmp_path / "personal-worktree"
    git(repo, "config", "extensions.worktreeConfig", "true")
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "personal", str(linked)], check=True)
    git(linked, "config", "--worktree", "remote.origin.url", "git@github.com:me/personal.git")
    (linked / "personal.txt").write_text("personal work")
    git(linked, "add", "personal.txt")
    requests = [op_request, dict(op_request, repo=str(linked))]
    plans = [ready(req) for req in requests]
    assert [p["identity"]["id"] for p in plans] == ["work-github", "personal"]
    home_config = (Path.home() / ".gitconfig").read_bytes()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(operations.execute, req, plan["plan_token"], f"worktree-{n}", authorized=True)
                   for n, (req, plan) in enumerate(zip(requests, plans))]
        results = [f.result(timeout=30) for f in futures]
    for n, result in enumerate(results):
        if not result["ok"]:
            assert result["problems"][0]["code"] == "operation_busy", result
            result = operations.execute(requests[n], plans[n]["plan_token"], f"worktree-{n}", authorized=True)
        assert result["ok"], result
    assert git(repo, "show", "-s", "--format=%ce") == "me@corp.test"
    assert git(linked, "show", "-s", "--format=%ce") == "me@personal.test"
    assert (Path.home() / ".gitconfig").read_bytes() == home_config


def test_native_index_lock_blocks_competing_staging(op_request: dict) -> None:
    repo = Path(op_request["repo"])
    index = repo / ".git/index"
    before = index.read_bytes()
    with locked_index(index, operations.file_hash(index)):
        (repo / "different").write_text("not approved")
        competitor = subprocess.run(["git", "-C", str(repo), "add", "different"], capture_output=True)
        assert competitor.returncode != 0
        assert index.read_bytes() == before


def test_policy_message_and_verified_email_are_enforced(op_request: dict, fake_bin: object) -> None:
    repo = Path(op_request["repo"])
    p = repo / ".gitpro-policy.toml"
    p.write_text('[policy]\nname="test"\nversion=1\n[[rule]]\nhost="github.com"\nowners=["corp-org"]\n'
                 'co_authored_by="require"\nrequire_verified_email=true\n')
    result = operations.preflight(op_request)
    assert result["decision"] == "blocked"
    assert any(p["code"] == "policy_co_authored_by" for p in result["problems"])
    op_request["message"] += "\n\nCo-Authored-By: Colleague <colleague@corp.test>\n"
    result = operations.preflight(op_request)
    assert result["decision"] == "needs_approval"
    fake_bin.add("gh", """
if argv[:2] == ["auth", "token"]: print("fake-token")
elif argv[:2] == ["api", "user"]: print("me-work")
elif "user/emails" in argv: print('[{"email":"me@corp.test","verified":true}]')
else: sys.exit(1)
""")
    assert ready(op_request, online=True)["ok"]


def test_deleted_committed_policy_cannot_authorize_its_removal(op_request: dict) -> None:
    repo = Path(op_request["repo"])
    p = repo / ".gitpro-policy.toml"
    p.write_text('[policy]\nname="test"\nversion=1\n[[rule]]\nhost="github.com"\nowners=["corp-org"]\n'
                 'co_authored_by="forbid"\n')
    git(repo, "add", ".gitpro-policy.toml")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "policy"], check=True)
    p.unlink()
    git(repo, "add", "-u")
    result = operations.preflight(op_request)
    assert result["decision"] == "needs_approval", result
    assert any(p["code"] == "policy_changed" for p in result["problems"])


def test_signing_is_preserved_and_verified(cfg: Callable, home: Path, tmp_path: Path) -> None:
    key = home / ".ssh/signing"
    key.parent.mkdir(parents=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    cfg(f'[default]\nname="Signer"\nemail="signer@corp.test"\nssh_key="{key}"\nsign=true\n')
    repo = repo_with_remote(tmp_path, "signed", None)
    (repo / "file").write_text("signed content")
    git(repo, "add", "file")
    req = {"version": 1, "operation": "commit", "repo": str(repo), "message": "Signed"}
    result = execute(req, "signed-commit")
    assert result["ok"], result
    assert "BEGIN SSH SIGNATURE" in git(repo, "cat-file", "commit", "HEAD")
    assert git(repo, "show", "-s", "--format=%G?") == "G"


def test_journal_failure_never_returns_success(op_request: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(self: Ledger, key: str, result: dict) -> None:
        raise OSError("disk full")
    monkeypatch.setattr(Ledger, "finish", failed)
    result = execute(op_request, "journal-failure")
    assert result["status"] == "indeterminate" and result["problems"][0]["code"] == "journal_failed"
    assert git(Path(op_request["repo"]), "rev-list", "--count", "HEAD") == "2"
    assert operations.status("journal-failure")["problems"][0]["code"] == "outcome_unknown"


def test_ledger_flushes_existing_state_ancestry_before_claim(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with configuration_lock():
        pass
    flushed = []
    real = operation_io.sync_path

    def observe(path: Path, *, directory: bool = False) -> None:
        assert directory
        real(path, directory=directory)
        flushed.append(path)

    monkeypatch.setattr(operation_io, "sync_path", observe)
    with contextlib.closing(Ledger()) as ledger:
        assert flushed == [state_dir(), *state_dir().parents]
        ledger.claim("durable", "fingerprint", intent={"version": 1})


def test_ledger_directory_flush_failure_prevents_git_write(op_request: dict,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(path: Path, *, directory: bool = False) -> None:
        raise OSError("directory flush failed")

    monkeypatch.setattr(operation_io, "sync_path", fail)
    result = execute(op_request, "unflushed")
    assert result["status"] == "blocked" and result["problems"][0]["code"] == "operation_failed"
    assert git(Path(op_request["repo"]), "rev-list", "--count", "HEAD") == "1"
    with contextlib.closing(Ledger(readonly=True)) as ledger:
        assert ledger.db.execute("SELECT COUNT(*) FROM operations").fetchone() == (0,)


@pytest.mark.parametrize("value", ["relative-state", ".", "~/.local/state"])
def test_relative_state_path_blocks_before_any_operation(
        op_request: dict, home: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    repo = Path(op_request["repo"])
    plan = ready(op_request)
    head = git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("XDG_STATE_HOME", value)
    for result in (operations.preflight(op_request),
                   operations.execute(op_request, plan["plan_token"], "invalid-state", authorized=True),
                   operations.status("invalid-state")):
        assert result["decision"] == "needs_input" and result["problems"][0]["code"] == "invalid_state_path", result
    with pytest.raises(OperationError, match="absolute path"):
        with configuration_lock():
            pytest.fail("relative state path acquired a lock")
    assert git(repo, "rev-parse", "HEAD") == head
    assert not (home / value / "gitpro").exists()
    assert not (home / ".local/state/gitpro").exists()


def test_absolute_state_path_keeps_existing_journal_readable(
        op_request: dict, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = home / "custom-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(root))
    result = execute(op_request, "saved-state")
    assert result["ok"], result
    monkeypatch.setenv("XDG_STATE_HOME", "custom-state")
    assert operations.status("saved-state")["problems"][0]["code"] == "invalid_state_path"
    monkeypatch.setenv("XDG_STATE_HOME", str(root))
    assert operations.status("saved-state")["outcome"] == result["outcome"]


def test_plan_schema_is_reproducible(capsys: pytest.CaptureFixture) -> None:
    capsys.readouterr()
    assert run("preflight", "--schema") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["request"]["additionalProperties"] is False
    assert data["result"]["additionalProperties"] is False
    assert set(data["result"]["required"]) == {"version", "command", "ok", "decision", "problems"}


def test_scoped_https_refuses_ambient_header_credentials(op_request: dict) -> None:
    repo = Path(op_request["repo"])
    git(repo, "remote", "set-url", "origin", "https://github.com/corp-org/app.git")
    git(repo, "config", "http.https://github.com/.extraheader", "Authorization: secret-value")
    req = {"version": 1, "operation": "push", "repo": str(repo), "remote": "origin", "branch": "main"}
    result = operations.preflight(req)
    assert result["decision"] == "needs_approval"
    assert result["problems"][0]["code"] == "http_header_override"
    assert "secret-value" not in json.dumps(result)


def test_actual_commit_message_changes_are_reported(op_request: dict) -> None:
    repo = Path(op_request["repo"])
    hook = repo / ".git/hooks/commit-msg"
    hook.write_text('#!/bin/sh\nprintf "\\nUnreviewed addition\\n" >> "$1"\n')
    hook.chmod(0o700)
    result = execute(op_request)
    assert result["status"] == "indeterminate"
    assert git(repo, "rev-list", "--count", "HEAD") == "2"


def test_explicit_configuration_is_passed_to_hooks(op_request: dict, cfg: Callable, home: Path) -> None:
    repo = Path(op_request["repo"])
    path = home / "explicit.toml"
    path.write_bytes((home / ".config/gitpro.toml").read_bytes())
    path.chmod(0o600)
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text('#!/bin/sh\n[ "$GITPRO_CONFIG" = "' + str(path) + '" ]\n')
    hook.chmod(0o700)
    result = execute(op_request, cfg_path=path)
    assert result["ok"], result


def test_cli_run_and_status_return_same_result(op_request: dict, tmp_path: Path,
                                               capsys: pytest.CaptureFixture) -> None:
    plan = ready(op_request)
    path = tmp_path / "operation.json"
    path.write_text(json.dumps(op_request))
    capsys.readouterr()
    assert run("run", "--request", str(path), "--plan-token", plan["plan_token"],
               "--idempotency-key", "cli-once", "-y") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "succeeded"
    assert run("operation-status", "--idempotency-key", "cli-once") == 0
    status_result = json.loads(capsys.readouterr().out)
    assert status_result["outcome"] == result["outcome"]
    assert run("run", "--request", str(path), "--plan-token", plan["plan_token"],
               "--idempotency-key", "cli-once", "-y") == 0
    assert json.loads(capsys.readouterr().out)["replayed"]


def test_new_remote_state_invalidates_push_plan(op_request: dict, bare_remote: Callable) -> None:
    repo = Path(op_request["repo"])
    target = bare_remote("destination.git")
    git(repo, "-c", "user.name=Default Me", "-c", "user.email=me@default.test", "commit", "--amend",
        "--author=Default Me <me@default.test>", "--no-edit")
    git(repo, "remote", "set-url", "origin", str(target))
    req = {"version": 1, "operation": "push", "repo": str(repo), "remote": "origin", "branch": "main"}
    plan = ready(req, online=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "HEAD:refs/heads/main"], check=True)
    result = operations.execute(req, plan["plan_token"], "stale-push", online=True, authorized=True)
    assert result["problems"][0]["code"] == "stale_plan"


def test_trust_file_principals_are_checked(cfg: Callable, home: Path, tmp_path: Path) -> None:
    key = home / ".ssh/signing"
    key.parent.mkdir(parents=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    cfg(f'[default]\nname="Signer"\nemail="signer@corp.test"\nssh_key="{key}"\nsign=true\n')
    repo = repo_with_remote(tmp_path, "signed", "git@github.com:corp-org/app.git")
    trust = repo / "allowed_signers"
    trust.write_text("someone-else@corp.test " + key.with_suffix(".pub").read_text())
    pol = repo / ".gitpro-policy.toml"
    pol.write_text('[policy]\nname="test"\nversion=1\nallowed_signers="allowed_signers"\n'
                   '[[rule]]\nhost="github.com"\nowners=["corp-org"]\nrequire_signing=true\n')
    (repo / "file").write_text("signed")
    git(repo, "add", "file")
    req = {"version": 1, "operation": "commit", "repo": str(repo), "message": "Signed"}
    result = operations.preflight(req)
    assert result["decision"] == "blocked"
    assert any(p["code"] == "policy_allowed_signers" for p in result["problems"])
    assert not git(repo, "rev-parse", "--verify", "--quiet", "HEAD")


def test_partial_clone_history_requires_fetch_without_network(op_request: dict) -> None:
    # An unavailable base is a missing input, never an implicit fetch.
    req = Request.parse(op_request)
    plan = operations.build(req)
    with pytest.raises(OperationError) as caught:
        operations._history(plan, "f" * 40, plan.snapshot["head"])
    assert caught.value.code == "missing_history"


def test_schema_and_runtime_limits_match(op_request: dict) -> None:
    too_large = dict(op_request, message="x" * 65537)
    assert operations.preflight(too_large)["problems"][0]["code"] == "invalid_request"
    assert operations.preflight(dict(op_request, message="\u65e5\u672c\u8a9e" * 21845))["problems"][0]["code"] == "invalid_request"


@pytest.mark.parametrize("login", ["me-work", "wrong-account"])
def test_ssh_login_is_checked_with_the_selected_key(op_request: dict, fake_bin: object, login: str,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin.add("ssh", f'print("Hi {login}! You\\\'ve successfully authenticated, but GitHub does not provide shell access.")\nsys.exit(1)')
    original = operations.process

    def remote(argv: list, *args: object, **kwargs: object) -> Process:
        if argv[:3] == ["git", "ls-remote", "--exit-code"]:
            return Process(2, "", "")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(operations, "process", remote)
    req = {"version": 1, "operation": "push", "repo": op_request["repo"], "remote": "origin", "branch": "main"}
    result = operations.preflight(req, online=True)
    if login == "me-work":
        assert result["ok"], result
    else:
        assert result["decision"] == "blocked" and result["problems"][0]["code"] == "ssh_account_mismatch"
    call = fake_bin.calls("ssh")[0]
    assert "-i" in call and "IdentitiesOnly=yes" in call and "UpdateHostKeys=no" in call
    assert call[call.index("-F") + 1] == "/dev/null"


def test_https_push_scopes_credentials_without_switching(op_request: dict, fake_bin: object,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Path(op_request["repo"])
    git(repo, "remote", "set-url", "origin", "https://github.com/corp-org/app.git")
    fake_bin.add("gh", """
if argv[:2] == ["auth", "token"]:
    assert not env.get("GH_TOKEN") and not env.get("GITHUB_TOKEN")
    print("chosen-account-token")
elif argv[:2] == ["api", "user"]:
    assert env["GH_TOKEN"] == "chosen-account-token"
    print("me-work")
else: sys.exit(1)
""")
    monkeypatch.setenv("GH_TOKEN", "wrong-token")
    monkeypatch.setenv("GITHUB_TOKEN", "other-wrong-token")
    original = operations.process
    published = []

    def remote(argv: list, path: Path, env: dict, **kwargs: object) -> Process:
        if argv[:3] == ["git", "ls-remote", "--exit-code"] or "push" in argv:
            assert env["GH_TOKEN"] == "chosen-account-token"
            assert "GITHUB_TOKEN" not in env
            assert env["GIT_CONFIG_VALUE_0"] == "" and env["GIT_CONFIG_VALUE_1"] == "!gh auth git-credential"
            if "push" in argv:
                assert "--atomic" in argv and any(s.startswith("--force-with-lease=") for s in argv)
                published.append(git(repo, "rev-parse", "HEAD"))
                return Process(0, "", "")
            return Process(0, published[0] + "\trefs/heads/main\n", "") if published else Process(2, "", "")
        return original(argv, path, env, **kwargs)

    monkeypatch.setattr(operations, "process", remote)
    req = {"version": 1, "operation": "push", "repo": str(repo), "remote": "origin", "branch": "main"}
    result = execute(req, "https-push", online=True)
    assert result["ok"] and len(published) == 1
    assert os.environ["GH_TOKEN"] == "wrong-token"
    assert not any("switch" in call for call in fake_bin.calls("gh"))


def test_sigterm_cancels_child_and_records_uncertainty(op_request: dict, tmp_path: Path) -> None:
    repo = Path(op_request["repo"])
    started = repo / "hook-started"
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text('#!/bin/sh\nprintf started > hook-started\nsleep 30\n')
    hook.chmod(0o700)
    plan = ready(op_request)
    path = tmp_path / "operation.json"
    path.write_text(json.dumps(op_request))
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    child = subprocess.Popen([sys.executable, "-m", "gitpro", "run", "--request", str(path),
                              "--plan-token", plan["plan_token"], "--idempotency-key", "cancelled", "-y"],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not started.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists()
        child.terminate()
        stdout, stderr = child.communicate(timeout=10)
        result = json.loads(stdout)
        assert result["status"] == "indeterminate", (result, stderr)
        assert not (repo / ".git/index.lock").exists()
        assert operations.status("cancelled")["status"] == "indeterminate"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
