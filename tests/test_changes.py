"""Approval-bound changes, using disposable homes/repositories only."""
from __future__ import annotations

import contextlib
import concurrent.futures
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import pty
import select
import threading
import time
from pathlib import Path

import pytest
from rich.prompt import Confirm

from conftest import DEFAULT_CONFIG, FAKE_PUB, TIMEOUT, git, repo_with_remote, run, write_config
from gitpro import change_contract, changes, config, mcp, operations
from gitpro.operation_contract import OperationError, digest
from gitpro.operation_io import configuration_lock, state_dir


@pytest.fixture(autouse=True)
def private_files():
    before = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(before)


def request(kind: str = "configure", **values: object) -> dict:
    return {"version": 1, "kind": kind, **({"toml": DEFAULT_CONFIG} if kind == "configure" else {}), **values}


def approve(monkeypatch: pytest.MonkeyPatch, proposal: dict, *, consent: bool = True) -> str:
    assert proposal["ok"], proposal
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(Confirm, "ask", lambda *a, **kw: consent)
    result = changes.approve(proposal)
    assert result["ok"] is consent, result
    return proposal["plan_id"]


def apply(monkeypatch: pytest.MonkeyPatch, raw: dict) -> dict:
    proposal = changes.propose(raw)
    identifier = approve(monkeypatch, proposal)
    result = changes.apply(identifier, authorized=True)
    assert result["status"] == "applied", result
    return result


def tree(home: Path) -> dict:
    return {str(p.relative_to(home)): (p.read_bytes(), p.stat().st_mode & 0o777)
            for p in home.rglob("*") if p.is_file() and ".local" not in p.relative_to(home).parts}


def test_proposal_is_read_only_and_missing_configuration_is_structured(home):
    before = tree(home)
    missing = changes.propose(request("regenerate"))
    assert missing["decision"] == "needs_input" and missing["problems"][0]["code"] == "missing_config"
    proposal = changes.propose(request())
    assert proposal["ok"] and proposal["decision"] == "needs_approval", proposal
    assert len(proposal["plan_id"]) == 64
    assert tree(home) == before and not state_dir().exists()
    assert any(c["path"] == str(home / config.USER_CONFIG) for c in proposal["changes"])
    assert changes.propose(request())["plan_id"] == proposal["plan_id"]


@pytest.mark.parametrize("raw", [
    {}, {"version": True, "kind": "regenerate"}, request("configure", approved=True),
    request("configure", repo="/tmp/x"), request("configure", toml=""),
    request("regenerate", toml=DEFAULT_CONFIG), request("repo-identity"),
    request("configure", config_path="../x.toml"), request("undo", change_id="not-an-id"),
    request("configure", toml="x" * 131072), request("configure", toml="not valid TOML"),
])
def test_invalid_requests_do_not_touch_home(home, raw):
    before = tree(home)
    result = changes.propose(raw)
    assert not result["ok"] and result["decision"] == "needs_input", result
    assert tree(home) == before


def test_configuration_requires_separate_terminal_approval(home, monkeypatch):
    proposal = changes.propose(request())
    result = changes.apply(proposal["plan_id"], authorized=True)
    assert result["decision"] == "needs_approval"
    assert changes.approve(proposal)["problems"][0]["code"] == "terminal_required"
    approve(monkeypatch, proposal, consent=False)
    assert changes.status(proposal["plan_id"])["decision"] == "needs_approval"
    approve(monkeypatch, proposal)
    assert not (home / ".gitconfig").exists()
    assert changes.status(proposal["plan_id"])["status"] == "approved"
    assert changes.status(proposal["plan_id"])["files"] == [c["path"] for c in proposal["changes"]]
    assert changes.apply(proposal["plan_id"])["decision"] == "needs_approval"
    done = changes.apply(proposal["plan_id"], authorized=True)
    assert done["status"] == "applied", done
    assert config.load_all(home / config.USER_CONFIG).identities[0].email == "me@corp.test"
    assert (home / ".ssh/config").stat().st_mode & 0o777 == 0o600
    assert (home / config.USER_CONFIG).stat().st_mode & 0o777 == 0o600
    assert changes.propose(request())["status"] == "no_changes"
    assert changes.propose(request("regenerate"))["status"] == "no_changes"


def test_repeat_apply_replays_even_after_external_edits(home, monkeypatch):
    done = apply(monkeypatch, request())
    target = home / ".gitconfig"
    target.write_text(target.read_text() + "\n# external edit\n")
    before = tree(home)
    replay = changes.apply(done["plan_id"], authorized=True)
    assert replay["status"] == "applied" and replay["replayed"]
    assert tree(home) == before
    assert changes.status(done["plan_id"])["status"] == "applied"


@pytest.mark.parametrize("drift", ["content", "mode", "replacement", "symlink", "parent"])
def test_approval_is_invalidated_by_file_or_topology_drift(home, monkeypatch, drift):
    write_config(home, DEFAULT_CONFIG)
    target = home / ".gitconfig"
    target.write_text("# personal settings\n")
    identifier = approve(monkeypatch, changes.propose(request("regenerate")))
    if drift == "content":
        target.write_text("# a different file\n")
    elif drift == "mode":
        target.chmod(0o644)
    elif drift == "replacement":
        old = target.read_bytes()
        target.unlink()
        target.write_bytes(old)
    elif drift == "symlink":
        saved = home / "saved"
        target.rename(saved)
        target.symlink_to(saved)
    else:
        source_parent = home / ".config"
        source_parent.rename(home / "previous-config")
        source_parent.mkdir()
        (home / config.USER_CONFIG).write_bytes((home / "previous-config/gitpro.toml").read_bytes())
    before = tree(home)
    result = changes.apply(identifier, authorized=True)
    assert not result["ok"], result
    assert result["problems"][0]["code"] in ("stale_plan", "unsafe_path")
    assert tree(home) == before and not (home / ".gitconfig-identities").exists()


def test_drift_during_terminal_review_never_records_approval(home, monkeypatch):
    proposal = changes.propose(request())
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: True)

    def consent(*args, **kwargs):
        (home / ".gitconfig").write_text("# changed while reviewing\n")
        return True

    monkeypatch.setattr(Confirm, "ask", consent)
    result = changes.approve(proposal)
    assert result["problems"][0]["code"] == "stale_plan"
    assert not (home / config.USER_CONFIG).exists()


def test_tampered_display_is_not_the_approval_evidence(home, monkeypatch, capsys):
    proposal = changes.propose(request())
    proposal["changes"] = [{"diff": "FAKE REVIEW WITH NO CHANGES"}]
    identifier = approve(monkeypatch, proposal)
    shown = capsys.readouterr().err
    assert "FAKE REVIEW" not in shown and "me@corp.test" in shown
    assert changes.apply(identifier, authorized=True)["ok"]


def test_handwritten_crlf_preserved_and_unrelated_credentials_not_exposed(home, monkeypatch):
    original = b'# personal\r\n[credential]\r\n\thelper = !echo secret-sentinel\r\n'
    (home / ".gitconfig").write_bytes(original)
    proposal = changes.propose(request())
    assert proposal["ok"], proposal
    assert "secret-sentinel" not in json.dumps(proposal)
    identifier = approve(monkeypatch, proposal)
    result = changes.apply(identifier, authorized=True)
    assert result["ok"], result
    assert (home / ".gitconfig").read_bytes().startswith(original)
    assert changes.propose(request("regenerate"))["status"] == "no_changes"


def test_partial_write_failure_restores_exact_bytes_and_modes(home, monkeypatch):
    original = b"# original\r\n"
    (home / ".gitconfig").write_bytes(original)
    (home / ".gitconfig").chmod(0o600)
    before = tree(home)
    identifier = approve(monkeypatch, changes.propose(request()))
    replace = changes._replace
    count = 0

    def fail_second(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("simulated disk failure")
        return replace(*args)

    monkeypatch.setattr(changes, "_replace", fail_second)
    result = changes.apply(identifier, authorized=True)
    assert not result["ok"] and result["status"] == "rolled_back", result
    assert tree(home) == before
    assert changes.apply(identifier, authorized=True)["replayed"]
    assert changes.status(identifier)["status"] == "rolled_back"


def test_failed_apply_preserves_unrelated_concurrent_changes(home, monkeypatch):
    proposal = changes.propose(request())
    identifier = approve(monkeypatch, proposal)
    replace = changes._replace
    first = Path(proposal["changes"][0]["path"])
    calls = 0

    def conflict(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            first.write_text("# another editor owns this now\n")
            raise OSError("failure after external edit")
        return replace(*args)

    monkeypatch.setattr(changes, "_replace", conflict)
    result = changes.apply(identifier, authorized=True)
    assert result["decision"] == "indeterminate", result
    assert first.read_text() == "# another editor owns this now\n"
    assert changes.apply(identifier, authorized=True)["problems"][0]["code"] == "outcome_unknown"
    recovered = changes.apply(identifier, authorized=True, recover=True)
    assert recovered["decision"] == "indeterminate"
    assert first.read_text() == "# another editor owns this now\n"


def test_unwritten_files_are_not_rolled_back(home, monkeypatch):
    proposal = changes.propose(request())
    identifier = approve(monkeypatch, proposal)
    real = changes._replace
    calls = 0

    def concurrent_writer(path, expected, wanted, store, key):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(changes._bytes(wanted))
            path.chmod(wanted["mode"])
        return real(path, expected, wanted, store, key)

    monkeypatch.setattr(changes, "_replace", concurrent_writer)
    result = changes.apply(identifier, authorized=True)
    assert result["status"] == "rolled_back", result
    assert Path(proposal["changes"][1]["path"]).exists()


def test_journal_claim_precedes_effects_and_failed_finalization_is_uncertain(home, monkeypatch):
    identifier = approve(monkeypatch, changes.propose(request()))
    real = changes.Store.transition

    def broken_finish(self, key, state, result=None):
        if state != "applying":
            raise sqlite3.OperationalError("simulated full disk")
        return real(self, key, state, result)

    monkeypatch.setattr(changes.Store, "transition", broken_finish)
    result = changes.apply(identifier, authorized=True)
    assert result["problems"][0]["code"] == "journal_failed"
    assert result["decision"] == "indeterminate" and (home / config.USER_CONFIG).exists()
    assert changes.status(identifier)["status"] == "applying"
    monkeypatch.setattr(changes.Store, "transition", real)
    assert changes.apply(identifier, authorized=True)["problems"][0]["code"] == "outcome_unknown"
    assert changes.apply(identifier, authorized=True, recover=True)["status"] == "rolled_back"
    assert not (home / config.USER_CONFIG).exists()


def test_failed_write_ahead_record_makes_no_target_changes(home, monkeypatch):
    identifier = approve(monkeypatch, changes.propose(request()))
    before = tree(home)

    def fail(*a, **kw):
        raise sqlite3.OperationalError("disk unavailable")

    monkeypatch.setattr(changes.Store, "transition", fail)
    assert not changes.apply(identifier, authorized=True)["ok"]
    assert tree(home) == before


def test_successful_change_has_separately_approved_exact_undo(home, monkeypatch):
    original = b"# preserve\r\n"
    (home / ".gitconfig").write_bytes(original)
    before = tree(home)
    done = apply(monkeypatch, request())
    undo = changes.propose(request("undo", change_id=done["plan_id"]))
    assert undo["decision"] == "needs_approval", undo
    assert changes.apply(undo["plan_id"], authorized=True)["decision"] == "needs_approval"
    approve(monkeypatch, undo)
    result = changes.apply(undo["plan_id"], authorized=True)
    assert result["status"] == "applied", result
    assert tree(home) == before


def test_undo_refuses_to_overwrite_subsequent_edits(home, monkeypatch):
    done = apply(monkeypatch, request())
    (home / ".gitconfig").write_text("# newer content\n")
    undo = changes.propose(request("undo", change_id=done["plan_id"]))
    assert undo["problems"][0]["code"] == "stale_plan"


def test_native_git_config_lock_is_respected(home, monkeypatch):
    identifier = approve(monkeypatch, changes.propose(request()))
    lock = home / ".gitconfig.lock"
    lock.write_text("other Git process")
    result = changes.apply(identifier, authorized=True)
    assert result["problems"][0]["code"] == "target_busy"
    assert result["status"] == "rolled_back" and lock.read_text() == "other Git process"


def test_change_exclusion_and_parallel_execution_reader_locks(home, monkeypatch):
    identifier = approve(monkeypatch, changes.propose(request()))
    with configuration_lock():
        with configuration_lock():
            result = changes.apply(identifier, authorized=True)
            assert result["problems"][0]["code"] == "operation_busy"
    with configuration_lock(exclusive=True):
        with pytest.raises(OperationError, match="required lock"):
            with configuration_lock():
                pytest.fail("a reader entered during a configuration write")
    assert changes.apply(identifier, authorized=True)["ok"]


def test_repo_identity_repair_then_operation_ready(cfg, tmp_path, monkeypatch):
    cfg()
    repo = repo_with_remote(tmp_path, "repair", "git@github.com:corp-org/repair.git", commits=1)
    git(repo, "config", "user.email", "wrong@other.test")
    git(repo, "config", "user.name", "Wrong Name")
    git(repo, "config", "core.abbrev", "10")
    before = (repo / ".git/config").read_bytes()
    proposal = changes.propose(request("repo-identity", repo=str(repo)))
    assert proposal["ok"], proposal
    assert [c["path"] for c in proposal["changes"]] == [str(repo / ".git/config")]
    identifier = approve(monkeypatch, proposal)
    result = changes.apply(identifier, authorized=True)
    assert result["ok"], result
    assert git(repo, "config", "user.email") == "me@corp.test"
    assert git(repo, "config", "user.name") == "Work Me"
    assert git(repo, "config", "core.abbrev") == "10"
    assert changes.propose(request("repo-identity", repo=str(repo)))["status"] == "no_changes"
    (repo / "staged").write_text("new\n")
    git(repo, "add", "staged")
    op = {"version": 1, "operation": "commit", "repo": str(repo), "message": "fix: after repair"}
    assert operations.preflight(op)["decision"] == "ready"
    undo = changes.propose(request("undo", change_id=identifier))
    approve(monkeypatch, undo)
    assert changes.apply(undo["plan_id"], authorized=True)["ok"]
    assert (repo / ".git/config").read_bytes() == before


def test_repo_repair_rejects_local_includes(cfg, tmp_path):
    cfg()
    repo = repo_with_remote(tmp_path, "unsupported", "git@github.com:corp-org/repair.git")
    include = repo / "included"
    include.write_text("[user]\nemail = wrong@elsewhere.test\n")
    git(repo, "config", "include.path", str(include))
    proposal = changes.propose(request("repo-identity", repo=str(repo)))
    assert proposal["problems"][0]["code"] == "unsupported_repair"
    assert git(repo, "config", "user.email") == "wrong@elsewhere.test"


def test_repair_approval_rejects_destination_drift(cfg, tmp_path, monkeypatch):
    cfg()
    repo = repo_with_remote(tmp_path, "drift", "git@github.com:corp-org/repair.git")
    git(repo, "config", "user.email", "wrong@other.test")
    identifier = approve(monkeypatch, changes.propose(request("repo-identity", repo=str(repo))))
    git(repo, "remote", "set-url", "origin", "git@github.com:me/repair.git")
    result = changes.apply(identifier, authorized=True)
    assert not result["ok"] and git(repo, "config", "user.email") == "wrong@other.test"


def test_cli_contract_and_schema(home, tmp_path, capsys):
    source = tmp_path / "request.json"
    source.write_text(json.dumps(request()))
    assert run("change", "propose", "--request", str(source), "--json") == 0
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["decision"] == "needs_approval"
    assert run("change", "apply", "--plan-id", proposal["plan_id"], "-y") == 2
    assert json.loads(capsys.readouterr().out)["decision"] == "needs_approval"
    assert run("change", "propose", "--schema") == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["request"] == change_contract.REQUEST_SCHEMA
    assert schema["result"] == change_contract.RESULT_SCHEMA
    source.write_text('{"version":1,"kind":"regenerate","kind":"configure"}')
    assert run("change", "propose", "--request", str(source)) == 2
    assert json.loads(capsys.readouterr().out)["problems"][0]["code"] == "invalid_request"


def rpc(name: str, arguments: dict, *, execution: bool = False) -> dict:
    inp = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": name, "arguments": arguments}}) + "\n")
    out = io.StringIO()
    mcp.serve(inp, out, allow_execution=execution)
    return json.loads(out.getvalue())


def test_mcp_proposal_parity_and_no_approval_tool(home, monkeypatch):
    raw = request()
    result = rpc("propose_change", {"request": raw})
    assert result["result"]["structuredContent"] == changes.propose(raw)
    assert not any("approve" in name for name in mcp.TOOLS)
    identifier = approve(monkeypatch, changes.propose(raw))
    for name in ("apply_change", "recover_change"):
        assert "error" in rpc(name, {"plan_id": identifier})
    assert "error" in rpc("approve_change", {"plan_id": identifier}, execution=True)
    applied = rpc("apply_change", {"plan_id": identifier}, execution=True)
    assert applied["result"]["structuredContent"]["status"] == "applied", applied
    assert rpc("change_status", {"plan_id": identifier})["result"]["structuredContent"]["status"] == "applied"


def test_hard_death_recovery_removes_only_recorded_native_lock(home, monkeypatch, tmp_path):
    identifier = approve(monkeypatch, changes.propose(request()))
    script = """
import os, signal, sys
from gitpro import changes
real = changes.files.atomic_write
def die(path, *args, **kwargs):
    real(path, *args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
changes.files.atomic_write = die
changes.apply(sys.argv[1], authorized=True)
"""
    env = dict(os.environ, PYTHONPATH=str(Path(changes.__file__).resolve().parents[1]))
    child = subprocess.run([sys.executable, "-c", script, identifier], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    assert child.returncode == -signal.SIGKILL, child.stderr
    assert changes.status(identifier)["status"] == "applying"
    with contextlib.closing(changes.Store(readonly=True)) as store:
        document, _, _ = store.lookup(identifier)
        first = Path(document["changes"][0]["path"])
        native_lock = first.with_name(first.name + ".lock")
        assert native_lock.exists()
        assert store.written(identifier) == {str(first)}
        assert digest(document) == identifier
    recovered = changes.apply(identifier, authorized=True, recover=True)
    assert recovered["status"] == "rolled_back", recovered
    assert not native_lock.exists() and not first.exists()
    assert not changes.apply(identifier, authorized=True)["ok"]


def test_finished_results_replay_during_other_configuration_changes(home, monkeypatch):
    done = apply(monkeypatch, request())
    with configuration_lock(exclusive=True):
        result = changes.apply(done["plan_id"], authorized=True)
        assert result["replayed"] and result["status"] == "applied"


def test_two_agents_cannot_apply_the_same_change_concurrently(home, monkeypatch):
    identifier = approve(monkeypatch, changes.propose(request()))
    entered, finish = threading.Event(), threading.Event()
    real = changes._replace
    attempts = []

    def pause(path, *args):
        if not attempts:
            attempts.append(path)
            entered.set()
            assert finish.wait(10)
        return real(path, *args)

    monkeypatch.setattr(changes, "_replace", pause)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(changes.apply, identifier, authorized=True)
        try:
            assert entered.wait(10)
            second = changes.apply(identifier, authorized=True)
            assert second["problems"][0]["code"] == "operation_busy", second
        finally:
            finish.set()
        assert first.result(timeout=10)["status"] == "applied"
    assert changes.apply(identifier, authorized=True)["replayed"]


def test_new_configuration_during_apply_is_detected_and_preserved(home, monkeypatch):
    apply(monkeypatch, request())
    stale = home / ".gitconfig-work-github"
    stale.write_text(stale.read_text() + "# stale generated content\n")
    before = stale.read_bytes()
    identifier = approve(monkeypatch, changes.propose(request("regenerate")))
    real = changes._replace
    source = home / config.USER_CONFIG
    newer = DEFAULT_CONFIG.replace("Work Me", "Updated Work Name")

    def change_source(*args):
        result = real(*args)
        source.write_text(newer)
        return result

    monkeypatch.setattr(changes, "_replace", change_source)
    result = changes.apply(identifier, authorized=True)
    assert result["status"] == "rolled_back", result
    assert source.read_text() == newer and stale.read_bytes() == before


def test_recovery_does_not_remove_replaced_native_lock(home, monkeypatch):
    identifier = approve(monkeypatch, changes.propose(request()))
    with contextlib.closing(changes.Store()) as store:
        document, _, _ = store.lookup(identifier)
        first = Path(document["changes"][0]["path"])
        first.parent.mkdir(parents=True, exist_ok=True)
        native_lock = first.with_name(first.name + ".lock")
        native_lock.touch(mode=0o600)
        store.record_lock(identifier, native_lock, native_lock.stat())
        native_lock.unlink()
        native_lock.write_text("a different Git process")
        store.record_write(identifier, first)
        first.write_bytes(changes._bytes(document["changes"][0]["after"]))
        first.chmod(document["changes"][0]["after"]["mode"])
        store.transition(identifier, "applying")
    result = changes.apply(identifier, authorized=True, recover=True)
    assert result["decision"] == "indeterminate", result
    assert native_lock.read_text() == "a different Git process" and first.exists()


def test_actual_cli_terminal_approval_and_apply(home, tmp_path):
    proposal = changes.propose(request())
    source = tmp_path / "proposal.json"
    source.write_text(json.dumps(proposal))
    master, slave = pty.openpty()
    env = dict(os.environ, NO_COLOR="1", PYTHONPATH=str(Path(changes.__file__).resolve().parents[1]))
    child = subprocess.Popen([sys.executable, "-m", "gitpro", "change", "approve", "--write", "--proposal", str(source)],
                             env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    captured = bytearray()
    deadline = time.monotonic() + 30
    answered = False
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    data = os.read(master, 8192)
                except OSError:
                    break
                if not data:
                    break
                captured.extend(data)
                if not answered and b"Approve this exact change?" in captured:
                    os.write(master, b"y\n")
                    answered = True
            if child.poll() is not None:
                break
        assert answered, captured.decode(errors="replace")
        assert child.wait(timeout=5) == 0, captured.decode(errors="replace")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)
    assert changes.status(proposal["plan_id"])["status"] == "approved"
    assert not (home / config.USER_CONFIG).exists()
    completed = subprocess.run([sys.executable, "-m", "gitpro", "change", "apply", "--plan-id",
                                proposal["plan_id"], "-y"], env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["status"] == "applied"


@pytest.mark.parametrize("url", ["https://user:secret-sentinel@host.test/repo",
                                "https://host.test/repo?access_token=secret-sentinel"])
def test_credential_urls_are_refused_consistently_without_echoing(home, url):
    raw = request(toml=DEFAULT_CONFIG + f'\n# {url}\n')
    direct = changes.propose(raw)
    assert direct["problems"][0]["code"] == "sensitive_configuration"
    assert "secret-sentinel" not in json.dumps(direct)
    remote = rpc("propose_change", {"request": raw})
    assert remote["result"]["structuredContent"] == direct


def test_invalid_configuration_explains_missing_identity_fields(home):
    raw = request(toml='[default]\n[[identity]]\nid = "work"\nemail = "me@corp.test"\nhost = "github.com"\n')
    result = changes.propose(raw)
    assert result["problems"][0]["code"] == "invalid_config"
    assert "missing name" in result["problems"][0]["message"]


def test_guard_and_signer_outputs_are_in_the_same_approved_plan(home, monkeypatch):
    (home / ".ssh").mkdir(mode=0o700)
    public = home / ".ssh/id_work.pub"
    public.write_text(FAKE_PUB + " test\n")
    text = DEFAULT_CONFIG.replace('ssh_key = "~/.ssh/id_work"', 'ssh_key = "~/.ssh/id_work"\nsign = true')
    text += "\n[guard]\nhooks = true\n"
    proposal = changes.propose(request(toml=text))
    assert proposal["ok"], proposal
    paths = {c["path"] for c in proposal["changes"]}
    assert str(home / ".config/git/allowed_signers") in paths
    assert str(home / ".config/gitpro/hooks/pre-commit") in paths
    identifier = approve(monkeypatch, proposal)
    result = changes.apply(identifier, authorized=True)
    assert result["status"] == "applied", result
    assert "me@corp.test" in (home / ".config/git/allowed_signers").read_text()
    assert (home / ".config/gitpro/hooks/pre-commit").stat().st_mode & 0o777 == 0o700
    assert changes.propose(request("regenerate"))["status"] == "no_changes"
    assert not (home / ".ssh/id_work").exists()


def test_reconfigure_removes_only_stale_generated_files_and_undo_restores_them(home, monkeypatch):
    apply(monkeypatch, request())
    original = tree(home)
    smaller = DEFAULT_CONFIG.split("[[identity]]", 1)[0]
    done = apply(monkeypatch, request(toml=smaller))
    assert not (home / ".gitconfig-personal").exists()
    assert not (home / ".gitconfig-work-github").exists()
    undo = changes.propose(request("undo", change_id=done["plan_id"]))
    approve(monkeypatch, undo)
    assert changes.apply(undo["plan_id"], authorized=True)["status"] == "applied"
    assert tree(home) == original


def test_durable_file_modes_lock_cleanup_and_state_directory_chain(home, monkeypatch):
    proposal = changes.propose(request())
    expected = {Path(c["path"]): c["after_mode"] for c in proposal["changes"]}
    synced = []
    real = changes._sync

    def observe(path, *, directory=False):
        if not directory:
            assert path.stat().st_mode & 0o777 == expected[path]
        locks = [p.with_name(p.name + ".lock") for p in expected if p.parent == path]
        synced.append((path, directory, any(lock.exists() for lock in locks)))
        return real(path, directory=directory)

    monkeypatch.setattr(changes, "_sync", observe)
    monkeypatch.setattr("gitpro.operation_io.sync_path", observe)
    identifier = approve(monkeypatch, proposal)
    directories = {path for path, directory, _ in synced if directory}
    assert {state_dir(), *state_dir().parents} <= directories
    assert changes.apply(identifier, authorized=True)["status"] == "applied"
    assert {path for path, directory, _ in synced if not directory} == set(expected)
    for parent in {p.parent for p in expected}:
        events = [locked for path, directory, locked in synced if directory and path == parent]
        assert events and events[-1] is False


@pytest.fixture(params=["normal", "bare", "separate", "linked"])
def git_metadata(home: Path, request: pytest.FixtureRequest) -> Path:
    metadata = home / "repository-metadata"
    if request.param == "bare":
        subprocess.run(["git", "init", "--quiet", "--bare", str(metadata)], check=True, timeout=TIMEOUT)
        return metadata
    repo = repo_with_remote(home, "metadata-worktree", None, commits=1)
    if request.param in ("separate", "linked"):
        subprocess.run(["git", "-C", str(repo), "init", "--quiet", "--separate-git-dir", str(metadata)],
                       check=True, timeout=TIMEOUT)
    if request.param == "linked":
        linked = home / "linked-worktree"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--quiet", "-b", "linked", str(linked)],
                       check=True, timeout=TIMEOUT)
        repo = linked
    return Path(git(repo, "rev-parse", "--absolute-git-dir"))


def custom_file_config(path: Path) -> str:
    return ('[default]\nname="Review"\nemail="review@corp.test"\n'
            '[[identity]]\nid="review"\nname="Review"\nemail="review@corp.test"\n'
            'host="github.com"\nowners=["review-owner"]\n' + f"file={json.dumps(str(path))}\n")


@pytest.mark.parametrize("target", ["config", "HEAD", "index", "refs/heads/topic", "objects/info/alternates"])
@pytest.mark.parametrize("kind", ["configure", "regenerate"])
def test_managed_changes_reject_git_metadata(home: Path, git_metadata: Path, target: str, kind: str) -> None:
    text = custom_file_config(git_metadata / target)
    if kind == "regenerate":
        write_config(home, text)
    raw = request(toml=text) if kind == "configure" else request("regenerate")
    before = tree(home)
    result = changes.propose(raw)
    assert result["decision"] == "blocked" and result["problems"][0]["code"] == "unsafe_target", result
    assert tree(home) == before and not state_dir().exists()


def test_configuration_source_cannot_be_inside_git_metadata(home: Path, git_metadata: Path) -> None:
    before = tree(home)
    result = changes.propose(request(config_path=str(git_metadata / "source.toml")))
    assert result["problems"][0]["code"] == "unsafe_target", result
    assert tree(home) == before


def test_stale_manifest_cannot_remove_git_metadata(home: Path, git_metadata: Path) -> None:
    from gitpro import render
    write_config(home, DEFAULT_CONFIG)
    stale = git_metadata / "stale-profile"
    stale.write_text(render.HEADER + "\n[user]\nname=Old\n")
    (home / ".gitconfig-identities").write_text(render.HEADER + "\n" + render.MANIFEST + str(stale) + "\n")
    before = tree(home)
    result = changes.propose(request("regenerate"))
    assert result["problems"][0]["code"] == "unsafe_target", result
    assert tree(home) == before


@pytest.mark.parametrize("name", [".git", ".GIT", ".GiT"])
def test_generated_file_cannot_replace_a_git_directory_pointer(home: Path, name: str) -> None:
    pointer = home / "worktree" / name
    pointer.parent.mkdir()
    pointer.write_text("gitdir: ../repository-metadata\n")
    result = changes.propose(request(toml=custom_file_config(pointer)))
    assert result["problems"][0]["code"] == "unsafe_target", result
    assert pointer.read_text() == "gitdir: ../repository-metadata\n"


def test_old_approval_cannot_write_git_metadata(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = repo_with_remote(home, "legacy", None, commits=1)
    target = repo / ".git/HEAD"
    before = target.read_bytes()
    with monkeypatch.context() as legacy:
        legacy.setattr(changes, "_protect_git_metadata", lambda path: None)
        identifier = approve(legacy, changes.propose(request(toml=custom_file_config(target))))
    result = changes.apply(identifier, authorized=True)
    assert result["problems"][0]["code"] == "unsafe_target", result
    assert target.read_bytes() == before
    assert changes.status(identifier)["status"] == "approved"


@pytest.mark.parametrize("recover", [False, True])
def test_legacy_git_metadata_backups_can_still_be_restored(
        home: Path, monkeypatch: pytest.MonkeyPatch, recover: bool) -> None:
    repo = repo_with_remote(home, "legacy", None, commits=1)
    target = repo / ".git/HEAD"
    before = target.read_bytes()
    with monkeypatch.context() as legacy:
        legacy.setattr(changes, "_protect_git_metadata", lambda path: None)
        result = apply(legacy, request(toml=custom_file_config(target)))
    assert target.read_bytes() != before
    identifier = result["plan_id"]
    if recover:
        with contextlib.closing(changes.Store()) as store:
            store.transition(identifier, "applying")
        restored = changes.apply(identifier, recover=True, authorized=True)
    else:
        restored = apply(monkeypatch, request("undo", change_id=identifier))
    assert restored["ok"], restored
    assert target.read_bytes() == before


def test_custom_generated_config_in_a_worktree_is_still_supported(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = repo_with_remote(home, "dotfiles", None, commits=1)
    head = (repo / ".git/HEAD").read_bytes()
    target = repo / "git/profile.inc"
    result = apply(monkeypatch, request(toml=custom_file_config(target)))
    assert result["status"] == "applied" and b"review@corp.test" in target.read_bytes()
    assert (repo / ".git/HEAD").read_bytes() == head


def test_relative_state_path_rejects_changes_before_approval(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "relative-state")
    before = tree(home)
    result = changes.propose(request())
    assert result["decision"] == "needs_input" and result["problems"][0]["code"] == "invalid_state_path"
    assert tree(home) == before and not (home / "relative-state").exists()
