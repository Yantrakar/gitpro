"""Shared helpers: undo.write, files.atomic_write, jsonfile, jsonout, forge, render.is_generated, fake_bin."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import TIMEOUT
from gitpro import config, files, forge, jsonfile, jsonout, render, undo
from gitpro.config import ConfigError


# ---------------------------------------------------------------- undo (32)

def test_undo_write(home, monkeypatch):  # 32
    monkeypatch.setattr(undo, "_stamp", lambda: "20261002-101500")
    first = undo.write("scan", ["git -C /r config user.email x"], what="removed repo-local user settings")
    assert first == home / ".local/state/gitpro/scan-undo-20261002-101500.sh"
    assert first.stat().st_mode & 0o777 == 0o700
    assert first.parent.stat().st_mode & 0o777 == 0o700
    lines = first.read_text().splitlines()
    assert lines[0] == "#!/bin/sh"
    assert lines[1].startswith("# puts back what 'gitpro scan' changed on ")
    assert lines[1].endswith("(removed repo-local user settings)")
    assert lines[2:] == ["git -C /r config user.email x"]
    assert "set -e" not in first.read_text()
    second = undo.write("scan", ["true"], what="x")
    assert second.name == "scan-undo-20261002-101500-1.sh"
    (first.parent / "scan-undo-20261002-101500-2.sh").write_text("")  # taken: O_EXCL moves on
    assert undo.write("scan", ["true"], what="x").name == "scan-undo-20261002-101500-3.sh"


def test_undo_state_dir_and_names(home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert undo.state_dir() == tmp_path / "state/gitpro"
    path = undo.write("agent install", ["rm -f /x"], what="added the hook")
    assert path.name.startswith("agent-undo-") and "'gitpro agent install'" in path.read_text()
    fix = undo.write("scan --fix", ["true"], what="x")  # V-014: no spaces in file names
    assert fix.name.startswith("scan-undo-") and " " not in fix.name
    assert undo.tilde(home / "a/b") == "~/a/b" and undo.tilde(Path("/etc/x")) == "/etc/x"


def test_undo_lines_all_run(home, tmp_path):
    marker = tmp_path / "ran"
    path = undo.write("t", ["false", f"touch {marker}"], what="test")
    subprocess.run(["sh", str(path)], timeout=TIMEOUT)
    assert marker.exists()


# ---------------------------------------------------------------- atomic_write (33)

def test_atomic_write_through_symlink(tmp_path):  # 33
    target = tmp_path / "real/settings.json"
    target.parent.mkdir()
    target.write_text("old")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    files.atomic_write(link, "new\n", 0o600)
    assert link.is_symlink() and target.read_text() == "new\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".")]  # no temp file left
    plain = tmp_path / "d/plain"
    files.atomic_write(plain, "x", 0o644)
    assert plain.read_text() == "x" and plain.stat().st_mode & 0o777 == 0o644
    assert files._atomic_write is files.atomic_write


# ---------------------------------------------------------------- jsonfile (34)

def _yes(seen):
    def confirm(diff):
        seen.append(diff)
        return True
    return confirm


def test_edit_json(home, monkeypatch):  # 34
    monkeypatch.setattr(undo, "_stamp", lambda: "20261002-101500")
    path = home / "s.json"
    original = '{\n    "a": 1\n}\n'
    path.write_text(original)
    path.chmod(0o640)
    seen = []

    def add(obj):
        obj["b"] = "é"
        return obj

    res = jsonfile.edit_json(path, add, command="agent install", confirm=_yes(seen))
    assert res.changed and seen == [res.diff] and '+    "b": "é"' in res.diff
    assert path.read_text() == '{\n    "a": 1,\n    "b": "é"\n}\n' == res.text
    assert path.stat().st_mode & 0o777 == 0o640
    assert res.backup.read_text() == original and res.backup.stat().st_mode & 0o777 == 0o600
    # V-015: backups live in gitpro's state dir, never next to the file (work trees, `git add .`)
    assert res.backup == home / ".local/state/gitpro/backup-20261002-101500/s.json"
    assert [p.name for p in home.iterdir() if p.name.startswith("s.json")] == ["s.json"]
    subprocess.run(["sh", str(res.undo)], check=True, timeout=TIMEOUT)
    assert path.read_text() == original

    same = jsonfile.edit_json(path, lambda o: o, command="agent install", confirm=_yes([]))
    assert not same.changed and same.backup is None and same.undo is None and same.diff == ""


def test_edit_json_declined(home):
    path = home / "s.json"
    path.write_text('{"a": 1}\n')
    res = jsonfile.edit_json(path, lambda o: {**o, "b": 2}, command="x", confirm=lambda d: False)
    assert not res.changed and res.diff and path.read_text() == '{"a": 1}\n' and res.backup is None


def test_edit_json_concurrent_change(home):
    path = home / "s.json"
    path.write_text('{"a": 1}\n')

    def sneaky(diff):
        path.write_text('{"a": 2}\n')
        return True

    with pytest.raises(ConfigError, match="changed while gitpro was running; nothing written"):
        jsonfile.edit_json(path, lambda o: {**o, "b": 2}, command="x", confirm=sneaky)
    assert path.read_text() == '{"a": 2}\n'


def test_jsonc(home):
    path = home / "devcontainer.json"
    path.write_text('{\n  // c\n  "a": "x // not a comment", /* b */\n  "l": [1, 2,],\n}\n')
    obj, text = jsonfile.read(path, jsonc=True)
    assert obj == {"a": "x // not a comment", "l": [1, 2]} and text.startswith("{")
    with pytest.raises(ConfigError, match="has comments; gitpro cannot edit it without losing them"
                                          " — add this by hand: SNIP"):
        jsonfile.edit_json(path, lambda o: {**o, "b": 1}, command="x", jsonc=True, confirm=_yes([]),
                           snippet="SNIP")
    plain = home / "plain.jsonc"
    plain.write_text('{\n\t"a": 1\n}\n')
    res = jsonfile.edit_json(plain, lambda o: {**o, "b": 1}, command="x", jsonc=True, confirm=_yes([]))
    assert plain.read_text() == '{\n\t"a": 1,\n\t"b": 1\n}\n' and res.changed


def test_parse_errors_and_create(home):
    bad = home / "bad.json"
    bad.write_text('{"a": }')
    with pytest.raises(ConfigError, match="cannot parse"):
        jsonfile.read(bad)
    new = home / "dir/new.json"
    with pytest.raises(ConfigError):
        jsonfile.edit_json(new, lambda o: o, command="x", confirm=_yes([]))
    res = jsonfile.edit_json(new, lambda o: {**o, "k": 1}, command="x", create={}, confirm=_yes([]), mkdir=True)
    assert json.loads(new.read_text()) == {"k": 1} and res.backup is None
    assert "rm -f" in res.undo.read_text()
    ssh = home / ".ssh/x.json"
    ssh.parent.mkdir()
    ssh.write_text("{}\n")
    ssh.chmod(0o644)
    jsonfile.edit_json(ssh, lambda o: {"a": 1}, command="x", confirm=_yes([]))
    assert ssh.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------- jsonout

def test_jsonout(capsys):
    jsonout.emit("thing", {"x": "é", "ok": False})
    assert capsys.readouterr().out == '{\n  "ok": false,\n  "command": "thing",\n  "x": "é"\n}\n'
    jsonout.error("why", "boom")
    assert json.loads(capsys.readouterr().out) == {"ok": False, "command": "why", "error": "boom"}

    class A:
        json = True
    assert jsonout.wants_json(A()) and not jsonout.wants_json(object())


# ---------------------------------------------------------------- render.is_generated

def test_is_generated_after_shebang():
    assert render.is_generated(f"{render.HEADER}\n")
    assert render.is_generated(f"#!/bin/sh\n{render.HEADER}\nexit 0\n")
    assert not render.is_generated(f"#!/bin/sh\necho\n{render.HEADER}\n")
    assert not render.is_generated(f"x\n{render.HEADER}\n")


# ---------------------------------------------------------------- forge (35)

TOKEN = "tok-SECRET-123"

GH = f"""
import json
if argv[:2] == ["auth", "token"]:
    if "nobody" in argv:
        print("no account", file=sys.stderr); sys.exit(1)
    print("{TOKEN}"); sys.exit(0)
if argv[:2] == ["api", "user"]:
    print("Me-Work"); sys.exit(0)
if argv[:2] == ["repo", "view"]:
    name = argv[2]
    if name == "corp-org/gone":
        print("GraphQL: Could not resolve to a Repository with the name 'corp-org/gone'.", file=sys.stderr)
        sys.exit(1)
    if name == "corp-org/flaky":
        print("HTTP 502: bad gateway", file=sys.stderr); print("more", file=sys.stderr); sys.exit(1)
    if name == "corp-org/old":
        print(json.dumps({{"nameWithOwner": "corp-org/new"}})); sys.exit(0)
    print(json.dumps({{"nameWithOwner": name.lower()}})); sys.exit(0)
sys.exit(3)
"""


def test_gh_env(fake_bin, monkeypatch, capsys):  # 35
    fake_bin.add("gh", GH)
    monkeypatch.setenv("GH_TOKEN", "OLD")
    monkeypatch.setenv("GITHUB_TOKEN", "OLD2")
    env = forge.gh_env("github.com", "me-work")
    assert env["GH_TOKEN"] == TOKEN and env["GH_HOST"] == "github.com"
    assert "GH_ENTERPRISE_TOKEN" not in env and "GITHUB_TOKEN" not in env
    entries = fake_bin.entries("gh")
    assert entries[0]["argv"] == ["auth", "token", "-u", "me-work", "-h", "github.com"]
    assert entries[0]["GH_TOKEN"] == "unset" and entries[0]["stdin"] == b""
    ghe = forge.gh_env("ghe.corp", "me")
    assert ghe["GH_ENTERPRISE_TOKEN"] == TOKEN and "GH_TOKEN" not in ghe and ghe["GH_HOST"] == "ghe.corp"
    with pytest.raises(ConfigError, match="gh has no login for nobody on github.com; run: gh auth login -h github.com"):
        forge.gh_env("github.com", "nobody")

    acc = forge.Account("github.com", "gh", "me-work")
    assert forge.logged_in(acc)
    assert not forge.logged_in(forge.Account("github.com", "gh", "someone"))
    assert forge.repo_exists(acc, "corp-org/app") == ("ok", None)
    assert forge.repo_exists(acc, "Corp-Org/App") == ("ok", None)  # case only: not a rename
    assert forge.repo_exists(acc, "corp-org/old") == ("renamed", "corp-org/new")
    assert forge.repo_exists(acc, "corp-org/gone") == ("missing", None)
    assert forge.repo_exists(acc, "corp-org/flaky") == ("unknown", "HTTP 502: bad gateway")
    assert forge.repo_exists(acc, "corp-org", "app") == ("ok", None)  # 03's (owner, repo) form
    r = forge.run(acc, ["repo", "view", "corp-org/app", "--json", "nameWithOwner"])
    assert r.returncode == 0 and "corp-org/app" in r.stdout

    cap = capsys.readouterr()
    assert TOKEN not in cap.out + cap.err
    assert TOKEN not in fake_bin.log("gh").read_text()
    assert all(TOKEN not in " ".join(c) for c in fake_bin.calls("gh"))
    # the repo view calls carried the token in their environment
    assert any(e["argv"][:2] == ["repo", "view"] and e["GH_TOKEN"] == "set" for e in fake_bin.entries("gh"))


def test_gh_too_old(fake_bin):  # V-029: `gh auth token -u` needs gh 2.40
    fake_bin.add("gh", 'sys.stderr.write("unknown shorthand flag: \'u\' in -u\\n"); sys.exit(1)')
    with pytest.raises(ConfigError, match="gh 2.40 or newer is needed"):
        forge.gh_env("github.com", "me")


def test_glab_run_and_missing_cli(fake_bin, monkeypatch):
    fake_bin.add("glab", 'print(env.get("GITLAB_HOST")); sys.exit(0)')
    monkeypatch.setenv("GITLAB_TOKEN", "OLD")
    r = forge.run(forge.Account("gitlab.corp.test", "glab", "me"), ["auth", "status"])
    assert r.stdout.strip() == "gitlab.corp.test"
    assert fake_bin.entries("glab")[0]["GITLAB_TOKEN"] == "unset"
    monkeypatch.setenv("PATH", str(fake_bin.path))
    assert not forge.available("gh") and forge.available("glab")
    assert forge.run(forge.Account("x.test", "gh", "me"), ["api"]).returncode == 127


def test_timeout(fake_bin):
    fake_bin.add("glab", "import time; time.sleep(5)")
    r = forge.run(forge.Account("gitlab.com", "glab", "me"), ["x"], timeout=1)
    assert r.returncode == 124


def test_accounts(home):
    text = ('[default]\ngh_account = "me"\nglab_account = "me-gl"\n'
            '[[identity]]\nid = "w"\nname = "W"\nemail = "w@corp.test"\nhost = "ghe.corp.test"\ngh_account = "w"\n'
            '[[identity]]\nid = "g"\nname = "G"\nemail = "g@corp.test"\nhost = "gitlab.corp.test"\n'
            'glab_account = "g"\n'
            '[[identity]]\nid = "d"\nname = "D"\nemail = "d@corp.test"\nhost = "github.com"\ngh_account = "me"\n')
    from conftest import write_config
    c = config.load_all(write_config(home, text))
    assert forge.accounts(c) == [
        forge.Account("github.com", "gh", "me"), forge.Account("gitlab.com", "glab", "me-gl"),
        forge.Account("ghe.corp.test", "gh", "w"), forge.Account("gitlab.corp.test", "glab", "g")]


def test_announce_and_no_online(capsys):
    accs = [forge.Account("github.com", "gh", "me"), forge.Account("github.com", "gh", "w")]
    forge.announce("whether each remote exists", accs, "owner/repo names")
    assert capsys.readouterr().out == ("online: asking github.com via gh (account me, w) whether each remote exists; "
                                       "sends owner/repo names\n")
    assert forge.no_online_error("lost", "remote check", accs, "owner/repo names") == 2
    err = capsys.readouterr().err
    assert err.startswith("✗ lost needs the network: it asks github.com via gh (account me, w)")
    assert "→ re-run with --online, or set [forge] online = true" in err


# ---------------------------------------------------------------- fake_bin (37)

def test_fake_bin_round_trip(fake_bin):  # 37
    fake_bin.add("gh", 'print("hi"); sys.exit(4)')
    r = subprocess.run(["gh", "repo", "a b", "it's \"q\"", ""], capture_output=True, text=True, input="in\n",
                       timeout=TIMEOUT)
    assert r.returncode == 4 and r.stdout == "hi\n"
    assert fake_bin.calls("gh") == [["repo", "a b", "it's \"q\"", ""]]
    assert fake_bin.entries("gh")[0]["stdin"] == b"in\n"
    assert fake_bin.calls("glab") == []
    assert os.access(fake_bin.path / "gh", os.X_OK)
