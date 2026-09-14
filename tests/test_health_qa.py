"""QA (package 09 review): adversarial cases. Fake ssh/ssh-keygen/ssh-add/gh/glab only."""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

from conftest import run
import test_health
from test_health import (AUTH_CONFIG, HI, NOPASS_CONFIG, UPLOAD_CONFIG, auth_json, by_name, health_json, set_pub,
                         setup, ssh_says, with_gh)


@pytest.fixture
def fakes(home, fake_bin, monkeypatch, tmp_path):
    for v in ("GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITLAB_TOKEN", "SSH_AUTH_SOCK"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.chdir(tmp_path)
    fake_bin.add("ssh-keygen", test_health.KEYGEN)
    fake_bin.add("ssh-add", test_health.SSH_ADD)
    fake_bin.add("ssh", test_health.SSH)
    (fake_bin.path / "ssh.json").write_text("{}")
    return fake_bin


GLAB = r'''
here = os.path.dirname(os.path.abspath(sys.argv[0]))
who = open(os.path.join(here, "glab_user")).read().strip()
keys = os.path.join(here, "glab_keys.json")
if argv[:2] == ["auth", "status"]:
    sys.stderr.write("gitlab.com\n  Logged in to gitlab.com as %s (keyring)\n" % who)
    sys.exit(0)
if argv[:2] == ["ssh-key", "list"]:
    if "--output" in argv:
        sys.stdout.write(open(keys).read() if os.path.exists(keys) else "[]")
        sys.exit(0)
    sys.exit(0)
if argv[:2] == ["ssh-key", "add"]:
    sys.exit(0)
sys.exit(1)
'''

GLAB_CONFIG = """\
[default]
name = "Default Me"
email = "me@default.test"

[[identity]]
id = "gl-work"
name = "Work Me"
email = "me@corp.test"
host = "gitlab.com"
owners = ["corp"]
ssh_key = "~/.ssh/id_work"
glab_account = "me-work"
"""


def _glab(fakes, user):
    fakes.add("glab", GLAB)
    (fakes.path / "glab_user").write_text(user)


def test_qa_glab_upload_never_goes_to_another_glab_login(home, cfg, fakes, capsys):
    # glab has no per-login selection: forge.run(glab) acts as whoever glab is logged in as on the host.
    setup(cfg, home, GLAB_CONFIG)
    _glab(fakes, "me-personal")
    run("keys", "--upload", "--online", "-y")
    capsys.readouterr()
    adds = [c for c in fakes.calls("glab") if c[:2] == ["ssh-key", "add"]]
    assert adds == [], "uploaded me-work's key while glab is logged in as me-personal"


def test_qa_glab_registered_check_not_attributed_to_wrong_login(home, cfg, fakes, capsys):
    setup(cfg, home, GLAB_CONFIG)
    _glab(fakes, "me-personal")
    mat = (home / ".ssh/id_work.pub").read_text().split()
    (fakes.path / "glab_keys.json").write_text(json.dumps(
        [{"id": 5, "key": f"{mat[0]} {mat[1]}", "usage_type": "auth_and_signing"}]))
    _rc, data, _err = health_json(capsys, "--online")
    reg = by_name(data)["id_work"]["registered"]
    assert reg is None or reg.get("account") != "me-work" or reg.get("auth") is not True, \
        "me-personal's key list reported as me-work's"


def test_qa_auth_pins_strict_host_key_checking(home, cfg, fakes, capsys):
    # a user ssh config with StrictHostKeyChecking accept-new/no must not make auth trust an unknown host
    setup(cfg, home, AUTH_CONFIG)
    ssh_says(fakes, {"git@github-work": [1, HI]})
    auth_json(capsys, "--identity", "work-github")
    argv = fakes.calls("ssh")[-1]
    assert "StrictHostKeyChecking=yes" in argv


def test_qa_upload_confirm_with_markup_in_comment(home, cfg, fakes, capsys, monkeypatch):
    setup(cfg, home, UPLOAD_CONFIG)
    set_pub(home, "id_work", comment="me@laptop [/]")     # rich markup in the key comment / title
    with_gh(fakes, "")
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    rc = run("keys", "--upload", "--online", "--identity", "work-github")
    assert rc == 1 and [c for c in fakes.calls("gh") if c[:2] == ["ssh-key", "add"]] == []


def test_qa_probe_stdin_is_devnull_even_when_ours_is_a_pipe(home, cfg, fakes, capsys):
    # pytest's fd capture already makes fd 0 /dev/null, so test_passphrase_probe cannot see a missing DEVNULL
    setup(cfg, home, NOPASS_CONFIG)
    r, w = os.pipe()
    saved = os.dup(0)
    os.dup2(r, 0)
    try:
        run("keys", "--health")
    finally:
        os.dup2(saved, 0)
        for fd in (saved, r, w):
            os.close(fd)
    capsys.readouterr()
    log = (fakes.path / "keygen-y.log").read_text().splitlines()
    assert log and all(line.endswith("devnull=True") for line in log)
