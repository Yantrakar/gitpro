import json
import os

from conftest import repo_with_remote, run

from gitpro import scan


def broken_submodule(root):
    """A submodule checkout whose superproject's .git/modules is gone: .git is a file pointing nowhere."""
    sub = root / "ext/bt"
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/bt\n")
    return sub


def test_broken_git_link(tmp_path):
    sub = broken_submodule(tmp_path)
    assert scan.skip_reason(sub, "fatal: not a git repository") == (
        "with a broken .git link", "broken .git: points to missing ../../.git/modules/bt")


def test_other_owner(tmp_path, monkeypatch):
    repo = repo_with_remote(tmp_path, "theirs", None)
    me = repo.stat().st_uid
    monkeypatch.setattr(os, "getuid", lambda: me + 1)  # as if someone else ran the scan
    reason, detail = scan.skip_reason(repo, "--local can only be used inside a git repository")
    name = scan._owner_name(me)
    assert reason == f"owned by {name}"
    assert detail == f"owned by {name}: git ignores other users' repos unless listed in safe.directory"


def test_other_errors_keep_gits_message(tmp_path):
    repo = repo_with_remote(tmp_path, "mine", None)
    assert scan.skip_reason(repo, "fatal: bad config") == ("git can't read", "fatal: bad config")


def test_skipped_summary():
    bad = [{"reason": "owned by root"}] * 26 + [{"reason": "with a broken .git link"}] * 4
    assert scan.skipped(bad) == "30 skipped: 26 owned by root, 4 with a broken .git link"
    assert scan.skipped([]) == ""


def test_scan_reports_skipped(home, tmp_path, capsys):
    root = tmp_path / "src"
    repo_with_remote(root, "app", None)
    sub = broken_submodule(root)

    assert run("scan", "--json", str(root)) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["repos"] == 1
    assert rep["unreadable"] == [{"path": str(sub), "reason": "with a broken .git link",
                                  "error": "broken .git: points to missing ../../.git/modules/bt"}]

    assert run("scan", str(root)) == 0
    out = capsys.readouterr().out
    assert "· 1 skipped: 1 with a broken .git link (-v lists them)" in out
    assert "⚠ skipped" not in out

    assert run("scan", "-v", str(root)) == 0
    out = capsys.readouterr().out
    assert "(-v lists them)" not in out
    assert f"⚠ skipped {sub}: broken .git: points to missing ../../.git/modules/bt" in out
