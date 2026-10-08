"""Dispatch, hot path, --version/--json, which/check --json, import cost, Python 3.9 rules."""
import ast
import json
import re
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from conftest import TIMEOUT, git, repo_with_remote, run
import gitpro
from gitpro import args as gargs
from gitpro import cli, hot

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "gitpro"


# ---------------------------------------------------------------- which / check (22, 23)

def test_which_not_a_repo(home, tmp_path, capsys):  # 22
    assert run("which", str(tmp_path)) == 2
    cap = capsys.readouterr()
    assert f"✗ {tmp_path}: not a git repository" in cap.err and cap.out == ""
    assert run("which", "--json", str(tmp_path)) == 2
    cap = capsys.readouterr()
    assert json.loads(cap.out)["ok"] is False and cap.err == ""


def test_which_json(cfg, tmp_path, capsys, home):  # 22
    cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    git(repo, "config", "url.git@github-x:.insteadOf", "git@github.com:")
    capsys.readouterr()
    assert run("which", "--json", str(repo)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["command"] == "which" and data["repo"] == str(repo)
    assert data["remotes"] == {"origin": "git@github.com:corp-org/app.git"}  # raw, before insteadOf
    assert list(data["settings"]) == ["user.name", "user.email", "user.signingkey", "commit.gpgsign",
                                      "core.sshcommand"]
    assert data["settings"]["user.email"] == {"value": "me@corp.test", "origin": f"file:{home}/.gitconfig-work-github",
                                              "scope": "generated"}
    assert data["settings"]["commit.gpgsign"] == {"value": None, "origin": None, "scope": None}
    assert "id_work" in data["settings"]["core.sshcommand"]["value"]


def test_which_text(cfg, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    capsys.readouterr()
    assert run("which", str(repo)) == 0
    out = capsys.readouterr().out
    assert "remote.origin.url git@github.com:corp-org/app.git" in out
    assert "user.email       me@corp.test   [file:~/.gitconfig-work-github]" in out
    assert "core.sshcommand" in out and "core.sshCommand" not in out


def test_check_json(cfg, home, capsys):  # 23
    cfg()
    capsys.readouterr()
    assert run("check", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "command": "check", "changes": [], "up_to_date": True}
    (home / ".gitconfig-personal").write_text("tampered\n")
    assert run("check", "--json") == 1
    data = json.loads(capsys.readouterr().out)
    assert data["up_to_date"] is False and data["ok"] is True
    assert data["changes"] == [{"action": "update", "path": str(home / ".gitconfig-personal"), "mode": None}]


# ---------------------------------------------------------------- version (24, 28)

def test_version(capsys):  # 24
    assert run("--version") == 0
    assert capsys.readouterr().out == f"gitpro {gitpro.__version__}\n"
    assert gitpro.__version__ == "0.2.0"
    assert run("--version", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["command"] == "version" and data["version"] == "0.2.0"
    assert data["python"] == ".".join(map(str, sys.version_info[:3]))
    assert {"why", "why.config", "which.json", "check.json"} <= set(data["capabilities"])
    assert data["capabilities"] == sorted(data["capabilities"])


def test_versions_agree():  # 28
    # Python uses PEP 440 (0.2.0.dev0); the JSON manifests need semver, which VS Code enforces (0.2.0-dev.0).
    v = gitpro.__version__.replace(".dev", "-dev.")
    assert re.fullmatch(r"\d+\.\d+\.\d+(-dev\.\d+)?", v)
    assert json.loads((ROOT / "vscode/package.json").read_text())["version"] == v
    assert json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"] == v
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    assert [p["version"] for p in market["plugins"]] == [v]


# ---------------------------------------------------------------- errors as JSON (25)

def test_json_error_on_stdout(home, tmp_path, capsys, monkeypatch):  # 25
    monkeypatch.chdir(repo_with_remote(tmp_path, "app", None))
    assert cli.main(["why", "--json"]) == 2
    cap = capsys.readouterr()
    data = json.loads(cap.out)
    assert data["ok"] is False and data["command"] == "why" and "no config found" in data["error"]
    assert cap.err == ""
    assert cli.main(["why"]) == 2
    assert "✗ no config found" in capsys.readouterr().err


def test_root_refusal_is_json(home, capsys, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert cli.main(["which", "--json"]) == 2
    assert "not root" in json.loads(capsys.readouterr().out)["error"]


# ---------------------------------------------------------------- extension dispatch (26)

def _module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def test_extensions_register_and_extend(home, capsys, monkeypatch):  # 26
    seen = []

    def register(sub, common, confirm):
        sp = sub.add_parser("hello", parents=[common], help="say hello")
        sp.add_argument("--loud", action="store_true")
        sp.set_defaults(func=lambda args: seen.append(("hello", args.loud)) or 0)
        cli.capability("hello", "hello.loud")

    def extend(subs):
        cli.extend_command(subs, "keys", lambda p: p.add_argument("--extra", action="store_true"),
                           lambda args: 7 if args.extra else None)

    _module(monkeypatch, "gitpro_test_ext", register=register, extend=extend)
    monkeypatch.setattr(cli, "EXTENSIONS", ["gitpro_test_ext"])
    p, subs = cli.build_parser()
    assert "hello" in p.format_help() and "hello" in subs
    assert cli.main(["hello", "--loud"]) == 0 and seen == [("hello", True)]
    assert cli.main(["keys", "--extra"]) == 7
    capsys.readouterr()
    # The handler returned None: the original cmd_keys runs (and fails: no config).
    assert cli.main(["keys"]) == 2
    assert "no config found" in capsys.readouterr().err
    cli.main(["--version", "--json"])
    assert "hello.loud" in json.loads(capsys.readouterr().out)["capabilities"]


def test_missing_extension_is_skipped_but_typos_are_not(monkeypatch, tmp_path):  # 26
    monkeypatch.setattr(cli, "EXTENSIONS", ["gitpro.not_built_yet"])
    cli.build_parser()
    (tmp_path / "gitpro_test_broken.py").write_text("import gitpro.also_missing\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(cli, "EXTENSIONS", ["gitpro_test_broken"])
    try:
        with pytest.raises(ModuleNotFoundError):
            cli.build_parser()
    finally:
        sys.modules.pop("gitpro_test_broken", None)


def test_extend_only_module(home, monkeypatch, capsys):  # V-005: scanroots has extend, no register
    def extend(subs):
        cli.extend_command(subs, "scan", lambda p: p.add_argument("--roots-test", action="store_true"),
                           lambda args: 5 if args.roots_test else None)
        cli.capability("scan.roots-test")
    _module(monkeypatch, "gitpro_test_extonly", extend=extend)
    monkeypatch.setattr(cli, "EXTENSIONS", ["gitpro_test_extonly"])
    assert cli.main(["scan", "--roots-test"]) == 5


def test_args_module_is_shared(monkeypatch):  # V-006
    assert cli.capability is gargs.capability and cli.CAPABILITIES is gargs.CAPABILITIES
    p = __import__("argparse").ArgumentParser(parents=[gargs.common(), gargs.confirm()])
    ns = p.parse_args(["-c", "/x.toml", "-y"])
    assert ns.config == Path("/x.toml") and ns.yes is True and ns.home == Path.home()


def test_guarded_error_path(capsys):  # V-006: one error helper for cli and hot modules
    from gitpro import config
    import argparse

    def boom(args):
        raise config.ConfigError("bad thing")
    assert gargs.guarded(boom, argparse.Namespace(command="guard", json=True)) == 2
    cap = capsys.readouterr()
    assert json.loads(cap.out) == {"ok": False, "command": "guard", "error": "bad thing"} and cap.err == ""
    assert gargs.guarded(boom, argparse.Namespace(command="guard")) == 2
    assert capsys.readouterr().err == "✗ bad thing\n"

    def cpe(args):
        raise subprocess.CalledProcessError(3, ["git", "a b"])
    assert gargs.guarded(cpe, argparse.Namespace()) == 2
    assert "✗ git 'a b' failed (3)" in capsys.readouterr().err


def test_caller_umask(home, capsys):  # V-007
    old = os.umask(0o022)
    try:
        cli.main(["which", str(home)])
        assert cli.CALLER_UMASK == 0o022 and os.umask(0o077) == 0o077
    finally:
        os.umask(old)


def test_caller_umask_helper(monkeypatch, capsys):  # QA01-5
    from gitpro import hot
    monkeypatch.setattr(cli, "CALLER_UMASK", None)
    monkeypatch.setattr(hot, "CALLER_UMASK", None)
    old = os.umask(0o027)
    try:
        assert cli.caller_umask() == 0o027
        assert os.umask(0o027) == 0o027            # reading it did not change it
        monkeypatch.setattr(hot, "CALLER_UMASK", 0o007)
        assert cli.caller_umask() == 0o007
        monkeypatch.setattr(cli, "CALLER_UMASK", 0o002)
        assert cli.caller_umask() == 0o002
    finally:
        os.umask(old)
    with pytest.raises(SystemExit):
        cli.build_parser()[0].parse_args(["-h"])
    out = capsys.readouterr().out
    assert "gitpro scan" in out and "for developers" not in out and "CALLER_UMASK" not in out


def test_version_json_dest_is_distinct(home, tmp_path, capsys):  # V-017
    p, _ = cli.build_parser()
    ns = p.parse_args(["which", "--json", "."])
    assert ns.json is True and ns.version_json is False


def test_module_without_register_is_ignored(monkeypatch):
    _module(monkeypatch, "gitpro_test_plain")
    monkeypatch.setattr(cli, "EXTENSIONS", ["gitpro_test_plain"])
    cli.build_parser()


def test_extensions_list():
    assert cli.EXTENSIONS == sorted(cli.EXTENSIONS) and "gitpro.resolve" in cli.EXTENSIONS
    assert len(cli.EXTENSIONS) == 18 and "gitpro.setup" in cli.EXTENSIONS


# ---------------------------------------------------------------- hot path (27)

def test_hot_falls_through_without_module(home, capsys, monkeypatch):  # 27
    monkeypatch.setitem(hot.HOT, "guard", "gitpro.not_built_guard")
    monkeypatch.setattr(cli, "EXTENSIONS", [m for m in cli.EXTENSIONS if m != "gitpro.guard"])  # 06 is built
    with pytest.raises(SystemExit) as e:
        hot.main(["guard", "check"])
    assert e.value.code == 2 and "invalid choice" in capsys.readouterr().err


def test_hot_calls_stub_without_cli(monkeypatch):  # 27
    got = []
    _module(monkeypatch, "gitpro_test_guard", main=lambda argv: got.append(argv) or 3)
    monkeypatch.setitem(hot.HOT, "guard", "gitpro_test_guard")
    code = ("import sys, types\n"
            "m = types.ModuleType('gitpro_test_guard'); m.main = lambda argv: print('stub', argv) or 3\n"
            "sys.modules['gitpro_test_guard'] = m\n"
            "from gitpro import hot\n"
            "hot.HOT['guard'] = 'gitpro_test_guard'\n"
            "rc = hot.main(['guard', 'check'])\n"
            "print('cli' in sys.modules.get('gitpro').__dict__, 'gitpro.cli' in sys.modules, 'rich' in sys.modules)\n"
            "sys.exit(rc)\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=TIMEOUT)
    assert r.returncode == 3, r.stderr
    assert r.stdout.splitlines() == ["stub ['guard', 'check']", "False False False"]
    assert hot.main(["guard", "x"]) == 3 and got == [["guard", "x"]]


def test_hot_does_not_hide_broken_module(monkeypatch, tmp_path):  # V-016
    (tmp_path / "gitpro_test_hotbroken.py").write_text("import gitpro.also_missing\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(hot.HOT, "guard", "gitpro_test_hotbroken")
    try:
        with pytest.raises(ModuleNotFoundError):
            hot.main(["guard", "check"])
    finally:
        sys.modules.pop("gitpro_test_hotbroken", None)


def test_hot_default_table():
    assert hot.HOT == {"guard": "gitpro.guard", "agent-check": "gitpro.agents", "prompt": "gitpro.prompt"}


def test_main_module_uses_hot():
    assert "hot" in (SRC / "__main__.py").read_text()
    assert 'gitpro = "gitpro.hot:main"' in (ROOT / "pyproject.toml").read_text()


# ---------------------------------------------------------------- import cost (29, 30)

def _imported(code: str) -> set:
    # sys.modules, not -X importtime: importtime does not log importlib.import_module() calls.
    code += "\nimport sys; print('\\n'.join(sys.modules))"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=TIMEOUT)
    assert r.returncode == 0, r.stderr
    return set(r.stdout.split())


def test_cli_imports_lazily():  # 29
    mods = _imported("import gitpro.cli")
    assert "gitpro.cli" in mods
    assert not {"gitpro.resolve", "gitpro.scan", "gitpro.plan"} & mods
    # V-013: build the parser so every built EXTENSIONS module is imported and registered.
    mods = _imported("import gitpro.cli; gitpro.cli.build_parser()")
    assert "gitpro.resolve" in mods
    assert not {"gitpro.scan", "gitpro.plan", "gitpro.render", "gitpro.view", "rich"} & mods


@pytest.mark.parametrize("name", ["config", "resolve", "hot", "jsonout", "undo", "files", "plan", "render",
                                  "jsonfile", "forge", "args"])
def test_core_modules_never_import_rich(name):  # 29
    mods = _imported(f"import gitpro.{name}")
    assert "rich" not in mods and "gitpro.cli" not in mods and "gitpro.view" not in mods


def test_version_is_fast():  # 30 (informational bound)
    import time
    t = time.monotonic()
    r = subprocess.run([sys.executable, "-m", "gitpro", "--version"], capture_output=True, text=True,
                       timeout=TIMEOUT, cwd=ROOT)
    assert r.returncode == 0 and r.stdout.startswith("gitpro ")
    assert time.monotonic() - t <= 2.0


# ---------------------------------------------------------------- Python 3.9 rules (31)

def _violations(tree: ast.AST) -> list:
    bad = []
    for node in ast.walk(tree):
        if type(node).__name__ == "Match":
            bad.append(f"match statement at line {node.lineno}")
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("isinstance", "issubclass") \
                and len(node.args) == 2 and isinstance(node.args[1], ast.BinOp) \
                and isinstance(node.args[1].op, ast.BitOr):
            bad.append(f"X | Y in {node.func.id} at line {node.lineno}")
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "zip" \
                and any(k.arg == "strict" for k in node.keywords):
            bad.append(f"zip(strict=) at line {node.lineno}")
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dataclass" \
                and any(k.arg in ("slots", "kw_only") for k in node.keywords):
            bad.append(f"dataclass(slots/kw_only) at line {node.lineno}")
        if isinstance(node, ast.Attribute) and node.attr == "UTC" and getattr(node.value, "id", "") == "datetime":
            bad.append(f"datetime.UTC at line {node.lineno}")
    return bad


def test_python39_rules():  # 31
    problems = {}
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text()
        tree = ast.parse(text)
        bad = _violations(tree)
        if path.name != "__main__.py" and "from __future__ import annotations" not in text:
            bad.append("missing 'from __future__ import annotations'")
        if bad:
            problems[path.name] = bad
    assert problems == {}


def test_rule_checker_catches_forbidden_forms():
    src = "isinstance(x, int | str)\nzip(a, b, strict=True)\n"
    assert len(_violations(ast.parse(src))) == 2


# ---------------------------------------------------------------- scan --json envelope (36)

def test_scan_json_envelope(cfg, tmp_path, capsys):  # 36
    cfg()
    root = tmp_path / "src"
    stale = repo_with_remote(root, "stale", "git@github.com:someone/app.git")
    git(stale, "config", "user.email", "old@personal.test")
    capsys.readouterr()
    assert run("scan", "--json", str(root)) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] is True and rep["command"] == "scan" and rep["repos"] == 1
    assert run("scan", "--fix", "--json", "--repo", str(stale)) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["ok"] is True and done["command"] == "scan"
    assert done["removed"] == [{"path": str(stale), "key": "user.email", "value": "old@personal.test"}]
    text = Path(done["undo"]).read_text()
    assert text.startswith("#!/bin/sh\n") and f"git -C {stale} config user.email old@personal.test" in text
    assert "set -e" not in text
    assert run("scan", "--fix", "--json", "--repo", str(stale)) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "command": "scan", "removed": [], "undo": None}
