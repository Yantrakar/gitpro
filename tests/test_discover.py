"""Package 04: `gitpro discover` and `init --discover` (spec 04 §10, tests 1-15, plus TOML checks).

Every test runs in a temporary HOME with a PATH that holds only fake executables and git: the real
gh/glab, shell history, editor recents and ~/.ssh are never touched.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG, git, run, write_config

from gitpro import config, discover

TOML = config._toml   # tomllib, or tomli before 3.11


@pytest.fixture(autouse=True)
def sandbox(home, tmp_path, monkeypatch):
    """PATH = a private bin dir holding only git (fake_bin goes in front of it); no shell-history or
    XDG variables from the real environment."""
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    (sysbin / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(sysbin))
    for var in ("HISTFILE", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    return sysbin


@pytest.fixture
def fbin(tmp_path, monkeypatch, sandbox):
    """conftest's fake_bin, but in front of the sandbox PATH only (never the real PATH)."""
    from conftest import FakeBin
    path = tmp_path / "fakebin"
    path.mkdir()
    monkeypatch.setenv("PATH", f"{path}{os.pathsep}{sandbox}")
    return FakeBin(path)


def disc(capsys, *argv: str, rc: int = 0) -> dict:
    capsys.readouterr()
    assert run("discover", "--json", *argv) == rc
    return json.loads(capsys.readouterr().out)


def text(capsys, *argv: str, rc: int = 0):
    capsys.readouterr()
    assert run(*argv) == rc
    c = capsys.readouterr()
    return c.out, c.err


def owners_of(d: dict) -> dict:
    return {(h["host"], o["owner"]): o for h in d["hosts"] for o in h["owners"]}


def mkrepo(path: Path, url: str | None = None, **local) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=60)
    if url:
        git(path, "remote", "add", "origin", url)
    for k, v in local.items():
        git(path, "config", f"user.{k}", v)
    return path


def write_zsh(home: Path, lines, raw: bytes = b"") -> Path:
    p = home / ".zsh_history"
    body = b"".join(f": 17000000{n:02d}:0;{ln}\n".encode() for n, ln in enumerate(lines))
    p.write_bytes(body + raw)
    return p


def blocks(toml_text: str) -> list:
    return TOML.loads(toml_text).get("identity", [])


def loads_with_gitpro(home: Path, toml_text: str, fill: dict | None = None) -> config.Config:
    """The snippet under a [default] table, blanks filled, through gitpro's own config check."""
    for old, new in (fill or {}).items():
        toml_text = toml_text.replace(old, new)
    path = home / "check.toml"
    path.write_text("[default]\n\n" + toml_text)
    path.chmod(0o600)
    return config.load_all(path)


# ---------------------------------------------------------------- 1-3 shell history

HIST = [
    "git clone git@github.com:corp-org/a.git ~/work/a",
    "curl -H 'Authorization: Bearer SECRET' https://api.corp.test/x",
    "git clone https://gitlab.com/grp/sub/b",
]


def test_history_only_with_flag(home, capsys):  # 1
    write_zsh(home, HIST)
    d = disc(capsys)
    assert d["sources"]["history"] is None and d["hosts"] == []
    out, _ = text(capsys, "discover")
    assert "add --history" in out and "~/.zsh_history" in out

    (home / "work").mkdir()           # roots are suggested only when they exist
    d = disc(capsys, "--history")
    seen = owners_of(d)
    assert ("github.com", "corp-org") in seen and ("gitlab.com", "grp/sub") in seen
    assert str(home / "work") in [r["path"] for r in d["roots"]]
    assert d["sources"]["history"]["lines"] == 2
    assert d["sources"]["history"]["files"] == [str(home / ".zsh_history")]
    out, _ = text(capsys, "discover", "--history")
    assert "history: 2 clone lines in ~/.zsh_history" in out


def test_history_secrets_and_userinfo(home, capsys):  # 2
    write_zsh(home, [
        "git clone https://me:ghp_abcdefghijklmnopqrstuvwxyz@github.com/me/x",
        "git clone https://me@github.com/me/y?x=1",
        "curl -H 'Authorization: Bearer SECRET' https://api.corp.test/x",
        "cd /tmp && git clone --depth 1 -b main https://oauth2:abcdef@gitlab.com/g/z.git",
    ])
    capsys.readouterr()
    assert run("discover", "--json", "--history") == 0
    c = capsys.readouterr()
    d = json.loads(c.out)
    assert d["sources"]["history"]["secrets_skipped"] == 2
    assert ("github.com", "me") in owners_of(d)
    assert owners_of(d)[("github.com", "me")]["repos"] == 1      # only y: x was dropped
    assert ("gitlab.com", "g") not in owners_of(d)
    for blob in (c.out, c.err, d["toml"]):
        assert "ghp_" not in blob and "SECRET" not in blob and "Authorization" not in blob
        assert "abcdef" not in blob
    assert "me@" not in d["toml"] and "?x=1" not in d["toml"]
    out, err = text(capsys, "discover", "--history", "-v")
    assert "history  https://github.com/me/y" in out
    for blob in (out, err):
        assert "me@" not in blob and "?x=1" not in blob and "ghp_" not in blob
        assert "curl" not in blob and "SECRET" not in blob and "git clone" not in blob
    assert "1 skipped" not in out and "2 skipped: looked like a secret" in out


def test_fish_and_zsh_metafied(home, capsys):  # 3
    fish = home / ".local/share/fish/fish_history"
    fish.parent.mkdir(parents=True)
    fish.write_text("- cmd: git clone git@github.com:me/y.git\n  when: 1700000000\n"
                    "- cmd: ls\n  when: 1700000001\n")
    # "dăn": ă is C4 83 in UTF-8; zsh writes the 0x83 byte as 0x83 0xA3 (Meta, byte ^ 0x20).
    owner = "dăn".encode()
    metafied = owner.replace(b"\x83", b"\x83\xa3")
    write_zsh(home, [], raw=b": 1700000000:0;git clone git@github.com:" + metafied + b"/app.git\n")
    d = disc(capsys, "--history")
    seen = owners_of(d)
    assert ("github.com", "me") in seen
    assert ("github.com", "dăn") in seen
    assert d["sources"]["history"]["lines"] == 2
    # a non-ASCII owner is reported, but never put in owners = [...] (gitpro's config rejects it)
    for b in blocks(d["toml"]):
        assert "dăn" not in b.get("owners", [])


def test_histfile_and_gh_shorthand(home, capsys, monkeypatch):
    hist = home / "custom_hist"
    hist.write_text("gh repo clone corp-org/tool ~/src/tool\nglab repo clone grp/sub/thing\n"
                    "git clone 'git@github.com:corp-org/quoted.git' \"$HOME/src/quoted\"\n")
    monkeypatch.setenv("HISTFILE", str(hist))
    (home / "src").mkdir()
    d = disc(capsys, "--history")
    seen = owners_of(d)
    assert seen[("github.com", "corp-org")]["repos"] == 2
    assert ("gitlab.com", "grp/sub") in seen
    assert [r["path"] for r in d["roots"]] == [str(home / "src")]
    assert d["sources"]["history"]["files"] == [str(hist)]


# ---------------------------------------------------------------- 4-5 editor recents

def vscdb(path: Path, entries) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    con.execute("INSERT INTO ItemTable VALUES (?, ?)",
                ("history.recentlyOpenedPathsList", json.dumps({"entries": entries})))
    con.execute("INSERT INTO ItemTable VALUES (?, ?)", ("other.key", "x"))
    con.commit()
    con.close()
    return path


def test_vscode_recents(home, tmp_path, capsys):  # 4
    work = home / "my work"                                   # a space in the root
    mkrepo(work / "z", "git@github.com:corp-org/z.git")
    (home / "notes").mkdir()
    db = vscdb(home / ".config/Code/User/globalStorage/state.vscdb", [
        {"folderUri": (work / "z").as_uri()},
        {"folderUri": (home / "notes").as_uri()},
        {"folderUri": "vscode-remote://ssh-remote%2Bbox/home/u/x"},
        {"fileUri": (home / "notes/todo.md").as_uri()},
    ])
    before = db.stat()
    d = disc(capsys)
    assert owners_of(d)[("github.com", "corp-org")]["repos"] == 1
    assert [r["path"] for r in d["roots"]] == [str(work)]
    v = d["sources"]["vscode"]
    assert v["remote_entries"] == 1 and v["entries"] == 2 and v["files"] == [str(db)]
    after = db.stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
    assert sorted(p.name for p in db.parent.iterdir()) == ["state.vscdb"]
    assert '"~/my work"' in d["toml"]
    loads_with_gitpro(home, d["toml"], {'name = ""': 'name = "X"', 'email = ""': 'email = "x@corp.test"'})


def test_vscode_bad_db_warns(home, capsys):
    db = home / ".config/Code/User/globalStorage/state.vscdb"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a database")
    capsys.readouterr()
    assert run("discover") == 0
    assert "⚠ vscode: could not read recents" in capsys.readouterr().err


def test_jetbrains_recents(home, capsys):  # 5
    mkrepo(home / "proj", "git@github.com:me/proj.git")
    (home / "other/notproj").mkdir(parents=True)
    xml = home / ".config/JetBrains/IntelliJIdea2024.1/options/recentProjects.xml"
    xml.parent.mkdir(parents=True)
    xml.write_text("""<application>
  <component name="RecentProjectsManager">
    <option name="additionalInfo">
      <map>
        <entry key="$USER_HOME$/proj"><value><RecentProjectMetaInfo /></value></entry>
        <entry key="$USER_HOME$/other/notproj"><value><RecentProjectMetaInfo /></value></entry>
      </map>
    </option>
  </component>
</application>
""")
    d = disc(capsys)
    assert ("github.com", "me") in owners_of(d)
    assert [r["path"] for r in d["roots"]] == [str(home)]
    assert d["sources"]["jetbrains"]["files"] == [str(xml)]
    out, _ = text(capsys, "discover", "-v")
    assert "jetbrains  ~/proj  git@github.com:me/proj.git" in out
    assert "notproj" not in out


# ---------------------------------------------------------------- 6 ssh config

@pytest.fixture
def never_open(monkeypatch):
    """Once armed (never_open[0] holds the names), fail the test when any file with one of those names
    is opened through open, io.open or os.open."""
    import builtins
    import io
    names: set = set()
    opened: list = []

    def guard(real):
        def wrapper(file, *a, **k):
            if (names and isinstance(file, (str, bytes, os.PathLike))
                    and Path(os.fsdecode(file)).name in names):
                opened.append(os.fsdecode(file))
                raise AssertionError(f"opened {file}")
            return real(file, *a, **k)
        return wrapper
    for mod, attr in ((builtins, "open"), (io, "open"), (os, "open")):
        monkeypatch.setattr(mod, attr, guard(getattr(mod, attr)))
    yield names, opened
    assert opened == []


def test_ssh_config(home, capsys, never_open):  # 6
    ssh = home / ".ssh"
    (ssh / "work").mkdir(parents=True)
    (ssh / "config").write_text(
        "Include config.d/*\nInclude work/*.conf\n\n"
        "Host github.com\n  IdentityFile ~/.ssh/id_default\n\n"
        "# BEGIN gitpro -- keep first\nHost github-managed\n  HostName github.com\n"
        "  IdentityFile ~/.ssh/id_managed\n# END gitpro\n\n"
        "Host box\n  HostName box.lan\n  User me\n")
    (ssh / "work/corp.conf").write_text("Host github-work\n  HostName github.com\n  IdentityFile ~/.ssh/id_work\n")
    (ssh / "id_work").write_text("PRIVATE KEY MUST NOT BE READ")
    (ssh / "id_work").chmod(0)                       # and never_open fails the test on any attempt
    (ssh / "id_work.pub").write_text("ssh-ed25519 AAAA me@corp.test\n")
    never_open[0].update({"id_work", "id_default", "id_managed"})
    d = disc(capsys)
    assert [a["alias"] for a in d["aliases"]] == ["github-work"]
    a = d["aliases"][0]
    assert a["host"] == "github.com" and a["key"] == "~/.ssh/id_work" and a["user"] is None and a["port"] is None
    assert d["sources"]["ssh"]["files"] == [str(ssh / "config"), str(ssh / "work/corp.conf")]
    assert d["sources"]["ssh"]["aliases"] == 1
    out, _ = text(capsys, "discover")
    assert "github-work → github.com  key ~/.ssh/id_work" in out
    assert "github-managed" not in out and "box" not in out


def test_ssh_include_star_never_opens_keys(home, capsys, never_open):
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "config").write_text("Include ~/.ssh/*\nInclude *\n")
    for name in ("id_ed25519", "known_hosts", "authorized_keys", "corp.pem", "corp.key", "signer"):
        (ssh / name).write_text("Host leaked\n  HostName github.com\n")
    (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAA me@corp.test\n")
    (ssh / "signer.pub").write_text("ssh-ed25519 AAAA me@corp.test\n")   # signer has a .pub: a key
    (ssh / "deploy").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nHost leaked2\n  HostName github.com\n")
    (ssh / "extra").write_text("Host gh-extra\n  HostName github.com\n")
    never_open[0].update({"id_ed25519", "known_hosts", "authorized_keys", "corp.pem", "corp.key", "signer",
                          "id_ed25519.pub"})
    d = disc(capsys, "--source", "ssh", "-v")
    assert [a["alias"] for a in d["aliases"]] == ["gh-extra"]
    assert d["sources"]["ssh"]["files"] == [str(ssh / "config"), str(ssh / "extra")]


@pytest.mark.parametrize("hostname,forge", [("ssh.github.com", "github.com"), ("altssh.gitlab.com", "gitlab.com")])
def test_ssh_port_443_alias_maps_to_the_forge(home, tmp_path, capsys, hostname, forge):
    (home / ".ssh").mkdir()
    (home / ".ssh/config").write_text(f"Host {forge}\n  Hostname {hostname}\n  Port 443\n"
                                      f"Host fg443\n  Hostname {hostname}\n  Port 443\n  IdentityFile ~/.ssh/id_x\n")
    root = home / "src"
    mkrepo(root / "a", "git@fg443:corp-org/a.git", email="me@corp.test", name="Me")
    mkrepo(root / "b", f"https://{forge}/corp-org/b.git", email="me@corp.test", name="Me")
    mkrepo(root / "c", "https://fg443/other/c.git")          # https never goes through ssh aliases
    write_config(home, f'[default]\n[scan]\nroots = ["{root}"]\n')
    d = disc(capsys)
    seen = owners_of(d)
    assert seen[(forge, "corp-org")]["repos"] == 2 and ("fg443", "other") in seen
    assert d["aliases"] == []
    b = next(b for b in blocks(d["toml"]) if b["host"] == forge)
    assert "ssh_alias" not in b and "ssh_port" not in b


def test_ssh_alias_joins_repo_and_block(home, tmp_path, capsys):
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "config").write_text("Host github-work\n  HostName github.com\n  User git\n  Port 2222\n"
                                "  IdentityFile ~/.ssh/id_work\n")
    root = tmp_path / "src"
    mkrepo(root / "app", "git@github-work:corp-org/app.git", email="me@corp.test", name="Work Me")
    mkrepo(root / "mine", "git@github.com:me/mine.git", email="me@personal.test")
    write_config(home, f'[default]\n[scan]\nroots = ["{root}"]\n')
    d = disc(capsys)
    assert ("github.com", "corp-org") in owners_of(d)          # the alias host was mapped back
    by_owner = {tuple(b["owners"]): b for b in blocks(d["toml"])}
    work = by_owner[("corp-org",)]
    assert work["ssh_alias"] == "github-work" and work["ssh_key"] == "~/.ssh/id_work"
    assert work["ssh_port"] == 2222 and "ssh_user" not in work
    assert "ssh_alias" not in by_owner[("me",)]
    loads_with_gitpro(home, d["toml"], {'name = ""': 'name = "Me"'})


# ---------------------------------------------------------------- 7 includeIf

def test_includeif(home, capsys):  # 7
    (home / ".gitconfig").write_text('[includeIf "gitdir:~/work/"]\n\tpath = ~/.gitconfig-work\n'
                                     '[includeIf "hasconfig:remote.*.url:git@gitlab.com:grp/**"]\n'
                                     '\tpath = .gitconfig-grp\n')
    (home / ".gitconfig-work").write_text('[user]\n\tname = Wörk \\"Me\\" O\'Neil\n\temail = me@corp.test\n')
    (home / ".gitconfig-grp").write_text('[user]\n\tname = Grp Me\n\temail = me@grp.test\n')
    mkrepo(home / "work/app", "git@github.com:corp-org/app.git")
    d = disc(capsys)
    assert d["sources"]["includeif"]["rules"] == 2
    assert str(home / "work") in [r["path"] for r in d["roots"]]
    corp = owners_of(d)[("github.com", "corp-org")]
    assert corp["emails"] == {"me@corp.test": 1}
    bs = {b["id"]: b for b in blocks(d["toml"])}
    assert bs["corp-org"]["name"] == 'Wörk "Me" O\'Neil' and bs["corp-org"]["email"] == "me@corp.test"
    assert bs["grp"]["email"] == "me@grp.test" and bs["grp"]["host"] == "gitlab.com"
    cfg = loads_with_gitpro(home, d["toml"])           # nothing to fill in: every value was seen
    assert {i.id for i in cfg.identities} == {"corp-org", "grp"}


def test_includeif_without_repos(home, capsys):
    (home / ".gitconfig").write_text('[includeIf "gitdir:~/work/"]\n\tpath = ~/.gitconfig-work\n')
    (home / ".gitconfig-work").write_text("[user]\n\tname = Work Me\n\temail = me@corp.test\n")
    d = disc(capsys)
    (b,) = blocks(d["toml"])
    assert b["email"] == "me@corp.test" and b["host"] == "" and b["owners"] == []
    assert "includeIf" in d["toml"]
    loads_with_gitpro(home, d["toml"], {'host = ""': 'host = "github.com"', "owners = []": 'owners = ["corp-org"]'})


# ---------------------------------------------------------------- 8 repos on disk

def test_repos_local_email_and_depth(home, tmp_path, capsys):  # 8
    root = tmp_path / "src"
    mkrepo(root / "app", "git@github.com:corp-org/app.git", email="me@corp.test")
    mkrepo(root / "a/b/c/deep", "git@github.com:corp-org/deep.git", email="deep@corp.test")
    mkrepo(root / "a/b/ok", "https://github.com/corp-org/ok", email="me@corp.test")
    write_config(home, f'[default]\n[scan]\nroots = ["{root}"]\n')
    d = disc(capsys)
    corp = owners_of(d)[("github.com", "corp-org")]
    assert corp["repos"] == 2 and corp["emails"] == {"me@corp.test": 2}
    assert d["sources"]["repos"]["count"] == 2
    assert d["sources"]["repos"]["roots"] == [str(root)]
    assert "[scan]" not in d["toml"]                  # the config already has [scan] roots


# ---------------------------------------------------------------- 9 existing config

def test_existing_config(home, tmp_path, capsys):  # 9
    root = tmp_path / "src"
    mkrepo(root / "app", "git@github.com:corp-org/app.git", email="me@corp.test")
    mkrepo(root / "x", "git@github.com:someone/x.git", email="me@corp.test")
    write_config(home, DEFAULT_CONFIG + f'\n[scan]\nroots = ["{root}"]\n')
    d = disc(capsys)
    seen = owners_of(d)
    assert seen[("github.com", "corp-org")]["in_config"] is True
    assert seen[("github.com", "someone")]["in_config"] is False
    assert "# github.com/corp-org: already in config" in d["toml"]
    owners = [o for b in blocks(d["toml"]) for o in b["owners"]]
    assert owners == ["someone"]
    ids = [b["id"] for b in blocks(d["toml"])]
    assert "work-github" not in ids and "personal" not in ids


def test_broken_config_is_ignored_with_warning(home, capsys):
    write_config(home, "[default]\nbogus = 1\n")
    capsys.readouterr()
    assert run("discover") == 0
    assert "identities are ignored" in capsys.readouterr().err


# ---------------------------------------------------------------- 10 online

GH_JSON = r'''
import json
if argv[:2] == ["auth", "status"]:
    print(json.dumps({"hosts": {"github.com": [
        {"state": "success", "active": True, "host": "github.com", "login": "me-work", "token": "gho_SHOULDNOTSHOW"},
        {"state": "success", "active": False, "host": "github.com", "login": "me-personal"}]}}))
    sys.exit(0)
if argv[:2] == ["api", "user/emails"]:
    print(json.dumps([{"email": "me@corp.test", "verified": True, "primary": True},
                      {"email": "old@corp.test", "verified": False, "primary": False}]))
    sys.exit(0)
sys.exit(9)
'''

GH_TEXT = r'''
import json
if argv[:2] == ["auth", "status"]:
    if "--json" in argv:
        print("unknown flag: --json", file=sys.stderr)
        sys.exit(1)
    print("github.com\n  ✓ Logged in to github.com account me-work (keyring)\n  - Active account: true\n"
          "  - Git operations protocol: ssh\n  - Token: gho_************************************\n\n"
          "  ✓ Logged in to github.com account me-personal (keyring)\n  - Active account: false\n")
    sys.exit(0)
if argv[:2] == ["api", "user/emails"]:
    print(json.dumps([{"email": "me@corp.test", "verified": True, "primary": True}]))
    sys.exit(0)
sys.exit(9)
'''


@pytest.mark.parametrize("body", [GH_JSON, GH_TEXT], ids=["json", "text"])
def test_online_gh(home, tmp_path, fbin, capsys, body):  # 10
    fbin.add("gh", body)
    root = tmp_path / "src"
    mkrepo(root / "app", "git@github.com:corp-org/app.git", email="me@corp.test", name="Work Me")
    write_config(home, f'[default]\n[scan]\nroots = ["{root}"]\n')
    d = disc(capsys, "--online")
    acc = {a["login"]: a for a in d["accounts"]}
    assert acc["me-work"]["verified"] is True and acc["me-work"]["emails"] == ["me@corp.test"]
    assert acc["me-work"]["cli"] == "gh" and acc["me-work"]["host"] == "github.com"
    assert acc["me-personal"]["verified"] is False
    (b,) = blocks(d["toml"])
    assert b["gh_account"] == "me-work"
    assert not [c for c in fbin.calls("gh") if c[:2] == ["auth", "token"]]
    assert all(e["GH_TOKEN"] == "unset" for e in fbin.entries("gh"))
    out, err = text(capsys, "discover", "--online")
    assert out.splitlines()[0].startswith("online: asking github.com via gh")
    assert "gho_" not in out + err and "gho_" not in json.dumps(d)
    loads_with_gitpro(home, d["toml"])


def test_online_gh_without_login_and_email_scope(home, fbin, capsys):
    fbin.add("gh", 'print("You are not logged into any GitHub hosts.", file=sys.stderr); sys.exit(1)')
    capsys.readouterr()
    assert run("discover", "--online", "--json") == 0
    c = capsys.readouterr()
    assert json.loads(c.out)["accounts"] == []
    assert "⚠ gh has no login; account discovery skipped" in c.err

    fbin.add("gh", r'''
import json
if argv[:2] == ["auth", "status"]:
    print(json.dumps({"hosts": {"github.com": [{"state": "success", "active": True, "login": "me"}]}})); sys.exit(0)
print("gh: Not Found (HTTP 404)", file=sys.stderr); sys.exit(1)
''')
    capsys.readouterr()
    assert run("discover", "--online", "--json") == 0
    c = capsys.readouterr()
    assert json.loads(c.out)["accounts"][0]["verified"] is False
    assert "gh auth refresh -h github.com -s user:email" in c.err


def test_online_glab(home, fbin, capsys):
    fbin.add("glab", r'''
if argv[:2] == ["auth", "status"]:
    print("gitlab.com\n  ✓ Logged in to gitlab.com as jdoe (/home/x/.config/glab-cli/config.yml)\n"
          "  ✓ Token: **************************", file=sys.stderr)
    sys.exit(0)
sys.exit(9)
''')
    d = disc(capsys, "--online")
    assert d["accounts"] == [{"host": "gitlab.com", "cli": "glab", "login": "jdoe", "emails": [], "verified": False}]
    assert d["sources"]["glab"] == {"logins": 1} and d["sources"]["gh"] is None


# ---------------------------------------------------------------- 11-12 init --discover

def test_init_discover_creates(home, capsys):  # 11
    write_zsh(home, HIST)
    (home / ".gitconfig").write_text('[includeIf "gitdir:~/work/"]\n\tpath = ~/.gitconfig-work\n')
    (home / ".gitconfig-work").write_text("[user]\n\tname = Work Me\n\temail = me@corp.test\n")
    (home / "work").mkdir()
    out, _ = text(capsys, "init", "-y", "--discover", "--history")
    target = home / ".config/gitpro.toml"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    body = target.read_text()
    from importlib import resources
    example = resources.files("gitpro").joinpath("example.toml").read_text()
    assert body.startswith(example)
    tail = body[len(example):]
    assert "# ---- discovered on " in tail and "by gitpro init --discover; uncomment what is right ----" in tail
    assert all(ln.startswith("#") for ln in tail.splitlines() if ln.strip())
    assert '# [[identity]]' in tail and '# roots = ["~/work"]' in tail
    assert "created" in out and "appended" in out
    assert config.load_scan(target) == config.Scan()
    with pytest.raises(config.ConfigError, match="placeholder"):
        config.load(target)
    # uncommenting the discovered section yields TOML that parses
    section = "\n".join(ln[2:] if ln.startswith("# ") else ln.lstrip("#")
                        for ln in tail.splitlines()[2:])
    TOML.loads(section)


def test_init_discover_existing(home, capsys):  # 12
    write_zsh(home, HIST)
    target = write_config(home, DEFAULT_CONFIG)
    before = target.read_bytes()
    out, _ = text(capsys, "init", "--discover", "--history")
    assert "already exists" in out
    assert target.read_bytes() == before
    assert "[[identity]]" in out and "gitlab.com" in out and "→ paste what you need" in out


def test_init_flags_need_discover(home, capsys):
    capsys.readouterr()
    assert run("init", "--history") == 2
    assert not (home / ".config/gitpro.toml").exists()
    assert run("init") == 0                            # the plain init is unchanged


# ---------------------------------------------------------------- 13-15 empty home, usage errors

def test_empty_home(home, capsys):  # 13
    out, _ = text(capsys, "discover")
    assert "nothing discovered; roots: ~" in out
    d = disc(capsys)
    assert d["hosts"] == [] and d["aliases"] == [] and d["roots"] == [] and d["accounts"] == []
    assert d["ok"] is True and d["command"] == "discover"
    assert "[[identity]]" in d["toml"]
    TOML.loads(d["toml"])
    assert not (home / ".config").exists()             # nothing written


def test_bad_sources(home, capsys):  # 14
    _, err = text(capsys, "discover", "--source", "bogus", rc=2)
    assert "✗ unknown source 'bogus' (history, vscode, jetbrains, ssh, includeif, repos, gh, glab)" in err
    _, err = text(capsys, "discover", "--source", "history", rc=2)
    assert "history is read only with --history" in err
    d = disc(capsys, "--source", "history", "--json", rc=2)
    assert d["ok"] is False


def test_source_restricts(home, capsys):
    write_zsh(home, HIST)
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "config").write_text("Host github-work\n  HostName github.com\n  IdentityFile ~/.ssh/id_work\n")
    d = disc(capsys, "--history", "--source", "ssh")
    assert d["sources"]["history"] is None and d["hosts"] == [] and len(d["aliases"]) == 1
    d = disc(capsys, "--history", "--source", "history")
    assert d["sources"]["ssh"] is None and d["aliases"] == [] and d["hosts"]


def test_online_without_cli(home, capsys):  # 15
    _, err = text(capsys, "discover", "--online", rc=2)
    assert "needs gh or glab" in err


# ---------------------------------------------------------------- sanitise and TOML

@pytest.mark.parametrize("url,want", [
    ("git@github.com:corp-org/a.git", "git@github.com:corp-org/a.git"),
    ("https://me@github.com/me/y?x=1#frag", "https://github.com/me/y"),
    ("https://me:pw@github.com/me/y", "https://github.com/me/y"),
    ("ssh://git@host.test:2222/grp/sub/r.git", "ssh://host.test:2222/grp/sub/r.git"),
    ("bad user@github.com:me/x", None),
    ("weird$user@github.com:me/x", "github.com:me/x"),
    ("https://x-access-token:abc@github.com/o/r", None),
    ("https://github.com/o/r?token=glpat-abcdefghijklmnopqrst", None),
    ("me:pw@github.com:o/r", None),
    ("file:///home/me/repo", None),
    ("/srv/git/repo.git", None),
    ("https://github.com/onlyowner", None),
])
def test_sanitise(url, want):
    assert discover.sanitise(url) == want


def test_tricky_values_make_valid_toml(home, tmp_path, capsys):
    root = home / "wörk space"        # (roots in temp dirs outside home are not suggested)
    mkrepo(root / "a", "git@github.com:Corp.Org/a.git", name='Zoë "Z" Ünïcode', email="zoe+git@corp.test")
    mkrepo(root / "b", "git@github.com:corp_two/b.git", name="Bad <Name>", email="not an email")
    mkrepo(root / "c", "https://gitlab.example/grp/c.git", email="me@corp.test")
    write_config(home, '[default]\n[scan]\nroots = ["~/elsewhere"]\n')
    (home / "elsewhere").mkdir()
    d = disc(capsys, "-c", str(home / ".config/gitpro.toml"))
    assert d["sources"]["repos"]["count"] == 0
    # roots come from the config only: point a vscode recent at the tricky root
    db = vscdb(home / ".config/Code/User/globalStorage/state.vscdb",
               [{"folderUri": (root / p).as_uri()} for p in ("a", "b", "c")])
    assert db.exists()
    d = disc(capsys)
    parsed = TOML.loads(d["toml"])
    bs = {b["id"]: b for b in parsed["identity"]}
    assert bs["corp.org"]["name"] == 'Zoë "Z" Ünïcode' and bs["corp.org"]["owners"] == ["Corp.Org"]
    assert bs["corp_two"]["name"] == "" and bs["corp_two"]["email"] == ""    # unusable values not copied
    assert "gitlab.example" not in [b["host"] for b in bs.values()]          # placeholder host skipped
    assert "# add to [scan] roots" in d["toml"] and str(root.name) in d["toml"]
    loads_with_gitpro(home, d["toml"], {'name = ""': 'name = "Two"', 'email = ""': 'email = "two@corp.test"'})


def test_render_toml_escapes():
    d = {
        "hosts": [{"host": "github.com", "owners": [
            {"owner": "o1", "repos": 3, "emails": {"a\\b@x.test": 3}, "names": {'Back\\slash "q" é中': 3},
             "in_config": False, "config_id": None, "aliases": {}},
            {"owner": "o2", "repos": 1, "emails": {}, "names": {"Tab\tName": 1, "Del\x7fName": 1},
             "in_config": False, "config_id": None, "aliases": {}}]}],
        "aliases": [{"alias": "gh-x", "host": "github.com", "user": None, "port": None,
                     "key": "~/.ssh/my key", "file": "/h/.ssh/config", "used_by": ["o1"]}],
        "roots": [{"path": "/tmp/a\x7fb", "repos": 0, "exists": False},
                  {"path": "/tmp/q\"uote", "repos": 1, "exists": True}],
        "accounts": [], "rules": [],
        "config": {"path": None, "loaded": False, "ids": [], "scan_roots": None, "aliases": []},
    }
    out = discover.render_toml(d)
    parsed = TOML.loads(out)
    assert parsed["scan"]["roots"] == ['/tmp/q"uote']           # the control-char root is left out
    b1, b2 = parsed["identity"]
    assert b1["name"] == 'Back\\slash "q" é中' and b1["email"] == "a\\b@x.test"
    assert "ssh_alias" not in b1 and "# ssh_alias" in out      # ssh_key with a space: not usable
    assert b2["name"] == "" and b2["owners"] == ["o2"]


# ---------------------------------------------------------------- QA round 1 fixes

def test_history_each_file_capped_newest_kept_and_marked(home, capsys, monkeypatch):
    monkeypatch.setattr(discover, "HISTORY_CAP", 50)
    zsh = write_zsh(home, ["git clone git@github.com:old/a.git"] + ["ls"] * 60 + ["git clone git@github.com:new/a.git"])
    (home / ".bash_history").write_text("git clone git@github.com:bash/a.git\n")
    d = disc(capsys, "--history")
    seen = owners_of(d)
    assert ("github.com", "new") in seen and ("github.com", "bash") in seen and ("github.com", "old") not in seen
    h = d["sources"]["history"]
    assert h["truncated"] == [str(zsh)] and set(h["files"]) == {str(zsh), str(home / ".bash_history")}
    out, _ = text(capsys, "discover", "--history")
    assert "history: read only the newest 50 lines of ~/.zsh_history" in out


def test_history_unreadable_file_not_listed(home, capsys):
    write_zsh(home, ["git clone git@github.com:zsh/a.git"])
    (home / ".bash_history").write_text("git clone git@github.com:bash/a.git\n")
    (home / ".bash_history").chmod(0)
    d = disc(capsys, "--history")
    assert d["sources"]["history"]["files"] == [str(home / ".zsh_history")]
    assert ("github.com", "zsh") in owners_of(d)


def test_repos_walks_home_when_nothing_else_names_roots(home, capsys):
    mkrepo(home / "code/proj", "git@github.com:me/proj.git")
    mkrepo(home / ".cache/x/hidden", "git@github.com:hidden/r.git")
    mkrepo(home / "Library/x/mac", "git@github.com:mac/r.git")
    mkrepo(home / "a/b/c/deep", "git@github.com:deep/r.git")              # depth 4
    d = disc(capsys)
    assert set(owners_of(d)) == {("github.com", "me")}
    assert d["sources"]["repos"]["roots"] == [str(home)]


def test_roots_in_temp_dirs_or_missing_are_not_suggested(home, tmp_path, capsys):
    mkrepo(tmp_path / "scratch/r", "git@github.com:tmpown/r.git")
    vscdb(home / ".config/Code/User/globalStorage/state.vscdb",
          [{"folderUri": (tmp_path / "scratch/r").as_uri()}])
    write_zsh(home, ["git clone git@github.com:gone/r.git ~/gone/r"])
    d = disc(capsys, "--history")
    assert {("github.com", "tmpown"), ("github.com", "gone")} <= set(owners_of(d))
    assert d["roots"] == [] and 'roots = ["~"]' in d["toml"]


def test_owners_differing_in_case_merge(home, capsys):
    mkrepo(home / "w/a", "git@github.com:Corp-Org/a.git", email="me@corp.test")
    mkrepo(home / "w/b", "git@github.com:Corp-Org/b.git", email="me@corp.test")
    mkrepo(home / "w/c", "https://github.com/corp-org/c.git", email="me@corp.test")
    d = disc(capsys)
    assert list(owners_of(d)) == [("github.com", "Corp-Org")]
    assert owners_of(d)[("github.com", "Corp-Org")]["repos"] == 3


GH_ME = r'''
import json
if argv[:2] == ["auth", "status"]:
    print(json.dumps({"hosts": {"github.com": [{"state": "success", "active": True, "login": "me"}]}}))
    sys.exit(0)
if argv[:2] == ["api", "user/emails"]:
    print(json.dumps([{"email": "me@home.test", "verified": True}]))
    sys.exit(0)
sys.exit(9)
'''


def test_verified_gh_email_fills_a_block_without_one(home, fbin, capsys):
    fbin.add("gh", GH_ME)
    mkrepo(home / "w/mine", "git@github.com:me/mine.git")
    d = disc(capsys, "--online")
    (b,) = blocks(d["toml"])
    assert b["email"] == "me@home.test" and b["gh_account"] == "me"
    assert "verified on the gh login me" in d["toml"]


def test_render_uses_the_home_option(home, tmp_path, capsys):
    other = tmp_path / "other"
    mkrepo(other / "w/r", "git@github.com:own/r.git")
    vscdb(other / ".config/Code/User/globalStorage/state.vscdb", [{"folderUri": (other / "w/r").as_uri()}])
    d = disc(capsys, "--home", str(other))
    assert 'roots = ["~/w"]' in d["toml"]


def test_unreadable_vscdb_is_skipped_silently(home, capsys):
    db = vscdb(home / ".config/Code/User/globalStorage/state.vscdb", [])
    db.chmod(0)
    d = disc(capsys)
    assert d["warnings"] == [] and d["sources"]["vscode"]["files"] == []
    out, err = text(capsys, "discover", "-v")
    assert "⚠" not in err and "state.vscdb is not readable" in out


def test_missing_home_skips_includeif_quietly(home, tmp_path, capsys):
    d = disc(capsys, "--home", str(tmp_path / "nohome"))
    assert d["warnings"] == [] and d["sources"]["includeif"] == {"rules": 0}


def test_ssh_values_with_control_characters_are_escaped(home, capsys):
    (home / ".ssh").mkdir()
    (home / ".ssh/config").write_text("Host gh-x\n  HostName github.com\n  IdentityFile ~/.ssh/k\x1b[31mred\n"
                                      "  User u\x07\n")
    out, _ = text(capsys, "discover")
    assert "\x1b" not in out and "\x07" not in out and "gh-x → github.com  key ~/.ssh/k?[31mred" in out


@pytest.mark.parametrize("pattern", ["me:hunter2pw@github.com:corp/**", "ssh://me:hunter2pw@github.com/corp/**"])
def test_includeif_scp_pattern_password_not_shown(home, capsys, pattern):
    (home / ".gitconfig").write_text(f'[includeIf "hasconfig:remote.*.url:{pattern}"]\n\tpath = ~/.gc-corp\n')
    (home / ".gc-corp").write_text("[user]\n\tname = C\n\temail = c@corp.test\n")
    for argv in (("discover", "--json"), ("discover", "-v")):
        out, err = text(capsys, *argv)
        assert "hunter2pw" not in out + err and "corp" in out
    d = disc(capsys)
    assert ("github.com", "corp") in owners_of(d)


def test_toml_escapes_unicode_line_breaks_and_c1():
    for ch in (" ", " ", "\u0085", "\x9b", "\x7f"):
        s = discover._s(f"a{ch}b")
        assert ch not in s and TOML.loads(f"v = {s}")["v"] == f"a{ch}b"
