"""lost: unpushed branches, gone upstreams, stashes, dirty trees, repos without a remote, duplicates,
stale and empty repos (spec 03 §10). Read-only: nothing here may change a repo."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import commit, repo_with_remote, run, write_config
from gitpro import lost

ME = ("Work Me", "me@corp.test")
IDENT = ["-c", "user.name=T", "-c", "user.email=t@t.test"]


def sh(repo: Path, *args: str) -> str:
    """git -C repo args, failing loudly (the conftest git() does not check)."""
    return subprocess.run(["git", "-C", str(repo), *IDENT, *args], check=True, capture_output=True, text=True,
                          timeout=60).stdout.strip()


def head_branch(repo: Path) -> str:
    return sh(repo, "symbolic-ref", "--short", "HEAD")


def mk(tmp_path: Path, name: str, url=None, commits: int = 1) -> Path:
    return repo_with_remote(tmp_path / "w", name, url, commits=commits)


def add_file(repo: Path, name: str, text: str = "x\n") -> None:
    (repo / name).write_text(text)
    sh(repo, "add", name)
    commit(repo, f"add {name}", author=ME)


def pushed(tmp_path: Path, bare_remote, name: str = "app") -> Path:
    """A repo whose one branch is pushed to a bare origin with -u."""
    bare = bare_remote(f"{name}.git")
    repo = mk(tmp_path, name, str(bare))
    sh(repo, "push", "-q", "-u", "origin", "HEAD")
    return repo


def lost_run(capsys, *argv: str):
    capsys.readouterr()
    rc = run("lost", *argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def lost_json(capsys, *argv: str):
    rc, out, err = lost_run(capsys, *argv, "--json")
    return rc, json.loads(out), err


@pytest.fixture
def w(tmp_path, home):
    d = tmp_path / "w"
    d.mkdir()
    return d


# ---------------------------------------------------------------- 1-3: branches

def test_unpushed_no_upstream(tmp_path, w, bare_remote, capsys):  # 1
    repo = pushed(tmp_path, bare_remote)
    sh(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "a", author=ME)
    commit(repo, "b", author=ME)
    rc, d, _ = lost_json(capsys, str(w))
    assert rc == 1 and d["ok"] is False and d["command"] == "lost"
    assert d["unpushed"] == [{"repo": str(repo), "branch": "feature", "commits": 2, "upstream": None, "ahead": None,
                              "detached": False, "fix": f"git -C {repo} push -u origin feature"}]
    rc, out, _ = lost_run(capsys, str(w))
    assert rc == 1
    assert "unpushed (1 branch in 1 repo)" in out
    assert "feature" in out and "2 commits, no upstream" in out
    assert f"→ git -C {repo} push -u origin feature" in out
    assert "as of last fetch" in out.splitlines()[0]

    sh(repo, "remote", "add", "other", str(bare_remote("other.git")))
    _, d, _ = lost_json(capsys, str(w))
    assert d["unpushed"][0]["fix"] is None


def test_fix_is_shell_quoted(tmp_path, w, bare_remote, capsys):
    bare = bare_remote("q.git")
    repo = mk(tmp_path, "my repo", str(bare))
    sh(repo, "push", "-q", "-u", "origin", "HEAD")
    sh(repo, "checkout", "-q", "-b", "it's")
    commit(repo, "a", author=ME)
    _, d, _ = lost_json(capsys, str(w))
    assert d["unpushed"][0]["fix"] == f"git -C '{repo}' push -u origin 'it'\"'\"'s'"


def test_ahead_then_pushed(tmp_path, w, bare_remote, capsys):  # 2
    repo = pushed(tmp_path, bare_remote)
    sh(repo, "checkout", "-q", "-b", "feature")
    sh(repo, "push", "-q", "-u", "origin", "feature")
    commit(repo, "one more", author=ME)
    rc, d, _ = lost_json(capsys, str(w))
    assert rc == 1
    assert d["unpushed"] == [{"repo": str(repo), "branch": "feature", "commits": 1, "upstream": "origin/feature",
                              "ahead": 1, "detached": False, "fix": None}]
    _, out, _ = lost_run(capsys, str(w))
    assert "1 ahead of origin/feature" in out
    sh(repo, "push", "-q")
    rc, out, _ = lost_run(capsys, str(w))
    assert rc == 0
    assert "✓ nothing lost under" in out
    rc, d, _ = lost_json(capsys, str(w))
    assert rc == 0 and d["ok"] is True and d["unpushed"] == []


def test_merged_gone(tmp_path, w, bare_remote, capsys):  # 3
    repo = pushed(tmp_path, bare_remote)
    main = head_branch(repo)
    sh(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "work", author=ME)
    sh(repo, "push", "-q", "-u", "origin", "feature")
    sh(repo, "checkout", "-q", main)
    sh(repo, "merge", "-q", "--ff-only", "feature")
    sh(repo, "push", "-q", "origin", main)
    sh(repo, "push", "-q", "origin", "--delete", "feature")
    sh(repo, "fetch", "-q", "--prune")
    rc, d, _ = lost_json(capsys, str(w))
    assert d["merged_gone"] == [{"repo": str(repo), "branch": "feature", "commits": 0}]
    assert d["unpushed"] == []
    rc, out, _ = lost_run(capsys, str(w))
    assert "upstream gone (1 branch)" in out and "merged, safe to delete" in out
    for destructive in ("branch -D", "branch -d", "stash drop", "remote remove", "push --delete"):
        assert destructive not in out


def test_gone_with_unpushed_commits(tmp_path, w, bare_remote, capsys):
    repo = pushed(tmp_path, bare_remote)
    sh(repo, "checkout", "-q", "-b", "spike")
    commit(repo, "work", author=ME)
    sh(repo, "push", "-q", "-u", "origin", "spike")
    sh(repo, "push", "-q", "origin", "--delete", "spike")
    sh(repo, "fetch", "-q", "--prune")
    rc, d, _ = lost_json(capsys, str(w))
    assert rc == 1
    assert d["merged_gone"] == [{"repo": str(repo), "branch": "spike", "commits": 1}]
    assert d["unpushed"] == []
    _, out, _ = lost_run(capsys, str(w))
    assert "1 commit not on any remote" in out


# ---------------------------------------------------------------- 4-6: stash, dirty, no remote

def test_stash(tmp_path, w, bare_remote, capsys):  # 4
    repo = pushed(tmp_path, bare_remote)
    add_file(repo, "f.txt")
    sh(repo, "push", "-q")
    (repo / "f.txt").write_text("changed\n")
    sh(repo, "stash", "-q")
    rc, d, _ = lost_json(capsys, str(w))
    assert rc == 1
    [s] = d["stashes"]
    assert s["repo"] == str(repo) and s["ref"] == "stash@{0}" and s["subject"].startswith("WIP on ")
    assert s["date"][4] == "-" and "T" in s["date"]
    assert d["dirty"] == []
    _, out, _ = lost_run(capsys, str(w))
    assert "stashes (1 in 1 repo)" in out and "stash@{0}" in out


def test_dirty(tmp_path, w, bare_remote, capsys):  # 5
    repo = pushed(tmp_path, bare_remote)
    add_file(repo, "f.txt")
    sh(repo, "push", "-q")
    (repo / "f.txt").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    (repo / ".gitignore").write_text("")
    (repo / "ignored.log").write_text("x")  # ignored once .gitignore says so: not counted
    sh(repo, "add", ".gitignore")
    sh(repo, "commit", "-q", "-m", "ignore")
    sh(repo, "push", "-q")
    (repo / ".gitignore").write_text("*.log\n")
    rc, d, _ = lost_json(capsys, str(w))
    assert rc == 1
    assert d["dirty"] == [{"repo": str(repo), "modified": 2, "untracked": 1}]
    _, out, _ = lost_run(capsys, str(w))
    assert "dirty (1 repo)" in out and "2 modified, 1 untracked" in out


def test_no_remote(tmp_path, w, capsys):  # 6
    repo = mk(tmp_path, "parser", None, commits=1)
    rc, d, _ = lost_json(capsys, str(w))
    assert rc == 1
    assert d["no_remote"] == [{"repo": str(repo), "commits": 1}]
    assert d["unpushed"] == []
    _, out, _ = lost_run(capsys, str(w))
    assert "no remote (1 repo)" in out and "1 commit, never pushed anywhere" in out


# ---------------------------------------------------------------- 7: duplicates

def _seeded(tmp_path, bare_remote) -> Path:
    bare = bare_remote("dup.git")
    seed = repo_with_remote(tmp_path / "seed", "s", str(bare), commits=1)
    sh(seed, "push", "-q", "origin", "HEAD")
    return bare


def test_duplicates(tmp_path, w, bare_remote, capsys):  # 7
    bare = _seeded(tmp_path, bare_remote)
    a, b = w / "a", w / "b"
    subprocess.run(["git", "clone", "-q", str(bare), str(a)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(b)], check=True)
    _, d, _ = lost_json(capsys, str(w))
    assert d["duplicates"] == [{"key": str(bare), "repos": [str(a), str(b)], "diverged": False, "detail": None}]

    commit(b, "extra", author=ME)
    _, d, _ = lost_json(capsys, str(w))
    [g] = d["duplicates"]
    assert g["diverged"] is True and g["detail"] == f"{b} has 1 commit not in {a}"
    _, out, _ = lost_run(capsys, str(w))
    assert "duplicates (1 group)" in out and f"diverged: {b} has 1 commit not in {a}" in out

    # B's objects are gone (an empty objects dir keeps it a repo git can open): not comparable.
    (b / ".git/objects").rename(b / ".git/objects.away")
    (b / ".git/objects").mkdir()
    _, d, _ = lost_json(capsys, str(w))
    [g] = d["duplicates"]
    assert g["diverged"] is None and g["detail"] == "HEADs differ; could not compare"


def test_duplicate_keys_normalise(tmp_path, w, cfg, capsys):
    cfg(_ALIAS_CONFIG, apply=False)
    one = mk(tmp_path, "one", "git@github.com:me/App.git", commits=0)
    two = mk(tmp_path, "two", "https://github.com/me/app", commits=0)
    three = mk(tmp_path, "three", "ssh://git@github.com/me/app.git", commits=0)
    four = mk(tmp_path, "four", "git@gh-work:corp-org/tool.git", commits=0)
    five = mk(tmp_path, "five", "https://github.com/corp-org/tool.git", commits=0)
    _, d, _ = lost_json(capsys, str(w))
    keys = {g["key"]: g["repos"] for g in d["duplicates"]}
    assert keys == {"github.com/me/app": sorted([str(one), str(two), str(three)]),
                    "github.com/corp-org/tool": sorted([str(four), str(five)])}


_ALIAS_CONFIG = """\
[default]
name = "Default Me"
email = "me@default.test"

[[identity]]
id = "work"
name = "Work Me"
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
ssh_key = "~/.ssh/id_work"
ssh_alias = "gh-work"
"""


# ---------------------------------------------------------------- 8-10: stale, empty, detached

def test_stale(tmp_path, w, capsys):  # 8
    repo = repo_with_remote(w, "old", None)
    commit(repo, "ancient", author=ME, date="2020-01-01T00:00:00+0000")
    rc, d, _ = lost_json(capsys, str(w), "--only", "stale")
    assert rc == 0  # stale alone is not "lost"
    [s] = d["stale"]
    assert s["repo"] == str(repo) and s["days"] > 365 and s["last_commit"] == "2020-01-01T00:00:00+00:00"
    assert d["no_remote"] == []
    _, out, _ = lost_run(capsys, str(w), "--only", "stale")
    assert "stale (1 repo" in out
    _, out, _ = lost_run(capsys, str(w))
    assert "stale (" not in out and "stale repos: 1 (-v lists them)" in out
    _, d, _ = lost_json(capsys, str(w))
    assert len(d["stale"]) == 1 and len(d["no_remote"]) == 1
    _, out, _ = lost_run(capsys, str(w), "-v")
    assert "stale (1 repo" in out
    _, d, _ = lost_json(capsys, str(w), "--stale-days", "100000")
    assert d["stale"] == []


def test_empty_then_dirty(tmp_path, w, capsys):  # 9
    repo = mk(tmp_path, "blank", None, commits=0)
    rc, d, _ = lost_json(capsys, str(w))
    assert rc == 0 and d["ok"] is True
    assert d["empty"] == [{"repo": str(repo)}]
    assert d["stale"] == [] and d["unpushed"] == [] and d["no_remote"] == []
    rc, out, _ = lost_run(capsys, str(w))
    assert rc == 0 and "✓ nothing lost under" in out and "empty (" not in out
    _, out, _ = lost_run(capsys, str(w), "-v")
    assert "empty (1 repo)" in out
    (repo / "x").touch()
    rc, d, _ = lost_json(capsys, str(w))
    assert rc == 1
    assert d["dirty"] == [{"repo": str(repo), "modified": 0, "untracked": 1}] and d["empty"] == []


def test_detached(tmp_path, w, bare_remote, capsys):  # 10
    repo = pushed(tmp_path, bare_remote)
    sh(repo, "checkout", "-q", "--detach")
    commit(repo, "floating", author=ME)
    rc, d, _ = lost_json(capsys, str(w))
    assert rc == 1
    assert d["unpushed"] == [{"repo": str(repo), "branch": None, "commits": 1, "upstream": None, "ahead": None,
                              "detached": True, "fix": None}]
    _, out, _ = lost_run(capsys, str(w))
    assert "(detached HEAD)" in out


# ---------------------------------------------------------------- 11-12: worktrees, --only

def test_worktree_listed_once(tmp_path, w, bare_remote, capsys):  # 11
    repo = pushed(tmp_path, bare_remote)
    wt = w / "wt"
    sh(repo, "worktree", "add", "-q", str(wt))
    rc, d, _ = lost_json(capsys, str(w))
    assert d["repos"] == 1
    assert d["worktrees"] == {str(repo): [str(wt)]}
    assert rc == 0
    _, out, _ = lost_run(capsys, str(w), "-v")
    assert str(wt) in out
    (wt / "dirt").touch()  # a linked worktree's own tree is checked too
    _, d, _ = lost_json(capsys, str(w))
    assert d["dirty"] == [{"repo": str(wt), "modified": 0, "untracked": 1}]


def test_only_filters(tmp_path, w, bare_remote, capsys):  # 12
    repo = pushed(tmp_path, bare_remote)
    add_file(repo, "f.txt")
    sh(repo, "push", "-q")
    (repo / "f.txt").write_text("changed\n")
    sh(repo, "stash", "-q")
    sh(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "a", author=ME)
    (repo / "loose").touch()
    mk(tmp_path, "solo", None)
    rc, d, _ = lost_json(capsys, str(w), "--only", "unpushed,stash")
    assert rc == 1
    assert len(d["unpushed"]) == 1 and len(d["stashes"]) == 1
    for key in ("merged_gone", "dirty", "no_remote", "remote_gone", "duplicates", "stale", "empty"):
        assert key in d and d[key] == [], key
    rc, out, err = lost_run(capsys, str(w), "--only", "foo")
    assert rc == 2
    assert ("✗ unknown kind 'foo' (unpushed, merged_gone, stash, dirty, no_remote, remote_gone, duplicate, "
            "stale, empty)") in err
    rc, d, _ = lost_json(capsys, str(w), "--only", "foo")
    assert rc == 2 and d["ok"] is False and "unknown kind" in d["error"]


# ---------------------------------------------------------------- 13-15: --online

TOKEN = "tok-SECRET-lost"

GH = f"""
import json
if argv[:2] == ["auth", "token"]:
    print("{TOKEN}"); sys.exit(0)
if argv[:2] == ["repo", "view"]:
    name = argv[2]
    if name == "corp-org/tool":
        print(json.dumps({{"nameWithOwner": "corp-org/tool-cli"}})); sys.exit(0)
    if name == "corp-org/oldlib":
        print("GraphQL: Could not resolve to a Repository with the name 'corp-org/oldlib'.", file=sys.stderr)
        sys.exit(1)
    if name.startswith("broken/"):
        print("HTTP 502: bad gateway (https://x-access-token:ghp_abcDEF123456@api.github.com/graphql) "
              "glpat-zzzYYY111 github_pat_11AAbb", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({{"nameWithOwner": name}})); sys.exit(0)
sys.exit(3)
"""


def test_online_remote_gone(tmp_path, w, cfg, fake_bin, capsys):  # 13
    cfg(apply=False)
    fake_bin.add("gh", GH)
    oldlib = mk(tmp_path, "oldlib", "git@github.com:corp-org/oldlib.git", commits=0)
    tool = mk(tmp_path, "tool", "git@github.com:corp-org/tool.git", commits=0)
    mk(tmp_path, "fine", "git@github.com:corp-org/fine.git", commits=0)
    mine = mk(tmp_path, "mine", "git@github.com:me/mine.git", commits=0)  # identity personal: no gh_account
    rc, out, err = lost_run(capsys, str(w), "--online")
    assert out.splitlines()[0].startswith("online: asking github.com via gh (account me-work) whether 3 repos exist")
    assert "sends owner/repo names only" in out.splitlines()[0]
    assert rc == 1
    assert "remote gone (2 repos)" in out and "missing via gh" in out and "renamed to corp-org/tool-cli" in out
    assert f"→ git -C {tool} remote set-url origin git@github.com:corp-org/tool-cli.git" in out
    # me/mine is never asked as me-work: another identity's login is not borrowed (m6)
    assert "⚠ remote check failed for github.com: identity personal has no gh_account; 1 repo unknown" in err
    calls = fake_bin.calls("gh")
    assert ["repo", "view", "corp-org/tool", "--json", "nameWithOwner"] in calls
    assert not any("me/mine" in c for c in calls)
    entries = fake_bin.entries("gh")
    assert all(e["GH_TOKEN"] == "unset" for e in entries if e["argv"][:2] == ["auth", "token"])
    assert all(e["GH_TOKEN"] == "set" for e in entries if e["argv"][:2] == ["repo", "view"])

    rc, d, err2 = lost_json(capsys, str(w), "--online")
    assert rc == 1 and d["online"] is True
    assert d["remote_gone"] == [
        {"repo": str(mine), "remote": "origin", "url": "git@github.com:me/mine.git", "status": "unknown",
         "renamed_to": None, "detail": "identity personal has no gh_account", "fix": None, "via": None},
        {"repo": str(oldlib), "remote": "origin", "url": "git@github.com:corp-org/oldlib.git", "status": "missing",
         "renamed_to": None, "detail": None, "fix": None, "via": "gh"},
        {"repo": str(tool), "remote": "origin", "url": "git@github.com:corp-org/tool.git", "status": "renamed",
         "renamed_to": "corp-org/tool-cli", "detail": None,
         "fix": f"git -C {tool} remote set-url origin git@github.com:corp-org/tool-cli.git", "via": "gh"}]
    assert err2.startswith("online: ")  # the network notice still shows; stdout stays one JSON object
    for text in (out, err, err2, json.dumps(d), " ".join(" ".join(c) for c in calls)):
        assert TOKEN not in text


def test_online_default_account(tmp_path, w, cfg, fake_bin, capsys):
    from conftest import DEFAULT_CONFIG
    cfg(DEFAULT_CONFIG.replace('email = "me@default.test"\n', 'email = "me@default.test"\ngh_account = "me-dflt"\n',
                               1), apply=False)
    fake_bin.add("gh", GH)
    mk(tmp_path, "mine", "git@github.com:me/mine.git", commits=0)     # personal: [default] lends its login
    mk(tmp_path, "tool", "git@github.com:corp-org/tool.git", commits=0)
    rc, out, err = lost_run(capsys, str(w), "--online")
    first = out.splitlines()[0]
    assert "account me-work, me-dflt" in first or "account me-dflt, me-work" in first
    logins = {e["argv"][3] for e in fake_bin.entries("gh") if e["argv"][:2] == ["auth", "token"]}
    assert logins == {"me-work", "me-dflt"}
    assert "remote check" not in err


def test_online_without_gh_or_glab(tmp_path, w, cfg, fake_bin, monkeypatch, capsys):  # 14
    cfg(apply=False)
    os.symlink(shutil.which("git"), fake_bin.path / "git")
    monkeypatch.setenv("PATH", str(fake_bin.path))
    mk(tmp_path, "tool", "git@github.com:me/tool.git", commits=0)
    rc, out, err = lost_run(capsys, str(w), "--online")
    assert rc == 2 and "needs gh or glab" in err and out == ""
    assert "✗ lost --online needs gh or glab on PATH (remote check); install one or run without --online" in err
    rc, d, _ = lost_json(capsys, str(w), "--online")
    assert rc == 2 and d["ok"] is False and "needs gh or glab" in d["error"]


def test_online_no_login(tmp_path, w, cfg, fake_bin, capsys):  # 15
    cfg(apply=False)
    fake_bin.add("gh", 'print("no account", file=sys.stderr); sys.exit(1)')
    repo = mk(tmp_path, "tool", "git@github.com:corp-org/tool.git", commits=0)
    mk(tmp_path, "lib", "git@github.com:corp-org/lib.git", commits=0)
    rc, d, _ = lost_json(capsys, str(w), "--online")
    assert rc == 0  # empty repos; the unknown remotes are a warning, not lost work
    assert [g["status"] for g in d["remote_gone"]] == ["unknown", "unknown"]
    assert d["remote_gone"][1]["repo"] == str(repo)
    want = ("remote check failed for github.com: gh has no login for me-work on github.com; "
            "run: gh auth login -h github.com; 2 repos unknown")
    assert d["warnings"] == [want]
    # gh is asked for the token once, not once per repo
    assert len([e for e in fake_bin.entries("gh") if e["argv"][:2] == ["auth", "token"]]) == 1
    rc, out, err = lost_run(capsys, str(w), "--online")
    assert f"⚠ {want}" in err
    assert "? unknown" not in out
    assert "nothing lost found under" in out and "(1 check failed)" in out and "✓" not in out
    _, out, _ = lost_run(capsys, str(w), "--online", "-v")
    assert "? unknown" in out


def test_online_host_without_account(tmp_path, w, cfg, fake_bin, capsys):
    cfg(_ALIAS_CONFIG, apply=False)  # no gh_account anywhere
    fake_bin.add("gh", GH)
    mk(tmp_path, "tool", "git@github.com:me/tool.git", commits=0)
    rc, d, err = lost_json(capsys, str(w), "--online")
    assert [g["status"] for g in d["remote_gone"]] == ["unknown"]
    assert d["warnings"] == ["remote check failed for github.com: no gh_account in config for github.com; "
                             "1 repo unknown"]
    assert fake_bin.calls("gh") == []


def test_online_failures_summarised_and_redacted(tmp_path, w, cfg, fake_bin, capsys):  # M2
    from conftest import DEFAULT_CONFIG
    text = DEFAULT_CONFIG.replace('owners = ["corp-org"]', 'owners = ["corp-org", "broken"]')
    cfg(text.replace('email = "me@default.test"\n', 'email = "me@default.test"\ngh_account = "me-dflt"\n', 1),
        apply=False)
    fake_bin.add("gh", GH)
    for n in ("a", "b", "c"):
        mk(tmp_path, n, f"git@github.com:broken/{n}.git", commits=0)
    bb = mk(tmp_path, "bb", "git@bitbucket.org:team/bb.git", commits=0)
    tok = mk(tmp_path, "tok", "https://me:hunter2-pw@github.com/corp-org/tool.git", commits=0)
    rc, d, err = lost_json(capsys, str(w), "--online")
    assert d["warnings"] == [
        "remote check not supported for bitbucket.org; 1 repo unknown",
        "remote check failed for github.com: HTTP 502: bad gateway (https://[REDACTED]@api.github.com/graphql) "
        "[REDACTED] [REDACTED]; 3 repos unknown"]
    by_repo = {g["repo"]: g for g in d["remote_gone"]}
    assert by_repo[str(bb)]["detail"] == "remote check not supported for bitbucket.org"
    assert by_repo[str(tok)]["status"] == "renamed"
    assert by_repo[str(tok)]["url"] == "https://[REDACTED]@github.com/corp-org/tool.git"
    assert by_repo[str(tok)]["fix"] is None     # a fix would have to print the password
    rc, out, err = lost_run(capsys, str(w), "--online", "-v")
    for text in (out, err, json.dumps(d)):
        for secret in ("ghp_", "glpat-", "github_pat_", "hunter2", "x-access-token"):
            assert secret not in text, secret
    assert "no gh login" not in err


def test_online_from_config_and_only_remote_gone_offline(tmp_path, w, cfg, fake_bin, capsys):
    from conftest import DEFAULT_CONFIG
    fake_bin.add("gh", GH)
    mk(tmp_path, "tool", "git@github.com:corp-org/tool.git", commits=0)
    cfg(apply=False)
    rc, out, err = lost_run(capsys, str(w), "--only", "remote_gone")
    assert rc == 2 and "needs the network" in err and fake_bin.calls("gh") == []
    cfg(DEFAULT_CONFIG + "\n[forge]\nonline = true\n", apply=False)
    rc, out, _ = lost_run(capsys, str(w))
    assert out.startswith("online: ") and "renamed to corp-org/tool-cli" in out
    n = len(fake_bin.calls("gh"))
    rc, out, err = lost_run(capsys, str(w), "--offline")     # --offline wins over config
    assert rc == 0 and not out.startswith("online:") and len(fake_bin.calls("gh")) == n
    assert "remote gone: run with --online" in out
    rc, d, _ = lost_json(capsys, str(w), "--offline")
    assert d["online"] is False and d["remote_gone"] == []
    rc, out, err = lost_run(capsys, str(w), "--offline", "--only", "remote_gone")
    assert rc == 2 and "needs the network" in err
    with pytest.raises(SystemExit) as e:
        lost_run(capsys, str(w), "--online", "--offline")
    assert e.value.code == 2 and "not allowed with argument" in capsys.readouterr().err


def test_online_from_config_without_gh_falls_back(tmp_path, w, cfg, fake_bin, monkeypatch, capsys):  # M3
    from conftest import DEFAULT_CONFIG
    cfg(DEFAULT_CONFIG + "\n[forge]\nonline = true\n", apply=False)
    os.symlink(shutil.which("git"), fake_bin.path / "git")
    monkeypatch.setenv("PATH", str(fake_bin.path))
    mk(tmp_path, "solo", None)
    rc, out, err = lost_run(capsys, str(w))
    assert rc == 1 and "no remote (1 repo)" in out and not out.startswith("online:")
    assert ("⚠ [forge] online = true, but neither gh nor glab is on PATH; checking offline (remote gone skipped)"
            in err)
    rc, d, _ = lost_json(capsys, str(w))
    assert rc == 1 and d["online"] is False and d["warnings"][0].startswith("[forge] online = true")
    rc, out, err = lost_run(capsys, str(w), "--online")      # explicit: still exit 2
    assert rc == 2 and "needs gh or glab" in err
    rc, out, err = lost_run(capsys, str(w), "--only", "remote_gone")
    assert rc == 2 and "needs gh or glab" in err


def test_vscode_never_goes_online():  # spec 03 §5
    src = (Path(__file__).resolve().parents[1] / "vscode/src/lost.ts").read_text()
    assert '["lost", "--json", "--offline"]' in src


def test_offline_hint(tmp_path, w, capsys):
    mk(tmp_path, "solo", None)
    _, out, _ = lost_run(capsys, str(w))
    assert "remote gone: run with --online to check remotes via gh/glab" in out
    _, d, _ = lost_json(capsys, str(w))
    assert d["online"] is False and d["remote_gone"] == []


# ---------------------------------------------------------------- 16-18: roots, unreadable, order

def test_roots_from_config(tmp_path, w, home, monkeypatch, capsys):  # 16
    repo = mk(tmp_path, "solo", None)
    write_config(home, f'[default]\nname = "Me"\nemail = "me@x.test"\n\n[scan]\nroots = ["{w}"]\n')
    _, d, _ = lost_json(capsys)
    assert d["roots"] == [str(w)] and d["roots_source"] == "config"
    assert d["no_remote"] == [{"repo": str(repo), "commits": 1}]

    monkeypatch.delattr("gitpro.scan.effective_roots", raising=False)
    _, d, _ = lost_json(capsys)
    assert d["roots"] == [str(w)] and d["roots_source"] == "config" and d["repos"] == 1

    calls = []

    def stub(dirs, scan_cfg):
        calls.append((dirs, scan_cfg))
        return [w], [], "args"
    monkeypatch.setattr("gitpro.scan.effective_roots", stub, raising=False)
    _, d, _ = lost_json(capsys)
    assert len(calls) == 1 and calls[0][0] == [] and calls[0][1].roots == (str(w),)
    assert d["roots_source"] == "args" and d["repos"] == 1


def test_roots_default_and_bad_config(tmp_path, home, monkeypatch, capsys):
    monkeypatch.delattr("gitpro.scan.effective_roots", raising=False)
    repo = repo_with_remote(home / "src", "solo", None, commits=1)
    _, d, _ = lost_json(capsys)
    assert d["roots"] == [str(home)] and d["roots_source"] == "default"
    assert [x["repo"] for x in d["no_remote"]] == [str(repo)]
    write_config(home, "[scan]\nroots = 3\n")
    rc, d, err = lost_json(capsys)
    assert rc == 1 and d["roots"] == [str(home)]
    assert any("scanning ~" in x for x in d["warnings"])


def test_repo_flag_skips_discovery(tmp_path, w, capsys):
    a = mk(tmp_path, "a", None)
    mk(tmp_path, "b", None)
    _, d, _ = lost_json(capsys, "--repo", str(a))
    assert d["repos"] == 1 and [x["repo"] for x in d["no_remote"]] == [str(a)]


def test_unreadable_repo_skipped(tmp_path, w, capsys):  # 17
    broken = w / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nonexistent/gitpro-test\n")
    mk(tmp_path, "fine", None)
    rc, d, _ = lost_json(capsys, str(w))
    assert d["skipped"] == [{"path": str(broken), "reason": "with a broken .git link",
                             "error": "broken .git: points to missing /nonexistent/gitpro-test"}]
    assert d["repos"] == 1
    rc, out, _ = lost_run(capsys, str(w))
    assert "1 skipped: 1 with a broken .git link" in out


def test_stable_order(tmp_path, w, capsys):  # 18
    names = ["f", "c", "a", "e", "b", "d"]
    for n in names:
        mk(tmp_path, n, None)
    want = [str(w / n) for n in sorted(names)]
    texts = set()
    for _ in range(3):
        _, d, _ = lost_json(capsys, str(w))
        assert [x["repo"] for x in d["no_remote"]] == want
        texts.add(lost_run(capsys, str(w))[1])
    assert len(texts) == 1


# ---------------------------------------------------------------- safety and failure modes

def _tree_digest(gitdir: Path) -> dict:
    return {str(p.relative_to(gitdir)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(gitdir.rglob("*")) if p.is_file()}


def test_read_only(tmp_path, w, bare_remote, capsys):
    repo = pushed(tmp_path, bare_remote)
    add_file(repo, "f.txt")
    (repo / "f.txt").write_text("changed\n")
    os.utime(repo / "f.txt", ns=(1, 1))  # stale stat data: a plain `git status` would rewrite the index
    sh(repo, "checkout", "-q", "-b", "feature")
    dup = w / "dup"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remotes/app.git"), str(dup)], check=True,
                   capture_output=True)
    before = {r: _tree_digest(r / ".git") for r in (repo, dup)}
    remote_before = _tree_digest(tmp_path / "remotes")
    lost_run(capsys, str(w), "-v")
    lost_json(capsys, str(w))
    assert {r: _tree_digest(r / ".git") for r in (repo, dup)} == before
    assert _tree_digest(tmp_path / "remotes") == remote_before


def test_timeout_skips_repo(tmp_path, w, monkeypatch, capsys):
    repo = mk(tmp_path, "slow", None)
    real = lost._run

    def slow(cmd, env, timeout):
        if cmd[:1] == ["git"] and "for-each-ref" in cmd:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return real(cmd, env, timeout)
    monkeypatch.setattr(lost, "_run", slow)
    rc, d, _ = lost_json(capsys, str(w))
    assert f"{repo}: timed out; skipped" in d["warnings"]
    assert d["skipped"] == [{"path": str(repo), "reason": "timed out", "error": "git for-each-ref timed out"}]
    assert d["no_remote"] == []


def test_check_failure_warns_and_continues(tmp_path, w, monkeypatch, capsys):
    repo = mk(tmp_path, "half", None)
    (repo / "new").touch()
    real = lost._run

    def broken(cmd, env, timeout):
        if cmd[:1] == ["git"] and "stash" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: stash exploded\nmore\n")
        return real(cmd, env, timeout)
    monkeypatch.setattr(lost, "_run", broken)
    rc, d, _ = lost_json(capsys, str(w))
    assert f"{repo}: git stash failed: fatal: stash exploded" in d["warnings"]
    assert d["dirty"] and d["no_remote"]


def test_capabilities(capsys, home):
    capsys.readouterr()
    run("--version", "--json")
    caps = json.loads(capsys.readouterr().out)["capabilities"]
    assert "lost" in caps and "lost.online" in caps


def test_collect_api(tmp_path, w):
    repo = mk(tmp_path, "solo", None)
    rep = lost.collect([w], (), None, online=False, kinds=set(lost.OFFLINE_KINDS), stale_days=180)
    assert rep["no_remote"] == [{"repo": str(repo), "commits": 1}]
    text = lost.render(rep, verbose=False)
    assert "no remote (1 repo)" in text


# ---------------------------------------------------------------- QA fixes

def _packs(repo: Path) -> set:
    return {p.name for p in (repo / ".git/objects/pack").glob("*.pack")}


def test_partial_clone_never_fetches(tmp_path, w, bare_remote, capsys):  # B1
    src = bare_remote("pc.git")
    sh(src, "config", "uploadpack.allowFilter", "true")
    sh(src, "config", "uploadpack.allowAnySHA1InWant", "true")
    sh(src, "symbolic-ref", "HEAD", "refs/heads/main")
    seed = repo_with_remote(tmp_path / "seed", "s", str(src), commits=0)
    (seed / "d").mkdir()
    (seed / "x").mkdir()
    (seed / "x/big.txt").write_text("".join(f"line {i}\n" for i in range(300)))   # outside the sparse cone
    (seed / "d/keep.txt").write_text("keep\n")
    sh(seed, "add", "-A")
    commit(seed, "init", author=ME)
    sh(seed, "push", "-q", "origin", "HEAD:main")
    log = tmp_path / "upload-pack.log"
    spy = tmp_path / "spy-upload-pack"
    spy.write_text(f'#!/bin/sh\necho "upload-pack $*" >> {log}\nexec git-upload-pack "$@"\n')
    spy.chmod(0o755)
    a = w / "a"
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", "-u", str(spy), f"file://{src}",
                    str(a)], check=True, capture_output=True)
    sh(a, "config", "remote.origin.uploadpack", str(spy))
    sh(a, "sparse-checkout", "set", "d")
    sh(a, "checkout", "-q", "main")
    # Repro 1: a staged delete of a file whose blob was never fetched plus a similar add: rename detection
    # would need the deleted blob.
    sh(a, "update-index", "--force-remove", "x/big.txt")
    (a / "d/moved.txt").write_text("".join(f"line {i}\n" for i in range(300)) + "x\n")
    sh(a, "add", "d/moved.txt")
    # Repro 2: a duplicate clone whose HEAD exists nowhere locally (its objects are gone): comparing it from
    # the partial clone would lazily fetch the commit.
    b = w / "b"
    subprocess.run(["git", "clone", "-q", f"file://{src}", str(b)], check=True, capture_output=True)
    commit(b, "only in b", author=ME)
    (b / ".git/objects").rename(b / ".git/objects.away")
    (b / ".git/objects").mkdir()
    log.write_text("")
    packs = _packs(a)
    rc, d, _ = lost_json(capsys, str(w))
    rc2, out, err = lost_run(capsys, str(w), "-v")
    assert log.read_text() == ""            # upload-pack never ran
    assert _packs(a) == packs               # and no pack was written
    assert {"repo": str(a), "modified": 2, "untracked": 0} in d["dirty"]
    [g] = d["duplicates"]
    assert g["repos"] == [str(a), str(b)] and g["diverged"] is None
    assert g["detail"] == "HEADs differ; could not compare"


def test_partial_clone_duplicates_still_compare(tmp_path, w, bare_remote, capsys):
    src = bare_remote("pc2.git")
    sh(src, "config", "uploadpack.allowFilter", "true")
    sh(src, "symbolic-ref", "HEAD", "refs/heads/main")
    seed = repo_with_remote(tmp_path / "seed", "s", str(src), commits=1)
    sh(seed, "push", "-q", "origin", "HEAD:main")
    a, b = w / "a", w / "b"
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", f"file://{src}", str(a)], check=True,
                   capture_output=True)
    subprocess.run(["git", "clone", "-q", f"file://{src}", str(b)], check=True, capture_output=True)
    commit(b, "extra", author=ME)
    _, d, _ = lost_json(capsys, str(w))
    [g] = d["duplicates"]
    assert g["diverged"] is True and g["detail"] == f"{b} has 1 commit not in {a}"


def test_branch_and_tag_same_name(tmp_path, w, bare_remote, capsys):  # M1
    repo = pushed(tmp_path, bare_remote)
    sh(repo, "checkout", "-q", "-b", "release")
    commit(repo, "r", author=ME)
    sh(repo, "tag", "release")
    commit(repo, "r2", author=ME)
    rc, d, _ = lost_json(capsys, str(w))
    assert d["unpushed"] == [{"repo": str(repo), "branch": "release", "commits": 2, "upstream": None, "ahead": None,
                              "detached": False, "fix": f"git -C {repo} push -u origin refs/heads/release"}]
    assert d["warnings"] == []
    sh(repo, "push", "-q", "-u", "origin", "refs/heads/release")
    sh(repo, "push", "-q", "origin", "--delete", "release")
    sh(repo, "fetch", "-q", "--prune")
    _, d, _ = lost_json(capsys, str(w))
    assert d["merged_gone"] == [{"repo": str(repo), "branch": "release", "commits": 2}]
    assert d["warnings"] == []


def test_submodule_reported_at_its_checkout(tmp_path, w, bare_remote, capsys):  # m1
    sub_src = _seeded(tmp_path, bare_remote)
    sup = pushed(tmp_path, bare_remote, "super")
    sh(sup, "-c", "protocol.file.allow=always", "submodule", "-q", "add", str(sub_src), "sub")
    commit(sup, "add sub", author=ME)
    sh(sup, "push", "-q")
    (sup / "sub/loose").touch()
    _, d, _ = lost_json(capsys, str(w))
    assert {"repo": str(sup / "sub"), "modified": 0, "untracked": 1} in d["dirty"]
    assert not any(".git/modules" in json.dumps(v) for k, v in d.items() if k != "roots")


def test_run_kills_the_process_group(tmp_path):  # m3
    pidfile = tmp_path / "pid"
    with pytest.raises(subprocess.TimeoutExpired):
        lost._run(["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], dict(os.environ), 1)
    pid = int(pidfile.read_text())
    import time
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail("the grandchild outlived the timeout")


def test_missing_paths_and_bare_repo(tmp_path, w, bare_remote, capsys):  # m4
    rc, out, err = lost_run(capsys, str(tmp_path / "nope"))
    assert rc == 2 and f"✗ {tmp_path / 'nope'}: no such directory" in err and out == ""
    rc, d, _ = lost_json(capsys, "--repo", str(tmp_path / "nope"))
    assert rc == 2 and "no such directory" in d["error"]
    bare = bare_remote("lonely.git")
    rc, out, err = lost_run(capsys, "--repo", str(bare))
    assert rc == 0 and f"⚠ {bare}: bare repository with no worktrees; nothing to check" in err
    assert "✓ nothing lost" in out
    # a bare repo with a linked worktree: --repo checks the worktree
    seed = repo_with_remote(tmp_path / "seed", "s", str(bare), commits=1)
    sh(seed, "push", "-q", "origin", "HEAD:main")
    wt = tmp_path / "wt"
    sh(bare, "worktree", "add", "-q", str(wt), "main")
    (wt / "loose").touch()
    rc, d, _ = lost_json(capsys, "--repo", str(bare))
    assert d["dirty"] == [{"repo": str(wt), "modified": 0, "untracked": 1}] and d["warnings"] == []


def test_failed_checks_are_not_a_clean_bill(tmp_path, w, bare_remote, monkeypatch, capsys):  # m5
    pushed(tmp_path, bare_remote)
    real = lost._run

    def broken(cmd, env, timeout):
        if cmd[:1] == ["git"] and "stash" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: stash exploded\n")
        return real(cmd, env, timeout)
    monkeypatch.setattr(lost, "_run", broken)
    rc, out, err = lost_run(capsys, str(w))
    assert "✓" not in out and "nothing lost found under" in out and "(1 check failed)" in out


def test_duplicates_detail_capped(monkeypatch):  # m10
    results = [{"repo": f"/w/r{i:02d}", "urls": [("origin", "git@github.com:me/app.git")], "skipped": None,
                "head": f"h{i}", "common": f"/w/r{i:02d}/.git"} for i in range(12)]
    monkeypatch.setattr(lost, "_ahead", lambda a, b: 1)
    [g] = lost._duplicates(results, None)
    parts = g["detail"].split("; ")
    assert len(parts) == 11 and parts[-1] == "and 12 more" and g["diverged"] is True
