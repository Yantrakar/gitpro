"""Package 10: the agent hook (`gitpro agent-check`) and its installer (`gitpro agent ...`).

Every settings.json test runs against the temp HOME from the `home` fixture, never the real ~/.claude.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from conftest import TIMEOUT, git, repo_with_remote, run
from gitpro import agents

ROOT = Path(__file__).resolve().parents[1]
WORK_URL = "git@github.com:corp-org/app.git"
OUTPUTS = []   # every (stdout + stderr) agent-check produced in this module, for test 16


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
                "GITPRO_SKIP", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(var, raising=False)


def claude(cmd, cwd, tool="Bash"):
    return {"session_id": "s1", "cwd": str(cwd), "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": {"command": cmd, "description": "d"}}


def check(monkeypatch, capsys, payload, *argv):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    rc = agents.main(["agent-check", *argv])
    out, err = capsys.readouterr()
    OUTPUTS.append(out + err)
    assert '"allow"' not in out + err and "additionalContext" not in out + err
    return rc, out, err


def deny_reason(out):
    data = json.loads(out)
    hso = data["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


@pytest.fixture
def repos(cfg, tmp_path):
    """good: origin corp-org, resolves to work-github. bad: the same with a repo-local personal email."""
    cfg()
    good = repo_with_remote(tmp_path, "good", WORK_URL)
    bad = repo_with_remote(tmp_path, "bad", "git@github.com:corp-org/bad.git")
    git(bad, "config", "user.email", "me@personal.test")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    return types.SimpleNamespace(good=good, bad=bad, elsewhere=elsewhere)


def set_mode(cfg_fixture, mode):
    from conftest import DEFAULT_CONFIG
    cfg_fixture(DEFAULT_CONFIG + f'\n[agents]\nhook = "{mode}"\n', apply=False)


# ---------------------------------------------------------------- 1-3: ignore paths

def test_other_tool_is_ignored_without_any_subprocess(home, monkeypatch, capsys):  # 1
    calls = []
    real = subprocess.run

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)
    monkeypatch.setattr(subprocess, "run", counting)
    rc, out, err = check(monkeypatch, capsys, claude("git commit -m x", home, tool="Read"))
    assert (rc, out, err) == (0, "", "") and calls == []


def test_unrelated_bash_command_is_ignored(home, monkeypatch, capsys):  # 2
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    rc, out, err = check(monkeypatch, capsys, claude("ls -la", home))
    assert (rc, out, err) == (0, "", "") and calls == []


def test_commit_in_correct_repo_says_nothing(repos, monkeypatch, capsys):  # 3
    rc, out, err = check(monkeypatch, capsys, claude("git commit -m x", repos.good))
    assert (rc, out, err) == (0, "", "")


# ---------------------------------------------------------------- 4-5: deny / warn / off

def test_repo_local_override_is_denied(repos, monkeypatch, capsys):  # 4
    rc, out, err = check(monkeypatch, capsys, claude("git commit -m x", repos.bad))
    assert rc == 0
    reason = deny_reason(out)
    assert reason.startswith("gitpro: this commit would be committed as me@personal.test")
    assert "work-github <me@corp.test>" in reason
    assert "config --unset user.email" in reason and "Ask the user" in reason
    assert "GITPRO_SKIP" not in reason and "bypass" not in reason      # DECISIONS Q6: no bypass hint
    # cause, ask the user, fix command: in that order
    assert reason.index("cause:") < reason.index("Ask the user") < reason.index("config --unset user.email")
    assert err.startswith("✗ gitpro:") and "config --unset user.email" in err


def test_warn_and_off_modes(repos, cfg, monkeypatch, capsys):  # 5
    set_mode(cfg, "warn")
    rc, out, err = check(monkeypatch, capsys, claude("git commit -m x", repos.bad))
    assert rc == 0 and err.startswith("⚠ gitpro:") and "fix: git -C" in err
    assert json.loads(out) == {"systemMessage": err.strip()}           # visible, no permission decision
    set_mode(cfg, "off")
    assert check(monkeypatch, capsys, claude("git commit -m x", repos.bad)) == (0, "", "")


# ---------------------------------------------------------------- 6-7: what the command says

def test_repo_dir_from_cd_and_dash_c(repos, monkeypatch, capsys):  # 6
    rc, out, _ = check(monkeypatch, capsys, claude(f"cd {repos.bad} && git push origin main", repos.elsewhere))
    assert str(repos.bad) in deny_reason(out)
    rc, out, _ = check(monkeypatch, capsys, claude(f"git -C {repos.bad} push", repos.elsewhere))
    assert str(repos.bad) in deny_reason(out)
    # the cd moves only what follows it
    assert check(monkeypatch, capsys, claude(f"git -C {repos.good} commit -m x; cd {repos.bad}",
                                             repos.elsewhere)) == (0, "", "")


def test_env_prefixes(repos, monkeypatch, capsys):  # 6
    rc, out, _ = check(monkeypatch, capsys, claude("GIT_AUTHOR_EMAIL=me@personal.test git commit -m x", repos.good))
    assert "me@personal.test" in deny_reason(out)
    assert check(monkeypatch, capsys, claude("GIT_AUTHOR_EMAIL=pat@other.test git commit -m x",
                                             repos.good)) == (0, "", "")
    rc, out, _ = check(monkeypatch, capsys, claude("GIT_COMMITTER_EMAIL=x@y.test git commit -m x", repos.good))
    reason = deny_reason(out)
    assert "committed as x@y.test" in reason and "GIT_COMMITTER_EMAIL" in reason
    rc, out, _ = check(monkeypatch, capsys, claude("git -c user.email=x@y.test commit -m x", repos.good))
    assert "committed as x@y.test" in deny_reason(out)


def test_inherited_environment_is_judged(repos, monkeypatch, capsys):  # 6
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "stale@old.test")
    rc, out, _ = check(monkeypatch, capsys, claude("git commit -m x", repos.good))
    assert "stale@old.test" in deny_reason(out)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "pat@other.test")      # a colleague's: fine
    assert check(monkeypatch, capsys, claude("git commit -m x", repos.good)) == (0, "", "")


def test_strings_shells_pipes_and_fallback(repos, monkeypatch, capsys):  # 6
    assert check(monkeypatch, capsys, claude('echo "git commit -m x"', repos.bad)) == (0, "", "")
    rc, out, _ = check(monkeypatch, capsys, claude(f"sh -c 'cd {repos.bad} && git commit -m x'", repos.elsewhere))
    assert str(repos.bad) in deny_reason(out)
    rc, out, _ = check(monkeypatch, capsys, claude("git log | git commit -m x", repos.bad))
    assert deny_reason(out)
    rc, out, _ = check(monkeypatch, capsys, claude('git commit -m "unbalanced', repos.bad))
    assert "me@personal.test" in deny_reason(out)
    assert check(monkeypatch, capsys, claude('git commit -m "unbalanced', repos.good)) == (0, "", "")


def test_author_flag(repos, monkeypatch, capsys):  # 7
    rc, out, _ = check(monkeypatch, capsys, claude('git commit --author="Me <me@personal.test>" -m x', repos.good))
    reason = deny_reason(out)
    assert "me@personal.test" in reason and "--author" in reason
    rc, out, _ = check(monkeypatch, capsys, claude("git commit --author 'Me <me@personal.test>' -m x", repos.good))
    assert deny_reason(out)
    assert check(monkeypatch, capsys, claude('git commit --author="Pat <pat@other.test>" -m x',
                                             repos.good)) == (0, "", "")


# ---------------------------------------------------------------- 8: forge commands

NO_DEFAULT = """\
[default]

[[identity]]
id = "work-github"
name = "Work Me"
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
"""


def test_forge_command_no_identity(cfg, tmp_path, fake_bin, monkeypatch, capsys):  # 8
    fake_bin.add("gh", "sys.exit(9)")
    fake_bin.add("glab", "sys.exit(9)")
    cfg(NO_DEFAULT)
    stranger = repo_with_remote(tmp_path, "stranger", "git@github.com:someone/x.git")
    good = repo_with_remote(tmp_path, "good", WORK_URL)
    rc, out, _ = check(monkeypatch, capsys, claude("gh pr create --fill", stranger))
    assert "no identity" in deny_reason(out)
    rc, out, _ = check(monkeypatch, capsys, claude("glab mr create", stranger))
    assert deny_reason(out)
    assert check(monkeypatch, capsys, claude("gh pr create --fill", good)) == (0, "", "")
    assert check(monkeypatch, capsys, claude("gh pr list", stranger)) == (0, "", "")
    assert check(monkeypatch, capsys, claude("gh api repos/x/y", stranger)) == (0, "", "")
    rc, out, _ = check(monkeypatch, capsys, claude("gh api -X POST repos/x/y/issues", stranger))
    assert deny_reason(out)
    assert fake_bin.calls("gh") == [] and fake_bin.calls("glab") == []


# ---------------------------------------------------------------- 9: other adapters

def test_experimental_adapters(repos, monkeypatch, capsys):  # 9
    generic = {"command": "git commit -m x", "cwd": str(repos.bad)}
    rc, out, err = check(monkeypatch, capsys, generic, "--agent", "generic")
    assert rc == 1 and out == "" and err.startswith("✗ gitpro:")
    rc, out, err = check(monkeypatch, capsys, generic, "--agent", "cursor")
    data = json.loads(out)
    assert rc == 0 and data["permission"] == "deny" and "Ask the user" in data["agentMessage"] and data["userMessage"]
    gem = {"tool_name": "run_shell_command", "tool_input": {"command": "git commit -m x"}, "cwd": str(repos.bad)}
    rc, out, err = check(monkeypatch, capsys, gem, "--agent", "gemini")
    data = json.loads(out)
    assert rc == 0 and data["decision"] == "deny" and "Ask the user" in data["reason"]
    clean = {"command": "git commit -m x", "cwd": str(repos.good)}
    for agent in ("generic", "cursor"):
        assert check(monkeypatch, capsys, clean, "--agent", agent) == (0, "", "")
    gem["cwd"] = str(repos.good)
    assert check(monkeypatch, capsys, gem, "--agent", "gemini") == (0, "", "")
    assert all(a.verified is False for n, a in agents.ADAPTERS.items() if n != "claude")
    assert agents.ADAPTERS["claude"].verified is True


def test_cwd_flag_overrides_payload(repos, monkeypatch, capsys):
    rc, out, _ = check(monkeypatch, capsys, claude("git commit -m x", repos.elsewhere), "--cwd", str(repos.bad))
    assert deny_reason(out)


# ---------------------------------------------------------------- 10: failure modes

@pytest.mark.parametrize("payload", ["", "not json", "[1, 2]", '{"tool_name": "Bash", "tool_input": 3}'])
def test_malformed_input_fails_open(home, monkeypatch, capsys, payload):  # 10
    rc, out, err = check(monkeypatch, capsys, payload)
    assert rc == 0 and out == "" and "cannot parse" in err and err.startswith("⚠ gitpro agent-check")


def test_skip_env(repos, monkeypatch, capsys):  # 10
    monkeypatch.setenv("GITPRO_SKIP", "1")
    rc, out, err = check(monkeypatch, capsys, claude("git commit -m x", repos.bad))
    assert rc == 0 and out == "" and err == "gitpro agent-check: skipped (GITPRO_SKIP)\n"


def test_exceptions_fail_open(repos, monkeypatch, capsys):  # 7 (failure modes)
    import gitpro.resolve as resolve

    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(resolve, "compare", boom)
    rc, out, err = check(monkeypatch, capsys, claude("git commit -m x", repos.bad))
    assert rc == 0 and out == "" and err.startswith("⚠ gitpro agent-check: ") and "kaboom" in err
    rc, out, err = check(monkeypatch, capsys, claude("git commit -m x", repos.bad), "--bogus-flag")
    assert rc == 0 and out == "" and err.startswith("⚠ gitpro agent-check")


def test_no_config_and_not_a_repo_say_nothing(home, tmp_path, monkeypatch, capsys):
    repo = repo_with_remote(tmp_path, "r", WORK_URL)
    assert check(monkeypatch, capsys, claude("git commit -m x", repo)) == (0, "", "")
    assert check(monkeypatch, capsys, claude("git commit -m x", tmp_path)) == (0, "", "")


def test_hot_entry_point_routes_agent_check(repos, monkeypatch, capsys):
    from gitpro import hot
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(claude("git commit -m x", repos.bad))))
    assert hot.main(["agent-check"]) == 0
    assert deny_reason(capsys.readouterr().out)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(claude("git commit -m x", repos.bad))))
    assert run("agent-check") == 0          # the cli route gives the same answer
    assert deny_reason(capsys.readouterr().out)


# ---------------------------------------------------------------- 11-14: install / uninstall / status

def settings(home):
    return home / ".claude/settings.json"


def gitpro_hooks(obj):
    return [h for g in obj.get("hooks", {}).get("PreToolUse", []) for h in g.get("hooks", [])
            if "gitpro agent-check" in h.get("command", "")]


def test_install_into_empty_home(home, capsys):  # 11
    assert run("agent", "install", "-y") == 0
    out = capsys.readouterr().out
    assert "→ plugin users already have this hook" in out and "✓ installed" in out and "↩ undo: sh " in out
    obj = json.loads(settings(home).read_text())
    [entry] = obj["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash"
    [hook] = entry["hooks"]
    assert hook["type"] == "command" and hook["timeout"] == 10
    assert "command -v gitpro" in hook["command"] and "gitpro agent-check --agent claude" in hook["command"]
    assert run("agent", "install", "-y") == 0
    assert "✓ already installed, nothing to do" in capsys.readouterr().out
    assert len(gitpro_hooks(json.loads(settings(home).read_text()))) == 1


def test_install_merges_and_undo_restores(home, capsys):  # 11
    original = json.dumps({"model": "x", "hooks": {"PreToolUse": [
        {"matcher": "Edit", "hooks": [{"type": "command", "command": "lint.sh"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "bell"}]}]},
        "env": {"A": "1"}}, indent=4) + "\n"
    settings(home).parent.mkdir()
    settings(home).write_text(original)
    assert run("agent", "install", "-y") == 0
    out = capsys.readouterr().out
    obj = json.loads(settings(home).read_text())
    assert obj["model"] == "x" and obj["env"] == {"A": "1"} and obj["hooks"]["Stop"]
    assert obj["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "lint.sh"
    assert len(gitpro_hooks(obj)) == 1
    backups = list((home / ".local/state/gitpro").glob("backup-*/settings.json"))
    assert len(backups) == 1 and backups[0].read_text() == original
    script = re.search(r"↩ undo: sh (\S+)", out)[1].replace("~", str(home), 1)
    subprocess.run(["sh", script], check=True, timeout=TIMEOUT)
    assert settings(home).read_bytes() == original.encode()


def test_install_refuses_bad_files(home, capsys):  # 12
    settings(home).parent.mkdir()
    for text, msg in (('{"a": 1,,}', "is not valid JSON; fix it by hand (nothing written)"),
                      ('{\n  // mine\n  "a": 1\n}\n', "has comments"),
                      ("[]\n", "not a JSON object"),
                      ('{"hooks": {"PreToolUse": {}}}', "hooks.PreToolUse")):
        settings(home).write_text(text)
        assert run("agent", "install", "-y") == 2
        assert msg in capsys.readouterr().err
        assert settings(home).read_text() == text
    assert not (home / ".local/state/gitpro").exists() or not list((home / ".local/state/gitpro").iterdir())


def test_install_needs_yes_without_terminal(home, capsys):
    assert run("agent", "install") == 0
    assert "Dry run" in capsys.readouterr().out
    assert not settings(home).exists()
    assert run("agent", "install", "--json") == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not settings(home).exists()


def test_project_scope(home, tmp_path, monkeypatch, capsys):  # 12, 15c
    repo = repo_with_remote(tmp_path, "proj", WORK_URL)
    monkeypatch.chdir(repo)
    exclude = repo / ".git/info/exclude"
    before = exclude.read_text() if exclude.exists() else None
    assert run("agent", "install", "--scope", "project", "-y") == 0
    out = capsys.readouterr().out
    local = repo / ".claude/settings.local.json"
    assert len(gitpro_hooks(json.loads(local.read_text()))) == 1
    assert not (repo / ".claude/settings.json").exists() and not settings(home).exists()
    assert ".claude/settings.local.json" in exclude.read_text().splitlines()
    assert "→ added .claude/settings.local.json to .git/info/exclude so it is not committed" in out
    script = re.search(r"↩ undo: sh (\S+)", out)[1].replace("~", str(home), 1)
    subprocess.run(["sh", script], check=True, timeout=TIMEOUT)
    assert not local.exists()
    assert (exclude.read_text() if exclude.exists() else None) == before


def test_project_scope_already_ignored(home, tmp_path, monkeypatch, capsys):  # 15c
    repo = repo_with_remote(tmp_path, "proj", WORK_URL)
    (repo / ".gitignore").write_text(".claude/settings.local.json\n")
    exclude = repo / ".git/info/exclude"
    before = exclude.read_bytes() if exclude.exists() else None
    monkeypatch.chdir(repo)
    assert run("agent", "install", "--scope", "project", "-y") == 0
    assert "→ added" not in capsys.readouterr().out
    assert (exclude.read_bytes() if exclude.exists() else None) == before


def test_project_scope_needs_a_repo(home, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert run("agent", "install", "--scope", "project", "-y") == 2
    assert "not a git repository" in capsys.readouterr().err


def test_uninstall(home, capsys):  # 13
    settings(home).parent.mkdir()
    settings(home).write_text(json.dumps({"model": "x"}) + "\n")
    assert run("agent", "install", "-y") == 0
    assert run("agent", "uninstall", "-y") == 0
    assert "✓ removed" in capsys.readouterr().out
    assert json.loads(settings(home).read_text()) == {"model": "x"}
    assert run("agent", "uninstall", "-y") == 0
    assert "not installed, nothing to do" in capsys.readouterr().out
    # other PreToolUse hooks stay, only gitpro's goes
    keep = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "mine.sh"}]}]}}
    settings(home).write_text(json.dumps(keep))
    assert run("agent", "install", "-y") == 0
    capsys.readouterr()
    assert run("agent", "uninstall", "-y", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["command"] == "agent uninstall" and data["changed"] is True and data["undo"]
    assert json.loads(settings(home).read_text()) == keep


def test_install_json(home, capsys):
    assert run("agent", "install", "-y", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"ok": True, "command": "agent install", "file": str(settings(home)), "changed": True,
                    "backup": None, "undo": data["undo"]} and Path(data["undo"]).exists()


def test_status_json(home, tmp_path, monkeypatch, capsys):  # 14
    repo = repo_with_remote(tmp_path, "proj", WORK_URL)
    monkeypatch.chdir(repo)
    monkeypatch.setitem(sys.modules, "gitpro.guard", None)          # guard not in this build
    assert run("agent", "status", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] and data["command"] == "agent status" and data["mode"] == "block"
    assert data["agents"]["claude"] == {"user": None, "project": None, "plugin": None, "experimental": False}
    assert data["agents"]["cursor"]["experimental"] is True and data["agents"]["gemini"]["experimental"] is True
    assert data["guard_installed"] is None
    run("agent", "install", "-y")
    run("agent", "install", "--scope", "project", "-y")
    obj = json.loads(settings(home).read_text())
    settings(home).write_text(json.dumps({**obj, "enabledPlugins": {"gitpro@gitpro": True}}))
    capsys.readouterr()
    assert run("agent", "status", "--json") == 0
    claude_ = json.loads(capsys.readouterr().out)["agents"]["claude"]
    assert claude_ == {"user": str(settings(home)), "project": str(repo / ".claude/settings.local.json"),
                       "plugin": "gitpro@gitpro", "experimental": False}


def test_status_text_and_guard_stub(home, cfg, monkeypatch, capsys):  # 14
    stub = types.ModuleType("gitpro.guard")
    stub.status = lambda: {"installed": "global"}
    monkeypatch.setitem(sys.modules, "gitpro.guard", stub)
    from conftest import DEFAULT_CONFIG
    cfg(DEFAULT_CONFIG + '\n[agents]\nhook = "warn"\n', apply=False)
    assert run("agent", "status") == 0
    out = capsys.readouterr().out
    assert "agents:   [agents] hook = warn" in out
    assert re.search(r"claude +hook: not installed", out)
    assert "cursor   not installed (experimental) → gitpro agent install --agent cursor --experimental" in out
    assert "codex    no pre-tool hook; covered by the git guard (installed) and MCP" in out
    stub.status = lambda: {"installed": "none"}
    assert run("agent", "status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["guard_installed"] is False


# ---------------------------------------------------------------- 15: plugin files

def test_plugin_hooks_file():  # 15
    data = json.loads((ROOT / "hooks/hooks.json").read_text())
    [entry] = data["hooks"]["PreToolUse"]
    [hook] = entry["hooks"]
    assert entry["matcher"] == "Bash" and hook["type"] == "command" and hook["timeout"] == 10
    assert "gitpro agent-check --agent claude" in hook["command"] and "command -v gitpro" in hook["command"]
    assert hook["command"] == agents.hook_command(None)
    manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    assert "hooks" not in manifest


# ---------------------------------------------------------------- 15a: tokeniser

def relevant(cmd):
    return [c for c in agents.parse_command(cmd) if c.relevant]


def test_tokeniser():  # 15a
    calls = agents.parse_command("git status\ngit commit -m x")
    assert [(c.prog, c.sub, c.relevant) for c in calls] == [("git", "status", False), ("git", "commit", True)]
    calls = agents.parse_command("echo hi > out && git push")
    assert [c.words[0] for c in calls] == ["echo", "git"] and calls[1].sub == "push" and calls[1].relevant
    assert calls[0].words == ["echo", "hi"]
    [c] = agents.parse_command("git commit -m 'a; b'")
    assert c.words == ["git", "commit", "-m", "a; b"]
    assert [c.sub for c in relevant("echo $(git commit -m x)")] == ["commit"]
    assert [c.sub for c in relevant('echo "$(git commit -m x)"')] == ["commit"]
    assert [c.sub for c in relevant("echo `git push`")] == ["push"]
    assert [c.sub for c in relevant("echo '`git push`'")] == []
    [c] = relevant("(cd sub && git commit -m x)")
    assert c.repo_dir("/w") == "/w/sub"
    [c] = relevant("cd sub && git -C ../other commit")
    assert c.repo_dir("/w") == "/w/other"
    calls = agents.parse_command("ls |& cat")
    assert [c.words for c in calls] == [["ls"], ["cat"]] and not relevant("ls |& cat")


def test_tokeniser_details():
    [c] = relevant("env FOO=1 sudo -u me nice -n 5 git -C /r -c user.email=x@y.test push upstream main")
    assert (c.sub, c.dir, c.config_email, c.remote) == ("push", "/r", "x@y.test", "upstream")
    [c] = relevant("GIT_AUTHOR_EMAIL=a@b.test GIT_COMMITTER_EMAIL=c@d.test /usr/bin/git commit")
    assert (c.author_env, c.committer_env) == ("a@b.test", "c@d.test")
    [c] = relevant("bash -lc 'git commit --author=\"P <p@q.test>\"'")
    assert c.author == "p@q.test"
    calls = relevant("(cd a; git commit); git -C b push")
    assert [c.repo_dir("/w") for c in calls] == ["/w/a", "/w/b"]
    assert relevant("(cd a; true); git push")[0].repo_dir("/w") == "/w"
    assert not relevant("cat <<EOF\ngit commit -m x\nEOF\n")
    assert [c.sub for c in relevant("cat <<'EOF' > f\nhi\nEOF\ngit push")] == ["push"]
    assert relevant("git commit -m x 2>&1 | tee log")[0].sub == "commit"
    assert not relevant("git tag") and not relevant("git tag -l v1") and relevant("git tag v1")
    assert [(c.prog, c.sub) for c in relevant("gh pr create; glab mr merge 3")] == [("gh", "pr create"),
                                                                                   ("glab", "mr merge")]
    assert relevant("gh api --method=PATCH x") and relevant("gh api -f a=b x") and not relevant("gh api x")
    nested = "sh -c \"sh -c 'sh -c \\\"sh -c git\\\\ commit\\\"'\""
    assert isinstance(agents.parse_command(nested), list)       # depth-capped, never raises
    [c] = relevant('git commit -m "oops')
    assert c.fallback and c.sub == "commit" and c.dir is None


# ---------------------------------------------------------------- 15b: gitpro on PATH

def test_status_warns_when_sh_cannot_find_gitpro(home, fake_bin, capsys):  # 15b
    fake_gitpro = fake_bin.path / "login-only" / "gitpro"
    fake_gitpro.parent.mkdir()
    fake_gitpro.write_text("#!/bin/sh\nexit 0\n")
    fake_gitpro.chmod(0o755)
    fake_bin.add("sh", f"""
if argv[:1] == ["-lc"]:
    print({str(fake_gitpro)!r})
    sys.exit(0)
sys.exit(1)
""")
    assert run("agent", "status") == 0
    err = capsys.readouterr().err
    assert f"⚠ gitpro is not on PATH for sh (found by login shell at {fake_gitpro})" in err
    assert "gitpro setup --agent claude rewrites the entry with the absolute path" in err
    assert run("agent", "install", "-y") == 0
    [hook] = gitpro_hooks(json.loads(settings(home).read_text()))
    assert str(fake_gitpro) in hook["command"] and "gitpro agent-check --agent claude" in hook["command"]


def test_reinstall_rewrites_a_stale_entry(home, capsys):
    stale = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "gitpro agent-check --agent claude", "timeout": 10}]}]}}
    settings(home).parent.mkdir()
    settings(home).write_text(json.dumps(stale))
    assert run("agent", "install", "-y") == 0
    [hook] = gitpro_hooks(json.loads(settings(home).read_text()))
    assert "command -v gitpro" in hook["command"]


def test_hook_command_runs_and_fails_open(home, tmp_path):
    """The installed shell line exits 0 without gitpro and passes gitpro's output through."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    env = {"PATH": f"{empty}:/usr/bin:/bin", "HOME": str(home)}
    for cmd in (agents.hook_command(None), agents.hook_command(str(tmp_path / "missing/gitpro"))):
        r = subprocess.run(["sh", "-c", cmd], input="{}", capture_output=True, text=True, env=env, timeout=TIMEOUT)
        assert (r.returncode, r.stdout) == (0, "")
    fake = tmp_path / "abs/gitpro"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho \"$@\"\nexit 2\n")
    fake.chmod(0o755)
    r = subprocess.run(["sh", "-c", agents.hook_command(str(fake))], input="{}", capture_output=True, text=True,
                       env=env, timeout=TIMEOUT)
    assert (r.returncode, r.stdout) == (0, "agent-check --agent claude\n")   # exit 2 never reaches the agent


# ---------------------------------------------------------------- 16: never a positive decision

PAYLOADS = [
    lambda r: claude("git commit -m x", r.good, tool="Read"),
    lambda r: claude("ls -la", r.good),
    lambda r: claude("git commit -m x", r.good),
    lambda r: claude("git commit -m x", r.bad),
    lambda r: claude("GIT_AUTHOR_EMAIL=pat@other.test git commit -m x", r.good),
    lambda r: claude("gh pr create", r.bad),
    lambda r: {"command": "git commit -m x", "cwd": str(r.bad)},
    lambda r: {"tool_name": "run_shell_command", "tool_input": {"command": "git push"}, "cwd": str(r.good)},
    lambda r: "garbage",
]


@pytest.mark.parametrize("mode", ["block", "warn", "off"])
@pytest.mark.parametrize("agent", ["claude", "cursor", "gemini", "generic"])
def test_never_a_positive_decision(repos, cfg, monkeypatch, capsys, mode, agent):  # 16
    set_mode(cfg, mode)
    for make in PAYLOADS:
        rc, out, err = check(monkeypatch, capsys, make(repos), "--agent", agent)
        assert "allow" not in out and "allow" not in err
        if out:
            obj = json.loads(out)
            if mode == "warn":
                assert agent == "claude" and list(obj) == ["systemMessage"]
            else:
                assert "deny" in json.dumps(obj) and mode == "block"
        assert rc in (0, 1) and (rc == 0 or agent == "generic")


def test_outputs_collected_never_allow():  # 16 (everything this module printed)
    for text in OUTPUTS:
        assert '"allow"' not in text and "additionalContext" not in text


def test_source_has_no_positive_decision():  # 16 (static)
    src = (ROOT / "src/gitpro/agents.py").read_text()
    assert not re.search(r"""["']allow["']""", src) and "additionalContext" not in src


# ---------------------------------------------------------------- 17-18

def test_import_is_light():  # 17
    code = "import gitpro.agents, sys; print('\\n'.join(sys.modules))"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=TIMEOUT)
    mods = set(r.stdout.split())
    assert "gitpro.agents" in mods
    assert not {"rich", "gitpro.cli", "gitpro.view", "gitpro.forge", "gitpro.scan"} & mods


def test_ignore_path_imports_no_git_machinery(home):  # 17 (time budget: no resolve/config on the ignore path)
    code = ("import io, json, sys; sys.stdin = io.StringIO(json.dumps({'tool_name': 'Bash', "
            "'tool_input': {'command': 'ls -la'}, 'cwd': '/'})); import gitpro.hot as h; "
            "rc = h.main(['agent-check']); print('\\n'.join(sys.modules)); sys.exit(rc)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=TIMEOUT,
                       env={**os.environ, "HOME": str(home)})
    assert r.returncode == 0, r.stderr
    mods = set(r.stdout.split())
    assert not {"rich", "gitpro.cli", "gitpro.resolve", "gitpro.render", "tomllib", "tomli"} & mods


@pytest.mark.parametrize("agent", ["cursor", "gemini"])
def test_experimental_install_needs_flag(home, capsys, agent):  # 18
    assert run("agent", "install", "--agent", agent, "-y") == 2
    err = capsys.readouterr().err
    assert f"✗ {agent} adapter is experimental; add --experimental" in err
    assert not (home / ".cursor").exists() and not (home / ".gemini").exists()
    assert run("agent", "install", "--agent", agent, "--experimental", "-y") == 0
    captured = capsys.readouterr()
    assert "⚠ experimental: field names unverified" in captured.err
    path = home / (".cursor/hooks.json" if agent == "cursor" else ".gemini/settings.json")
    assert "gitpro agent-check --agent " + agent in path.read_text()
    assert run("agent", "uninstall", "--agent", agent, "-y") == 0
    assert "gitpro agent-check" not in path.read_text()


@pytest.mark.parametrize("agent", ["codex", "opencode"])
def test_agents_without_hooks(home, capsys, agent):
    assert run("agent", "install", "--agent", agent, "-y") == 0
    err = capsys.readouterr().err
    assert f"⚠ {agent} has no pre-tool hook; use the git guard (gitpro guard install)" in err
    assert not (home / ".claude").exists()
    assert run("agent", "install", "--agent", agent, "-y", "--json") == 2       # JSON: an error, not success
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and "no pre-tool hook" in data["error"]


def test_capabilities(capsys):
    assert run("--version", "--json") == 0
    caps = json.loads(capsys.readouterr().out)["capabilities"]
    assert {"agent-check", "agent.install", "agent.status"} <= set(caps)


# ---------------------------------------------------------------- QA (package 10 review): regressions found by QA

@pytest.mark.parametrize("cmd", [
    "if true; then git commit -m x; fi",
    "if false; then :; else git commit -m x; fi",
    "for i in 1; do git commit -m x; done",
    "while true; do git commit -m x; break; done",
    "{ git commit -m x; }",
    "! git commit -m x",
])
def test_qa_control_structures_are_checked(repos, monkeypatch, capsys, cmd):
    """Shell reserved words in command position must not hide the git call (silent miss)."""
    rc, out, err = check(monkeypatch, capsys, claude(cmd, repos.bad))
    assert rc == 0 and "config --unset user.email" in deny_reason(out)


@pytest.mark.parametrize("cmd", [
    'cd "$(git rev-parse --show-toplevel)" && git commit -m x',
    'git -C "$(git rev-parse --show-toplevel)" commit -m x',
    'git -C "$GITPRO_QA_UNSET_DIR" commit -m x',
])
def test_qa_substitution_repo_dir_falls_back_to_cwd(repos, monkeypatch, capsys, cmd):
    """A repo dir the hook cannot know ($(...) or an unset variable) must not turn into 'not a repo'."""
    monkeypatch.delenv("GITPRO_QA_UNSET_DIR", raising=False)
    rc, out, err = check(monkeypatch, capsys, claude(cmd, repos.bad))
    assert rc == 0 and "config --unset user.email" in deny_reason(out)


def test_qa_reinstall_keeps_user_empty_group(home, capsys):
    """Installing twice must not delete a user's own PreToolUse group just because its hooks list is empty."""
    mine = {"matcher": "Write", "hooks": []}
    settings(home).parent.mkdir()
    settings(home).write_text(json.dumps({"hooks": {"PreToolUse": [mine]}}))
    assert run("agent", "install", "-y") == 0
    capsys.readouterr()
    assert run("agent", "install", "-y") == 0
    assert "already installed" in capsys.readouterr().out
    groups = json.loads(settings(home).read_text())["hooks"]["PreToolUse"]
    assert mine in groups and len(gitpro_hooks({"hooks": {"PreToolUse": groups}})) == 1


# ---------------------------------------------------------------- package 10 fixes after the QA review

def subs(cmd):
    return [(c.prog, c.sub) for c in relevant(cmd)]


def test_reserved_words_case_and_time():
    assert subs("case $x in a) git push;; b|c) git commit -m x;; esac") == [("git", "push"), ("git", "commit")]
    assert subs("until false; do git push; done; time git commit") == [("git", "push"), ("git", "commit")]
    assert subs("if a; then :; elif b; then git push; fi") == [("git", "push")]
    assert subs("for f in $(git commit -m x); do :; done") == [("git", "commit")]
    assert subs("function f { git push; }") == [("git", "push")]
    # a case pattern's ")" closes no subshell: the cd inside the subshell does not leak out
    [c] = relevant("(cd a; case x in y) true;; esac); git push")
    assert c.repo_dir("/w") == "/w"


def test_unknowable_dirs_keep_what_is_known():
    [c] = relevant('cd /abs && git -C "$GITPRO_QA_UNSET_DIR" commit')
    assert c.repo_dir("/w") == "/abs"
    [c] = relevant('cd "$(pwd)" && git commit')
    assert c.repo_dir("/w") == "/w"


def test_reinstall_dedupes_gitpro_entries(home, capsys):
    entry = {"type": "command", "command": "gitpro agent-check --agent claude"}
    data = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [entry, dict(entry)]},
                                     {"matcher": "Bash", "hooks": [dict(entry)]}]}}
    settings(home).parent.mkdir()
    settings(home).write_text(json.dumps(data))
    assert run("agent", "install", "-y") == 0
    groups = json.loads(settings(home).read_text())["hooks"]["PreToolUse"]
    assert len(groups) == 1 and len(gitpro_hooks({"hooks": {"PreToolUse": groups}})) == 1


def test_environment_fixes_that_work(repos, monkeypatch, capsys):
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "stale@old.test")
    rc, out, _ = check(monkeypatch, capsys, claude("git commit -m x", repos.good))
    reason = deny_reason(out)
    assert "the fix is `GIT_COMMITTER_EMAIL=me@corp.test git commit -m x`" in reason
    assert "unset" not in reason and "user's shell" in reason
    for cmd in ("unset GIT_COMMITTER_EMAIL; git commit -m x", "env -u GIT_COMMITTER_EMAIL git commit -m x",
                "env -i PATH=/usr/bin git commit -m x", "GIT_COMMITTER_EMAIL=me@corp.test git commit -m x",
                "export GIT_COMMITTER_EMAIL=me@corp.test && git commit -m x"):
        assert check(monkeypatch, capsys, claude(cmd, repos.good)) == (0, "", ""), cmd
    # unset inside a subshell stays there
    rc, out, _ = check(monkeypatch, capsys, claude("(unset GIT_COMMITTER_EMAIL); git commit -m x", repos.good))
    assert deny_reason(out)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "me@personal.test")
    rc, out, _ = check(monkeypatch, capsys, claude("git commit -m x", repos.good))
    reason = deny_reason(out)
    assert "`GIT_AUTHOR_EMAIL=me@corp.test git commit -m x`" in reason and "unset" not in reason


def test_env_override_problem_fix_is_a_prefix(repos, monkeypatch, capsys):
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Somebody Else")
    rc, out, _ = check(monkeypatch, capsys, claude("git commit -m x", repos.good))
    reason = deny_reason(out)
    assert "GIT_COMMITTER_NAME='Work Me' git commit -m x" in reason and "unset" not in reason


def test_big_and_hostile_commands_are_fast():
    import time
    start = time.monotonic()
    assert subs("$(" * 25000 + " git commit -m x") == [("git", "commit")]           # unbalanced, < 64KB
    [c] = relevant("x " * 40000 + "git -C /r commit -m x")                          # > 64KB: regex scan
    assert c.fallback and c.dir == "/r"
    assert subs("`" + "a " * 30000) == []
    assert time.monotonic() - start < 5


def test_heredoc_marker_inside_quotes_is_not_a_heredoc():
    assert subs('echo "a <<EOF"\ngit commit -m x\necho EOF') == [("git", "commit")]
    assert subs("echo 'a <<EOF'\ngit push") == [("git", "push")]
    assert subs("echo x # <<EOF\ngit push") == [("git", "push")]
    assert subs("cat <<A <<'B'\ngit commit\nA\ngit push\nB\ngit tag v1") == [("git", "tag")]
    # the usual agent commit: a heredoc inside "$(...)"; its body's ) ' " do not confuse the tokeniser
    calls = agents.parse_command('git commit -m "$(cat <<\'EOF\'\nFix\n\n1) it\'s "done"\nEOF\n)" && git push')
    assert [(c.prog, c.sub) for c in calls if c.relevant] == [("git", "commit"), ("git", "push")]
    assert not any(c.fallback for c in calls)


@pytest.mark.parametrize("cmd", [
    "eval 'git commit -m x'",
    "builtin command git commit",
    "doas -u me git push",
    "ionice -c 3 git push",
    "strace -f -o /tmp/t git push",
    "flock -w 5 /tmp/lock git push",
    "flock /tmp/lock -c 'git push'",
    "watch -n 5 git push",
    "2>/dev/null git push",
    "bash <<<'git push'",
    "bash -s <<< 'git push'",
    "echo 'git push' | bash",
    "printf 'git status\\ngit push\\n' | sh",
    "git -c alias.ci=commit ci -m x",
    "hub commit -m x",
    "hub pull-request -m x",
    "glab api -X POST projects/1/issues",
    "gh api graphql -f query='mutation { addStar }'",
])
def test_more_wrappers_and_forms(cmd):
    assert relevant(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "git rebase --abort", "git merge --quit", "git cherry-pick --skip", "git am --abort", "git revert --quit",
    "git commit --dry-run", "git commit -h", "git push -n", "git push --dry-run", "git push --help",
    "gh api graphql -f query='query { viewer { login } }'", "gh api graphql -f query=' { viewer { login } }'",
    "glab api projects/1", "watch -x git status", "echo 'git push'", "bash script.sh <<<'git push'",
    "git -c alias.st='!git status' st", "git status 2>/dev/null",
])
def test_no_false_denials(cmd):
    assert not relevant(cmd), cmd


def test_continue_and_redirect_details():
    assert subs("git rebase --continue") == [("git", "rebase")]
    [c] = relevant("git push 2>/dev/null")
    assert c.remote is None and c.words == ["git", "push"]
    [c] = relevant("pushd /r && git commit")
    assert c.repo_dir("/w") == "/r"
    [c] = relevant("GIT_DIR=/r/.git git commit")
    assert c.repo_dir("/w") == "/r/.git"
    [c] = relevant("git -C /a --git-dir=b commit")
    assert c.repo_dir("/w") == "/a/b"
    assert subs('git -C x commit -m "oops') == [("git", "commit")]
    assert subs('glab mr create -t "oops') == [("glab", "mr create")]


def test_dir_from_git_dir_is_judged(repos, monkeypatch, capsys):
    rc, out, _ = check(monkeypatch, capsys, claude(f"GIT_DIR={repos.bad}/.git git commit -m x", repos.elsewhere))
    assert "config --unset user.email" in deny_reason(out)


def test_config_alias_is_followed(repos, monkeypatch, capsys):
    git(repos.bad, "config", "alias.ci", "commit -v")
    git(repos.bad, "config", "alias.sh", "!git commit")
    rc, out, _ = check(monkeypatch, capsys, claude("git ci -m x", repos.bad))
    assert "config --unset user.email" in deny_reason(out)
    assert check(monkeypatch, capsys, claude("git sh", repos.bad)) == (0, "", "")      # ! aliases not followed
    assert check(monkeypatch, capsys, claude("git nosuch", repos.bad)) == (0, "", "")


def test_unknown_agent_and_experimental_order(home, capsys):
    assert run("agent", "install", "--agent", "vim", "-y", "--json") == 2
    assert "unknown agent" in json.loads(capsys.readouterr().out)["error"]
    assert run("agent", "install", "--agent", "vim", "-y") == 2
    assert "✗ unknown agent 'vim'" in capsys.readouterr().err
    assert run("agent", "install", "--agent", "cursor", "-y") == 2
    err = capsys.readouterr().err
    assert err.index("⚠ experimental: field names unverified") < err.index("✗ cursor adapter is experimental")


def test_bypass_is_documented_for_humans(home, capsys):
    assert run("agent", "status") == 0
    assert "GITPRO_SKIP=1" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run("agent-check", "--help")
    assert "GITPRO_SKIP=1" in capsys.readouterr().out


# ---------------------------------------------------------------- package 10 fixes after QA round 2

def dirs(cmd, cwd="/cwd"):
    return [(c.sub, c.repo_dir(cwd)) for c in agents.parse_command(cmd) if c.relevant]


def test_shell_variables_and_for_loops_name_the_repo(repos, monkeypatch, capsys):
    assert dirs('R=/r; git -C "$R" commit') == [("commit", "/r")]
    assert dirs('R=/r; cd ${R}/sub && git push') == [("push", "/r/sub")]
    assert dirs('A=/a B=$A/b; git --git-dir="$B/.git" commit') == [("commit", "/a/b/.git")]
    assert dirs('R=/r; (R=/other); git -C "$R" commit') == [("commit", "/r")]                 # subshell scope
    assert dirs('R=/r; unset R; git -C "$R" commit') == [("commit", "/cwd")]
    assert dirs('R=$(pwd)/x; git -C "$R" commit') == [("commit", "/cwd")]                      # not knowable
    assert dirs("for d in /a /b; do git -C $d commit; done") == [("commit", "/a"), ("commit", "/b")]
    assert dirs("for i in 1 2; do for j in x y; do git -C /r/$i/$j push; done; done") == [
        ("push", "/r/1/x"), ("push", "/r/1/y"), ("push", "/r/2/x"), ("push", "/r/2/y")]
    many = " ".join(f"/r{k}" for k in range(agents.MAX_FOR + 1))
    assert dirs(f"for d in {many}; do git -C $d push; done") == [("push", "/cwd")]           # too many: once
    assert dirs("d=/x; for d in $(ls); do git -C $d push; done") == [("push", "/cwd")]
    assert dirs("for d in /r/*; do git -C $d push; done") == [("push", "/cwd")]
    # the reported cases
    rc, out, _ = check(monkeypatch, capsys, claude(f'for d in {repos.bad}; do (cd "$d" && git push); done',
                                                   repos.elsewhere))
    assert rc == 0 and str(repos.bad) in deny_reason(out)
    assert check(monkeypatch, capsys, claude(f'R={repos.good}; git -C "$R" commit -m x', repos.bad)) == (0, "", "")


def test_variables_reach_child_shells_only_when_exported():
    assert dirs("R=/r; sh -c 'git -C \"$R\" commit'") == [("commit", "/cwd")]
    assert dirs("export R=/r; sh -c 'git -C \"$R\" commit'") == [("commit", "/r")]
    assert dirs("R=/r sh -c 'git -C \"$R\" commit'") == [("commit", "/r")]
    assert dirs("R=/r; eval 'git -C \"$R\" commit'") == [("commit", "/r")]
    assert dirs('R=/r; echo "$(git -C "$R" commit)"') == [("commit", "/r")]


def test_cd_back_and_the_directory_stack():
    assert dirs("cd /a && cd /b && cd - && git push") == [("push", "/a")]
    assert dirs("cd - && git push") == [("push", "/cwd")]                                     # OLDPWD unknown
    assert dirs("pushd /a && pushd /b && popd && git push") == [("push", "/a")]
    assert dirs("pushd /a && pushd /b && pushd && git push") == [("push", "/a")]             # swap
    assert dirs("popd && git push") == [("push", "/cwd")]                                     # empty stack


@pytest.mark.parametrize("cmd", [
    "git commit -am --dry-run", "git commit --message --dry-run", "git commit -F -h", "git commit -- --dry-run",
    "git commit -C HEAD --author -h", "git merge -m --abort topic", "git rebase -x --abort main",
    "git cherry-pick -X --skip abc", "git tag -m -l v1", "git push origin -o --dry-run",
])
def test_exemption_flags_as_option_values_are_judged(cmd):
    assert relevant(cmd), cmd


@pytest.mark.parametrize("cmd", ["git push -fn", "git push origin main -n", "git commit -q --dry-run -m x",
                                 "git merge --no-ff --abort"])
def test_exemption_flags_in_option_position(cmd):
    assert not relevant(cmd), cmd


def test_heredoc_into_a_shell_is_its_script():
    assert dirs("bash -s <<EOF\ncd /r && git push\nEOF") == [("push", "/r")]
    assert dirs("sh <<-'EOF'\n\tgit commit -m x\n\tEOF\ngit status") == [("commit", "/cwd")]
    assert dirs("cat <<'EOF' | sh\ngit push\nEOF") == [("push", "/cwd")]
    assert dirs("cat <<EOF\n$(git commit -m x)\nEOF") == [("commit", "/cwd")]                # unquoted: runs
    assert dirs("cat <<'EOF'\n$(git commit -m x)\nEOF") == []                                # quoted: text
    assert dirs("git commit -F - <<'EOF'\nfix (it's \"x\"\nEOF\ngit push") == [("commit", "/cwd"), ("push", "/cwd")]
    assert dirs("python3 <<'EOF'\ngit push\nEOF") == []


def test_fallback_scan_is_linear():
    import time
    start = time.monotonic()
    assert agents.parse_command("'" + "git -C " * 9000)[:1] == []                        # the QA input
    agents.parse_command("'" + "git --a=git " * 9000)
    agents.parse_command("'" + "git -c git " * 9000)
    assert time.monotonic() - start < 2
    start = time.monotonic()                                                             # loops, values
    agents.parse_command("for d in a; do " * 4000)
    assert dirs("for d in a; do " * 3000 + "git -C $d push;" + " done;" * 3000) == [("push", "/cwd")]
    assert dirs("R=x; " + "R=$R$R; " * 40 + 'git -C "$R" push') == [("push", "/cwd")]
    assert time.monotonic() - start < 2
    [c] = agents.parse_command("' git -C /r -c x=y --no-pager commit")
    assert c.fallback and c.dir == "/r" and c.sub == "commit"


def test_alias_lookup_is_bounded(repos, monkeypatch, capsys):
    seen = {}
    real = subprocess.run

    def slow(cmd, *a, **k):
        if "alias.ci" in cmd:
            seen["timeout"] = k.get("timeout")
            raise subprocess.TimeoutExpired(cmd, k.get("timeout"))
        return real(cmd, *a, **k)
    monkeypatch.setattr(agents.subprocess, "run", slow)
    assert check(monkeypatch, capsys, claude("git ci -m x", repos.bad)) == (0, "", "")      # no opinion
    assert seen["timeout"] <= 3


@pytest.mark.parametrize("cmd, env, unset", [
    ("X=v; export X; git commit", {"X": "v"}, set()),
    ("declare -x X=v; git commit", {"X": "v"}, set()),
    ("typeset -x X=v; git commit", {"X": "v"}, set()),
    ("X=v; declare -x X; git commit", {"X": "v"}, set()),
    ("export X=v; export -n X; git commit", {}, {"X"}),
    ("X=v; git commit", {}, set()),
    ("export X=v | true; git commit", {}, set()),
    ("unset X | true; git commit", {}, set()),
    ("true | unset X; git commit", {}, set()),
    ("export X=v & git commit", {}, set()),
])
def test_export_forms_and_pipeline_builtins(cmd, env, unset):
    [c] = relevant(cmd)
    assert (c.env, c.unset) == (env, unset)
    assert dirs("cd /x | true; git push") == [("push", "/cwd")]


def test_bang_alias_from_dash_c_is_parsed_not_run():
    assert dirs("git -c alias.p='!cd /r && git push' p") == [("push", "/r")]
    assert dirs("git -c alias.s='!git status' s") == []


# ---------------------------------------------------------------- QA round 2 (package 10 review): regressions found by QA

def test_qa2_assigned_variable_dir_is_expanded(repos, monkeypatch, capsys):
    """`R=dir; git -C "$R" ...` names a knowable repo: cutting $R back to the cwd judges the wrong repo."""
    rc, out, err = check(monkeypatch, capsys, claude(f'R={repos.bad}; git -C "$R" commit -m x', repos.good))
    assert rc == 0 and "config --unset user.email" in deny_reason(out)
    assert check(monkeypatch, capsys, claude(f'R={repos.good}; cd "$R" && git commit -m x', repos.bad)) == (0, "", "")


@pytest.mark.parametrize("cmd", ["git commit -m --dry-run", "git commit -m -h"])
def test_qa2_exemption_flags_only_count_as_options(repos, monkeypatch, capsys, cmd):
    """`-m --dry-run` / `-m -h` is a commit with that message, not a dry run or help: it really commits."""
    rc, out, err = check(monkeypatch, capsys, claude(cmd, repos.bad))
    assert rc == 0 and "config --unset user.email" in deny_reason(out)


def test_qa2_heredoc_into_a_shell_is_checked(repos, monkeypatch, capsys):
    rc, out, err = check(monkeypatch, capsys, claude("sh <<'EOF'\ngit commit -m x\nEOF", repos.bad))
    assert rc == 0 and "config --unset user.email" in deny_reason(out)


@pytest.mark.parametrize("back", ["cd -", "popd"])
def test_qa2_cd_dash_and_popd_go_back(repos, monkeypatch, capsys, back):
    """`cd X && cd - && git push` pushes from the starting dir, not from X."""
    head = "pushd" if back == "popd" else "cd"
    rc, out, err = check(monkeypatch, capsys, claude(f"{head} {repos.good} && {back} && git push", repos.bad))
    assert rc == 0 and "config --unset user.email" in deny_reason(out)


TOKEN_URL = "https://user:ghp_xxx@github.com/o/r.git"


def test_remote_url_with_token_is_redacted(cfg, tmp_path, monkeypatch, capsys):
    from conftest import DEFAULT_CONFIG
    cfg(DEFAULT_CONFIG.replace('name = "Default Me"\nemail = "me@default.test"\n', ""))   # [default] without email
    r = repo_with_remote(tmp_path, "tok", TOKEN_URL)
    git(r, "config", "user.email", "me@x.test")
    rc, out, err = check(monkeypatch, capsys, claude("git commit -m x", r))
    reason = deny_reason(out)
    assert "ghp_xxx" not in out + err
    assert "no identity matches this repo (https://user:***@github.com/o/r.git)" in reason


def test_matched_url_is_redacted():
    m = types.SimpleNamespace(remote="origin", url=TOKEN_URL)
    rep = types.SimpleNamespace(expected=types.SimpleNamespace(id="w", email="w@x.test", match=m))
    assert agents._who_is_expected(rep) == "origin (https://user:***@github.com/o/r.git) belongs to identity w <w@x.test>"
    m.remote = None
    assert "ghp_xxx" not in agents._who_is_expected(rep)


def test_two_identity_remotes_are_denied(repos, monkeypatch, capsys):  # spec 21 B1, O5
    git(repos.good, "remote", "add", "upstream", "git@github.com:me/app.git")
    rc, out, err = check(monkeypatch, capsys, claude("git commit -m x", repos.good))
    reason = deny_reason(out)
    assert reason.startswith("gitpro: this commit runs in a repo whose remotes belong to different identities")
    assert "gitpro pin -y --remote origin" in reason
    # a push names its remote and is judged alone: origin is work's, the commits are personal's
    rc, out, err = check(monkeypatch, capsys, claude("git push origin HEAD", repos.good))
    assert "origin (git@github.com:corp-org/app.git) belongs to identity work-github" in deny_reason(out)
    assert run("pin", "-y", "--remote", "origin", str(repos.good)) == 0
    capsys.readouterr()
    for cmd in ("git commit -m x", "git push origin HEAD"):
        assert check(monkeypatch, capsys, claude(cmd, repos.good))[1] == ""
