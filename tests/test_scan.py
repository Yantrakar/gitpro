import json
import os
import subprocess

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


def test_scan_json_redacts_remote_credentials(home, tmp_path, capsys):
    root = tmp_path / "src"
    repo = repo_with_remote(root, "tok", "https://user:ghp_xxx@github.com/o/r.git")
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "me@x.test"], check=True)
    assert run("scan", "--json", str(root)) == 0
    out = capsys.readouterr().out
    rep = json.loads(out)
    assert "ghp_xxx" not in out
    assert rep["overrides"][0]["remote"] == "https://user:***@github.com/o/r.git"
    assert rep["overrides"][0]["owner"] == "github.com/o"
    assert run("scan", "-v", str(root)) == 0
    assert "ghp_xxx" not in capsys.readouterr().out


def test_qa_scan_owner_fallback_redacts_credentials(home, tmp_path, capsys):
    """QA (wpfix): a remote host_owner() cannot parse falls back to the raw URL (or a mis-split of it) in
    overrides[].owner, the identities' owner counts, `scan -v` and `scan --suggest`; userinfo secrets must not."""
    root = tmp_path / "src"
    for name, url in (("upper", "HTTPS://x:SECRETUP@github.com/o/u.git"), ("nopath", "https://u:SECRETNP@host.example/")):
        repo = repo_with_remote(root, name, url)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "me@x.test"], check=True)
    for argv in (["--json"], ["-v"], ["--suggest"], ["--suggest", "--json"]):
        assert run("scan", *argv, str(root)) == 0
        out = capsys.readouterr().out
        assert "SECRET" not in out, (argv, out)
