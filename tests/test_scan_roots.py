"""Package 02: saved scan roots and excludes (spec 02 §10, tests 1-14)."""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG, git, repo_with_remote, run, write_config

from gitpro import config, scan, scanroots


def scan_json(capsys, *argv: str) -> dict:
    capsys.readouterr()
    assert run("scan", "--json", *argv) == 0
    return json.loads(capsys.readouterr().out)


def roots_config(home: Path, roots=None, exclude=None, extra: str = "") -> Path:
    body = "[scan]\n"
    if roots is not None:
        body += f"roots = {json.dumps([str(r) for r in roots])}\n"
    if exclude is not None:
        body += f"exclude = {json.dumps([str(e) for e in exclude])}\n"
    return write_config(home, DEFAULT_CONFIG + extra + "\n" + body)


def listed(rep: dict) -> set:
    """Paths of every repo the scan inspected (all of them have a repo-local user.email here)."""
    return {o["path"] for o in rep["overrides"]}


def stale(parent: Path, name: str) -> Path:
    """A repo whose own .git/config sets user.email, so the scan report lists it under overrides."""
    repo = repo_with_remote(parent, name, "git@github.com:someone/app.git")
    git(repo, "config", "user.email", "old@x.test")
    return repo


def test_roots_from_config(home, tmp_path, capsys):  # 1
    a, b = stale(tmp_path / "a", "r1"), stale(tmp_path / "b", "r2")
    roots_config(home, [tmp_path / "a", tmp_path / "b"])
    rep = scan_json(capsys)
    assert rep["roots_source"] == "config"
    assert rep["roots"] == [str(tmp_path / "a"), str(tmp_path / "b")]
    assert listed(rep) == {str(a), str(b)}
    assert rep["exclude"] == [] and rep["skipped_excluded"] == 0


def test_positional_dir_wins_and_footer(home, tmp_path, capsys):  # 2
    stale(tmp_path / "a", "r1")
    b = stale(tmp_path / "b", "r2")
    path = roots_config(home, [tmp_path / "a"])
    rep = scan_json(capsys, str(tmp_path / "b"))
    assert rep["roots_source"] == "args" and rep["roots"] == [str(tmp_path / "b")]
    assert listed(rep) == {str(b)}
    assert run("scan", str(tmp_path / "b")) == 0
    out = capsys.readouterr().out
    assert out.count("[scan] roots = [") == 1
    assert f"→ to keep these roots: add to ~/{path.relative_to(home)}: [scan] roots = " \
           f"{json.dumps([str(tmp_path / 'b')])}" in out
    # no config: nothing to suggest adding to
    path.unlink()
    assert run("scan", str(tmp_path / "b")) == 0
    assert "[scan] roots" not in capsys.readouterr().out


def test_default_root_without_config(home, capsys):  # 3
    rep = scan_json(capsys)
    assert rep["roots"] == [str(home)] and rep["roots_source"] == "default"


def test_exclude_glob_any_depth(home, tmp_path, capsys):  # 4
    stale(tmp_path / "a/vendor", "x")
    keep = stale(tmp_path / "a", "keep")
    roots_config(home, [tmp_path / "a"], ["**/vendor"])
    rep = scan_json(capsys)
    assert listed(rep) == {str(keep)}
    assert rep["skipped_excluded"] == 1 and rep["exclude"] == ["**/vendor"]
    assert run("scan") == 0
    head = capsys.readouterr().out.splitlines()[0]
    assert head.endswith("hidden directories, node_modules and 1 excluded dir skipped")


def test_hidden_dirs_are_not_counted(home, capsys):  # 5
    stale(home / ".cache", "r")
    roots_config(home, exclude=["~/.cache"])
    rep = scan_json(capsys)
    assert rep["skipped_excluded"] == 0 and rep["repos"] == 0
    assert rep["roots_source"] == "config" and rep["roots"] == [str(home)]
    assert run("scan") == 0
    assert "hidden directories and node_modules skipped" in capsys.readouterr().out


def test_absolute_pattern_is_anchored(home, tmp_path, capsys):  # 6
    stale(tmp_path / "a/old-2024", "r")
    deep = stale(tmp_path / "a/x/old", "r")
    roots_config(home, [tmp_path / "a"], [f"{tmp_path}/a/old*"])
    rep = scan_json(capsys)
    assert listed(rep) == {str(deep)} and rep["skipped_excluded"] == 1


def test_repo_flag_ignores_excludes(home, tmp_path, capsys):  # 7
    r = stale(tmp_path / "a/vendor", "x")
    roots_config(home, [tmp_path / "a"], ["**/vendor"])
    rep = scan_json(capsys, "--repo", str(r))
    assert listed(rep) == {str(r)} and rep["skipped_excluded"] == 0


def test_missing_roots(home, tmp_path, capsys):  # 8
    r = stale(tmp_path / "a", "r")
    roots_config(home, [tmp_path / "a", tmp_path / "gone"])
    capsys.readouterr()
    assert run("scan", "--json") == 0
    cap = capsys.readouterr()
    assert f"⚠ root does not exist: {tmp_path / 'gone'}" in cap.err
    rep = json.loads(cap.out)
    assert listed(rep) == {str(r)} and rep["roots"] == [str(tmp_path / "a")]

    roots_config(home, [tmp_path / "gone", tmp_path / "gone2"])
    assert run("scan") == 2
    assert f"✗ no scan root exists (looked at: {tmp_path / 'gone'}, {tmp_path / 'gone2'})" \
        in capsys.readouterr().err
    assert run("scan", "--json") == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "no scan root exists" in out["error"]


def test_excluded_root_is_skipped(home, tmp_path, capsys):
    stale(tmp_path / "a", "r")
    b = stale(tmp_path / "b", "r")
    roots_config(home, [tmp_path / "a", tmp_path / "b"], [f"{tmp_path}/a"])
    capsys.readouterr()
    assert run("scan", "--json") == 0
    cap = capsys.readouterr()
    assert f"⚠ root {tmp_path / 'a'} is excluded by '{tmp_path}/a'; skipped" in cap.err
    assert listed(json.loads(cap.out)) == {str(b)}


def test_roots_flag(home, tmp_path, capsys):  # 9
    (tmp_path / "a").mkdir()
    path = roots_config(home, ["~", tmp_path / "a"], ["~/.cache", "**/node_modules", "~/go/pkg/mod"])
    capsys.readouterr()
    assert run("scan", "--roots") == 0
    out = capsys.readouterr().out
    assert out == (f"roots (from ~/{path.relative_to(home)}):\n  ~\n  {tmp_path / 'a'}\n"
                   "exclude:\n  ~/.cache\n  **/node_modules  (always)\n  ~/go/pkg/mod\n"
                   "  **/.*  (always: hidden directories)\n")
    assert run("scan", "--roots", "--json") == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep == {"ok": True, "command": "scan", "roots": [str(home), str(tmp_path / "a")],
                   "exclude": ["~/.cache", "**/node_modules", "~/go/pkg/mod"], "source": "config",
                   "config": str(path), "missing": [], "written": ["~", str(tmp_path / "a")]}

    path.unlink()
    assert run("scan", "--roots") == 0
    out = capsys.readouterr().out
    assert out.startswith("roots (default):\n  ~\n") and "**/node_modules  (always)" in out
    assert run("scan", "--roots", str(tmp_path / "a"), str(tmp_path / "gone")) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"roots (from command line):\n  {tmp_path / 'a'}\n  {tmp_path / 'gone'}  (missing)\n")
    assert run("scan", "--roots", "--json") == 0
    assert json.loads(capsys.readouterr().out)["source"] == "default"


def test_config_flag(home, tmp_path, capsys):  # 10
    r = stale(tmp_path / "a", "r")
    other = tmp_path / "other.toml"
    other.write_text(DEFAULT_CONFIG + f'\n[scan]\nroots = ["{tmp_path}/a"]\n')
    other.chmod(0o600)
    rep = scan_json(capsys, "-c", str(other))
    assert rep["roots_source"] == "config" and listed(rep) == {str(r)}
    assert run("scan", "--home", str(home), "--config", str(other), "--json") == 0  # --home is accepted


def test_scan_right_after_init(home, tmp_path, capsys):  # 11
    path = tmp_path / "fresh.toml"
    assert run("init", str(path)) == 0
    capsys.readouterr()
    assert run("scan", "-c", str(path)) == 0
    assert "✗" not in capsys.readouterr().err
    rep = scan_json(capsys, "-c", str(path))
    assert rep["roots"] == [str(home)]


def test_lenient_and_bad_scan_tables(home, tmp_path, capsys):  # 12
    r = stale(tmp_path / "a", "r")
    broken = DEFAULT_CONFIG.replace('email = "me@corp.test"\n', "")
    path = write_config(home, broken + f'\n[scan]\nroots = ["{tmp_path}/a"]\n')
    rep = scan_json(capsys)
    assert rep["roots_source"] == "config" and listed(rep) == {str(r)}

    path = write_config(home, DEFAULT_CONFIG + '\n[scan]\nroots = "x"\n')
    capsys.readouterr()
    assert run("scan", "--json") == 0
    cap = capsys.readouterr()
    assert cap.err.count("⚠") == 1
    assert f"⚠ ~/{path.relative_to(home)}: [scan]: roots must be a list; scanning ~" in cap.err
    rep = json.loads(cap.out)
    assert rep["roots"] == [str(home)] and rep["roots_source"] == "default"

    write_config(home, "[default\n")
    assert run("scan", "--json") == 0
    assert "; scanning ~" in capsys.readouterr().err


def test_find_max_depth(tmp_path):  # 13
    one = repo_with_remote(tmp_path / "root", "one", None)
    two = repo_with_remote(tmp_path / "root/x", "two", None)
    assert scan.find([tmp_path / "root"], max_depth=1) == [one]
    assert scan.find([tmp_path / "root"], max_depth=2) == [one, two]
    assert scan.find([tmp_path / "root"]) == [one, two]
    assert scan.find([one], max_depth=0) == [one]


def test_find_exclude_collects(home, tmp_path):
    keep = repo_with_remote(tmp_path / "root", "keep", None)
    repo_with_remote(tmp_path / "root/node_modules", "nm", None)
    repo_with_remote(tmp_path / "root/vendor", "v", None)
    vendor_repo = repo_with_remote(tmp_path / "root", "vendor2", None)
    seen: set = set()
    found = scan.find([tmp_path / "root"], exclude=("vendor", "/nowhere/*"), excluded=seen)
    assert found == [keep, vendor_repo]
    assert seen == {tmp_path / "root/vendor"}


def test_exclude_normalisation(home):
    me = str(home)
    assert scan.excluded_by(Path(me + "/.cache"), ["~/.cache"]) == "~/.cache"
    assert scan.excluded_by(Path(me + "/.cache2"), ["~/.cache"]) is None
    assert scan.excluded_by(Path(me + "/x/.cache"), ["~/.cache"]) is None
    assert scan.excluded_by(Path("/srv/git/a/vendor"), ["**/vendor"]) == "**/vendor"
    assert scan.excluded_by(Path(me + "/vendor"), ["**/vendor"]) == "**/vendor"
    assert scan.excluded_by(Path("/srv/git/vendor-x"), ["**/vendor"]) is None
    assert scan.excluded_by(Path("/a/b/node_modules"), ["node_modules"]) == "node_modules"
    assert scan.excluded_by(Path("/mnt/d/work/old"), ["/mnt/d/work/old*"]) == "/mnt/d/work/old*"
    assert scan.excluded_by(Path("/mnt/d/work/old-2024"), ["/mnt/d/work/old*"]) == "/mnt/d/work/old*"
    assert scan.excluded_by(Path("/mnt/d/work/x/old"), ["/mnt/d/work/old*"]) is None
    assert scan.excluded_by(Path(me + "/.cache/x"), ["~/.cache/**"]) == "~/.cache/**"
    assert scan.excluded_by(Path(me + "/go/pkg/mod"), ["~/go/pkg/mod/"]) == "~/go/pkg/mod/"


def test_effective_roots(home, tmp_path):
    s = config.Scan(roots=("~", "/srv/git", "~/src"), exclude=("**/vendor",))
    assert scan.effective_roots([], s) == ([home, Path("/srv/git"), home / "src"], ["**/vendor"], "config")
    assert scan.effective_roots([str(tmp_path)], s) == ([tmp_path], ["**/vendor"], "args")
    assert scan.effective_roots(["~/x", "~/x"], None) == ([home / "x"], [], "args")
    assert scan.effective_roots([], None) == ([home], [], "default")


def test_derive_id():
    assert scan.derive_id("Corp_Org", "github.com", set()) == "corp_org"
    assert scan.derive_id("me", "gitlab.com", {"me"}) == "me-gitlab"
    assert scan.derive_id("a b", "github.com", {"a-b", "a-b-github"}) == "a-b-github-github"
    taken = {"x"}
    scan.derive_id("y", "github.com", taken)
    assert taken == {"x"}  # the caller adds what it uses


def test_capability(home, capsys):
    assert run("--version", "--json") == 0
    assert "scan.roots" in json.loads(capsys.readouterr().out)["capabilities"]


# QA fixes: [scan] roots get their own check (not PATH_RE), excludes follow symlinked roots.

@pytest.mark.parametrize("root", ["/data/w/sp ace", "~/Ünïcode", "/mnt/c/Users/First Last", "/", "~", "~/"])
def test_scan_roots_accept_any_printable_path(root):
    assert config.scan_root_error(root) is None
    assert config._scan({"roots": [root]}).roots == (root,)


@pytest.mark.parametrize("root,why", [
    ("~/a/../b", 'root "~/a/../b" must not contain a .. segment'),
    ("/x/..", 'root "/x/.." must not contain a .. segment'),
    ("/a\tb", 'root "/a\\tb" contains a control character'),
    ("/a\x7fb", 'root "/a\\u007fb" contains a control character'),
    ("src", 'roots must start with ~/ or / (or be ~): "src"'),
    ("~user/x", 'roots must start with ~/ or / (or be ~): "~user/x"'),
])
def test_scan_roots_rejected_with_the_reason(root, why):
    assert config.scan_root_error(root) == why
    with pytest.raises(config.ConfigError) as e:
        config._scan({"roots": [root]})
    if "control" in why:  # the generic check on every string value may get there first
        assert "control character" in str(e.value)
    else:
        assert str(e.value) == f"[scan]: {why}"


def test_root_with_space_and_unicode_is_scanned(home, tmp_path, capsys):
    a, b = stale(tmp_path / "sp ace", "r"), stale(home / "Ünïcode", "r")
    roots_config(home, [tmp_path / "sp ace", "~/Ünïcode"])
    capsys.readouterr()
    assert run("scan", "--json") == 0
    cap = capsys.readouterr()
    assert "⚠" not in cap.err
    rep = json.loads(cap.out)
    assert rep["roots_source"] == "config" and listed(rep) == {str(a), str(b)}


def test_empty_roots_warns_and_scans_home(home, tmp_path, capsys):
    path = roots_config(home, [], ["**/vendor"])
    capsys.readouterr()
    assert run("scan", "--json") == 0
    cap = capsys.readouterr()
    assert f"⚠ ~/{path.relative_to(home)}: [scan] roots is empty; scanning ~" in cap.err
    rep = json.loads(cap.out)
    assert rep["roots"] == [str(home)] and rep["roots_source"] == "default" and rep["exclude"] == ["**/vendor"]


def test_hint_lists_only_usable_and_new_roots(home, tmp_path, capsys):
    a = tmp_path / "a"
    stale(a, "r")
    odd = tmp_path / "x\ty"
    odd.mkdir()
    path = roots_config(home, [a])
    capsys.readouterr()
    assert run("scan", str(a)) == 0  # the configured roots already: nothing to add
    assert "[scan] roots" not in capsys.readouterr().out
    assert run("scan", str(odd)) == 0  # [scan] would refuse it: no hint
    assert "[scan] roots" not in capsys.readouterr().out
    assert run("scan", str(odd), str(tmp_path / "gone"), str(home)) == 0  # missing and odd roots left out
    out = capsys.readouterr().out
    assert f"add to ~/{path.relative_to(home)}: [scan] roots = [\"~\"]" in out


def test_toml_snippet_parses(home):
    roots = [home, home / "Ünï code", Path("/a\"q\\b"), Path("/del\x7fx"), Path("/tab\tx")]
    got = config._toml.loads("[scan]\nroots = " + scanroots.toml_list(roots) + "\n")
    assert got["scan"]["roots"] == ["~", "~/Ünï code", "/a\"q\\b", "/del\x7fx", "/tab\tx"]


def test_exclude_under_symlinked_root(home, tmp_path, capsys):
    real = tmp_path / "real"
    keep, old = stale(real, "keep"), stale(real / "old", "r")
    (home / "work").symlink_to(real)
    path = roots_config(home, ["~/work"], ["~/work/old"])
    rep = scan_json(capsys)
    assert rep["roots"] == [str(real)] and listed(rep) == {str(keep)} and rep["skipped_excluded"] == 1
    assert old not in [Path(p) for p in listed(rep)]

    assert run("scan", "--roots") == 0
    assert f"roots (from ~/{path.relative_to(home)}):\n  ~/work → {real}\n" in capsys.readouterr().out
    assert run("scan", "--roots", "--json") == 0
    assert json.loads(capsys.readouterr().out)["written"] == ["~/work"]

    other = stale(tmp_path / "other", "r")
    roots_config(home, ["~/work", str(tmp_path / "other")], ["~/work"])
    capsys.readouterr()
    assert run("scan", "--json") == 0
    cap = capsys.readouterr()
    assert f"⚠ root {real} is excluded by '~/work'; skipped" in cap.err
    assert listed(json.loads(cap.out)) == {str(other)}


def test_exclude_prefix_is_resolved_and_escaped(tmp_path):
    (tmp_path / "t[1]").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "t[1]")
    norm = scan._normalise(f"{tmp_path}/link/old*", "/unused")
    assert fnmatch.fnmatchcase(f"{tmp_path}/t[1]/old-2024", norm)
    assert not fnmatch.fnmatchcase(f"{tmp_path}/t1/old-2024", norm)
    home_norm = scan._normalise("~/x", "/no/such/h[1]")
    assert fnmatch.fnmatchcase("/no/such/h[1]/x", home_norm) and not fnmatch.fnmatchcase("/no/such/h1/x", home_norm)
    assert scan._normalise("/", "/h") == "/" and scan._normalise("/*", "/h") == "/*"
    assert os.path.realpath("/") == "/"
