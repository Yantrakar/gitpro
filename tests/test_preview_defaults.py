"""Default previews leave target files, history, approvals and helper state untouched."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import git, repo_with_remote, run
from gitpro import changes, devcontainer
from gitpro.operation_io import state_dir
from test_approval_policy import private_files
from test_changes import request
from test_operations import op_request
from test_setup import interact


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None, p.stat().st_mode)
            for p in root.rglob("*") if not p.is_symlink()}


def test_init_and_discovery_preview_without_creating_parents(home: Path) -> None:
    before = snapshot(home)
    assert run("init") == 0
    assert run("init", "--discover") == 0
    assert snapshot(home) == before
    assert run("init", "-y") == 0
    assert (home / ".config/gitpro.toml").is_file()


def test_setup_default_never_asks_to_save(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remaining = interact(monkeypatch, ["Fixture", "fixture@local.test"], [True, False])
    before = snapshot(home)
    assert run("setup") == 0
    assert snapshot(home) == before
    assert all(not answers for answers in remaining)


def test_apply_and_uninstall_default_do_not_change_files(cfg, home: Path) -> None:
    cfg(apply=False)
    before = snapshot(home)
    assert run("apply") == 0 and snapshot(home) == before
    assert run("apply", "-y") == 0
    before = snapshot(home)
    assert run("uninstall") == 0 and snapshot(home) == before


def test_pin_and_unpin_preview_and_explicit_write(cfg, tmp_path: Path) -> None:
    cfg()
    repo = repo_with_remote(tmp_path, "repo", "git@github.com:corp-org/repo.git")
    before = snapshot(repo)
    assert run("pin", str(repo)) == 0 and snapshot(repo) == before
    assert run("pin", "-y", str(repo)) == 0
    before = snapshot(repo)
    assert run("unpin", str(repo)) == 0 and snapshot(repo) == before
    assert run("unpin", "-y", str(repo)) == 0
    assert not (repo / ".git/git-identities").exists()


@pytest.mark.parametrize("command", ["agent", "mcp", "guard"])
def test_install_uninstall_previews_never_create_undo(cfg, home: Path, command: str, capsys) -> None:
    cfg()
    before = snapshot(home)
    assert run(command, "install", "--json") == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert snapshot(home) == before
    assert run(command, "install", "-y") == 0
    capsys.readouterr()
    before = snapshot(home)
    assert run(command, "uninstall", "--json") == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert snapshot(home) == before


def test_run_defaults_to_preflight_without_claiming(op_request: dict, tmp_path: Path, capsys) -> None:
    source = tmp_path / "request.json"
    source.write_text(json.dumps(op_request))
    before = git(Path(op_request["repo"]), "rev-parse", "HEAD")
    capsys.readouterr()
    assert run("run", "--request", str(source)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "preflight" and result["decision"] == "ready"
    assert git(Path(op_request["repo"]), "rev-parse", "HEAD") == before
    assert not state_dir().exists()


def test_approval_and_apply_preview_do_not_record_grants(home: Path, tmp_path: Path, capsys) -> None:
    # CLI establishes private creation modes before planning the fixture.
    run("init")
    proposal = changes.propose(request())
    assert proposal["ok"], proposal
    source = tmp_path / "proposal.json"
    source.write_text(json.dumps(proposal))
    for command in (["change", "approve", "--proposal", str(source)],
                    ["change", "apply", "--plan-id", proposal["plan_id"], "--proposal", str(source)]):
        capsys.readouterr()
        assert run(*command) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["command"] == "change propose" and result["plan_id"] == proposal["plan_id"]
        assert not state_dir().exists() and not (home / ".gitconfig").exists()


def test_legacy_generated_container_line_requires_explicit_migration() -> None:
    old = devcontainer.LINE.replace('gitpro pin -y "$dir"', 'gitpro pin "$dir"')
    assert not devcontainer.has_pin(old)
    assert devcontainer.propose(old) == devcontainer.LINE
    assert devcontainer.has_pin(devcontainer.LINE)
