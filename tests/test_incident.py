"""19 report, part 2: incident records (the last crash, the last denials) and the `internal error` exit.

A record is written where the failure happens and holds only fixed fields: no message, no locals, no argv,
no path outside the gitpro package. The planted values come from test_report's world.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import TIMEOUT, run, write_config
from gitpro import cli, undo
from test_report import CANARY_EMAIL, CONFIG, assert_clean, build_world, record_world, world  # noqa: F401

NO_INCIDENT_YET = pytest.mark.xfail(strict=True, reason="incident records land in step 2")

TWO_LINES = ["✗ internal error (KeyError): this is a gitpro bug", "→ draft a report with: gitpro report --crash"]


def crash_file() -> Path:
    from gitpro import incident
    return incident.state_dir() / "last-crash.json"


def denial_file() -> Path:
    from gitpro import incident
    return incident.state_dir() / "denials.jsonl"


def boom_in_cli(monkeypatch, world, exc=None):
    def boom(args):
        canary_local = CANARY_EMAIL      # noqa: F841 - a local that must never reach a record
        raise exc if exc is not None else KeyError(f"{CANARY_EMAIL} {world.home}")
    monkeypatch.setattr(cli, "cmd_check", boom)


def agent_payload(world, cmd="git commit -m x"):
    return {"session_id": "s1", "cwd": str(world.repo), "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": cmd, "description": "d"}}


def agent_check(world, monkeypatch, capsys):
    from gitpro import agents
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(agent_payload(world))))
    rc = agents.main(["agent-check"])
    out, err = capsys.readouterr()
    return rc, out, err


# ---------------------------------------------------------------- the crash record

@NO_INCIDENT_YET
def test_cli_crash_is_recorded_without_the_message(world, monkeypatch, capsys):
    boom_in_cli(monkeypatch, world)
    assert run("check") == 2
    capsys.readouterr()
    path = crash_file()
    assert path.parent == undo.state_dir() and path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600 and (path.parent.stat().st_mode & 0o777) == 0o700
    text = path.read_text()
    assert_clean(text, world, "last-crash.json")
    rec = json.loads(text)
    assert set(rec) == {"time", "version", "command", "type", "frames", "outside"}
    assert rec["command"] == "check" and rec["type"] == "KeyError"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\dZ", rec["time"])
    assert 1 <= len(rec["frames"]) <= 30 and rec["outside"] >= 1
    for frame in rec["frames"]:
        assert len(frame) == 3 and re.fullmatch(r"gitpro/[A-Za-z0-9_/]+\.py", frame[0])
        assert isinstance(frame[1], int) and re.fullmatch(r"[A-Za-z_<][\w<>]*", frame[2])
    from gitpro import __version__
    assert rec["version"] == __version__


@NO_INCIDENT_YET
def test_a_second_crash_overwrites_the_first(world, monkeypatch, capsys):
    boom_in_cli(monkeypatch, world, KeyError("one"))
    run("check")
    boom_in_cli(monkeypatch, world, ValueError("two"))
    run("check")
    capsys.readouterr()
    assert json.loads(crash_file().read_text())["type"] == "ValueError"


@NO_INCIDENT_YET
def test_an_odd_exception_class_name_is_stored_as_exception(world, monkeypatch, capsys):
    odd = type("Odd name; rm -rf", (Exception,), {})
    boom_in_cli(monkeypatch, world, odd("x"))
    run("check")
    capsys.readouterr()
    assert json.loads(crash_file().read_text())["type"] == "Exception"


# ---------------------------------------------------------------- 10.6 the exit

@NO_INCIDENT_YET
def test_unexpected_exception_exits_two_with_two_lines(world, monkeypatch, capsys):
    boom_in_cli(monkeypatch, world)
    assert run("check") == 2
    out, err = capsys.readouterr()
    assert out == "" and err.splitlines() == TWO_LINES
    assert "Traceback" not in err
    assert_clean(out + err, world, "output")


@NO_INCIDENT_YET
def test_debug_env_prints_the_traceback(world, monkeypatch, capsys):
    monkeypatch.setenv("GITPRO_DEBUG", "1")
    boom_in_cli(monkeypatch, world)
    assert run("check") == 2
    err = capsys.readouterr().err
    assert err.splitlines()[:2] == TWO_LINES or all(line in err for line in TWO_LINES)
    assert "Traceback" in err


@NO_INCIDENT_YET
def test_unexpected_exception_under_json(world, monkeypatch, capsys):
    boom_in_cli(monkeypatch, world)
    assert run("check", "--json") == 2
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert data["ok"] is False and data["command"] == "check" and data["error"] == "internal error (KeyError)"
    assert "Traceback" not in err
    assert_clean(out + err, world, "output")


def test_keyboard_interrupt_still_exits_130(world, monkeypatch, capsys):
    boom_in_cli(monkeypatch, world, KeyboardInterrupt())
    assert run("check") == 130
    assert "internal error" not in capsys.readouterr().err


def test_existing_errors_keep_their_exit_codes(world, monkeypatch, capsys):
    from gitpro.config import ConfigError
    boom_in_cli(monkeypatch, world, ConfigError("bad thing"))
    assert run("check") == 2
    assert capsys.readouterr().err == "✗ bad thing\n"
    boom_in_cli(monkeypatch, world, subprocess.CalledProcessError(3, ["git", "x"]))
    assert run("check") == 2
    assert capsys.readouterr().err == "✗ git x failed (3)\n"
    boom_in_cli(monkeypatch, world, OSError("disk gone"))
    assert run("check") == 2
    assert capsys.readouterr().err == "✗ disk gone\n"


# ---------------------------------------------------------------- the other crash writers

@NO_INCIDENT_YET
def test_agent_check_crash_is_recorded_and_still_fails_open(world, monkeypatch, capsys):
    from gitpro import resolve
    monkeypatch.setattr(resolve, "compare", lambda *a, **k: (_ for _ in ()).throw(KeyError(CANARY_EMAIL)))
    rc, out, err = agent_check(world, monkeypatch, capsys)
    assert rc == 0 and "deny" not in out
    rec = json.loads(crash_file().read_text())
    assert rec["command"] == "agent-check" and rec["type"] == "KeyError"
    assert_clean(crash_file().read_text(), world, "last-crash.json")


@NO_INCIDENT_YET
def test_guard_hook_crash_is_recorded_and_still_warns(world, monkeypatch, capsys):
    from gitpro import guard, resolve
    monkeypatch.chdir(world.repo)
    monkeypatch.setattr(resolve, "compare", lambda *a, **k: (_ for _ in ()).throw(KeyError(CANARY_EMAIL)))
    assert guard.check("pre-commit", []) == 0
    assert "not blocking" in capsys.readouterr().err
    rec = json.loads(crash_file().read_text())
    assert rec["command"].startswith("guard") and rec["type"] == "KeyError"
    assert_clean(crash_file().read_text(), world, "last-crash.json")


@NO_INCIDENT_YET
def test_mcp_tool_crash_is_recorded_and_the_traceback_stays(world, monkeypatch, capsys):
    from gitpro import mcp
    from test_mcp import call_msg, init, rpc

    def broken(args, ctx):
        raise KeyError(f"{CANARY_EMAIL} {world.home}")
    monkeypatch.setattr(mcp.TOOLS["identities"], "fn", broken)
    out = rpc(init(), call_msg("identities"))
    assert out[1]["result"]["isError"] is True
    assert "Traceback" in capsys.readouterr().err
    rec = json.loads(crash_file().read_text())
    assert rec["command"] == "mcp identities" and rec["type"] == "KeyError"
    assert_clean(crash_file().read_text(), world, "last-crash.json")


# ---------------------------------------------------------------- the denial record

@NO_INCIDENT_YET
def test_agent_denial_is_recorded(world, monkeypatch, capsys):
    rc, out, err = agent_check(world, monkeypatch, capsys)
    assert "deny" in out
    path = denial_file()
    assert (path.stat().st_mode & 0o777) == 0o600 and (path.parent.stat().st_mode & 0o777) == 0o700
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert set(rec) == {"time", "version", "source", "agent", "action", "reason", "codes"}
    assert rec["source"] == "agent-check" and rec["agent"] == "claude"
    assert rec["reason"] in {"committer", "author", "no_identity", "pushed_to", "problem", "push"}
    assert "override" in rec["codes"] and rec["action"] == "commit"
    assert_clean(path.read_text(), world, "denials.jsonl")


@NO_INCIDENT_YET
def test_guard_denial_is_recorded(world, monkeypatch, capsys):
    from gitpro import guard
    monkeypatch.chdir(world.repo)
    assert guard.check("pre-commit", []) == 3
    capsys.readouterr()
    rec = json.loads(denial_file().read_text().splitlines()[-1])
    assert rec["source"] == "guard" and rec["agent"] is None and rec["action"] == "pre-commit"
    assert rec["reason"] in {"committer", "author"}
    assert_clean(denial_file().read_text(), world, "denials.jsonl")


@NO_INCIDENT_YET
def test_warn_mode_records_nothing(world, monkeypatch, capsys):
    from gitpro import guard
    write_config(world.home, CONFIG + '\n[agents]\nhook = "warn"\n\n[guard]\non_mismatch = "warn"\n')
    rc, out, err = agent_check(world, monkeypatch, capsys)
    assert rc == 0
    monkeypatch.chdir(world.repo)
    assert guard.check("pre-commit", []) == 0
    capsys.readouterr()
    assert not denial_file().exists()


@NO_INCIDENT_YET
def test_denials_file_keeps_the_last_twenty(world):
    from gitpro import incident
    for n in range(25):
        incident.record_denial("agent-check", "claude", f"act{n}", "problem", ("override",))
    lines = denial_file().read_text().splitlines()
    assert len(lines) == 20
    assert json.loads(lines[0])["action"] == "act5" and json.loads(lines[-1])["action"] == "act24"
    assert (denial_file().stat().st_mode & 0o777) == 0o600


@NO_INCIDENT_YET
def test_an_unwritable_state_dir_changes_nothing_for_the_hooks(world, monkeypatch, capsys, tmp_path):
    from gitpro import guard
    first = agent_check(world, monkeypatch, capsys)
    monkeypatch.chdir(world.repo)
    g1 = guard.check("pre-commit", [])
    g1_err = capsys.readouterr().err
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    second = agent_check(world, monkeypatch, capsys)
    monkeypatch.chdir(world.repo)
    g2 = guard.check("pre-commit", [])
    g2_err = capsys.readouterr().err
    assert first == second and g1 == g2 == 3 and g1_err == g2_err
    boom_in_cli(monkeypatch, world)
    assert run("check") == 2
    assert capsys.readouterr().err.splitlines() == TWO_LINES


@NO_INCIDENT_YET
def test_state_dir_is_the_undo_state_dir(world, monkeypatch, tmp_path):
    from gitpro import incident
    assert incident.state_dir() == undo.state_dir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert incident.state_dir() == undo.state_dir() == tmp_path / "xdg" / "gitpro"


# ---------------------------------------------------------------- the hot path stays light

def test_agent_check_import_stays_light_with_incident(world):
    r = subprocess.run([sys.executable, "-X", "importtime", "-m", "gitpro", "agent-check"], input="",
                       capture_output=True, text=True, timeout=TIMEOUT, cwd=world.home)
    mods = {line.split("|")[-1].strip() for line in r.stderr.splitlines() if "|" in line}
    assert not {m for m in mods if m == "rich" or m.startswith("rich.")}
    assert "gitpro.report" not in mods and "gitpro.cli" not in mods
