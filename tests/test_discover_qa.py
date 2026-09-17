"""QA review of package 04 (discover): adversarial cases the main suite does not cover.

The first group pins behaviour that held at review (secrets, read-only state.vscdb, odd ssh
configs). The second group covers one bug each found in review; they were xfail(strict=True) until
the bugs were fixed and now pass.
"""
from __future__ import annotations

import json
import sqlite3

from conftest import run, write_config
from test_discover import TOML, blocks, disc, mkrepo, owners_of, sandbox, vscdb, write_zsh  # noqa: F401


# ---------------------------------------------------------------- holds today

SECRETS = ("ghp_" + "A" * 36, "github_pat_" + "B" * 30, "glpat-" + "C" * 20, "hunter2pw", "s3cr3tpw",
           "QUERYTOK", "FRAGTOK", "tok_ABCDEF", "xoxb-1234", "AKIAQAEXAMPLEKEY")


def test_qa_history_secret_matrix(home, capsys):
    a, b, c = SECRETS[:3]
    write_zsh(home, [
        "git clone https://user:hunter2pw@github.com/o1/r1",
        f"git clone https://{a}@github.com/o2/r2",
        f"git clone https://x-access-token:{a}@github.com/o3/r3",
        f"git clone https://oauth2:{c}@gitlab.com/o4/r4.git",
        "git clone 'https://gitlab.com/o5/r5.git?private_token=QUERYTOK#FRAGTOK'",
        "git clone ssh://git:s3cr3tpw@github.com/o6/r6.git",
        f"export GH_TOKEN={b}; git clone https://github.com/o7/r7",
        "AWS_SECRET=AKIAQAEXAMPLEKEY git clone git@github.com:o8/r8.git",
        "curl -H \"Authorization: token tok_ABCDEF\" https://api.github.com/user && git clone https://github.com/o9/r9",
        "git clone -c http.extraHeader='Authorization: Bearer xoxb-1234' https://github.com/o10/r10",
        "git clone https://me:pa%40ss%2Fhunter2pw@github.com/o11/r11",
        "git clone https://user:12#hunter2pw@github.com/o12/r12",
        "git clone https://user:hu/nter2pw@github.com/o13/r13",
    ])
    blobs = []
    for argv in (("discover", "--history", "--json"), ("discover", "--history", "-v"),
                 ("init", "--discover", "--history")):
        capsys.readouterr()
        assert run(*argv) == 0
        c_ = capsys.readouterr()
        blobs += [c_.out, c_.err]
    blobs.append((home / ".config/gitpro.toml").read_text())
    everything = "\n".join(blobs)
    for s in SECRETS + ("curl", "export", "git clone", "AWS_SECRET", "extraHeader"):
        assert s not in everything, s
    d = json.loads(blobs[0])
    seen = {o for (_, o) in owners_of(d)}
    assert {"o1", "o5", "o6", "o7", "o8", "o9", "o10", "o11"} <= seen
    assert not {"o2", "o3", "o4"} & seen
    assert d["sources"]["history"]["secrets_skipped"] == 3


def test_qa_vscdb_family_and_locked_wal_db_stay_untouched(home, capsys):
    for i, (base, app) in enumerate([(".config", "Code - Insiders"), (".config", "VSCodium"),
                                      ("Library/Application Support", "Cursor")]):
        mkrepo(home / f"w{i}/r", f"git@github.com:own{i}/r.git")
        vscdb(home / base / app / "User/globalStorage/state.vscdb", [{"folderUri": (home / f"w{i}/r").as_uri()}])
    # VS Code itself with the db in WAL mode and a writer holding an exclusive lock
    db = home / ".config/Code/User/globalStorage/state.vscdb"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(str(db), isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    con.execute("BEGIN EXCLUSIVE")
    con.execute("INSERT INTO ItemTable VALUES ('k', 'v')")
    try:
        before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in home.rglob("state.vscdb*")}
        d = disc(capsys)
        after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in home.rglob("state.vscdb*")}
    finally:
        con.execute("ROLLBACK")
        con.close()
    assert before == after
    assert {("github.com", f"own{i}") for i in range(3)} <= set(owners_of(d))
    assert len(d["sources"]["vscode"]["files"]) == 4


def test_qa_vscdb_that_is_a_directory_is_skipped(home, capsys):
    (home / ".config/Code/User/globalStorage/state.vscdb").mkdir(parents=True)
    d = disc(capsys)
    assert d["sources"]["vscode"]["files"] == [] and d["warnings"] == []


def test_qa_ssh_include_loop_crlf_fifo_and_broken_links(home, capsys):
    import os
    ssh = home / ".ssh"
    (ssh / "config.d").mkdir(parents=True)
    (ssh / "config").write_bytes(b"Include config\r\nInclude config.d/loop\r\n"
                                 b"Host=gh-crlf\r\n\tHostName=github.com\r\n\tIdentityFile=~/.ssh/id_crlf\r\n")
    (ssh / "config.d/loop").write_text("Include ../config\nInclude loop\nHost gh-loop\n HostName gitlab.com\n")
    os.mkfifo(ssh / "config.d/fifo")                       # reading it would hang
    (ssh / "config.d/dangling").symlink_to("/nonexistent/x")
    d = disc(capsys, "--source", "ssh")
    assert [a["alias"] for a in d["aliases"]] == ["gh-crlf", "gh-loop"]
    assert d["aliases"][0]["key"] == "~/.ssh/id_crlf"


# ---------------------------------------------------------------- bugs found in review (fixed)

def test_qa_history_cap_in_one_file_does_not_skip_the_others(home, capsys, monkeypatch):
    from gitpro import discover
    monkeypatch.setattr(discover, "HISTORY_CAP", 50)
    (home / ".bash_history").write_text("echo x\n" * 51)
    write_zsh(home, ["git clone git@github.com:zshown/a.git"])
    d = disc(capsys, "--history")
    assert ("github.com", "zshown") in owners_of(d)


def test_qa_history_cap_keeps_the_newest_lines(home, capsys, monkeypatch):
    from gitpro import discover
    monkeypatch.setattr(discover, "HISTORY_CAP", 50)
    write_zsh(home, ["ls"] * 60 + ["git clone git@github.com:recent/a.git"])
    d = disc(capsys, "--history")
    assert ("github.com", "recent") in owners_of(d)


def test_qa_ssh_over_443_is_not_an_alias(home, tmp_path, capsys):
    (home / ".ssh").mkdir()
    (home / ".ssh/config").write_text("Host github.com\n  Hostname ssh.github.com\n  Port 443\n  User git\n")
    root = tmp_path / "src"
    mkrepo(root / "a", "git@github.com:corp-org/a.git", email="me@corp.test", name="Me")
    mkrepo(root / "b", "https://github.com/corp-org/b.git", email="me@corp.test", name="Me")
    write_config(home, f'[default]\n[scan]\nroots = ["{root}"]\n')
    d = disc(capsys)
    assert set(owners_of(d)) == {("github.com", "corp-org")}
    (b,) = blocks(d["toml"])
    assert b["host"] == "github.com" and b.get("ssh_alias") != "github.com"


def test_qa_uncommented_section_survives_unicode_line_separators(home, tmp_path, capsys):
    mkrepo(home / "w/a", "git@github.com:own/a.git", name="Line Sep", email="me@corp.test")
    vscdb(home / ".config/Code/User/globalStorage/state.vscdb", [{"folderUri": (home / "w/a").as_uri()}])
    capsys.readouterr()
    assert run("init", "--discover") == 0
    body = (home / ".config/gitpro.toml").read_text()
    tail = body.split("uncomment what is right ----\n", 1)[1]
    TOML.loads("\n".join(ln[2:] if ln.startswith("# ") else ln for ln in tail.split("\n")))


def test_qa_glob_owner_from_url_is_not_proposed(home, capsys):
    write_zsh(home, ["git clone git@github.com:*/x.git ~/src/x"])
    d = disc(capsys, "--history")
    for b in blocks(d["toml"]):
        assert "*" not in "".join(b["owners"]) and b["id"] != "-"


def test_qa_includeif_pattern_userinfo_not_shown(home, capsys):
    (home / ".gitconfig").write_text('[includeIf "hasconfig:remote.*.url:https://me:hunter2pw@github.com/corp/**"]\n'
                                     "\tpath = ~/.gc-corp\n")
    (home / ".gc-corp").write_text("[user]\n\tname = C\n\temail = c@corp.test\n")
    capsys.readouterr()
    assert run("discover", "--json") == 0
    assert "hunter2pw" not in capsys.readouterr().out
