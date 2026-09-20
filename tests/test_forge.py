"""forge: a read-only view of the user's repos through gh/glab, plus the opt-in similar search (spec 05).

Fake gh/glab only (conftest fake_bin); no network. The fakes read their data from JSON files next to them."""
from __future__ import annotations

import io
import json
import shutil

import pytest
from conftest import repo_with_remote, run

from gitpro import forge

BASE = """\
[default]
name = "Default Me"
email = "me@default.test"

[scan]
roots = ["~/src"]

[[identity]]
id = "work-github"
name = "Work Me"
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
gh_account = "me-work"

[[identity]]
id = "personal"
name = "Personal Me"
email = "me@personal.test"
host = "github.com"
owners = ["me"]
gh_account = "me"
"""

GH = r'''
import json, os
here = os.path.dirname(os.path.abspath(sys.argv[0]))
data = json.load(open(os.path.join(here, "gh.json")))
tok = env.get("GH_TOKEN") or env.get("GH_ENTERPRISE_TOKEN") or ""
login = tok[len("FAKE_TOKEN_"):] if tok.startswith("FAKE_TOKEN_") else ""
if argv[:2] == ["auth", "token"]:
    u = argv[argv.index("-u") + 1]
    if u in data.get("no_login", []):
        print("no oauth token found for " + u, file=sys.stderr); sys.exit(1)
    print("FAKE_TOKEN_" + u); sys.exit(0)
if argv[:2] == ["repo", "list"]:
    if argv[2] in data.get("list_fail", {}):
        print(data["list_fail"][argv[2]], file=sys.stderr); sys.exit(1)
    print(json.dumps(data.get("repos", {}).get(argv[2], []))); sys.exit(0)
if argv[:2] == ["repo", "view"]:
    v = data.get("views", {}).get(argv[2])
    if v is None:
        print("GraphQL: Could not resolve to a Repository with the name '%s'." % argv[2], file=sys.stderr)
        sys.exit(1)
    print(json.dumps(v)); sys.exit(0)
if argv[0] == "api" and "user/emails" in argv:
    if "emails_fail" in data:
        print(data["emails_fail"], file=sys.stderr); sys.exit(1)
    for e in data.get("emails", {}).get(login, []):
        print(e)
    sys.exit(0)
if argv[0] == "api" and "user/orgs" in argv:
    for o in data.get("orgs", {}).get(login, []):
        print(o)
    sys.exit(0)
if argv[:2] == ["search", "repos"]:
    count = os.path.join(here, "search.count")
    n = int(open(count).read()) + 1 if os.path.exists(count) else 1
    open(count, "w").write(str(n))
    if data.get("search_fail_at") == n:
        print("HTTP 403: API rate limit exceeded for user ID 1.", file=sys.stderr); sys.exit(1)
    print(json.dumps(data.get("search", {}).get(argv[-1], []))); sys.exit(0)
print("unexpected gh call", file=sys.stderr)
sys.exit(3)
'''

GLAB = r'''
import json, os
here = os.path.dirname(os.path.abspath(sys.argv[0]))
data = json.load(open(os.path.join(here, "glab.json")))
if argv[:2] == ["auth", "status"]:
    print("gitlab.com\n  ✓ Logged in to gitlab.com as %s (/home/x/.config/glab-cli/config.yml)" % data["login"],
          file=sys.stderr)
    sys.exit(0)
if argv[:2] == ["repo", "list"]:
    page = int(argv[argv.index("--page") + 1])
    print(json.dumps(data.get("pages", {}).get(str(page), []))); sys.exit(0)
if argv[0] == "api" and "user/emails" in argv:
    print(json.dumps(data.get("emails", []))); sys.exit(0)
if argv[0] == "api" and "user" in argv:
    print(json.dumps({"username": data["login"], "email": data.get("email")})); sys.exit(0)
print("unexpected glab call", file=sys.stderr)
sys.exit(3)
'''


def repo(nwo, *, fork=False, archived=False, pushed="2026-09-20T10:11:12Z", host="github.com"):
    return {"nameWithOwner": nwo, "isFork": fork, "isArchived": archived, "sshUrl": f"git@{host}:{nwo}.git",
            "pushedAt": pushed}


REPOS = {"me-work": [repo("corp-org/infra"), repo("corp-org/old-site", archived=True, pushed="2023-01-05T00:00:00Z")],
         "me": [repo("me/dotfiles")]}


@pytest.fixture
def gh(fake_bin):
    """gh(**data): install the fake gh with this data (repos, views, emails, orgs, search, ...)."""
    def make(**data):
        data.setdefault("repos", REPOS)
        (fake_bin.path / "gh.json").write_text(json.dumps(data))
        fake_bin.add("gh", GH)
        return fake_bin
    return make


@pytest.fixture
def glab(fake_bin):
    def make(**data):
        (fake_bin.path / "glab.json").write_text(json.dumps(data))
        fake_bin.add("glab", GLAB)
        return fake_bin
    return make


def setup(cfg, home, text=BASE):
    (home / "src").mkdir(exist_ok=True)
    return cfg(text, apply=False)


def no_cli(monkeypatch, *names):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: None if n in names else real(n, *a, **k))


def out_err(capsys):
    c = capsys.readouterr()
    return c.out, c.err


def jrun(capsys, *argv):
    rc = run(*argv, "--json")
    out, err = out_err(capsys)
    return rc, json.loads(out), err


def gh_calls(fb):
    return [" ".join(c) for c in fb.calls("gh")]


def no_token_anywhere(fb, *texts):
    assert all("FAKE_TOKEN" not in t for t in texts)
    assert all("FAKE_TOKEN" not in " ".join(c) for c in fb.calls("gh"))


READ_ONLY_GH = (("auth", "token"), ("repo", "list"), ("repo", "view"), ("search", "repos"))


def assert_read_only(fb):
    for c in fb.calls("gh"):
        if c[0] == "api":
            assert c[1:3] == ["--method", "GET"], c
        else:
            assert tuple(c[:2]) in READ_ONLY_GH, c
    for c in fb.calls("glab"):
        assert tuple(c[:2]) in (("auth", "status"), ("repo", "list")) or c[:3] == ["api", "--method", "GET"], c


# ---------------------------------------------------------------- 1-5 repos

def test_repos_needs_online(home, cfg, gh, capsys):  # 1
    setup(cfg, home)
    fb = gh()
    assert run("forge", "repos") == 2
    out, err = out_err(capsys)
    assert "✗ forge repos needs the network" in err and "github.com via gh" in err and "me-work" in err
    assert "→ re-run with --online, or set [forge] online = true" in err
    assert fb.calls("gh") == []
    rc, j, _ = jrun(capsys, "forge", "repos")
    assert rc == 2 and j["ok"] is False and "--online" in j["error"]
    assert fb.calls("gh") == []


def test_repos_online(home, cfg, gh, capsys):  # 2
    setup(cfg, home)
    fb = gh()
    infra = repo_with_remote(home / "src", "infra", "git@github.com:corp-org/infra.git")
    assert run("forge", "repos", "--online") == 0
    out, err = out_err(capsys)
    lines = out.splitlines()
    assert lines[0].startswith("online: asking github.com via gh (account me-work, me) for the repo lists")
    assert "sends nothing else" in lines[0]
    work = next(ln for ln in lines if ln.startswith("work-github"))
    assert "me-work@github.com" in work and "2 repos · 1 cloned · 1 not cloned · 1 archived" in work
    personal = next(ln for ln in lines if ln.startswith("personal"))
    assert "1 repo · 0 cloned · 1 not cloned" in personal
    assert "→ gitpro clone git@github.com:me/dotfiles.git" in out
    old = next(ln for ln in lines if "corp-org/old-site" in ln)
    assert "pushed 2023-01-05" in old and "archived" in old and "→" not in old
    assert "corp-org/infra" not in out.split("work-github", 1)[1].split("personal")[0].split("not cloned:")[1]
    calls = gh_calls(fb)
    assert any(c.startswith("repo list me-work --limit 1000 --json") for c in calls)
    assert any(c.startswith("repo list me --limit 1000") for c in calls)
    assert all(e["GH_TOKEN"] == "set" for e in fb.entries("gh") if e["argv"][:2] != ["auth", "token"])
    no_token_anywhere(fb, out, err)
    assert_read_only(fb)

    rc, j, err = jrun(capsys, "forge", "repos", "--online")
    assert rc == 0 and j["ok"] and j["command"] == "forge repos" and j["online"] is True
    assert j["sent"] == {"github.com": ["repo list me-work", "repo list me"]}
    ids = {i["id"]: i for i in j["identities"]}
    assert ids["work-github"]["account"] == "me-work" and ids["work-github"]["cli"] == "gh"
    rows = {r["name_with_owner"]: r for r in ids["work-github"]["repos"]}
    assert rows["corp-org/infra"]["cloned"] == str(infra)
    assert rows["corp-org/old-site"]["cloned"] is None and rows["corp-org/old-site"]["archived"] is True
    assert ids["personal"]["repos"][0]["clone_cmd"] == "gitpro clone git@github.com:me/dotfiles.git"
    assert err.startswith("online: asking")
    no_token_anywhere(fb, json.dumps(j), err)


def test_not_cloned_and_alias_origin(home, cfg, gh, capsys):
    text = BASE.replace('gh_account = "me-work"', 'gh_account = "me-work"\nssh_alias = "github-work"\nssh_key = "~/.ssh/id_work"')
    setup(cfg, home, text)
    gh()
    repo_with_remote(home / "src", "infra", "git@github-work:corp-org/infra.git")   # cloned via the alias
    rc, j, _ = jrun(capsys, "forge", "repos", "--online", "--not-cloned", "--identity", "work-github")
    assert rc == 0 and [i["id"] for i in j["identities"]] == ["work-github"]
    assert [r["name_with_owner"] for r in j["identities"][0]["repos"]] == ["corp-org/old-site"]
    assert run("forge", "repos", "--online", "--not-cloned") == 0
    out, _ = out_err(capsys)
    assert "corp-org/old-site" in out and "corp-org/infra" not in out and "me/dotfiles" in out


def test_scan_excludes_respected(home, cfg, gh, capsys):
    setup(cfg, home, BASE.replace('roots = ["~/src"]', 'roots = ["~/src"]\nexclude = ["~/src/old"]'))
    gh()
    (home / "src/old").mkdir(parents=True)
    repo_with_remote(home / "src/old", "infra", "git@github.com:corp-org/infra.git")
    rc, j, _ = jrun(capsys, "forge", "repos", "--online", "--identity", "work-github")
    rows = {r["name_with_owner"]: r for r in j["identities"][0]["repos"]}
    assert rows["corp-org/infra"]["cloned"] is None


def test_enterprise_token_var(home, cfg, gh, capsys):  # 3
    text = BASE + ('\n[[identity]]\nid = "ghe"\nname = "E"\nemail = "e@corp.test"\nhost = "ghe.corp.test"\n'
                   'owners = ["ent"]\ngh_account = "ent-me"\n')
    setup(cfg, home, text)
    fb = gh(repos={"ent-me": [repo("ent/tool", host="ghe.corp.test")]})
    assert run("forge", "repos", "--online", "--identity", "ghe") == 0
    out, err = out_err(capsys)
    assert "ent/tool" in out
    listed = [e for e in fb.entries("gh") if e["argv"][:2] == ["repo", "list"]]
    assert listed and all(e["GH_ENTERPRISE_TOKEN"] == "set" and e["GH_TOKEN"] == "unset" for e in listed)
    no_token_anywhere(fb, out, err)


def test_config_online(home, cfg, gh, capsys):  # 4
    setup(cfg, home, BASE + "\n[forge]\nonline = true\n")
    fb = gh()
    assert run("forge", "repos") == 0
    out, _ = out_err(capsys)
    assert out.startswith("online: asking github.com via gh")
    assert any(c[:2] == ["repo", "list"] for c in fb.calls("gh"))


def test_orgs(home, cfg, gh, capsys):  # 5
    setup(cfg, home)
    fb = gh(orgs={"me-work": ["corp-org", "side-org"]},
            repos={**REPOS, "side-org": [repo("side-org/thing")], "corp-org": [repo("corp-org/infra")]})
    rc, j, err = jrun(capsys, "forge", "repos", "--online", "--orgs", "--identity", "work-github")
    assert rc == 0
    calls = gh_calls(fb)
    i_orgs = next(n for n, c in enumerate(calls) if "user/orgs" in c)
    assert calls[i_orgs].startswith("api --method GET user/orgs --paginate")
    assert any(c.startswith("repo list side-org ") for c in calls[i_orgs:])
    names = [r["name_with_owner"] for r in j["identities"][0]["repos"]]
    assert names.count("corp-org/infra") == 1 and "side-org/thing" in names
    assert "user/orgs" in j["sent"]["github.com"] and "repo list side-org" in j["sent"]["github.com"]
    assert "repo lists and organisations" in err.splitlines()[0]
    assert_read_only(fb)


def test_list_error_is_redacted_and_skips(home, cfg, gh, capsys):
    setup(cfg, home)
    gh(list_fail={"me-work": "HTTP 401: Bad credentials ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 "
                             "https://me-work:sekret@github.com/x glpat-abcdefghijklmnopqrst"})
    assert run("forge", "repos", "--online") == 0       # the other account still answered
    out, err = out_err(capsys)
    assert "⚠ github.com: HTTP 401: Bad credentials" in err and "work-github skipped" in err
    assert "ghp_ABCDEF" not in err and "sekret" not in err and "glpat-abc" not in err
    assert "me/dotfiles" in out


def test_not_logged_in(home, cfg, gh, capsys):
    setup(cfg, home)
    gh(no_login=["me-work"])
    assert run("forge", "repos", "--online", "--identity", "work-github") == 2
    _, err = out_err(capsys)
    assert "gh has no login for me-work on github.com; run: gh auth login -h github.com" in err
    assert run("forge", "repos", "--online") == 0       # another account worked: warn and go on
    out, err = out_err(capsys)
    assert "⚠ gh has no login for me-work" in err and "me/dotfiles" in out


def test_identity_without_account(home, cfg, gh, capsys):
    setup(cfg, home, BASE.replace('gh_account = "me"\n', ""))
    gh()
    assert run("forge", "repos", "--online") == 0
    _, err = out_err(capsys)
    assert "⚠ identity personal has no gh_account/glab_account; skipped" in err
    assert run("forge", "repos", "--online", "--identity", "nope") == 2
    assert "unknown identity" in out_err(capsys)[1]


# ---------------------------------------------------------------- 6-7 forks

FORKS = {"me": [repo("me/libfoo", fork=True), repo("me/oldtool", fork=True), repo("me/requests", fork=True),
                repo("me/solo", fork=True), repo("me/own")]}
VIEWS = {"me/libfoo": {"nameWithOwner": "me/libfoo", "parent": {"name": "libfoo", "owner": {"login": "neworg"}}},
         "me/oldtool": {"nameWithOwner": "me/oldtool", "parent": None},
         "me/requests": {"nameWithOwner": "me/requests", "parent": {"nameWithOwner": "psf/requests"}},
         "me/solo": {"nameWithOwner": "me/solo", "parent": {"name": "solo", "owner": {"login": "up"}}}}


def test_forks(home, cfg, gh, capsys):  # 6, 7
    setup(cfg, home)
    fb = gh(repos=FORKS, views=VIEWS)
    src = home / "src"
    libfoo = repo_with_remote(src, "libfoo", "git@github.com:me/libfoo.git")
    from conftest import git
    git(libfoo, "remote", "add", "upstream", "git@github.com:oldorg/libfoo.git")
    old = repo_with_remote(src, "oldtool", "git@github.com:me/oldtool.git")
    req = repo_with_remote(src, "requests", "git@github.com:me/requests.git")
    git(req, "remote", "add", "upstream", "https://github.com/psf/requests.git")
    solo = repo_with_remote(src, "solo", "git@github.com:me/solo.git")
    rc, j, _ = jrun(capsys, "forge", "forks", "--online", "--identity", "personal")
    assert rc == 0 and j["command"] == "forge forks"
    forks = {f["name_with_owner"]: f for f in j["forks"]}
    assert set(forks) == {"me/libfoo", "me/oldtool", "me/requests", "me/solo"}
    lf = forks["me/libfoo"]
    assert lf["status"] == "moved" and lf["parent"] == "neworg/libfoo" and lf["local"] == str(libfoo)
    assert lf["upstream_url"] == "git@github.com:oldorg/libfoo.git"
    assert lf["fix"] == f"git -C {libfoo} remote set-url upstream git@github.com:neworg/libfoo.git"
    assert forks["me/oldtool"]["status"] == "parent_deleted" and forks["me/oldtool"]["parent"] is None
    assert forks["me/oldtool"]["local"] == str(old)
    assert forks["me/requests"]["status"] == "ok"
    assert forks["me/solo"]["status"] == "missing"
    assert forks["me/solo"]["fix"] == f"git -C {solo} remote add upstream git@github.com:up/solo.git"
    assert not any(c[:3] == ["repo", "view", "me/own"] for c in fb.calls("gh"))
    assert_read_only(fb)

    assert run("forge", "forks", "--online", "--identity", "personal") == 0
    out, _ = out_err(capsys)
    assert "forks of me (github.com via gh)" in out
    assert "parent (deleted)" in out and "upstream is gone" in out
    assert "upstream remote points to oldorg/libfoo" in out
    assert "→ git -C ~/src/libfoo remote set-url upstream git@github.com:neworg/libfoo.git" in out
    assert "upstream ok" in out


# ---------------------------------------------------------------- 8-9 email

def test_email(home, cfg, gh, capsys):  # 8
    setup(cfg, home)
    fb = gh(emails={"me-work": ["me@corp.test", "Other@corp.test"], "me": ["someone@else.test"]})
    assert run("forge", "email", "--online") == 1
    out, err = out_err(capsys)
    work = next(ln for ln in out.splitlines() if ln.startswith("work-github"))
    assert "me@corp.test" in work and "verified on github.com (me-work)" in work and "✓" in work
    personal = next(ln for ln in out.splitlines() if ln.startswith("personal"))
    assert "✗" in personal and "not a verified email of github.com account me" in personal
    assert "https://github.com/settings/emails" in personal
    assert any(c[:4] == ["api", "--method", "GET", "user/emails"] for c in fb.calls("gh"))
    rc, j, _ = jrun(capsys, "forge", "email", "--online")
    assert rc == 1 and j["ok"] is False
    by = {e["id"]: e for e in j["emails"]}
    assert by["work-github"]["verified"] is True and by["personal"]["verified"] is False
    assert by["work-github"]["account"] == "me-work" and by["personal"]["fix"]
    no_token_anywhere(fb, out, err, json.dumps(j))
    assert_read_only(fb)


def test_email_scope_missing(home, cfg, gh, capsys):  # 9
    setup(cfg, home)
    gh(emails_fail="gh: This API operation needs the \"user:email\" scope. (HTTP 403)")
    rc, j, err = jrun(capsys, "forge", "email", "--online")
    assert rc == 0
    assert "gh token for me-work lacks the user:email scope; run: gh auth refresh -h github.com -s user:email" in err
    assert all(e["verified"] is None for e in j["emails"])
    assert any("user:email" in w for w in j["warnings"])


# ---------------------------------------------------------------- 10-11 similar

SEARCH = {"app": [{"nameWithOwner": "me/app", "isFork": False}, {"nameWithOwner": "other/app", "isFork": True}],
          "parser": [{"nameWithOwner": "someone/parser", "isFork": False}]}


def similar_setup(cfg, home, gh, text=BASE, **data):
    setup(cfg, home, text)
    fb = gh(search=SEARCH, **data)
    src = home / "src"
    repo_with_remote(src, "app", "git@github.com:me/app.git")
    repo_with_remote(src, "parser", None)
    repo_with_remote(src, "tool", "git@gitlab.com:grp/tool.git")
    return fb


def searches(fb):
    return [c for c in fb.calls("gh") if c[:2] == ["search", "repos"]]


def test_similar_gate(home, cfg, gh, capsys, monkeypatch):  # 10
    monkeypatch.setattr(forge, "SEARCH_PAUSE", 0)
    fb = similar_setup(cfg, home, gh, BASE.replace('gh_account = "me-work"\n', ""))
    assert run("forge", "similar", "--online") == 2
    _, err = out_err(capsys)
    assert "✗ forge similar sends your local repo names (2 names from ~/src) to github.com via gh" in err
    assert "→ re-run with --online --send-repo-names to allow that" in err
    assert run("forge", "similar", "--send-repo-names") == 2
    assert run("forge", "similar", "--online", "--send-repo-names") == 2     # no terminal, no -y
    out, err = out_err(capsys)
    assert "no terminal to confirm on" in err
    rc, j, _ = jrun(capsys, "forge", "similar", "--online", "--send-repo-names")
    assert rc == 2 and "-y" in j["error"]
    assert searches(fb) == []

    rc, j, err = jrun(capsys, "forge", "similar", "--online", "--send-repo-names", "-y")
    assert rc == 0 and j["command"] == "forge similar"
    assert j["sent"] == ["app", "parser"] and j["not_sent"] == 1
    first = err.splitlines()[0]
    assert first.startswith("online: asking github.com via gh (account me)") and "app, parser" in first
    assert "tool" not in err
    sent = [c[-1] for c in searches(fb)]
    assert sent == ["app", "parser"]
    m = {x["name"]: x for x in j["matches"]}
    res = {r["name_with_owner"]: r for r in m["app"]["results"]}
    assert res["me/app"]["yours"] is True and res["me/app"]["cloned"] == str(home / "src/app")
    assert res["other/app"]["yours"] is False and res["other/app"]["cloned"] is None
    assert_read_only(fb)


def test_similar_name_filter_and_config(home, cfg, gh, capsys, monkeypatch):  # 10
    monkeypatch.setattr(forge, "SEARCH_PAUSE", 0)
    text = BASE.replace('gh_account = "me-work"\n', "") + "\n[forge]\nsimilar = true\n"
    fb = similar_setup(cfg, home, gh, text)
    assert run("forge", "similar", "--online", "-y", "--name", "par*") == 0
    out, _ = out_err(capsys)
    assert out.splitlines()[0].startswith("online: asking github.com via gh (account me)")
    assert [c[-1] for c in searches(fb)] == ["parser"]
    assert "someone/parser" in out and "not yours" in out


def test_similar_tty_confirm(home, cfg, gh, capsys, monkeypatch):
    monkeypatch.setattr(forge, "SEARCH_PAUSE", 0)
    fb = similar_setup(cfg, home, gh, BASE.replace('gh_account = "me-work"\n', ""))

    class Tty(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr("sys.stdin", Tty("n\n"))
    monkeypatch.setattr("gitpro.view.confirm", lambda q: False)
    assert run("forge", "similar", "--online", "--send-repo-names") == 1
    assert searches(fb) == []
    assert "nothing sent" in out_err(capsys)[1]


def test_similar_rate_limited(home, cfg, gh, capsys, monkeypatch):  # 11
    monkeypatch.setattr(forge, "SEARCH_PAUSE", 0)
    fb = similar_setup(cfg, home, gh, BASE.replace('gh_account = "me-work"\n', ""), search_fail_at=2)
    rc, j, err = jrun(capsys, "forge", "similar", "--online", "--send-repo-names", "-y")
    assert rc == 0
    assert "⚠ github.com: rate limited after 1 names; the rest were not sent" in err
    assert j["sent"] == ["app"] and j["failed"] == ["parser"]
    assert len(searches(fb)) == 2


# ---------------------------------------------------------------- 12 missing CLI, config-only online

def test_gh_missing(home, cfg, gh, capsys, monkeypatch):  # 12
    setup(cfg, home)
    fb = gh()
    no_cli(monkeypatch, "gh", "glab")
    assert run("forge", "repos", "--online") == 2
    _, err = out_err(capsys)
    assert "✗ forge repos needs gh or glab on PATH" in err
    assert fb.calls("gh") == []


def test_config_online_never_hard_fails(home, cfg, gh, capsys, monkeypatch):
    setup(cfg, home, BASE + "\n[forge]\nonline = true\nsimilar = true\n")
    gh()
    no_cli(monkeypatch, "gh", "glab")
    for sub in ("repos", "forks", "email", "similar"):
        assert run("forge", sub) == 0, sub
        out, err = out_err(capsys)
        assert "⚠ [forge] online = true, but forge " + sub in err and "needs gh or glab on PATH" in err
        assert not out.startswith("online:")
    rc, j, _ = jrun(capsys, "forge", "repos")
    assert rc == 0 and j["online"] is False and j["warnings"]


# ---------------------------------------------------------------- 13 repo_exists (01's, unchanged)

def test_repo_exists_states(gh):  # 13
    gh(views={"me/new": {"nameWithOwner": "me/new"}, "me/old": {"nameWithOwner": "me/new"}})
    acc = forge.Account("github.com", "gh", "me")
    assert forge.repo_exists(acc, "me/gone")[0] == "missing"
    assert forge.repo_exists(acc, "me/old") == ("renamed", "me/new")
    assert forge.repo_exists(acc, "me", "new") == ("ok", None)


def test_repo_exists_unknown(fake_bin):  # 13
    fake_bin.add("gh", 'if argv[:2] == ["auth", "token"]:\n    print("T"); sys.exit(0)\n'
                       'print("error connecting to api.github.com", file=sys.stderr); sys.exit(1)')
    assert forge.repo_exists(forge.Account("github.com", "gh", "me"), "me/x") == (
        "unknown", "error connecting to api.github.com")


# ---------------------------------------------------------------- 14 glab

GLAB_CFG = """\
[default]
name = "Default Me"
email = "me@default.test"

[scan]
roots = ["~/src"]

[[identity]]
id = "gitlab-work"
name = "Jane"
email = "jane@corp.test"
host = "gitlab.com"
owners = ["grp"]
glab_account = "{login}"
"""


def gl_repo(n):
    return {"path_with_namespace": f"grp/r{n}", "ssh_url_to_repo": f"git@gitlab.com:grp/r{n}.git",
            "archived": False, "last_activity_at": "2026-05-11T00:00:00Z"}


def test_glab_login_mismatch(home, cfg, gh, glab, capsys):  # 14
    setup(cfg, home, GLAB_CFG.format(login="other"))
    gh()
    fb = glab(login="jane.doe", pages={"1": [gl_repo(1)]})
    rc = run("forge", "repos", "--online")
    out, err = out_err(capsys)
    assert "⚠ glab is logged in to gitlab.com as jane.doe, identity gitlab-work expects other" in err
    assert "glab auth login --hostname gitlab.com" in err
    assert not any(c[:2] == ["repo", "list"] for c in fb.calls("glab"))
    assert "grp/r1" not in out and rc == 2
    assert run("forge", "email", "--online") == 2
    assert not any(c[0] == "api" for c in fb.calls("glab"))


def test_glab_pages(home, cfg, gh, glab, capsys):  # 14
    setup(cfg, home, GLAB_CFG.format(login="jane.doe"))
    gh()
    pages = {"1": [gl_repo(n) for n in range(100)], "2": [gl_repo(n) for n in range(100, 103)]}
    fb = glab(login="jane.doe", pages=pages)
    repo_with_remote(home / "src", "r5", "git@gitlab.com:grp/r5.git")
    rc, j, err = jrun(capsys, "forge", "repos", "--online")
    assert rc == 0 and "gitlab.com via glab (account jane.doe)" in err.splitlines()[0]
    repos = j["identities"][0]["repos"]
    assert len(repos) == 103 and j["identities"][0]["cli"] == "glab"
    assert sum(r["cloned"] is not None for r in repos) == 1
    lists = [c for c in fb.calls("glab") if c[:2] == ["repo", "list"]]
    assert [c[c.index("--page") + 1] for c in lists] == ["1", "2"]
    assert all("--mine" in c and "--per-page" in c for c in lists)
    assert all(e["GITLAB_TOKEN"] == "unset" for e in fb.entries("glab"))
    assert_read_only(fb)


def test_glab_email(home, cfg, gh, glab, capsys):
    setup(cfg, home, GLAB_CFG.format(login="jane.doe"))
    gh()
    fb = glab(login="jane.doe", email="jane@other.test",
              emails=[{"email": "jane@corp.test", "confirmed_at": "2026-01-01T00:00:00Z"}])
    assert run("forge", "email", "--online") == 0
    out, _ = out_err(capsys)
    assert "verified on gitlab.com (jane.doe)" in out
    assert_read_only(fb)


def test_capabilities_and_help(capsys):
    from gitpro import cli
    from gitpro.args import CAPABILITIES
    cli.build_parser()
    assert {"forge", "forge.similar"} <= CAPABILITIES
    assert run("forge") == 2
    assert "repos" in out_err(capsys)[1]


# ---------------------------------------------------------------- QA follow-ups

ONE = BASE.replace('gh_account = "me-work"\n', "")


def test_gh_env_host_case_insensitive(gh):
    gh()
    env = forge.gh_env("GitHub.COM", "me")
    assert env["GH_TOKEN"] == "FAKE_TOKEN_me" and "GH_ENTERPRISE_TOKEN" not in env
    env = forge.gh_env("ghe.corp.test", "me")
    assert env["GH_ENTERPRISE_TOKEN"] == "FAKE_TOKEN_me" and "GH_TOKEN" not in env


def test_token_fetched_once_per_account(home, cfg, gh, capsys):
    setup(cfg, home)
    views = {f"me/f{n}": {"nameWithOwner": f"me/f{n}", "parent": {"nameWithOwner": f"up/f{n}"}} for n in range(3)}
    fb = gh(repos={"me-work": [], "me": [repo(f"me/f{n}", fork=True) for n in range(3)]}, views=views)
    assert run("forge", "forks", "--online") == 0
    tokens = [c for c in fb.calls("gh") if c[:2] == ["auth", "token"]]
    assert sorted(c[c.index("-u") + 1] for c in tokens) == ["me", "me-work"]
    assert len([c for c in fb.calls("gh") if c[:2] == ["repo", "view"]]) == 3


def test_redact_tokens():
    tok = "s3cr3t-Value-42"
    text = (f"echo {tok} gldt-abcdefgh12 glrt-abcdefgh12 glsoat-ABCDEFGH12 token=" + "a" * 40
            + " Authorization: token " + "b" * 40 + " commit " + "c" * 40)
    got = forge.redact(text, [tok])
    assert tok not in got and "gldt-" not in got and "glrt-" not in got and "glsoat-" not in got
    assert "a" * 40 not in got and "b" * 40 not in got and "c" * 40 in got      # a bare sha stays


def test_repo_list_limit_warning(home, cfg, gh, capsys):
    setup(cfg, home, ONE)
    gh(repos={"me": [repo(f"me/r{n}") for n in range(1000)]})
    rc, j, _ = jrun(capsys, "forge", "repos", "--online")
    assert rc == 0 and any("showing the first 1000 repos; there may be more" in w for w in j["warnings"])


def test_glab_pages_null_and_repeated_page(fake_bin):
    acc = forge.Account("gitlab.com", "glab", "jane")
    fake_bin.add("glab", 'print("null")')
    assert forge.glab_pages(acc, ["repo", "list"]) == ([], None)
    fake_bin.add("glab", 'print("  ")')
    assert forge.glab_pages(acc, ["repo", "list"]) == ([], None)
    page = json.dumps([{"id": n, "path_with_namespace": f"g/r{n}"} for n in range(100)])
    before = len(fake_bin.calls("glab"))
    fake_bin.add("glab", f"print({page!r})")       # a CLI that ignores --page
    items, err = forge.glab_pages(acc, ["repo", "list"])
    assert err is None and len(items) == 100
    assert len(fake_bin.calls("glab")) - before == 2


def test_similar_every_remote_on_host(home, cfg, gh, capsys, monkeypatch):
    monkeypatch.setattr(forge, "SEARCH_PAUSE", 0)
    setup(cfg, home, ONE)
    fb = gh(search={})
    both = repo_with_remote(home / "src", "both", "git@github.com:me/both.git")
    from conftest import git
    git(both, "remote", "add", "mirror", "git@gitlab.com:me/both.git")
    gh_only = repo_with_remote(home / "src", "ghonly", "git@github.com:me/ghonly.git")
    git(gh_only, "remote", "add", "upstream", "https://github.com/up/ghonly.git")
    rc, j, _ = jrun(capsys, "forge", "similar", "--online", "--send-repo-names", "-y")
    assert rc == 0 and j["sent"] == ["ghonly"] and j["not_sent"] == 1
    assert [c[-1] for c in searches(fb)] == ["ghonly"]


def test_similar_ctrl_c_between_searches(home, cfg, gh, capsys, monkeypatch):
    import types

    def interrupt(_s):
        raise KeyboardInterrupt
    monkeypatch.setattr(forge, "time", types.SimpleNamespace(sleep=interrupt))
    fb = similar_setup(cfg, home, gh, ONE)
    assert run("forge", "similar", "--online", "--send-repo-names", "-y") == 130
    out, err = out_err(capsys)
    assert "me/app" in out and "✗ interrupted; sent so far: app" in err
    assert [c[-1] for c in searches(fb)] == ["app"]
    (fb.path / "search.count").unlink()
    rc = run("forge", "similar", "--online", "--send-repo-names", "-y", "--json")
    out, err = out_err(capsys)
    j = json.loads(out)
    assert rc == 130 and j["ok"] is False and j["sent"] == ["app"] and "interrupted" in j["error"]


def test_similar_not_logged_in(home, cfg, gh, capsys, monkeypatch):
    monkeypatch.setattr(forge, "SEARCH_PAUSE", 0)
    similar_setup(cfg, home, gh, ONE, no_login=["me"])
    assert run("forge", "similar", "--online", "--send-repo-names", "-y") == 2
    _, err = out_err(capsys)
    assert "⚠ gh has no login for me" in err and "nothing sent to github.com" in err
    setup(cfg, home, ONE + "\n[forge]\nonline = true\nsimilar = true\n")
    assert run("forge", "similar", "-y") == 0
    assert "⚠ gh has no login for me" in out_err(capsys)[1]


def test_control_characters_escaped(home, cfg, gh, capsys, monkeypatch):
    monkeypatch.setattr(forge, "SEARCH_PAUSE", 0)
    setup(cfg, home, ONE)
    gh(search={"evil\x1b[2Jname": [{"nameWithOwner": "x/evil\x1b[2Jname", "isFork": False}]})
    repo_with_remote(home / "src", "evil\x1b[2Jname", None)
    assert run("forge", "similar", "--online", "--send-repo-names", "-y") == 0
    out, err = out_err(capsys)
    assert "\x1b" not in out + err and "evil\\x1b[2Jname" in out.splitlines()[0]
