"""resolve: expected vs effective identity, matched by the same rule git uses (includeIf hasconfig)."""
import json
import subprocess

import pytest

from conftest import DEFAULT_CONFIG, commit, git, repo_with_remote, run
from gitpro import config, render, resolve
from gitpro.config import Config, Default, Identity

GH = "git@github.com:corp-org"


# ---------------------------------------------------------------- wildmatch (10)

@pytest.mark.parametrize("pattern, text, want", [
    (f"{GH}/**", f"{GH}/sub/app.git", True),
    (f"{GH}/**", f"{GH}/app", True),
    (f"{GH}/**", f"{GH}/app.git", True),
    ("git@github.com:corp-*", f"{GH}/app.git", False),
    ("git@github.com:corp-*/app.git", f"{GH}/app.git", True),
    ("**/app.git", f"{GH}/app.git", True),
    ("**/app.git", "x/y/z/app.git", True),
    ("*app.git", f"{GH}/app.git", False),
    ("GIT@github.com:corp-org/**", f"{GH}/app.git", False),
    (f"{GH}/app.git", f"{GH}/app", False),
    (f"{GH}/sub/app.git", f"{GH}/sub/app", False),
    (f"{GH}/ap?", f"{GH}/app", True),
    (f"{GH}/ap?", f"{GH}/a/p", False),
    (f"{GH}/[ab]pp", f"{GH}/app", True),
    (f"{GH}/[!ab]pp", f"{GH}/app", False),
    ("https://github.com/**", "https://github.com/corp-org/app.git", True),
    ("a/**/b", "a/b", True),
    ("a/**/b", "a/x/y/b", True),
    ("a.b", "aXb", False),
    ("x+(y)", "x+(y)", True),
    # V-001: ** is special only as a whole path component; elsewhere it is *.
    ("git@github.com:**", "git@github.com:o/r.git", False),
    ("git@github.com:**", "git@github.com:r.git", True),
    ("git@github.com:o**", "git@github.com:o/r.git", False),
    ("git@github.com:o**", "git@github.com:oxr.git", True),
    ("git@github.com:*/**", "git@github.com:o/r.git", True),
    ("git@github.com:*/**", "git@github.com:o/s/r.git", True),
    ("git@github.com:*/**", "git@github.com:r.git", False),
    ("**", "a/b/c", True),
    ("a/**", "a/", True),
    ("a/**b", "a/x/b", False),
])
def test_wildmatch(pattern, text, want):
    assert resolve.wildmatch(pattern, text) is want


# ---------------------------------------------------------------- match_url (11, 12)

def ident(id, owners=(), **kw):
    return Identity(id=id, name="N", email=f"{id}@x.test", host=kw.pop("host", "github.com"), owners=list(owners), **kw)


def test_later_identity_wins():
    c = Config(Default(), (ident("first", ["a"]), ident("second", ["a", "b"])))
    i, pat = resolve.match_url("git@github.com:a/app.git", c)
    assert i.id == "second" and pat == "git@github.com:a/**"
    assert resolve.match_url("git@github.com:b/app.git", c)[0].id == "second"
    assert resolve.match_url("git@github.com:c/app.git", c) == (None, None)


def test_alias_and_userinfo():
    w = ident("w", ["corp-org"], ssh_alias="github-work", ssh_key="~/.ssh/id_work")
    c = Config(Default(), (w,))
    assert resolve.match_url("git@github-work:corp-org/app.git", c)[0] is w
    assert resolve.match_url("https://me@github.com/corp-org/app.git", c) == (None, None)
    assert resolve.match_url("https://github.com/corp-org/app.git", c)[1] in render.url_patterns(w)


@pytest.mark.parametrize("url, want", [
    ("git@github.com:corp-org/app.git", ("github.com", "corp-org", "app")),
    ("git@github.com:corp-org/app", ("github.com", "corp-org", "app")),
    ("ssh://git@gitlab.corp.test:2222/grp/sub/app.git", ("gitlab.corp.test", "grp/sub", "app")),
    ("ssh://gitlab.corp.test/grp/app.git", ("gitlab.corp.test", "grp", "app")),
    ("https://me@github.com/corp-org/app.git", ("github.com", "corp-org", "app")),
    ("https://github.com/corp-org/app/", ("github.com", "corp-org", "app")),
    ("github.com:corp-org/app.git", ("github.com", "corp-org", "app")),
    ("/srv/git/app.git", (None, None, "app")),
    ("file:///srv/git/app.git", (None, None, "app")),
    ("../app", (None, None, "app")),
])
def test_parse_url(url, want):
    assert resolve.parse_url(url) == want


# ---------------------------------------------------------------- differential against real git (13)

DIFF_CONFIG = """
[default]

[[identity]]
id = "gh-corp"
name = "Corp"
email = "corp@corp.test"
host = "github.com"
owners = ["corp-org"]
ssh_key = "~/.ssh/id_work"
ssh_alias = "github-work"

[[identity]]
id = "gl"
name = "Gl"
email = "gl@corp.test"
host = "gitlab.corp.test"
owners = ["grp/sub"]
ssh_port = 2222

[[identity]]
id = "whole"
name = "Whole"
email = "whole@corp.test"
host = "git.whole.test"
ssh_key = "~/.ssh/id_whole"
ssh_alias = "whole-alias"

[[identity]]
id = "team"
name = "Team"
email = "team@corp.test"
host = "github.com"
owners = ["team-*"]
"""

DIFF_URLS = [
    "git@github.com:corp-org/app.git",                 # ssh
    "https://github.com/corp-org/app.git",             # https
    "git@github-work:corp-org/app.git",                # alias
    "ssh://git@gitlab.corp.test:2222/grp/sub/app.git",  # port
    "ssh://git@gitlab.corp.test/grp/sub/app.git",
    "git@gitlab.corp.test:grp/sub/deep/app.git",       # nested group
    "git@gitlab.corp.test:grp/subx/app.git",
    "https://me@github.com/corp-org/app.git",          # userinfo
    "git@github.com:corp-org/app",                     # no .git
    "git@github.com:Corp-Org/app.git",                 # uppercase owner
    "git@github.com:corp-org",                         # no repo
    "git@github.com:team-x/app.git",
    "git@github.com:team-x/sub/app.git",
    "https://git.whole.test/any/thing.git",
    "ssh://git@github.com:22/corp-org/app.git",
    # owner-less identity (V-001): scp, alias, ssh://, single segment
    "git@git.whole.test:any/thing.git",
    "git@git.whole.test:any/deep/thing.git",
    "git@git.whole.test:thing.git",
    "git@whole-alias:any/thing.git",
    "ssh://git@git.whole.test/any/thing.git",
    "ssh://git@whole-alias/thing.git",
]


def test_match_url_agrees_with_git(cfg, tmp_path):
    path = cfg(DIFF_CONFIG)
    c = config.load_all(path)
    for n, url in enumerate(DIFF_URLS):
        repo = repo_with_remote(tmp_path, f"d{n}", url)
        assert resolve.expected(repo, c).email == (git(repo, "config", "user.email") or None), url


# Raw patterns straight into includeIf, so git itself decides (V-001).
PATTERN_CASES = [
    ("git@github.com:**", "git@github.com:o/r.git"),
    ("git@github.com:**", "git@github.com:r.git"),
    ("git@github.com:o**", "git@github.com:o/r.git"),
    ("git@github.com:*/**", "git@github.com:o/r.git"),
    ("git@github.com:*/**", "git@github.com:o/s/r.git"),
    ("git@github.com:corp-*", "git@github.com:corp-org/app.git"),
    ("**/app.git", "git@github.com:corp-org/app.git"),
    ("*app.git", "git@github.com:corp-org/app.git"),
    ("https://github.com/**", "https://github.com/corp-org/app.git"),
    ("https://github.com/*/a?p.git", "https://github.com/corp-org/app.git"),
    ("https://github.com/[!x]*/**", "https://github.com/corp-org/app.git"),
    ("a/**/b", "a/b"),
    ("a/**/b", "a/x/y/b"),
    ("a/**b", "a/x/b"),
]


def test_wildmatch_agrees_with_git(home, tmp_path):
    (home / ".inc").write_text("[user]\n\temail = hit@x.test\n")
    for n, (pattern, url) in enumerate(PATTERN_CASES):
        (home / ".gitconfig").write_text(f'[includeIf "hasconfig:remote.*.url:{pattern}"]\n\tpath = ~/.inc\n')
        repo = repo_with_remote(tmp_path, f"p{n}", url)
        assert resolve.wildmatch(pattern, url) is (git(repo, "config", "user.email") == "hit@x.test"), (pattern, url)


# ---------------------------------------------------------------- compare (14..21)

def load(path):
    return config.load_all(path, "user config")


def test_matching_repo(cfg, tmp_path, capsys):  # 14
    c = load(cfg())
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    rep = resolve.compare(repo, c)
    assert rep.expected.id == "work-github" and rep.expected.email == "me@corp.test"
    assert rep.expected.match.remote == "origin" and rep.expected.match.pattern == "git@github.com:corp-org/**"
    assert rep.expected.ssh_key == str(c.path.parent.parent / ".ssh/id_work")
    assert rep.effective.email.scope == "generated" and rep.effective.email.value == "me@corp.test"
    assert rep.effective.remotes == {"origin": "git@github.com:corp-org/app.git"}
    assert rep.problems == () and rep.ok()
    capsys.readouterr()
    assert run("why", str(repo)) == 0
    out = capsys.readouterr().out
    assert "✓ identity matches" in out
    assert "config    ~/.config/gitpro.toml (user config)" in out
    assert "matched origin by pattern git@github.com:corp-org/**" in out
    assert "ssh key   ~/.ssh/id_work   via core.sshcommand" in out


def test_repo_override(cfg, tmp_path, capsys):  # 15
    path = cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    git(repo, "config", "user.email", "old@personal.test")
    rep = resolve.compare(repo, load(path))
    assert [p.code for p in rep.problems] == ["override"]
    assert "config --unset user.email" in rep.problems[0].fix
    assert rep.problems[0].message == \
        "user.email is set in the repo's .git/config and overrides the work-github identity"
    assert rep.effective.email.scope == "repo" and rep.effective.email.origin == f"file:{repo}/.git/config"
    capsys.readouterr()
    assert run("why", str(repo)) == 1
    out = capsys.readouterr().out
    assert "✗ user.email is set in the repo's .git/config" in out
    assert "(or: gitpro scan --fix --repo" in out
    assert run("why", "--json", str(repo)) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["command"] == "why"
    assert data["config"] == {"path": str(path), "how": "user config"}
    assert data["problems"][0]["code"] == "override"
    assert data["problems"][0]["fix_argv"] == ["git", "-C", str(repo.resolve()), "config", "--unset", "user.email"]
    assert data["effective"]["email"]["scope"] == "repo" and data["effective"]["pinned"] is False
    assert data["expected"]["match"]["rule"] == "matched origin by pattern git@github.com:corp-org/**"


def test_pin_stale(cfg, tmp_path, capsys):  # 16
    cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    assert run("pin", str(repo)) == 0
    path = cfg(DEFAULT_CONFIG.replace("me@corp.test", "new@corp.test"))
    rep = resolve.compare(repo, load(path))
    assert rep.effective.pinned and rep.effective.email.scope == "pin"
    assert [p.code for p in rep.problems] == ["pin_stale"]
    assert rep.problems[0].fix == f"gitpro pin {repo}"


def test_no_identity(cfg, tmp_path, capsys):  # 17
    path = cfg("[default]\n" + DEFAULT_CONFIG.split("\n\n", 1)[1])
    repo = repo_with_remote(tmp_path, "foo", "git@gitlab.com:someone/foo.git")
    rep = resolve.compare(repo, load(path))
    assert rep.problems[0].code == "no_identity"
    assert rep.expected.id is None and rep.expected.match.rule == \
        "no identity pattern matches git@gitlab.com:someone/foo.git"
    capsys.readouterr()
    assert run("why", str(repo)) == 1
    out = capsys.readouterr().out
    assert "git will refuse to commit" in out and "→ add an identity: gitpro scan --suggest --repo" in out
    assert "effective (unset)" in out


def test_url_form(cfg, tmp_path):  # 12
    path = cfg()
    repo = repo_with_remote(tmp_path, "app", "https://me@github.com/corp-org/app.git")
    rep = resolve.compare(repo, load(path))
    codes = [p.code for p in rep.problems]
    assert "url_form" in codes
    p = rep.problems[codes.index("url_form")]
    assert p.fix == f"git -C {repo} remote set-url origin https://github.com/corp-org/app.git"
    assert "no user in the URL" in p.message


def test_url_form_missing_user_and_port(cfg, tmp_path):
    path = cfg()
    c = load(path)
    r1 = repo_with_remote(tmp_path, "a", "ssh://github.com/corp-org/app.git")
    p1 = [p for p in resolve.compare(r1, c).problems if p.code == "url_form"][0]
    assert p1.fix.endswith("remote set-url origin ssh://git@github.com/corp-org/app.git")
    r2 = repo_with_remote(tmp_path, "b", "ssh://git@github.com:2200/corp-org/app.git")
    p2 = [p for p in resolve.compare(r2, c).problems if p.code == "url_form"][0]
    assert p2.fix.endswith("remote set-url origin ssh://git@github.com/corp-org/app.git")


def test_explicit_env(cfg, tmp_path, monkeypatch):  # V-004: hooks pass their own env
    path = cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    c = load(path)
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "pat@other.test")
    assert resolve.compare(repo, c, env={}).problems == ()
    rep = resolve.compare(repo, c, env={"GIT_COMMITTER_EMAIL": "x@y.test", "GIT_AUTHOR_NAME": "Work Me"})
    assert [(p.code, p.fix) for p in rep.problems] == [("env_override", "unset GIT_COMMITTER_EMAIL")]


def test_env_override(cfg, tmp_path, monkeypatch):  # 18
    path = cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    c = load(path)
    # `git -c user.email=...` in the user's own command line is invisible to a separate process.
    assert git(repo, "-c", "user.email=x@y.test", "config", "user.email") == "x@y.test"
    assert resolve.compare(repo, c).problems == ()
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "x@y.test")
    rep = resolve.compare(repo, c)
    assert [p.code for p in rep.problems] == ["env_override"]
    assert rep.problems[0].fix == "unset GIT_AUTHOR_EMAIL"
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "me@corp.test")
    assert resolve.compare(repo, c).problems == ()


def test_worktree(cfg, tmp_path):  # 19
    path = cfg()
    c = load(path)
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git", commits=1)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    assert run("pin", str(repo)) == 0
    rep = resolve.compare(wt, c)
    assert rep.effective.pinned and rep.problems == ()
    assert rep.effective.email.origin == f"file:{repo}/.git/git-identities"
    assert rep.repo == wt
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(wt, "config", "--worktree", "user.email", "wt@corp.test")
    rep = resolve.compare(wt, c)
    assert rep.effective.email.scope == "worktree"
    assert rep.effective.email.origin.startswith("file:/")
    assert [p.code for p in rep.problems] == ["override"]


def test_pin_seen_from_subdir_and_bare(cfg, tmp_path):  # V-003: relative origins
    c = load(cfg())
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    assert run("pin", str(repo)) == 0
    (repo / "sub/deep").mkdir(parents=True)
    rep = resolve.compare(repo / "sub/deep", c)
    assert rep.effective.pinned and rep.effective.email.scope == "pin" and rep.problems == ()
    assert rep.effective.email.origin == f"file:{repo}/.git/git-identities" and rep.repo == repo
    bare = tmp_path / "b.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=60)
    git(bare, "remote", "add", "origin", "git@github.com:corp-org/b.git")
    git(bare, "config", "user.email", "bare@corp.test")
    eff = resolve.effective(bare)
    assert eff.email.origin == f"file:{bare}/config" and eff.email.scope == "repo"
    assert resolve.compare(bare, c).repo == bare


def test_extra_checks_and_policy_code(cfg, tmp_path, monkeypatch, capsys):  # V-007
    c = load(cfg())
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    seen = []

    def check(report):  # spec 01 §4.3: check(report) -> Iterable[Problem]
        seen.append((report.repo, report.expected.id, report.effective.email.value, report.problems))
        return [resolve.Problem("policy", "corp policy says no", None)]

    def broken(report):
        raise RuntimeError("policy file unreadable")
    monkeypatch.setattr(resolve, "EXTRA_CHECKS", [broken, check])
    rep = resolve.compare(repo, c)
    assert [p.code for p in rep.problems] == ["policy"] and not rep.ok()
    assert seen == [(repo, "work-github", "me@corp.test", ())]
    assert "policy" in resolve.PROBLEM_CODES
    err = capsys.readouterr().err
    assert err.startswith("⚠ skipped check") and "policy file unreadable" in err


DEFAULT_KEY_CONFIG = DEFAULT_CONFIG.replace('email = "me@default.test"\n',
                                            'email = "me@default.test"\nssh_key = "~/.ssh/id_default"\n')


def test_default_key_is_not_expected(cfg, tmp_path):  # V-008: render writes no sshCommand for [default]
    c = load(cfg(DEFAULT_KEY_CONFIG))
    repo = repo_with_remote(tmp_path, "x", "git@gitlab.com:someone/x.git")
    rep = resolve.compare(repo, c)
    assert rep.expected.id is None and rep.expected.ssh_key is None and rep.problems == ()


SIGN_CONFIG = DEFAULT_CONFIG.replace('gh_account = "me-work"', 'gh_account = "me-work"\nsign = true') + """
[[identity]]
id = "nokey"
name = "No Key"
email = "nokey@corp.test"
host = "gitlab.corp.test"
"""


def test_signing_and_keys(cfg, tmp_path):  # 20
    path = cfg(SIGN_CONFIG)
    c = load(path)
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    rep = resolve.compare(repo, c)
    assert rep.expected.sign is True and rep.problems == ()
    git(repo, "config", "commit.gpgsign", "false")
    assert "sign_mismatch" in [p.code for p in resolve.compare(repo, c).problems]
    git(repo, "config", "--unset", "commit.gpgsign")
    git(repo, "config", "user.signingkey", "/other.pub")
    assert "sign_mismatch" in [p.code for p in resolve.compare(repo, c).problems]
    git(repo, "config", "--unset", "user.signingkey")
    git(repo, "config", "core.sshCommand", "ssh -i /other/key")
    assert [p.code for p in resolve.compare(repo, c).problems] == ["key_mismatch"]

    nokey = repo_with_remote(tmp_path, "nokey", "git@gitlab.corp.test:any/app.git")
    rep = resolve.compare(nokey, c)
    assert rep.expected.id == "nokey" and rep.expected.ssh_key is None
    assert "key_mismatch" not in [p.code for p in rep.problems]


@pytest.mark.parametrize("default_sign", [False, True])
def test_later_identity_can_explicitly_disable_signing(cfg, tmp_path, default_sign):
    text = SIGN_CONFIG + """
[[identity]]
id = "unsigned"
name = "Unsigned Me"
email = "unsigned@corp.test"
host = "github.com"
owners = ["corp-org"]
sign = false
"""
    if default_sign:
        text = text.replace('email = "me@default.test"',
                            'email = "me@default.test"\nssh_key = "~/.ssh/id_default"\nsign = true')
    c = load(cfg(text))
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    rep = resolve.compare(repo, c)
    assert rep.expected.id == "unsigned" and not rep.expected.sign and rep.ok()
    assert git(repo, "config", "--bool", "commit.gpgsign") == "false"
    assert git(repo, "config", "--bool", "tag.gpgsign") == "false"
    for argv in (("commit", "--allow-empty", "-qm", "unsigned"), ("tag", "-am", "unsigned", "unsigned")):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True, timeout=60)
    assert "gpgsig " not in git(repo, "cat-file", "commit", "HEAD")
    assert "BEGIN SSH SIGNATURE" not in git(repo, "cat-file", "tag", "unsigned")
    assert run("check") == 0


def test_git_dir_in_env_is_ignored(cfg, tmp_path, monkeypatch):  # 21
    path = cfg()
    other = repo_with_remote(tmp_path, "other", "git@github.com:me/x.git")
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    rep = resolve.compare(repo, load(path))
    assert rep.expected.id == "work-github" and rep.effective.remotes["origin"].endswith("corp-org/app.git")


def test_remote_argument_restricts_matching(cfg, tmp_path):
    path = cfg()
    c = load(path)
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    git(repo, "remote", "add", "fork", "git@github.com:me/app.git")
    assert resolve.expected(repo, c).id == "personal"  # later identity wins, as for git
    assert resolve.expected(repo, c, remote="origin").id == "work-github"
    assert resolve.expected(repo, c, remote="git@github.com:corp-org/x.git").id == "work-github"
    assert resolve.expected(repo, c, remote="fork").match.remote == "fork"




def test_global_override_and_not_applied(cfg, home, tmp_path):
    path = cfg()
    c = load(path)
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    gc = home / ".gitconfig"
    gc.write_text(gc.read_text() + '[user]\n\temail = "late@global.test"\n')
    assert "global_override" in [p.code for p in resolve.compare(repo, c).problems]
    gc.write_text("")
    assert "not_applied" in [p.code for p in resolve.compare(repo, c).problems]


def test_own_emails(cfg):
    c = load(cfg())
    assert resolve.own_emails(c) == {"me@default.test", "me@corp.test", "me@personal.test"}


def test_is_repo(tmp_path, home):
    repo = repo_with_remote(tmp_path, "app", None, commits=1)
    (repo / "sub").mkdir()
    assert resolve.is_repo(repo / "sub") == repo
    assert resolve.is_repo(tmp_path / "nope") is None
    bare = tmp_path / "b.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=60)
    assert resolve.is_repo(bare) == bare
    assert commit(repo, "x", author=("A", "a@x.test"))


def test_ownerless_url_patterns():  # V-001: "host:**" does not cross "/" in git, so scp uses */** and *
    whole_host = Identity(id="w", name="n", email="e@x.test", host="git.corp.test")
    assert render.url_patterns(whole_host) == [
        "git@git.corp.test:*", "git@git.corp.test:*/**", "ssh://git@git.corp.test/**", "https://git.corp.test/**",
    ]


# ---------------------------------------------------------------- QA01 follow-ups

def test_url_form_https_port(cfg, tmp_path):  # QA01-7
    c = load(cfg())
    repo = repo_with_remote(tmp_path, "app", "https://github.com:8443/corp-org/app.git")
    p = [p for p in resolve.compare(repo, c).problems if p.code == "url_form"][0]
    assert p.fix_argv[-1] == "https://github.com/corp-org/app.git" and "(no port in the URL)" in p.message


def test_home_argument_decides_block_check(cfg, tmp_path):  # QA01-7: --home
    c = load(cfg())
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    assert resolve.compare(repo, c).problems == ()
    other = tmp_path / "other-home"
    other.mkdir()
    assert [p.code for p in resolve.compare(repo, c, home=other).problems] == ["not_applied"]


def test_report_repo_is_work_tree_inside_git_dir(cfg, tmp_path):  # QA01-7
    c = load(cfg())
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git", commits=1)
    git(repo, "config", "user.email", "old@personal.test")
    for where in (repo / ".git", repo / ".git" / "hooks"):
        rep = resolve.compare(where, c)
        assert rep.repo == repo.resolve()
        assert [p.code for p in rep.problems] == ["override"]
        assert rep.effective.email.origin == f"file:{repo.resolve()}/.git/config"
    git(repo, "worktree", "add", "-q", str(tmp_path / "wt"))
    rep = resolve.compare(repo / ".git" / "worktrees" / "wt", c)
    assert rep.repo == (tmp_path / "wt").resolve()


def test_identity_git_extras_shape_expected(cfg, tmp_path):  # QA01-2
    text = DEFAULT_CONFIG.replace('gh_account = "me-work"\n',
                                  'gh_account = "me-work"\n\n[identity.git.core]\n'
                                  'sshCommand = "ssh -i ~/.ssh/id_other -o IdentitiesOnly=yes"\n')
    c = load(cfg(text))
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    rep = resolve.compare(repo, c)
    assert rep.expected.ssh_key.endswith("/.ssh/id_other") and rep.problems == ()


def test_mismatch_from_own_global_config_says_so(cfg, home, tmp_path):  # QA01-2
    c = load(cfg())
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    gc = home / ".gitconfig"
    gc.write_text(gc.read_text() + "[commit]\n\tgpgsign = true\n")
    p = [p for p in resolve.compare(repo, c).problems if p.code == "sign_mismatch"][0]
    assert "from your own ~/.gitconfig, outside gitpro's block" in p.message
    assert list(p.fix_argv) == ["git", "config", "--file", str(gc), "--unset", "commit.gpgsign"]
