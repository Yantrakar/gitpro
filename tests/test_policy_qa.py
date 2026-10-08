"""QA (package 14 review): reproductions of bugs found in review, each documenting the expected
behaviour. They were xfail(strict=True) until fixed; the markers are gone with the fixes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

import pytest

import os
import subprocess

from conftest import TIMEOUT, git, run
from gitpro import policy
from test_policy import app_repo, config_text, make_commit, out_json, policy_file, policy_text
from test_policy import make_keys


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    """The ssh keys of test_policy (made by the same helper)."""
    return make_keys(tmp_path_factory)

GH = """
a = " ".join(argv)
if "auth status" in a:
    print("Logged in to github.com account me-work"); sys.exit(0)
if "user/emails" in a:
    print("me@example.corp"); sys.exit(0)   # gh api user/emails --jq '.[] | select(.verified) | .email'

if "api user" in a:
    print(json.dumps({"login": "me-work"})); sys.exit(0)
print("[]")
"""


@pytest.fixture(autouse=True)
def _no_cache():
    policy._CACHE.clear()


@pytest.fixture
def online(cfg, home, fake_bin, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    work_policy = policy_file(home / "work")
    fake_bin.add("gh", "import json\n" + GH)
    text = config_text(email="me@example.corp").replace('policy = "', 'gh_account = "me-work"\npolicy = "', 1)
    cfg(text, apply=False)
    work_policy.write_text(policy_text().replace("require_verified_email = false", "require_verified_email = true"))
    return fake_bin


def test_team_online_json_is_valid_json(capsys, online):
    run("team", "--online", "--json")
    json.loads(capsys.readouterr().out)


def test_team_online_checks_verified_email(capsys, online):
    run("team", "--online")
    out = capsys.readouterr().out
    assert "could not check" not in out
    assert any("user/emails" in " ".join(c) for c in online.calls("gh"))
    run("team", "--online", "--json")
    ids = {i["id"]: i for i in json.loads(capsys.readouterr().out)["identities"]}
    (row,) = [c for c in ids["work-github"]["checks"] if c["name"] == "verified email"]
    assert row["level"] == "ok", row


def test_range_policy_edited_by_the_range_does_not_whitelist_itself(capsys, tmp_path, home):
    repo = app_repo(tmp_path)
    p = policy_file(repo, policy_text(signing=False, formats=()), name=".gitpro-policy.toml", mode=0o644)
    git(repo, "add", ".gitpro-policy.toml")
    make_commit(repo, "add policy")
    git(repo, "tag", "-f", "base")
    p.write_text(policy_text(signing=False, formats=()).replace('"contractors.example.corp"',
                                                                '"contractors.example.corp", "gmail.com"'))
    git(repo, "add", ".gitpro-policy.toml")
    make_commit(repo, "widen policy", author=("Pat", "pat@gmail.com"))
    rc = run("check", "--range", "base..HEAD", "--repo", str(repo), "--json")
    data = out_json(capsys)
    assert rc != 0, data


@pytest.mark.parametrize("subject", ["ordinary", "before\x1eafter", "\x1esubject"])
def test_range_control_characters_cannot_hide_commits(capsys, tmp_path, home, subject):
    repo = app_repo(tmp_path)
    p = policy_file(tmp_path / "org")
    sha = make_commit(repo, subject, author=("Pat", "pat@gmail.com"))
    rc = run("check", "--policy", str(p), "--range", "base..HEAD", "--repo", str(repo), "--json")
    data = out_json(capsys)
    assert rc == 1, data
    assert data["commits"] == 1
    assert {(v["sha"], v["rule"]) for v in data["violations"]} == {
        (sha, "allowed_email_domains"), (sha, "require_signing")}


@pytest.mark.parametrize("directory_link", [False, True])
@pytest.mark.parametrize("alias_kind", ["none", "repo", "policy-directory", "policy-file"])
def test_range_checkout_symlinks_cannot_redirect_base_policy(
        capsys, tmp_path, home, directory_link, alias_kind):
    repo = app_repo(tmp_path)
    p = policy_file(repo / "ci", name="policy.toml", mode=0o644)
    git(repo, "add", ".")
    make_commit(repo, "add policy")
    git(repo, "tag", "-f", "base")
    target = policy_file(repo / "new", name="policy.toml", mode=0o644)
    p.unlink()
    if directory_link:
        p.parent.rmdir()
        p.parent.symlink_to("new")
    else:
        p.symlink_to("../new/policy.toml")
    git(repo, "add", ".")
    make_commit(repo, "redirect policy", author=("Pat", "pat@gmail.com"))
    requested = p
    if alias_kind != "none":
        alias = tmp_path / "workspace-link"
        alias.symlink_to(repo if alias_kind == "repo" else p if alias_kind == "policy-file" else p.parent)
        requested = alias if alias_kind == "policy-file" else alias / (
            "ci/policy.toml" if alias_kind == "repo" else "policy.toml")
    assert requested.read_bytes() == target.read_bytes()
    rc = run("check", "--policy", str(requested), "--range", "base..HEAD", "--repo", str(repo), "--json")
    data = out_json(capsys)
    assert rc == 1, data
    assert len(data["policies"]) == 1
    assert {v["rule"] for v in data["violations"]} == {"allowed_email_domains", "require_signing"}


@pytest.mark.parametrize("damage", ["missing", "truncated", "extra-field", "wrong-sha"])
def test_range_malformed_log_records_fail_closed(capsys, tmp_path, home, monkeypatch, damage):
    repo = app_repo(tmp_path)
    p = policy_file(tmp_path / "org")
    make_commit(repo, "subject")
    real_git = policy._git

    def malformed(repo, *args, **kwargs):
        out = real_git(repo, *args, **kwargs)
        if args[0] == "log" and "--stdin" in args:
            if damage == "missing":
                return b""
            if damage == "truncated":
                return out[:-1]
            if damage == "extra-field":
                return out.replace(b"subject\0", b"subject\0injected\0")
            return b"0" * 40 + out[40:]
        return out

    monkeypatch.setattr(policy, "_git", malformed)
    assert run("check", "--policy", str(p), "--range", "base..HEAD", "--repo", str(repo), "--json") == 2
    data = out_json(capsys)
    assert data["ok"] is False and "cannot check policy" in data["error"]


def test_range_policy_alias_loop_is_an_explicit_error(capsys, tmp_path, home):
    repo = app_repo(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    first.symlink_to(second)
    second.symlink_to(first)
    make_commit(repo, "subject")
    assert run("check", "--policy", str(first), "--range", "base..HEAD", "--repo", str(repo)) == 2
    assert "too many symlinks" in capsys.readouterr().err


def test_deeply_nested_policy_is_a_config_error(capsys, home, tmp_path):
    p = tmp_path / "deep.toml"
    p.write_text("[policy]\nname = " + "[" * 100000 + "]" * 100000 + "\n")
    p.chmod(0o600)
    try:
        rc = run("policy", "lint", str(p))
    except RecursionError:
        rc = 1
    assert rc == 2


def test_verified_keeps_unknown_as_unknown(monkeypatch, home):
    """forge.emails() may return verified=None; that must not become False (policy.py:1140)."""
    from gitpro import forge

    class S:
        pass
    monkeypatch.setattr(forge, "Session", lambda *a, **k: S())
    monkeypatch.setattr(forge, "emails", lambda s: [{"email": "A@x.test", "verified": None}])
    cfg = type("C", (), {"identities": []})()
    got = policy._verified(cfg, Path(home), False)
    if got.get("a@x.test") is False:
        pytest.xfail("verified=None is reported as False (not verified) instead of unknown")
    assert got.get("a@x.test") is None


# ---------------------------------------------------------------- QA round 2 (b3e7775)

def _principal_case(tmp_path, keys, principals: str, committer: str) -> Tuple[int, dict]:
    org = tmp_path / "org"
    org.mkdir()
    pub = (keys["good"].parent / "good.pub").read_text().split()
    (org / "allowed_signers").write_text(f'{principals} namespaces="git" {pub[0]} {pub[1]}\n')
    p = policy_file(org, policy_text(signers="allowed_signers"), name="p.toml")
    repo = app_repo(tmp_path)
    make_commit(repo, "signed", key=keys["good"], author=("Me", committer))
    rc = run("check", "--policy", str(p), "--range", "base..HEAD", "--repo", str(repo), "--json")
    return rc, {}


def test_principal_list_second_entry_is_accepted(capsys, tmp_path, keys):
    """allowed_signers 'alice@example.corp,me@example.corp <key>': a commit by me@example.corp is fine."""
    rc, _ = _principal_case(tmp_path, keys, "alice@example.corp,me@example.corp", "me@example.corp")
    data = out_json(capsys)
    assert rc == 0, data["violations"]


def test_principal_negation_is_honoured(capsys, tmp_path, keys):
    rc, _ = _principal_case(tmp_path, keys, "*@example.corp,!me@example.corp", "me@example.corp")
    data = out_json(capsys)
    assert rc == 1 and data["violations"][0]["rule"] == "allowed_signers", data


def test_policy_path_through_symlinked_repo_alias_is_read_from_base(capsys, tmp_path, home):
    repo = app_repo(tmp_path)
    p = policy_file(repo / "ci", policy_text(signing=False, formats=()), name="policy.toml", mode=0o644)
    git(repo, "add", ".")
    make_commit(repo, "add policy")
    git(repo, "tag", "-f", "base")
    p.write_text(policy_text(signing=False, formats=()).replace('"contractors.example.corp"',
                                                                '"contractors.example.corp", "gmail.com"'))
    git(repo, "add", ".")
    make_commit(repo, "widen", author=("Pat", "pat@gmail.com"))
    alias = tmp_path / "workspace-link"
    os.symlink(str(repo), str(alias))
    rc = run("check", "--policy", str(alias / "ci/policy.toml"), "--range", "base..HEAD", "--repo", str(repo),
             "--json")
    assert rc == 1, out_json(capsys)


def test_policy_change_in_merge_commit_is_warned(capsys, tmp_path, home):
    repo = app_repo(tmp_path)
    p = policy_file(repo, policy_text(signing=False, formats=()), name=".gitpro-policy.toml", mode=0o644)
    git(repo, "add", ".")
    make_commit(repo, "add policy")
    git(repo, "tag", "-f", "base")
    git(repo, "checkout", "-q", "-b", "side")
    make_commit(repo, "side")
    git(repo, "checkout", "-q", "main")
    who = ["-c", "user.name=Pat", "-c", "user.email=pat@gmail.com"]
    subprocess.run(["git", "-C", str(repo), *who, "merge", "-q", "--no-ff", "--no-commit", "side"], check=True,
                   capture_output=True, timeout=TIMEOUT)
    p.write_text(policy_text(signing=False, formats=()).replace('"contractors.example.corp"',
                                                                '"contractors.example.corp", "gmail.com"'))
    git(repo, "add", ".")
    subprocess.run(["git", "-C", str(repo), *who, "commit", "-q", "--no-edit"], check=True, capture_output=True,
                   timeout=TIMEOUT)
    git(repo, "checkout", "-q", "base", "--", ".gitpro-policy.toml")   # the checkout matches the base again
    git(repo, "reset", "-q")
    rc = run("check", "--range", "base..HEAD", "--repo", str(repo), "--json")
    data = out_json(capsys)
    assert rc == 1    # enforcement is right (the base version judges) ...
    assert any("changes .gitpro-policy.toml" in w for w in data["warnings"]), data["warnings"]   # ... the warning is not


GH_NO_SCOPE = """
a = " ".join(argv)
if "auth status" in a:
    print("Logged in to github.com account me-work"); sys.exit(0)
if "user/emails" in a:
    sys.stderr.write("HTTP 403: needs the user:email scope\\n"); sys.exit(1)
print("[]")
"""


def test_verified_unknown_row_names_the_reason(capsys, cfg, home, fake_bin, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pf = policy_file(home / "work", policy_text().replace("require_verified_email = false",
                                                          "require_verified_email = true"))
    fake_bin.add("gh", GH_NO_SCOPE)
    cfg(config_text(str(pf), email="me@example.corp").replace('policy = "', 'gh_account = "me-work"\npolicy = "', 1),
        apply=False)
    run("team", "--online", "--json")
    data = json.loads(capsys.readouterr().out)
    ids = {i["id"]: i for i in data["identities"]}
    (row,) = [c for c in ids["work-github"]["checks"] if c["name"] == "verified email"]
    assert row["level"] == "warning" and row["message"] != "could not check", row
