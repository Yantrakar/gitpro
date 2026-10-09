"""`gitpro mcp` (spec 11): stdio MCP server with read-only tools, and `mcp install/uninstall`."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from conftest import TIMEOUT, git, repo_with_remote, run, write_config
from gitpro import __version__, mcp

ROOT = Path(__file__).resolve().parent.parent
WORK_URL = "git@github.com:corp-org/app.git"
ENTRY = {"type": "stdio", "command": "gitpro", "args": ["mcp"]}
ENV_VARS = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_SSH_COMMAND",
            "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITLAB_TOKEN")


def has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


@pytest.fixture(autouse=True)
def sandbox(home, fake_bin, monkeypatch, tmp_path):
    """PATH = fake executables + git and sh only (no real claude, gh or glab); a scratch cwd; no identity env vars."""
    gitbin = tmp_path / "gitbin"
    gitbin.mkdir()
    for tool in ("git", "sh", "cp", "rm", "chmod"):
        (gitbin / tool).symlink_to(shutil.which(tool))
    monkeypatch.setenv("PATH", f"{fake_bin.path}{os.pathsep}{gitbin}")
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    return work


def lines(text: str) -> list:
    return [json.loads(ln) for ln in text.splitlines()]


def rpc(*msgs, cfg_path=None, log=None) -> list:
    """Every response serve() writes for msgs (dicts, or raw strings sent as they are)."""
    inp = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in msgs)
    out = io.StringIO()
    assert mcp.serve(io.StringIO(inp), out, cfg_path, log=log) == 0
    return lines(out.getvalue())


def init(version="2025-11-25", id=0) -> dict:
    return {"jsonrpc": "2.0", "id": id, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def call_msg(name, args=None, id=1) -> dict:
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}


def call(name, args=None, version="2025-11-25", cfg_path=None) -> dict:
    """The tools/call result (asserts it is a result, not a JSON-RPC error)."""
    out = rpc(init(version), call_msg(name, args), cfg_path=cfg_path)
    assert len(out) == 2 and "result" in out[1], out
    return out[1]["result"]


def text_of(result) -> str:
    return result["content"][0]["text"]


def payload(result) -> dict:
    assert result["isError"] is False, text_of(result)
    obj = json.loads(text_of(result))
    if "structuredContent" in result:
        assert result["structuredContent"] == obj
    return obj


def cli_json(capsys, *argv) -> dict:
    capsys.readouterr()
    run(*argv)
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def work_repo(cfg, tmp_path):
    cfg()
    return repo_with_remote(tmp_path, "app", WORK_URL)


# ---------------------------------------------------------------- protocol (1-4, 10)

def test_initialize_newest():
    out = rpc(init("1999-01-01"))
    r = out[0]["result"]
    assert out[0]["id"] == 0 and out[0]["jsonrpc"] == "2.0"
    assert r["protocolVersion"] == mcp.PROTOCOL_VERSIONS[-1]
    assert r["serverInfo"] == {"name": "gitpro", "version": __version__}
    assert r["capabilities"]["tools"] == {"listChanged": False}
    assert "read-only" in r["instructions"]


def test_protocol_versions_constant():
    assert mcp.PROTOCOL_VERSIONS[0] == "2024-11-05"
    assert list(mcp.PROTOCOL_VERSIONS) == sorted(mcp.PROTOCOL_VERSIONS)


def test_old_protocol_echoed_without_structured_content(home):
    out = rpc(init("2024-11-05"), call_msg("identities"))
    assert out[0]["result"]["protocolVersion"] == "2024-11-05"
    assert "structuredContent" not in out[1]["result"]


def test_new_protocol_has_structured_content(work_repo):
    out = rpc(init("2025-06-18"), call_msg("which_identity", {"path": str(work_repo)}))
    assert out[0]["result"]["protocolVersion"] == "2025-06-18"
    assert out[1]["result"]["structuredContent"]["ok"] is True


def test_notifications_get_no_reply_and_ping():
    out = io.StringIO()
    mcp.serve(io.StringIO(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
                          + json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                        "params": {"requestId": 3}}) + "\n"
                          + json.dumps({"jsonrpc": "2.0", "method": "no/such/thing"}) + "\n"), out, None)
    assert out.getvalue() == ""
    assert rpc({"jsonrpc": "2.0", "id": "p", "method": "ping"}) == [{"jsonrpc": "2.0", "id": "p", "result": {}}]


def test_resources_and_prompts_are_empty():
    out = rpc({"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
              {"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})
    assert out[0]["result"] == {"resources": []} and out[1]["result"] == {"prompts": []}


def test_tools_list():
    tools = {t["name"]: t for t in rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"]}
    for name in ("which_identity", "why", "check_commit", "scan", "identities"):
        assert name in tools
    for name, module in (("lost", "gitpro.lost"), ("audit", "gitpro.audit"), ("env_for", "gitpro.envcmd")):
        assert (name in tools) == has(module)
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object" and t["description"]
        assert t["annotations"] == {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                                    "openWorldHint": t["name"] in ("preflight", "reconcile_operation")}
    assert tools["why"]["inputSchema"]["required"] == ["path"]
    assert tools["scan"]["inputSchema"]["properties"]["roots"]["maxItems"] == 20


def test_protocol_errors_and_server_continues():
    out = rpc("{not json", {"jsonrpc": "2.0", "id": 5}, {"jsonrpc": "2.0", "id": 6, "method": "frobnicate"},
              call_msg("no_such_tool", id=7), call_msg("why", {}, id=8),
              call_msg("why", {"path": 3}, id=9), call_msg("identities", {"extra": 1}, id=10),
              {"jsonrpc": "2.0", "id": 11, "method": "ping"})
    codes = [(m["id"], m.get("error", {}).get("code")) for m in out]
    assert codes == [(None, -32700), (5, -32600), (6, -32601), (7, -32602), (8, -32602), (9, -32602),
                     (10, -32602), (11, None)]
    assert out[-1]["result"] == {}
    assert "no_such_tool" in out[3]["error"]["message"]


def test_batch_and_non_object_requests_are_invalid():
    out = rpc("[1, 2]", "42")
    assert [m["error"]["code"] for m in out] == [-32600, -32600]


# ---------------------------------------------------------------- tools (5-9, 11, 12)

def test_which_identity_matches_cli(work_repo, capsys):
    got = payload(call("which_identity", {"path": str(work_repo)}))
    want = cli_json(capsys, "which", "--json", str(work_repo))
    assert got == want
    assert got["ok"] is True and got["settings"]["user.email"]["value"] == "me@corp.test"


def test_why_reports_override(work_repo, capsys):
    git(work_repo, "config", "user.email", "me@personal.test")
    r = call("why", {"path": str(work_repo)})
    assert r["isError"] is False
    got = payload(r)
    assert got["problems"][0]["code"] == "override" and got["ok"] is False
    want = cli_json(capsys, "why", "--json", str(work_repo))
    assert got == want


def test_why_with_remote(work_repo):
    git(work_repo, "remote", "add", "mine", "git@github.com:me/app.git")
    got = payload(call("why", {"path": str(work_repo), "remote": "mine"}))
    assert got["expected"]["id"] == "personal"
    assert payload(call("why", {"path": str(work_repo), "remote": "origin"}))["expected"]["id"] == "work-github"


def test_why_relative_path_is_resolved(work_repo, monkeypatch):
    monkeypatch.chdir(work_repo.parent)
    assert payload(call("why", {"path": "app"}))["repo"] == str(work_repo)


def test_check_commit_committer_mismatch_blocks(work_repo):
    got = payload(call("check_commit", {"path": str(work_repo), "committer_email": "me@personal.test"}))
    assert got["would_block"] is True and got["ok"] is False and got["mode"] == "block"
    assert got["fix"] and got["reason"] and "me@personal.test" in got["reason"]
    assert got["problems"][0]["code"] == "committer"


def test_check_commit_colleague_author_passes(work_repo):
    got = payload(call("check_commit", {"path": str(work_repo), "author_email": "pat@other.test",
                                        "committer_email": "me@corp.test"}))
    assert got == {"ok": True, "command": "check_commit", "would_block": False, "mode": "block", "problems": [],
                   "reason": None, "fix": None}


def test_check_commit_own_author_blocks(work_repo):
    got = payload(call("check_commit", {"path": str(work_repo), "author_email": "ME@personal.test"}))
    assert got["would_block"] is True and got["problems"][0]["code"] == "author"
    assert "--author=" in got["fix"]


def test_check_commit_default_committer_is_effective(work_repo):
    assert payload(call("check_commit", {"path": str(work_repo)}))["would_block"] is False
    git(work_repo, "config", "user.email", "me@personal.test")
    got = payload(call("check_commit", {"path": str(work_repo)}))
    assert got["would_block"] is True
    assert got["fix"] == f"git -C {work_repo} config --unset user.email"


def test_check_commit_warn_mode(cfg, tmp_path):
    from conftest import DEFAULT_CONFIG
    cfg(DEFAULT_CONFIG + '\n[agents]\nhook = "warn"\n')
    repo = repo_with_remote(tmp_path, "app", WORK_URL)
    got = payload(call("check_commit", {"path": str(repo), "committer_email": "me@personal.test"}))
    assert got["would_block"] is False and got["mode"] == "warn" and got["ok"] is False and got["problems"]


@pytest.mark.skipif(not has("gitpro.trailers"), reason="needs package 08 (trailers)")
def test_check_commit_message_trailers(cfg, tmp_path):
    from conftest import DEFAULT_CONFIG
    text = DEFAULT_CONFIG.replace('gh_account = "me-work"', 'gh_account = "me-work"\n\n[identity.trailers]\n'
                                  'co_authored_by = "forbid"')
    cfg(text)
    repo = repo_with_remote(tmp_path, "app", WORK_URL)
    msg = "fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
    got = payload(call("check_commit", {"path": str(repo), "message": msg}))
    assert got["would_block"] is True and any(p["code"] == "trailer" for p in got["problems"])


@pytest.mark.skipif(not has("gitpro.trailers"), reason="needs package 08 (trailers)")
def test_check_commit_require_violation_has_its_own_fix(cfg, tmp_path):
    from conftest import DEFAULT_CONFIG
    text = DEFAULT_CONFIG.replace('gh_account = "me-work"', 'gh_account = "me-work"\n\n[identity.trailers]\n'
                                  'co_authored_by = "require"')
    cfg(text)
    repo = repo_with_remote(tmp_path, "app", WORK_URL)
    got = payload(call("check_commit", {"path": str(repo), "message": "fix\n"}))
    trailer = [p for p in got["problems"] if p["code"] == "trailer"]
    assert len(trailer) == 1 and "requires a Co-Authored-By trailer" in trailer[0]["message"]
    assert "Co-Authored-By: Name <email>" in trailer[0]["fix"] and "remove" not in trailer[0]["fix"]
    ok = payload(call("check_commit", {"path": str(repo),
                                       "message": "fix\n\nCo-Authored-By: Pat <pat@corp.test>\n"}))
    assert not [p for p in ok["problems"] if p["code"] == "trailer"]


def test_scan_matches_cli_and_never_fixes(work_repo, capsys):
    git(work_repo, "config", "user.email", "me@personal.test")
    before = (work_repo / ".git/config").read_bytes()
    got = payload(call("scan", {"repos": [str(work_repo)]}))
    assert (work_repo / ".git/config").read_bytes() == before
    want = cli_json(capsys, "scan", "--json", "--repo", str(work_repo))
    assert got == want and got["overrides"][0]["path"] == str(work_repo)


def test_scan_roots(work_repo, capsys):
    got = payload(call("scan", {"roots": [str(work_repo.parent)]}))
    assert got == cli_json(capsys, "scan", "--json", str(work_repo.parent))
    assert got["repos"] >= 1


def test_scan_roots_capped_and_must_exist(tmp_path):
    out = rpc(init(), call_msg("scan", {"roots": [str(tmp_path)] * 21}))
    assert out[1]["error"]["code"] == -32602
    r = call("scan", {"roots": [str(tmp_path / "missing")]})
    assert r["isError"] is True and "not a directory" in text_of(r)


def test_identities(work_repo):
    got = payload(call("identities"))
    assert got["ok"] is True and got["command"] == "identities"
    assert got["config"].endswith(".config/gitpro.toml")
    assert [(i["id"], i["email"]) for i in got["identities"]] == [("work-github", "me@corp.test"),
                                                                  ("personal", "me@personal.test")]
    assert set(got["identities"][0]) == {"id", "name", "email", "host", "owners", "ssh_alias", "gh_account",
                                         "glab_account", "clone_dir", "sign"}
    assert got["identities"][0]["owners"] == ["corp-org"] and got["identities"][0]["gh_account"] == "me-work"
    assert got["default"]["email"] == "me@default.test"
    blob = json.dumps(got)
    assert "ssh-ed25519" not in blob and "id_work" not in blob and '"git"' not in blob


def test_identities_never_include_git_tables(cfg):
    from conftest import DEFAULT_CONFIG
    cfg(DEFAULT_CONFIG + '\n[identity.git]\n"core.editor" = "secret-editor"\n', apply=False)
    blob = text_of(call("identities"))
    assert "secret-editor" not in blob and "core.editor" not in blob


@pytest.mark.skipif(not has("gitpro.envcmd"), reason="needs package 12 (env)")
def test_env_for_commands_never_values(work_repo, capsys, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_secretvalue123")
    got = payload(call("env_for", {"path": str(work_repo)}))
    assert got["env_commands"]["GH_TOKEN"] == "gh auth token -u me-work -h github.com"
    assert "ghp_secretvalue123" not in json.dumps(got)
    assert got == cli_json(capsys, "env", "--json", str(work_repo))
    fish = payload(call("env_for", {"path": str(work_repo), "shell": "fish"}))
    assert any(ln.startswith("set -gx GH_TOKEN") for ln in fish["lines"])
    out = rpc(init(), call_msg("env_for", {"path": str(work_repo), "shell": "bash"}))
    assert out[1]["error"]["code"] == -32602


@pytest.mark.skipif(not has("gitpro.audit"), reason="needs package 08 (audit)")
def test_audit_never_rewrites(work_repo):
    from conftest import commit
    commit(work_repo, "wrong", author=("Personal Me", "me@personal.test"))
    head = git(work_repo, "rev-parse", "HEAD")
    got = payload(call("audit", {"path": str(work_repo), "all": True}))
    assert got["command"] == "audit"
    assert git(work_repo, "rev-parse", "HEAD") == head


@pytest.mark.skipif(not has("gitpro.lost"), reason="needs package 03 (lost)")
def test_lost_offline(work_repo, fake_bin):
    fake_bin.add("gh", "sys.exit(1)")
    got = payload(call("lost", {"roots": [str(work_repo.parent)]}))
    assert got["command"] == "lost" and got.get("online") in (False, None)
    assert fake_bin.calls("gh") == []
    out = rpc(init(), call_msg("lost", {"roots": [str(work_repo.parent)], "only": ["remote_gone"]}))
    assert out[1]["error"]["code"] == -32602


def test_not_a_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    for name in ("which_identity", "check_commit"):
        r = call(name, {"path": str(plain)})
        assert r["isError"] is True and "not a git repository" in text_of(r)
        assert text_of(r).startswith("✗ ") and "structuredContent" not in r
    r = call("which_identity", {"path": str(tmp_path / "nope")})
    assert r["isError"] is True and "not a git repository" in text_of(r)


def test_accidental_print_goes_to_stderr(home, monkeypatch, capsys):
    tool = mcp.TOOLS["identities"]

    def noisy(args, ctx):
        print("oops")
        return {"ok": True, "command": "identities"}
    monkeypatch.setattr(tool, "fn", noisy)
    out = io.StringIO()
    inp = "".join(json.dumps(m) + "\n" for m in (init(), call_msg("identities"), call_msg("identities", id=2)))
    mcp.serve(io.StringIO(inp), out, None)
    assert len(lines(out.getvalue())) == 3
    assert "oops" not in out.getvalue()
    assert "oops" in capsys.readouterr().err


def test_internal_error_is_a_tool_error(home, monkeypatch, capsys):
    def broken(args, ctx):
        raise ZeroDivisionError("boom")
    monkeypatch.setattr(mcp.TOOLS["identities"], "fn", broken)
    out = rpc(init(), call_msg("identities"), {"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert out[1]["result"]["isError"] is True
    assert text_of(out[1]["result"]) == "✗ internal error: ZeroDivisionError"
    assert out[2]["result"] == {}
    assert "Traceback" in capsys.readouterr().err


def test_no_config(home, tmp_path):
    repo = repo_with_remote(tmp_path, "app", WORK_URL)
    assert payload(call("which_identity", {"path": str(repo)}))["ok"] is True
    r = call("why", {"path": str(repo)})
    assert r["isError"] is True and "no config found" in text_of(r)
    r = call("identities")
    assert r["isError"] is True and "no config found" in text_of(r)


def test_config_path_and_config_errors(home, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[default\n")
    bad.chmod(0o600)
    r = call("identities", cfg_path=bad)
    assert r["isError"] is True and text_of(r).startswith("✗ ")
    good = write_config(home, '[default]\nname = "X"\nemail = "x@x.test"\n')
    alt = tmp_path / "alt.toml"
    alt.write_text(good.read_text().replace("x@x.test", "alt@x.test"))
    alt.chmod(0o600)
    assert payload(call("identities", cfg_path=alt))["default"]["email"] == "alt@x.test"


def test_lost_absent_is_unknown_tool(monkeypatch):
    monkeypatch.setitem(sys.modules, "gitpro.lost", None)
    names = [t["name"] for t in rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"]]
    assert "lost" not in names
    out = rpc(init(), call_msg("lost", {}))
    assert out[1]["error"]["code"] == -32602


def test_log_file_has_requests_stderr_only_tool_names(work_repo, tmp_path, capsys):
    log = tmp_path / "mcp.log"
    rpc(init(), call_msg("why", {"path": str(work_repo)}), log=log)
    err = capsys.readouterr().err
    assert "why" in err and str(work_repo) not in err
    text = log.read_text()
    assert str(work_repo) in text and '"tools/call"' in text
    assert log.stat().st_mode & 0o077 == 0


def test_serve_restores_git_terminal_prompt(monkeypatch):
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    seen = {}

    def spy(args, ctx):
        seen["v"] = os.environ.get("GIT_TERMINAL_PROMPT")
        return {"ok": True, "command": "identities"}
    monkeypatch.setattr(mcp.TOOLS["identities"], "fn", spy)
    rpc(init(), call_msg("identities"))
    assert seen["v"] == "0" and "GIT_TERMINAL_PROMPT" not in os.environ


# ---------------------------------------------------------------- subprocess (13)

def test_subprocess_server(home, tmp_path):
    env = dict(os.environ, PYTHONPATH=str(Path(mcp.__file__).resolve().parent.parent))
    msgs = [init(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            call_msg("which_identity", {"path": str(tmp_path / "secret-arg-xyz")}, id=2)]
    r = subprocess.run([sys.executable, "-m", "gitpro", "mcp"], input="".join(json.dumps(m) + "\n" for m in msgs),
                       capture_output=True, text=True, env=env, timeout=TIMEOUT, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = lines(r.stdout)
    assert [m["id"] for m in out] == [0, 1, 2]
    assert out[0]["result"]["serverInfo"]["name"] == "gitpro"
    assert out[2]["result"]["isError"] is True
    assert "secret-arg-xyz" not in r.stderr


# ---------------------------------------------------------------- install / uninstall (14, 15)

def claude_json(home) -> dict:
    return json.loads((home / ".claude.json").read_text())


def state_files(home, prefix):
    d = home / ".local/state/gitpro"
    return sorted(p.name for p in d.iterdir() if p.name.startswith(prefix)) if d.exists() else []


def test_install_claude_without_cli_edits_claude_json(home, capsys):
    (home / ".claude.json").write_text(json.dumps({"numStartups": 3}, indent=2) + "\n")
    assert run("mcp", "install", "--agent", "claude", "-y") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("→ plugin users already have the server")
    assert claude_json(home) == {"numStartups": 3, "mcpServers": {"gitpro": ENTRY}}
    assert len(state_files(home, "mcp-undo-")) == 1 and state_files(home, "backup-")
    assert "↩ undo: sh" in out
    # idempotent
    assert run("mcp", "install", "--agent", "claude", "-y", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["command"] == "mcp install" and data["changed"] is False
    assert data["via"] == "file" and data["undo"] is None
    assert len(state_files(home, "mcp-undo-")) == 1


def test_install_claude_creates_file_and_undo_removes_it(home, capsys):
    assert run("mcp", "install", "-y", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["changed"] is True and data["file"] == str(home / ".claude.json") and data["backup"] is None
    assert claude_json(home)["mcpServers"]["gitpro"] == ENTRY
    subprocess.run(["sh", data["undo"]], check=True, timeout=TIMEOUT)
    assert not (home / ".claude.json").exists()


def test_install_claude_via_cli(home, fake_bin, capsys):
    fake_bin.add("claude", "")
    assert run("mcp", "install", "--agent", "claude", "-y", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert fake_bin.calls("claude") == [["mcp", "add", "--scope", "user", "--transport", "stdio", "gitpro", "--",
                                         "gitpro", "mcp"]]
    assert not (home / ".claude.json").exists()
    assert data["via"] == "claude-cli" and data["changed"] is True and data["undo"]
    assert "claude mcp remove --scope user gitpro" in Path(data["undo"]).read_text()


def test_install_claude_cli_failure_falls_back(home, fake_bin, capsys):
    fake_bin.add("claude", "sys.exit(4)")
    assert run("mcp", "install", "-y") == 0
    cap = capsys.readouterr()
    assert "⚠ claude mcp add failed (4); editing ~/.claude.json directly" in cap.err
    assert claude_json(home)["mcpServers"]["gitpro"] == ENTRY
    assert len(state_files(home, "mcp-undo-")) == 1


def test_install_needs_yes_without_terminal(home, capsys):
    assert run("mcp", "install") == 0
    assert "Dry run" in capsys.readouterr().out
    assert not (home / ".claude.json").exists()
    assert run("mcp", "install", "--json") == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_uninstall_claude(home, fake_bin, capsys):
    assert run("mcp", "install", "-y") == 0
    assert run("mcp", "uninstall", "-y") == 0
    assert claude_json(home).get("mcpServers", {}) == {}
    capsys.readouterr()
    assert run("mcp", "uninstall", "-y", "--json") == 0
    assert json.loads(capsys.readouterr().out)["changed"] is False
    # an entry that is not gitpro's own is left alone
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"gitpro": {"command": "other"}}}))
    assert run("mcp", "uninstall", "-y") == 1
    assert claude_json(home)["mcpServers"]["gitpro"] == {"command": "other"}


def test_uninstall_claude_via_cli(home, fake_bin, capsys):
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"gitpro": ENTRY}}))
    fake_bin.add("claude", "")
    assert run("mcp", "uninstall", "-y", "--json") == 0
    assert fake_bin.calls("claude") == [["mcp", "remove", "--scope", "user", "gitpro"]]
    assert json.loads(capsys.readouterr().out)["via"] == "claude-cli"


def test_project_scope_needs_write(home, tmp_path, monkeypatch, capsys):
    repo = repo_with_remote(tmp_path, "team", WORK_URL)
    monkeypatch.chdir(repo)
    assert run("mcp", "install", "--scope", "project", "-y") == 2
    err = capsys.readouterr().err
    assert "✗ .mcp.json is shared with your team; re-run with --write to edit it (backup + undo), " \
           "or use --scope user" in err
    assert not (repo / ".mcp.json").exists()
    assert run("mcp", "install", "--scope", "project", "--write", "-y", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["file"] == str(repo / ".mcp.json") and data["backup"] is None
    assert json.loads((repo / ".mcp.json").read_text()) == {"mcpServers": {"gitpro": ENTRY}}
    # existing file: backup
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}, indent=4))
    assert run("mcp", "install", "--scope", "project", "--write", "-y", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["changed"] is True and data["backup"] and Path(data["backup"]).exists()
    assert set(json.loads((repo / ".mcp.json").read_text())["mcpServers"]) == {"other", "gitpro"}


def test_project_scope_refuses_comments(home, tmp_path, monkeypatch, capsys):
    repo = repo_with_remote(tmp_path, "team", WORK_URL)
    monkeypatch.chdir(repo)
    text = '{\n  // team servers\n  "mcpServers": {}\n}\n'
    (repo / ".mcp.json").write_text(text)
    assert run("mcp", "install", "--scope", "project", "--write", "-y") == 2
    assert "comments" in capsys.readouterr().err
    assert (repo / ".mcp.json").read_text() == text


def test_project_scope_outside_repo(home, capsys):
    assert run("mcp", "install", "--scope", "project", "--write", "-y") == 2
    assert "not a git repository" in capsys.readouterr().err


@pytest.mark.parametrize("agent,rel,key,entry", [
    ("cursor", ".cursor/mcp.json", "mcpServers", ENTRY),
    ("gemini", ".gemini/settings.json", "mcpServers", {"command": "gitpro", "args": ["mcp"]}),
    ("opencode", ".config/opencode/opencode.json", "mcp", {"type": "local", "command": ["gitpro", "mcp"]}),
    ("claude-desktop", ".config/Claude/claude_desktop_config.json", "mcpServers",
     {"command": "gitpro", "args": ["mcp"]}),
])
def test_install_other_agents(home, capsys, agent, rel, key, entry):
    if agent == "claude-desktop" and sys.platform == "darwin":
        rel = "Library/Application Support/Claude/claude_desktop_config.json"
    assert run("mcp", "install", "--agent", agent, "-y") == 0
    assert json.loads((home / rel).read_text())[key]["gitpro"] == entry
    assert run("mcp", "uninstall", "--agent", agent, "-y") == 0
    assert json.loads((home / rel).read_text())[key] == {}


def test_project_scope_only_for_claude_and_cursor(home, tmp_path, monkeypatch, capsys):
    repo = repo_with_remote(tmp_path, "team", WORK_URL)
    monkeypatch.chdir(repo)
    assert run("mcp", "install", "--agent", "gemini", "--scope", "project", "--write", "-y") == 2
    assert run("mcp", "install", "--agent", "cursor", "--scope", "project", "-y") == 2
    assert ".cursor/mcp.json is shared with your team" in capsys.readouterr().err
    assert run("mcp", "install", "--agent", "cursor", "--scope", "project", "--write", "-y") == 0
    assert json.loads((repo / ".cursor/mcp.json").read_text())["mcpServers"]["gitpro"] == ENTRY


def test_capabilities(capsys):
    run("--version", "--json")
    caps = json.loads(capsys.readouterr().out)["capabilities"]
    assert "mcp" in caps and "mcp.install" in caps


# ---------------------------------------------------------------- static (16)

def test_plugin_files():
    data = json.loads((ROOT / ".mcp.json").read_text())
    assert data == {"mcpServers": {"gitpro": ENTRY}}
    manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    assert "mcpServers" not in manifest


def test_module_is_stdlib_only():
    import ast
    tree = ast.parse(Path(mcp.__file__).read_text())
    top = {n.names[0].name.split(".")[0] for n in tree.body if isinstance(n, ast.Import)}
    top |= {n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    assert not top & {"rich", "textual", "tomli"}


# ---------------------------------------------------------------- doctor (16 integration)

def test_doctor_section_shapes(home):
    from gitpro import doctor
    sec = mcp.doctor_section(doctor.Ctx(home))
    assert sec["status"] == "ok" and "not registered" in sec["summary"]
    assert run("mcp", "install", "-y") == 0
    sec = mcp.doctor_section(doctor.Ctx(home))
    assert sec["status"] == "ok" and "claude" in sec["summary"] and sec["problems"] == []
    (home / ".cursor").mkdir()
    (home / ".cursor/mcp.json").write_text("{broken")
    sec = mcp.doctor_section(doctor.Ctx(home))
    assert sec["status"] == "warning" and "mcp.json" in sec["problems"][0]["message"]


def test_doctor_uses_real_module(home, cfg, capsys, monkeypatch):
    cfg()
    agents = types.ModuleType("gitpro.agents")
    agents.status_data = lambda home, explicit_config=None: {
        "mode": "block", "agents": {"claude": {"user": None, "project": None, "plugin": None}}, "warnings": []}
    monkeypatch.setitem(sys.modules, "gitpro.agents", agents)
    for name in ("gitpro.guard", "gitpro.health", "gitpro.policy"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "gitpro.mcp", mcp)
    assert run("mcp", "install", "-y") == 0
    capsys.readouterr()
    run("doctor", "--json")
    data = json.loads(capsys.readouterr().out)
    sec = {s["name"]: s for s in data["sections"]}["agents"]
    assert "mcp registered: claude" in sec["summary"]


# ---------------------------------------------------------------- QA fixes: other packages' signatures

def _fake_module(monkeypatch, name: str, **attrs) -> types.ModuleType:
    import gitpro
    mod = types.ModuleType(f"gitpro.{name}")
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, f"gitpro.{name}", mod)
    monkeypatch.setattr(gitpro, name, mod, raising=False)
    return mod


LOST_OFFLINE = ("unpushed", "merged_gone", "stash", "dirty", "no_remote", "duplicate", "stale", "empty")


def _fake_lost(monkeypatch, seen: dict):
    """03's real signature: collect(roots, exclude, cfg, *, online, kinds: Set[str], stale_days, repos=None,
    roots_source="args", announce_to=None); it computes `kinds & {...}`."""
    def collect(roots, exclude, cfg, *, online, kinds, stale_days, repos=None, roots_source="args",
                announce_to=None):
        assert isinstance(roots, list) and roots and all(isinstance(r, Path) and r.is_absolute() for r in roots)
        assert isinstance(exclude, list) and all(isinstance(e, str) for e in exclude)
        assert online is False and repos is None and announce_to is None
        assert isinstance(kinds, set) and kinds & {"unpushed", "stash"} is not None
        assert kinds <= set(LOST_OFFLINE) | {"remote_gone"}
        assert isinstance(stale_days, int) and not isinstance(stale_days, bool)
        assert roots_source in ("args", "config", "default")
        seen.update(roots=roots, exclude=exclude, cfg=cfg, kinds=kinds, stale_days=stale_days, source=roots_source)
        return {"ok": True, "warnings": [], "roots": [str(r) for r in roots], "roots_source": roots_source,
                "repos": [], "online": False}
    return _fake_module(monkeypatch, "lost", KINDS=LOST_OFFLINE[:5] + ("remote_gone",) + LOST_OFFLINE[5:],
                        OFFLINE_KINDS=LOST_OFFLINE, collect=collect)


def test_lost_calls_collect_with_03_signature(work_repo, monkeypatch):
    seen: dict = {}
    _fake_lost(monkeypatch, seen)
    got = payload(call("lost", {"roots": [str(work_repo.parent)]}))
    assert got["command"] == "lost" and got["ok"] is True
    assert seen["roots"] == [work_repo.parent.resolve()] and seen["source"] == "args"
    assert seen["kinds"] == set(LOST_OFFLINE) and seen["stale_days"] == 180 and seen["cfg"] is not None
    payload(call("lost", {"roots": [str(work_repo.parent)], "only": ["stash", "dirty"]}))
    assert seen["kinds"] == {"stash", "dirty"}


def test_lost_default_and_config_roots(home, cfg, monkeypatch):
    seen: dict = {}
    _fake_lost(monkeypatch, seen)
    payload(call("lost"))
    assert seen["source"] == "default" and seen["roots"] == [home] and seen["cfg"] is None
    (home / "code").mkdir()
    cfg('[default]\nname = "D"\nemail = "d@x.test"\n\n[scan]\nroots = ["~/code"]\nexclude = ["**/vendor"]\n'
        'stale_days = 30\n', apply=False)
    payload(call("lost"))
    assert seen["source"] == "config" and seen["roots"] == [home / "code"]
    assert seen["exclude"] == ["**/vendor"] and seen["stale_days"] == 30


def test_lost_uses_scan_effective_roots(home, monkeypatch, tmp_path):
    from gitpro import scan
    seen: dict = {}
    _fake_lost(monkeypatch, seen)
    asked = []

    def effective_roots(args_roots, cfg_scan):
        asked.append(args_roots)
        return [tmp_path], ["x/**"], "args"
    monkeypatch.setattr(scan, "effective_roots", effective_roots, raising=False)
    payload(call("lost", {"roots": [str(tmp_path)]}))
    assert asked == [[str(tmp_path.resolve())]]
    assert seen["roots"] == [tmp_path] and seen["exclude"] == ["x/**"] and seen["source"] == "args"


@pytest.mark.parametrize("style", ["positional", "keyword_only"])
def test_audit_calls_collect_with_08_signature(work_repo, monkeypatch, style):
    seen = {}

    def check(repo, cfg, rng, all_):
        assert isinstance(repo, Path) and repo.is_absolute() and cfg is not None
        assert rng in (None, "HEAD~1..HEAD") and isinstance(all_, bool)
        seen.update(repo=repo, range=rng, all=all_)
        return {"commits": [], "agent_commits": []}
    if style == "positional":
        def collect(repo, cfg, range, all):     # 08 §11: collect(repo, cfg, range, all) -> dict
            return check(repo, cfg, range, all)
    else:
        def collect(repo, cfg, *, range=None, all=False):
            return check(repo, cfg, range, all)
    _fake_module(monkeypatch, "audit", collect=collect)
    got = payload(call("audit", {"path": str(work_repo), "range": "HEAD~1..HEAD", "all": True}))
    assert got["command"] == "audit" and got["ok"] is True
    assert seen == {"repo": work_repo.resolve(), "range": "HEAD~1..HEAD", "all": True}
    payload(call("audit", {"path": str(work_repo)}))
    assert seen["range"] is None and seen["all"] is False


def test_check_commit_calls_trailers_with_08_signature(work_repo, monkeypatch):
    seen = {}

    def policy_for(identity, default):
        seen["identity"] = identity.id
        return "forbid"

    def check(trailers, policy):
        seen["trailers"], seen["policy"] = trailers, policy
        return [f"{k}: {v} is not allowed" for k, v in trailers if k.lower() == "co-authored-by"]
    _fake_module(monkeypatch, "trailers", policy_for=policy_for, check=check)
    msg = "fix\n\nCo-Authored-By: Bot <bot@x.test>\n"
    got = payload(call("check_commit", {"path": str(work_repo), "committer_email": "me@corp.test",
                                        "author_email": "me@corp.test", "message": msg}))
    assert seen["identity"] == "work-github" and seen["policy"] == "forbid"
    assert ("Co-Authored-By", "Bot <bot@x.test>") in [tuple(t) for t in seen["trailers"]]
    assert [p["code"] for p in got["problems"]] == ["trailer"] and got["ok"] is False


# ---------------------------------------------------------------- QA fixes: credentials in remote urls

SECRET_URL = "https://x-access-token:ghp_SECRET123@github.com/corp-org/app.git"
TOKEN_URL = "https://ghp_TOKEN456@github.com/corp-org/other.git"
SECRETS = ("ghp_SECRET123", "ghp_TOKEN456")


def test_redact_url():
    from gitpro.resolve import redact_url
    assert redact_url(SECRET_URL) == "https://x-access-token:***@github.com/corp-org/app.git"
    assert redact_url(TOKEN_URL) == "https://***@github.com/corp-org/other.git"
    assert redact_url("https://u:p@ss@host/x") == "https://u:***@host/x"
    for same in ("git@github.com:corp-org/app.git", "ssh://git@github.com/corp-org/app.git",
                 "https://github.com/corp-org/app.git", "/srv/repo.git", "", None):
        assert redact_url(same) == same


# The same cases are in vscode/src/test/exec.test.ts (redactUrl).
QUERY_CASES = [
    ("https://h/o/r.git?access_token=abc&x=1#frag", "https://h/o/r.git?access_token=***&x=1#frag"),
    ("https://gitlab.com/o/r.git?private_token=glpat-1&ref=main", "https://gitlab.com/o/r.git?private_token=***&ref=main"),
    ("https://h/a?x=1&Token=t1;sig=s2", "https://h/a?x=1&Token=***;sig=***"),
    ("https://h/a?X-Amz-Signature=ab&X-Amz-Credential=cd&a=b", "https://h/a?X-Amz-Signature=***&X-Amz-Credential=***&a=b"),
    ("https://u:p@h/a?auth=z", "https://u:***@h/a?auth=***"),
    ("https://h/a?password=&key=v", "https://h/a?password=***&key=***"),
    ("https://h/a?tokenish=1&mykey=2&ref=token", "https://h/a?tokenish=1&mykey=2&ref=token"),
]


@pytest.mark.parametrize("url,shown", QUERY_CASES)
def test_redact_url_masks_secret_query_values(url, shown):
    from gitpro.resolve import redact_url
    assert redact_url(url) == shown


def test_redact_url_secret_keys_match_the_extension():
    import re
    from pathlib import Path
    from gitpro.resolve import SECRET_QUERY_KEYS
    ts = (Path(__file__).parent.parent / "vscode" / "src" / "exec.ts").read_text()
    keys = re.search(r"const SECRET_KEYS = \[(.*?)\];", ts, re.S)
    assert keys and tuple(re.findall(r'"([^"]+)"', keys[1])) == SECRET_QUERY_KEYS


@pytest.fixture
def secret_repo(work_repo):
    git(work_repo, "remote", "set-url", "origin", SECRET_URL)
    git(work_repo, "remote", "add", "fork", TOKEN_URL)
    return work_repo


def test_cli_never_shows_url_credentials(secret_repo, capsys):
    for argv in (("why", "--json", str(secret_repo)), ("why", str(secret_repo)),
                 ("which", "--json", str(secret_repo)), ("which", str(secret_repo))):
        capsys.readouterr()
        run(*argv)
        cap = capsys.readouterr()
        for s in SECRETS:
            assert s not in cap.out + cap.err, argv
    data = cli_json(capsys, "why", "--json", str(secret_repo))
    assert data["remotes"] == {"origin": "https://x-access-token:***@github.com/corp-org/app.git",
                               "fork": "https://***@github.com/corp-org/other.git"}
    # matching still uses the real url: userinfo makes the work pattern miss, and the fix says so
    assert any(p["code"] == "url_form" and "x-access-token:***@" in p["message"] for p in data["problems"])


@pytest.mark.parametrize("tool", ["which_identity", "why", "check_commit"])
def test_mcp_never_returns_url_credentials(secret_repo, tool):
    out = rpc(init(), call_msg(tool, {"path": str(secret_repo), "committer_email": "me@personal.test"}
                               if tool == "check_commit" else {"path": str(secret_repo)}))
    text = json.dumps(out)
    for s in SECRETS:
        assert s not in text
    assert out[1]["result"]["isError"] is False
    if tool != "check_commit":       # check_commit's problems name no url (the backstop covers it anyway)
        assert "***@github.com" in text


def test_mcp_redaction_backstop(home, monkeypatch):
    def leaky(args, ctx):
        return {"ok": True, "command": "identities", "nested": [{"msg": f"origin is {SECRET_URL}; bad"}],
                TOKEN_URL: "key", "ssh": "git@github.com:corp-org/app.git"}
    monkeypatch.setattr(mcp.TOOLS["identities"], "fn", leaky)
    out = rpc(init(), call_msg("identities"))
    text = json.dumps(out)
    assert not any(s in text for s in SECRETS)
    got = payload(out[1]["result"])
    assert got["nested"][0]["msg"] == "origin is https://x-access-token:***@github.com/corp-org/app.git; bad"
    assert got["ssh"] == "git@github.com:corp-org/app.git"


# ---------------------------------------------------------------- QA fixes: one request never kills the server

PING = {"jsonrpc": "2.0", "id": 99, "method": "ping"}


def test_deep_nesting_is_a_parse_error():
    out = rpc("[" * 100000 + "]" * 100000, '{"a":' * 100000 + "1" + "}" * 100000, PING)
    assert [m.get("error", {}).get("code") for m in out] == [-32700, -32700, None]


def test_bad_id_and_jsonrpc_are_invalid_requests():
    out = rpc({"jsonrpc": "2.0", "id": None, "method": "ping"}, {"id": 1, "method": "ping"},
              {"jsonrpc": "1.0", "id": 2, "method": "ping"}, {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
              {"method": "notifications/initialized"}, PING)
    assert [(m["id"], m.get("error", {}).get("code")) for m in out] == [
        (None, -32600), (1, -32600), (2, -32600), (None, -32600), (99, None)]


def test_validation_grammar():
    out = rpc(init(), call_msg("scan", {"roots": "/"}), call_msg("identities", None, id=2))
    assert out[1]["error"]["message"] == "'roots' must be an array"


@pytest.mark.parametrize("exc", [SystemExit(3), KeyboardInterrupt()])
def test_tool_exit_is_a_tool_error(home, monkeypatch, exc):
    def stop(args, ctx):
        raise exc
    monkeypatch.setattr(mcp.TOOLS["identities"], "fn", stop)
    out = rpc(init(), call_msg("identities"), PING)
    assert out[1]["result"]["isError"] is True and "internal error" in text_of(out[1]["result"])
    assert out[2]["result"] == {}


@pytest.mark.parametrize("value", [{"ok": True, "p": Path("/x")}, ["not", "an", "object"], {"n": float("nan")}])
def test_bad_tool_result_is_a_tool_error(home, monkeypatch, capsys, value):
    monkeypatch.setattr(mcp.TOOLS["identities"], "fn", lambda args, ctx: value)
    out = rpc(init(), call_msg("identities"), PING)
    assert out[1]["result"]["isError"] is True and "internal error" in text_of(out[1]["result"])
    assert out[2]["result"] == {}


def test_unexpected_handler_error_is_internal_error(monkeypatch, capsys):
    def broken(self, method, params):
        raise ZeroDivisionError
    monkeypatch.setattr(mcp.Server, "dispatch", broken)
    out = rpc(PING, PING)
    assert [m["error"]["code"] for m in out] == [-32603, -32603]


def test_log_write_failure_is_ignored(capsys):
    class Full:
        def write(self, text):
            raise OSError(28, "No space left on device")

        def flush(self):
            pass
    out = io.StringIO()
    server = mcp.Server(out, None, Full())
    for m in (init(), PING):
        server.send(server.handle(json.dumps(m)))
    assert [m["id"] for m in lines(out.getvalue())] == [0, 99]
    assert capsys.readouterr().err.count("--log stopped") == 1


def _read_line(proc, timeout=TIMEOUT) -> dict:
    import select
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    assert ready, "no response: the server is stuck"
    return json.loads(proc.stdout.readline())


def test_child_process_cannot_touch_the_protocol(home, tmp_path):
    """A tool whose child process writes to stdout and reads stdin (and that writes to fd 1 itself): the
    responses stay clean and the child never steals a request."""
    driver = (
        "import os, subprocess, sys\n"
        "from gitpro import mcp\n"
        "def noisy(args, ctx):\n"
        "    os.write(1, b'raw-fd-junk\\n')\n"
        "    r = subprocess.run(['sh', '-c', 'echo child-junk; read x; echo \"child-read:$x\"'])\n"
        "    return {'ok': True, 'command': 'identities', 'rc': r.returncode}\n"
        "mcp.TOOLS['identities'].fn = noisy\n"
        "from gitpro.cli import main\n"
        "sys.exit(main(['mcp']))\n")
    env = dict(os.environ, PYTHONPATH=str(Path(mcp.__file__).resolve().parent.parent))
    proc = subprocess.Popen([sys.executable, "-c", driver], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, cwd=str(tmp_path))
    try:
        got = []
        for m in (init(), call_msg("identities"), PING, call_msg("identities", id=3), PING):
            proc.stdin.write((json.dumps(m) + "\n").encode())
            proc.stdin.flush()     # one request at a time: a child reading the protocol would block here
            got.append(_read_line(proc))
        proc.stdin.close()
        assert proc.wait(TIMEOUT) == 0
        err = proc.stderr.read().decode()
    finally:
        if proc.poll() is None:
            proc.kill()
    assert [m["id"] for m in got] == [0, 1, 99, 3, 99]
    assert payload(got[1]["result"])["rc"] == 0
    assert "raw-fd-junk" in err and "child-junk" in err and "child-read:\n" in err


# ---------------------------------------------------------------- QA fixes: install

def test_install_refuses_foreign_entry_without_force(home, capsys):
    other = {"command": "npx", "args": ["some-other-gitpro"]}
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"gitpro": other}}))
    assert run("mcp", "install", "-y") == 2
    err = capsys.readouterr().err
    assert "not gitpro's own" in err and "--force" in err
    assert claude_json(home)["mcpServers"]["gitpro"] == other
    assert run("mcp", "install", "-y", "--json") == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert run("mcp", "install", "-y", "--force") == 0
    assert claude_json(home)["mcpServers"]["gitpro"] == ENTRY


def test_install_cli_fallback_rechecks_foreign_entry(home, fake_bin, capsys):
    # the CLI fails ("already exists") after something else registered a 'gitpro' server
    fake_bin.add("claude", """
import json, pathlib
p = pathlib.Path(env["HOME"]) / ".claude.json"
p.write_text(json.dumps({"mcpServers": {"gitpro": {"command": "other"}}}))
print("MCP server gitpro already exists in user config", file=sys.stderr)
sys.exit(1)
""")
    assert run("mcp", "install", "-y") == 2
    assert "not gitpro's own" in capsys.readouterr().err
    assert claude_json(home)["mcpServers"]["gitpro"] == {"command": "other"}


def test_install_cli_fallback_confirms_once_and_warns(home, fake_bin, capsys, monkeypatch):
    from gitpro import view
    fake_bin.add("claude", "sys.exit(1)")
    asked = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(view, "confirm", lambda q, *a, **k: asked.append(q) or True)
    assert run("mcp", "install", "--write") == 0
    cap = capsys.readouterr()
    assert len(asked) == 1
    assert "close Claude Code first" in cap.err
    assert claude_json(home)["mcpServers"]["gitpro"] == ENTRY


def test_install_undo_note_says_whole_file(home, capsys):
    (home / ".claude.json").write_text("{}\n")
    assert run("mcp", "install", "-y") == 0
    cap = capsys.readouterr()
    assert "restores the whole file" in cap.out and "Claude Code" in cap.out
    assert "close Claude Code first" in cap.err


def test_uninstall_project_drops_empty_servers(home, tmp_path, monkeypatch, capsys):
    repo = repo_with_remote(tmp_path, "team", WORK_URL)
    monkeypatch.chdir(repo)
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"gitpro": ENTRY}, "other": 1}))
    assert run("mcp", "uninstall", "--scope", "project", "--write", "-y") == 0
    assert json.loads((repo / ".mcp.json").read_text()) == {"other": 1}
