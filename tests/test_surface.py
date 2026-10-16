"""The 0.4 command surface (spec 22): setup learning identities from the repos, setup -n, the new
forms of why, scan, pin and audit, and how deprecated commands behave."""
import json
import subprocess
import sys
from collections import deque

import pytest
from rich.prompt import Confirm, Prompt

from conftest import commit, git, repo_with_remote, run, write_config, DEFAULT_CONFIG
from gitpro import cli, completion, config, deprecation, learn, pin, scan, setup

WORK = "git@github.com:corp-org/app.git"
JANE = ("Jane Doe", "jane@corp.test")


def out_json(capsys):
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- the commit identity (§5.3)

def test_commit_identity_is_the_most_frequent_committer(tmp_path):
    repo = repo_with_remote(tmp_path, "app", WORK, commits=3, author=JANE)
    commit(repo, "other", author=("Jo", "jo@corp.test"))
    git(repo, "checkout", "-q", "-b", "topic")               # a checkout line is not a commit
    assert scan.commit_identity(repo) == JANE


def test_commit_identity_reads_only_the_tail(tmp_path, monkeypatch):
    repo = repo_with_remote(tmp_path, "app", WORK)
    line = "{0} {0} {1} <{2}> 1700000000 +0000\tcommit: x\n"
    zero = "0" * 40
    old = line.format(zero, "Old Me", "old@corp.test") * 50
    new = line.format(zero, "New Me", "new@corp.test") * 3
    log = repo / ".git" / "logs" / "HEAD"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(old + new)
    assert scan.commit_identity(repo) == ("Old Me", "old@corp.test")
    monkeypatch.setattr(scan, "REFLOG_TAIL", len(new) + 10)  # the tail holds the new lines and a cut one
    assert scan.commit_identity(repo) == ("New Me", "new@corp.test")


def test_commit_identity_none_without_commits(tmp_path):
    assert scan.commit_identity(repo_with_remote(tmp_path, "app", WORK)) is None
    assert scan.commit_identity(tmp_path) is None


# ---------------------------------------------------------------- proposals (§5.4)

def owner(name, repos, emails, people=()):
    return {"owner": name, "repos": repos, "emails": emails, "people": [list(p) for p in people], "names": {}}


def test_propose_groups_owners_by_their_email():
    d = {"hosts": [{"host": "github.com", "owners": [
        owner("corp-org", 3, {"jane@corp.test": 5, "jane@old.test": 1},
              [("Jane Doe", "jane@corp.test", 5), ("J", "jane@old.test", 1)]),
        owner("corp-labs", 1, {"jane@corp.test": 1}, [("J D", "jane@corp.test", 1)]),
        owner("me", 2, {"me@home.test": 2}, [("Me", "me@home.test", 2)]),
        owner("someone", 1, {}),
    ]}], "aliases": [{"alias": "gh-work", "host": "github.com", "key": "~/.ssh/id_work", "used_by": ["corp-org"]}]}
    got = learn.propose(d, None)
    first, second = got.identities
    assert first["email"] == "jane@corp.test" and first["owners"] == ["corp-org", "corp-labs"]
    assert first["name"] == "Jane Doe" and first["repos"] == 4 and first["also"] == [("jane@old.test", 1)]
    assert first["ssh_alias"] == "gh-work" and first["ssh_key"] == "~/.ssh/id_work"
    assert second["email"] == "me@home.test" and "ssh_alias" not in second
    assert first["id"] != second["id"]
    assert got.unassigned == ["github.com/someone (1 repo, no commits)"]


def test_propose_skips_owners_in_the_config(home):
    path = write_config(home, DEFAULT_CONFIG)
    d = {"hosts": [{"host": "github.com", "owners": [dict(owner("corp-org", 1, {"x@corp.test": 1}), in_config=True)]}]}
    assert learn.propose(d, config.load_all(path)).identities == []


def test_learn_finds_the_identity_of_the_commits(home):
    repo_with_remote(home / "code", "app", WORK, commits=2, author=JANE)
    got = learn.learn([home / "code"], home, None, budget=20)
    assert [(i["name"], i["email"], i["host"], i["owners"]) for i in got.identities] == \
        [("Jane Doe", "jane@corp.test", "github.com", ["corp-org"])]
    assert got.committed == 1


# ---------------------------------------------------------------- setup PATH (§5.4)

def interact(monkeypatch, answers, confirms):
    values, choices = deque(answers), deque(confirms)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(Prompt, "ask", lambda *a, **k: values.popleft())
    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: choices.popleft())
    return values, choices


@pytest.mark.parametrize("answers, expect", [
    (["y", ""], ("corp-org", "Jane Doe")),                   # keep them; no SSH key
    (["e", "e", "corp", "Jane D", "jane@corp.test", "github.com", "corp-org", ""], ("corp", "Jane D")),
])
def test_setup_path_proposes_the_identities_found(home, monkeypatch, answers, expect):
    repo_with_remote(home / "code", "app", WORK, commits=2, author=JANE)
    remaining = interact(monkeypatch, answers, [False, False, True])   # fallback, add another, save
    assert run("setup", "--agent", "none", str(home / "code")) == 0
    ident = config.load_all(home / config.USER_CONFIG).identities[0]
    assert (ident.id.startswith(expect[0]), ident.name, ident.email, ident.owners) == \
        (True, expect[1], "jane@corp.test", ("corp-org",))
    assert remaining == (deque(), deque())


def test_setup_path_n_adds_nothing_found(home, monkeypatch):
    repo_with_remote(home / "code", "app", WORK, commits=1, author=JANE)
    interact(monkeypatch, ["n"], [False, False, False])
    run("setup", "--agent", "none", str(home / "code"))
    target = home / config.USER_CONFIG
    assert not target.exists() or not config.load_all(target).identities


def test_setup_path_must_be_a_folder(home, monkeypatch, capsys):
    interact(monkeypatch, [], [])
    assert run("setup", str(home / "nope")) == 2
    assert "not a folder" in capsys.readouterr().err


# ---------------------------------------------------------------- setup -n (§5.6, §5.7)

def test_setup_n_without_config(home, capsys):
    assert run("setup", "-n", "--json") == 1
    data = out_json(capsys)
    assert data["ok"] is False and data["identities"] == [] and data["pending"][0]["group"] == "config"


def test_setup_n_reports_what_is_pending(home, cfg, capsys):
    cfg()
    capsys.readouterr()
    rc = run("setup", "-n", "--json", "--agent", "none")
    data = out_json(capsys)
    assert rc == (1 if data["pending"] else 0) and data["ok"] == (not data["pending"])
    assert [i["id"] for i in data["identities"]] == ["work-github", "personal"]
    assert run("setup", "-y", "--agent", "none") == 0
    capsys.readouterr()
    assert run("setup", "-n", "--json", "--agent", "none") == 0
    assert out_json(capsys)["pending"] == []


def test_setup_flag_errors(home, capsys):
    assert run("setup", "-n", "-y") == 2
    assert run("setup", "--json") == 2
    assert "-n" in capsys.readouterr().err


# ---------------------------------------------------------------- why (§6)

def test_why_json_has_settings_and_the_commit_judgement(cfg, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", WORK)
    capsys.readouterr()
    assert run("why", "--json", str(repo)) == 0
    data = out_json(capsys)
    assert run("which", "--json", str(repo)) == 0
    assert data["settings"] == out_json(capsys)["settings"]
    assert data["commit"]["problems"] == [] and data["commit"]["would_block"] is False
    assert run("why", "--json", "--committer", "me@personal.test", str(repo)) == 0
    commit_ = out_json(capsys)["commit"]
    assert commit_["problems"] and commit_["reason"]


def test_why_json_without_config_still_shows_the_settings(home, tmp_path, capsys):
    repo = repo_with_remote(tmp_path, "app", WORK)
    assert run("why", "--json", str(repo)) == 2
    data = out_json(capsys)
    assert data["ok"] is False and "no config found" in data["error"] and set(data["settings"]) >= {"user.email", "user.name"}


# ---------------------------------------------------------------- scan --lost (§7)

def test_scan_lost_matches_lost(cfg, tmp_path, capsys):
    cfg()
    solo = repo_with_remote(tmp_path / "w", "solo", None, commits=1, author=JANE)
    capsys.readouterr()
    assert run("scan", "--lost", "--json", str(tmp_path / "w")) == 1      # as lost: 1 when it finds some
    got = out_json(capsys)["lost"]
    assert run("lost", "--json", "--offline", str(tmp_path / "w")) == 1
    want = out_json(capsys)
    assert [e["repo"] for e in got["no_remote"]] == [str(solo.resolve())] == [e["repo"] for e in want["no_remote"]]
    assert "ok" not in got and "command" not in got
    assert run("scan", "--lost", str(tmp_path / "w")) == 1
    assert "solo" in capsys.readouterr().out


def test_scan_lost_does_not_go_with_fix(home, capsys):
    assert run("scan", "--lost", "--fix", "-y", str(home)) == 2
    assert "--lost" in capsys.readouterr().err


# ---------------------------------------------------------------- pin --remove (§8), audit --fix (§9)

def test_pin_remove_is_unpin(cfg, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", WORK)
    assert run("pin", "-y", str(repo)) == 0
    capsys.readouterr()
    assert run("pin", "--remove", str(repo)) == 0               # preview, as unpin
    assert "Would remove the pin" in capsys.readouterr().out and (repo / ".git" / pin.FILE).exists()
    assert run("pin", "--remove", "-y", str(repo)) == 0
    assert not (repo / ".git" / pin.FILE).exists()
    assert run("pin", "--remove", "--remote", "origin", str(repo)) == 2


def test_audit_fix_is_rewrite():
    p, _ = cli.build_parser()
    assert p.parse_args(["audit", "--fix"]).rewrite is True
    old = p.parse_args(["audit", "--rewrite"])
    assert old.rewrite is False and old.rewrite_flag is True


# ---------------------------------------------------------------- help and deprecation (§4, §10)

def test_help_lists_the_commands_in_order():
    p, subs = cli.build_parser()
    assert cli.shown_commands(cli._children(p)) == list(cli.MAIN) + list(cli.MORE)
    text = p.format_help()
    for name in ("plan", "apply", "which", "lost", "unpin", "doctor", "agent-check", "preflight"):
        assert f"\n    {name} " not in text and f"\n    {name}\n" not in text
    assert "--suggest" not in subs["scan"].format_help() and "--generate" not in subs["keys"].format_help()
    tree = completion.tree(p)
    assert "plan" not in tree.subs and "which" not in tree.subs and "check" not in tree.subs["guard"].subs
    assert "setup" in tree.subs and "install" in tree.subs["guard"].subs


def test_nested_deprecations_keep_the_kept_ones():
    p, _ = cli.build_parser()
    sub = cli._children(p)
    wrapped = lambda path: cli._find(sub, path).get_default("func").__qualname__.endswith("_deprecate.<locals>.run")
    assert wrapped("agent install") and wrapped("agent status") and wrapped("mcp install") and wrapped("plan")
    assert not wrapped("agent uninstall") and not wrapped("mcp uninstall") and not wrapped("why")


def test_a_deprecated_command_says_what_replaces_it(cfg, tmp_path, monkeypatch, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", WORK)
    monkeypatch.delenv("GITPRO_NO_DEPRECATION")
    capsys.readouterr()
    assert run("which", str(repo)) == 0
    assert capsys.readouterr().err == ("⚠ gitpro which is deprecated and will be removed in 0.5.0; "
                                       "use: gitpro why --json\n")
    assert run("which", "--json", str(repo)) == 0                 # JSON: a field, nothing on stderr
    cap = capsys.readouterr()
    assert json.loads(cap.out)["deprecated"] == {"removed_in": "0.5.0", "use": "gitpro why --json"}
    assert cap.err == ""
    assert run("scan", "--suggest", str(tmp_path)) == 0
    assert "gitpro scan --suggest is deprecated" in capsys.readouterr().err
    assert run("why", "--json", str(repo)) == 0                   # the field does not leak to the next run
    assert "deprecated" not in out_json(capsys)
    monkeypatch.setenv("GITPRO_NO_DEPRECATION", "1")
    assert run("which", "--json", str(repo)) == 0
    cap = capsys.readouterr()
    assert "deprecated" not in json.loads(cap.out) and cap.err == ""


def test_a_deprecated_command_without_replacement():
    assert deprecation.notice("gitpro run", None) == \
        "gitpro run is deprecated and will be removed in 0.5.0; nothing replaces it"
    assert deprecation.field(None) == {"removed_in": "0.5.0", "use": None}


def test_deprecations_capability(capsys):
    assert run("--version", "--json") == 0
    caps = out_json(capsys)["capabilities"]
    assert {"deprecations.v1", "why.commit", "scan.lost", "pin.remove", "audit.fix", "setup.dry-run",
            "setup.learn"} <= set(caps)


def test_setup_without_terminal_is_refused(cfg):
    home = cfg(apply=False).parents[1]                           # the keys exist: nothing else to ask
    r = subprocess.run([sys.executable, "-m", "gitpro", "setup", "--agent", "none"], cwd=home,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "gitpro setup -n" in r.stderr


def test_only_the_drift_check_is_deprecated(cfg, tmp_path, monkeypatch, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", WORK)
    monkeypatch.delenv("GITPRO_NO_DEPRECATION")
    capsys.readouterr()
    run("check")
    assert "gitpro check without --policy, --repo or --range is deprecated" in capsys.readouterr().err
    run("check", "--repo", str(repo))                             # the team policy check stays
    assert "deprecated" not in capsys.readouterr().err
