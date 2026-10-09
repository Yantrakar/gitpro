"""Shared fixtures for every package (spec 01 §7, README §10). Nothing here touches the network or the real home."""
from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import pytest

from gitpro import cli

TIMEOUT = 60  # seconds, for every subprocess a test starts
QUIET_START = 0.025  # seconds: a bare `python -c pass` on a quiet machine (about 0.013 on the dev box)


def slowdown() -> float:
    """How many times slower than a quiet machine this one runs right now (at least 1): a bare python start-up
    (best of 3) against QUIET_START, or the load average per CPU, whichever is larger. Wall-clock asserts use
    `took < budget or took < budget * slowdown()`, so load (parallel suites, a busy box) stretches the budget
    instead of failing the test, while a real slowdown of gitpro on a quiet machine still fails."""
    best = min(_timed([sys.executable, "-c", "pass"]) for _ in range(3))
    try:
        per_cpu = os.getloadavg()[0] / (os.cpu_count() or 1)
    except (AttributeError, OSError):       # no load average (Windows)
        per_cpu = 0.0
    return max(1.0, best / QUIET_START, per_cpu)


def _timed(argv: Sequence[str]) -> float:
    t = time.monotonic()
    subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT)
    return time.monotonic() - t


DEFAULT_CONFIG = """\
[default]
name = "Default Me"
email = "me@default.test"

[[identity]]
id = "work-github"
name = "Work Me"
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
ssh_key = "~/.ssh/id_work"
gh_account = "me-work"

[[identity]]
id = "personal"
name = "Personal Me"
email = "me@personal.test"
host = "github.com"
owners = ["me"]
ssh_key = "~/.ssh/id_personal"
"""

# A syntactically plausible ed25519 public key; no private key exists for it.
FAKE_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGl0aXNub3RhcmVhbGtleWl0aXNub3RhcmVhbGtleQ"


@pytest.fixture(autouse=True)
def isolated_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent hosts and Git hooks inject config, identity and repository overrides."""
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway $HOME; git subprocesses see it too."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.chdir(h)
    # A --basetemp inside a checkout must not let plain test directories discover that checkout.
    ceiling = str(tmp_path.parent.resolve())
    inherited = os.environ.get("GIT_CEILING_DIRECTORIES")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", ceiling + os.pathsep + inherited if inherited else ceiling)
    monkeypatch.setenv("HOME", str(h))
    for var in ("GITPRO_CONFIG", "GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a terminal
    return h.resolve()


def write_config(home: Path, text: str, mode: int = 0o600) -> Path:
    path = home / ".config/gitpro.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def run(*argv: str) -> int:
    return cli.main(list(argv))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          timeout=TIMEOUT).stdout.strip()


Person = Tuple[str, str]


def _person_env(author: Optional[Person], committer: Optional[Person], date: Optional[str]) -> dict:
    env = dict(os.environ)
    if author:
        env.update(GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1])
    if committer:
        env.update(GIT_COMMITTER_NAME=committer[0], GIT_COMMITTER_EMAIL=committer[1])
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return env


def commit(repo: Path, message: str, *, author: Optional[Person] = None, committer: Optional[Person] = None,
           trailers: Sequence[str] = (), date: Optional[str] = None) -> str:
    """One empty commit; returns its sha. author/committer are (name, email); committer defaults to author.
    With neither given, git resolves the identity from the config (the generated files). Hooks run;
    a failing commit raises CalledProcessError (stdout/stderr attached)."""
    committer = committer or author
    msg = message + ("\n\n" + "\n".join(trailers) if trailers else "")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", msg],
                   env=_person_env(author, committer, date), check=True, capture_output=True, text=True,
                   timeout=TIMEOUT)
    return git(repo, "rev-parse", "HEAD")


def repo_with_remote(tmp_path: Path, name: str, url: Optional[str], *, commits: int = 0,
                     author: Person = ("Work Me", "me@corp.test"), committer: Optional[Person] = None) -> Path:
    """git init tmp_path/name, origin = url (none when url is None), then `commits` empty commits
    with explicit identities, so the result does not depend on the generated config."""
    repo = tmp_path / name
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=TIMEOUT)
    if url:
        git(repo, "remote", "add", "origin", url)
    for n in range(commits):
        commit(repo, f"commit {n + 1}", author=author, committer=committer or author)
    return repo


@pytest.fixture
def bare_remote(tmp_path) -> Callable[[str], Path]:
    """bare_remote("origin.git") -> path of a new bare repo; use the path as a remote url for pushes."""
    def make(name: str) -> Path:
        path = tmp_path / "remotes" / name
        subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True, timeout=TIMEOUT)
        return path
    return make


def set_git_hooks_sandbox(monkeypatch, home: Path) -> None:
    """git reads HOME/.gitconfig (GIT_CONFIG_GLOBAL unset), never the system config, never prompts."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GITPRO_SKIP"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------- fake executables

_PRELUDE = '''\
import os, select, shlex, sys
argv = sys.argv[1:]
env = dict(os.environ)


def _stdin():
    try:
        # Never block on a terminal or on an inherited pipe nobody writes to.
        if os.isatty(0) or not select.select([0], [], [], 1.0)[0]:
            return b""
        return sys.stdin.buffer.read()
    except (OSError, ValueError):
        return b""


stdin = _stdin()


def _flag(k):
    return "set" if os.environ.get(k) else "unset"


with open({log!r}, "a", encoding="utf-8") as _log:
    _log.write(shlex.join(argv) + " env:GH_TOKEN=" + _flag("GH_TOKEN")
               + " env:GH_ENTERPRISE_TOKEN=" + _flag("GH_ENTERPRISE_TOKEN")
               + " env:GITLAB_TOKEN=" + _flag("GITLAB_TOKEN")
               + " stdin=" + shlex.quote(stdin.decode("latin-1").encode("unicode_escape").decode("ascii")) + "\\n")
'''

_SUFFIX_RE = re.compile(r" env:GH_TOKEN=(set|unset) env:GH_ENTERPRISE_TOKEN=(set|unset)"
                        r" env:GITLAB_TOKEN=(set|unset) stdin=(.*)$")


class FakeBin:
    """A directory first on PATH holding fake executables (Python scripts, never shell strings)."""

    def __init__(self, path: Path):
        self.path = path

    def log(self, name: str) -> Path:
        return self.path / f".{name}.log"

    def add(self, name: str, body: str = "") -> Path:
        """Write an executable `name` that logs its call, then runs `body`: Python with
        argv (arguments, without the program name), stdin (bytes) and env (dict) in scope.
        body may print and sys.exit(rc); falling off the end exits 0."""
        script = self.path / name
        script.write_text(f"#!{sys.executable}\n" + _PRELUDE.format(log=str(self.log(name)))
                          + textwrap.dedent(body) + "\nsys.exit(0)\n")
        script.chmod(0o755)
        return script

    def entries(self, name: str) -> List[dict]:
        """Every call: {"argv": [...], "GH_TOKEN": "set"|"unset", "GH_ENTERPRISE_TOKEN": ..., "GITLAB_TOKEN": ...,
        "stdin": bytes}; [] when never called."""
        out: List[dict] = []
        if not self.log(name).exists():
            return out
        for line in self.log(name).read_text(encoding="utf-8").splitlines():
            m = _SUFFIX_RE.search(line)
            assert m, f"unparsable fake log line: {line!r}"
            raw = shlex.split(m[4])
            data = (raw[0] if raw else "").encode("ascii").decode("unicode_escape").encode("latin-1")
            out.append({"argv": shlex.split(line[:m.start()]), "GH_TOKEN": m[1], "GH_ENTERPRISE_TOKEN": m[2],
                        "GITLAB_TOKEN": m[3], "stdin": data})
        return out

    def calls(self, name: str) -> List[List[str]]:
        """argv of every call, in order ([] when never called)."""
        return [e["argv"] for e in self.entries(name)]


@pytest.fixture
def fake_bin(tmp_path, monkeypatch) -> FakeBin:
    path = tmp_path / "fakebin"
    path.mkdir()
    monkeypatch.setenv("PATH", f"{path}{os.pathsep}{os.environ.get('PATH', '')}")
    return FakeBin(path)


# ---------------------------------------------------------------- a working config

_KEY_RE = re.compile(r'^\s*ssh_key\s*=\s*"(~/[^"]+)"', re.M)


@pytest.fixture
def cfg(home, tmp_path) -> Callable[..., Path]:
    """cfg(text=None, *, apply=True): write a config (DEFAULT_CONFIG unless text is given), create every
    ssh_key it names as an empty private file plus a fake .pub, run `apply -y` unless apply=False;
    return the config path. May be called again to change the config."""
    def make(text: Optional[str] = None, *, apply: bool = True) -> Path:
        text = DEFAULT_CONFIG if text is None else text
        for key in _KEY_RE.findall(text):
            priv = home / key[2:]
            priv.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not priv.exists():
                priv.write_text("")
                priv.chmod(0o600)
            pub = priv.with_name(priv.name + ".pub")
            if not pub.exists():
                pub.write_text(f"{FAKE_PUB} {priv.name}@gitpro.test\n")
        path = write_config(home, text)
        if apply:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["apply", "-y"])
            assert rc == 0, f"apply failed: {out.getvalue()}{err.getvalue()}"
        return path
    return make
