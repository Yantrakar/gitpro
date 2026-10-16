"""`gitpro doctor` (spec 16 §2.1, tests 1-9): every check in one ordered report."""
from __future__ import annotations

import json
import shutil
import sys
import types

import pytest

from conftest import DEFAULT_CONFIG, git, repo_with_remote, run
from gitpro import doctor

OPTIONAL = ("gitpro.guard", "gitpro.health", "gitpro.agents", "gitpro.mcp", "gitpro.policy")
ENV_VARS = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_SSH_COMMAND",
            "GIT_CONFIG_COUNT", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_PARAMETERS", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITLAB_TOKEN")


@pytest.fixture(autouse=True)
def sandbox(home, fake_bin, monkeypatch, tmp_path):
    """No real repo under the cwd, a fake ssh and ssh-keygen, no identity env vars, optional packages absent."""
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    fake_bin.add("ssh", "sys.stderr.write('OpenSSH_9.6p1 Ubuntu-3, OpenSSL 3.0.13 30 Jan 2024\\n')")
    fake_bin.add("ssh-keygen", """
host, path = argv[argv.index("-F") + 1], argv[argv.index("-f") + 1]
try:
    lines = open(path).read().splitlines()
except OSError:
    sys.exit(1)
hits = [ln for ln in lines if ln.split(" ")[0] == host]
print("\\n".join(hits))
sys.exit(0 if hits else 1)
""")
    for name in OPTIONAL:
        monkeypatch.setitem(sys.modules, name, None)
    return work


def doc(capsys, *argv):
    """(exit code, stdout) of `gitpro doctor ARGV`."""
    rc = run("doctor", *argv)
    return rc, capsys.readouterr().out


def doc_json(capsys, *argv):
    rc = run("doctor", "--json", *argv)
    data = json.loads(capsys.readouterr().out)
    return rc, data, {s["name"]: s for s in data["sections"]}


def row(out: str, name: str) -> str:
    """The text row of a section (with its continuation lines)."""
    lines = out.splitlines()
    for n, line in enumerate(lines):
        if line.startswith(name + " ") or line.startswith(f"· {name}:") or line.startswith(f"⚠ {name}:"):
            more = [ln for ln in lines[n + 1:n + 6] if ln.startswith("          ")]
            return "\n".join([line, *more])
    raise AssertionError(f"no {name} row in:\n{out}")


# ---------------------------------------------------------------- 1-3

def test_no_config(home, capsys):  # 1
    rc, out = doc(capsys)
    assert rc == 1
    r = row(out, "config")
    assert "✗" in r and "no config found" in r and "→ gitpro setup" in r
    rc, data, s = doc_json(capsys)
    assert rc == 1 and data["ok"] is False and data["command"] == "doctor"
    assert s["config"]["status"] == "error"
    assert s["config"]["problems"][0]["fix"] == "gitpro setup"
    for name in ("applied", "files", "lint", "ssh", "repo"):
        assert s[name]["status"] == "skipped", name
    assert data["summary"]["problems"] >= 1


def test_applied_config_is_clean(home, cfg, capsys):  # 2
    cfg()
    rc, out = doc(capsys)
    assert rc == 0, out
    assert "✓" in row(out, "config") and "2 identities" in row(out, "config")
    assert "up to date" in row(out, "applied") and row(out, "applied").rstrip().endswith("✓")
    assert row(out, "files").rstrip().endswith("✓")
    assert "· guard: not installed (optional)" in out
    assert out.splitlines()[0].startswith("gitpro ") and " git 2." in out.splitlines()[0]
    rc, data, s = doc_json(capsys)
    assert rc == 0 and data["ok"] is True
    assert s["guard"]["status"] == "missing" and s["policy"]["status"] == "missing"
    assert s["config"]["status"] == "ok" and s["applied"]["status"] == "ok" and s["files"]["status"] == "ok"
    assert set(data["versions"]) == {"gitpro", "python", "git", "os"}
    assert [x["name"] for x in data["sections"]] == [n for n, _f, _m in doctor.SECTIONS]


def test_change_without_apply(home, cfg, capsys):  # 3
    cfg()
    cfg(DEFAULT_CONFIG.replace("me@corp.test", "new@corp.test"), apply=False)
    rc, out = doc(capsys)
    assert rc == 1
    r = row(out, "applied")
    assert "✗" in r and "1 pending" in r and "→ gitpro apply" in r


# ---------------------------------------------------------------- 4-5: lint and block order

def test_stray_user_email_is_a_lint_warning(home, cfg, capsys):  # 4
    cfg()
    gc = home / ".gitconfig"
    gc.write_text("[user]\n    email = stray@old.test\n" + gc.read_text())
    rc, out = doc(capsys)
    r = row(out, "lint")
    assert "⚠" in r and "user.email" in r and "→ remove it from ~/.gitconfig" in r


def test_block_not_last(home, cfg, capsys):  # 5
    cfg()
    gc = home / ".gitconfig"
    gc.write_text(gc.read_text() + "[alias]\n    st = status\n")
    rc, out = doc(capsys)
    r = row(out, "files")
    assert "✗" in r and "not last" in r and "→ gitpro apply" in r
    assert rc == 1


def test_ssh_block_not_first(home, cfg, capsys):
    cfg()
    sc = home / ".ssh/config"
    sc.write_text("Host old\n    User x\n\n" + sc.read_text())
    rc, out = doc(capsys)
    assert "not first" in row(out, "files") and rc == 1


# ---------------------------------------------------------------- 6: env, never values

def test_env_vars_set_or_not(home, cfg, capsys, monkeypatch):  # 6
    cfg()
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "x@y.test")
    monkeypatch.setenv("GH_TOKEN", "SECRET")
    rc, out = doc(capsys, "-v")
    r = row(out, "env")
    assert "⚠" in r and "→ unset GIT_AUTHOR_EMAIL" in out
    assert "GH_TOKEN is set" in out
    assert "SECRET" not in out and "x@y.test" not in out
    rc, data, s = doc_json(capsys)
    text = json.dumps(data)
    assert "SECRET" not in text and "x@y.test" not in text
    assert s["env"]["status"] == "warning"
    assert {"message": "GH_TOKEN is set in this shell", "fix": "unset GH_TOKEN"}.items() \
        <= next(p for p in s["env"]["problems"] if "GH_TOKEN" in p["message"]).items()


def test_core_sshcommand_outside_block_is_env(home, cfg, capsys):
    cfg()
    gc = home / ".gitconfig"
    gc.write_text("[core]\n\tsshCommand = ssh -i ~/.ssh/old\n" + gc.read_text())
    _rc, data, s = doc_json(capsys)
    assert any("core.sshCommand" in p["message"] for p in s["env"]["problems"])
    assert not any("sshcommand" in p["message"].lower() for p in s["lint"]["problems"])


def test_verbose_mentions_prompt_cache(home, cfg, capsys):
    cfg()
    _rc, out = doc(capsys, "-v")
    assert "prompt-cache.json" in out
    assert row(out, "versions")


# ---------------------------------------------------------------- 7: repo

def test_repo_override(home, cfg, capsys, tmp_path):  # 7
    cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    git(repo, "config", "user.email", "old@personal.test")
    rc, out = doc(capsys, str(repo))
    r = row(out, "repo")
    assert "✗" in r and "work-github" in r and "config --unset user.email" in r
    assert rc == 1


def test_repo_ok_and_outside(home, cfg, capsys, tmp_path, monkeypatch):
    cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    monkeypatch.chdir(repo)
    rc, data, s = doc_json(capsys)
    assert s["repo"]["status"] == "ok" and "work-github" in s["repo"]["summary"]
    monkeypatch.chdir(tmp_path)
    _rc, _data, s = doc_json(capsys)
    assert s["repo"]["status"] == "skipped"


# ---------------------------------------------------------------- 8: a section that raises

def test_section_that_raises(home, cfg, capsys, monkeypatch):  # 8
    cfg()

    def boom(ctx):
        raise RuntimeError("boom")
    sections = [(n, boom if n == "ssh" else f, m) for n, f, m in doctor.SECTIONS]
    monkeypatch.setattr(doctor, "SECTIONS", sections)
    rc, out = doc(capsys)
    assert "⚠ ssh: check failed (boom)" in out
    assert row(out, "repo") and row(out, "env") and row(out, "applied")
    assert rc == 0, out
    _rc, out = doc(capsys, "-v")
    assert "RuntimeError: boom" in out
    _rc, _d, s = doc_json(capsys)
    assert s["ssh"]["status"] == "warning"


# ---------------------------------------------------------------- 9: git too old

def test_git_too_old(home, cfg, capsys, fake_bin):  # 9
    cfg()
    real = shutil.which("git")
    fake_bin.add("git", f"""
if argv == ["--version"]:
    print("git version 2.30.0")
    sys.exit(0)
os.execv({real!r}, [{real!r}] + argv)
""")
    rc, out = doc(capsys)
    r = row(out, "versions")
    assert "✗" in r and "2.30" in r and "ppa:git-core/ppa" in r and "brew install git" in r
    assert rc == 1


# ---------------------------------------------------------------- ssh section

def test_ssh_modes_and_known_hosts(home, cfg, capsys):
    cfg()
    (home / ".ssh").chmod(0o755)
    (home / ".ssh/config.d/git-identities").chmod(0o644)
    _rc, _d, s = doc_json(capsys)
    msgs = " ".join(p["message"] + " " + p["fix"] for p in s["ssh"]["problems"])
    assert "chmod 700" in msgs and "chmod 600" in msgs
    assert "github.com not in ~/.ssh/known_hosts" in msgs
    (home / ".ssh").chmod(0o700)
    (home / ".ssh/config.d/git-identities").chmod(0o600)
    (home / ".ssh/known_hosts").write_text("github.com ssh-ed25519 AAAA\n")
    _rc, _d, s = doc_json(capsys)
    assert s["ssh"]["status"] == "ok", s["ssh"]


def test_old_tool_marker(home, cfg, capsys):
    cfg()
    sc = home / ".ssh/config"
    sc.write_text(sc.read_text().replace("# BEGIN gitpro", "# BEGIN git-identities")
                  .replace("# END gitpro", "# END git-identities"))
    _rc, _d, s = doc_json(capsys)
    assert any("git-identities" in p["message"] for p in s["ssh"]["problems"])


# ---------------------------------------------------------------- optional packages (stubs)

def test_stub_guard_status(home, cfg, capsys, monkeypatch):
    cfg()
    guard = types.ModuleType("gitpro.guard")
    guard.status = lambda: {"installed": "global", "on_mismatch": "block", "hooks": ["pre-commit"], "error": None}
    monkeypatch.setitem(sys.modules, "gitpro.guard", guard)
    rc, out = doc(capsys)
    assert "installed, block" in row(out, "guard") and row(out, "guard").rstrip().endswith("✓")
    guard.status = lambda: {"installed": "none", "on_mismatch": "block", "hooks": [], "error": "core.hooksPath set"}
    _rc, _d, s = doc_json(capsys)
    assert s["guard"]["status"] == "warning" and "core.hooksPath" in json.dumps(s["guard"])


def test_doctor_section_hook_wins(home, cfg, capsys, monkeypatch):
    cfg()
    policy = types.ModuleType("gitpro.policy")
    policy.doctor_section = lambda ctx: {"status": "error", "summary": "Example Corp: signing required",
                                         "problems": [{"message": "sign=false", "fix": "set sign = true"}]}
    monkeypatch.setitem(sys.modules, "gitpro.policy", policy)
    rc, out = doc(capsys)
    r = row(out, "policy")
    assert "✗" in r and "→ set sign = true" in r and rc == 1


def test_stub_policy_check_machine(home, cfg, capsys, monkeypatch):
    cfg()
    policy = types.ModuleType("gitpro.policy")
    policy.check_machine = lambda cfg: ["signing required but sign=false"]
    monkeypatch.setitem(sys.modules, "gitpro.policy", policy)
    _rc, _d, s = doc_json(capsys)
    assert s["policy"]["status"] == "error"
    policy.check_machine = lambda cfg: []
    _rc, _d, s = doc_json(capsys)
    assert s["policy"]["status"] == "ok"


def test_stub_agents_and_mcp(home, cfg, capsys, monkeypatch):
    cfg()
    agents = types.ModuleType("gitpro.agents")
    seen = []

    def status_data(home, explicit_config=None):   # the shape of 10's agents.status_data
        seen.append((home, explicit_config))
        return {"mode": "block", "agents": {"claude": {"user": str(home / ".claude/settings.json"), "project": None,
                                                       "plugin": None, "experimental": False}},
                "guard_installed": None, "gitpro_path": {"sh": "/usr/bin/gitpro", "login": None},
                "warnings": ["gitpro is not on PATH for sh → gitpro agent install --agent claude"]}
    agents.status_data = status_data
    mcp = types.ModuleType("gitpro.mcp")
    mcp.status_data = lambda home: {"summary": "mcp registered"}
    monkeypatch.setitem(sys.modules, "gitpro.agents", agents)
    monkeypatch.setitem(sys.modules, "gitpro.mcp", mcp)
    _rc, out = doc(capsys)
    r = row(out, "agents")
    assert "claude hook installed · mcp registered" in r and "→ gitpro agent install --agent claude" in r
    assert seen == [(home, None)]


def test_stub_health_keys_offline_and_online(home, cfg, capsys, monkeypatch):
    cfg()
    seen = []
    health = types.ModuleType("gitpro.health")

    def check_keys(cfg, online=False):
        seen.append(online)
        return {"summary": "2 keys · 1 without passphrase",
                "problems": [{"message": "id_work has no passphrase", "fix": "gitpro keys --health"}]}
    health.check_keys = check_keys
    monkeypatch.setitem(sys.modules, "gitpro.health", health)
    _rc, _d, s = doc_json(capsys)
    assert s["keys"]["status"] == "warning" and s["keys"]["problems"][0]["fix"] == "gitpro keys --health"
    assert seen == [False] and s["forge"]["status"] == "skipped"
    # --online with neither gh nor glab on PATH
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: None if n in ("gh", "glab") else real_which(n, *a, **k))
    run("doctor", "--json", "--online")
    captured = capsys.readouterr()
    s = {x["name"]: x for x in json.loads(captured.out)["sections"]}   # the announcement is not in the JSON
    assert captured.err.count("online: asking") == 1
    assert "needs gh or glab" in json.dumps(s["forge"]) and "needs gh or glab" in json.dumps(s["keys"])


def test_capability_registered(home):
    from gitpro import args, cli
    cli.build_parser()
    assert "doctor" in args.CAPABILITIES


def test_run_returns_report(home, cfg):
    path = cfg()
    rep = doctor.run(path, None, False)
    assert rep["command"] == "doctor" and rep["ok"] is True
    assert {s["name"] for s in rep["sections"]} >= {"versions", "config", "repo", "env"}


# ---------------------------------------------------------------- QA (review of 16): real bugs, expected to fail until fixed

def test_qa_guard_installed_none_is_not_installed(home, cfg, capsys, monkeypatch):
    """Spec 06 §4: guard.status()["installed"] is "global"|"none"; the string "none" is truthy."""
    cfg()
    guard = types.ModuleType("gitpro.guard")
    guard.status = lambda: {"installed": "none", "on_mismatch": "block", "hooks": [], "error": None}
    monkeypatch.setitem(sys.modules, "gitpro.guard", guard)
    _rc, out = doc(capsys)
    assert "not installed" in row(out, "guard"), out


def test_qa_agents_real_status_signature(home, cfg, capsys, monkeypatch):
    """Package 10's agents.status(args) is the CLI command; the data function is status_data(home, config)."""
    cfg()
    agents = types.ModuleType("gitpro.agents")

    def status(args):  # the real signature in 10
        raise AssertionError("doctor must not call the CLI command")
    agents.status = status
    agents.status_data = lambda home, explicit_config=None: {"summary": "claude hook installed"}
    monkeypatch.setitem(sys.modules, "gitpro.agents", agents)
    _rc, _d, s = doc_json(capsys)
    assert s["agents"]["status"] == "ok", s["agents"]


def test_qa_optional_module_import_error_is_a_failed_section(home, cfg, monkeypatch):
    """An optional module that raises ImportError on import (not ModuleNotFoundError) must not stop the report."""
    cfg()
    monkeypatch.delitem(sys.modules, "gitpro.guard", raising=False)

    class Broken:
        def find_spec(self, name, path=None, target=None):
            if name == "gitpro.guard":
                raise ImportError("cannot import name 'NO_SUCH_NAME' from 'gitpro.render'")
            return None
    monkeypatch.setattr(sys, "meta_path", [Broken(), *sys.meta_path])
    rep = doctor.run(None, None, False)
    text = json.dumps(rep, default=str)
    assert "check failed" in text and "guard" in text


def test_online_announces_once_in_text(home, cfg, capsys):
    cfg()
    rc, out = doc(capsys, "--online")
    assert out.count("online: asking") == 1 and out.startswith("online: asking")


def test_forge_missing_hidden_offline(home, cfg, capsys):
    cfg()
    _rc, out = doc(capsys)
    assert "forge" not in out
    _rc, out = doc(capsys, "-v")
    assert "· forge: not installed (optional)" in out


def test_env_lists_git_config_system_and_parameters(home, cfg, capsys, monkeypatch):
    cfg()
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'user.email'='x@y.test'")
    _rc, _d, s = doc_json(capsys)
    msgs = [p["message"] for p in s["env"]["problems"]]
    assert "GIT_CONFIG_SYSTEM is set in this shell" in msgs and "GIT_CONFIG_PARAMETERS is set in this shell" in msgs
    assert "x@y.test" not in json.dumps(s)
