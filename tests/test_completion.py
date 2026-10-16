"""`gitpro completion` (spec 16 §2.3, tests 18-21): scripts generated from the argparse tree."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import types

import pytest

from conftest import TIMEOUT, run
from gitpro import cli, completion

ZSH = shutil.which("zsh")
FISH = shutil.which("fish")


def subcommand_names(parser: argparse.ArgumentParser, path=()):
    """Every (path, name) of parser's subcommands, recursively."""
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            for name, sp in a.choices.items():
                if getattr(sp, "gitpro_hidden", False):    # hidden and deprecated (spec 22 §4, §10.3)
                    continue
                yield (*path, name)
                yield from subcommand_names(sp, (*path, name))


def script(capsys, shell: str) -> str:
    assert run("completion", shell) == 0
    return capsys.readouterr().out


def stub_parser() -> argparse.ArgumentParser:
    """A parser shaped like the later packages' commands: nested subcommands, choices, --identity, <hook>."""
    p = argparse.ArgumentParser(prog="gitpro")
    sub = p.add_subparsers(dest="command")
    g = sub.add_parser("guard", help="commit guard")
    gsub = g.add_subparsers(dest="action")
    inst = gsub.add_parser("install", help="install the hooks")
    inst.add_argument("--shell", choices=["bash", "zsh"])
    inst.add_argument("--identity", metavar="ID")
    inst.add_argument("--global", dest="glob", action="store_true")
    run_ = gsub.add_parser("run", help="run one hook")
    run_.add_argument("hook")
    a = sub.add_parser("agents", help="agent hooks")
    a.add_argument("--agent", choices=["claude", "codex", "copilot"])
    a.add_argument("--format", choices=["text", "json"])
    a.add_argument("--out", metavar="PATH")
    t = sub.add_parser("tag")
    t.add_argument("--color").gitpro_values = "shells"
    return p


def bash_complete(text: str, words, tmp_path, path_env=None) -> list:
    """Source a bash script, complete `words` (the last one is the word under the cursor), return COMPREPLY."""
    f = tmp_path / "gitpro.bash"
    f.write_text(text)
    words = list(words)
    arr = " ".join("'" + w.replace("'", "'\\''") + "'" for w in words)
    code = (f"source {f}\nCOMP_WORDS=({arr})\nCOMP_CWORD={len(words) - 1}\n"
            "_gitpro\nprintf '%s\\n' \"${COMPREPLY[@]}\"\n")
    env = dict(os.environ, PATH=path_env) if path_env else None
    r = subprocess.run(["bash", "--norc", "--noprofile", "-c", code], capture_output=True, text=True,
                       timeout=TIMEOUT, env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    return [w for w in r.stdout.splitlines() if w]


# ---------------------------------------------------------------- 18: every subcommand, valid syntax

def test_bash_has_every_subcommand_and_parses(home, capsys, tmp_path):  # 18
    text = script(capsys, "bash")
    parser, _ = cli.build_parser()
    names = list(subcommand_names(parser))
    assert {"completion", "prompt", "setup", "why"} <= {n[-1] for n in names}
    assert not {"doctor", "apply", "plan", "which", "lost", "agent-check"} & {n[0] for n in names}
    for path in names:
        assert path[-1] in text
    f = tmp_path / "c.bash"
    f.write_text(text)
    r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True, timeout=TIMEOUT)
    assert r.returncode == 0, r.stderr


def test_bash_completes_subcommands(home, capsys, tmp_path):  # 18 (the task: loads and completes)
    text = script(capsys, "bash")
    parser, _ = cli.build_parser()
    top = {n[0] for n in subcommand_names(parser)}
    assert set(bash_complete(text, ["gitpro", ""], tmp_path)) == top
    assert bash_complete(text, ["gitpro", "wh"], tmp_path) == ["why"]
    assert set(bash_complete(text, ["gitpro", "p"], tmp_path)) == {"pin", "policy", "prompt"}
    assert "--max-ms" in bash_complete(text, ["gitpro", "prompt", "--"], tmp_path)
    assert "--lost" in bash_complete(text, ["gitpro", "scan", "--"], tmp_path)
    assert bash_complete(text, ["gitpro", "completion", "--values", "h"], tmp_path) == ["hooks"]


def test_extension_commands_appear(home, capsys, monkeypatch):  # 18: commands added by later packages
    def register(sub, common, confirm):
        sp = sub.add_parser("zzlater", parents=[common], help="added later")
        sp.add_argument("--flavour", choices=["mint", "lime"])

    def extend(subs):
        cli.extend_command(subs, "scan", lambda p: p.add_argument("--zzextra", action="store_true"),
                           lambda args: None)

    fake = types.ModuleType("gitpro.zzlater")
    fake.register, fake.extend = register, extend
    monkeypatch.setitem(sys.modules, "gitpro.zzlater", fake)
    monkeypatch.setattr(cli, "EXTENSIONS", [*cli.EXTENSIONS, "gitpro.zzlater"])
    for shell in ("bash", "zsh", "fish"):
        text = script(capsys, shell)
        assert "zzlater" in text and "zzextra" in text
        assert "mint lime" in text


def test_zsh_starts_with_compdef_and_parses(home, capsys, tmp_path):  # 18
    text = script(capsys, "zsh")
    assert text.startswith("#compdef gitpro\n")
    if not ZSH:
        pytest.skip("zsh not installed")
    f = tmp_path / "_gitpro"
    f.write_text(text)
    r = subprocess.run([ZSH, "-n", str(f)], capture_output=True, text=True, timeout=TIMEOUT)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not ZSH, reason="zsh not installed")
def test_zsh_completes_with_stubbed_compsys(tmp_path):
    f = tmp_path / "_gitpro"
    f.write_text(completion.generate(stub_parser(), "zsh"))

    def complete(*words):
        code = (f"compadd() {{ [[ $1 == -- ]] && shift; print -l -- \"$@\"; }}\n_files() {{ print FILES; }}\n"
                f"source {f}\nwords=({' '.join(repr(w) for w in words)})\nCURRENT={len(words)}\n_gitpro\n")
        r = subprocess.run([ZSH, "-f", "-c", code], capture_output=True, text=True, timeout=TIMEOUT)
        assert r.returncode == 0, r.stderr
        return r.stdout.split()

    assert set(complete("gitpro", "")) == {"guard", "agents", "tag"}
    assert set(complete("gitpro", "guard", "")) == {"install", "run"}
    assert set(complete("gitpro", "agents", "--agent", "")) == {"claude", "codex", "copilot"}
    assert complete("gitpro", "agents", "--out", "") == ["FILES"]
    assert "--shell" in complete("gitpro", "guard", "install", "-")


def test_fish_has_complete_lines(home, capsys, tmp_path):  # 18
    text = script(capsys, "fish")
    assert "complete -c gitpro" in text
    assert "-a setup" in text and "-l lost" in text and "-a doctor" not in text
    if FISH:
        f = tmp_path / "gitpro.fish"
        f.write_text(text)
        r = subprocess.run([FISH, "-n", str(f)], capture_output=True, text=True, timeout=TIMEOUT)
        assert r.returncode == 0, r.stderr


def test_install_line_on_stderr(home, capsys):
    assert run("completion", "bash") == 0
    err = capsys.readouterr().err
    assert '# add to ~/.bashrc: eval "$(gitpro completion bash)"' in err


# ---------------------------------------------------------------- 19: choices and dynamic values

def test_choices_values_in_bash():  # 19
    text = completion.generate(stub_parser(), "bash")
    assert "'bash zsh'" in text                     # --shell
    assert "'claude codex copilot'" in text         # --agent
    assert "'text json'" in text                    # --format
    assert "completion --values identities" in text  # --identity
    assert "completion --values hooks" in text       # <hook>
    assert "completion --values shells" in text      # gitpro_values attribute


def test_bash_completes_values_nested(tmp_path, fake_bin):  # 19-20: dynamic values call gitpro at completion time
    fake_bin.add("gitpro", """
if argv[:2] == ["completion", "--values"]:
    print({"identities": "work-github\\npersonal", "hooks": "pre-commit\\npre-push\\ncommit-msg"}.get(argv[2], ""))
""")
    text = completion.generate(stub_parser(), "bash")
    path = os.environ["PATH"]
    assert set(bash_complete(text, ["gitpro", "guard", ""], tmp_path, path)) == {"install", "run"}
    assert set(bash_complete(text, ["gitpro", "guard", "install", "--shell", ""], tmp_path, path)) == {"bash", "zsh"}
    assert bash_complete(text, ["gitpro", "guard", "install", "--identity", "w"], tmp_path, path) == ["work-github"]
    # an option that takes a value is skipped when walking to the subcommand
    assert set(bash_complete(text, ["gitpro", "guard", "run", "pre-"], tmp_path, path)) == {"pre-commit", "pre-push"}
    assert set(bash_complete(text, ["gitpro", "agents", "--agent", "codex", "--format", ""], tmp_path, path)) \
        == {"text", "json"}
    (tmp_path / "notes.txt").write_text("")
    assert bash_complete(text, ["gitpro", "agents", "--out", "not"], tmp_path, path) == ["notes.txt"]


def test_unsafe_words_are_dropped():
    p = argparse.ArgumentParser(prog="gitpro")
    p.add_argument("--mode", choices=["ok", "$(touch x)", "a b"])
    text = completion.generate(p, "bash")
    assert "touch" not in text and "a b" not in text
    assert "'ok'" in text


def test_values_identities(home, cfg, capsys):  # 20
    cfg(apply=False)
    assert run("completion", "--values", "identities") == 0
    assert capsys.readouterr().out.splitlines() == ["work-github", "personal"]


def test_values_no_config_is_empty(home, capsys):  # 20
    assert run("completion", "--values", "identities") == 0
    assert capsys.readouterr().out == ""


def test_values_hooks_and_shells(home, capsys):  # 20
    assert run("completion", "--values", "hooks") == 0
    assert capsys.readouterr().out.split() == ["pre-commit", "pre-push", "commit-msg"]
    assert completion.values("shells") == ["bash", "zsh", "fish"]


def test_values_kinds(home, capsys, monkeypatch):  # 20
    monkeypatch.setitem(sys.modules, "gitpro.lost", None)   # 03 absent
    assert completion.values("kinds") == []
    lost = types.ModuleType("gitpro.lost")
    lost.KINDS = ("stash", "branch")
    monkeypatch.setitem(sys.modules, "gitpro.lost", lost)
    assert run("completion", "--values", "kinds") == 0
    assert capsys.readouterr().out.split() == ["stash", "branch"]


# ---------------------------------------------------------------- 21: errors

def test_unknown_shell_exits_2(home, capsys):  # 21
    assert run("completion", "tcsh") == 2
    assert "unknown shell 'tcsh' (bash, zsh, fish)" in capsys.readouterr().err


def test_no_shell_exits_2(home, capsys):
    assert run("completion") == 2
    assert "completion bash|zsh|fish" in capsys.readouterr().err


def test_generate_rejects_unknown_shell():
    with pytest.raises(ValueError):
        completion.generate(argparse.ArgumentParser(), "tcsh")


def test_capability_registered(home, capsys):
    from gitpro import args
    cli.build_parser()
    assert "completion" in args.CAPABILITIES


# ---------------------------------------------------------------- QA (review of 16): real bugs, expected to fail until fixed

def test_qa_dynamic_values_are_filtered(home, capsys, monkeypatch):
    """bash's compgen -W expands $(...) in its word list, so --values must drop unsafe words like static choices."""
    lost = types.ModuleType("gitpro.lost")
    lost.KINDS = ("stash", "$(touch PWNED)", "`id`")
    monkeypatch.setitem(sys.modules, "gitpro.lost", lost)
    assert run("completion", "--values", "kinds") == 0
    assert capsys.readouterr().out.split() == ["stash"]


# ---------------------------------------------------------------- review fixes

def test_env_only_and_shell_values(home, capsys):  # R-067
    envcmd = pytest.importorskip("gitpro.envcmd")
    text = script(capsys, "bash")
    assert f"'{' '.join(envcmd.KEYS)}'" in text
    assert "'sh fish pwsh bash zsh'" in text
    stub = argparse.ArgumentParser(prog="gitpro")
    sp = stub.add_subparsers().add_parser("env")
    sp.add_argument("--only")
    sp.add_argument("--shell")
    sp.add_argument("--other").gitpro_choices = ["x", "$(bad)"]
    node = completion.tree(stub).subs["env"]
    vals = {o.flags[0]: o.values for o in node.opts}
    assert vals["--only"] == list(envcmd.KEYS) and vals["--other"] == ["x"]


def test_unsafe_identity_ids_are_dropped(home, cfg, capsys):
    cfg(apply=False)
    assert completion.values("identities", None) == ["work-github", "personal"]
    assert completion._safe(["ok", "a b", "$(x)", "`id`", "-flag"]) == ["ok"]
