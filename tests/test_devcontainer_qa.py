"""QA findings for package 13 (reviewer-added): regression tests for bugs found in review, now fixed."""
import os
import shutil
import subprocess

import pytest

from conftest import repo_with_remote, run
from gitpro import devcontainer as dc

GH = "git@github.com:corp-org/app.git"


@pytest.fixture(autouse=True)
def on_host(monkeypatch, tmp_path):
    for var in dc.CONTAINER_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(dc, "DOCKERENV", tmp_path / "no-dockerenv")


@pytest.mark.parametrize("shell", [s for s in ("sh", "dash", "bash") if shutil.which(s)])
def test_qa_newline_in_workspace_path_is_not_executed(fake_bin, tmp_path, shell):
    fake_bin.add("gitpro", "sys.exit(0)")
    marker = tmp_path / "PWNED"
    folder = f"/home/u/a\nGITPRO_PATH\ntouch {marker}\nb"
    script = dc.LINE.replace("${localWorkspaceFolder}", folder)
    subprocess.run([shutil.which(shell), "-c", script], cwd=tmp_path, env={"PATH": os.environ["PATH"]},
                   capture_output=True, timeout=30)
    assert not marker.exists()


def test_qa_write_refuses_symlink_out_of_repo(cfg, home, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", GH)
    outside = home / ".claude/settings.json"
    outside.parent.mkdir(parents=True, exist_ok=True)
    original = '{\n  "permissions": {"allow": []}\n}\n'
    outside.write_text(original)
    (repo / ".devcontainer").mkdir()
    (repo / ".devcontainer/devcontainer.json").symlink_to(outside)
    run("devcontainer", str(repo), "--write", "-y")
    capsys.readouterr()
    assert outside.read_text() == original


def test_qa_undo_restores_crlf_bytes(cfg, home, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", GH)
    path = repo / ".devcontainer/devcontainer.json"
    path.parent.mkdir()
    original = b'{\r\n  "name": "x",\r\n  "image": "y"\r\n}\r\n'
    path.write_bytes(original)
    assert run("devcontainer", str(repo), "--write", "-y") == 0
    capsys.readouterr()
    undo = sorted((home / ".local/state/gitpro").glob("devcontainer-undo-*.sh"))[-1]
    subprocess.run(["sh", str(undo)], check=True)
    assert path.read_bytes() == original


def test_qa_status_flag_before_dir(cfg, home, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", GH)
    try:
        rc = run("devcontainer", "status", "--json", str(repo))
    except SystemExit as e:
        rc = e.code
    capsys.readouterr()
    assert rc == 0
