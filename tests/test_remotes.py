"""A repo whose remotes belong to different identities (spec 21, B1): git applies every matching
include and the later one wins, so the work for the other remote gets the wrong identity."""
import subprocess

from conftest import DEFAULT_CONFIG, git, repo_with_remote, run
from gitpro import config, pin, render, resolve

WORK = "git@github.com:corp-org/app.git"
MINE = "git@github.com:me/app.git"
SIGNING = DEFAULT_CONFIG.replace('ssh_key = "~/.ssh/id_personal"', 'ssh_key = "~/.ssh/id_personal"\nsign = true')


def load(path):
    return config.load_all(path, "user config")


def fork(tmp_path, origin=WORK, upstream=MINE, name="fork"):
    repo = repo_with_remote(tmp_path, name, origin)
    git(repo, "remote", "add", "upstream", upstream)
    return repo


def codes(rep):
    return [p.code for p in rep.problems]


# ---------------------------------------------------------------- compare

def test_two_identities_is_ambiguous(cfg, tmp_path):
    rep = resolve.compare(fork(tmp_path), load(cfg()))
    assert codes(rep) == ["ambiguous"]
    p = rep.problems[0]
    assert "origin → work-github, upstream → personal" in p.message
    assert "git applies personal" in p.message
    assert p.fix_argv == ("gitpro", "pin", "-y", "--remote", "origin", str(rep.repo))
    assert rep.expected.id == "personal"        # what git applies: the other checks stay quiet


def test_same_identity_is_not_ambiguous(cfg, tmp_path):
    repo = fork(tmp_path, upstream="git@github.com:corp-org/upstream.git")
    assert codes(resolve.compare(repo, load(cfg()))) == []


def test_unmatched_remote_is_not_ambiguous(cfg, tmp_path):
    repo = fork(tmp_path, upstream="git@gitlab.com:someone/app.git")
    rep = resolve.compare(repo, load(cfg()))
    assert codes(rep) == [] and rep.expected.id == "work-github"


def test_push_remote_order(cfg, tmp_path):
    path = cfg()
    repo = fork(tmp_path)
    git(repo, "config", "remote.pushDefault", "upstream")
    assert resolve.compare(repo, load(path)).problems[0].fix_argv[4] == "upstream"
    branch = git(repo, "symbolic-ref", "--short", "HEAD")
    git(repo, "config", f"branch.{branch}.pushRemote", "origin")
    assert resolve.compare(repo, load(path)).problems[0].fix_argv[4] == "origin"


def test_no_push_remote_names_the_choice(cfg, tmp_path):
    path = cfg()
    repo = repo_with_remote(tmp_path, "two", None)
    git(repo, "remote", "add", "corp", WORK)
    git(repo, "remote", "add", "mine", MINE)
    p = resolve.compare(repo, load(path)).problems[0]
    assert p.code == "ambiguous" and p.fix_argv is None
    assert p.fix == f"pin the remote this repo's work goes to: gitpro pin --remote <corp|mine> {repo.resolve()}"


def test_a_named_remote_or_destination_is_judged_alone(cfg, tmp_path):
    path = cfg()
    repo = fork(tmp_path)
    for kw in ({"remote": "origin"}, {"remote": "upstream"}, {"remote": "origin", "remote_url": WORK}):
        assert "ambiguous" not in codes(resolve.compare(repo, load(path), **kw))
    assert resolve.compare(repo, load(path), remote="origin").expected.id == "work-github"


def test_why_fails(cfg, tmp_path, capsys):
    cfg()
    repo = fork(tmp_path)
    capsys.readouterr()
    assert run("why", str(repo)) == 1
    out = capsys.readouterr().out
    assert "different identities" in out and f"gitpro pin -y --remote origin {repo.resolve()}" in out


# ---------------------------------------------------------------- pin --remote

def test_pin_remote_settles_it(cfg, tmp_path, capsys):
    path = cfg()
    repo = fork(tmp_path)
    assert run("pin", "--remote", "origin", str(repo)) == 0          # preview
    assert not (repo / ".git" / pin.FILE).exists()
    assert run("pin", "-y", "--remote", "origin", str(repo)) == 0
    text = (repo / ".git" / pin.FILE).read_text()
    assert text.startswith(render.HEADER) and f"{resolve.PIN_MARK}work-github\n" in text
    rep = resolve.compare(repo, load(path))
    assert codes(rep) == [] and rep.expected.id == "work-github"
    assert rep.expected.match.rule == "pinned to work-github, the identity of origin"
    assert rep.effective.email.value == "me@corp.test" and rep.effective.email.scope == "pin"
    assert rep.effective.sshcommand.scope == "pin"
    assert resolve.ssh_key_of(rep.effective.sshcommand.value).endswith("/.ssh/id_work")
    assert resolve.expected(repo, load(path)).id == "work-github"      # trailers, policy, guard
    capsys.readouterr()
    assert run("why", str(repo)) == 0


def test_pin_remote_turns_off_the_other_identitys_signing(cfg, tmp_path):
    path = cfg(SIGNING)
    repo = fork(tmp_path)
    rep = resolve.compare(repo, load(path))
    assert codes(rep) == ["ambiguous"] and rep.expected.sign      # personal signs, and wins
    assert run("pin", "-y", "--remote", "origin", str(repo)) == 0
    rep = resolve.compare(repo, load(path))
    assert codes(rep) == [] and git(repo, "config", "--get", "commit.gpgsign") == "false"
    assert git(repo, "config", "--get", "tag.gpgsign") == "false"


def test_pin_remote_carries_the_signing_identity(cfg, tmp_path):
    path = cfg(SIGNING)
    repo = fork(tmp_path)
    assert run("pin", "-y", "--remote", "upstream", str(repo)) == 0
    rep = resolve.compare(repo, load(path))
    assert codes(rep) == [] and rep.expected.id == "personal"
    assert rep.effective.gpgsign.scope == "pin" and rep.effective.signingkey.scope == "pin"


def test_pin_without_remote_needs_one(cfg, tmp_path, capsys):
    cfg()
    repo = fork(tmp_path)
    capsys.readouterr()
    assert run("pin", "-y", str(repo)) == 2
    err = capsys.readouterr().err
    assert "gitpro pin --remote <origin|upstream>" in err
    assert not (repo / ".git" / pin.FILE).exists()


def test_pin_remote_errors(cfg, tmp_path, capsys):
    cfg()
    repo = fork(tmp_path, upstream="git@gitlab.com:someone/app.git")
    assert run("pin", "-y", "--remote", "nope", str(repo)) == 2
    assert run("pin", "-y", "--remote", "upstream", str(repo)) == 2
    assert "no identity in the config matches upstream" in capsys.readouterr().err
    assert not (repo / ".git" / pin.FILE).exists()


def test_plain_pin_is_unchanged(cfg, tmp_path):
    cfg()
    repo = repo_with_remote(tmp_path, "app", WORK)
    assert run("pin", "-y", str(repo)) == 0
    assert (repo / ".git" / pin.FILE).read_text() == (
        f"{render.HEADER}\n# Re-run 'gitpro pin' after changing the config.\n"
        '[user]\n    name = "Work Me"\n    email = "me@corp.test"\n')


def test_old_pin_must_be_pinned_again(cfg, tmp_path, capsys):
    """A name/email pin from 0.3 has no marker: 0.3 pinned git's winner, so it does not answer which remote."""
    path = cfg()
    repo = fork(tmp_path)
    (repo / ".git" / pin.FILE).write_text(f"{render.HEADER}\n[user]\n    name = Work Me\n    email = me@corp.test\n")
    git(repo, "config", "--add", "include.path", pin.FILE)
    rep = resolve.compare(repo, load(path))
    amb = [p for p in rep.problems if p.code == "ambiguous"]
    assert amb and "pin is from gitpro 0.3" in amb[0].message
    assert amb[0].fix_argv == ("gitpro", "pin", "-y", "--remote", "origin", str(rep.repo))
    capsys.readouterr()
    assert run("why", "--json", str(repo)) == 1
    assert run("pin", "-y", "--remote", "origin", str(repo)) == 0
    assert codes(resolve.compare(repo, load(path))) == []


def test_pin_marker_for_a_gone_identity_is_ambiguous_again(cfg, tmp_path):
    path = cfg()
    repo = fork(tmp_path)
    assert run("pin", "-y", "--remote", "origin", str(repo)) == 0
    path = cfg(DEFAULT_CONFIG.replace('id = "work-github"', 'id = "corp"'))
    assert "ambiguous" in codes(resolve.compare(repo, load(path)))


def test_unpin_removes_a_remote_pin(cfg, tmp_path):
    path = cfg()
    repo = fork(tmp_path)
    assert run("pin", "-y", "--remote", "origin", str(repo)) == 0
    assert run("unpin", "-y", str(repo)) == 0
    assert codes(resolve.compare(repo, load(path))) == ["ambiguous"]
    assert subprocess.run(["git", "-C", str(repo), "config", "--local", "--get", "include.path"]).returncode == 1


# ---------------------------------------------------------------- scan

def scan_json(capsys, root):
    import json
    capsys.readouterr()
    assert run("scan", "--json", str(root)) == 0
    return json.loads(capsys.readouterr().out)


def test_scan_lists_ambiguous_repos_with_the_fix(cfg, tmp_path, capsys):
    cfg()
    repo = fork(tmp_path)
    rep = scan_json(capsys, tmp_path)
    assert [a["path"] for a in rep["ambiguous"]] == [str(repo.resolve())]
    assert rep["ambiguous"][0]["fix_argv"] == ["gitpro", "pin", "-y", "--remote", "origin", str(repo.resolve())]
    capsys.readouterr()
    assert run("scan", str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "1 repo with remotes for different identities" in out and "fix: gitpro pin -y --remote origin" in out
    assert run("pin", "-y", "--remote", "origin", str(repo)) == 0
    assert scan_json(capsys, tmp_path)["ambiguous"] == []


def test_scan_lists_repos_with_their_own_hooks_path(cfg, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "web", WORK)
    git(repo, "config", "core.hooksPath", ".husky")
    rep = scan_json(capsys, tmp_path)
    assert rep["own_hooks"] == [{"path": str(repo.resolve()), "hooks_path": ".husky", "guarded": False,
                                 "fix": f"gitpro guard install --repo {repo.resolve()}"}]
    capsys.readouterr()
    assert run("scan", str(tmp_path)) == 0
    assert "its own core.hooksPath: the commit guard does not run there" in capsys.readouterr().out


def test_scan_without_a_config_skips_the_remote_check(tmp_path, capsys):
    fork(tmp_path)
    rep = scan_json(capsys, tmp_path)
    assert rep["ambiguous"] == [] and rep["own_hooks"] == []
