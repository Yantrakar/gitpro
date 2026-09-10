"""QA regression tests for 12 (env): bugs found in review. They fail until envcmd.py is fixed."""
import json

from conftest import DEFAULT_CONFIG, repo_with_remote, run

DEFAULT_WITH_ACCOUNTS = DEFAULT_CONFIG.replace(
    'email = "me@default.test"\n',
    'email = "me@default.test"\ngh_account = "me-default"\nglab_account = "me-dlab"\n', 1)


def out(capsys, *argv):
    rc = run("env", *argv)
    c = capsys.readouterr()
    return rc, c.out, c.err


def body(o):
    return [ln for ln in o.splitlines() if not ln.startswith("#")]


def test_qa_default_glab_account_is_paired_with_gitlab_com(cfg, tmp_path, capsys, fake_bin):
    """README: [default].glab_account is a gitlab.com login. envcmd pairs it with github.com."""
    cfg(DEFAULT_WITH_ACCOUNTS)
    fake_bin.add("glab", "import sys; sys.exit(0 if argv[:2] == ['auth', 'token'] else 1)")
    r = repo_with_remote(tmp_path, "gl", "git@gitlab.com:someone/x.git")
    _, o, _ = out(capsys, str(r))
    assert "GITLAB_HOST='github.com'" not in o
    assert "glab auth token -h 'github.com'" not in o


def test_qa_default_gh_account_not_used_for_a_non_github_remote(cfg, tmp_path, capsys):
    """A GHE / gitlab repo with no identity must not get GH_HOST=github.com and the default's github token."""
    cfg(DEFAULT_WITH_ACCOUNTS)
    for name, url in (("ghe", "git@ghe.corp.test:x/y.git"), ("gl", "git@gitlab.com:someone/x.git")):
        r = repo_with_remote(tmp_path, name, url)
        _, o, _ = out(capsys, str(r), "--only", "gh")
        assert "me-default" not in o, f"{url}: {o}"


def test_qa_identity_without_gh_account_does_not_borrow_the_default(cfg, tmp_path, capsys):
    """Spec 12 §2/§7: gh lines only when the identity has gh_account; an identity without accounts → exit 0,
    nothing set. envcmd silently uses [default].gh_account under the matched identity's header."""
    cfg(DEFAULT_WITH_ACCOUNTS)
    r = repo_with_remote(tmp_path, "p", "git@github.com:me/x.git")   # identity "personal", no gh_account
    rc, o, _ = out(capsys, str(r))
    assert rc == 0
    assert "me-default" not in o and body(o) == []


def test_qa_pwsh_doubles_typographic_single_quotes(cfg, tmp_path, capsys):
    """PowerShell treats U+2018..U+201B as single quotes: 'O’Brien' ends the string at ’ (parse error;
    a crafted name runs code). Every such character inside '...' must be doubled."""
    cfg(DEFAULT_CONFIG.replace('name = "Work Me"', 'name = "O’Brien"'))
    r = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    _, o, _ = out(capsys, str(r), "--only", "author", "--shell", "pwsh")
    assert "$env:GIT_AUTHOR_NAME = 'O’’Brien'" in o


def test_qa_json_error_object_uses_error_key(cfg, tmp_path, capsys):
    """README §6: errors under --json are {"ok": false, "command", "error"}; envcmd writes "message"."""
    cfg()
    plain = tmp_path / "plain"
    plain.mkdir()
    rc, o, _ = out(capsys, str(plain), "--json")
    assert rc == 2 and "error" in json.loads(o)
