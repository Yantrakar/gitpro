import pytest

from gitpro import render
from gitpro.config import ConfigError, Identity


def test_with_block_is_idempotent_and_placed():
    body = "[include]\n\tpath = x"
    once = render.with_block("[core]\n\teditor = vim\n", body, first=False, where="f")
    assert once.startswith("[core]") and once.endswith("# END gitpro\n")
    assert render.with_block(once, body, first=False, where="f") == once

    top = render.with_block("Host a\n", "Include y", first=True, where="f")
    assert top.startswith("# BEGIN gitpro") and top.endswith("Host a\n")
    assert render.with_block(top, "Include y", first=True, where="f") == top


def test_strip_block_undoes_with_block():
    for original in ("", "[core]\n\teditor = vim\n"):
        for first in (True, False):
            added = render.with_block(original, "body", first=first, where="f")
            assert render.strip_block(added, "f") == original


def test_unpaired_marker_is_refused():
    with pytest.raises(ConfigError, match="unpaired"):
        render.with_block("# BEGIN gitpro\nmine\n", "body", first=False, where="f")


def test_url_patterns():
    i = Identity(id="w", name="n", email="e@x.test", host="github.com", owners=["org"], ssh_alias="gh-w")
    assert render.url_patterns(i) == [
        "git@github.com:org/**", "ssh://git@github.com/org/**",
        "git@gh-w:org/**", "ssh://git@gh-w/org/**",
        "https://github.com/org/**",
    ]
    whole_host = Identity(id="w", name="n", email="e@x.test", host="git.corp.test")
    assert "https://git.corp.test/**" in render.url_patterns(whole_host)


def test_gval_escapes():
    assert render.gval('a "b" \\c') == '"a \\"b\\" \\\\c"'
    assert render.gval(True) == "true"


def test_extra_git_config():
    lines = render.git_lines({"credential": {"https://github.com": {"helper": ["", "!gh auth git-credential"]}},
                              "pull": {"ff": "only"}}, "w")
    assert lines == ['[credential "https://github.com"]', '\thelper = ""', '\thelper = "!gh auth git-credential"',
                     "[pull]", '\tff = "only"']
    with pytest.raises(ConfigError, match="bad git key"):
        render.git_lines({"core": {"bad key": 1}}, "w")
