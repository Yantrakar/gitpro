"""14 policy: the team policy file, `check --policy` (machine and --range/CI mode), the guard entries,
`team`, `policy show/lint` and the composite GitHub Action (spec 14 §11, tests 1-14)."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from conftest import TIMEOUT, git, repo_with_remote, run, set_git_hooks_sandbox, write_config
from gitpro import config, policy, resolve

ROOT = Path(__file__).resolve().parents[1]
APP = "git@github.com:example-corp/app.git"
OTHER = "git@github.com:someone-else/app.git"
ME = ("Me", "me@example.corp")
CLAUDE = "Co-Authored-By: Claude <noreply@anthropic.com>"
NAME = "Example Corp engineering"
CONTACT = "it-sec@example.corp"

POLICY = """\
# gitpro policy
[policy]
name = "Example Corp engineering"
version = 1
contact = "it-sec@example.corp"
{signers}
[[rule]]
host = "github.com"
owners = ["example-corp", "example-corp-*"]
allowed_email_domains = ["example.corp", "contractors.example.corp"]
allowed_emails = [{allowed}]
require_signing = {signing}
signing_formats = [{formats}]
co_authored_by = "forbid"
forbid_emails = ["*@users.noreply.github.com"]
require_verified_email = false
"""

CONFIG = """\
[default]
name = "Default Me"
email = "me@default.test"

[[identity]]
id = "work-github"
name = "Work Me"
email = "{email}"
host = "github.com"
owners = ["example-corp"]
ssh_key = "~/.ssh/id_work"
sign = {sign}
policy = "{policy}"
{trailers}
[[identity]]
id = "gitlab-work"
name = "Lab Me"
email = "me@lab.test"
host = "gitlab.com"
owners = ["lab"]
"""

REAL_GITPRO = """
import io
sys.stdin = io.TextIOWrapper(io.BytesIO(stdin))
sys.exit(__import__("gitpro.hot").hot.main(argv))
"""


def policy_text(*, signers: Optional[str] = None, allowed: Sequence[str] = (), signing: bool = True,
                formats: Sequence[str] = ("ssh",)) -> str:
    return POLICY.format(signers=f'allowed_signers = "{signers}"\n' if signers else "",
                         allowed=", ".join(f'"{a}"' for a in allowed), signing="true" if signing else "false",
                         formats=", ".join(f'"{f}"' for f in formats))


def policy_file(directory: Path, text: Optional[str] = None, *, name: str = "gitpro-policy.toml",
                mode: int = 0o600) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(policy_text() if text is None else text)
    path.chmod(mode)
    return path


def config_text(policy_path: str = "~/work/gitpro-policy.toml", *, email: str = "me@corp.test", sign: bool = False,
                trailers: str = "") -> str:
    return CONFIG.format(email=email, sign="true" if sign else "false", policy=policy_path,
                         trailers=f"\n[identity.trailers]\n{trailers}\n" if trailers else "")


@pytest.fixture(autouse=True)
def sandbox(home, tmp_path, monkeypatch):
    """The cwd is not a repo (the tests run inside gitpro's own checkout otherwise)."""
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    for v in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GITPRO_SKIP"):
        monkeypatch.delenv(v, raising=False)
    policy._CACHE.clear()
    return work


@pytest.fixture
def work_policy(home):
    """~/work/gitpro-policy.toml (0600) with the example rule."""
    return policy_file(home / "work")


def out_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- signing helpers

@pytest.fixture(scope="module")
def keys(tmp_path_factory) -> Dict[str, Path]:
    """Two ed25519 keys made by the real ssh-keygen: "good" (in allowed_signers) and "other"."""
    return make_keys(tmp_path_factory)


def make_keys(tmp_path_factory) -> Dict[str, Path]:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("needs ssh-keygen")
    d = tmp_path_factory.mktemp("keys")
    out = {}
    for name in ("good", "other"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{name}@test", "-f", str(d / name)],
                       check=True, capture_output=True, timeout=TIMEOUT)
        out[name] = d / name
    return out


def signers_file(directory: Path, key: Path, name: str = "allowed_signers") -> Path:
    pub = (key.parent / (key.name + ".pub")).read_text().split()
    path = directory / name
    path.write_text(f'me@example.corp namespaces="git" {pub[0]} {pub[1]}\n')
    return path


def _env(author: Tuple[str, str], committer: Optional[Tuple[str, str]], date: str) -> dict:
    committer = committer or author
    return dict(os.environ, GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1], GIT_COMMITTER_NAME=committer[0],
                GIT_COMMITTER_EMAIL=committer[1], GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)


def make_commit(repo: Path, msg: str, *, key: Optional[Path] = None, author: Tuple[str, str] = ME,
                committer: Optional[Tuple[str, str]] = None, trailers: Sequence[str] = (),
                date: str = "2026-10-01T10:00:00+00:00") -> str:
    sign = (["-c", "gpg.format=ssh", "-c", f"user.signingkey={key}.pub", "-c", "commit.gpgsign=true"]
            if key else ["-c", "commit.gpgsign=false"])
    full = msg + ("\n\n" + "\n".join(trailers) if trailers else "")
    subprocess.run(["git", "-C", str(repo), *sign, "commit", "-q", "--allow-empty", "-m", full],
                   env=_env(author, committer, date), check=True, capture_output=True, text=True, timeout=TIMEOUT)
    return git(repo, "rev-parse", "HEAD")


def app_repo(tmp_path: Path, url: str = APP, name: str = "app") -> Path:
    repo = repo_with_remote(tmp_path, name, url)
    git(repo, "checkout", "-q", "-b", "main")
    make_commit(repo, "base")
    git(repo, "tag", "base")
    return repo


def commit_to_base(repo: Path, *paths: str) -> None:
    """Commit paths (already written in the checkout) and move the tag base there: in --range mode the
    repo's policy is read from the range base."""
    git(repo, "add", "--", *paths)
    make_commit(repo, "policy")
    git(repo, "tag", "-f", "base")


def check_range(capsys, repo: Path, *extra: str, rng: str = "base..HEAD") -> Tuple[int, dict]:
    rc = run("check", *extra, "--range", rng, "--repo", str(repo), "--json")
    return rc, out_json(capsys)


# ---------------------------------------------------------------- 1: lint

def test_lint_example_is_ok(capsys, work_policy):
    assert run("policy", "lint", str(work_policy)) == 0
    assert "✓" in capsys.readouterr().out
    assert run("policy", "lint", str(work_policy), "--json") == 0
    data = out_json(capsys)
    assert data["ok"] is True and data["errors"] == [] and data["command"] == "policy"


def test_lint_reports_every_error(capsys, home):
    bad = policy_text().replace('co_authored_by = "forbid"', 'co_authored_by = "maybe"\ncolour = "blue"')
    p = policy_file(home / "work", bad)
    assert run("policy", "lint", str(p)) == 2
    err = capsys.readouterr().err
    assert "colour" in err and "unknown key" in err
    assert "co_authored_by" in err and "maybe" in err
    assert err.count("✗") == 2
    assert run("policy", "lint", str(p), "--json") == 2
    data = out_json(capsys)
    assert data["ok"] is False and len(data["errors"]) == 2


def test_lint_missing_allowed_signers(capsys, home):
    p = policy_file(home / "work", policy_text(signers="nowhere/allowed_signers"))
    assert run("policy", "lint", str(p)) == 2
    assert "allowed_signers" in capsys.readouterr().err
    with pytest.raises(config.ConfigError, match=r"allowed_signers .*nowhere/allowed_signers not found"):
        policy.load(p)


@pytest.mark.parametrize("snippet, words", [
    ('version = "one"', "version"),
    ('version = 2', "version"),
    ('signing_formats = ["pgp"]', "signing_formats"),
    ('allowed_email_domains = ["exa mple.corp"]', "allowed_email_domains"),
    ('host = "bad host"', "host"),
    ('owners = ["a b"]', "owners"),
    ('require_signing = "yes"', "require_signing"),
])
def test_load_validation(home, snippet, words):
    key = snippet.split(" =")[0]
    text = "\n".join(snippet if line.startswith(key + " =") else line for line in policy_text().splitlines())
    assert snippet in text
    p = policy_file(home / "work", text)
    with pytest.raises(config.ConfigError, match=re.escape(words)):
        policy.load(p)


def test_load_needs_a_rule_and_valid_toml(home):
    p = policy_file(home / "work", '[policy]\nname = "x"\n')
    with pytest.raises(config.ConfigError, match=r"\[\[rule\]\]"):
        policy.load(p)
    p.write_text("[policy\n")
    with pytest.raises(config.ConfigError, match=str(p)):
        policy.load(p)


def test_load_returns_the_policy(home, keys):
    (home / "work").mkdir()
    signers = signers_file(home / "work", keys["good"])
    p = policy_file(home / "work", policy_text(signers="allowed_signers"))
    pol = policy.load(p)
    assert pol.name == NAME and pol.contact == CONTACT and pol.version == 1
    assert pol.allowed_signers == str(signers)
    (rule,) = pol.rules
    assert rule.owners == ("example-corp", "example-corp-*") and rule.require_signing is True
    assert rule.co_authored_by == "forbid" and rule.signing_formats == ("ssh",)


# ---------------------------------------------------------------- 2: show / applicable

def test_show_matching_rule(capsys, cfg, work_policy, tmp_path):
    cfg(config_text(), apply=False)
    repo = app_repo(tmp_path)
    assert run("policy", "show", str(repo)) == 0
    out = capsys.readouterr().out
    assert NAME in out and "example-corp" in out
    assert run("policy", "show", str(repo), "--json") == 0
    data = out_json(capsys)
    assert len(data["policies"]) == 1 and len(data["policies"][0]["rules"]) == 1
    assert data["policies"][0]["rules"][0]["host"] == "github.com"


def test_show_other_owner(capsys, cfg, work_policy, tmp_path):
    cfg(config_text(), apply=False)
    repo = app_repo(tmp_path, OTHER, "other")
    assert run("policy", "show", str(repo)) == 0
    assert "no policy applies" in capsys.readouterr().out
    assert run("policy", "show", str(repo), "--json") == 0
    assert out_json(capsys)["policies"] == []


def test_applicable_policy_files_globs_and_subgroups(home):
    """[policy].files apply to every repo; rules match on host and owner globs (case-insensitive, subgroups)."""
    p = policy_file(home / "org")
    text = config_text().replace('policy = "~/work/gitpro-policy.toml"\n', "") + \
        '\n[policy]\nfiles = ["~/org/gitpro-policy.toml"]\n'
    c = config.load_all(write_config(home, text))
    ((pol, rules),) = policy.applicable(APP, c)
    assert pol.path == str(p) and len(rules) == 1
    for url, n in [(APP, 1), ("https://github.com/Example-Corp-Labs/x.git", 1),
                   ("git@github.com:example-corp/sub/x.git", 1), (OTHER, 0), ("git@gitlab.com:example-corp/x.git", 0)]:
        found = policy.applicable(url, c)
        assert sum(len(rules) for _, rules in found) == n, url


def test_applicable_identity_policy(home, work_policy, tmp_path):
    c = config.load_all(write_config(home, config_text()))
    assert len(policy.applicable(APP, c)) == 1
    assert policy.applicable(OTHER, c) == []
    repo = app_repo(tmp_path)
    ((pol, _),) = policy.applicable(repo, c)
    assert pol.name == NAME


# ---------------------------------------------------------------- 3 / 4: machine mode

def test_machine_mode_sign_false(capsys, cfg, work_policy):
    cfg(config_text(email="me@example.corp"), apply=False)
    assert run("check", "--policy", str(work_policy)) == 1
    out = capsys.readouterr().out
    assert f"policy {NAME} (~/work/gitpro-policy.toml) for work-github" in out
    assert "✗ identity work-github has sign = false → set sign = true, run gitpro setup" in out
    assert "⚠ not checked (run gitpro team --online" in out
    assert "1 problem" in out
    assert run("check", "--policy", str(work_policy), "--json") == 1
    data = out_json(capsys)
    assert data["mode"] == "machine" and data["ok"] is False
    (ident,) = data["identities"]
    assert ident["id"] == "work-github" and ident["policy"] == NAME
    checks = {c["name"]: c for c in ident["checks"]}
    assert checks["signing"]["ok"] is False and "sign = true" in checks["signing"]["fix"]


def test_machine_mode_compliant(capsys, cfg, work_policy):
    cfg(config_text(email="me@example.corp", sign=True, trailers='co_authored_by = "forbid"'), apply=False)
    rc = run("check", "--policy", str(work_policy))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "✓" in out.split("signing", 1)[1].splitlines()[0]
    assert '✓ [identity.trailers] co_authored_by = "forbid"' in out


def test_machine_mode_from_identity_policy_and_repo(capsys, cfg, work_policy, tmp_path):
    """No --policy: the identity's own policy; --repo picks the identity of the repo."""
    cfg(config_text(email="me@example.corp"), apply=False)
    repo = app_repo(tmp_path)
    assert run("check", "--repo", str(repo)) == 1
    out = capsys.readouterr().out
    assert "sign = false" in out
    assert 'policy forbids Co-Authored-By but the identity allows it' in out
    other = app_repo(tmp_path, OTHER, "other")
    assert run("check", "--repo", str(other)) == 0
    assert "no policy applies" in capsys.readouterr().out


def test_machine_mode_email_domain(capsys, cfg, home):
    policy_file(home / "work")
    cfg(config_text(sign=True), apply=False)
    assert run("check", "--policy", str(home / "work/gitpro-policy.toml"), "--json") == 1
    checks = {c["name"]: c for c in out_json(capsys)["identities"][0]["checks"]}
    assert checks["email domain"]["ok"] is False
    assert "corp.test" in checks["email domain"]["message"]
    policy_file(home / "work", policy_text(allowed=["me@corp.test"]))
    assert run("check", "--policy", str(home / "work/gitpro-policy.toml")) == 0
    out = capsys.readouterr().out
    assert "me@corp.test" in out and "✗" not in out


def test_missing_policy_file(capsys, cfg, home):
    cfg(config_text(), apply=False)
    assert run("check", "--policy", "/nonexistent/p.toml") == 2
    err = capsys.readouterr().err
    assert "✗ policy /nonexistent/p.toml: not found" in err


def test_machine_mode_missing_identity_policy(capsys, cfg, home):
    cfg(config_text("~/work/nope.toml"), apply=False)
    assert policy.check_machine(config.load_all(home / ".config/gitpro.toml"))   # problems, never raises
    assert run("check", "--policy", str(policy_file(home / "x"))) == 2
    err = capsys.readouterr().err
    assert "✗ policy ~/work/nope.toml: not found (identity work-github)" in err
    assert run("team") == 2
    assert "not found (identity work-github)" in capsys.readouterr().err


# ---------------------------------------------------------------- 5: range mode

def test_range_mode_four_commits(capsys, home, tmp_path, keys):
    d = tmp_path / "org"
    d.mkdir()
    signers_file(d, keys["good"])
    p = policy_file(d, policy_text(signers="allowed_signers"))
    repo = app_repo(tmp_path)
    a1 = make_commit(repo, "a1", key=keys["good"], author=("Pat", "pat@gmail.com"), committer=ME)
    b2 = make_commit(repo, "b2")
    c3 = make_commit(repo, "c3", key=keys["good"], trailers=[CLAUDE])
    d4 = make_commit(repo, "d4", key=keys["good"], date="2026-10-02T10:00:00+00:00")
    rc, data = check_range(capsys, repo, "--policy", str(p))
    assert rc == 1
    assert data["mode"] == "policy" and data["commits"] == 4 and data["contact"] == CONTACT
    assert data["range"] == "base..HEAD" and data["remote"] == APP
    assert data["policies"] == [{"name": NAME, "file": str(p), "rules": 1}]
    got = sorted((v["sha"], v["rule"], v["which"]) for v in data["violations"])
    assert got == sorted([(a1, "allowed_email_domains", "author"), (b2, "require_signing", "signature"),
                          (c3, "co_authored_by", "trailer")])
    sig = {s["sha"]: s for s in data["signing"]}
    assert sig[d4]["status"] == "G" and sig[d4]["signed"] is True and sig[d4]["warning"] is None
    assert sig[b2]["status"] == "N" and sig[b2]["signed"] is False
    v = next(v for v in data["violations"] if v["sha"] == a1)
    assert v["author_email"] == "pat@gmail.com" and v["committer_email"] == "me@example.corp"
    assert v["message"] == "email domain gmail.com not allowed (example.corp, contractors.example.corp)"

    rc = run("check", "--policy", str(p), "--range", "base..HEAD", "--repo", str(repo))
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith(f"policy {NAME} for github.com/example-corp/app, 4 commits in base..HEAD\n")
    assert f"{a1[:7]}  2026-10-01  pat@gmail.com" in out
    assert "✗ not signed (require_signing)" in out
    assert "Co-Authored-By: Claude <noreply@anthropic.com>" in out
    assert f"{d4[:7]}  2026-10-02" in out and "✓ signed (ssh, good signature)" in out
    assert out.rstrip().endswith(f"3 of 4 commits violate the policy; contact {CONTACT}")


def test_range_committer_violation_names_it(capsys, tmp_path, home):
    p = policy_file(tmp_path / "org", policy_text(signing=False, formats=()))
    repo = app_repo(tmp_path)
    sha = make_commit(repo, "x", author=ME, committer=("Bot", "bot@users.noreply.github.com"))
    rc, data = check_range(capsys, repo, "--policy", str(p))
    assert rc == 1
    hits = [(v["sha"], v["rule"], v["which"]) for v in data["violations"]]
    assert (sha, "forbid_emails", "committer") in hits
    assert data["signing"] == []


def test_range_clean_and_empty(capsys, tmp_path, home):
    p = policy_file(tmp_path / "org", policy_text(signing=False, formats=()))
    repo = app_repo(tmp_path)
    make_commit(repo, "fine")
    rc, data = check_range(capsys, repo, "--policy", str(p))
    assert rc == 0 and data["ok"] is True and data["violations"] == [] and data["commits"] == 1
    assert run("check", "--policy", str(p), "--range", "HEAD..HEAD", "--repo", str(repo)) == 0
    assert "✓ no commits in HEAD..HEAD" in capsys.readouterr().out
    for bad in ("..", "", "--output=/tmp/x"):
        assert run("check", "--policy", str(p), f"--range={bad}", "--repo", str(repo)) == 2
        capsys.readouterr()


# ---------------------------------------------------------------- 6 / 7 / 8: CI mode, policy discovery

def test_ci_mode_without_config(capsys, home, tmp_path):
    p = policy_file(tmp_path / "checkout", policy_text(signing=False, formats=()), mode=0o644)
    assert not (home / ".config/gitpro.toml").exists()
    repo = app_repo(tmp_path)
    make_commit(repo, "ok")
    rc, data = check_range(capsys, repo, "--policy", str(p))
    assert rc == 0, data
    assert data["policies"][0]["file"] == str(p)


def test_world_readable_policy_refused_in_machine_mode(capsys, cfg, home):
    p = policy_file(home / "work", mode=0o664)
    cfg(config_text(), apply=False)
    assert run("check", "--policy", str(p)) == 2
    err = capsys.readouterr().err
    assert "must be owned by you and not group/world-writable" in err and f"chmod 600 {p}" in err


def test_repo_policy_file_found(capsys, tmp_path, home):
    repo = app_repo(tmp_path)
    policy_file(repo, policy_text(signing=False, formats=()), name=".gitpro-policy.toml", mode=0o644)
    commit_to_base(repo, ".gitpro-policy.toml")
    make_commit(repo, "x", author=("Pat", "pat@gmail.com"))
    rc, data = check_range(capsys, repo)
    assert rc == 1
    assert data["warnings"] == []
    assert data["policies"][0]["file"] == str(repo / ".gitpro-policy.toml")
    assert data["violations"][0]["rule"] == "allowed_email_domains"
    assert data["violations"][0]["which"] == "both"


def test_range_no_policy_applies(capsys, tmp_path, home):
    repo = app_repo(tmp_path)
    make_commit(repo, "x")
    assert run("check", "--range", "base..HEAD", "--repo", str(repo)) == 2
    err = capsys.readouterr().err
    assert f"✗ no policy applies to {APP}; pass --policy FILE or add .gitpro-policy.toml" in err
    p = policy_file(tmp_path / "org")
    other = app_repo(tmp_path, OTHER, "other")
    assert run("check", "--policy", str(p), "--range", "base..HEAD", "--repo", str(other), "--json") == 2
    assert "no policy applies" in out_json(capsys)["error"]


# ---------------------------------------------------------------- 9: the %G? table

def test_signature_states(capsys, tmp_path, home, keys):
    org = tmp_path / "org"
    org.mkdir()
    signers_file(org, keys["good"])
    open_policy = policy_file(org, policy_text(), name="open.toml")
    strict = policy_file(org, policy_text(signers="allowed_signers"), name="strict.toml")
    gpg_only = policy_file(org, policy_text(signers="allowed_signers", formats=("gpg",)), name="gpg.toml")
    repo = app_repo(tmp_path)

    unknown = make_commit(repo, "signed by a key nobody lists", key=keys["other"])
    rc, data = check_range(capsys, repo, "--policy", str(open_policy), rng=f"base..{unknown}")
    assert rc == 0, data
    (s,) = data["signing"]
    assert s["status"] == "E" and s["signed"] is True and s["warning"] == "unverifiable here"
    rc = run("check", "--policy", str(open_policy), "--range", f"base..{unknown}", "--repo", str(repo))
    assert rc == 0 and "⚠ unverifiable here" in capsys.readouterr().out
    rc, data = check_range(capsys, repo, "--policy", str(strict), rng=f"base..{unknown}")
    assert rc == 1 and data["violations"][0]["rule"] == "require_signing"
    assert data["signing"][0]["status"] == "E"

    git(repo, "reset", "-q", "--hard", "base")
    unsigned = make_commit(repo, "plain")
    rc, data = check_range(capsys, repo, "--policy", str(open_policy), rng=f"base..{unsigned}")
    assert rc == 1 and data["violations"][0]["message"] == "not signed (require_signing)"

    git(repo, "reset", "-q", "--hard", "base")
    good = make_commit(repo, "good", key=keys["good"])
    raw = subprocess.run(["git", "-C", str(repo), "cat-file", "commit", good], capture_output=True,
                         check=True, timeout=TIMEOUT).stdout
    forged = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"],
                            input=raw.replace(b"\ngood\n", b"\nevil\n"), capture_output=True, check=True,
                            timeout=TIMEOUT).stdout.decode().strip()
    assert forged != good
    rc, data = check_range(capsys, repo, "--policy", str(strict), rng=f"base..{good}")
    assert rc == 0 and data["signing"][0]["status"] == "G"
    rc, data = check_range(capsys, repo, "--policy", str(strict), rng=f"base..{forged}")
    assert rc == 1 and data["signing"][0]["status"] == "B" and data["signing"][0]["signed"] is False

    rc, data = check_range(capsys, repo, "--policy", str(gpg_only), rng=f"base..{good}")
    assert rc == 1
    assert data["violations"][0]["rule"] == "signing_formats"
    assert data["violations"][0]["message"] == "format ssh not allowed (gpg)"


def test_signer_must_be_the_committer(capsys, tmp_path, home, keys):
    """git does not bind the allowed_signers principal to the committer: gitpro does."""
    org = tmp_path / "org"
    org.mkdir()
    signers_file(org, keys["good"])
    strict = policy_file(org, policy_text(signers="allowed_signers"), name="strict.toml")
    repo = app_repo(tmp_path)
    c = make_commit(repo, "my key, someone else's name", key=keys["good"], author=("Al", "al@example.corp"))
    rc, data = check_range(capsys, repo, "--policy", str(strict), rng=f"base..{c}")
    assert rc == 1 and data["signing"][0]["status"] == "G"
    (v,) = data["violations"]
    assert v["rule"] == "allowed_signers"
    assert v["message"] == "signed by me@example.corp (allowed_signers), but committed as al@example.corp"
    assert policy.principal_ok("*@example.corp,!al@example.corp", "me@EXAMPLE.corp")
    assert not policy.principal_ok("*@example.corp,!al@example.corp", "al@example.corp")
    assert not policy.principal_ok("", "me@example.corp")


def test_signer_principals_parses_allowed_signers(tmp_path, keys):
    good = (keys["good"].parent / "good.pub").read_text().split()
    other = (keys["other"].parent / "other.pub").read_text().split()
    fp = subprocess.run(["ssh-keygen", "-lf", str(keys["good"]) + ".pub"], capture_output=True, text=True,
                        check=True, timeout=TIMEOUT).stdout.split()[1]
    assert policy.ssh_fingerprint(good[1]) == fp
    f = tmp_path / "allowed_signers"
    f.write_text("# comment\n\n"
                 f'"alice@x.test,me@x.test" namespaces="git,file",valid-after="20200101" {good[0]} {good[1]} c\n'
                 f"bob@x.test {good[0]} {good[1]}\n"
                 f'mallory@x.test namespaces="file" {good[0]} {good[1]}\n'
                 f"ca@x.test cert-authority {other[0]} {other[1]}\n"
                 "broken line\n")
    got = policy.signer_principals([str(f)])
    assert got == {fp: ["alice@x.test", "me@x.test", "bob@x.test"]}
    assert policy.principal_ok(got[fp], "bob@x.test") and not policy.principal_ok(got[fp], "mallory@x.test")


def test_gpg_signature_message_and_unverifiable_summary(capsys, tmp_path, home, keys):
    rule = policy.Rule(host="github.com", require_signing=True)
    assert policy.signing_problems([rule], "E", "gpg", True) == [
        ("require_signing", "gpg signature cannot be checked: the policy's allowed_signers lists ssh keys only")]
    assert policy.signing_problems([rule], "E", "ssh", True)[0][1] == \
        "signed by a key that is not in the policy's allowed_signers"
    assert policy.signing_problems([rule], "E", "gpg", False) == []
    org = tmp_path / "org"
    open_policy = policy_file(org, policy_text())
    repo = app_repo(tmp_path)
    c = make_commit(repo, "signed by a key nobody lists", key=keys["other"])
    rc, data = check_range(capsys, repo, "--policy", str(open_policy), rng=f"base..{c}")
    assert rc == 0 and data["unverifiable"] == 1
    assert run("check", "--policy", str(open_policy), "--range", f"base..{c}", "--repo", str(repo)) == 0
    assert "⚠ 1 signature could not be verified in this environment" in capsys.readouterr().out


def test_owner_glob_star_stays_in_one_segment(home):
    def rule(*owners: str) -> policy.Rule:
        return policy.Rule(host="gitlab.com", owners=owners)
    assert policy._owner_ok(("corp",), "corp/team/app")             # a group covers its subgroups
    assert policy._owner_ok(("corp/*",), "corp/team/sub")
    assert policy._owner_ok(("CORP-*",), "corp-eu")
    assert not policy._owner_ok(("corp*sub",), "corp/team/sub")     # * does not cross /
    assert policy._owner_ok(("corp/**/sub",), "corp/team/sub")
    assert not policy._owner_ok(("corp-*",), "corp")
    assert rule("corp-*").owners == ("corp-*",)


def test_range_policy_added_by_the_range_is_not_enforced(capsys, tmp_path, home):
    repo = app_repo(tmp_path)
    policy_file(repo, policy_text(signing=False, formats=()), name=".gitpro-policy.toml", mode=0o644)
    git(repo, "add", ".gitpro-policy.toml")
    make_commit(repo, "add a policy", author=("Pat", "pat@gmail.com"))
    rc, data = check_range(capsys, repo)
    assert rc == 0 and data["ok"] is True and data["policies"] == []
    assert data["warnings"] == [".gitpro-policy.toml: policy added in this range; not enforced until merged"]
    assert run("check", "--range", "base..HEAD", "--repo", str(repo)) == 0
    out, err = capsys.readouterr()
    assert "no policy enforced for 1 commit in base..HEAD" in out
    assert "⚠ .gitpro-policy.toml: policy added in this range; not enforced until merged" in err


def test_range_policy_judged_by_the_base_version(capsys, tmp_path, home, keys):
    repo = app_repo(tmp_path)
    signers_file(repo, keys["good"])
    text = policy_text(signers="allowed_signers")
    policy_file(repo, text, name=".gitpro-policy.toml", mode=0o644)
    commit_to_base(repo, ".gitpro-policy.toml", "allowed_signers")
    # the range lists its own key and widens the domains: neither counts
    signers_file(repo, keys["other"])
    policy_file(repo, text.replace('"contractors.example.corp"', '"contractors.example.corp", "gmail.com"'),
                name=".gitpro-policy.toml", mode=0o644)
    git(repo, "add", "-A")
    make_commit(repo, "widen", author=("Pat", "pat@gmail.com"), committer=ME, key=keys["other"])
    rc, data = check_range(capsys, repo)
    assert rc == 1
    assert {v["rule"] for v in data["violations"]} == {"allowed_email_domains", "require_signing"}
    assert data["policies"][0]["file"] == str(repo / ".gitpro-policy.toml")
    base = git(repo, "rev-parse", "base")[:7]
    assert data["warnings"] == [f"this range changes {f}; it is judged by the base version ({base})"
                                for f in (".gitpro-policy.toml", "allowed_signers")]
    # --policy inside the repo follows the same rule; outside it is read as given
    rc, data = check_range(capsys, repo, "--policy", str(repo / ".gitpro-policy.toml"))
    assert rc == 1 and len(data["warnings"]) == 2
    outside = policy_file(tmp_path / "org", (repo / ".gitpro-policy.toml").read_text().replace(
        'allowed_signers = "allowed_signers"', f'allowed_signers = "{repo / "allowed_signers"}"'))
    rc, data = check_range(capsys, repo, "--policy", str(outside))
    assert rc == 0 and data["warnings"] == [], data


def test_range_base_policy_symlinks_and_paths_refused(capsys, tmp_path, home):
    repo = app_repo(tmp_path)
    secret = policy_file(tmp_path / "elsewhere", policy_text(signing=False, formats=()))
    (repo / ".gitpro-policy.toml").symlink_to(secret)
    commit_to_base(repo, ".gitpro-policy.toml")
    make_commit(repo, "x")
    assert run("check", "--range", "base..HEAD", "--repo", str(repo)) == 2
    assert "is a symlink in" in capsys.readouterr().err
    assert run("policy", "show", str(repo)) == 2
    assert "leaves the repo (a symlink?); refused" in capsys.readouterr().err

    repo2 = app_repo(tmp_path, name="app2")
    policy_file(repo2, policy_text(signers="~/allowed_signers"), name=".gitpro-policy.toml", mode=0o644)
    commit_to_base(repo2, ".gitpro-policy.toml")
    make_commit(repo2, "x")
    assert run("check", "--range", "base..HEAD", "--repo", str(repo2)) == 2
    assert "allowed_signers must be a path inside the repo" in capsys.readouterr().err

    assert run("check", "--range", "HEAD", "--repo", str(repo2)) == 2
    assert "has no single base commit" in capsys.readouterr().err


def test_range_broken_config_warns_and_uses_the_repo_policy(capsys, tmp_path, home):
    repo = app_repo(tmp_path)
    policy_file(repo, policy_text(signing=False, formats=()), name=".gitpro-policy.toml", mode=0o644)
    commit_to_base(repo, ".gitpro-policy.toml")
    make_commit(repo, "x", author=("Pat", "pat@gmail.com"))
    write_config(home, "[default\n")
    rc, data = check_range(capsys, repo)
    assert rc == 1 and data["violations"][0]["rule"] == "allowed_email_domains"
    assert data["warnings"] and data["warnings"][0].startswith("config not used: ")


# ---------------------------------------------------------------- 10: hook entries

def _ctx(c, repo: Path, *, hook: str = "pre-commit", committer: Optional[str] = None,
         author: Optional[str] = None, args: Tuple[str, ...] = (), stdin: str = "", report=True):
    from gitpro import guard
    said: List[str] = []
    rep = resolve.compare(repo, c) if report and hook != "commit-msg" else None
    return guard.Context(hook, repo, args, stdin, c, rep, committer, author, said.append), said


@pytest.fixture
def git_calls(monkeypatch):
    calls: List[List[str]] = []
    real = subprocess.run

    def counting(argv, *a, **kw):
        if argv and argv[0] == "git":
            calls.append(list(argv))
        return real(argv, *a, **kw)
    monkeypatch.setattr(subprocess, "run", counting)
    return calls


def test_hook_commit(home, work_policy, tmp_path, git_calls):
    pytest.importorskip("gitpro.guard")
    c = config.load_all(write_config(home, config_text(email="me@example.corp")))
    repo = app_repo(tmp_path)
    ctx, said = _ctx(c, repo, committer="x@gmail.com", author="me@example.corp")
    git_calls.clear()
    assert policy.hook_commit(ctx) == 3
    assert said[0].startswith("✗ gitpro: ") and "x@gmail.com" in said[0]
    assert f"  cause: policy: {NAME} ({CONTACT})" in said
    assert not any("GITPRO_SKIP" in line for line in said)
    ctx, said = _ctx(c, repo, committer="me@example.corp", author="me@example.corp")
    assert policy.hook_commit(ctx) == 0 and said == []
    other = app_repo(tmp_path, OTHER, "other")
    ctx, said = _ctx(c, other, committer="x@gmail.com", author="x@gmail.com")
    git_calls.clear()
    assert policy.hook_commit(ctx) == 0 and said == []
    assert git_calls == []


def test_hook_commit_warn_mode_and_errors(home, work_policy, tmp_path):
    pytest.importorskip("gitpro.guard")
    c = config.load_all(write_config(home, config_text(email="me@example.corp")))
    c = dataclasses.replace(c, guard=dataclasses.replace(c.guard, on_mismatch="warn"))
    repo = app_repo(tmp_path)
    ctx, said = _ctx(c, repo, committer="x@gmail.com")
    assert policy.hook_commit(ctx) == 3
    assert said[0].startswith("⚠ gitpro: ") and '  (not blocking: [guard] on_mismatch = "warn")' in said
    work_policy.write_text("[policy\n")
    ctx, said = _ctx(c, repo, committer="x@gmail.com")
    assert policy.hook_commit(ctx) == 0
    assert len(said) == 1 and said[0].startswith("⚠ gitpro policy: ") and said[0].endswith("; not blocking")


def test_hook_message(home, work_policy, tmp_path):
    pytest.importorskip("gitpro.guard")
    c = config.load_all(write_config(home, config_text(email="me@example.corp")))
    repo = app_repo(tmp_path)
    msg = tmp_path / "MSG"
    msg.write_text(f"subject\n\n{CLAUDE}\n")
    ctx, said = _ctx(c, repo, hook="commit-msg", args=(str(msg),))
    assert policy.hook_message(ctx) == 3
    assert any("Co-Authored-By: Claude" in line for line in said)
    assert f"  cause: policy: {NAME} ({CONTACT})" in said
    msg.write_text("subject\n\nSigned-off-by: Me <me@example.corp>\n")
    ctx, said = _ctx(c, repo, hook="commit-msg", args=(str(msg),))
    assert policy.hook_message(ctx) == 0 and said == []


def test_hook_push(home, tmp_path, keys, bare_remote):
    pytest.importorskip("gitpro.guard")
    policy_file(home / "work")
    c = config.load_all(write_config(home, config_text(email="me@example.corp")))
    repo = app_repo(tmp_path)
    git(repo, "update-ref", "refs/remotes/origin/main", "base")
    sha = make_commit(repo, "unsigned")
    stdin = f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n"
    ctx, said = _ctx(c, repo, hook="pre-push", args=("origin", APP), stdin=stdin)
    assert policy.hook_push(ctx) == 3
    assert any("not signed" in line for line in said)
    git(repo, "reset", "-q", "--hard", "base")
    sha = make_commit(repo, "signed", key=keys["good"])
    stdin = f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n"
    ctx, said = _ctx(c, repo, hook="pre-push", args=("origin", APP), stdin=stdin)
    assert policy.hook_push(ctx) == 0, said


def test_hooks_return_at_once_without_any_policy(home, tmp_path, git_calls):
    pytest.importorskip("gitpro.guard")
    text = config_text().replace('policy = "~/work/gitpro-policy.toml"\n', "")
    c = config.load_all(write_config(home, text))
    repo = app_repo(tmp_path)
    msg = tmp_path / "MSG"
    msg.write_text(f"x\n\n{CLAUDE}\n")
    ctx, said = _ctx(c, repo, hook="commit-msg", args=(str(msg),))
    git_calls.clear()
    assert policy.hook_message(ctx) == 0 and policy.hook_commit(ctx) == 0 and policy.hook_push(ctx) == 0
    assert git_calls == [] and said == []


# ---------------------------------------------------------------- 11: through a real git commit

def _has_guard() -> bool:
    try:
        import gitpro.guard  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _has_guard(), reason="needs package 06 (gitpro.guard)")
def test_guard_blocks_a_commit(home, cfg, fake_bin, monkeypatch, tmp_path, capsys):
    set_git_hooks_sandbox(monkeypatch, home)
    fake_bin.add("gitpro", REAL_GITPRO)
    policy_file(home / "work")
    cfg(config_text(email="me@example.corp"))
    assert run("guard", "install", "-y") == 0
    capsys.readouterr()
    repo = repo_with_remote(tmp_path, "app", APP)

    def gcommit(*pre: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), *pre, "commit", "--allow-empty", "--no-gpg-sign", "-m", "x"],
                              capture_output=True, text=True, env=dict(os.environ, **env), timeout=TIMEOUT)

    r = gcommit("-c", "user.email=x@gmail.com")
    assert r.returncode != 0
    assert f"policy: {NAME} ({CONTACT})" in r.stderr
    assert git(repo, "rev-list", "--all", "--count") == "0"
    ok = gcommit()
    assert ok.returncode == 0, ok.stderr
    skipped = gcommit("-c", "user.email=x@gmail.com", GITPRO_SKIP="1")
    assert skipped.returncode == 0, skipped.stderr


# ---------------------------------------------------------------- 12: team

def test_team(capsys, cfg, work_policy):
    cfg(config_text(email="me@example.corp"), apply=False)
    assert run("team") == 1
    out = capsys.readouterr().out
    assert out.startswith("team status (this machine)\n")
    row = next(line for line in out.splitlines() if line.strip().startswith("work-github"))
    cols = row.split()
    assert NAME.split()[0] in row
    # email ✓, signing ✗, key registered ⚠ (--online)
    assert "✓" in cols and "✗" in cols and "⚠" in row
    assert row.index("✓") < row.index("✗")
    lab = next(line for line in out.splitlines() if line.strip().startswith("gitlab-work"))
    assert "(none)" in lab
    assert "guard:" in out and "agent hook:" in out
    hint = next(line for line in out.splitlines() if "violates its policy" in line).split("→ ", 1)[1]
    assert hint == "gitpro check --policy ~/work/gitpro-policy.toml"
    # the suggested command parses and shows the details (expanded the way a shell would)
    argv = [os.path.expanduser(a) for a in shlex.split(hint)[1:]]
    assert run(*argv) == 1
    assert "has sign = false" in capsys.readouterr().out


def test_team_json_optional_modules(capsys, cfg, work_policy, monkeypatch):
    cfg(config_text(email="me@example.corp", sign=True, trailers='co_authored_by = "forbid"'), apply=False)
    assert run("team", "--json") == 0
    data = out_json(capsys)
    assert data["command"] == "team" and data["ok"] is True
    assert "guard" in data and "agents" in data
    ids = {i["id"]: i for i in data["identities"]}
    assert ids["work-github"]["policy"] == NAME and ids["gitlab-work"]["policy"] is None
    monkeypatch.setitem(sys.modules, "gitpro.guard", None)
    monkeypatch.setitem(sys.modules, "gitpro.agents", None)
    assert run("team", "--json") == 0
    data = out_json(capsys)
    assert data["guard"] is None and data["agents"] is None
    assert run("team") == 0
    out = capsys.readouterr().out
    assert "guard: · not installed" in out and "agent hook: · not installed" in out


def test_team_online_without_cli(capsys, cfg, work_policy, monkeypatch):
    from gitpro import forge
    cfg(config_text(email="me@example.corp"), apply=False)
    monkeypatch.setattr(forge, "available", lambda cli: False)
    assert run("team", "--online") == 2
    assert "gh" in capsys.readouterr().err


# ---------------------------------------------------------------- 13: the GitHub Action

def _steps(text: str) -> List[Dict[str, object]]:
    """The steps of a composite action: per step its scalar keys, `env` (dict) and `run` (block text).
    A tiny reader for this one file (no PyYAML): `- ` items under `steps:`, `key: value`, `key: |`."""
    lines = text.splitlines()
    start = next(n for n, line in enumerate(lines) if line.strip() == "steps:")
    steps: List[Dict[str, object]] = []
    cur: Dict[str, object] = {}
    item_indent = None
    n = start + 1
    while n < len(lines):
        line = lines[n]
        if not line.strip():
            n += 1
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if body.startswith("- "):
            item_indent = indent
            cur = {}
            steps.append(cur)
            body = body[2:]
            indent += 2
        key, _, value = body.partition(":")
        value = value.strip()
        if value == "|":
            block = []
            n += 1
            while n < len(lines) and (not lines[n].strip() or len(lines[n]) - len(lines[n].lstrip()) > indent):
                block.append(lines[n])
                n += 1
            cut = min(len(b) - len(b.lstrip()) for b in block if b.strip())
            cur[key] = "\n".join(b[cut:] for b in block).strip("\n") + "\n"
            continue
        if not value:   # a nested map (env:, with:)
            sub: Dict[str, str] = {}
            n += 1
            while n < len(lines) and (not lines[n].strip() or len(lines[n]) - len(lines[n].lstrip()) > indent):
                k, _, v = lines[n].strip().partition(":")
                if k:
                    sub[k] = v.strip()
                n += 1
            cur[key] = sub
            continue
        cur[key] = value
        n += 1
        assert item_indent is not None
    return steps


def test_action_yml_structure():
    text = (ROOT / "action.yml").read_text()
    for needle in ("using: composite", 'python3 -m venv "$RUNNER_TEMP/gitpro"',
                   'bin/pip" install --quiet "$GITHUB_ACTION_PATH"', 'bin/gitpro" check --policy',
                   "is-shallow-repository", "fetch-depth", "default: .gitpro-policy.toml"):
        assert needle in text, needle
    steps = _steps(text)
    runs = [s["run"] for s in steps if "run" in s]
    assert len(runs) == 2
    for r in runs:
        assert "${{" not in str(r)
    blocks = [m.group(2) for m in re.finditer(r"^([ \t]*)(?:- )?run: \|\n((?:\1[ \t]+\S.*\n|[ \t]*\n)*)", text,
                                              re.MULTILINE)]
    assert len(blocks) == 2 and all("python3" in b or "gitpro" in b for b in blocks)
    assert not any(re.search(r"\$\{\{\s*inputs\.", b) for b in blocks)
    assert steps[1]["env"] == {"GITPRO_POLICY": "${{ inputs.policy }}", "GITPRO_RANGE": "${{ inputs.range }}",
                               "GITPRO_WORKDIR": "${{ inputs.working-directory }}"}
    assert all(s.get("shell") == "bash" for s in steps if "run" in s)
    assert steps[2]["uses"].startswith("actions/github-script@")
    script = text.split("script: |", 1)[1]
    assert "${{" not in script
    for needle in ("existsSync", "gitpro did not produce a result; see the check step", "fork pull request",
                   "core.notice", "@\\u200b", "CODEOWNERS"):
        assert needle in (script if needle != "CODEOWNERS" else text), needle


@pytest.fixture
def action_env(fake_bin, tmp_path, home):
    """A fake python3 whose `-m venv DIR` makes DIR/bin/pip (logs) and DIR/bin/gitpro (this gitpro)."""
    fake_bin.add("python3", f"""
if argv[:2] == ["-m", "venv"]:
    import pathlib
    b = pathlib.Path(argv[2]) / "bin"
    b.mkdir(parents=True)
    (b / "pip").write_text("#!/bin/sh\\necho \\"$@\\" >> \\"$(dirname \\"$0\\")/pip.log\\"\\n")
    (b / "gitpro").write_text("#!{sys.executable}\\nimport sys\\nfrom gitpro.cli import main\\nsys.exit(main())\\n")
    for f in ("pip", "gitpro"):
        (b / f).chmod(0o755)
    sys.exit(0)
sys.exit(1)
""")
    runner = tmp_path / "runner"
    runner.mkdir()
    return runner


def _run_step(script: str, cwd: Path, env: Dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=str(cwd),
                          capture_output=True, text=True, env=dict(os.environ, **env), timeout=TIMEOUT)


def test_action_steps_run_locally(action_env, tmp_path, home):
    steps = [s for s in _steps((ROOT / "action.yml").read_text()) if "run" in s]
    env = {"RUNNER_TEMP": str(action_env), "GITHUB_ACTION_PATH": str(ROOT)}
    repo = app_repo(tmp_path)
    policy_file(repo, policy_text(signing=False, formats=()), name=".gitpro-policy.toml", mode=0o644)
    commit_to_base(repo, ".gitpro-policy.toml")
    make_commit(repo, "clean")

    r = _run_step(str(steps[0]["run"]), repo, env)
    assert r.returncode == 0, r.stderr
    assert (action_env / "gitpro/bin/pip.log").read_text().split() == ["install", "--quiet", str(ROOT)]

    r = _run_step(str(steps[1]["run"]), repo, {**env, "GITPRO_POLICY": ".gitpro-policy.toml", "GITPRO_RANGE": ".."})
    assert r.returncode == 2
    assert "commit range is empty" in r.stdout

    r = _run_step(str(steps[1]["run"]), repo, {**env, "GITPRO_POLICY": ".gitpro-policy.toml",
                                               "GITPRO_RANGE": "base..HEAD"})
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(r.stdout)
    assert data["ok"] is True and data["commits"] == 1 and data["mode"] == "policy"
    assert json.loads((action_env / "gitpro-policy.json").read_text()) == data

    make_commit(repo, "bad", author=("Pat", "pat@gmail.com"))
    r = _run_step(str(steps[1]["run"]), repo, {**env, "GITPRO_POLICY": ".gitpro-policy.toml",
                                               "GITPRO_RANGE": "base..HEAD"})
    assert r.returncode == 1
    assert json.loads(r.stdout)["violations"][0]["sha"]

    # working-directory: the step runs from the workspace root, the checkout is a subdirectory
    r = _run_step(str(steps[1]["run"]), tmp_path, {**env, "GITPRO_POLICY": ".gitpro-policy.toml",
                                                   "GITPRO_RANGE": "base..HEAD", "GITPRO_WORKDIR": repo.name})
    assert r.returncode == 1 and json.loads(r.stdout)["commits"] == 2


def test_action_refuses_shallow_clone(action_env, tmp_path, home):
    steps = [s for s in _steps((ROOT / "action.yml").read_text()) if "run" in s]
    src = app_repo(tmp_path)
    make_commit(src, "one")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{src}", str(shallow)], check=True,
                   capture_output=True, timeout=TIMEOUT)
    r = _run_step(str(steps[1]["run"]), shallow, {"RUNNER_TEMP": str(action_env), "GITPRO_POLICY": "p.toml",
                                                  "GITPRO_RANGE": "a..b"})
    assert r.returncode == 2 and "shallow clone" in r.stdout


# ---------------------------------------------------------------- 13a: resolve.EXTRA_CHECKS

def test_extra_check_registered_once():
    import importlib
    assert policy.check_report in resolve.EXTRA_CHECKS
    importlib.reload(policy)
    names = [f for f in resolve.EXTRA_CHECKS if getattr(f, "__module__", "") == "gitpro.policy"]
    assert len(names) == 1


def test_why_reports_policy_problem(capsys, cfg, work_policy, tmp_path):
    cfg(config_text())
    repo = app_repo(tmp_path)
    assert run("why", str(repo), "--json") == 1
    probs = [p for p in out_json(capsys)["problems"] if p["code"] == "policy"]
    assert probs and any("corp.test" in p["message"] for p in probs)
    assert any("sign" in p["message"] for p in probs)


def test_why_without_policy_has_no_policy_problem(capsys, cfg, tmp_path):
    cfg(config_text().replace('policy = "~/work/gitpro-policy.toml"\n', ""))
    repo = app_repo(tmp_path)
    run("why", str(repo), "--json")
    assert not [p for p in out_json(capsys)["problems"] if p["code"] == "policy"]


# ---------------------------------------------------------------- 14: cache

def test_policy_cache(home, monkeypatch):
    p = policy_file(home / "work")
    parses = []
    real = config._toml.loads

    def counting(text, *a, **kw):
        parses.append(1)
        return real(text, *a, **kw)
    monkeypatch.setattr(config._toml, "loads", counting)
    first = policy.load(p)
    assert policy.load(p) is first
    assert len(parses) == 1
    p.write_text(policy_text().replace(NAME, "Renamed policy"))
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert policy.load(p).name == "Renamed policy"
    assert len(parses) == 2


# ---------------------------------------------------------------- capabilities, doctor

def test_capabilities(capsys):
    assert run("--version", "--json") == 0
    caps = out_json(capsys)["capabilities"]
    assert {"policy", "check.policy", "team"} <= set(caps)


def test_plain_check_untouched(capsys, cfg):
    cfg(config_text().replace('policy = "~/work/gitpro-policy.toml"\n', ""))
    assert run("check", "--json") == 0
    assert out_json(capsys)["up_to_date"] is True


def test_doctor_section(home, work_policy):
    from gitpro import doctor
    c = config.load_all(write_config(home, config_text(email="me@example.corp")))
    sec = policy.doctor_section(doctor.Ctx(home=home, cfg=c))
    assert sec["status"] == "error" and "sign = false" in sec["problems"][0]["message"]
    c2 = config.load_all(write_config(home, config_text().replace('policy = "~/work/gitpro-policy.toml"\n', "")))
    sec = policy.doctor_section(doctor.Ctx(home=home, cfg=c2))
    assert sec["status"] == "ok" and "no policy" in sec["summary"]
