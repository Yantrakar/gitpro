"""QA review of package 01: reproductions of bugs found in review, now fixed (they were strict xfails)."""
import os
import stat
import subprocess


from conftest import DEFAULT_CONFIG, git, repo_with_remote
from gitpro import config, jsonfile, resolve


def load(path):
    return config.load_all(path, "user config")


def test_qa_multi_url_remote_matches_like_git(cfg, tmp_path):
    c = load(cfg())
    repo = repo_with_remote(tmp_path, "mirror", "git@example.org:someone/app.git")
    git(repo, "remote", "set-url", "--add", "origin", "git@github.com:corp-org/app.git")
    assert git(repo, "config", "user.email") == "me@corp.test"      # git: the second url matches
    rep = resolve.compare(repo, c)
    assert rep.expected.email == "me@corp.test"                     # gitpro says me@default.test
    assert rep.problems == ()                                       # gitpro says not_applied


def test_qa_identity_git_gpgsign_is_not_a_sign_mismatch(cfg, tmp_path):
    text = DEFAULT_CONFIG.replace('gh_account = "me-work"\n',
                                  'gh_account = "me-work"\n\n[identity.git.commit]\ngpgsign = true\n')
    c = load(cfg(text))
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    assert git(repo, "config", "commit.gpgsign") == "true"           # what the user asked gitpro for
    assert [p.code for p in resolve.compare(repo, c).problems] == []  # gitpro says sign_mismatch


def test_qa_extra_checks_take_the_report(cfg, tmp_path, monkeypatch):
    c = load(cfg())
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    monkeypatch.setattr(resolve, "EXTRA_CHECKS", [lambda r: [resolve.Problem("policy", "x", None)]])
    assert [p.code for p in resolve.compare(repo, c).problems] == ["policy"]


def test_qa_edit_json_undo_restores_the_original_mode(home):
    path = home / "settings.json"
    path.write_text('{"a": 1}\n')
    path.chmod(0o644)
    res = jsonfile.edit_json(path, lambda o: {**o, "b": 2}, command="agent install",
                             confirm=lambda diff: True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    subprocess.run(["sh", str(res.undo)], check=True, timeout=60, env=dict(os.environ))
    assert path.read_text() == '{"a": 1}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
