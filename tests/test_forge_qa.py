"""QA findings for 05 (forge): regressions for bugs found in review, plus hardening checks that pass.

Fake gh/glab only (conftest fake_bin); no network."""
from __future__ import annotations

import io
import json

import pytest
from conftest import git, repo_with_remote, run
from test_forge import BASE, GH, GLAB_CFG, REPOS, gl_repo, jrun, out_err, repo, searches, setup, similar_setup

from gitpro import forge


@pytest.fixture
def gh(fake_bin):
    """test_forge's fake gh (same data-file protocol)."""
    def make(**data):
        data.setdefault("repos", REPOS)
        (fake_bin.path / "gh.json").write_text(json.dumps(data))
        fake_bin.add("gh", GH)
        return fake_bin
    return make


ONE = BASE.replace('gh_account = "me-work"\n', "")
FORK = {"me": [repo("me/libfoo", fork=True)]}
VIEW = {"me/libfoo": {"nameWithOwner": "me/libfoo", "parent": {"nameWithOwner": "neworg/libfoo"}}}


# ---------------------------------------------------------------- bugs (fail before the fix)

def test_qa_forks_redacts_upstream_credentials(home, cfg, gh, capsys):
    """An upstream remote with a token in its userinfo must not reach the fix line or upstream_url."""
    setup(cfg, home)
    gh(repos=FORK, views=VIEW)
    lf = repo_with_remote(home / "src", "libfoo", "git@github.com:me/libfoo.git")
    git(lf, "remote", "add", "upstream", "https://me:ghp_SECRETSECRETSECRET1234@github.com/oldorg/libfoo.git")
    rc, j, err = jrun(capsys, "forge", "forks", "--online", "--identity", "personal")
    assert rc == 0 and j["forks"][0]["status"] == "moved"
    assert "ghp_SECRET" not in json.dumps(j) + err
    assert run("forge", "forks", "--online", "--identity", "personal") == 0
    out, err = out_err(capsys)
    assert "ghp_SECRET" not in out + err


def test_qa_forks_redacts_origin_credentials_in_fix(home, cfg, gh, capsys):
    """No upstream: the suggested `remote add upstream` URL is built from origin and must not carry its token."""
    setup(cfg, home)
    gh(repos=FORK, views=VIEW)
    repo_with_remote(home / "src", "libfoo", "https://x-access-token:sekret123@github.com/me/libfoo.git")
    rc, j, err = jrun(capsys, "forge", "forks", "--online", "--identity", "personal")
    assert j["forks"][0]["status"] == "missing"
    assert "sekret123" not in json.dumps(j) + err
    run("forge", "forks", "--online", "--identity", "personal")
    out, err = out_err(capsys)
    assert "sekret123" not in out + err


def test_qa_config_online_never_exits_2_when_no_account_answers(home, cfg, gh, capsys):
    """Project rule: a config-only [forge] online = true warns and falls back; only an explicit --online exits 2."""
    setup(cfg, home, BASE + "\n[forge]\nonline = true\n")
    gh(no_login=["me-work", "me"])
    assert run("forge", "repos") == 0
    _, err = out_err(capsys)
    assert "⚠ gh has no login for me-work" in err
    assert run("forge", "repos", "--online") == 2


def test_qa_forks_upstream_local_path_gives_no_bogus_fix(home, cfg, gh, capsys):
    """upstream = a local mirror path: no 'points to None/libfoo', no fix that glues the parent onto the path."""
    setup(cfg, home)
    gh(repos=FORK, views=VIEW)
    lf = repo_with_remote(home / "src", "libfoo", "git@github.com:me/libfoo.git")
    git(lf, "remote", "add", "upstream", "/srv/mirror/libfoo")
    rc, j, _ = jrun(capsys, "forge", "forks", "--online", "--identity", "personal")
    fix = j["forks"][0]["fix"] or ""
    assert "/srv/mirror/neworg/libfoo" not in fix
    run("forge", "forks", "--online", "--identity", "personal")
    assert "None/" not in out_err(capsys)[0]


def test_qa_email_rate_limit_is_not_reported_as_missing_scope(home, cfg, gh, capsys):
    setup(cfg, home)
    gh(emails_fail="HTTP 403: API rate limit exceeded for user ID 1.")
    rc, j, err = jrun(capsys, "forge", "email", "--online")
    assert "lacks the user:email scope" not in err
    assert "rate limit" in err


def test_qa_gh_null_output_explains_the_skip(home, cfg, fake_bin, capsys):
    """gh printing `null` exits 2 with 'see the warnings above' but no warning names the account."""
    setup(cfg, home, ONE)
    fake_bin.add("gh", 'if argv[:2] == ["auth", "token"]:\n    print("T"); sys.exit(0)\nprint("null")')
    rc, j, err = jrun(capsys, "forge", "repos", "--online")
    assert rc == 2
    assert any("personal skipped" in w for w in j["warnings"])


# ---------------------------------------------------------------- hardening (pass today)

def test_qa_similar_ctrl_c_at_prompt_sends_nothing(home, cfg, gh, capsys, monkeypatch):
    fb = similar_setup(cfg, home, gh, ONE)

    class Tty(io.StringIO):
        def isatty(self):
            return True

    def interrupt(question):
        raise KeyboardInterrupt
    monkeypatch.setattr("sys.stdin", Tty(""))
    monkeypatch.setattr("gitpro.view.confirm", interrupt)
    assert run("forge", "similar", "--online", "--send-repo-names") == 130
    assert searches(fb) == []
    assert fb.calls("gh") == []          # not even `gh auth token` before the answer


def test_qa_similar_config_consent_still_needs_y_under_json(home, cfg, gh, capsys):
    fb = similar_setup(cfg, home, gh, ONE + "\n[forge]\nonline = true\nsimilar = true\n")
    rc, j, _ = jrun(capsys, "forge", "similar")
    assert rc == 2 and searches(fb) == []


def test_qa_similar_odd_names_after_double_dash(home, cfg, gh, capsys, monkeypatch):
    monkeypatch.setattr(forge, "SEARCH_PAUSE", 0)
    setup(cfg, home, ONE)
    fb = gh(search={})
    names = ["-rf", "--help", "my app", "ünïcødé", "x" * 200, "$(touch pwned)"]
    for n in names:
        repo_with_remote(home / "src", n, None)
    rc, j, _ = jrun(capsys, "forge", "similar", "--online", "--send-repo-names", "-y")
    assert rc == 0 and sorted(j["sent"]) == sorted(names)
    assert all(c[-2] == "--" for c in searches(fb)) and len(searches(fb)) == len(names)
    assert not (home / "pwned").exists()


def test_qa_similar_other_hosts_and_local_paths_not_sent(home, cfg, gh, capsys, monkeypatch):
    monkeypatch.setattr(forge, "SEARCH_PAUSE", 0)
    setup(cfg, home, ONE)
    gh(search={})
    repo_with_remote(home / "src", "internal", "git@corp-gitlab:team/internal.git")
    repo_with_remote(home / "src", "mirror", "/srv/git/mirror.git")
    repo_with_remote(home / "src", "fileurl", "file:///srv/git/fileurl.git")
    repo_with_remote(home / "src", "pub", "https://GitHub.com/me/Pub.git")
    rc, j, _ = jrun(capsys, "forge", "similar", "--online", "--send-repo-names", "-y")
    assert j["sent"] == ["Pub"] and j["not_sent"] == 3


def test_qa_echoed_token_redacted(home, cfg, fake_bin, capsys):
    setup(cfg, home)
    tok = "gho_" + "A" * 36
    fake_bin.add("gh", f"TOK = {tok!r}\n" + r'''
if argv[:2] == ["auth", "token"]:
    print(TOK); sys.exit(0)
print("error: GH_TOKEN=%s args=%r" % (env.get("GH_TOKEN"), argv), file=sys.stderr)
sys.exit(1)
''')
    for sub in ("repos", "forks", "email"):
        rc, j, err = jrun(capsys, "forge", sub, "--online")
        assert rc == 2 and tok not in err + json.dumps(j)


def test_qa_glab_exactly_one_full_page(home, cfg, gh, fake_bin, capsys):
    setup(cfg, home, GLAB_CFG.format(login="jane.doe"))
    page = json.dumps([gl_repo(n) for n in range(100)])
    fake_bin.add("glab", r'''
if argv[:2] == ["auth", "status"]:
    print("  ✓ Logged in to gitlab.com as jane.doe (x)", file=sys.stderr); sys.exit(0)
page = int(argv[argv.index("--page") + 1])
print(PAGE if page == 1 else "[]")
'''.replace("PAGE", repr(page)))
    rc, j, _ = jrun(capsys, "forge", "repos", "--online")
    assert rc == 0 and len(j["identities"][0]["repos"]) == 100
    assert len([c for c in fake_bin.calls("glab") if c[:2] == ["repo", "list"]]) == 2
