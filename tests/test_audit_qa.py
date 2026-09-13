"""QA findings for package 08 (independent review): each test fails on the reviewed code and documents one bug."""
from __future__ import annotations

import os
import subprocess

from conftest import commit, git, run
from gitpro import config, trailers
from test_audit import ME, PERSONAL, repo  # noqa: F401  (fixture)


def raw(r, rev: str) -> bytes:
    return subprocess.run(["git", "-C", str(r), "cat-file", "commit", rev], capture_output=True, check=True).stdout


def test_qa_default_bot_glob_does_not_match_human_noreply_emails(home, cfg):
    """'*[bot]@users.noreply.github.com' must not act as the class [bot]: a human whose GitHub noreply
    address ends in b, o or t is not an agent."""
    c = config.load_all(cfg(apply=False))
    for human in ("123+scott@users.noreply.github.com", "456+bob@users.noreply.github.com",
                  "789+leo@users.noreply.github.com"):
        assert not any(trailers.glob_match(g, human) for g in c.agents.bot_emails), human
    assert any(trailers.glob_match(g, "dependabot[bot]@users.noreply.github.com") for g in c.agents.bot_emails)


def test_qa_rewrite_signs_when_commit_gpgsign_is_set(repo, capsys, tmp_path):  # noqa: F811
    """Spec 08 §2.1: the replayed commits are signed when commit.gpgsign is set. git commit-tree ignores
    commit.gpgsign, so without -S every re-parented signed commit silently loses its signature."""
    gpg = tmp_path / "fakegpg"
    gpg.write_text("#!/bin/sh\ncat >/dev/null\necho '[GNUPG:] SIG_CREATED ' >&2\n"
                   "printf -- '-----BEGIN PGP SIGNATURE-----\\nfake\\n-----END PGP SIGNATURE-----\\n'\n")
    gpg.chmod(0o755)
    git(repo, "config", "gpg.program", str(gpg))
    git(repo, "config", "commit.gpgsign", "true")
    commit(repo, "bad", author=PERSONAL)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-S", "-m", "signed"], check=True,
                   capture_output=True, env=dict(os.environ, GIT_AUTHOR_NAME=ME[0], GIT_AUTHOR_EMAIL=ME[1],
                                                 GIT_COMMITTER_NAME=ME[0], GIT_COMMITTER_EMAIL=ME[1]))
    assert b"\ngpgsig " in raw(repo, "HEAD")
    assert run("audit", "--rewrite", "-y", str(repo)) == 0
    assert b"\ngpgsig " in raw(repo, "HEAD")


def test_qa_rewrite_keeps_descendant_identity_bytes(repo, capsys):  # noqa: F811
    """A re-parented descendant keeps author/committer byte for byte: a Latin-1 name (encoding header),
    a name git would strip ("Bob;") and a -0000 timezone, as written by tools other than git commit."""
    bad = commit(repo, "bad", author=PERSONAL)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    author = "author Andr\xe9 Bob; <c@other.test> 1700000000 -0000".encode("latin-1")
    committer = b"committer Colleague <c@other.test> 1700000000 +0530"
    body = (f"tree {tree}\nparent {bad}\n".encode() + author + b"\n" + committer +
            b"\nencoding ISO-8859-1\n\n" + "caf\xe9\n".encode("latin-1"))
    sha = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"], input=body,
                         capture_output=True, check=True).stdout.decode().strip()
    git(repo, "update-ref", "refs/heads/main", sha)
    assert run("audit", "--rewrite", "-y", str(repo)) == 0
    lines = raw(repo, "HEAD").split(b"\n")
    assert author in lines and committer in lines
    assert raw(repo, "HEAD").endswith(b"\n\n" + "caf\xe9\n".encode("latin-1"))
