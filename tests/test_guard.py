"""06 guard: global chained git hooks (pre-commit / pre-push identity guard), against real git in a temp HOME."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG, TIMEOUT, git, repo_with_remote, run, set_git_hooks_sandbox, slowdown
from gitpro import guard, plan, render

WORK = "git@github.com:corp-org/app.git"
REAL_GITPRO = """
import io
sys.stdin = io.TextIOWrapper(io.BytesIO(stdin))
sys.exit(__import__("gitpro.hot").hot.main(argv))
"""
STUBS = ["applypatch-msg", "pre-applypatch", "post-applypatch", "prepare-commit-msg", "post-commit",
         "pre-merge-commit", "pre-rebase", "post-checkout", "post-merge", "pre-receive", "update",
         "proc-receive", "post-receive", "post-update", "pre-auto-gc", "post-rewrite",
         "sendemail-validate"]


# ---------------------------------------------------------------- helpers

def sh(cmd, cwd=None, env=None, stdin=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, timeout=TIMEOUT)


def gcommit(repo: Path, msg: str = "change", *extra: str, **env: str) -> subprocess.CompletedProcess:
    e = dict(os.environ, **env)
    return subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "--no-gpg-sign", "-m", msg, *extra],
                          capture_output=True, text=True, env=e, timeout=TIMEOUT)


def gitc(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=dict(os.environ, **env), timeout=TIMEOUT)


def no_gitpro_path() -> str:
    """PATH without any directory that holds a gitpro (uv and the venv put theirs on PATH)."""
    return os.pathsep.join(d for d in os.environ["PATH"].split(os.pathsep)
                           if d and not os.access(os.path.join(d, "gitpro"), os.X_OK))


@pytest.fixture
def g(home, cfg, fake_bin, monkeypatch, tmp_path):
    """Sandboxed git, a real gitpro on PATH (the fake runs gitpro.hot.main in-process), config applied."""
    set_git_hooks_sandbox(monkeypatch, home)
    fake_bin.add("gitpro", REAL_GITPRO)
    cfg()
    return home


@pytest.fixture
def installed(g, capsys):
    assert run("guard", "install", "-y") == 0
    capsys.readouterr()
    return g


@pytest.fixture
def work(installed, tmp_path, fake_bin):
    """Loopback SSH preserves the destination URL Git passes to pre-push; insteadOf would replace it."""
    bare = tmp_path / "remotes" / "app.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=TIMEOUT)
    routes = {"corp-org/app.git": str(bare), "me/app.git": str(bare.with_name("personal.git")),
              "someone/app.git": str(bare.with_name("default.git"))}
    ssh = fake_bin.path / "ssh"
    ssh.write_text(f"#!{sys.executable}\n"
                   "import os, shlex, sys\n"
                   "args = shlex.split(sys.argv[-1])\n"
                   f"routes = {routes!r}\n"
                   "if len(args) != 2 or args[0] not in ('git-upload-pack', 'git-receive-pack') "
                   "or args[1] not in routes:\n    sys.exit(1)\n"
                   f"os.execv({shutil.which('git')!r}, ['git', args[0][4:], routes[args[1]]])\n")
    ssh.chmod(0o755)
    repo = repo_with_remote(tmp_path, "app", WORK)
    git(repo, "checkout", "-q", "-b", "main")
    return repo


def hooks_dir(home: Path) -> Path:
    return home / ".config/gitpro/hooks"


def write_hook(repo: Path, name: str, body: str) -> Path:
    p = repo / ".git/hooks" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


# ---------------------------------------------------------------- 21: the HOOKS table

def test_hooks_table_matches_01():
    assert guard.HOOKS == {
        "pre-commit": ["gitpro.guard:identity_commit", "gitpro.policy:hook_commit"],
        "pre-push": ["gitpro.guard:identity_push", "gitpro.policy:hook_push"],
        "commit-msg": ["gitpro.trailers:hook_message", "gitpro.policy:hook_message"]}
    assert guard.BLOCK == 3
    for entries in guard.HOOKS.values():
        for spec in entries:
            mod = spec.split(":")[0]
            try:
                __import__(mod)
            except ModuleNotFoundError:
                continue
            assert callable(guard._entry(spec)), spec


def test_hook_names():
    assert render.HOOK_NAMES[:3] == ("pre-commit", "pre-push", "commit-msg")
    assert list(render.HOOK_NAMES[3:]) == STUBS and len(render.HOOK_NAMES) == 20
    for n in ("reference-transaction", "post-index-change", "fsmonitor-watchman", "p4-changelist"):
        assert n not in render.HOOK_NAMES


# ---------------------------------------------------------------- 1, 2: install

def test_install_global(g, tmp_path, capsys):
    assert run("guard", "install", "-y") == 0
    out = capsys.readouterr().out
    assert "guard installed" in out and "↩ undo: gitpro guard uninstall" in out and "'gitpro apply' keeps" in out
    d = hooks_dir(g)
    names = sorted(p.name for p in d.iterdir())
    assert names == sorted([*render.HOOK_NAMES, "README"])
    for n in render.HOOK_NAMES:
        text = (d / n).read_text()
        assert (d / n).stat().st_mode & 0o777 == 0o700
        assert text.splitlines()[0] == "#!/bin/sh" and text.splitlines()[1].startswith(render.MARKER)
        assert render.is_generated(text)
    assert render.is_generated((d / "README").read_text())
    gf = g / ".gitconfig-gitpro-guard"
    assert gf.stat().st_mode & 0o777 == 0o600 and f'hooksPath = "{d}"' in gf.read_text()
    assert 'path = "~/.gitconfig-gitpro-guard"' in (g / ".gitconfig-identities").read_text()
    repo = repo_with_remote(tmp_path, "any", WORK)
    assert git(repo, "config", "core.hooksPath") == str(d)
    assert run("guard", "status", "--json") == 0
    st = json.loads(capsys.readouterr().out)
    assert st["ok"] and st["command"] == "guard status" and st["installed"] == "global"
    assert all(st["hooks"][n] for n in render.HOOK_NAMES) and st["system_hooks_path"] is None
    assert guard.status()["installed"] == "global"
    # idempotent
    assert run("guard", "install", "-y") == 0
    assert "already installed" in capsys.readouterr().out


def test_install_without_tty_writes_nothing(g, capsys):
    assert run("guard", "install") == 0
    assert "Dry run" in capsys.readouterr().out
    assert not hooks_dir(g).exists() and not (g / ".gitconfig-gitpro-guard").exists()
    assert "gitpro-guard" not in (g / ".gitconfig-identities").read_text()
    assert run("guard", "install", "--json") == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_install_json_and_uninstall_json(g, capsys):
    assert run("guard", "install", "-y", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["command"] == "guard install" and len(out["changes"]) == 20 + 3
    assert Path(out["undo"]).is_file()
    assert run("guard", "uninstall", "-y", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and len(out["changes"]) == 20 + 3 and Path(out["undo"]).is_file()
    # the uninstall undo script puts everything back
    assert sh(["sh", out["undo"]]).returncode == 0
    assert (hooks_dir(g) / "pre-commit").is_file() and guard.status()["installed"] == "global"


def test_install_undo_script(g, capsys):
    before = (g / ".gitconfig-identities").read_text()
    assert run("guard", "install", "-y") == 0
    script = sorted((g / ".local/state/gitpro").glob("guard-undo-*.sh"))[-1]
    assert sh(["sh", str(script)]).returncode == 0
    assert (g / ".gitconfig-identities").read_text() == before
    assert not hooks_dir(g).exists() and not (g / ".gitconfig-gitpro-guard").exists()   # empty dir removed too
    assert guard.status()["installed"] == "none"


def test_install_refuses_before_apply(home, monkeypatch, cfg, capsys):
    set_git_hooks_sandbox(monkeypatch, home)
    cfg(apply=False)
    assert run("guard", "install", "-y") == 2
    assert "gitpro apply first" in capsys.readouterr().err
    assert not hooks_dir(home).exists()


def test_install_refuses_hand_written_file(g, capsys):
    d = hooks_dir(g)
    d.mkdir(parents=True)
    (d / "pre-commit").write_text("#!/bin/sh\necho mine\n")
    assert run("guard", "install", "-y") == 2
    assert "not written by gitpro" in capsys.readouterr().err
    assert (d / "pre-commit").read_text() == "#!/bin/sh\necho mine\n"


# ---------------------------------------------------------------- 3: plan / apply / uninstall

def test_guard_hooks_true_in_toml(g, cfg, capsys, tmp_path):
    cfg(DEFAULT_CONFIG + "\n[guard]\nhooks = true\n")
    assert (hooks_dir(g) / "pre-push").is_file() and (g / ".gitconfig-gitpro-guard").is_file()
    assert run("plan") == 0
    assert "up to date" in capsys.readouterr().out
    assert run("apply", "-y") == 0
    assert "up to date" in capsys.readouterr().out
    assert run("guard", "install", "-y") == 0
    assert "already installed" in capsys.readouterr().out
    assert run("uninstall", "-y") == 0
    assert not any(p.is_file() for p in hooks_dir(g).rglob("*")) if hooks_dir(g).exists() else True
    assert not (g / ".gitconfig-gitpro-guard").exists() and not (g / ".gitconfig-identities").exists()


def test_install_then_apply_keeps_hooks_and_uninstall_removes(installed, cfg, capsys):
    g = installed
    assert run("plan") == 0
    assert "up to date" in capsys.readouterr().out
    assert run("apply", "-y") == 0
    assert "up to date" in capsys.readouterr().out
    # changing the config keeps the include and the hooks
    cfg(DEFAULT_CONFIG.replace("Work Me", "Work Me Too"))
    assert (hooks_dir(g) / "pre-commit").is_file()
    assert "gitpro-guard" in (g / ".gitconfig-identities").read_text()
    assert run("plan") == 0
    assert "up to date" in capsys.readouterr().out
    assert run("guard", "uninstall", "-y") == 0
    out = capsys.readouterr().out
    assert "guard removed" in out and "↩ undo: sh" in out
    assert not hooks_dir(g).exists() and not (g / ".gitconfig-gitpro-guard").exists()
    assert "gitpro-guard" not in (g / ".gitconfig-identities").read_text()
    assert run("plan") == 0
    assert "up to date" in capsys.readouterr().out
    assert run("apply", "-y") == 0
    assert not hooks_dir(g).exists()
    assert run("guard", "uninstall", "-y") == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_gitpro_uninstall_removes_guard(installed, capsys):
    assert run("uninstall", "-y") == 0
    assert not any(hooks_dir(installed).glob("*")) and not (installed / ".gitconfig-gitpro-guard").exists()


def test_uninstall_warns_when_toml_wants_hooks(g, cfg, capsys):
    cfg(DEFAULT_CONFIG + "\n[guard]\nhooks = true\n")
    capsys.readouterr()
    assert run("guard", "uninstall", "-y") == 0
    assert "hooks = true" in capsys.readouterr().err


# ---------------------------------------------------------------- 4-8: pre-commit

def test_clean_commit_passes_silently(work):
    r = gcommit(work)
    assert r.returncode == 0 and "gitpro" not in r.stderr
    assert git(work, "log", "-1", "--format=%ce") == "me@corp.test"


def test_repo_override_blocked(work):
    git(work, "config", "user.email", "wrong@x")
    r = gcommit(work)
    assert r.returncode == 1
    assert "✗ gitpro: this commit would be committed as wrong@x" in r.stderr
    assert "identity work-github <me@corp.test>" in r.stderr
    assert "cause: user.email is set in the repo's .git/config" in r.stderr
    assert "fix:   git -C" in r.stderr and "config --unset user.email" in r.stderr
    assert "skip once: GITPRO_SKIP=1 git commit" in r.stderr
    assert gcommit(work, GITPRO_SKIP="1").returncode == 0
    assert gcommit(work, GITPRO_SKIP="0").returncode == 0
    assert gcommit(work, "x", "--no-verify").returncode == 0


def test_env_committer_and_author(work):
    r = gcommit(work, GIT_COMMITTER_EMAIL="wrong@x")
    assert r.returncode == 1 and "environment" in r.stderr
    assert gcommit(work, GIT_AUTHOR_EMAIL="wrong@x").returncode == 0
    r = gcommit(work, GIT_AUTHOR_EMAIL="me@personal.test")
    assert r.returncode == 1 and "author is your personal email" in r.stderr


def test_teammates_never_blocked(work):
    colleague = dict(GIT_AUTHOR_NAME="C", GIT_AUTHOR_EMAIL="c@other.test", GIT_COMMITTER_NAME="C",
                     GIT_COMMITTER_EMAIL="c@other.test", GITPRO_SKIP="1")
    assert gcommit(work, "base").returncode == 0
    assert gcommit(work, "pair", "--author=Colleague <c@other.test>").returncode == 0
    git(work, "checkout", "-q", "-b", "side")
    for n in (1, 2):
        (work / f"theirs{n}.txt").write_text(f"{n}\n")
        git(work, "add", f"theirs{n}.txt")
        assert gcommit(work, f"theirs {n}", **colleague).returncode == 0
    git(work, "checkout", "-q", "main")
    r = gitc(work, "cherry-pick", "side~1")
    assert r.returncode == 0, r.stderr
    assert git(work, "log", "-1", "--format=%ae %ce") == "c@other.test me@corp.test"
    assert gcommit(work, "amended", "--amend", "--no-edit").returncode == 0
    r = gitc(work, "merge", "--no-ff", "--no-edit", "side")
    assert r.returncode == 0, r.stderr
    # git am of a colleague's patch
    patch = gitc(work, "format-patch", "-1", "--stdout", "side").stdout
    git(work, "checkout", "-q", "-b", "am", "main~3")
    r = subprocess.run(["git", "-C", str(work), "am", "-q"], input=patch, capture_output=True, text=True,
                       timeout=TIMEOUT)
    assert r.returncode == 0, r.stderr
    assert git(work, "log", "-1", "--format=%ae") == "c@other.test"


def test_warn_mode(work, cfg):
    cfg(DEFAULT_CONFIG + '\n[guard]\non_mismatch = "warn"\n')
    git(work, "config", "user.email", "wrong@x")
    r = gcommit(work)
    assert r.returncode == 0
    assert "⚠ gitpro: this commit would be committed as wrong@x" in r.stderr and "not blocking" in r.stderr


def test_a_fork_is_refused_until_pinned(installed, cfg, tmp_path):
    # 0.3 let the later identity win for every remote and committed without a word; hooks installed by 0.3 enforce
    # the new rule on upgrade. The guide (Upgrading from 0.3) names three ways out; each is pinned here.
    repo = repo_with_remote(tmp_path, "fork", WORK)
    git(repo, "remote", "add", "upstream", "git@github.com:me/app.git")
    r = gcommit(repo)
    assert r.returncode == 1
    assert "the remotes belong to different identities (origin → work-github, upstream → personal)" in r.stderr
    assert "fix:   gitpro pin -y --remote origin" in r.stderr and "skip once: GITPRO_SKIP=1 git commit" in r.stderr
    assert gcommit(repo, GITPRO_SKIP="1").returncode == 0                       # skip once
    cfg(DEFAULT_CONFIG + '\n[guard]\non_mismatch = "warn"\n')                   # a warning instead
    r = gcommit(repo)
    assert r.returncode == 0 and "⚠ gitpro: the remotes belong to different identities" in r.stderr
    cfg(DEFAULT_CONFIG)
    assert gcommit(repo).returncode == 1                                        # block is the default again
    assert run("pin", "-y", "--remote", "origin", str(repo)) == 0               # a pin is the answer
    r = gcommit(repo)
    assert r.returncode == 0 and "gitpro" not in r.stderr
    assert git(repo, "log", "-1", "--format=%ce") == "me@corp.test"


# ---------------------------------------------------------------- 9: chaining

def test_repo_hooks_still_run(work, tmp_path):
    marker = tmp_path / "pre-commit.ran"
    write_hook(work, "pre-commit", f'echo ran > "{marker}"\n')
    assert gcommit(work).returncode == 0 and marker.exists()
    write_hook(work, "pre-commit", "exit 3\n")
    assert gcommit(work).returncode == 1
    # the script passes the repo hook's code through unchanged
    r = sh(["sh", str(hooks_dir(work.parent / "home") / "pre-commit")], cwd=work)
    assert r.returncode == 3
    (work / ".git/hooks/pre-commit").unlink()

    co = tmp_path / "post-checkout.ran"
    write_hook(work, "post-checkout", f'echo "$@" > "{co}"\n')
    git(work, "checkout", "-q", "-b", "x")
    assert co.exists() and len(co.read_text().split()) == 3

    write_hook(work, "prepare-commit-msg", 'echo "Added-By: hook" >> "$1"\n')
    assert gcommit(work, "with hook").returncode == 0
    assert "Added-By: hook" in git(work, "log", "-1", "--format=%B")

    pm = tmp_path / "pre-merge.ran"
    write_hook(work, "pre-merge-commit", f'echo ran > "{pm}"\n')
    git(work, "checkout", "-q", "-b", "y")
    assert gcommit(work, "on y").returncode == 0
    git(work, "checkout", "-q", "x")
    r = gitc(work, "merge", "--no-ff", "--no-edit", "y")
    assert r.returncode == 0, r.stderr
    assert pm.exists()


@pytest.mark.parametrize("hook", ["pre-commit", "pre-push"])
@pytest.mark.parametrize("flags", [[], ["-e"]])
@pytest.mark.parametrize("gitpro_rc, want", [(0, 0), (2, 0), (3, 1), (None, 0)])
def test_block_matrix(tmp_path, home, hook, flags, gitpro_rc, want):
    """9a: the repo-mode block and the global chain script, with sh and sh -e."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if gitpro_rc is not None:
        (bindir / "gitpro").write_text(f"#!/bin/sh\ncat >/dev/null\nexit {gitpro_rc}\n")
        (bindir / "gitpro").chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=f"{bindir}{os.pathsep}{no_gitpro_path()}")
    env.pop("GITPRO_SKIP", None)
    stdin = "refs/heads/main 1111111111111111111111111111111111111111 refs/heads/main " + "0" * 40 + "\n"
    count = 'n=$(wc -l | tr -d " "); echo "lines=$n" >&2\n' if hook == "pre-push" else ""
    variants = {
        "block": "#!/bin/sh\n" + render.guard_block(hook) + count + "exit 0\n",
        "block-last": "#!/bin/sh\n" + render.guard_block(hook),
    }
    for kind, text in variants.items():
        f = tmp_path / f"{kind}-{hook}"
        f.write_text(text)
        r = sh(["sh", *flags, str(f)], env=env, stdin=stdin if hook == "pre-push" else "")
        assert r.returncode == want, (kind, r.stderr)
        if hook == "pre-push" and kind == "block" and want == 0:
            assert "lines=1" in r.stderr
    # global chain script, in a repo whose own hook counts stdin lines
    repo = repo_with_remote(tmp_path, "r", None)
    write_hook(repo, hook, 'n=$(wc -l | tr -d " "); echo "repo-lines=$n" >&2\n')
    gdir = tmp_path / "ghooks"
    gdir.mkdir()
    script = gdir / hook
    script.write_text(render.hook_script(hook, None, ""))
    script.chmod(0o700)
    r = sh(["sh", *flags, str(script), "origin", "url"], cwd=repo, env=env, stdin=stdin if hook == "pre-push" else "")
    assert r.returncode == want, r.stderr
    if want == 0 and hook == "pre-push":
        assert "repo-lines=1" in r.stderr
    if gitpro_rc == 2:
        assert "⚠ gitpro guard: exited 2; not blocking" in r.stderr


def test_rendered_scripts_parse():
    shells = [s for s in ("sh", "dash", "bash", "busybox") if shutil.which(s)]
    for n in render.HOOK_NAMES:
        for timeout in ("", "timeout --foreground 30"):
            text = render.hook_script(n, "/x y/it's", "/opt/gitpro", timeout) if n in render.GUARD_HOOKS else \
                render.hook_stub(n, "/x y/it's")
            for shell in shells:
                argv = [shell, "sh", "-n"] if shell == "busybox" else [shell, "-n"]
                assert subprocess.run(argv, input=text, text=True, timeout=TIMEOUT).returncode == 0, (n, shell)
        assert "[ $? -eq 3 ] && exit 1" not in text
        assert render.is_generated(text)
    assert "chain='/x y/it'\"'\"'s'" in render.hook_stub("post-merge", "/x y/it's")
    assert "chain=''" in render.hook_stub("post-merge", None)
    assert "gitpro guard check" not in render.hook_stub("post-merge", None)


# ---------------------------------------------------------------- 10: chain_to, no recursion

def test_chain_to(g, cfg, tmp_path, capsys):
    old = tmp_path / "oldhooks"
    old.mkdir()
    order = tmp_path / "order"
    (old / "pre-commit").write_text(f'#!/bin/sh\necho chain >> "{order}"\n')
    (old / "pre-commit").chmod(0o755)
    cfg(DEFAULT_CONFIG + f'\n[guard]\nchain_to = "{old}"\n')
    assert run("guard", "install", "-y") == 0
    repo = repo_with_remote(tmp_path, "app", WORK)
    write_hook(repo, "pre-commit", f'echo repo >> "{order}"\n')
    assert gcommit(repo).returncode == 0
    assert order.read_text().split() == ["repo", "chain"]
    capsys.readouterr()
    assert run("guard", "status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["chain_to"] == str(old)


def test_chain_to_validation(g, cfg, tmp_path, capsys):
    path = cfg(apply=False)
    path.write_text(DEFAULT_CONFIG + f'\n[guard]\nchain_to = "{hooks_dir(g)}"\n')
    assert run("guard", "install", "-y") == 2
    assert "chain_to" in capsys.readouterr().err
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o777)
    path.write_text(DEFAULT_CONFIG + f'\n[guard]\nchain_to = "{loose}"\n')
    assert run("guard", "install", "-y") == 2
    assert "writable" in capsys.readouterr().err
    path.write_text(DEFAULT_CONFIG + f'\n[guard]\nchain_to = "{tmp_path / "missing"}"\n')
    assert run("guard", "install", "-y") == 2
    assert "not a directory" in capsys.readouterr().err
    assert not hooks_dir(g).exists()


def test_symlink_loop_does_not_recurse(installed, tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(hooks_dir(installed))
    # config validation refuses this chain_to, so write the scripts as a hand-edited install would
    for n in render.HOOK_NAMES:
        p = hooks_dir(installed) / n
        p.write_text(render.hook_script(n, str(loop), "") if n in render.GUARD_HOOKS else render.hook_stub(n, str(loop)))
    repo = repo_with_remote(tmp_path, "app", WORK)
    marker = tmp_path / "repo.ran"
    write_hook(repo, "pre-commit", f'echo x >> "{marker}"\n')
    # and a repo hook that is itself a symlink to gitpro's script
    (repo / ".git/hooks/post-commit").symlink_to(hooks_dir(installed) / "post-commit")
    t = time.monotonic()
    r = gcommit(repo)
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - t < 20 and marker.read_text() == "x\n"


def test_depth_counter_stops_loops(installed, tmp_path):
    repo = repo_with_remote(tmp_path, "app", WORK)
    r = sh(["sh", str(hooks_dir(installed) / "post-merge")], cwd=repo,
           env=dict(os.environ, GITPRO_HOOK_DEPTH="8"))
    assert r.returncode == 0 and "nested" in r.stderr


# ---------------------------------------------------------------- 11, 14: fail open

@pytest.mark.parametrize("body, rc", [
    ("sys.exit(1)", 1),
    ("print('✗ broken', file=sys.stderr); sys.exit(2)", 2),
    ("raise RuntimeError('boom')", 1),
    ("sys.exit(127)", 127),
])
def test_broken_gitpro_never_blocks(work, fake_bin, body, rc):
    fake_bin.add("gitpro", body)
    r = gcommit(work)
    assert r.returncode == 0, r.stderr
    assert f"⚠ gitpro guard: exited {rc}; not blocking" in r.stderr
    assert r.stderr.count("⚠ gitpro guard") == 1          # pre-commit warned; commit-msg stays quiet
    r = gcommit(work, "again")
    assert r.stderr.count("⚠ gitpro guard") == 1          # once per commit, every commit


def test_no_gitpro_anywhere(g, monkeypatch, tmp_path, fake_bin):
    monkeypatch.setattr(plan, "gitpro_bin", lambda: "")
    assert run("guard", "install", "-y") == 0
    (fake_bin.path / "gitpro").unlink()
    monkeypatch.setenv("PATH", no_gitpro_path())
    repo = repo_with_remote(tmp_path, "app", WORK)
    git(repo, "config", "user.email", "wrong@x")
    r = gcommit(repo)
    assert r.returncode == 0 and r.stderr == ""


def test_fallback_gitpro_bin_is_used(g, monkeypatch, tmp_path, fake_bin):
    alt = tmp_path / "alt"
    alt.mkdir()
    shutil.move(str(fake_bin.path / "gitpro"), str(alt / "gitpro"))
    monkeypatch.setattr(plan, "gitpro_bin", lambda: str(alt / "gitpro"))
    assert run("guard", "install", "-y") == 0
    monkeypatch.setenv("PATH", no_gitpro_path())
    repo = repo_with_remote(tmp_path, "app", WORK)
    git(repo, "config", "user.email", "wrong@x")
    assert gcommit(repo).returncode == 1


def test_missing_config_fails_open(work, g):
    (g / ".config/gitpro.toml").unlink()
    r = gcommit(work)
    assert r.returncode == 0
    assert "⚠ gitpro guard: no config found" in r.stderr and "not blocking" in r.stderr
    assert r.stderr.count("⚠ gitpro guard") == 1, r.stderr


def test_check_argv_errors_fail_open(capsys):
    from gitpro import hot
    assert hot.main(["guard", "check"]) == 0
    assert "not blocking" in capsys.readouterr().err
    assert hot.main(["guard", "check", "bogus-hook"]) == 0
    assert "not blocking" in capsys.readouterr().err


def test_check_crash_inside_entry_fails_open(work, monkeypatch, capsys):
    monkeypatch.chdir(work)
    monkeypatch.setattr(guard, "_load_cfg", lambda home: 1 / 0)
    from gitpro import resolve
    monkeypatch.setattr(resolve, "TIMEOUT", resolve.TIMEOUT)
    assert guard.check("pre-commit", []) == 0
    assert "⚠ gitpro guard: ZeroDivisionError" in capsys.readouterr().err


def test_entry_errors_warn(work, monkeypatch, capsys):
    from gitpro import resolve
    monkeypatch.setattr(resolve, "TIMEOUT", resolve.TIMEOUT)
    monkeypatch.chdir(work)

    def boom(ctx):
        raise ValueError("bad entry")
    monkeypatch.setitem(guard.HOOKS, "pre-commit", ["gitpro.guard:identity_commit", "gitpro.nope:x",
                                                    "tests_guard_fake:boom"])
    import types
    mod = types.ModuleType("tests_guard_fake")
    mod.boom = boom
    monkeypatch.setitem(sys.modules, "tests_guard_fake", mod)
    assert guard.check("pre-commit", []) == 0
    assert "⚠ gitpro tests_guard_fake:boom: bad entry; not blocking" in capsys.readouterr().err


# ---------------------------------------------------------------- 12: pre-push

def _wrong(repo: Path, msg: str, email: str = "me@personal.test") -> None:
    r = gcommit(repo, msg, GIT_AUTHOR_NAME="P", GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME="P",
                GIT_COMMITTER_EMAIL=email, GITPRO_SKIP="1")
    assert r.returncode == 0, r.stderr


def test_pre_push_blocks_own_wrong_commits(work, tmp_path):
    assert gcommit(work, "base").returncode == 0
    assert gitc(work, "push", "-q", "-u", "origin", "main").returncode == 0
    lines = tmp_path / "lines"
    write_hook(work, "pre-push", f'wc -l | tr -d " " > "{lines}"\n')
    _wrong(work, "one")
    _wrong(work, "two")
    r = gitc(work, "push", "origin", "main")
    assert r.returncode != 0
    assert "✗ gitpro: 2 commits to origin" in r.stderr and "committed as me@personal.test" in r.stderr
    assert "work-github <me@corp.test>" in r.stderr and " one" in r.stderr and " two" in r.stderr
    assert "git rebase -i" in r.stderr or "audit --rewrite" in r.stderr
    assert "--reset-author" not in r.stderr
    r = gitc(work, "rebase", "-q", "origin/main", "--exec", "git commit -q --amend --no-edit --allow-empty --reset-author")
    assert r.returncode == 0, r.stderr
    r = gitc(work, "push", "origin", "main")
    assert r.returncode == 0, r.stderr
    assert lines.read_text().strip() == "1"


def test_pre_push_ignores_commits_other_remotes_have(work, tmp_path):
    assert gcommit(work, "base").returncode == 0
    assert gitc(work, "push", "-q", "origin", "main").returncode == 0
    fork = tmp_path / "remotes" / "fork.git"
    subprocess.run(["git", "init", "-q", "--bare", str(fork)], check=True, timeout=TIMEOUT)
    git(work, "remote", "add", "fork", str(fork))
    git(work, "checkout", "-q", "-b", "feature")
    _wrong(work, "on the fork")
    assert gitc(work, "push", "-q", "fork", "feature", GITPRO_SKIP="1").returncode == 0
    r = gitc(work, "push", "origin", "feature")
    assert r.returncode == 0, r.stderr


def test_pre_push_by_url(work):
    assert gcommit(work, "base").returncode == 0
    r = gitc(work, "push", WORK, "main")
    assert r.returncode == 0, r.stderr
    _wrong(work, "wrong")
    r = gitc(work, "push", WORK, "main")
    assert r.returncode != 0 and f"to {WORK}" in r.stderr


@pytest.mark.parametrize("setting", ["pushurl", "insteadOf", "pushInsteadOf"])
@pytest.mark.parametrize("owner, email, target", [
    ("me", "me@personal.test", "personal.git"),
    ("someone", "me@default.test", "default.git"),
])
def test_pre_push_checks_actual_destination(work, tmp_path, setting, owner, email, target):
    destination = f"git@github.com:{owner}/app.git"
    bare = tmp_path / "remotes" / target
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=TIMEOUT)
    if setting == "pushurl":
        git(work, "config", "remote.origin.pushurl", destination)
    else:
        git(work, "config", f"url.{destination}.{setting}", WORK)
    assert gcommit(work, "work identity").returncode == 0
    r = gitc(work, "push", "origin", "main")
    assert r.returncode != 0 and "committed as me@corp.test" in r.stderr and email in r.stderr
    assert git(bare, "show-ref", "--heads") == ""
    r = gcommit(work, "destination identity", "--amend", "--reset-author",
                GIT_AUTHOR_NAME="Me", GIT_AUTHOR_EMAIL=email,
                GIT_COMMITTER_NAME="Me", GIT_COMMITTER_EMAIL=email, GITPRO_SKIP="1")
    assert r.returncode == 0, r.stderr
    r = gitc(work, "push", "origin", "main")
    assert r.returncode == 0, r.stderr
    assert git(bare, "rev-parse", "refs/heads/main") == git(work, "rev-parse", "HEAD")


def test_pre_push_loads_destination_identity_policy(work, cfg, installed, tmp_path):
    pol = installed / "personal-policy.toml"
    pol.write_text('[policy]\nname = "Personal"\nversion = 1\n'
                   '[[rule]]\nhost = "github.com"\nowners = ["me"]\n'
                   'allowed_email_domains = ["personal.test"]\n')
    cfg(DEFAULT_CONFIG.replace('id = "personal"', f'id = "personal"\npolicy = "{pol}"'))
    bare = tmp_path / "remotes" / "personal.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, timeout=TIMEOUT)
    git(work, "config", "remote.origin.pushurl", "git@github.com:me/app.git")
    _wrong(work, "colleague", "colleague@other.test")
    r = gitc(work, "push", "origin", "main")
    assert r.returncode != 0 and "this push breaks policy Personal" in r.stderr
    assert git(bare, "show-ref", "--heads") == ""


def test_pre_push_teammates_commits_pass(work):
    assert gcommit(work, "mine").returncode == 0
    _wrong(work, "pat 1", "pat@other.test")
    _wrong(work, "pat 2", "pat@other.test")
    r = gitc(work, "push", "origin", "main")
    assert r.returncode == 0, r.stderr
    _wrong(work, "own but wrong", "me@personal.test")
    r = gitc(work, "push", "origin", "main")
    assert r.returncode != 0 and "1 commit to origin" in r.stderr


def test_pre_push_remote_ahead(work, tmp_path):
    assert gcommit(work, "base").returncode == 0
    assert gitc(work, "push", "-q", "origin", "main").returncode == 0
    other = tmp_path / "other"
    bare = tmp_path / "remotes" / "app.git"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(bare), str(other)], check=True, timeout=TIMEOUT,
                   env=dict(os.environ, GITPRO_SKIP="1"))
    _wrong(other, "from elsewhere", "pat@other.test")
    assert gitc(other, "push", "-q", "origin", "HEAD:main", GITPRO_SKIP="1").returncode == 0
    assert gcommit(work, "mine").returncode == 0
    r = gitc(work, "push", "origin", "main")
    assert r.returncode != 0 and "gitpro" not in r.stderr and "rejected" in r.stderr
    r = gitc(work, "push", "--force", "origin", "main")
    assert r.returncode == 0 and "gitpro" not in r.stderr, r.stderr


def test_identity_push_unknown_remote_sha(work, monkeypatch, capsys):
    from gitpro import resolve
    monkeypatch.setattr(resolve, "TIMEOUT", resolve.TIMEOUT)
    monkeypatch.chdir(work)
    _wrong(work, "wrong")
    head = git(work, "rev-parse", "HEAD")
    stdin = f"refs/heads/main {head} refs/heads/main {'ab' * 20}\n"
    assert guard.check("pre-push", ["origin", WORK], stdin) == 3
    assert "1 commit to origin" in capsys.readouterr().err
    stdin = f"(delete) {'0' * 40} refs/heads/old {head}\n"
    assert guard.check("pre-push", ["origin", WORK], stdin) == 0


# ---------------------------------------------------------------- 13: git lfs

def test_lfs(work, fake_bin):
    fake_bin.add("git-lfs", "")
    assert gcommit(work, "base").returncode == 0

    def prepush_calls():
        return [c for c in fake_bin.calls("git-lfs") if c[:1] == ["pre-push"]]
    assert gitc(work, "push", "-q", "origin", "main").returncode == 0
    assert prepush_calls() == []
    git(work, "config", "filter.lfs.process", "git-lfs filter-process")
    assert gcommit(work, "two").returncode == 0
    assert gitc(work, "push", "-q", "origin", "main").returncode == 0
    calls = prepush_calls()
    assert len(calls) == 1 and calls[0][1] == "origin"
    assert b"refs/heads/main" in [e for e in fake_bin.entries("git-lfs") if e["argv"][0] == "pre-push"][0]["stdin"]
    write_hook(work, "pre-push", 'git lfs pre-push "$@"\n')
    assert gcommit(work, "three").returncode == 0
    assert gitc(work, "push", "-q", "origin", "main").returncode == 0
    assert len(prepush_calls()) == 2
    (work / ".git/hooks/pre-push").unlink()
    git(work, "config", "--unset", "filter.lfs.process")
    subprocess.run(["git", "config", "--global", "filter.lfs.process", "git-lfs filter-process"], check=True,
                   timeout=TIMEOUT)
    assert gcommit(work, "four").returncode == 0
    assert gitc(work, "push", "-q", "origin", "main").returncode == 0
    assert len(prepush_calls()) == 3


# ---------------------------------------------------------------- 15: someone else's core.hooksPath

def test_foreign_hooks_path(g, cfg, tmp_path, capsys):
    other = tmp_path / "elsewhere"
    other.mkdir()
    subprocess.run(["git", "config", "--global", "core.hooksPath", str(other)], check=True, timeout=TIMEOUT)
    assert run("guard", "status") == 0
    assert "not gitpro" in capsys.readouterr().out
    assert run("guard", "install", "-y") == 2
    err = capsys.readouterr().err
    assert "core.hooksPath is already" in err and "global" in err
    assert not hooks_dir(g).exists()
    cfg(DEFAULT_CONFIG + f'\n[guard]\nchain_to = "{other}"\n')
    assert run("guard", "install", "-y") == 0
    repo = repo_with_remote(tmp_path, "app", WORK)
    assert git(repo, "config", "core.hooksPath") == str(hooks_dir(g))


def test_toml_hooks_refuse_foreign_hooks_path(g, cfg, tmp_path, capsys):
    other = tmp_path / "elsewhere"
    other.mkdir()
    subprocess.run(["git", "config", "--global", "core.hooksPath", str(other)], check=True, timeout=TIMEOUT)
    cfg(DEFAULT_CONFIG + "\n[guard]\nhooks = true\n", apply=False)
    assert run("apply", "-y") == 2
    assert "core.hooksPath is already" in capsys.readouterr().err
    assert not hooks_dir(g).exists()


def test_system_hooks_path(g, monkeypatch, tmp_path, capsys):
    sysconf = tmp_path / "sys"
    sysconf.write_text("[core]\n\thooksPath = /x\n")
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(sysconf))
    assert run("guard", "status") == 0
    assert "⚠ system gitconfig sets core.hooksPath = /x" in capsys.readouterr().err
    assert run("guard", "status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["system_hooks_path"] == "/x"
    assert run("guard", "install", "-y") == 2
    assert "system" in capsys.readouterr().err


def test_status_never_raises(home, monkeypatch):
    monkeypatch.setattr(guard, "_config_entries", lambda h: 1 / 0)
    st = guard.status()
    assert st["installed"] == "none" and "error" in st
    for k in ("hooks_dir", "on_mismatch", "chain_to", "gitpro_bin", "hooks", "system_hooks_path"):
        assert k in st


def test_status_text(installed, capsys):
    assert run("guard", "status") == 0
    out = capsys.readouterr().out
    assert "guard: installed (global, " in out and "on_mismatch = block" in out
    assert "17 pass-through hooks ✓" in out and "chain_to: (none)" in out
    assert "system core.hooksPath: (none)" in out


def test_status_not_installed(g, capsys):
    assert run("guard", "status") == 0
    assert "guard: not installed → gitpro guard install" in capsys.readouterr().out


# ---------------------------------------------------------------- 16, 17: repo mode

HUSKY_HOOK = "#!/usr/bin/env sh\necho husky-ran\n"


def _husky_repo(base: Path) -> Path:
    repo = repo_with_remote(base, "husky", WORK)
    (repo / ".husky/_").mkdir(parents=True)
    (repo / ".husky/_/pre-commit").write_text("#!/bin/sh\n. husky\n")
    (repo / ".husky/pre-commit").write_text(HUSKY_HOOK)
    (repo / ".husky/pre-commit").chmod(0o755)
    git(repo, "config", "core.hooksPath", ".husky")
    return repo


def test_repo_mode_husky(g, capsys):
    repo = _husky_repo(g)
    assert run("guard", "status", "--repos") == 0
    out = capsys.readouterr().out
    assert "global guard bypassed" in out and ".husky" in out and "guard install --repo" in out
    assert run("guard", "status", "--repos", "--json") == 0
    st = json.loads(capsys.readouterr().out)
    assert st["bypassed_repos"] == [{"repo": str(repo), "hooks_path": ".husky", "repo_mode": False}]
    before = git(repo, "status", "--porcelain")
    underscore = (repo / ".husky/_/pre-commit").read_text()
    assert run("guard", "install", "--repo", str(repo), "-y") == 0
    out = capsys.readouterr().out
    assert "↩ undo: sh" in out
    text = (repo / ".husky/pre-commit").read_text()
    assert text.startswith("#!/usr/bin/env sh\n# BEGIN gitpro -- keep first\n") and text.endswith("echo husky-ran\n")
    assert (repo / ".husky/pre-commit").stat().st_mode & 0o777 == 0o755   # the file keeps its mode
    assert (repo / ".husky/_/pre-commit").read_text() == underscore
    assert git(repo, "status", "--porcelain") == before
    state = g / ".local/state/gitpro"
    backups = list(state.glob("guard-backup-*/*/pre-commit"))
    assert len(backups) == 1 and backups[0].read_text() == HUSKY_HOOK
    assert not list(repo.rglob("*.bak*"))
    assert run("guard", "status", "--repos", "--json") == 0
    assert json.loads(capsys.readouterr().out)["bypassed_repos"][0]["repo_mode"] is True
    # idempotent
    assert run("guard", "install", "--repo", str(repo), "-y") == 0
    assert (repo / ".husky/pre-commit").read_text() == text
    script = sorted(state.glob("guard-undo-*.sh"))[0]
    assert sh(["sh", str(script)]).returncode == 0
    assert (repo / ".husky/pre-commit").read_bytes() == HUSKY_HOOK.encode()
    assert not (repo / ".husky/pre-push").exists()


def test_global_guard_leaves_husky_repos_alone(installed, tmp_path):
    repo = _husky_repo(tmp_path)
    git(repo, "config", "user.email", "wrong@x")
    r = gcommit(repo)
    assert r.returncode == 0 and "husky-ran" in r.stderr + r.stdout and "gitpro" not in r.stderr
    assert git(repo, "config", "core.hooksPath") == ".husky"


def test_repo_mode_uninstall(g, capsys):
    repo = _husky_repo(g)
    assert run("guard", "install", "--repo", str(repo), "-y") == 0
    assert run("guard", "uninstall", "--repo", str(repo), "-y") == 0
    assert (repo / ".husky/pre-commit").read_text() == HUSKY_HOOK
    assert not (repo / ".husky/pre-push").exists() and not (repo / ".husky/commit-msg").exists()


def test_repo_mode_refusals(g, tmp_path, capsys):
    repo = _husky_repo(tmp_path)
    git(repo, "add", ".husky/pre-commit")
    assert run("guard", "install", "--repo", str(repo), "-y") == 2
    err = capsys.readouterr().err
    assert "tracked by git" in err and "gitpro guard check pre-commit" in err
    assert (repo / ".husky/pre-commit").read_text() == HUSKY_HOOK
    git(repo, "config", "core.hooksPath", ".husky/_")
    assert run("guard", "install", "--repo", str(repo), "-y") == 2
    err = capsys.readouterr().err
    assert "managed by husky" in err and ".husky/pre-commit" in err
    git(repo, "config", "core.hooksPath", ".hooks-local")
    (repo / ".hooks-local").mkdir()
    (repo / ".gitignore").write_text(".hooks-local\n")
    assert run("guard", "install", "--repo", str(repo), "-y") == 2
    assert "managed by" in capsys.readouterr().err
    git(repo, "config", "core.hooksPath", ".myhooks")
    (repo / ".myhooks").mkdir()
    (repo / ".myhooks/pre-commit").write_text("#!/usr/bin/env python3\nprint('hi')\n")
    (repo / ".myhooks/pre-commit").chmod(0o755)
    assert run("guard", "install", "--repo", str(repo), "-y") == 2
    assert "not a shell script" in capsys.readouterr().err
    assert run("guard", "install", "--repo", str(tmp_path), "-y") == 2
    assert "not a git repository" in capsys.readouterr().err


def test_repo_mode_skips_disabled_hook(g, tmp_path, capsys):
    repo = repo_with_remote(tmp_path, "app", WORK)
    off = repo / ".git/hooks/pre-commit"
    off.write_text("#!/bin/sh\necho off\n")
    off.chmod(0o644)
    assert run("guard", "install", "--repo", str(repo), "-y") == 0
    out = capsys.readouterr().out
    assert "· skip" in out and "not executable" in out
    assert off.read_text() == "#!/bin/sh\necho off\n" and off.stat().st_mode & 0o777 == 0o644
    assert (repo / ".git/hooks/pre-push").stat().st_mode & 0o777 == 0o700


def test_repo_mode_block_at_runtime(g, tmp_path, fake_bin, monkeypatch, capsys):
    repo = repo_with_remote(tmp_path, "app", WORK)
    assert run("guard", "install", "--repo", str(repo), "-y") == 0
    hook = repo / ".git/hooks/pre-commit"
    assert hook.read_text() == "#!/bin/sh\n" + render.guard_block("pre-commit")
    assert gcommit(repo).returncode == 0                  # real gitpro, clean repo, block only
    git(repo, "config", "user.email", "wrong@x")
    assert gcommit(repo).returncode == 1                  # real gitpro refuses
    assert gcommit(repo, GITPRO_SKIP="1").returncode == 0
    fake_bin.add("gitpro", "sys.exit(2)")
    assert gcommit(repo).returncode == 0
    fake_bin.add("gitpro", "sys.exit(3)")
    assert gcommit(repo).returncode == 1
    (fake_bin.path / "gitpro").unlink()
    monkeypatch.setenv("PATH", no_gitpro_path())
    assert gcommit(repo).returncode == 0


# ---------------------------------------------------------------- 18-20: guard test, timeout, speed

def test_guard_test_command(g, tmp_path, capsys):
    repo = repo_with_remote(tmp_path, "app", WORK)
    assert run("guard", "test", str(repo)) == 0
    assert "✓ guard: would allow commits as me@corp.test (work-github)" in capsys.readouterr().out
    git(repo, "config", "user.email", "wrong@x")
    assert run("guard", "test", str(repo)) == 3
    assert "✗ gitpro: this commit would be committed as wrong@x" in capsys.readouterr().err
    assert run("guard", "test", str(tmp_path)) == 2
    assert "not a git repository" in capsys.readouterr().err


def test_hanging_git_times_out(g, tmp_path, fake_bin):
    repo = repo_with_remote(tmp_path, "app", WORK)
    real = shutil.which("git")
    fake_bin.add("git", f"""
import time
if "var" in argv:
    time.sleep(30)
os.execv({real!r}, [{real!r}] + argv)
""")
    t = time.monotonic()
    r = sh([sys.executable, "-m", "gitpro", "guard", "check", "pre-commit"], cwd=repo)
    took = time.monotonic() - t
    assert took < 7.5 or took < 7.5 * slowdown()    # the 5 s git timeout, not the 30 s hang
    assert r.returncode == 0 and "timed out" in r.stderr and "not blocking" in r.stderr


def test_check_is_fast_and_light(g, tmp_path):
    repo = repo_with_remote(tmp_path, "app", WORK)
    t = time.monotonic()
    r = sh([sys.executable, "-m", "gitpro", "guard", "check", "pre-commit"], cwd=repo)
    took = time.monotonic() - t
    assert r.returncode == 0 and r.stderr == ""
    assert took < 1.5 or took < 1.5 * slowdown(), took
    r = sh([sys.executable, "-X", "importtime", "-c", "import gitpro.guard"])
    mods = {line.split("|")[-1].strip() for line in r.stderr.splitlines() if "|" in line}
    assert not {"rich", "gitpro.cli", "gitpro.view"} & mods
    code = ("import sys, os; os.chdir(sys.argv[1]); from gitpro import guard; guard.check('pre-commit', []);"
            "print(sorted(m for m in sys.modules if m == 'rich' or m.startswith(('rich.', 'gitpro.'))))")
    r = sh([sys.executable, "-c", code, str(repo)])
    loaded = r.stdout
    assert "rich" not in loaded and "gitpro.cli" not in loaded and "gitpro.view" not in loaded, loaded


# ---------------------------------------------------------------- QA (independent review)

def test_qa_push_to_checkout_stub_keeps_update_instead(installed, tmp_path):
    dest = tmp_path / "dest"
    subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True, timeout=TIMEOUT)
    git(dest, "config", "receive.denyCurrentBranch", "updateInstead")
    (dest / "f").write_text("v1\n")
    git(dest, "add", "f")
    assert gcommit(dest, "v1").returncode == 0
    src = tmp_path / "src"
    subprocess.run(["git", "clone", "-q", str(dest), str(src)], check=True, timeout=TIMEOUT)
    (src / "f").write_text("v2\n")
    assert gcommit(src, "v2", "-a").returncode == 0
    assert gitc(src, "push", "-q", "origin", "main").returncode == 0
    # without the guard git checks the pushed tree out in dest; with the stub present it no longer does
    assert (dest / "f").read_text() == "v2\n"
    assert gitc(dest, "status", "--porcelain").stdout == ""


def test_qa_chain_to_validated_on_apply(g, cfg, tmp_path, capsys):
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o777)
    path = cfg(apply=False)
    path.write_text(DEFAULT_CONFIG + f'\n[guard]\nhooks = true\nchain_to = "{loose}"\n')
    assert run("apply", "-y") == 2
    assert "writable" in capsys.readouterr().err
    assert not (hooks_dir(g) / "pre-commit").exists()


def test_qa_repo_mode_never_edits_foreign_global_hooks_dir(g, tmp_path, capsys):
    foreign = tmp_path / "foreign-hooks"
    foreign.mkdir()
    git(tmp_path, "config", "--global", "core.hooksPath", str(foreign))
    repo = repo_with_remote(tmp_path, "app", WORK)
    assert run("guard", "install", "--repo", str(repo), "-y") == 2
    err = capsys.readouterr().err
    assert "global config" in err and "other repos share it" in err and str(foreign) in err
    assert list(foreign.iterdir()) == []


# ---------------------------------------------------------------- QA fixes

def test_git_dir_dotfiles_repo_is_judged(installed, tmp_path):
    """`git --git-dir=~/.dotfiles --work-tree=~`: the work tree is no repo; the git dir is judged."""
    home = installed
    dot = home / ".dotfiles"
    subprocess.run(["git", "init", "-q", "--bare", str(dot)], check=True, timeout=TIMEOUT)
    (home / ".bashrc_test").write_text("hi\n")

    def dgit(*a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", f"--git-dir={dot}", f"--work-tree={home}", *a], cwd=str(home),
                              capture_output=True, text=True, timeout=TIMEOUT)
    assert dgit("add", ".bashrc_test").returncode == 0
    r = dgit("commit", "-q", "--no-gpg-sign", "-m", "dot")
    assert r.returncode == 0 and "⚠" not in r.stderr, r.stderr      # no remote: nothing to judge, no warning
    assert dgit("remote", "add", "origin", "git@github.com:me/dotfiles.git").returncode == 0
    r = dgit("commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", "dot2")
    assert r.returncode == 0 and r.stderr == "", r.stderr            # the include gives the personal email
    assert dgit("config", "user.email", "me@corp.test").returncode == 0
    r = dgit("commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", "dot3")
    assert r.returncode == 1 and "belongs to identity personal" in r.stderr, r.stderr


def test_git_dir_inside_another_repo_judges_git_dir(installed, tmp_path):
    """GIT_DIR names a work repo while the work tree sits in a personal repo: judge GIT_DIR's repo."""
    work = repo_with_remote(tmp_path, "app", WORK)
    personal = repo_with_remote(tmp_path, "mine", "git@github.com:me/mine.git")
    (personal / "sub").mkdir()
    env = dict(os.environ, GIT_DIR=str(work / ".git"), GIT_WORK_TREE=str(personal / "sub"))
    r = subprocess.run(["git", "commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", "cross"],
                       cwd=str(personal / "sub"), env=env, capture_output=True, text=True, timeout=TIMEOUT)
    assert r.returncode == 0 and r.stderr == "", r.stderr
    assert git(work, "log", "-1", "--format=%ce") == "me@corp.test"
    git(work, "config", "user.email", "me@personal.test")
    r = subprocess.run(["git", "commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", "cross2"],
                       cwd=str(personal / "sub"), env=env, capture_output=True, text=True, timeout=TIMEOUT)
    assert r.returncode == 1 and "belongs to identity work-github" in r.stderr


def test_home_repo_local_hooks_path_is_not_foreign(g, capsys):
    """A dotfiles repo at HOME with its own core.hooksPath is that repo's, not a global setting."""
    subprocess.run(["git", "init", "-q", str(g)], check=True, timeout=TIMEOUT)
    git(g, "config", "core.hooksPath", ".githooks")
    assert guard.foreign_hooks_path(g) == []
    assert run("guard", "install", "-y") == 0
    capsys.readouterr()
    st = guard.status()
    assert st["installed"] == "global" and st["foreign_hooks_path"] == []


def test_chain_advice_is_absolute(g, tmp_path, capsys):
    assert guard.chain_advice("~/hooks") == f'set [guard] chain_to = "{g}/hooks" so gitpro runs those hooks too'
    assert "relative" in guard.chain_advice(".githooks") and "chain_to =" not in guard.chain_advice(".githooks")
    git(tmp_path, "config", "--global", "core.hooksPath", "~/team-hooks")
    assert run("guard", "install", "-y") == 2
    assert f'chain_to = "{g}/team-hooks"' in capsys.readouterr().err


def test_hook_timeout_probe(fake_bin, monkeypatch):
    plan.hook_timeout.cache_clear()
    try:
        fake_bin.add("timeout", "sys.exit(1 if '--foreground' in argv else 0)")   # busybox: no --foreground
        assert plan.hook_timeout() == ""
        plan.hook_timeout.cache_clear()
        fake_bin.add("timeout", "sys.exit(0)")
        assert plan.hook_timeout() == "timeout --foreground 30"
    finally:
        plan.hook_timeout.cache_clear()
    text = render.hook_script("pre-commit", None, None, "timeout --foreground 30")
    assert 'in_tmp timeout --foreground 30 "$gp" guard check pre-commit' in text
    assert "timeout" not in render.hook_script("pre-commit", None, None).split("in_tmp() {")[1]


def test_baked_timeout_missing_at_runtime_does_not_warn(work, g, monkeypatch, tmp_path):
    """Scripts rendered with timeout --foreground still run the check when timeout(1) is gone."""
    script = render.hook_script("pre-commit", None, None, "timeout --foreground 30")
    (hooks_dir(g) / "pre-commit").write_text(script)
    bare = tmp_path / "nt"
    bare.mkdir()
    for tool in ("git", "sh", "cat", "dirname", "mktemp", "rm", "grep", "python3"):
        found = shutil.which(tool)
        if found:
            (bare / tool).symlink_to(found)
    gp = shutil.which("gitpro")
    (bare / "gitpro").symlink_to(gp)
    monkeypatch.setenv("PATH", str(bare))
    git(work, "config", "user.email", "wrong@x")
    r = gcommit(work)
    assert r.returncode == 1 and "exited" not in r.stderr, r.stderr


def test_scripts_need_no_path_format(installed):
    for name in render.HOOK_NAMES:
        assert "rev-parse --path-format" not in (hooks_dir(installed) / name).read_text()


def test_interrupted_pre_push_removes_temp_file(installed, tmp_path):
    chain = tmp_path / "chain"
    chain.mkdir()
    (chain / "pre-push").write_text("#!/bin/sh\nkill -TERM $PPID\nsleep 1\n")
    (chain / "pre-push").chmod(0o755)
    script = tmp_path / "pre-push"
    script.write_text(render.hook_script("pre-push", str(chain), None))
    script.chmod(0o755)
    tmpdir = tmp_path / "t"
    tmpdir.mkdir()
    repo = repo_with_remote(tmp_path, "app", WORK)
    env = dict(os.environ, TMPDIR=str(tmpdir), GITPRO_SKIP="1")
    for shell in ("sh", "dash", "bash"):
        if not shutil.which(shell):
            continue
        r = sh([shell, str(script), "origin", WORK], cwd=repo, env=env, stdin="refs/heads/main 1 refs/heads/main 0\n")
        assert r.returncode == 143, (shell, r.returncode, r.stderr)
        assert list(tmpdir.iterdir()) == [], shell


def test_non_utf8_git_output_is_decoded(work):
    raw = work.parent / "raw"
    raw.write_bytes(b"caf\xe9 latin-1\n")
    blob = git(work, "hash-object", "-w", str(raw))
    assert guard._hook_git(work, "cat-file", "-p", blob).stdout == "caf\ufffd latin-1\n"


def test_latin1_subject_is_reported(work):
    git(work, "config", "user.email", "me@personal.test")
    msg = work.parent / "msg"
    msg.write_bytes(b"caf\xe9 latin-1\n")
    assert gitc(work, "commit", "-q", "--allow-empty", "--no-gpg-sign", "-F", str(msg),
                GITPRO_SKIP="1").returncode == 0
    r = gitc(work, "push", "-q", "origin", "main")
    assert r.returncode != 0 and "committed as me@personal.test" in r.stderr and "latin-1" in r.stderr, r.stderr


def test_doctor_section(g, capsys):
    class Ctx:
        home = g
    out = guard.doctor_section(Ctx())
    assert out["status"] == "skipped" and "guard install" in out["summary"]
    assert run("guard", "install", "-y") == 0
    capsys.readouterr()
    out = guard.doctor_section(Ctx())
    assert out["status"] == "ok" and out["problems"] == [] and "on_mismatch = block" in out["summary"]
    (hooks_dir(g) / "post-merge").unlink()
    out = guard.doctor_section(Ctx())
    assert out["status"] == "error" and "post-merge" in out["problems"][0]["message"]


def test_gitpro_uninstall_removes_empty_hooks_dir(installed, capsys):
    assert run("uninstall", "-y") == 0
    assert not hooks_dir(installed).exists() and not (installed / ".gitconfig-gitpro-guard").exists()


def test_chain_to_revalidated_on_every_apply(g, cfg, tmp_path, capsys):
    chain = tmp_path / "chain"
    chain.mkdir()
    chain.chmod(0o755)
    path = cfg(apply=False)
    path.write_text(DEFAULT_CONFIG + f'\n[guard]\nhooks = true\nchain_to = "{chain}"\n')
    assert run("apply", "-y") == 0
    assert (hooks_dir(g) / "pre-commit").exists()
    chain.chmod(0o777)
    capsys.readouterr()
    assert run("apply", "-y") == 2
    assert "writable" in capsys.readouterr().err


def test_qa_repo_hook_runs_with_exported_cdpath(work, tmp_path):
    marker = tmp_path / "pre-commit.ran"
    write_hook(work, "pre-commit", f'echo ran > "{marker}"\n')
    assert gcommit(work, CDPATH=".").returncode == 0
    assert marker.exists()


def test_repo_hook_runs_with_cdpath_decoy(work, tmp_path):
    """A CDPATH dir holding its own .git must not stand in for the repo's."""
    decoy = tmp_path / "decoy"
    (decoy / ".git/hooks").mkdir(parents=True)
    marker = tmp_path / "ran"
    write_hook(work, "pre-commit", f'echo repo > "{marker}"\n')
    (decoy / ".git/hooks/pre-commit").write_text(f'#!/bin/sh\necho decoy > "{marker}"\n')
    (decoy / ".git/hooks/pre-commit").chmod(0o755)
    assert gcommit(work, CDPATH=str(decoy)).returncode == 0
    assert marker.read_text() == "repo\n"


def test_warn_once_file_is_per_commit(work, g, fake_bin):
    """pre-commit clears the note on every run and commit-msg removes it: a stale file never hides a warning."""
    common = work / ".git"
    note = common / "gitpro-guard-warned"
    fake_bin.add("gitpro", "sys.exit(1)")
    r = gcommit(work)
    assert r.stderr.count("⚠ gitpro guard") == 1 and not note.exists()
    note.write_text("12345\n")                       # stale, as left by an aborted commit
    fake_bin.add("gitpro", "sys.exit(0 if argv[2] == 'pre-commit' else 1)")
    r = gcommit(work, "x")
    assert r.stderr.count("⚠ gitpro guard: exited 1") == 1 and not note.exists(), r.stderr
    # a reused pid: the stale note names this very parent; a clean pre-commit must still clear it
    note.write_text(f"{os.getpid()}\n")
    msg = work / ".git/MSG"
    msg.write_text("x\n")
    r = sh(["sh", str(hooks_dir(g) / "pre-commit")], cwd=work)
    assert r.returncode == 0 and r.stderr == "" and not note.exists(), r.stderr
    note.write_text(f"{os.getpid()}\n")
    r = sh(["sh", str(hooks_dir(g) / "pre-commit")], cwd=work)
    r = sh(["sh", str(hooks_dir(g) / "commit-msg"), str(msg)], cwd=work)
    assert r.stderr.count("⚠ gitpro guard: exited 1") == 1, r.stderr


def test_missing_chain_to_warns_and_renders_without_chain(g, cfg, tmp_path, capsys):
    chain = tmp_path / "chain"
    chain.mkdir()
    path = cfg(apply=False)
    path.write_text(DEFAULT_CONFIG + f'\n[guard]\nhooks = true\nchain_to = "{chain}"\n')
    assert run("apply", "-y") == 0
    assert guard._chain_of(hooks_dir(g) / "pre-commit") == str(chain)
    chain.rmdir()
    guard._WARNED_MISSING.clear()
    capsys.readouterr()
    assert run("apply", "-y") == 0
    err = capsys.readouterr().err
    assert f"⚠ [guard] chain_to {chain} does not exist" in err and "remove chain_to" in err
    assert guard._chain_of(hooks_dir(g) / "pre-commit") is None

    class Ctx:
        home = g
        cfg = __import__("gitpro.config", fromlist=["x"]).load_all(path, "flag")
    out = guard.doctor_section(Ctx())
    assert out["status"] == "warning" and "does not exist" in out["problems"][0]["message"]


def test_where_redacts_url_credentials():
    import types
    from gitpro import guard
    m = types.SimpleNamespace(remote="origin", url="https://user:ghp_xxx@github.com/o/r.git")
    assert guard._where(types.SimpleNamespace(match=m)) == "origin (https://user:***@github.com/o/r.git)"
    m.remote = None
    assert guard._where(types.SimpleNamespace(match=m)) == "https://user:***@github.com/o/r.git"
    m.url = None
    assert guard._where(types.SimpleNamespace(match=m)) == "this repo"


def test_two_identity_remotes_blocked_until_pinned(work):
    """spec 21 B1: origin is work, upstream personal; git applies personal, so the commit is blocked."""
    git(work, "remote", "add", "upstream", "git@github.com:me/app.git")
    r = gcommit(work)
    assert r.returncode == 1
    assert "✗ gitpro: the remotes belong to different identities" in r.stderr
    assert f"fix:   gitpro pin -y --remote origin {work}" in r.stderr
    assert run("pin", "-y", "--remote", "origin", str(work)) == 0
    assert gcommit(work).returncode == 0
    assert git(work, "log", "-1", "--format=%ce") == "me@corp.test"
