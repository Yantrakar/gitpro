"""Guided setup saves only validated configuration after explicit confirmation."""
import errno
import json
import os
import pty
import select
import stat
import subprocess
import sys
import time
from collections import deque
from importlib import resources
from pathlib import Path

import pytest
from rich.prompt import Confirm, Prompt

from conftest import DEFAULT_CONFIG, TIMEOUT, git, repo_with_remote, run, write_config
from gitpro import config, files


def interact(monkeypatch, answers=(), confirms=(), on_save=None):
    values, choices = deque(answers), deque(confirms)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def ask(*args, **kwargs):
        value = values.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    def confirm(label, **kwargs):
        if label == "Save this configuration?" and on_save:
            on_save()
        return choices.popleft()

    monkeypatch.setattr(Prompt, "ask", ask)
    monkeypatch.setattr(Confirm, "ask", confirm)
    return values, choices


IDENTITY = ["work", 'Jane "JJ" Doe', "jane@corp.test", "github.com", "my-org", ""]


def test_setup_creates_minimal_private_config_without_applying(home, monkeypatch, capsys):
    remaining = interact(monkeypatch, IDENTITY, [False, True, False, True])
    assert run("setup") == 0
    target = home / config.USER_CONFIG
    cfg = config.load_all(target)
    assert cfg.default.email is None
    assert cfg.identities[0].name == 'Jane "JJ" Doe'
    assert cfg.identities[0].owners == ["my-org"]
    assert cfg.identities[0].sign is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert all(not q for q in remaining)
    assert not (home / ".gitconfig").exists()
    assert not (home / ".ssh").exists()
    assert not (home / ".gitconfig-work").exists()
    assert "gitpro plan" in capsys.readouterr().out


def test_setup_allows_fallback_only(home, monkeypatch):
    interact(monkeypatch, ["Personal Me", "me@personal.test"], [True, False, True])
    assert run("setup") == 0
    cfg = config.load_all(home / config.USER_CONFIG)
    assert cfg.default.email == "me@personal.test" and not cfg.identities


@pytest.mark.parametrize("approve", [False, True])
def test_setup_preserves_existing_bytes_and_makes_backup(home, monkeypatch, approve):
    target = write_config(home, "# keep this comment\r\n" + DEFAULT_CONFIG.replace("\n", "\r\n"))
    before = target.read_bytes()
    interact(monkeypatch, ["extra", "Me", "me@other.test", "gitlab.com", "team/sub", ""],
             [True, False, approve])
    assert run("setup") == (0 if approve else 1)
    if approve:
        assert target.read_bytes().startswith(before)
        assert list(target.parent.glob("gitpro.toml.bak.*"))[0].read_bytes() == before
        assert config.load_all(target).identities[-1].id == "extra"
    else:
        assert target.read_bytes() == before
        assert not list(target.parent.glob("gitpro.toml.bak.*"))


def test_setup_existing_no_changes_is_a_noop(home, monkeypatch):
    target = write_config(home, DEFAULT_CONFIG)
    before = target.read_bytes()
    interact(monkeypatch, confirms=[False])
    assert run("setup") == 0
    assert target.read_bytes() == before
    assert not list(target.parent.glob("*.bak.*"))


def test_setup_adds_multiple_identities(home, monkeypatch):
    other = ["personal", "Personal Me", "me@personal.test", "gitlab.com", "my-group/sub", ""]
    interact(monkeypatch, IDENTITY + other, [False, True, True, False, True])
    assert run("setup") == 0
    cfg = config.load_all(home / config.USER_CONFIG)
    assert [i.id for i in cfg.identities] == ["work", "personal"]


def test_setup_existing_empty_config_can_be_left_unchanged(home, monkeypatch):
    target = write_config(home, "[default]\n")
    interact(monkeypatch, confirms=[False])
    assert run("setup") == 0
    assert target.read_text() == "[default]\n"


@pytest.mark.parametrize("interrupt", [EOFError(), KeyboardInterrupt()])
def test_setup_interrupt_writes_nothing(home, monkeypatch, interrupt):
    interact(monkeypatch, [interrupt], [True])
    assert run("setup") == 130
    assert not (home / config.USER_CONFIG).exists()
    assert not (home / ".config").exists()


def test_setup_declined_save_creates_no_files(home, monkeypatch):
    interact(monkeypatch, IDENTITY, [False, True, False, False])
    assert run("setup") == 1
    assert not (home / ".config").exists()


def test_setup_refuses_noninteractive_input(home, capsys):
    assert run("setup") == 2
    assert "interactive terminal" in capsys.readouterr().err
    assert not (home / config.USER_CONFIG).exists()


def test_setup_reprompts_invalid_input(home, monkeypatch):
    values = ["bad label", "work", "<bad>", "Jane", "bad email", "jane@corp.example", "jane@corp.test",
              "-host", "github.com", "bad owner", "org", ""]
    remaining = interact(monkeypatch, values, [False, True, False, True])
    assert run("setup") == 0
    assert all(not q for q in remaining)
    assert config.load_all(home / config.USER_CONFIG).identities[0].host == "github.com"


def test_setup_requires_confirmation_for_whole_host(home, monkeypatch):
    values = IDENTITY[:4] + ["", "org", ""]
    interact(monkeypatch, values, [False, True, False, False, True])
    assert run("setup") == 0
    assert config.load_all(home / config.USER_CONFIG).identities[0].owners == ["org"]


def test_setup_uses_existing_key_without_reading_private_contents(home, monkeypatch):
    key = home / ".ssh" / "id_work"
    key.parent.mkdir()
    key.write_bytes(b"not a real private key")
    (key.parent / "id_work.pub").write_text("ssh-ed25519 AAAA test\n")
    interact(monkeypatch, IDENTITY[:-1] + ["~/.ssh/id_work", "gitpro-work"],
             [False, True, True, False, True])
    original_open = Path.open

    def forbid_private_read(path, *args, **kwargs):
        assert path != key, "setup attempted to open the private key"
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "open", forbid_private_read)
        assert run("setup") == 0
    identity = config.load_all(home / config.USER_CONFIG).identities[0]
    assert identity.ssh_alias == "gitpro-work" and identity.sign is True
    assert key.read_bytes() == b"not a real private key"
    assert not (home / ".ssh" / "config").exists()


def test_setup_replaces_only_the_unchanged_example(home, monkeypatch):
    target = write_config(home, resources.files("gitpro").joinpath("example.toml").read_text())
    old = target.read_bytes()
    interact(monkeypatch, IDENTITY, [False, True, False, True])
    assert run("setup") == 0
    assert config.load_all(target).identities[0].email == "jane@corp.test"
    assert list(target.parent.glob("*.bak.*"))[0].read_bytes() == old


def test_setup_keeps_invalid_existing_config(home, monkeypatch):
    target = write_config(home, "[default]\nunknown = true\n")
    before = target.read_bytes()
    interact(monkeypatch)
    assert run("setup") == 2
    assert target.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "dangling-symlink", "directory", "fifo", "writable"])
def test_setup_refuses_unsafe_destinations(home, monkeypatch, kind):
    target = home / "config.toml"
    other = home / "other"
    if kind in ("symlink", "dangling-symlink"):
        if kind == "symlink":
            other.write_text(DEFAULT_CONFIG)
        target.symlink_to(other)
    elif kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_text(DEFAULT_CONFIG)
        target.chmod(0o666)
    interact(monkeypatch)
    assert run("setup", "-c", str(target)) == 2
    if kind == "symlink":
        assert other.read_text() == DEFAULT_CONFIG


@pytest.mark.parametrize("exists", [False, True])
def test_setup_refuses_concurrent_edits(home, monkeypatch, capsys, exists):
    target = home / config.USER_CONFIG
    if exists:
        write_config(home, DEFAULT_CONFIG)

    def change():
        target.parent.mkdir(exist_ok=True)
        target.write_text("# concurrent edit\n[default]\n")

    values = ["extra", *IDENTITY[1:]] if exists else IDENTITY
    interact(monkeypatch, values, ([True, False, True] if exists else [False, True, False, True]), change)
    assert run("setup") == 2
    assert "changed during setup" in capsys.readouterr().err
    assert target.read_text() == "# concurrent edit\n[default]\n"
    assert not target.with_name(target.name + ".setup.lock").exists()


def test_setup_custom_path_overrides_environment(home, monkeypatch):
    target = home / "with spaces" / "custom.toml"
    monkeypatch.setenv("GITPRO_CONFIG", str(home / "ignored.toml"))
    interact(monkeypatch, IDENTITY, [False, True, False, True])
    assert run("setup", "-c", str(target)) == 0
    assert config.load_all(target).identities
    assert not (home / "ignored.toml").exists()


def test_setup_uses_environment_config_path(home, monkeypatch):
    target = home / "from-env.toml"
    monkeypatch.setenv("GITPRO_CONFIG", str(target))
    interact(monkeypatch, IDENTITY, [False, True, False, True])
    assert run("setup") == 0
    assert config.load_all(target).identities
    assert not (home / config.USER_CONFIG).exists()


@pytest.mark.parametrize("rel", [".gitconfig", ".ssh/config", ".gitconfig-identities",
                                 ".config/git/allowed_signers", ".config/gitpro/hooks",
                                 ".ssh/config.d/git-identities", ".gitconfig/child.toml"])
def test_setup_validates_managed_output_collisions_before_saving(home, monkeypatch, capsys, rel):
    interact(monkeypatch, ["Me", "me@personal.test"], [True, False])
    assert run("setup", "-c", str(home / rel)) == 2
    assert "conflicts with managed output" in capsys.readouterr().err
    assert not (home / rel).exists()


def test_setup_retries_a_reserved_identity_label(home, monkeypatch):
    interact(monkeypatch, ["identities", *IDENTITY[1:]] + IDENTITY, [False, True, False, True])
    assert run("setup") == 0
    assert config.load_all(home / config.USER_CONFIG).identities[0].id == "work"


def test_setup_respects_an_existing_save_lock(home, monkeypatch, capsys):
    target = write_config(home, DEFAULT_CONFIG)
    lock = target.with_name(target.name + ".setup.lock")
    lock.write_text("another wizard")
    before = target.read_bytes()
    interact(monkeypatch, ["extra", *IDENTITY[1:]], [True, False, True])
    assert run("setup") == 2
    assert "another setup" in capsys.readouterr().err
    assert target.read_bytes() == before
    assert lock.read_text() == "another wizard"
    assert not list(target.parent.glob("*.bak.*"))


def test_setup_detects_symlink_replacement_after_backup(home, monkeypatch):
    target = write_config(home, DEFAULT_CONFIG)
    other = home / "untouched"
    other.write_bytes(b"must not read or overwrite")
    backup = files._backup

    def replace_with_link(path, data=None):
        result = backup(path, data)
        target.unlink()
        target.symlink_to(other)
        return result

    monkeypatch.setattr(files, "_backup", replace_with_link)
    interact(monkeypatch, ["extra", *IDENTITY[1:]], [True, False, True])
    assert run("setup") == 2
    assert target.is_symlink()
    assert other.read_bytes() == b"must not read or overwrite"
    assert list(target.parent.glob("*.bak.*"))[0].read_text() == DEFAULT_CONFIG


def test_setup_exclusive_create_protects_last_moment_race(home, monkeypatch):
    target = home / config.USER_CONFIG
    link = os.link

    def create_first(src, dst):
        Path(dst).write_bytes(b"a concurrent creator")
        return link(src, dst)

    monkeypatch.setattr(os, "link", create_first)
    interact(monkeypatch, IDENTITY, [False, True, False, True])
    assert run("setup") == 2
    assert target.read_bytes() == b"a concurrent creator"
    assert not target.with_name(target.name + ".setup.lock").exists()


def test_setup_failed_write_preserves_config_and_backup(home, monkeypatch):
    target = write_config(home, DEFAULT_CONFIG)
    before = target.read_bytes()
    interact(monkeypatch, ["extra", *IDENTITY[1:]], [True, False, True])

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(files, "atomic_write", fail)
    assert run("setup") == 2
    assert target.read_bytes() == before
    assert list(target.parent.glob("*.bak.*"))[0].read_bytes() == before
    assert not target.with_name(target.name + ".setup.lock").exists()


def test_setup_capability_and_shared_validation(home, capsys):
    assert run("--version", "--json") == 0
    assert "setup" in json.loads(capsys.readouterr().out)["capabilities"]
    target = write_config(home, DEFAULT_CONFIG)
    assert config.loads(DEFAULT_CONFIG, target) == config.load_all(target)


@pytest.mark.parametrize("finish, expected", [("y", 0), ("n", 1), ("eof", 130)])
def test_real_terminal_setup_and_git_identity_selection(home, tmp_path, finish, expected):
    master, slave = pty.openpty()
    proc = None
    output = bytearray()
    try:
        proc = subprocess.Popen([sys.executable, "-m", "gitpro", "setup"], cwd=home,
                                stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        slave = None
        answers = ["n", "y", "work", "Jane Doe", "jane@corp.test", "github.com", "my-org", "", "n", finish]
        os.write(master, b"\x04" if finish == "eof" else ("\n".join(answers) + "\n").encode())
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError as e:
                    if e.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
            elif proc.poll() is not None:
                break
        assert proc.wait(timeout=1) == expected, output.decode(errors="replace")
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=TIMEOUT)
        os.close(master)
        if slave is not None:
            os.close(slave)
    assert not (home / ".gitconfig").exists()
    if finish != "y":
        assert not (home / ".config").exists()
        return
    assert "Review your configuration" in output.decode()
    assert run("plan") == 0
    assert not (home / ".gitconfig").exists()
    assert run("apply", "-y") == 0
    work = repo_with_remote(tmp_path, "work", "git@github.com:my-org/app.git")
    personal = repo_with_remote(tmp_path, "personal", "git@github.com:someone-else/app.git")
    assert git(work, "config", "user.email") == "jane@corp.test"
    assert git(work, "config", "user.name") == "Jane Doe"
    assert git(work, "config", "commit.gpgsign") == "false"
    assert git(personal, "config", "user.email") == ""


@pytest.mark.parametrize("kind", ["regular", "symlink", "dangling"])
def test_exclusive_atomic_write_never_overwrites_an_entry(tmp_path, kind):
    target, other = tmp_path / "target", tmp_path / "other"
    if kind == "regular":
        target.write_bytes(b"old")
    else:
        if kind == "symlink":
            other.write_bytes(b"old")
        target.symlink_to(other)
    with pytest.raises(FileExistsError):
        files.atomic_write(target, b"new", 0o600, overwrite=False)
    if kind == "regular":
        assert target.read_bytes() == b"old"
    else:
        assert target.is_symlink()
        if kind == "dangling":
            assert not other.exists()
        else:
            assert other.read_bytes() == b"old"
    assert not list(tmp_path.glob(".target.*"))
