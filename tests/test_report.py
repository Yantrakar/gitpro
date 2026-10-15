"""19 report: `gitpro report` never lets a name, email, host, owner, repo, path, login, key or token out.

The planted-value tests come first (spec 10.1, 10.2): a world full of obviously fake values containing the
word "canary", then every output the command can produce is searched for them. The oracle here is written
independently of the implementation (its own regexes, its own list of forbidden strings). Fake `gh`, fake
`webbrowser.open`; nothing is opened, sent or pushed.
"""
from __future__ import annotations

import io
import json
import os
import pwd
import re
import socket
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import List, NamedTuple, Optional

import pytest

from conftest import TIMEOUT, git, run, write_config
from gitpro import cli

# step 4 lands `gitpro report`, step 3 the redactor and the scan, step 5 --security/--online
NO_ONLINE_YET = pytest.mark.xfail(strict=True, reason="--online lands in step 5")

CANARY_EMAIL = "canary.person@canary-corp.test"
CANARY_HOST = "git.canary-corp.test"
CANARY_TOKEN = "ghp_CANARYCANARYCANARYCANARYCANARYCANARY12"
REMOTE = f"https://canaryuser:{CANARY_TOKEN}@{CANARY_HOST}/canaryowner/canaryrepo.git"

CONFIG = """\
[default]
name = "Canary Defaultname"
email = "canary.default@canary-corp.test"

[[identity]]
id = "canaryidalpha"
name = "Canary Personname"
email = "canary.person@canary-corp.test"
host = "git.canary-corp.test"
owners = ["canaryowner"]
ssh_key = "~/.ssh/canary_key_file"

[[identity]]
id = "canaryidbeta"
name = "Canary Betaname"
email = "canary.beta@canary-corp.test"
host = "github.com"
owners = ["canarybetaowner"]
gh_account = "canarylogin"
"""

GH_STUB = """
if argv[:2] == ["auth", "token"]:
    print("stubtoken")
    sys.exit(0)
if argv[:2] == ["issue", "create"]:
    print("https://github.com/Yantrakar/gitpro/issues/7")
    sys.exit(0)
sys.exit(1)
"""

# ---------------------------------------------------------------- the independent oracle

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PLACEHOLDER_EMAIL = re.compile(r"^email-\d+@users\.noreply\.github\.com$")
_PATTERNS = {
    "path": re.compile(r"/(?:home|Users|root|tmp)/"),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "long run": re.compile(r"[A-Za-z0-9]{40,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "public key": re.compile(r"ssh-(?:ed25519|rsa) AAAA|ecdsa-sha2-"),
    "token": re.compile(r"gh[pousr]_|github_pat_|glpat-|AKIA[0-9A-Z]{16}|xox[abprs]-"),
}


class World(NamedTuple):
    home: Path
    repo: Path
    tmp: Path
    fake_bin: object
    opened: List[str]


def forbidden(world: World) -> List[str]:
    """Substrings (lower case) that must never appear anywhere."""
    out = ["canary", str(world.tmp).lower(), "stubtoken", "aws_secret", "ghp_", "akia"]
    try:
        real = pwd.getpwuid(os.getuid())
        out += [real.pw_dir.lower()] + ([real.pw_name.lower()] if len(real.pw_name) >= 4 else [])
    except KeyError:
        pass
    return out


def leaks(text: str, world: World) -> List[str]:
    """What is wrong with `text` (empty list: clean). Also checks the text once percent-decoded."""
    found: List[str] = []
    for variant in (text, urllib.parse.unquote(text), urllib.parse.unquote_plus(text)):
        low = variant.lower()
        found += [f"contains {s!r}" for s in forbidden(world) if s in low]
        found += [f"matches {name}" for name, rx in _PATTERNS.items() if rx.search(variant)]
        found += [f"email {m}" for m in _EMAIL.findall(variant) if not _PLACEHOLDER_EMAIL.match(m)]
    return sorted(set(found))


def assert_clean(text: str, world: World, what: str) -> None:
    bad = leaks(text, world)
    assert not bad, f"{what} leaks: {bad}\n{text}"


# ---------------------------------------------------------------- the world

def build_world(tmp_path: Path, monkeypatch, fake_bin) -> World:
    """HOME=<tmp>/home/canaryuser, a config with two canary identities, a repo with a token in its remote
    url, a repo-local user.email override, and canary secrets in the environment."""
    home = tmp_path / "home" / "canaryuser"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh").chmod(0o700)
    for name in ("canary_key_file",):
        (home / ".ssh" / name).write_text("")
        (home / ".ssh" / name).chmod(0o600)
        (home / ".ssh" / f"{name}.pub").write_text(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGl0aXNub3RhcmVhbGtleWl0aXNub3RhcmVhbGtleQ canary@key\n")
    monkeypatch.chdir(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "canaryuser")
    monkeypatch.setenv("LOGNAME", "canaryuser")
    for var in ("GITPRO_CONFIG", "GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "GITPRO_DEBUG"):
        monkeypatch.delenv(var, raising=False)
    ceiling = str(tmp_path.parent.resolve())
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", ceiling)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GH_TOKEN", "ghp_CANARYENVCANARYENVCANARYENVCANARYENV12")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "canaryawssecretcanaryawssecretcanary00")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))      # not a terminal
    monkeypatch.setattr(socket, "gethostname", lambda: "canaryhost.canary-corp.test")
    write_config(home, CONFIG)
    fake_bin.add("gh", GH_STUB)
    opened: List[str] = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    assert run("apply", "-y") == 0
    repo = home / "work" / "canaryrepo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=TIMEOUT)
    git(repo, "remote", "add", "origin", REMOTE)
    git(repo, "config", "user.email", "canary.override@canary-corp.test")
    git(repo, "config", "core.sshCommand", f"ssh -i {home}/.ssh/canary_key_file -F {home}/canary_ssh_config")
    return World(home, repo, tmp_path, fake_bin, opened)


@pytest.fixture
def world(tmp_path, monkeypatch, fake_bin) -> World:
    return build_world(tmp_path, monkeypatch, fake_bin)


def go(world: World, capsys, *argv: str):
    rc = run("report", *argv)
    out, err = capsys.readouterr()
    return rc, out, err


def record_world(world: World, monkeypatch, capsys) -> None:
    """Crash and denial records made the way production makes them."""
    from gitpro import agents, guard
    real = cli.cmd_check

    def boom(args):
        canary_local = CANARY_EMAIL     # noqa: F841 - a local that must never reach the record
        raise KeyError(f"{CANARY_EMAIL} {world.home}")
    with monkeypatch.context() as m:
        m.setattr(cli, "cmd_check", boom)
        assert run("check") == 2
    assert cli.cmd_check is real
    payload = {"session_id": "s1", "cwd": str(world.repo), "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "tool_input": {"command": "git commit -m x", "description": "d"}}
    with monkeypatch.context() as m:
        m.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        agents.main(["agent-check"])
    with monkeypatch.context() as m:
        m.chdir(world.repo)
        guard.check("pre-commit", [])
    capsys.readouterr()


# ================================================================ 10.1 planted values

MODES = [
    ("bug", ["--repo", "{repo}"]),
    ("bug-message", ["--repo", "{repo}", "-m",
                     f"it said {CANARY_EMAIL} on {CANARY_HOST} for canaryowner/canaryrepo at {{home}}"]),
    ("bug-norepo", []),
    ("crash", ["--crash"]),
    ("denial", ["--denial"]),
    ("security", ["--security", "--repo", "{repo}"]),
    ("security-norepo", ["--security"]),
]


@pytest.mark.parametrize("json_mode", [False, True], ids=["print", "json"])
@pytest.mark.parametrize("name,argv", MODES, ids=[m[0] for m in MODES])
def test_planted_values_never_appear(world, monkeypatch, capsys, name, argv, json_mode):
    record_world(world, monkeypatch, capsys)
    argv = [a.format(repo=world.repo, home=world.home) for a in argv]
    rc, out, err = go(world, capsys, "--json" if json_mode else "--print", *argv)
    assert rc == 0, out + err
    assert_clean(out, world, "stdout")
    assert_clean(err, world, "stderr")
    if json_mode:
        data = json.loads(out)
        assert data["ok"] is True and data["command"] == "report"
        for key in ("title", "body", "url"):
            assert_clean(data[key], world, f"json {key}")
    assert world.fake_bin.calls("gh") == []
    assert world.opened == []


def test_planted_values_not_in_the_browser_url(world, monkeypatch, capsys):
    record_world(world, monkeypatch, capsys)
    for argv in (["--repo", str(world.repo), "-m", f"{CANARY_EMAIL} broke"], ["--crash"], ["--denial"],
                 ["--security", "--repo", str(world.repo)]):
        rc, out, err = go(world, capsys, "-y", *argv)
        assert rc == 0, out + err
        assert_clean(out, world, "stdout")
        assert_clean(err, world, "stderr")
    assert len(world.opened) == 4
    for url in world.opened:
        assert_clean(url, world, "browser url")
    assert world.opened[-1] == "https://github.com/Yantrakar/gitpro/security/advisories/new"
    assert world.fake_bin.calls("gh") == []


def test_the_draft_still_helps(world, monkeypatch, capsys):
    record_world(world, monkeypatch, capsys)
    rc, out, err = go(world, capsys, "--print", "--repo", str(world.repo))
    assert rc == 0, err
    for needle in ("email-1", "email-2", "override", "https", "host-1", "url_form"):
        assert needle in out, needle
    assert "gitpro" in out.lower()


@NO_ONLINE_YET
def test_planted_values_not_in_the_gh_call(world, monkeypatch, capsys):
    record_world(world, monkeypatch, capsys)
    rc, out, err = go(world, capsys, "--online", "-y", "--repo", str(world.repo), "-m", f"{CANARY_EMAIL} broke")
    assert rc == 0, out + err
    calls = [e for e in world.fake_bin.entries("gh") if e["argv"][:2] == ["issue", "create"]]
    assert len(calls) == 1
    assert calls[0]["argv"][:6] == ["issue", "create", "--repo", "Yantrakar/gitpro", "--title", calls[0]["argv"][5]]
    assert "--body-file" in calls[0]["argv"] and calls[0]["argv"][-1] == "-"
    assert_clean(" ".join(calls[0]["argv"]), world, "gh argv")
    assert_clean(calls[0]["stdin"].decode(), world, "gh stdin")
    assert_clean(out, world, "stdout")
    assert_clean(err, world, "stderr")


# ================================================================ 10.2 each layer on its own

KINDS = [["--repo", "{repo}"], ["--repo", "{repo}", "-m", f"hello from {CANARY_EMAIL}"], ["--crash"],
         ["--denial"]]


def _blocked(world, capsys, argv, json_mode=True):
    argv = [a.format(repo=world.repo) for a in argv]
    rc, out, err = go(world, capsys, "--json" if json_mode else "--print", *argv)
    assert rc == 2, out + err
    assert_clean(out, world, "stdout")
    assert_clean(err, world, "stderr")
    assert world.fake_bin.calls("gh") == [] and world.opened == []
    if json_mode:
        data = json.loads(out)
        assert data["ok"] is False and data["blocked"]
        assert all(set(b) == {"kind", "section"} for b in data["blocked"])


def test_layer_three_alone_blocks_when_the_allowlist_is_bypassed(world, monkeypatch, capsys):
    """Layer 1 off: the why section is the raw report. Layers 2 and 3 on."""
    record_world(world, monkeypatch, capsys)
    from gitpro import report
    monkeypatch.setattr(report, "why_section", lambda rep, red: json.dumps(rep.to_json(), indent=1))
    _blocked(world, capsys, ["--repo", "{repo}"])
    _blocked(world, capsys, ["--repo", "{repo}"], json_mode=False)


def test_layer_three_alone_blocks_when_the_redactor_does_nothing(world, monkeypatch, capsys):
    """Layer 2 off: P returns the value itself, the -m text is not rewritten. Layers 1 and 3 on."""
    record_world(world, monkeypatch, capsys)
    from gitpro import report
    monkeypatch.setattr(report.Redactor, "P", lambda self, kind, value: value)
    monkeypatch.setattr(report.Redactor, "rewrite", lambda self, text: text, raising=False)
    _blocked(world, capsys, ["--repo", "{repo}"])
    _blocked(world, capsys, ["--repo", "{repo}", "-m", f"hello from {CANARY_EMAIL}"])


def test_layer_three_alone_blocks_with_layers_one_and_two_off(world, monkeypatch, capsys):
    record_world(world, monkeypatch, capsys)
    from gitpro import report
    monkeypatch.setattr(report, "why_section", lambda rep, red: json.dumps(rep.to_json(), indent=1))
    monkeypatch.setattr(report.Redactor, "P", lambda self, kind, value: value)
    monkeypatch.setattr(report.Redactor, "rewrite", lambda self, text: text, raising=False)
    for argv in KINDS[:2]:
        _blocked(world, capsys, argv)


@pytest.mark.parametrize("argv", KINDS + [["--security", "--repo", "{repo}"], ["--security"]],
                         ids=["bug", "message", "crash", "denial", "security-repo", "security"])
def test_layers_one_and_two_alone_are_clean_when_the_scan_is_off(world, monkeypatch, capsys, argv):
    """Layer 3 off: the allowlist and the redactor must already have kept every planted value out."""
    record_world(world, monkeypatch, capsys)
    from gitpro import report
    monkeypatch.setattr(report, "scan", lambda *a, **k: [])
    argv = [a.format(repo=world.repo) for a in argv]
    rc, out, err = go(world, capsys, "--json", *argv)
    assert rc == 0, out + err
    assert_clean(out, world, "stdout")
    assert_clean(err, world, "stderr")


# ================================================================ 10.3 the scan

SECTIONS = ("title", "versions", "why", "crash", "denial", "message")

# (kind, text) - every pattern and every known-value kind has a positive case
PATTERN_CASES = [
    ("token", "token ghp_" + "A1b2C3d4" * 3),
    ("token", "github_pat_" + "A1b2C3d4" * 4),
    ("token", "glpat-" + "A1b2C3d4" * 3),
    ("token", "Authorization: token " + "0123456789abcdef" * 2 + "01234567"),
    ("url-credentials", "remote is https://someuser:hunter2@host.invalid/x/y.git"),
    ("url-credentials", "remote is https://ghp_" + "A1b2C3d4" * 3 + "@host.invalid/x/y.git"),
    ("private-key", "-----BEGIN OPENSSH PRIVATE KEY-----"),
    ("private-key", "-----BEGIN RSA PRIVATE KEY-----"),
    ("ssh-key", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGl0aXNub3RhcmVhbGtleQ"),
    ("ssh-key", "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ"),
    ("ssh-key", "ecdsa-sha2-nistp256 AAAAE2VjZHNh"),
    ("ssh-key", "sk-ssh-ed25519@openssh.com AAAAGnNr"),
    ("cloud-key", "AKIAIOSFODNN7EXAMPLE"),
    ("cloud-key", "xoxb-1234567890-abcdefghij"),
    ("long-run", "Zm9vYmFy" * 6),
    ("long-run", "0123456789abcdef" * 3),
    ("email", "write to someone@elsewhere.invalid please"),
    ("path", "see /home/someone/project"),
    ("path", "see /Users/someone/project"),
    ("path", "see /root/project"),
    ("path", "see /tmp/scratch/x"),
    ("ip", "connect to 192.168.10.20 now"),
]

KNOWN_CASES = [
    ("id", "canaryidalpha"), ("name", "Canary Personname"), ("email", "canary.person@canary-corp.test"),
    ("host", "git.canary-corp.test"), ("owner", "canaryowner"), ("key", "canary_key_file"),
    ("account", "canarylogin"), ("path", "/srv/some/where/canaryrepo"), ("user", "canaryuser"),
    ("machine", "canaryhost"),
]

CLEAN_CASES = [
    "email-1@users.noreply.github.com",
    "email-12@users.noreply.github.com and email-2@users.noreply.github.com",
    "github.com",
    "https://github.com/Yantrakar/gitpro/issues/new?title=%5Bbug%5D&body=x",
    "gitpro 0.9.3 python 3.12.3 git 2.43.0 linux",
    "OpenSSH_9.6p1 Ubuntu-3ubuntu13.14, OpenSSL 3.0.13",
    "gitpro/forge.py:512 in redact",
    "host-1 owner-1 repo-1 id-1 name-1 key-1 path-1 account-1",
    "problem: override, url_form",
]


@pytest.mark.parametrize("kind,text", PATTERN_CASES, ids=[f"{k}-{i}" for i, (k, _) in enumerate(PATTERN_CASES)])
def test_scan_pattern_is_found(kind, text):
    from gitpro import report
    for section in SECTIONS:
        hits = report.scan({section: f"before {text} after"}, [])
        assert {"kind": kind, "section": section} in hits, (kind, section, hits)


@pytest.mark.parametrize("kind,value", KNOWN_CASES, ids=[k for k, _ in KNOWN_CASES])
def test_scan_known_value_is_found(kind, value):
    from gitpro import report
    known = [(kind, value)]
    for text in (f"x {value} y", f"x {value.upper()} y", f"({value.lower()})"):
        assert {"kind": kind, "section": "message"} in report.scan({"message": text}, known)


def test_scan_known_values_need_a_word_boundary_and_three_characters():
    from gitpro import report
    assert report.scan({"message": "canaryowners and mycanaryowner"}, [("owner", "canaryowner")]) == []
    assert report.scan({"message": "ab abc"}, [("name", "ab")]) == []
    assert report.scan({"message": "an abc here"}, [("name", "abc")]) == [{"kind": "name", "section": "message"}]


@pytest.mark.parametrize("text", CLEAN_CASES)
def test_scan_lets_ordinary_text_through(text):
    from gitpro import report
    for section in SECTIONS:
        assert report.scan({section: text}, []) == [], text


def test_scan_decodes_the_url_body_once_more():
    from gitpro import report
    body = "reach me at someone%40elsewhere.invalid"
    url = "https://github.com/Yantrakar/gitpro/issues/new?title=t&body=" + body
    hits = report.scan({"title": "t"}, [], url=url)
    assert {"kind": "email", "section": "url"} in hits
    ok = "https://github.com/Yantrakar/gitpro/issues/new?" + urllib.parse.urlencode(
        {"title": "[bug] t", "body": "email-1@users.noreply.github.com"}, quote_via=urllib.parse.quote)
    assert report.scan({"title": "t"}, [], url=ok) == []
    assert report.scan({}, [], url="https://github.com/Yantrakar/gitpro/security/advisories/new") == []


def test_scan_sees_encoded_known_values_in_the_url():
    from gitpro import report
    url = "https://github.com/Yantrakar/gitpro/issues/new?" + urllib.parse.urlencode(
        {"title": "t", "body": "from canaryowner"}, quote_via=urllib.parse.quote)
    assert {"kind": "owner", "section": "url"} in report.scan({}, [("owner", "canaryowner")], url=url)


def test_scan_hits_have_exactly_kind_and_section_never_the_value():
    from gitpro import report
    hits = report.scan({"message": "mail me: someone@elsewhere.invalid"}, [])
    assert hits and all(set(h) == {"kind", "section"} for h in hits)
    assert "someone" not in json.dumps(hits)


def test_known_hosts_constant_is_pinned():
    from gitpro import report
    assert set(report.KNOWN_HOSTS) == {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org",
                                       "git.sr.ht", "gitea.com"}


def test_block_message_names_kinds_and_sections_but_not_values(capsys):
    from gitpro import report
    hits = [{"kind": "email", "section": "message"}, {"kind": "token", "section": "why"}]
    text = report.block_text(hits)
    assert text.splitlines()[0] == "✗ report not sent: the draft contains email, token in message, why"
    assert "→ remove it from -m" not in text      # a why hit is a gitpro bug
    assert 'gitpro report --print -m "report blocked"' in text
    only = report.block_text([{"kind": "email", "section": "message"}]).splitlines()
    assert only[1] == "→ remove it from -m"


def test_the_redactor_numbers_each_kind_in_order_and_reuses_placeholders():
    from gitpro import report
    red = report.Redactor()
    assert red.P("email", "A@b.test") == "email-1@users.noreply.github.com"
    assert red.P("email", "c@d.test") == "email-2@users.noreply.github.com"
    assert red.P("email", "a@B.test") == "email-1@users.noreply.github.com"
    assert red.P("host", "git.one.test") == "host-1"
    assert red.P("host", "GIT.one.test") == "host-1"
    assert red.P("host", "git.two.test") == "host-2"
    assert red.P("owner", "someone") == "owner-1"
    assert red.P("repo", "thing") == "repo-1"


def test_the_redactor_rewrites_message_text_longest_value_first():
    from gitpro import report
    red = report.Redactor()
    red.P("owner", "acme")
    red.P("repo", "acme-tools")
    out = red.rewrite("clone acme/acme-tools failed")
    assert "acme" not in out
    assert out == "clone owner-1/repo-1 failed"
