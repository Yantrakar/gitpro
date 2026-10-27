"""Spec 23: the commit verdicts of `why`, the no-remote message, MCP error parity, per-agent setup coverage and
the installed gitpro skill (all with a throwaway HOME)."""
from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG, repo_with_remote, run, write_config
from gitpro import resolve, setup, skillcopy
from test_mcp import call, payload, text_of

WORK = "git@github.com:corp-org/app.git"
SKILL = Path(__file__).resolve().parent.parent / "skills" / "gitpro" / "SKILL.md"


@pytest.fixture(autouse=True)
def plain_path(home, monkeypatch, tmp_path):
    """PATH with git, sh and ssh-keygen only: no real claude, so no agent is found by itself."""
    bin_ = tmp_path / "bin-plain"
    bin_.mkdir()
    for tool in ("git", "sh", "ssh-keygen", "cp", "rm", "chmod"):
        found = shutil.which(tool)
        if found:
            (bin_ / tool).symlink_to(found)
    monkeypatch.setenv("PATH", str(bin_))
    monkeypatch.setenv("GITPRO_NO_DEPRECATION", "1")


def out_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- 3.1 scan --fix --json previews

def test_scan_fix_json_without_yes_previews(cfg, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", WORK)
    argv = ["git", "-C", str(repo), "config", "user.email", "x@elsewhere.test"]
    subprocess.run(argv, check=True)
    capsys.readouterr()
    assert run("scan", "--fix", "--json", str(tmp_path)) in (0, 1)
    data = out_json(capsys)
    assert data["dry_run"] is True and "removed" not in data
    assert "x@elsewhere.test" in (repo / ".git" / "config").read_text()
    assert run("scan", "--fix", "--json", "-y", str(tmp_path)) == 0
    assert "x@elsewhere.test" not in (repo / ".git" / "config").read_text()


# ---------------------------------------------------------------- 3.2 guard_would_block

def test_guard_would_block_follows_the_guard_mode_not_the_agent_hook(cfg, tmp_path, capsys):
    cfg(DEFAULT_CONFIG + '\n[agents]\nhook = "warn"\n')
    repo = repo_with_remote(tmp_path, "app", WORK)
    capsys.readouterr()
    assert run("why", "--json", "--committer", "me@default.test", str(repo)) == 0
    c = out_json(capsys)["commit"]
    assert c["problems"] and c["would_block"] is False and c["mode"] == "warn"
    assert c["guard_would_block"] is True        # [guard] on_mismatch is "block" by default


def test_guard_would_block_is_false_in_warn_mode_and_for_a_good_commit(cfg, tmp_path, capsys):
    cfg(DEFAULT_CONFIG + '\n[guard]\non_mismatch = "warn"\n')
    repo = repo_with_remote(tmp_path, "app", WORK)
    capsys.readouterr()
    assert run("why", "--json", "--committer", "me@default.test", str(repo)) == 0
    c = out_json(capsys)["commit"]
    assert c["would_block"] is True and c["guard_would_block"] is False
    assert run("why", "--json", "--committer", "me@corp.test", str(repo)) == 0
    c = out_json(capsys)["commit"]
    assert c["problems"] == [] and c["would_block"] is False and c["guard_would_block"] is False


def test_guard_would_block_does_not_record_a_denial(cfg, tmp_path, capsys, monkeypatch):
    cfg()
    repo = repo_with_remote(tmp_path, "app", WORK)
    seen = []
    from gitpro import guard
    monkeypatch.setattr(guard, "_record_denial", lambda *a, **k: seen.append(a), raising=False)
    capsys.readouterr()
    run("why", "--json", "--committer", "me@default.test", str(repo))
    assert seen == []


# ---------------------------------------------------------------- 3.3 a repo with no remote

def test_no_identity_without_a_default_does_not_offer_setup_add(home, tmp_path, capsys):
    write_config(home, '[default]\n\n[[identity]]\nid = "work-github"\nname = "Work Me"\nemail = "me@corp.test"\n'
                       'host = "github.com"\nowners = ["corp-org"]\n')
    repo = repo_with_remote(tmp_path, "norem", None)
    capsys.readouterr()
    assert run("why", str(repo)) == 1
    out = capsys.readouterr().out
    assert "gitpro has no identity for this repo" in out
    assert "git will refuse to commit" in out
    assert "[default]" in out


def test_no_remote_with_a_default_identity_names_the_default(cfg, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "norem", None)
    capsys.readouterr()
    assert run("why", "--json", str(repo)) == 0
    data = out_json(capsys)
    assert data["expected"]["email"] == "me@default.test"


# ---------------------------------------------------------------- 3.4 MCP error parity

def test_mcp_why_error_is_the_object_why_json_prints(home, tmp_path, capsys):
    repo = repo_with_remote(tmp_path, "app", WORK)
    capsys.readouterr()
    assert run("why", "--json", str(repo)) == 2
    cli = out_json(capsys)
    got = call("why", {"path": str(repo)})
    assert got["isError"] is True
    mcp_obj = json.loads(text_of(got))
    assert mcp_obj["error"] == cli["error"] and mcp_obj["config"] is None
    assert mcp_obj["settings"] == cli["settings"]
    assert got["structuredContent"] == mcp_obj


def test_mcp_why_outside_a_repo_is_a_plain_error(home, tmp_path):
    got = call("why", {"path": str(tmp_path)})
    assert got["isError"] is True and "not a git repository" in text_of(got)


# ---------------------------------------------------------------- 3.5 setup coverage per agent

def test_setup_n_json_reports_each_requested_agent(cfg, capsys):
    cfg()
    capsys.readouterr()
    assert run("setup", "-n", "--json", "--agent", "claude", "--agent", "cursor") == 1
    data = out_json(capsys)
    rows = {r["agent"]: r for r in data["agents"]}
    assert rows["claude"] == {"agent": "claude", "hook": "missing", "mcp": "missing"}
    assert rows["cursor"]["hook"] == "missing" and rows["cursor"]["mcp"] == "missing"
    assert data["uncovered"] == []


def test_setup_n_reports_installed_after_setup_y(cfg, home, capsys):
    cfg()
    assert run("setup", "-y", "--no-passphrase", "--agent", "gemini") == 0
    capsys.readouterr()
    assert run("setup", "-n", "--json", "--agent", "gemini") == 0
    data = out_json(capsys)
    assert data["agents"][0]["hook"] == "installed" and data["agents"][0]["mcp"] == "installed"
    assert data["ok"] is True


def test_setup_n_reports_the_plugin(cfg, home, capsys):
    cfg()
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {"gitpro@yantrakar": True}}))
    assert run("setup", "-y", "--no-passphrase", "--agent", "claude") == 0
    capsys.readouterr()
    assert run("setup", "-n", "--json", "--agent", "claude") == 0
    assert out_json(capsys)["agents"] == [{"agent": "claude", "hook": "plugin", "mcp": "plugin"}]


@pytest.mark.parametrize("name", ["codex", "copilot"])
def test_setup_names_an_agent_it_cannot_cover_and_exits_1(cfg, capsys, name):
    cfg()
    capsys.readouterr()
    assert run("setup", "-n", "--agent", name) == 1
    out = capsys.readouterr().out
    assert "✗" in out and "no hook or MCP registration" in out
    assert run("setup", "-n", "--json", "--agent", name) == 1
    data = out_json(capsys)
    assert data["ok"] is False and data["uncovered"] and data["agents"][0]["hook"] == "unavailable"
    assert data["agents"][0]["mcp"] == "unavailable" and data["agents"][0]["reason"]
    assert run("setup", "-y", "--no-passphrase", "--agent", name) == 1


def test_setup_without_agent_option_only_notes_an_uncovered_agent(cfg, capsys):
    cfg()
    assert run("setup", "-y", "--no-passphrase") == 0
    capsys.readouterr()
    assert run("setup", "-n", "--json") == 0
    assert out_json(capsys)["uncovered"] == []


# ---------------------------------------------------------------- 5 the installed skill

def _skill_home(home: Path, text: str = "old skill\n") -> Path:
    d = home / ".agents" / "skills" / "gitpro"
    d.mkdir(parents=True)
    f = d / "SKILL.md"
    f.write_text(text)
    f.chmod(0o640)
    return f


def test_the_bundled_skill_is_the_source_file():
    assert skillcopy.bundled() == SKILL.read_bytes()


def test_setup_n_reports_a_stale_skill_once_through_links(cfg, home, capsys):
    cfg()
    real = _skill_home(home)
    (home / ".claude" / "skills").parent.mkdir(exist_ok=True)
    (home / ".claude" / "skills").symlink_to(home / ".agents" / "skills")
    (home / ".copilot").mkdir()
    (home / ".copilot" / "skills").symlink_to(home / ".agents" / "skills")
    capsys.readouterr()
    assert run("setup", "-n", "--json") == 1
    data = out_json(capsys)
    assert data["skills"] == [{"path": str(real.resolve()), "state": "differs"}]
    assert [p["group"] for p in data["pending"]].count("gitpro skill") == 1
    assert real.read_text() == "old skill\n"          # -n changes nothing


def test_setup_n_with_a_current_skill_is_clean(cfg, home, capsys):
    cfg()
    real = _skill_home(home, SKILL.read_text())
    assert run("setup", "-y", "--no-passphrase") == 0
    capsys.readouterr()
    assert run("setup", "-n", "--json") == 0
    data = out_json(capsys)
    assert data["skills"] == [{"path": str(real.resolve()), "state": "current"}]


def test_setup_y_updates_through_the_link_with_a_backup(cfg, home, capsys):
    cfg()
    real = _skill_home(home)
    link_dir = home / ".claude" / "skills"
    link_dir.parent.mkdir()
    link_dir.symlink_to(home / ".agents" / "skills")
    assert run("setup", "-y", "--no-passphrase") == 0
    assert real.read_bytes() == SKILL.read_bytes()
    assert link_dir.is_symlink() and (link_dir / "gitpro" / "SKILL.md").read_bytes() == SKILL.read_bytes()
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    assert not list(real.parent.glob("SKILL.md.bak.*"))   # nothing new in the skills folder
    backups = list((home / ".local" / "state" / "gitpro" / "skill-backups").glob("SKILL.md.bak.*"))
    assert len(backups) == 1 and backups[0].read_text() == "old skill\n"
    undo = list((home / ".local" / "state" / "gitpro").glob("setup-undo-*.sh"))
    assert any("SKILL.md" in u.read_text() for u in undo)
    capsys.readouterr()
    assert run("setup", "-n") == 0


def test_setup_y_never_creates_a_skill(cfg, home):
    cfg()
    (home / ".claude").mkdir()
    assert run("setup", "-y", "--no-passphrase", "--agent", "none") == 0
    for loc in skillcopy.LOCATIONS:
        assert not (home / loc).exists()


def test_a_symlinked_skill_file_stays_a_link(cfg, home):
    cfg()
    target = home / "elsewhere" / "SKILL.md"
    target.parent.mkdir()
    target.write_text("old\n")
    d = home / ".copilot" / "skills" / "gitpro"
    d.mkdir(parents=True)
    (d / "SKILL.md").symlink_to(target)
    assert run("setup", "-y", "--no-passphrase") == 0
    assert (d / "SKILL.md").is_symlink() and target.read_bytes() == SKILL.read_bytes()


# ---------------------------------------------------------------- packaging

def test_the_build_config_ships_the_skill_in_wheel_and_sdist():
    text = (SKILL.parent.parent.parent / "pyproject.toml").read_text()
    assert '"skills/gitpro/SKILL.md" = "gitpro/skill/SKILL.md"' in text
    assert "/skills/gitpro/SKILL.md" in text.split("[tool.hatch.build.targets.sdist]")[1]
    assert skillcopy.PACKAGED == "skill/SKILL.md"


def test_setup_agent_table_is_complete():
    assert set(setup.NO_COVERAGE) <= set(setup.AGENTS)
    assert "copilot" in setup.AGENTS and "copilot" in setup.TITLE
    assert resolve.why_error  # shared by the CLI and the MCP server
