"""08 trailers: trailer policy per identity and the commit-msg guard entry (spec 08 §2.2, §2.3, tests 13-15)."""
from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG, TIMEOUT, git, repo_with_remote, run, set_git_hooks_sandbox, write_config
from gitpro import config, trailers

WORK = "git@github.com:corp-org/app.git"
CLAUDE = "Co-Authored-By: Claude <noreply@anthropic.com>"
REAL_GITPRO = """
import io
sys.stdin = io.TextIOWrapper(io.BytesIO(stdin))
sys.exit(__import__("gitpro.hot").hot.main(argv))
"""


def with_trailers(body: str, table: str = "identity") -> str:
    """DEFAULT_CONFIG with a trailers table: work-github's ([identity.trailers]) or [default.trailers]."""
    if table == "identity":
        return DEFAULT_CONFIG.replace('gh_account = "me-work"\n',
                                      f'gh_account = "me-work"\n\n[identity.trailers]\n{body}\n')
    return DEFAULT_CONFIG.replace('email = "me@default.test"\n',
                                  f'email = "me@default.test"\n\n[default.trailers]\n{body}\n')


def load(home: Path, text: str):
    return config.load_all(write_config(home, text))


def ident(c, id_: str):
    return next(i for i in c.identities if i.id == id_)


# ---------------------------------------------------------------- policy_for / check

def test_policy_for_identity_default_and_builtin(home):
    c = load(home, with_trailers('co_authored_by = "forbid"'))
    p = trailers.policy_for(ident(c, "work-github"), c.default)
    assert (p.co_authored_by, p.who, p.table) == ("forbid", "identity work-github", "[identity.trailers]")
    assert p.active()
    q = trailers.policy_for(ident(c, "personal"), c.default)
    assert q.co_authored_by == "allow" and not q.active()
    r = trailers.policy_for(None, c.default)
    assert r.who == "the [default] identity" and not r.active()


def test_policy_for_falls_back_to_default(home):
    text = with_trailers('co_authored_by = "require"\nforbid_emails = ["*@evil.test"]', table="default")
    c = load(home, text)
    p = trailers.policy_for(ident(c, "personal"), c.default)
    assert p.co_authored_by == "require" and p.table == "[default.trailers]"
    assert p.forbid_emails == ("*@evil.test",) and p.emails_table == "[default.trailers]"
    # the identity's own value wins over the default, field by field
    c2 = load(home, text.replace('gh_account = "me-work"\n',
                                 'gh_account = "me-work"\n\n[identity.trailers]\nco_authored_by = "allow"\n'))
    w = trailers.policy_for(ident(c2, "work-github"), c2.default)
    assert w.co_authored_by == "allow" and w.table == "[identity.trailers]"
    assert w.forbid_emails == ("*@evil.test",)


def test_check_rules():
    claude = [("Co-Authored-By", "Claude <noreply@anthropic.com>")]
    pat = [("co-authored-by", "Pat <pat@corp.test>")]
    forbid = trailers.Policy("forbid", (), "identity work-github", "[identity.trailers]")
    msgs = trailers.check(claude, forbid)
    assert msgs == ['Co-Authored-By: Claude <noreply@anthropic.com> is not allowed for identity work-github '
                    '([identity.trailers] co_authored_by = "forbid")']
    assert trailers.check(claude, trailers.Policy()) == []
    assert trailers.check([("Signed-off-by", "Me <me@corp.test>")], forbid) == []
    req = trailers.Policy("require", (), "identity work-github", "[identity.trailers]")
    assert trailers.check([], req) == ["identity work-github requires a Co-Authored-By trailer on every commit"]
    assert trailers.check(pat, req) == []
    emails = trailers.Policy("allow", ("*@corp.test",), "identity work-github", "", "[identity.trailers]")
    assert trailers.check(claude, emails) == []
    assert "forbid_emails has \"*@corp.test\"" in trailers.check(pat, emails)[0]


def test_glob_match_brackets_and_case():
    assert trailers.glob_match("*[bot]@users.noreply.github.com", "dependabot[bot]@users.noreply.github.com")
    assert trailers.glob_match("NoReply@Anthropic.com", "noreply@anthropic.COM")
    assert not trailers.glob_match("*[bot]@users.noreply.github.com", "me@corp.test")
    assert trailers.email_of("Claude <noreply@anthropic.com>") == "noreply@anthropic.com"
    assert trailers.email_of("plain@x.test") == "plain@x.test"


def test_parse_reads_trailers_like_git(tmp_path):
    f = tmp_path / "msg"
    f.write_text(f"subject\n\nbody line\n\n{CLAUDE}\nSigned-off-by: Me <me@corp.test>\n"
                 "# a comment: not a trailer\n")
    got = trailers.parse(f)
    assert ("Co-Authored-By", "Claude <noreply@anthropic.com>") in got
    assert ("Signed-off-by", "Me <me@corp.test>") in got
    assert trailers.parse_text("just a subject\n") == []


# ---------------------------------------------------------------- 13: hook_message(ctx)

@pytest.fixture
def ctx_for(home, cfg, tmp_path, monkeypatch):
    """ctx_for(message, trailers_body) -> (Context, said lines) for a work-github repo; report=None as guard
    passes it for commit-msg."""
    from gitpro import guard
    set_git_hooks_sandbox(monkeypatch, home)
    repo = repo_with_remote(tmp_path, "app", WORK)
    monkeypatch.chdir(repo)

    def make(message: str, body: str = 'co_authored_by = "forbid"'):
        path = cfg(with_trailers(body), apply=False)
        c = config.load_all(path)
        f = repo / ".git/COMMIT_EDITMSG"
        f.write_text(message)
        said = []
        ctx = guard.Context("commit-msg", repo, (".git/COMMIT_EDITMSG",), "", c, None, None, None, said.append)
        return ctx, said
    return make


def test_hook_message_forbidden_trailer_blocks(ctx_for):
    ctx, said = ctx_for(f"x\n\n{CLAUDE}\n")
    assert trailers.hook_message(ctx) == 3
    assert said[0] == ('✗ gitpro: Co-Authored-By: Claude <noreply@anthropic.com> is not allowed for identity '
                       'work-github ([identity.trailers] co_authored_by = "forbid")')
    assert "  fix:   remove the trailer from the message" in said
    text = "\n".join(said)
    assert "GITPRO_SKIP" not in text and "--no-verify" not in text     # DECISIONS Q6: no bypass for agents


def test_hook_message_allowed_passes(ctx_for):
    ctx, said = ctx_for("x\n\nSigned-off-by: Me <me@corp.test>\n")
    assert trailers.hook_message(ctx) == 0 and said == []
    ctx, said = ctx_for(f"x\n\n{CLAUDE}\n", body='co_authored_by = "allow"')
    assert trailers.hook_message(ctx) == 0 and said == []


def test_hook_message_require_without_trailers_blocks(ctx_for):
    ctx, said = ctx_for("x\n", body='co_authored_by = "require"')
    assert trailers.hook_message(ctx) == 3
    assert said[0] == "✗ gitpro: identity work-github requires a Co-Authored-By trailer on every commit"
    ctx, said = ctx_for(f"x\n\n{CLAUDE}\n", body='co_authored_by = "require"')
    assert trailers.hook_message(ctx) == 0


def test_hook_message_fails_open_when_git_is_missing(ctx_for, monkeypatch):
    ctx, said = ctx_for(f"x\n\n{CLAUDE}\n")

    def boom(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", boom)
    assert trailers.hook_message(ctx) == 0
    assert len(said) == 1 and said[0].startswith("⚠ gitpro trailers:") and "not blocking" in said[0]


def test_hook_message_fails_open_on_any_error(ctx_for):
    ctx, said = ctx_for(f"x\n\n{CLAUDE}\n")
    ctx = dataclasses.replace(ctx, args=())        # no message file
    assert trailers.hook_message(ctx) == 0
    assert said and said[0].startswith("⚠ gitpro trailers:")
    ctx2, said2 = ctx_for(f"x\n\n{CLAUDE}\n")
    ctx2 = dataclasses.replace(ctx2, args=("does/not/exist",))
    assert trailers.hook_message(ctx2) == 0 and said2[0].startswith("⚠")


def test_hook_message_warn_once(ctx_for, monkeypatch, tmp_path):
    ctx, said = ctx_for(f"x\n\n{CLAUDE}\n")
    ctx = dataclasses.replace(ctx, args=())
    once = tmp_path / "once"
    once.write_text("4242")
    monkeypatch.setenv("GITPRO_WARNED_FILE", str(once))
    monkeypatch.setenv("GITPRO_GIT_PID", "4242")
    assert trailers.hook_message(ctx) == 0 and said == []     # pre-commit already warned for this git command
    monkeypatch.setenv("GITPRO_GIT_PID", "1")
    assert trailers.hook_message(ctx) == 0 and len(said) == 1


def test_hook_message_warn_mode(ctx_for):
    ctx, said = ctx_for(f"x\n\n{CLAUDE}\n")
    ctx = dataclasses.replace(ctx, cfg=dataclasses.replace(ctx.cfg, guard=dataclasses.replace(ctx.cfg.guard, on_mismatch="warn")))
    assert trailers.hook_message(ctx) == 3
    assert said[0].startswith("⚠ gitpro: Co-Authored-By")
    assert said[-1] == '  (not blocking: [guard] on_mismatch = "warn")'


def test_hook_message_personal_repo_is_unaffected(ctx_for, tmp_path):
    ctx, said = ctx_for(f"x\n\n{CLAUDE}\n")
    git(ctx.repo, "remote", "set-url", "origin", "git@github.com:me/dotfiles.git")
    assert trailers.hook_message(ctx) == 0 and said == []


# ---------------------------------------------------------------- 14: through a real git commit

def _has_guard() -> bool:
    try:
        import gitpro.guard  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _has_guard(), reason="needs package 06 (gitpro.guard)")
def test_commit_msg_hook_end_to_end(home, cfg, fake_bin, monkeypatch, tmp_path, capsys):
    set_git_hooks_sandbox(monkeypatch, home)
    fake_bin.add("gitpro", REAL_GITPRO)
    cfg(with_trailers('co_authored_by = "forbid"'))
    assert run("guard", "install", "-y") == 0
    capsys.readouterr()
    repo = repo_with_remote(tmp_path, "app", WORK)
    git(repo, "checkout", "-q", "-b", "main")

    def gcommit(*extra: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "--no-gpg-sign", "-m", "x",
                               *extra], capture_output=True, text=True, env=dict(os.environ, **env),
                              timeout=TIMEOUT)

    r = gcommit("--trailer", CLAUDE)
    assert r.returncode != 0
    assert "is not allowed for identity work-github" in r.stderr
    assert "GITPRO_SKIP" not in r.stderr
    assert git(repo, "rev-list", "--all", "--count") == "0"
    ok = gcommit("--trailer", "Signed-off-by: Work Me <me@corp.test>")
    assert ok.returncode == 0, ok.stderr
    skipped = gcommit("--trailer", CLAUDE, GITPRO_SKIP="1")
    assert skipped.returncode == 0, skipped.stderr
    assert git(repo, "rev-list", "--count", "HEAD") == "2"


# ---------------------------------------------------------------- 15: hot path imports

def test_import_is_light():
    r = subprocess.run([sys.executable, "-X", "importtime", "-c", "import gitpro.trailers"],
                       capture_output=True, text=True, timeout=TIMEOUT)
    assert r.returncode == 0, r.stderr
    mods = {line.split("|")[-1].strip() for line in r.stderr.splitlines() if "|" in line}
    assert "gitpro.trailers" in mods
    for bad in ("rich", "gitpro.cli", "gitpro.view", "gitpro.resolve", "gitpro.audit"):
        assert bad not in mods, bad
