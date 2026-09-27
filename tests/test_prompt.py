"""`gitpro prompt` (spec 16 §2.2, tests 10-17): segment text, cache keys, budget, import cost."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG, TIMEOUT, git, repo_with_remote, run, slowdown
from gitpro import hot, prompt

ROOT = Path(__file__).resolve().parent.parent
WORK_URL = "git@github.com:corp-org/app.git"

NO_DEFAULT = """\
[default]

[[identity]]
id = "work-github"
name = "Work Me"
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
ssh_key = "~/.ssh/id_work"
"""


def say(capsys, *argv) -> str:
    """Run `gitpro prompt ARGV` through hot.main (the real route) and return stdout; exit must be 0."""
    assert hot.main(["prompt", *map(str, argv)]) == 0
    return capsys.readouterr().out


class GitCounter:
    """Counts git subprocesses started in this process (monkeypatched subprocess.run)."""

    def __init__(self, monkeypatch):
        self.n = 0
        real = subprocess.run

        def counting(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and cmd and os.path.basename(str(cmd[0])) == "git":
                self.n += 1
            return real(cmd, *a, **kw)
        monkeypatch.setattr(subprocess, "run", counting)

    def take(self) -> int:
        n, self.n = self.n, 0
        return n


@pytest.fixture
def work_repo(cfg, tmp_path):
    cfg()
    return repo_with_remote(tmp_path, "app", WORK_URL)


# ---------------------------------------------------------------- 10, 11: the text

def test_matching_repo(work_repo, capsys):  # 10
    assert say(capsys, work_repo) == "✓ work-github\n"


def test_override_shows_effective_email(work_repo, capsys):  # 10
    git(work_repo, "config", "user.email", "old@personal.test")
    assert say(capsys, work_repo) == "✗ old@personal.test\n"


def test_no_identity(cfg, tmp_path, capsys):  # 10
    cfg(NO_DEFAULT)
    repo = repo_with_remote(tmp_path, "other", "git@gitlab.com:someone/thing.git")
    assert say(capsys, repo) == "⚠ no identity\n"


def test_default_identity_is_ok(cfg, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "other", "git@gitlab.com:someone/thing.git")
    assert say(capsys, repo, "--format", "{sym} {id}|{state}|{email}") == "✓ default|ok|me@default.test\n"


def test_outside_a_repo_is_empty(cfg, tmp_path, capsys):  # 10
    cfg()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert say(capsys, plain) == ""


def test_subdirectory_and_cli_route(work_repo, capsys):
    sub = work_repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert run("prompt", str(sub)) == 0
    assert capsys.readouterr().out == "✓ work-github\n"


def test_linked_worktree(work_repo, tmp_path, capsys):
    git(work_repo, "commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", "x")
    wt = tmp_path / "wt"
    git(work_repo, "worktree", "add", "-q", str(wt))
    assert say(capsys, wt) == "✓ work-github\n"
    assert prompt._git_dirs(wt)[1] == (work_repo / ".git").resolve()


def test_errors_print_nothing(home, tmp_path, capsys):  # 10: no config, any error -> empty, exit 0
    repo = repo_with_remote(tmp_path, "app", WORK_URL)
    assert say(capsys, repo) == ""
    (repo / ".git" / "config").write_text("[broken")
    assert say(capsys, repo) == ""


def test_format(work_repo, capsys):  # 11
    assert say(capsys, work_repo, "--format", "{id}|{state}|{key}") == "work-github|ok|id_work\n"


def test_bad_format_prints_nothing(work_repo, capsys):
    assert say(capsys, work_repo, "--format", "{nope}") == ""


# ---------------------------------------------------------------- 12, 13: cache

def test_cache_hit_makes_no_git_calls(work_repo, capsys, monkeypatch):  # 12
    counter = GitCounter(monkeypatch)
    fmt = ("--format", "{sym} {email}")
    assert say(capsys, work_repo, *fmt) == "✓ me@corp.test\n" and counter.take() > 0
    assert say(capsys, work_repo, *fmt) == "✓ me@corp.test\n" and counter.take() == 0
    cache = prompt.cache_path()
    assert cache.stat().st_mode & 0o777 == 0o600

    git(work_repo, "config", "user.email", "x@elsewhere.test")       # .git/config changed
    assert say(capsys, work_repo, *fmt) == "✗ x@elsewhere.test\n" and counter.take() > 0
    git(work_repo, "config", "--unset", "user.email")
    assert say(capsys, work_repo, *fmt) == "✓ me@corp.test\n" and counter.take() > 0
    assert say(capsys, work_repo, *fmt) == "✓ me@corp.test\n" and counter.take() == 0


def test_cache_follows_the_toml(work_repo, cfg, capsys, monkeypatch):  # 12
    counter = GitCounter(monkeypatch)
    assert say(capsys, work_repo, "--format", "{email}") == "me@corp.test\n"
    counter.take()
    cfg(DEFAULT_CONFIG.replace('email = "me@corp.test"', 'email = "new@corp.test"'), apply=False)
    assert say(capsys, work_repo, "--format", "{state}") == "mismatch\n" and counter.take() > 0
    cfg(DEFAULT_CONFIG.replace('email = "me@corp.test"', 'email = "new@corp.test"'))
    assert say(capsys, work_repo, "--format", "{email}|{state}") == "new@corp.test|ok\n" and counter.take() > 0


def test_cache_follows_the_identity_file(work_repo, home, capsys, monkeypatch):  # 12 (~/.gitconfig-<id>)
    counter = GitCounter(monkeypatch)
    say(capsys, work_repo)
    counter.take()
    f = home / ".gitconfig-work-github"
    f.write_text(f.read_text().replace("me@corp.test", "edited@corp.test"))
    assert say(capsys, work_repo, "--format", "{email}") == "edited@corp.test\n" and counter.take() > 0


def test_cache_follows_pin(work_repo, capsys, monkeypatch):  # 12
    counter = GitCounter(monkeypatch)
    say(capsys, work_repo)
    say(capsys, work_repo)
    assert counter.take() > 0
    say(capsys, work_repo)
    assert counter.take() == 0
    assert run("pin", str(work_repo)) == 0
    capsys.readouterr()
    counter.take()
    assert say(capsys, work_repo) == "✓ work-github\n" and counter.take() > 0


def test_cache_follows_env(work_repo, capsys, monkeypatch):  # 12 (env hash)
    counter = GitCounter(monkeypatch)
    say(capsys, work_repo)
    counter.take()
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "someone@else.test")
    assert say(capsys, work_repo) == "✗ me@corp.test\n" and counter.take() > 0
    raw = prompt.cache_path().read_text()
    assert "someone@else.test" not in raw          # env values are hashed, never stored


def test_git_config_global_bypasses_cache(work_repo, home, capsys, monkeypatch):  # 13
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    counter = GitCounter(monkeypatch)
    for _ in range(2):
        assert say(capsys, work_repo) == "✓ work-github\n"
        assert counter.take() > 0
    assert not prompt.cache_path().exists()


# ---------------------------------------------------------------- 14: budget

def test_budget_zero(work_repo, capsys, monkeypatch):  # 14
    counter = GitCounter(monkeypatch)
    assert say(capsys, work_repo, "--max-ms", "0") == "" and counter.take() > 0
    assert say(capsys, work_repo, "--max-ms", "0") == "✓ work-github\n" and counter.take() == 0


def test_over_budget_prints_stale_value(work_repo, capsys):  # 14
    assert say(capsys, work_repo) == "✓ work-github\n"
    git(work_repo, "config", "user.email", "old@personal.test")
    assert say(capsys, work_repo, "--max-ms", "0") == "✓ work-github\n"    # stale, but the slow run was cached
    assert say(capsys, work_repo, "--max-ms", "0") == "✗ old@personal.test\n"


def test_within_budget_in_process(work_repo):  # time budget: cold run within 80 ms, cached run far below
    cache = prompt.cache_path()
    cold = []
    for _ in range(5):          # best of 5: a loaded machine may stall one run
        if cache.exists():
            cache.unlink()
        t = time.monotonic()
        assert prompt.segment(work_repo) == "✓ work-github"
        cold.append(time.monotonic() - t)
    warm = []
    for _ in range(5):
        t = time.monotonic()
        assert prompt.segment(work_repo) == "✓ work-github"
        warm.append(time.monotonic() - t)
    if min(cold) * 1000 >= prompt.DEFAULT_MAX_MS or min(warm) * 1000 >= 15:   # a busy machine stretches the budget
        k = slowdown()
        assert min(cold) * 1000 < prompt.DEFAULT_MAX_MS * k, (cold, k)
        assert min(warm) * 1000 < 15 * k, (warm, k)


def test_cached_prompt_process_is_fast(work_repo, home, fake_bin):  # time budget, end to end
    env = dict(os.environ)
    r = subprocess.run([sys.executable, "-m", "gitpro", "prompt"], cwd=work_repo, env=env, capture_output=True,
                       text=True, timeout=TIMEOUT)
    assert r.stdout == "✓ work-github\n", r.stderr
    fake_bin.add("git", "print('fake git called', file=sys.stderr)\nsys.exit(1)")   # any git call now shows
    best = None
    for _ in range(5):          # best of 5, stopping once within budget: a loaded machine may stall a run
        t = time.monotonic()
        r = subprocess.run([sys.executable, "-m", "gitpro", "prompt"], cwd=work_repo, env=dict(os.environ),
                           capture_output=True, text=True, timeout=TIMEOUT)
        took = time.monotonic() - t
        best = took if best is None else min(best, took)
        assert r.returncode == 0 and r.stdout == "✓ work-github\n", r.stderr
        if best < 0.5:
            break
    assert fake_bin.calls("git") == []
    # python start-up plus a stat per input; no git, no rich. Under load the budget grows with the slowdown.
    assert best < 0.5 or best < 0.5 * slowdown(), best


# ---------------------------------------------------------------- 15-17

def test_install_help(capsys):  # 15
    out = say(capsys, "--install-help")
    lines = out.splitlines()
    assert [ln.split(":")[0] for ln in lines] == ["bash", "zsh", "fish", "starship"]
    assert "add-zsh-hook precmd" in lines[1]
    assert "$(gitpro prompt)" in lines[0]


def test_import_cost():  # 16
    r = subprocess.run([sys.executable, "-X", "importtime", "-c", "import gitpro.prompt"], capture_output=True,
                       text=True, timeout=TIMEOUT)
    assert r.returncode == 0, r.stderr
    names = {line.rsplit("|", 1)[-1].strip() for line in r.stderr.splitlines() if "|" in line}
    assert "gitpro.prompt" in names
    assert not {n for n in names if n == "rich" or n.startswith("rich.")}
    assert not {"gitpro.cli", "gitpro.view"} & names


def test_hot_route_never_loads_rich(work_repo):  # 16: the whole run, not only the import
    code = ("import sys\nfrom gitpro import hot\nrc = hot.main(['prompt', sys.argv[1]])\n"
            "bad = [m for m in sys.modules if m == 'rich' or m.startswith('rich.') or m in ('gitpro.cli', 'gitpro.view')]\n"
            "print(rc, bad)\n")
    r = subprocess.run([sys.executable, "-c", code, str(work_repo)], capture_output=True, text=True,
                       timeout=TIMEOUT)
    assert r.stdout.splitlines() == ["✓ work-github", "0 []"], r.stderr


def test_garbage_cache(work_repo, capsys):  # 17
    cache = prompt.cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    for junk in ("not json{", "[1, 2]", json.dumps({"version": 1, "entries": {str(work_repo / ".git"): "x"}})):
        cache.write_text(junk)
        cache.chmod(0o644)
        assert say(capsys, work_repo) == "✓ work-github\n"
        data = json.loads(cache.read_text())
        assert data["version"] == prompt.CACHE_VERSION and len(data["entries"]) == 1
        assert cache.stat().st_mode & 0o777 == 0o600


def test_cache_entry_shape(work_repo, home, capsys):
    say(capsys, work_repo)
    entry = next(iter(json.loads(prompt.cache_path().read_text())["entries"].values()))
    assert set(entry["result"]) == {"id", "email", "key", "state"}
    paths = {k[0] for k in entry["key"]}
    assert {str(work_repo / ".git" / "config"), str(work_repo / ".git" / "git-identities"), str(home / ".gitconfig"),
            str(home / ".gitconfig-identities"), str(home / ".gitconfig-work-github"),
            str(home / ".gitconfig-personal"), str(home / ".config/gitpro.toml")} <= paths


def test_xdg_state_home(work_repo, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    say(capsys, work_repo)
    assert (tmp_path / "state" / "gitpro" / "prompt-cache.json").is_file()


def test_capability_registered():
    from gitpro import cli
    cli.build_parser()
    assert "prompt" in cli.CAPABILITIES


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_bash_install_line_works(work_repo, home):
    """The bash snippet, evaluated by a real bash, puts the segment in PS1."""
    line = prompt.INSTALL_HELP.splitlines()[0].split(":", 1)[1].strip()
    gitpro_bin = home.parent / "bin"
    gitpro_bin.mkdir()
    script = gitpro_bin / "gitpro"
    script.write_text(f"#!{sys.executable}\nimport sys\nfrom gitpro import hot\nsys.exit(hot.main(sys.argv[1:]))\n")
    script.chmod(0o755)
    env = dict(os.environ, PATH=f"{gitpro_bin}{os.pathsep}{os.environ['PATH']}")
    r = subprocess.run(["bash", "--norc", "--noprofile", "-c", f"PS1='$ '; {line}; eval \"echo $PS1\""],
                       cwd=work_repo, env=env, capture_output=True, text=True, timeout=TIMEOUT)
    assert r.stdout.strip() == "✓ work-github $", r.stderr


def test_mirrored_constants_and_find(home, tmp_path, monkeypatch):
    """prompt keeps config/resolve out of a cache hit; its copies must agree with the originals."""
    from gitpro import config, resolve
    assert (prompt.CONFIG_ENV_VAR, prompt.USER_CONFIG) == (config.ENV_VAR, config.USER_CONFIG)
    assert (prompt.PIN_FILE, prompt.MANAGED_GITCONFIG) == (resolve.PIN_FILE, resolve.MANAGED_GITCONFIG)
    with pytest.raises(config.ConfigError, match="no config found"):
        prompt.find_config(None, home)
    user = home / ".config/gitpro.toml"
    user.parent.mkdir(parents=True)
    user.write_text("")
    explicit = tmp_path / "x.toml"
    for case in ("user", "env", "explicit"):
        if case == "env":
            monkeypatch.setenv("GITPRO_CONFIG", str(tmp_path / "env.toml"))
        arg = explicit if case == "explicit" else None
        assert prompt.find_config(arg, home) == config.find(arg, home)[0]


def test_cache_hit_imports_neither_config_nor_resolve(work_repo):
    code = ("import sys\nfrom gitpro import hot\nrc = hot.main(['prompt', sys.argv[1]])\n"
            "print(rc, sorted(m for m in ('gitpro.config', 'gitpro.resolve') if m in sys.modules))\n")
    outs = [subprocess.run([sys.executable, "-c", code, str(work_repo)], capture_output=True, text=True,
                           timeout=TIMEOUT).stdout.splitlines() for _ in range(2)]
    assert outs[0] == ["✓ work-github", "0 ['gitpro.config', 'gitpro.resolve']"]
    assert outs[1] == ["✓ work-github", "0 []"]


# ---------------------------------------------------------------- QA (review of 16): real bugs, expected to fail until fixed

def test_qa_unwritable_state_dir_still_prints(work_repo, capsys):
    """A state dir that cannot be written (here: a file in its place) must not blank the prompt."""
    state = prompt.cache_path().parent
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("not a directory")
    assert say(capsys, work_repo) == "✓ work-github\n"


def test_qa_non_numeric_cache_time_is_ignored(work_repo, capsys):
    """Over MAX_ENTRIES the cache is sorted by "t"; a string "t" must not blank the prompt forever."""
    cache = prompt.cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    junk = {f"/nowhere/{n}/.git": {"key": [], "env": "", "result": {"id": "x", "email": "", "key": "", "state": "ok"},
                                   "t": "later"} for n in range(prompt.MAX_ENTRIES + 5)}
    cache.write_text(json.dumps({"version": prompt.CACHE_VERSION, "entries": junk}))
    assert say(capsys, work_repo) == "✓ work-github\n"


def test_qa_unencodable_output_prints_nothing(work_repo):
    """An ASCII-only stdout must not turn the prompt into a traceback (spec: errors print nothing, exit 0)."""
    env = {**os.environ, "PYTHONIOENCODING": "ascii"}
    r = subprocess.run([sys.executable, "-c", "import sys; from gitpro import hot; sys.exit(hot.main(sys.argv[1:]))",
                        "prompt", str(work_repo)], env=env, capture_output=True,
                       text=True, timeout=TIMEOUT)
    assert r.returncode == 0 and "Traceback" not in r.stderr, r.stderr


# ---------------------------------------------------------------- review fixes

def test_right_email_other_problem_is_a_warning(work_repo, capsys):
    """A key mismatch with the right email is ⚠ (state warn), not ✗; ✗ stays for a wrong email."""
    git(work_repo, "config", "core.sshCommand", "ssh -i ~/.ssh/id_personal -o IdentitiesOnly=yes")
    assert say(capsys, work_repo) == "⚠ work-github\n"
    assert say(capsys, work_repo, "--format", "{state}|{sym}|{key}") == "warn|⚠|id_personal\n"


def test_bad_arguments_print_nothing(home, capsys):
    assert hot.main(["prompt", "--max-ms", "soon"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert hot.main(["prompt", "--no-such-flag"]) == 0
    assert capsys.readouterr().err == ""


def test_ctrl_c_prints_nothing(work_repo, capsys, monkeypatch):
    def interrupted(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(prompt, "segment", interrupted)
    assert say(capsys, work_repo) == ""


def test_closed_pipe_exits_0_quietly(work_repo):
    code = "import sys; from gitpro import hot; sys.exit(hot.main(sys.argv[1:]))"
    p = subprocess.Popen([sys.executable, "-c", code, "prompt", str(work_repo)], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    p.stdout.close()                       # the reader went away before the prompt printed
    _out, err = p.communicate(timeout=TIMEOUT)
    assert p.returncode == 0 and b"Traceback" not in err and b"BrokenPipe" not in err, err
