"""lint.warnings: advisory checks (spec 18: AWS CodeCommit's SSH user)."""
import pytest

from gitpro import lint
from gitpro.config import Default, Identity
from gitpro.plan import Env

CC = "git-codecommit.us-east-2.amazonaws.com"


def ident(host=CC, **kw):
    return Identity(id="cc", name="N", email="cc@x.test", host=host, owners=["v1/repos"], **kw)


def warn(tmp_path, i):
    return [w for w in lint.warnings(Default(), [i], Env(home=tmp_path)) if "CodeCommit" in w]


def test_codecommit_default_ssh_user_with_key(tmp_path):
    assert warn(tmp_path, ident(ssh_key="~/.ssh/id_cc")) == [
        "identity 'cc': CodeCommit's SSH user is the SSH key ID, not git; set ssh_user to it"]


def test_codecommit_default_ssh_user_with_alias(tmp_path):
    assert len(warn(tmp_path, ident(ssh_alias="cc-alias"))) == 1


def test_codecommit_host_case_insensitive(tmp_path):
    assert len(warn(tmp_path, ident(host=CC.upper(), ssh_key="~/.ssh/id_cc"))) == 1


@pytest.mark.parametrize("i", [
    ident(ssh_key="~/.ssh/id_cc", ssh_user="APKAEIBAERJR2EXAMPLE"),
    ident(),                                     # https only
    ident(host="github.com", ssh_key="~/.ssh/id_cc"),
])
def test_no_codecommit_warning(tmp_path, i):
    assert warn(tmp_path, i) == []
