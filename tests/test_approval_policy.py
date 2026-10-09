"""Operator grants are narrow, revocable and separate from client requests."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
from typing import Callable, Iterator, Optional

import pytest

from conftest import DEFAULT_CONFIG, git, repo_with_remote, run
from gitpro import approval_policy, changes, mcp
from gitpro.operation_io import Ledger, state_dir
from test_changes import approve, request


@pytest.fixture(autouse=True)
def private_files() -> Iterator[None]:
    mode = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(mode)


def write_policy(path: Path, proposal: dict, **updates: object) -> Path:
    rule = {"id": "runner", "kinds": [proposal["request"]["kind"]],
            "config_paths": [proposal["request"]["config_path"]], "repo_roots": [],
            "write_paths": [c["path"] for c in proposal["changes"]], "write_roots": []}
    rule.update(updates)
    path.write_text(json.dumps({"version": 1, "rules": [rule]}))
    path.chmod(0o600)
    return path


def test_scoped_grant_is_unattended_exact_and_audited(home: Path, tmp_path: Path) -> None:
    raw = request()
    proposal = changes.propose(raw)
    path = write_policy(tmp_path / "operator.json", proposal)
    inspected = changes.propose(raw, policy_path=path)
    assert inspected["decision"] == "ready" and inspected["authority"]["kind"] == "policy", inspected
    assert not state_dir().exists()
    result = changes.apply(proposal["plan_id"], raw=raw, policy_path=path, authorized=True)
    assert result["status"] == "applied", result
    assert result["authority"]["rule_id"] == "runner"
    assert result["authority"]["policy_sha256"] == approval_policy.load(path).sha256
    assert changes.status(proposal["plan_id"])["authority"] == result["authority"]
    replay = changes.apply(proposal["plan_id"], raw=raw, authorized=True)
    assert replay["replayed"] and replay["status"] == "applied"
    conflict = changes.apply(proposal["plan_id"], raw=request(toml=DEFAULT_CONFIG + "\n# altered\n"),
                             policy_path=path, authorized=True)
    assert conflict["problems"][0]["code"] == "idempotency_conflict"


@pytest.mark.parametrize("scope", ["kind", "config", "file", "root_prefix", "split_rules"])
def test_every_file_must_fit_one_rule(home: Path, tmp_path: Path, scope: str) -> None:
    proposal = changes.propose(request())
    path = write_policy(tmp_path / "operator.json", proposal)
    data = json.loads(path.read_text())
    rule = data["rules"][0]
    if scope == "kind":
        rule["kinds"] = ["regenerate"]
    elif scope == "config":
        rule["config_paths"] = [str(home / ".config/another.toml")]
    elif scope == "file":
        rule["write_paths"].pop()
    elif scope == "root_prefix":
        rule["write_paths"] = []
        rule["write_roots"] = [str(home.parent / "hom")]
    else:
        other = dict(rule, id="other", write_paths=rule["write_paths"][1:])
        rule["write_paths"] = rule["write_paths"][:1]
        data["rules"].append(other)
    path.write_text(json.dumps(data))
    result = changes.apply(proposal["plan_id"], raw=request(), policy_path=path, authorized=True)
    assert result["problems"][0]["code"] == "approval_scope_denied", result
    assert not (home / ".gitconfig").exists()


def test_expiry_revocation_and_explicit_reauthorization(home: Path, tmp_path: Path) -> None:
    proposal = changes.propose(request())
    path = write_policy(tmp_path / "operator.json", proposal)
    changes._authorize(proposal["plan_id"], request(), path, None)
    original = changes.status(proposal["plan_id"])["authority"]
    data = json.loads(path.read_text())
    data["expires_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(data))
    expired = changes.apply(proposal["plan_id"], policy_path=path, authorized=True)
    assert expired["problems"][0]["code"] == "approval_policy_expired"
    data.pop("expires_at")
    path.write_text(json.dumps(data))
    changed = changes.apply(proposal["plan_id"], policy_path=path, authorized=True)
    assert changed["problems"][0]["code"] == "approval_policy_changed"
    assert changes.apply(proposal["plan_id"], authorized=True)["decision"] == "needs_approval"
    result = changes.apply(proposal["plan_id"], raw=request(), policy_path=path, authorized=True)
    assert result["status"] == "applied"
    assert result["authority"]["policy_fingerprint"] != original["policy_fingerprint"]


def test_human_can_approve_outside_operator_scope(home: Path, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = changes.propose(request())
    path = write_policy(tmp_path / "operator.json", proposal, kinds=["regenerate"])
    inspected = changes.propose(request(), policy_path=path)
    assert inspected["decision"] == "needs_approval" and inspected["ok"]
    approve(monkeypatch, proposal)
    result = changes.apply(proposal["plan_id"], raw=request(), policy_path=path, authorized=True)
    assert result["status"] == "applied" and result["authority"]["kind"] == "human"


def test_policy_cannot_authorize_its_own_replacement(home: Path) -> None:
    path = home / "operator.json"
    path.write_text(json.dumps({"version": 1, "rules": [{
        "id": "broad", "kinds": ["configure"], "config_paths": [str(home / ".config/gitpro.toml")],
        "repo_roots": [], "write_paths": [], "write_roots": [str(home)]}]}))
    raw = request(toml='[default]\n[[identity]]\nid="x"\nname="X"\nemail="x@corp.test"\n'
                      'host="github.com"\nfile="~/operator.json"\n')
    result = changes.propose(raw, policy_path=path)
    assert result["ok"] and result["decision"] == "needs_approval", result
    assert "own replacement" in result["problems"][0]["message"]


def test_changes_cannot_overwrite_their_coordination_state(home: Path) -> None:
    raw = request(toml='[default]\n[[identity]]\nid="x"\nname="X"\nemail="x@corp.test"\n'
                      'host="github.com"\nfile="~/.local/state/gitpro/operations/configuration.lock"\n')
    result = changes.propose(raw)
    assert result["problems"][0]["code"] == "unsafe_target", result


@pytest.mark.parametrize("edit", ["unknown", "missing_scope", "root", "relative", "duplicate", "wrong_type",
                                 "symlink", "permissions"])
def test_policy_loading_fails_closed(home: Path, tmp_path: Path, edit: str) -> None:
    proposal = changes.propose(request())
    path = write_policy(tmp_path / "operator.json", proposal)
    data = json.loads(path.read_text())
    if edit == "unknown":
        data["approved"] = True
    elif edit == "missing_scope":
        del data["rules"][0]["config_paths"]
    elif edit == "root":
        data["rules"][0]["write_roots"] = ["/"]
    elif edit == "relative":
        data["rules"][0]["write_roots"] = ["."]
    elif edit == "duplicate":
        data["rules"] *= 2
    elif edit == "wrong_type":
        data["version"] = True
    path.write_text(json.dumps(data))
    if edit == "permissions":
        path.chmod(0o666)
    elif edit == "symlink":
        other = path.with_name("actual.json")
        path.rename(other)
        path.symlink_to(other)
    result = changes.apply(proposal["plan_id"], raw=request(), policy_path=path, authorized=True)
    assert not result["ok"] and not (home / ".gitconfig").exists()


def test_policy_grants_do_not_bypass_stale_plans_or_host_permission(home: Path, tmp_path: Path) -> None:
    proposal = changes.propose(request())
    path = write_policy(tmp_path / "operator.json", proposal)
    denied = changes.apply(proposal["plan_id"], raw=request(), policy_path=path)
    assert denied["problems"][0]["code"] == "authorization_required"
    (home / ".gitconfig").write_text("# concurrent edit\n")
    result = changes.apply(proposal["plan_id"], raw=request(), policy_path=path, authorized=True)
    assert result["problems"][0]["code"] == "stale_plan"
    assert (home / ".gitconfig").read_text() == "# concurrent edit\n"


def test_original_rollback_permission_survives_policy_revocation(home: Path, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = changes.propose(request())
    path = write_policy(tmp_path / "operator.json", proposal)
    real = changes.Store.transition

    def fail_final(self: changes.Store, key: str, state: str, result: Optional[dict] = None) -> None:
        if state != "applying":
            raise sqlite3.OperationalError("uncertain result")
        real(self, key, state, result)

    monkeypatch.setattr(changes.Store, "transition", fail_final)
    done = changes.apply(proposal["plan_id"], raw=request(), policy_path=path, authorized=True)
    assert done["decision"] == "indeterminate"
    path.unlink()
    monkeypatch.setattr(changes.Store, "transition", real)
    replay = changes.apply(proposal["plan_id"], raw=request(), policy_path=path, authorized=True)
    assert replay["problems"][0]["code"] == "outcome_unknown"
    result = changes.apply(proposal["plan_id"], recover=True, authorized=True)
    assert result["status"] == "rolled_back", result
    assert result["authority"]["kind"] == "policy" and not (home / ".gitconfig").exists()


def test_cli_policy_flow_and_client_cannot_select_policy(home: Path, tmp_path: Path,
                                                       capsys: pytest.CaptureFixture) -> None:
    proposal = changes.propose(request())
    path = write_policy(tmp_path / "operator.json", proposal)
    source = tmp_path / "proposal.json"
    source.write_text(json.dumps(proposal))
    assert run("change", "apply", "--plan-id", proposal["plan_id"], "--proposal", str(source),
               "--approval-policy", str(path), "-y") == 0
    assert json.loads(capsys.readouterr().out)["authority"]["kind"] == "policy"
    tool = mcp.TOOLS["apply_change"]
    with pytest.raises(mcp.RpcError):
        mcp.validate(tool.schema, {"plan_id": proposal["plan_id"], "approval_policy": str(path)})
    assert not any("approve" in name for name in mcp.TOOLS)


def test_mcp_uses_only_operator_startup_policy(home: Path, tmp_path: Path) -> None:
    proposal = changes.propose(request())
    path = write_policy(tmp_path / "operator.json", proposal)
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "apply_change", "arguments": {
                   "plan_id": proposal["plan_id"], "request": request()}}}
    out = io.StringIO()
    mcp.serve(io.StringIO(json.dumps(message) + "\n"), out, allow_execution=True)
    assert json.loads(out.getvalue())["result"]["structuredContent"]["decision"] == "needs_approval"
    out = io.StringIO()
    mcp.serve(io.StringIO(json.dumps(message) + "\n"), out, allow_execution=True, approval_policy=path)
    assert json.loads(out.getvalue())["result"]["structuredContent"]["status"] == "applied"


def test_lost_policy_metadata_is_not_treated_as_human_approval(home: Path, tmp_path: Path) -> None:
    proposal = changes.propose(request())
    path = write_policy(tmp_path / "operator.json", proposal)
    changes._authorize(proposal["plan_id"], request(), path, None)
    with contextlib.closing(changes.Store()) as store, store.db:
        store.db.execute("DELETE FROM change_authorizations WHERE id=?", (proposal["plan_id"],))
    result = changes.apply(proposal["plan_id"], authorized=True)
    assert result["problems"][0]["code"] == "journal_corrupt"
    assert not (home / ".gitconfig").exists()


def test_linked_worktree_and_undo_require_separate_repo_and_file_scopes(
        cfg: Callable[..., Path], tmp_path: Path) -> None:
    cfg()
    repo = repo_with_remote(tmp_path, "main", "git@github.com:corp-org/repair.git", commits=1)
    worktree = tmp_path / "agent"
    git(repo, "worktree", "add", "-b", "agent", str(worktree))
    assert (worktree / ".git").is_file()
    git(repo, "config", "user.email", "wrong@other.test")
    before = (repo / ".git/config").read_bytes()
    raw = request("repo-identity", repo=str(worktree))
    proposal = changes.propose(raw)
    assert proposal["ok"], proposal
    assert [c["path"] for c in proposal["changes"]] == [str(repo / ".git/config")]
    path = write_policy(tmp_path / "operator.json", proposal, repo_roots=[str(repo)])
    denied = changes.apply(proposal["plan_id"], raw=raw, policy_path=path, authorized=True)
    assert denied["problems"][0]["code"] == "approval_scope_denied"
    write_policy(path, proposal, repo_roots=[str(worktree)])
    applied = changes.apply(proposal["plan_id"], raw=raw, policy_path=path, authorized=True)
    assert applied["status"] == "applied", applied
    assert git(worktree, "config", "user.email") == "me@corp.test"

    undo_raw = request("undo", change_id=proposal["plan_id"])
    undo = changes.propose(undo_raw)
    assert undo["ok"], undo
    denied = changes.apply(undo["plan_id"], raw=undo_raw, policy_path=path, authorized=True)
    assert denied["problems"][0]["code"] == "approval_scope_denied"
    data = json.loads(path.read_text())
    data["rules"][0].update(kinds=["undo"], repo_roots=[str(repo)])
    path.write_text(json.dumps(data))
    denied = changes.apply(undo["plan_id"], raw=undo_raw, policy_path=path, authorized=True)
    assert denied["problems"][0]["code"] == "approval_scope_denied"
    data["rules"][0]["repo_roots"] = [str(worktree)]
    path.write_text(json.dumps(data))
    restored = changes.apply(undo["plan_id"], raw=undo_raw, policy_path=path, authorized=True)
    assert restored["status"] == "applied", restored
    assert (repo / ".git/config").read_bytes() == before


def test_legacy_human_approval_migrates_without_policy_metadata(home: Path) -> None:
    document = changes.build(request())
    identifier = changes.digest(document)
    with contextlib.closing(Ledger()) as ledger, ledger.db:
        ledger.db.execute("CREATE TABLE changes (id TEXT PRIMARY KEY, document TEXT NOT NULL, "
                          "state TEXT NOT NULL, approved_at REAL NOT NULL, result TEXT)")
        ledger.db.execute("INSERT INTO changes VALUES (?, ?, 'approved', 0, NULL)",
                          (identifier, json.dumps(document)))
    assert changes.status(identifier)["authority"] == {"kind": "human"}
    applied = changes.apply(identifier, authorized=True)
    assert applied["status"] == "applied" and applied["authority"] == {"kind": "human"}, applied
    with contextlib.closing(changes.Store(readonly=True)) as store:
        assert store.db.execute("SELECT authority_kind FROM changes WHERE id=?", (identifier,)).fetchone() == ("human",)
