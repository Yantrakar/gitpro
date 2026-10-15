from pathlib import Path

import pytest

from conftest import write_config
from gitpro import config

GOOD_DEFAULT = '[default]\nname = "Me"\nemail = "me@home.test"\n'
IDENTITY = '[[identity]]\nid = "w"\nname = "Me"\nemail = "me@corp.test"\nhost = "git.corp.test"\n'


def load(home: Path, text: str):
    return config.load(write_config(home, text))


def test_minimal_config(home):
    d, ids = load(home, GOOD_DEFAULT + IDENTITY)
    assert d.email == "me@home.test"
    assert ids[0].file == "~/.gitconfig-w" and ids[0].owners == []


def test_sourcehut_owner_is_allowed(home):
    _, ids = load(home, GOOD_DEFAULT + IDENTITY.replace("git.corp.test", "git.sr.ht") + 'owners = ["~me", "~me/sub"]\n')
    assert ids[0].owners == ("~me", "~me/sub")


def test_default_without_person_is_allowed(home):
    d, _ = load(home, "[default]\n")
    assert d.name is None and d.email is None


@pytest.mark.parametrize("extra, message", [
    ('owners = ["-oProxyCommand=x"]', "bad owner"),
    ('owners = ["~"]', "bad owner"),
    ('owners = ["~-x"]', "bad owner"),
    ('owners = ["~~me"]', "bad owner"),
    ('owners = ["grp/~me"]', "bad owner"),
    ('owners = ["me~"]', "bad owner"),
    ('ssh_key = "~/.ssh/key with space"', "must start with"),
    ('ssh_key = "~/../etc/key"', "no '..'"),
    ('file = "relative/path"', "must start with"),
    ('ssh_port = 70000', "bad ssh_port"),
    ('ssh_alias = "alias"', "ssh_alias needs ssh_key"),
    ('emial = "typo@corp.test"', "unknown key(s) emial"),
    ('rewrite = "yes"', "must be true or false"),
])
def test_rejects_bad_identity_values(home, extra, message):
    with pytest.raises(config.ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        load(home, GOOD_DEFAULT + IDENTITY + extra + "\n")


@pytest.mark.parametrize("text, message", [
    ("", "missing \\[default\\]"),
    ('[default]\nname = "Me"\n', "bad or missing email"),
    ('[default]\nname = "Me"\nemail = "me@corp.example"\n', "placeholder"),
    ('[default]\nsign = true\n', "sign needs ssh_key and email"),
    ('[default]\nssh_hosts = ["github.com"]\n', "ssh_hosts needs ssh_key"),
    ('[default]\n[[identity]]\nid = "w"\n', "missing name, email, host"),
    (GOOD_DEFAULT + IDENTITY + IDENTITY, "duplicate ids"),
    (GOOD_DEFAULT + IDENTITY.replace("git.corp.test", "gitlab.corp.example"), "placeholder"),
    (GOOD_DEFAULT + IDENTITY.replace('"Me"', '"Me\\n"'), "control characters"),
    ('[default]\n[identity]\nid = "w"\n', "\\[\\[identity\\]\\]"),
    ("[default\n", "Expected"),
])
def test_rejects_bad_configs(home, text, message):
    with pytest.raises(config.ConfigError, match=message):
        load(home, text)


def test_rejects_writable_config(home):
    path = write_config(home, GOOD_DEFAULT, mode=0o664)
    with pytest.raises(config.ConfigError, match="not group/world-writable"):
        config.load(path)


def test_find_order(home, tmp_path, monkeypatch):
    with pytest.raises(config.ConfigError, match="gitpro setup"):
        config.find(None, home)
    user = write_config(home, GOOD_DEFAULT)
    assert config.find(None, home) == (user, "user config")
    monkeypatch.setenv(config.ENV_VAR, str(tmp_path / "env.toml"))
    assert config.find(None, home)[1] == f"${config.ENV_VAR}"
    assert config.find(tmp_path / "x.toml", home) == (tmp_path / "x.toml", "--config")
