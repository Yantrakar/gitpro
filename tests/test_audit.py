"""08 audit: wrong-author and agent-authored commits, the trailer policy, `--rewrite` (spec 08 §10, tests 1-12)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG, TIMEOUT, commit, git, repo_with_remote, run, set_git_hooks_sandbox
from gitpro import audit

WORK = "git@github.com:corp-org/app.git"
ME = ("Work Me", "me@corp.test")
PERSONAL = ("Personal Me", "me@personal.test")
COLLEAGUE = ("Colleague", "c@other.test")
CLAUDE = "Co-Authored-By: Claude <noreply@anthropic.com>"
FORBID = DEFAULT_CONFIG.replace('gh_account = "me-work"\n',
                                'gh_account = "me-work"\n\n[identity.trailers]\nco_authored_by = "forbid"\n')


def policy_config(body: str) -> str:
    return DEFAULT_CONFIG.replace('gh_account = "me-work"\n', f'gh_account = "me-work"\n\n[identity.trailers]\n{body}\n')


@pytest.fixture
def repo(home, cfg, tmp_path, monkeypatch, bare_remote):
    """A work-github repo (origin WORK, pushing to a bare repo through insteadOf) with a tracked file and
    3 pushed commits by me@corp.test; config FORBID applied, no guard installed."""
    set_git_hooks_sandbox(monkeypatch, home)
    cfg(FORBID)
    bare = bare_remote("app.git")
    r = repo_with_remote(tmp_path, "app", WORK)
    git(r, "config", f"url.{bare}.insteadOf", WORK)
    git(r, "checkout", "-q", "-b", "main")
    (r / "README").write_text("hello\n")
    git(r, "add", "README")
    commit(r, "commit 1", author=ME)
    commit(r, "commit 2", author=ME)
    commit(r, "commit 3", author=ME)
    push(r)
    return r


def push(r: Path) -> None:
    subprocess.run(["git", "-C", str(r), "push", "-q", "-u", "origin", "main"], check=True, capture_output=True,
                   timeout=TIMEOUT)


def audit_json(capsys, *argv: str):
    capsys.readouterr()
    rc = run("audit", *argv, "--json")
    out = capsys.readouterr().out
    return rc, json.loads(out)


def audit_text(capsys, *argv: str):
    capsys.readouterr()
    rc = run("audit", *argv)
    o = capsys.readouterr()
    return rc, o.out, o.err


def fmt(r: Path, sha: str, f: str) -> str:
    return subprocess.run(["git", "-C", str(r), "log", "-1", f"--format={f}", sha], capture_output=True,
                          text=True, timeout=TIMEOUT).stdout


# ---------------------------------------------------------------- 1-5: the author audit

def test_clean_repo(repo, capsys):
    rc, out, _ = audit_text(capsys, "--all", str(repo))
    assert rc == 0
    assert "✓ all 3 commits are committed as me@corp.test; no agent trailers" in out
    assert "identity work-github <me@corp.test>  3 commits (3 pushed, 0 unpushed, as of last fetch)" in out
    rc, data = audit_json(capsys, str(repo))
    assert rc == 0 and data["ok"] is True and data["command"] == "audit"
    assert (data["commits"], data["pushed"], data["unpushed"]) == (3, 3, 0)
    assert data["range"] == "HEAD --not --remotes"
    assert data["identity"] == {"id": "work-github", "email": "me@corp.test"}
    assert data["wrong_author"] == [] and data["agents"] == [] and data["rewritten"] is None
    assert data["policy"]["co_authored_by"] == "forbid" and data["policy"]["violations"] == 0


def test_unpushed_wrong_committer(repo, capsys):
    tip = git(repo, "rev-parse", "HEAD")
    a = commit(repo, "add login", author=PERSONAL)
    b = commit(repo, "fix tests", author=PERSONAL)
    rc, data = audit_json(capsys, str(repo))
    assert rc == 1 and data["ok"] is False
    assert [w["sha"] for w in data["wrong_author"]] == [b, a]
    assert all(w["pushed"] is False and w["why"] == "committer" for w in data["wrong_author"])
    assert data["rewrite"] == {"possible": True, "base": tip, "commits": 2, "replayed": 2, "reason": None}
    rc, out, _ = audit_text(capsys, str(repo))
    assert rc == 1
    assert "wrong author (2 unpushed, 0 pushed)" in out
    assert "→ rewrite the 2 unpushed: gitpro audit --rewrite " in out
    assert "me@personal.test" in out and "unpushed" in out


def test_colleague_author_is_never_flagged(repo, capsys):
    commit(repo, "their patch", author=COLLEAGUE, committer=ME)
    rc, data = audit_json(capsys, str(repo))
    assert rc == 0 and data["wrong_author"] == []
    own = commit(repo, "mine, wrong author", author=PERSONAL, committer=ME)
    rc, data = audit_json(capsys, str(repo))
    assert rc == 1
    assert [(w["sha"], w["why"]) for w in data["wrong_author"]] == [(own, "author")]


def test_pushed_wrong_author_is_report_only(repo, capsys):
    bad = commit(repo, "initial import", author=PERSONAL)
    push(repo)
    rc, data = audit_json(capsys, str(repo))
    assert rc == 1
    assert [(w["sha"], w["pushed"]) for w in data["wrong_author"]] == [(bad, True)]
    assert data["rewrite"]["possible"] is False
    rc, out, _ = audit_text(capsys, str(repo))
    assert "pushed · report only" in out and "→ rewrite" not in out
    assert "pushed commits are left alone" in out
    before = git(repo, "rev-parse", "HEAD")
    rc, out, err = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 2
    assert "1 of the wrong-author commits is pushed; rewrite only covers unpushed commits" in err
    assert git(repo, "rev-parse", "HEAD") == before


def test_merge_in_range_cannot_be_rewritten(repo, capsys):
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "side work", author=PERSONAL)
    git(repo, "checkout", "-q", "main")
    commit(repo, "main work", author=ME)
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "--no-ff", "--no-edit", "--no-gpg-sign", "side"],
                   check=True, capture_output=True, timeout=TIMEOUT,
                   env=dict(os.environ, GIT_AUTHOR_NAME=ME[0], GIT_AUTHOR_EMAIL=ME[1],
                            GIT_COMMITTER_NAME=ME[0], GIT_COMMITTER_EMAIL=ME[1]))
    rc, data = audit_json(capsys, str(repo))
    assert rc == 1 and len(data["wrong_author"]) == 1
    assert data["rewrite"]["possible"] is False and "merge commits" in data["rewrite"]["reason"]
    rc, out, _ = audit_text(capsys, str(repo))
    assert "→ 1 unpushed commit needs a manual rebase (merge commits in range)" in out
    rc, out, err = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 2 and "merge commits in range" in err


# ---------------------------------------------------------------- 6-7: --rewrite

def test_rewrite_fixes_only_the_offenders(repo, capsys, home, cfg):
    cfg(policy_config('co_authored_by = "allow"'), apply=False)     # Pat's trailer is no policy finding
    old_tip = git(repo, "rev-parse", "HEAD")
    c1 = commit(repo, "one", author=ME, date="2026-09-01T10:00:00+02:00")
    c2 = commit(repo, "two", author=PERSONAL, date="2026-09-02T10:00:00+02:00")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "--no-gpg-sign", "--cleanup=verbatim",
                    "-m", "three\n\nbody line\n\nCo-Authored-By: Pat <pat@corp.test>\n\n"], check=True,
                   capture_output=True, timeout=TIMEOUT,
                   env=dict(os.environ, GIT_AUTHOR_NAME=COLLEAGUE[0], GIT_AUTHOR_EMAIL=COLLEAGUE[1],
                            GIT_COMMITTER_NAME=PERSONAL[0], GIT_COMMITTER_EMAIL=PERSONAL[1],
                            GIT_AUTHOR_DATE="2026-09-03T10:00:00+02:00", GIT_COMMITTER_DATE="2026-09-03T10:00:00+02:00"))
    c3 = git(repo, "rev-parse", "HEAD")
    c4 = commit(repo, "four", author=COLLEAGUE, date="2026-09-04T10:00:00+02:00")
    (repo / "README").write_text("changed\n")
    git(repo, "commit", "-q", "-a", "--no-gpg-sign", "-m", "five")
    c5 = git(repo, "rev-parse", "HEAD")
    chain = [c1, c2, c3, c4, c5]
    before = {c: (fmt(repo, c, "%T%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI"), fmt(repo, c, "%B")) for c in chain}
    status = git(repo, "status", "--porcelain")
    reflog = git(repo, "reflog", "show", "--format=%gs", "refs/heads/main").splitlines()

    rc, out, err = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 0, out + err
    assert "✓ rewrote 2 commits (4 new shas: 2 fixed, 2 descendants re-parented)" in out
    assert "update-ref" in out and "↩ undo: sh " in out

    new = git(repo, "rev-list", "--reverse", f"{old_tip}..HEAD").splitlines()
    assert len(new) == 5
    n1, n2, n3, n4, n5 = new
    assert n1 == c1
    assert all(n != o for n, o in zip(new[1:], chain[1:]))
    assert fmt(repo, n2, "%ae %ce").strip() == "me@corp.test me@corp.test"
    assert fmt(repo, n2, "%an %cn").strip() == "Work Me Work Me"
    assert fmt(repo, n3, "%ae %ce").strip() == "c@other.test me@corp.test"
    assert fmt(repo, n4, "%ae %ce").strip() == "c@other.test c@other.test"
    assert fmt(repo, n5, "%ae %ce").strip() == "me@corp.test me@corp.test"
    for o, n in zip(chain, new):
        assert fmt(repo, n, "%T%x00%aI%x00%cI") == fmt(repo, o, "%T%x00%aI%x00%cI")
        assert fmt(repo, n, "%B") == before[o][1]
    assert before[c5][0] == fmt(repo, n5, "%T%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI")
    raw = subprocess.run(["git", "-C", str(repo), "cat-file", "commit", n3], capture_output=True, timeout=TIMEOUT)
    assert raw.stdout.endswith(b"\n\nbody line\n\nCo-Authored-By: Pat <pat@corp.test>\n\n")    # trailing blank line kept
    assert git(repo, "status", "--porcelain") == status == ""
    after = git(repo, "reflog", "show", "--format=%gs", "refs/heads/main").splitlines()
    assert after[1:] == reflog and after[0] == "gitpro audit --rewrite"
    assert not (repo / ".git/rebase-merge").exists()

    rc, data = audit_json(capsys, str(repo))
    assert rc == 0 and data["wrong_author"] == []

    scripts = list((home / ".local/state/gitpro").glob("audit*undo*.sh"))
    assert len(scripts) == 1
    assert os.stat(scripts[0]).st_mode & 0o777 == 0o700
    u = subprocess.run(["sh", str(scripts[0])], capture_output=True, text=True, timeout=TIMEOUT)
    assert u.returncode == 0, u.stderr
    assert git(repo, "rev-parse", "HEAD") == c5
    again = subprocess.run(["sh", str(scripts[0])], capture_output=True, text=True, timeout=TIMEOUT)
    assert again.returncode != 0 and again.stderr.strip()
    assert git(repo, "rev-parse", "HEAD") == c5


def test_rewrite_json_needs_yes_and_reports(repo, capsys):
    commit(repo, "two", author=PERSONAL)
    before = git(repo, "rev-parse", "HEAD")
    capsys.readouterr()
    assert run("audit", "--rewrite", "--json", str(repo)) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert git(repo, "rev-parse", "HEAD") == before
    rc, data = audit_json(capsys, "--rewrite", "-y", str(repo))
    assert rc == 0, data
    assert data["rewritten"]["count"] == 1 and Path(data["rewritten"]["undo"]).exists()
    assert git(repo, "rev-parse", "HEAD") != before


def test_rewrite_without_terminal_or_declined(repo, capsys, monkeypatch):
    commit(repo, "two", author=PERSONAL)
    before = git(repo, "rev-parse", "HEAD")
    rc, out, err = audit_text(capsys, "--rewrite", str(repo))
    assert rc == 2 and "re-run with --yes" in err
    monkeypatch.setattr(audit, "_confirm", lambda q, yes: False)
    rc, out, err = audit_text(capsys, "--rewrite", str(repo))
    assert rc == 1 and "aborted" in out
    assert "rewrite 1 unpushed commit on main" in out
    assert git(repo, "rev-parse", "HEAD") == before
    assert not list(Path(os.environ["HOME"], ".local/state/gitpro").glob("audit*undo*.sh"))


def test_rewrite_refuses_dirty_tree_and_wrong_config(repo, capsys):
    commit(repo, "two", author=PERSONAL)
    before = git(repo, "rev-parse", "HEAD")
    (repo / "README").write_text("dirty\n")
    rc, out, err = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 2 and "working tree has changes; commit or stash them first" in err
    assert git(repo, "rev-parse", "HEAD") == before
    git(repo, "checkout", "--", "README")
    git(repo, "config", "user.email", "me@personal.test")
    rc, out, err = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 2
    assert "git would still commit as me@personal.test; fix the repo first:" in err
    assert git(repo, "rev-parse", "HEAD") == before


def test_rewrite_refuses_detached_head_and_rebase(repo, capsys):
    commit(repo, "two", author=PERSONAL)
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--detach")
    rc, _, err = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 2 and "HEAD is detached" in err
    git(repo, "checkout", "-q", "main")
    (repo / ".git/rebase-merge").mkdir()
    rc, _, err = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 2 and "a rebase is in progress" in err
    assert git(repo, "rev-parse", "HEAD") == head


def test_rewrite_with_nothing_to_do(repo, capsys):
    rc, out, _ = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 0 and "nothing to rewrite" in out


def test_rewrite_and_agents_conflict(repo, capsys):
    rc, _, err = audit_text(capsys, "--rewrite", "--agents", "-y", str(repo))
    assert rc == 2


# ---------------------------------------------------------------- 8-11: agents and the trailer policy

def test_agent_trailer_and_forbid_policy(repo, capsys, cfg):
    sha = commit(repo, "refactor 100% of parser ✓", author=ME, trailers=[CLAUDE])
    rc, data = audit_json(capsys, str(repo))
    assert rc == 1 and data["ok"] is False
    assert data["agents"][0]["sha"] == sha and data["agents"][0]["kind"] == "agent_trailer"
    assert data["agents"][0]["who"] == "Claude <noreply@anthropic.com>" and data["agents"][0]["pushed"] is False
    assert data["policy"]["violations"] == 1
    rc, out, _ = audit_text(capsys, str(repo))
    assert "agent commits (1)" in out and "policy work-github: forbid  ✗" in out
    assert "refactor 100% of parser ✓" in out
    assert '→ [identity.trailers] co_authored_by = "forbid" for work-github; the commit-msg guard blocks new ones' in out
    cfg(policy_config('co_authored_by = "allow"'), apply=False)
    rc, data = audit_json(capsys, str(repo))
    assert rc == 0 and len(data["agents"]) == 1 and data["policy"]["violations"] == 0


def test_message_parsing_edge_cases(repo, capsys):
    msg = "subject with %s and %x00 and ünïcödé\n\nbody\n\nmore body after an empty line"
    sha = commit(repo, msg, author=ME)
    rc, data = audit_json(capsys, "--agents", str(repo))
    assert rc == 0
    assert data["checked"] == 4 and data["wrong_author"] is None
    rc, out, _ = audit_text(capsys, "-v", str(repo))
    rows = audit.log(repo, [f"{sha}^!"])
    assert len(rows) == 1 and rows[0].subject == "subject with %s and %x00 and ünïcödé"
    assert rows[0].trailers == []


def test_bot_author_is_agent_author(repo, capsys):
    commit(repo, "bump deps", author=("dependabot[bot]", "dependabot[bot]@users.noreply.github.com"),
           committer=ME)
    rc, data = audit_json(capsys, str(repo))
    assert [a["kind"] for a in data["agents"]] == ["agent_author"]
    assert data["agents"][0]["who"] == "dependabot[bot] <dependabot[bot]@users.noreply.github.com>"
    assert data["wrong_author"] == []


def test_human_co_author_and_forbid_emails(repo, capsys, cfg):
    cfg(policy_config('co_authored_by = "allow"'), apply=False)
    commit(repo, "pair work", author=ME, trailers=["Co-Authored-By: Pat <pat@corp.test>"])
    rc, data = audit_json(capsys, str(repo))
    assert rc == 0 and data["co_authored"] == 1 and data["agents"] == []
    cfg(policy_config('forbid_emails = ["*@corp.test"]'), apply=False)
    rc, data = audit_json(capsys, str(repo))
    assert rc == 1 and data["policy"]["violations"] == 1
    assert 'forbid_emails has "*@corp.test"' in data["policy"]["commits"][0]["messages"][0]


def test_require_policy(repo, capsys, cfg):
    cfg(policy_config('co_authored_by = "require"'), apply=False)
    commit(repo, "no trailers", author=ME)
    rc, data = audit_json(capsys, str(repo))
    assert rc == 1 and data["policy"]["violations"] >= 1
    assert any("requires a Co-Authored-By trailer" in m for c in data["policy"]["commits"] for m in c["messages"])
    rc, out, _ = audit_text(capsys, str(repo))
    assert "requires a Co-Authored-By trailer" in out


# ---------------------------------------------------------------- 12: ranges

def test_all_and_range(repo, capsys):
    commit(repo, "four", author=ME)
    rc, data = audit_json(capsys, "--all", str(repo))
    assert data["commits"] == 4 and data["checked"] == 4 and data["range"] == "HEAD"
    rc, data = audit_json(capsys, "--range", "HEAD~1..HEAD", str(repo))
    assert data["commits"] == 1 and data["range"] == "HEAD~1..HEAD"
    rc, data = audit_json(capsys, str(repo))
    assert (data["commits"], data["pushed"], data["unpushed"]) == (4, 3, 1)


def test_range_errors(repo, capsys):
    rc, _, err = audit_text(capsys, "--range", "nope..HEAD", str(repo))
    assert rc == 2 and "git rev-list failed" in err
    rc, _, err = audit_text(capsys, "--range", "--output=/tmp/x HEAD", str(repo))
    assert rc == 2 and "is not a revision" in err


def test_not_a_repo(home, cfg, tmp_path, capsys):
    cfg(apply=False)
    d = tmp_path / "plain"
    d.mkdir()
    rc, _, err = audit_text(capsys, str(d))
    assert rc == 2 and f"{d}: not a git repository" in err


def test_no_identity_skips_author_check(home, cfg, tmp_path, capsys, monkeypatch):
    set_git_hooks_sandbox(monkeypatch, home)
    text = DEFAULT_CONFIG.replace('name = "Default Me"\nemail = "me@default.test"\n', "")
    cfg(text, apply=False)
    r = repo_with_remote(tmp_path, "elsewhere", "git@gitlab.example.test:someone/thing.git", commits=1,
                         author=PERSONAL)
    commit(r, "agent", author=ME, trailers=[CLAUDE])
    rc, out, err = audit_text(capsys, str(r))
    assert "⚠ no identity for gitlab.example.test/someone; author check skipped" in err
    assert "agent commits (1)" in out
    rc, data = audit_json(capsys, str(r))
    assert data["wrong_author"] is None and data["agents"][0]["kind"] == "agent_trailer"
    assert any("author check skipped" in w for w in data["warnings"])


def test_empty_repo(home, cfg, tmp_path, capsys):
    cfg(apply=False)
    r = repo_with_remote(tmp_path, "empty", WORK)
    rc, data = audit_json(capsys, str(r))
    assert rc == 0 and data["commits"] == 0


def test_capabilities():
    from gitpro import cli
    cli.build_parser()
    assert {"audit", "audit.rewrite", "trailers"} <= cli.CAPABILITIES


def test_collect_is_plain_json(repo, home):
    """mcp (11) calls collect(repo, cfg, range, all) and returns the dict as JSON."""
    from gitpro import config
    commit(repo, "two", author=PERSONAL, trailers=[CLAUDE])
    data = audit.collect(repo, config.load_all(home / ".config/gitpro.toml"), None, False)
    back = json.loads(json.dumps(data))
    assert back == data and not any(k.startswith("_") for k in data)
    assert data["ok"] is False and data["rewrite"]["possible"] is True and "_chain" not in data["rewrite"]


# ---------------------------------------------------------------- QA follow-ups: bytes, signatures, notes, undo

def raw_commit(r: Path, rev: str) -> bytes:
    return subprocess.run(["git", "-C", str(r), "cat-file", "commit", rev], capture_output=True, check=True,
                          timeout=TIMEOUT).stdout


def test_rewrite_keeps_extra_headers_and_drops_stale_signature(repo, capsys):
    bad = commit(repo, "bad", author=PERSONAL)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    body = (f"tree {tree}\nparent {bad}\n"
            "author Work Me <me@corp.test> 1700000000 +0100\ncommitter Work Me <me@corp.test> 1700000000 +0100\n"
            "change-id zzzzkkkkzzzzkkkk\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n fake\n -----END PGP SIGNATURE-----\n\nsigned once\n").encode()
    sha = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"], input=body,
                         capture_output=True, check=True, timeout=TIMEOUT).stdout.decode().strip()
    git(repo, "update-ref", "refs/heads/main", sha)
    rc, out, err = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 0, out + err
    assert "1 signed commit loses its signature (commit.gpgsign is off)" in out
    new = raw_commit(repo, "HEAD")
    assert b"\nchange-id zzzzkkkkzzzzkkkk\n" in new and b"gpgsig" not in new
    assert new.replace(f"parent {git(repo, 'rev-parse', 'HEAD~1')}".encode(), f"parent {bad}".encode()) == \
        body.replace(b"gpgsig -----BEGIN PGP SIGNATURE-----\n fake\n -----END PGP SIGNATURE-----\n", b"")


def test_rewrite_copies_notes(repo, capsys):
    commit(repo, "bad", author=PERSONAL)
    top = commit(repo, "noted", author=ME)
    git(repo, "notes", "add", "-m", "review: ok", top)
    rc, out, err = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 0, out + err
    assert "1 note is copied to the new commit" in out and "✓ copied 1 note" in out
    assert git(repo, "rev-parse", "HEAD") != top
    assert git(repo, "notes", "show", "HEAD") == "review: ok"


def test_failed_rewrite_removes_the_undo_script(repo, capsys, monkeypatch, home):
    commit(repo, "bad", author=PERSONAL)
    before = git(repo, "rev-parse", "HEAD")

    def broken(*a, **k):
        raise audit.ConfigError("rewrite failed; the branch was not changed")
    monkeypatch.setattr(audit, "replay", broken)
    rc, out, err = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 2 and "rewrite failed; the branch was not changed" in err
    assert git(repo, "rev-parse", "HEAD") == before
    assert not list((home / ".local/state/gitpro").glob("audit*undo*.sh"))


def test_forbid_emails_judges_only_own_commits(repo, capsys, cfg):
    cfg(policy_config('forbid_emails = ["*@other.test"]'), apply=False)
    commit(repo, "their work", author=COLLEAGUE, trailers=["Co-Authored-By: Claude <noreply@anthropic.com>"])
    rc, data = audit_json(capsys, str(repo))
    assert rc == 0 and data["policy"]["violations"] == 0
    rc, out, _ = audit_text(capsys, str(repo))
    assert "✗" not in out


def test_policy_mark_only_with_violations(repo, capsys):
    commit(repo, "their work", author=COLLEAGUE, trailers=[CLAUDE])     # forbid, but not my commit
    rc, out, _ = audit_text(capsys, str(repo))
    assert rc == 0 and "policy work-github: forbid  ✓" in out and "✗" not in out


def test_rewrite_preview_says_commits_are_signed(repo, capsys, tmp_path):
    gpg = tmp_path / "fakegpg"
    gpg.write_text("#!/bin/sh\ncat >/dev/null\necho '[GNUPG:] SIG_CREATED ' >&2\n"
                   "printf -- '-----BEGIN PGP SIGNATURE-----\\nfake\\n-----END PGP SIGNATURE-----\\n'\n")
    gpg.chmod(0o755)
    git(repo, "config", "gpg.program", str(gpg))
    git(repo, "config", "commit.gpgsign", "true")
    commit(repo, "bad", author=PERSONAL)
    commit(repo, "after", author=ME)
    rc, out, err = audit_text(capsys, "--rewrite", "-y", str(repo))
    assert rc == 0, out + err
    assert "the 2 new commits are signed (commit.gpgsign = true)" in out
    assert b"\ngpgsig " in raw_commit(repo, "HEAD") and b"\ngpgsig " in raw_commit(repo, "HEAD~1")
    assert git(repo, "log", "-2", "--format=%ae %ce") == "me@corp.test me@corp.test\nme@corp.test me@corp.test"
