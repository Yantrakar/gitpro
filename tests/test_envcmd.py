"""env: shell lines that fetch the forge token for a repo's identity (spec 12). Never a token value."""
import json
import shutil
import subprocess

import pytest

from conftest import DEFAULT_CONFIG, git, repo_with_remote, run

GH = "git@github.com:corp-org/app.git"
GHE_CFG = DEFAULT_CONFIG + """
[[identity]]
id = "ghe"
name = "Ghe Me"
email = "me@ghe.test"
host = "ghe.corp.test"
owners = ["corp"]
ssh_key = "~/.ssh/id_ghe"
gh_account = "me-ghe"
"""
GLAB_CFG = DEFAULT_CONFIG + """
[[identity]]
id = "lab"
name = "Lab Me"
email = "me@lab.test"
host = "gitlab.com"
owners = ["someone"]
ssh_key = "~/.ssh/id_lab"
glab_account = "me-lab"
"""
SECRET = "SECRET-TOKEN-VALUE"


@pytest.fixture
def repo(cfg, tmp_path):
    cfg()
    return repo_with_remote(tmp_path, "app", GH)


def out(capsys, *argv):
    rc = run("env", *argv)
    c = capsys.readouterr()
    return rc, c.out, c.err


def sh_n(text, tmp_path):
    f = tmp_path / "o.sh"
    f.write_text(text)
    assert subprocess.run(["sh", "-n", str(f)]).returncode == 0


def test_default_output(repo, capsys, tmp_path):  # 1
    rc, o, _ = out(capsys, str(repo))
    assert rc == 0
    assert o.startswith("# gitpro env for ") and "identity work-github <me@corp.test>" in o
    assert "export GH_HOST='github.com'\n" in o
    assert """export GH_TOKEN="$(gh auth token -u 'me-work' -h 'github.com')"\n""" in o
    assert "direnv allow" in o.splitlines()[-1]
    assert "GIT_SSH_COMMAND" not in o and "GIT_AUTHOR" not in o
    sh_n(o, tmp_path)


def test_never_runs_gh_or_prints_token(repo, capsys, fake_bin):  # 2
    fake_bin.add("gh", f'print("{SECRET}")')
    for extra in ([], ["--json"], ["--shell", "fish"]):
        rc, o, e = out(capsys, str(repo), *extra)
        assert rc == 0 and SECRET not in o + e
    assert fake_bin.calls("gh") == []


def test_fish_and_pwsh(repo, capsys):  # 3
    _, o, _ = out(capsys, str(repo), "--shell", "fish")
    assert "set -gx GH_TOKEN (gh auth token -u 'me-work' -h 'github.com')" in o
    assert "set -gx GH_HOST 'github.com'" in o
    _, o, _ = out(capsys, str(repo), "--shell", "pwsh")
    assert "$env:GH_TOKEN = (gh auth token -u 'me-work' -h 'github.com')" in o
    assert "$env:GH_HOST = 'github.com'" in o
    _, o, _ = out(capsys, str(repo), "--shell", "bash")
    assert "export GH_HOST=" in o


def test_unset(repo, capsys):  # 4
    _, o, _ = out(capsys, str(repo), "--unset")
    assert "unset GH_HOST GH_TOKEN GH_ENTERPRISE_TOKEN GITLAB_HOST GITLAB_TOKEN\n" in o and "export" not in o
    _, o, _ = out(capsys, str(repo), "--unset", "--shell", "fish")
    assert "set -e -g GH_HOST\n" in o and "set -e -g GH_TOKEN\n" in o
    _, o, _ = out(capsys, str(repo), "--unset", "--shell", "pwsh")
    assert "Remove-Item Env:GH_HOST, Env:GH_TOKEN, Env:GH_ENTERPRISE_TOKEN, Env:GITLAB_HOST, Env:GITLAB_TOKEN -ErrorAction SilentlyContinue" in o


def body(o):
    return [ln for ln in o.splitlines() if not ln.startswith("#")]


def test_only_ssh_author(repo, capsys):  # 5
    _, o, _ = out(capsys, str(repo), "--only", "ssh")
    assert body(o) == ["export GIT_SSH_COMMAND='ssh -i ~/.ssh/id_work -o IdentitiesOnly=yes'"]
    assert "# overrides core.sshCommand for every repo in this shell" in o
    _, o, _ = out(capsys, str(repo), "--only", "author")
    assert body(o) == ["export GIT_AUTHOR_NAME='Work Me'", "export GIT_AUTHOR_EMAIL='me@corp.test'",
                       "export GIT_COMMITTER_NAME='Work Me'", "export GIT_COMMITTER_EMAIL='me@corp.test'"]
    _, o, _ = out(capsys, str(repo), "--only", "gh,ssh")
    assert any(ln.startswith("export GH_TOKEN") for ln in body(o))
    assert any(ln.startswith("export GIT_SSH_COMMAND") for ln in body(o))
    assert "GIT_AUTHOR" not in o


def test_override_adds_author(repo, capsys):  # 6
    git(repo, "config", "user.email", "wrong@x.test")
    rc, o, _ = out(capsys, str(repo))
    assert rc == 0
    assert "# repo .git/config overrides the identity; these env vars win over it" in o
    assert "export GIT_AUTHOR_EMAIL='me@corp.test'" in o
    assert "# ⚠ " in o


def test_ghe_and_json(cfg, tmp_path, capsys, home):  # 7, 10
    cfg(GHE_CFG)
    r = repo_with_remote(tmp_path, "ghe", "git@ghe.corp.test:corp/app.git")
    rc, o, _ = out(capsys, str(r))
    assert rc == 0
    assert "export GH_HOST='ghe.corp.test'" in o and "export GH_ENTERPRISE_TOKEN=" in o and "GH_TOKEN=" not in o
    _, j, _ = out(capsys, str(r), "--json")
    assert json.loads(j)["forge"]["token_var"] == "GH_ENTERPRISE_TOKEN"
    r2 = repo_with_remote(tmp_path, "gh", GH)
    d = json.loads(out(capsys, str(r2), "--json")[1])
    assert d["forge"] == {"host": "github.com", "cli": "gh", "account": "me-work", "token_var": "GH_TOKEN"}
    assert d["env_commands"]["GH_TOKEN"] == "gh auth token -u me-work -h github.com"
    assert "GH_TOKEN" not in d["env"] and d["env"]["GH_HOST"] == "github.com"
    assert d["ok"] is True and d["identity"]["id"] == "work-github" and d["problems"] == []
    assert d["ssh_key"].endswith("/.ssh/id_work")
    assert d["lines"][0] == "export GH_HOST='github.com'"


def test_glab(cfg, tmp_path, capsys, fake_bin, monkeypatch):  # 8
    cfg(GLAB_CFG)
    r = repo_with_remote(tmp_path, "lab", "git@gitlab.com:someone/x.git")
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    rc, o, _ = out(capsys, str(r))   # no glab on PATH... (fake_bin is first, system glab may exist)
    fake_bin.add("glab", "import sys; sys.exit(0 if argv[:2] == ['auth', 'token'] else 1)")
    rc, o, _ = out(capsys, str(r))
    assert rc == 0
    assert """export GITLAB_TOKEN="$(unset GITLAB_TOKEN; glab auth token -h 'gitlab.com')"\n""" in o
    assert "export GITLAB_HOST='gitlab.com'" in o
    monkeypatch.setenv("GITLAB_TOKEN", "x-secret")
    _, o, _ = out(capsys, str(r))
    assert "# GITLAB_TOKEN is already set in this shell; the line below replaces it" in o
    assert "x-secret" not in o
    fake_bin.add("glab", "import sys; sys.exit(1)")
    rc, o, _ = out(capsys, str(r))
    assert rc == 0 and "# glab auth token not available in your glab" in o and "GITLAB_TOKEN=" not in o


def test_glab_missing(cfg, tmp_path, capsys, monkeypatch):  # 8
    cfg(GLAB_CFG)
    r = repo_with_remote(tmp_path, "lab", "git@gitlab.com:someone/x.git")
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    rc, o, _ = out(capsys, str(r))
    assert rc == 0 and "GITLAB" not in o


def test_not_repo_and_no_identity(cfg, tmp_path, capsys):  # 9
    cfg()
    plain = tmp_path / "plain"
    plain.mkdir()
    rc, o, _ = out(capsys, str(plain))
    assert rc == 2 and len(o.strip().splitlines()) == 1 and o.startswith("# gitpro env: ")
    (tmp_path / "o1").write_text(o)
    r = repo_with_remote(tmp_path, "other", "git@gitlab.com:someone/x.git")
    rc, o2, _ = out(capsys, str(r))
    assert rc == 1 and o2.strip() == "# gitpro env: no identity for gitlab.com/someone; nothing to set"
    (tmp_path / "o2").write_text(o2)
    for name in ("o1", "o2"):
        assert subprocess.run(["sh", "-c", f'eval "$(cat {tmp_path / name})"']).returncode == 0


def test_no_config(home, tmp_path, capsys):  # 7 (failure table)
    r = repo_with_remote(tmp_path, "r", GH)
    rc, o, _ = out(capsys, str(r))
    assert rc == 2 and o.startswith("# gitpro env: no config found")


def test_identity_without_accounts(cfg, tmp_path, capsys):
    cfg()
    r = repo_with_remote(tmp_path, "p", "git@github.com:me/x.git")
    rc, o, _ = out(capsys, str(r))
    assert rc == 0 and "identity personal has no gh_account/glab_account; nothing to set" in o


def test_bad_flags(repo, capsys):  # 11
    rc, o, e = out(capsys, str(repo), "--shell", "csh")
    assert rc == 2 and "✗ unknown shell 'csh' (sh, fish, pwsh)" in e and o == ""
    rc, o, e = out(capsys, str(repo), "--only", "foo")
    assert rc == 2 and "✗ unknown key 'foo' (gh, glab, ssh, author)" in e and o == ""


def test_injection_rejected(cfg, tmp_path, capsys):  # 12
    cfg(DEFAULT_CONFIG.replace('gh_account = "me-work"', 'gh_account = "me;rm -rf x"'), apply=False)
    r = repo_with_remote(tmp_path, "app", GH)
    rc, o, e = out(capsys, str(r))
    assert rc == 2 and o == "" and "gh_account" in e and "✗" in e


def test_valid_rejects_unsafe_directly():
    from gitpro import envcmd
    from gitpro.config import ConfigError
    with pytest.raises(ConfigError, match="invalid gh_account"):
        envcmd._gh_items("github.com", "$(touch x)")
    with pytest.raises(ConfigError, match="invalid host"):
        envcmd._gh_items("a b", "me")


def test_check_old_gh(repo, capsys, fake_bin):  # 13
    fake_bin.add("gh", """
        import sys
        if argv[:2] == ["auth", "token"] and "-u" in argv:
            print("unknown shorthand flag: 'u' in -u", file=sys.stderr); sys.exit(1)
    """)
    rc, o, e = out(capsys, str(repo), "--check")
    assert rc == 2 and "✗ gh 2.40 or newer is needed for multiple accounts" in e and o == ""
    assert fake_bin.calls("gh") == [["auth", "token", "-u", "me-work", "-h", "github.com"]]


def test_check_ok_hides_token(repo, capsys, fake_bin):
    fake_bin.add("gh", f'print("{SECRET}")')
    rc, o, e = out(capsys, str(repo), "--check")
    assert rc == 0 and SECRET not in o + e and "✓" in e
    for call in fake_bin.calls("gh"):
        assert SECRET not in " ".join(call)


# ------------------------------------------------ evaluate the output for real

def test_eval_sh(repo, capsys, tmp_path, fake_bin):
    """Evaluate the lines in sh against a fake gh: the variable holds the fetched token, quoting survives."""
    fake_bin.add("gh", f'print("{SECRET}")')
    _, o, _ = out(capsys, str(repo), "--only", "gh,ssh,author")
    f = tmp_path / "env.sh"
    f.write_text(o)
    r = subprocess.run(["sh", "-c", f'. {f}; printf "%s|%s|%s|%s" "$GH_TOKEN" "$GH_HOST" "$GIT_AUTHOR_NAME" '
                                    f'"$GIT_SSH_COMMAND"'], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("|")[:3] == [SECRET, "github.com", "Work Me"]
    assert r.stdout.endswith("-o IdentitiesOnly=yes")
    u = tmp_path / "unset.sh"
    _, o, _ = out(capsys, str(repo), "--unset")
    u.write_text(o)
    r = subprocess.run(["sh", "-c", f'. {f}; . {u}; printf "%s" "${{GH_TOKEN-gone}}"'], capture_output=True, text=True)
    assert r.stdout == "gone"


def test_quoting_of_awkward_name(cfg, tmp_path, capsys):
    cfg(DEFAULT_CONFIG.replace('name = "Work Me"', 'name = "O\'Brien $(x) `y` \\\\ z"'))
    r = repo_with_remote(tmp_path, "app", GH)
    _, o, _ = out(capsys, str(r), "--only", "author")
    f = tmp_path / "a.sh"
    f.write_text(o)
    res = subprocess.run(["sh", "-c", f'. {f}; printf "%s" "$GIT_AUTHOR_NAME"'], capture_output=True, text=True)
    assert res.stdout == "O'Brien $(x) `y` \\ z"


@pytest.mark.skipif(shutil.which("fish") is None, reason="fish not installed")
def test_eval_fish(repo, capsys, tmp_path, fake_bin):
    fake_bin.add("gh", f'print("{SECRET}")')
    _, o, _ = out(capsys, str(repo), "--shell", "fish")
    f = tmp_path / "e.fish"
    f.write_text(o)
    r = subprocess.run(["fish", "-c", f"source {f}; echo $GH_TOKEN"], capture_output=True, text=True)
    assert r.stdout.strip() == SECRET


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_eval_pwsh(repo, capsys, tmp_path, fake_bin):
    fake_bin.add("gh", f'print("{SECRET}")')
    _, o, _ = out(capsys, str(repo), "--shell", "pwsh")
    f = tmp_path / "e.ps1"
    f.write_text(o)
    f.write_text(o + "\n[Console]::Out.Write($env:GH_TOKEN)\n")
    r = subprocess.run(["pwsh", "-NoProfile", "-File", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == SECRET



TOKEN_URL = "https://user:ghp_xxx@github.com/o/r.git"
DEFAULT_GH_CFG = DEFAULT_CONFIG.replace('email = "me@default.test"\n', 'email = "me@default.test"\ngh_account = "me-d"\n', 1)


def test_remote_url_with_token_is_redacted(cfg, tmp_path, capsys):
    """A password or token in the remote URL never reaches the output: text, --json, --unset, other shells."""
    cfg(DEFAULT_GH_CFG)
    r = repo_with_remote(tmp_path, "tok", TOKEN_URL)
    for extra in ([], ["--json"], ["--unset"], ["--shell", "fish"], ["--only", "gh,glab,ssh,author"]):
        rc, o, e = out(capsys, str(r), *extra)
        assert rc == 0, o + e
        assert "ghp_xxx" not in o + e
    cfg()                                   # no [default] account: the "nothing to set" answer
    for extra in ([], ["--json"], ["--unset"]):
        _, o, e = out(capsys, str(r), *extra)
        assert "ghp_xxx" not in o + e


def test_matched_url_is_redacted_in_header(cfg, tmp_path, home):
    """The header names the matched remote url; any userinfo secret in it is shown as ***."""
    import dataclasses
    from pathlib import Path

    from gitpro import config, envcmd, resolve
    path = cfg()
    c = config.load_all(*config.find(path, home))
    r = repo_with_remote(tmp_path, "app", GH)
    rep = resolve.compare(Path(r), c)
    m = dataclasses.replace(rep.expected.match, url=TOKEN_URL)
    rep = dataclasses.replace(rep, expected=dataclasses.replace(rep.expected, match=m))
    text = "\n".join(envcmd.lines(rep, c))
    assert "(origin https://user:***@github.com/o/r.git)" in text and "ghp_xxx" not in text
    assert "ghp_xxx" not in json.dumps(envcmd.as_json(rep, c))
