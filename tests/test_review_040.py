"""Fixes from the 0.4.0 review (PR 20): keys over an orphan .pub, a FIFO reflog, the walk deadline, scan --fix
with worktrees, -n on pin, scan and audit, scan --lost's exit code, the deprecation of guard install and of a
failing --json command, and the undo of a settings file gitpro created."""
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import TIMEOUT, commit, git, repo_with_remote, run
from gitpro import hot, jsonfile, keys, pin, scan

WORK = "git@github.com:corp-org/app.git"
JANE = ("Jane Doe", "jane@corp.test")


def out_json(capsys):
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- keys: an orphan .pub or a symlink is left alone

def test_generate_skips_an_orphan_pub_and_a_symlink(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(keys.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    orphan = tmp_path / "id_agent"
    orphan.with_name("id_agent.pub").write_text("ssh-ed25519 AAAA held-by-an-agent\n")
    link = tmp_path / "id_link"
    link.symlink_to(tmp_path / "gone")
    new = tmp_path / "id_new"
    ks = [SimpleNamespace(path=str(p), email="me@corp.test") for p in (orphan, link, new)]
    assert keys.generate(ks, Path, passphrase=False) == 1
    assert [c[c.index("-f") + 1] for c in calls] == [str(new)]
    assert orphan.with_name("id_agent.pub").read_text() == "ssh-ed25519 AAAA held-by-an-agent\n"


def test_setup_does_not_create_a_key_over_an_orphan_pub(home, cfg, capsys):
    cfg()
    priv = home / ".ssh" / "id_work"
    priv.unlink()                                   # the .pub stays: e.g. a key held by an SSH agent
    capsys.readouterr()
    run("setup", "-n", "--json", "--agent", "none")
    rep = out_json(capsys)
    assert not [p for p in rep["pending"] if p["group"] == "SSH key" and "id_work" in p["what"]]
    assert any("id_work: no private key, but a .pub is there" in n for n in rep["notes"])


# ---------------------------------------------------------------- the reflog and the walk

def test_commit_identity_does_not_hang_on_a_fifo(tmp_path):
    repo = repo_with_remote(tmp_path, "app", WORK, commits=1, author=JANE)
    log = repo / ".git" / "logs" / "HEAD"
    log.unlink()
    os.mkfifo(log)
    start = time.monotonic()
    assert scan.commit_identity(repo) is None
    assert time.monotonic() - start < 5


def test_find_stops_at_the_deadline(tmp_path):
    repo_with_remote(tmp_path, "app", WORK)
    assert scan.find([tmp_path]) == [tmp_path / "app"]
    assert scan.find([tmp_path], deadline=time.monotonic() - 1) == []


# ---------------------------------------------------------------- scan --fix with linked worktrees

def test_scan_fix_removes_a_shared_setting_once(cfg, tmp_path, capsys):
    cfg()
    root = tmp_path / "src"
    repo = repo_with_remote(root, "app", WORK, commits=1, author=JANE)
    git(repo, "config", "user.email", "old@personal.test")
    git(repo, "worktree", "add", "-q", str(root / "app-topic"), "-b", "topic")
    capsys.readouterr()
    assert run("scan", "--json", str(root)) == 0
    over = {o["path"]: o["fixable"] for o in out_json(capsys)["overrides"]}
    assert sorted(over.values()) == [[], ["user.email"]]          # one config file, fixed from one entry
    assert run("scan", "--fix", "-y", str(root)) == 0
    out = capsys.readouterr().out
    assert git(repo, "config", "--local", "--get-all", "user.email") == ""
    script = Path(out.split("↩ undo: sh ")[1].split()[0]).expanduser().read_text()
    assert script.count("config user.email old@personal.test") == 1


# ---------------------------------------------------------------- -n on pin, scan and audit (spec 22 R4)

def test_scan_fix_n_shows_only(cfg, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path / "src", "app", WORK)
    git(repo, "config", "user.email", "old@personal.test")
    capsys.readouterr()
    assert run("scan", "--fix", "-n", "--json", "--repo", str(repo)) == 0
    rep = out_json(capsys)
    assert rep["dry_run"] is True and "removed" not in rep and rep["overrides"][0]["fixable"] == ["user.email"]
    assert run("scan", "--fix", "-n", "--repo", str(repo)) == 0
    assert "dry run, nothing changed" in capsys.readouterr().out
    assert git(repo, "config", "--local", "user.email") == "old@personal.test"
    assert run("scan", "--fix", "-n", "-y", "--repo", str(repo)) == 2
    assert "choose one: -n shows the change, -y makes it" in capsys.readouterr().err
    assert run("scan", "--fix", "--json", "--repo", str(repo)) == 0   # --json alone previews (spec 23 section 3.1)
    rep = out_json(capsys)
    assert rep["dry_run"] is True and "removed" not in rep
    assert git(repo, "config", "--local", "user.email") == "old@personal.test"
    assert run("scan", "--fix", "--json", "-y", "--repo", str(repo)) == 0
    assert out_json(capsys)["removed"][0]["key"] == "user.email"


def test_pin_n_shows_only(cfg, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", WORK)
    assert run("pin", "-n", str(repo)) == 0
    assert not (repo / ".git" / pin.FILE).exists()
    assert run("pin", "-n", "-y", str(repo)) == 2
    assert run("pin", "-y", str(repo)) == 0
    assert run("pin", "--remove", "-n", str(repo)) == 0
    assert "Would remove the pin" in capsys.readouterr().out and (repo / ".git" / pin.FILE).exists()


def test_audit_fix_n_shows_only(home, cfg, tmp_path, monkeypatch, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", WORK)
    git(repo, "checkout", "-q", "-b", "main")
    commit(repo, "wrong", author=("Personal Me", "me@personal.test"))
    before = git(repo, "rev-parse", "HEAD")
    capsys.readouterr()
    rc = run("audit", "--fix", "-n", "--json", str(repo))
    data = out_json(capsys)
    assert rc == 1 and data["dry_run"] is True and data["rewrite"]["commits"] == 1
    assert run("audit", "--fix", "-n", str(repo)) == 1
    assert "Dry run: nothing written. Rewrite the 1 unpushed commit with -y." in capsys.readouterr().out
    assert run("audit", "--fix", "-n", "-y", str(repo)) == 2
    assert git(repo, "rev-parse", "HEAD") == before


# ---------------------------------------------------------------- exit codes and deprecations

def test_scan_lost_exits_1_when_it_finds_lost_work(cfg, tmp_path, capsys):
    cfg()
    repo_with_remote(tmp_path / "w", "solo", None, commits=1, author=JANE)
    assert run("scan", "--lost", "--json", str(tmp_path / "w")) == 1
    assert run("scan", "--lost", str(tmp_path / "w")) == 1
    (tmp_path / "empty").mkdir()
    assert run("scan", "--lost", str(tmp_path / "empty")) == 0


def test_guard_install_from_the_hot_path_is_deprecated(home, monkeypatch, capsys):
    monkeypatch.delenv("GITPRO_NO_DEPRECATION")
    hot.main(["guard", "install"])                                 # a preview: nothing written
    assert "gitpro guard install without --repo is deprecated" in capsys.readouterr().err


def test_a_failing_deprecated_command_still_carries_the_field(cfg, tmp_path, monkeypatch, capsys):
    cfg()
    monkeypatch.delenv("GITPRO_NO_DEPRECATION")
    assert run("which", "--json", str(tmp_path / "missing")) == 2
    rep = out_json(capsys)
    assert rep["ok"] is False and rep["deprecated"] == {"removed_in": "0.5.0", "use": "gitpro why --json"}


# ---------------------------------------------------------------- the undo of a file gitpro created

def test_undo_of_a_created_file_keeps_it_once_changed(home):
    path = home / ".gemini" / "settings.json"
    yes = lambda diff: True
    first = jsonfile.edit_json(path, lambda o: {**o, "hooks": {}}, command="agent install", create={},
                               confirm=yes, mkdir=True)
    jsonfile.edit_json(path, lambda o: {**o, "mcpServers": {}}, command="mcp install", create={}, confirm=yes)
    r = subprocess.run(["sh", str(first.undo)], capture_output=True, text=True, timeout=TIMEOUT)
    assert path.exists() and "kept ~/.gemini/settings.json: it changed after gitpro created it" in r.stderr
    other = home / "made.json"
    res = jsonfile.edit_json(other, lambda o: {"k": 1}, command="x", create={}, confirm=yes)
    subprocess.run(["sh", str(res.undo)], check=True, timeout=TIMEOUT)
    assert not other.exists()
    subprocess.run(["sh", str(res.undo)], check=True, timeout=TIMEOUT)   # already gone: nothing to say
