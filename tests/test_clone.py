"""clone: clone with the identity, ssh alias and folder the URL calls for (spec 07). Local repos only, never the network."""
import json
import os
import signal
import stat
import subprocess
import sys
import time

import pytest

from conftest import DEFAULT_CONFIG, git, run
from gitpro import clone, config

WORK = DEFAULT_CONFIG.replace('gh_account = "me-work"\n',
                              'gh_account = "me-work"\nssh_alias = "github-work"\nclone_dir = "~/work"\n')
LAB = """
[[identity]]
id = "lab"
name = "Lab Me"
email = "me@lab.test"
host = "gitlab.corp.test"
owners = ["team"]
ssh_key = "~/.ssh/id_lab"
ssh_alias = "lab-alias"
ssh_user = "gitlab"
"""
SRC = '\n[clone]\ndir = "~/src"\n'
NO_DEFAULT_EMAIL = DEFAULT_CONFIG.replace('name = "Default Me"\nemail = "me@default.test"\n', "")


def load(path):
    return config.load_all(path, "test")


def out(capsys, *argv):
    rc = run("clone", *argv)
    c = capsys.readouterr()
    return rc, c.out, c.err


def seeded(bare_remote, tmp_path, name="upstream"):
    """A bare repo with one commit holding README.md."""
    bare = bare_remote(name)
    work = tmp_path / f"seed-{name}"
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "README.md").write_text("hello\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="Seed", GIT_AUTHOR_EMAIL="seed@x.test",
               GIT_COMMITTER_NAME="Seed", GIT_COMMITTER_EMAIL="seed@x.test")
    for argv in (["add", "README.md"], ["commit", "-q", "--no-gpg-sign", "-m", "seed"],
                 ["push", "-q", str(bare), "HEAD:refs/heads/main"]):
        subprocess.run(["git", "-C", str(work), *argv], check=True, env=env, capture_output=True)
    subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    return bare


def spy_clone(monkeypatch):
    """Records the argv of every `git clone` gitpro starts (run() uses subprocess.Popen)."""
    seen = []
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, argv, *a, **kw):
            if list(argv[:2]) == ["git", "clone"]:
                seen.append(list(argv))
            super().__init__(argv, *a, **kw)
    monkeypatch.setattr(subprocess, "Popen", Spy)
    return seen


@pytest.fixture
def no_ssh_env(monkeypatch):
    for var in ("GIT_SSH_COMMAND", "GIT_SSH", "GIT_SSH_VARIANT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


# ---------------------------------------------------------------- plan (pure)

def test_plan_rewrites_to_alias_and_picks_dir(cfg, home):  # 1
    p = clone.plan("https://github.com/corp-org/app", load(cfg(WORK, apply=False)))
    assert p.identity.id == "work-github"
    assert p.clone_url == "git@github-work:corp-org/app.git"
    assert p.dir == home / "work/github.com/corp-org/app"
    assert p.auth == "ssh-alias"
    lines = clone.preview(p, home)
    assert lines[0] == "clone git@github-work:corp-org/app.git"
    assert "  into      ~/work/github.com/corp-org/app" in lines
    assert any(ln.startswith("  identity  work-github  Work Me <me@corp.test>") and "corp-org on github.com" in ln
               for ln in lines)
    assert "  ssh       alias github-work → github.com, key ~/.ssh/id_work" in lines


def test_plan_https_kept_unless_ssh(cfg, home):  # 2
    c = load(cfg(apply=False))
    p = clone.plan("https://github.com/corp-org/app", c)
    assert p.identity.id == "work-github"
    assert p.clone_url == "https://github.com/corp-org/app"
    assert p.auth == "https"
    assert any("https (credential helper)" in ln and "add --ssh to use key ~/.ssh/id_work" in ln
               for ln in clone.preview(p, home))
    p = clone.plan("https://github.com/corp-org/app", c, ssh=True)
    assert p.clone_url == "git@github.com:corp-org/app.git"
    assert p.auth == "ssh"


def test_plan_alias_uses_ssh_user(cfg):  # 2
    c = load(cfg(DEFAULT_CONFIG + LAB, apply=False))
    p = clone.plan("https://gitlab.corp.test/team/sub/tool.git", c)
    assert p.identity.id == "lab"
    assert p.clone_url == "gitlab@lab-alias:team/sub/tool.git"
    assert clone.plan("gitlab@gitlab.corp.test:team/x.git", c).clone_url == "gitlab@lab-alias:team/x.git"


def test_rewritten_url_still_matches_the_identity(cfg):
    """The stored remote url is what git matches includeIf against: it must give the same identity."""
    from gitpro import resolve
    c = load(cfg(WORK + LAB, apply=False))
    for url in ("https://github.com/corp-org/app", "git@github.com:corp-org/app.git",
                "ssh://git@github.com/corp-org/app.git", "https://gitlab.corp.test/team/x"):
        p = clone.plan(url, c)
        assert resolve.match_url(p.clone_url, c)[0] == p.identity, url


def test_alias_with_port_keeps_scp_form(cfg):
    """The alias' ssh Host block carries the port; ssh://alias:port would match none of the identity's patterns."""
    from gitpro import resolve
    c = load(cfg(WORK.replace('ssh_alias = "github-work"\n', 'ssh_alias = "github-work"\nssh_port = 2222\n'),
                 apply=False))
    p = clone.plan("https://github.com/corp-org/app", c)
    assert p.clone_url == "git@github-work:corp-org/app.git"
    assert resolve.match_url(p.clone_url, c)[0].id == "work-github"
    p = clone.plan("https://github.com/corp-org/app", c, no_rewrite=True, ssh=True)
    assert p.clone_url == "ssh://git@github.com:2222/corp-org/app.git"
    assert resolve.match_url(p.clone_url, c)[0].id == "work-github"


def test_no_rewrite_and_forced_identity(cfg, home):  # 3
    c = load(cfg(WORK.replace('ssh_key = "~/.ssh/id_personal"\n',
                              'ssh_key = "~/.ssh/id_personal"\nclone_dir = "~/me"\n'), apply=False))
    p = clone.plan("git@github.com:corp-org/app.git", c, no_rewrite=True)
    assert p.clone_url == "git@github.com:corp-org/app.git"
    assert p.auth == "ssh"
    assert p.identity.id == "work-github"
    p = clone.plan("https://github.com/corp-org/app", c, identity="personal")
    assert p.identity.id == "personal"
    assert p.dir == home / "me/github.com/corp-org/app"
    with pytest.raises(config.ConfigError, match=r"no identity 'nope' \(have: work-github, personal\)"):
        clone.plan("https://github.com/corp-org/app", c, identity="nope")


def test_no_match_uses_clone_dir(cfg, home):  # 4
    p = clone.plan("https://github.com/someone/x", load(cfg(DEFAULT_CONFIG + SRC, apply=False)))
    assert p.identity is None
    assert p.dir == home / "src/github.com/someone/x"
    assert p.clone_url == "https://github.com/someone/x"


def test_no_dir_anywhere_uses_cwd(cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = clone.plan("git@github.com:someone/x.git", load(cfg(apply=False)))
    assert p.dir == tmp_path / "github.com/someone/x"


def test_layout_and_escape(cfg, home):  # 5
    c = load(cfg(WORK.replace('clone_dir = "~/work"\n', "") + SRC + 'layout = "{id}/{repo}"\n', apply=False))
    assert clone.plan("https://github.com/corp-org/app", c).dir == home / "src/work-github/app"
    assert clone.plan("https://github.com/someone/x", c).dir == home / "src/default/x"
    c = load(cfg(DEFAULT_CONFIG + SRC, apply=False))
    with pytest.raises(config.ConfigError, match="target leaves ~/src"):
        clone.plan("https://github.com/../x", c)
    with pytest.raises(config.ConfigError, match="target leaves"):
        clone.plan("git@github.com:a/../../x.git", c)


def test_alias_host_in_layout_is_the_real_host(cfg, home):
    p = clone.plan("git@github-work:corp-org/app.git", load(cfg(WORK, apply=False)))
    assert p.dir == home / "work/github.com/corp-org/app"
    assert p.clone_url == "git@github-work:corp-org/app.git"


def test_shorthands(cfg, home, capsys):  # 6
    c = load(cfg(WORK, apply=False))
    with pytest.raises(config.ConfigError, match=r"'corp-org/app' needs --identity ID \(which host\?\)"):
        clone.plan("corp-org/app", c)
    p = clone.plan("corp-org/app", c, identity="work-github")
    assert p.clone_url == "git@github-work:corp-org/app.git"
    assert p.dir == home / "work/github.com/corp-org/app"
    p = clone.plan("github.com/someone/x", c)
    assert p.clone_url == "https://github.com/someone/x"
    assert p.identity is None
    rc, _, e = out(capsys, "corp-org/app", "--dry-run")
    assert rc == 2 and "✗ 'corp-org/app' needs --identity ID (which host?)" in e
    rc, o, _ = out(capsys, "corp-org/app", "--identity", "work-github", "--dry-run")
    assert rc == 0 and "clone git@github-work:corp-org/app.git" in o


def test_unparsable(cfg, capsys):
    cfg(apply=False)
    for bad in ("", "https://", "ext::sh -c x", "https://github.com", "a\nb"):
        rc, _, e = out(capsys, bad, "--dry-run")
        assert rc == 2, bad
        assert "cannot parse" in e, (bad, e)


# ---------------------------------------------------------------- the clone itself

def test_real_clone(cfg, home, tmp_path, bare_remote, capsys, monkeypatch):  # 7
    cfg()
    bare = seeded(bare_remote, tmp_path)
    dest = tmp_path / "dest"
    rc, o, e = out(capsys, str(bare), str(dest), "-y")
    assert rc == 0, e
    assert (dest / "README.md").read_text() == "hello\n"
    assert "✓ cloned into" in o
    assert f"↩ undo: rm -r {dest}" in o

    rc, o, _ = out(capsys, str(bare), str(tmp_path / "dest2"), "--json", "-y")
    data = json.loads(o)
    assert rc == 0
    assert data["ok"] is True and data["command"] == "clone"
    assert data["git_exit"] == 0 and data["dir"] == str(tmp_path / "dest2")
    assert data["url"] == str(bare) and data["clone_url"] == str(bare)
    assert data["identity"] is None and data["dry_run"] is False and data["problems"] == []

    seen = spy_clone(monkeypatch)
    assert run("clone", str(bare), str(tmp_path / "d3"), "-y") == 0
    assert seen[-1] == ["git", "clone", "--", str(bare), str(tmp_path / "d3")]
    assert run("clone", str(bare), str(tmp_path / "d4"), "-y", "--", "--depth", "1") == 0
    assert seen[-1] == ["git", "clone", "--depth", "1", "--", str(bare), str(tmp_path / "d4")]
    assert run("clone", f"file://{bare}", str(tmp_path / "d5"), "-y", "--", "--depth", "1") == 0
    assert git(tmp_path / "d5", "rev-parse", "--is-shallow-repository") == "true"


def test_clone_restores_caller_umask(cfg, home, tmp_path, bare_remote, capsys):  # 8
    cfg(DEFAULT_CONFIG + '\n[clone]\ndir = "' + str(tmp_path / "base") + '"\n', apply=False)
    bare = seeded(bare_remote, tmp_path)
    old = os.umask(0o022)
    try:
        assert run("clone", str(bare), "-y") == 0
        assert os.umask(0o022) == 0o077     # gitpro's own umask is untouched by the clone
        # Same caller umask, same process: the files gitpro itself writes are still private.
        assert run("apply", "-y") == 0
    finally:
        os.umask(old)
    dest = tmp_path / "base/upstream"
    assert stat.S_IMODE((dest / "README.md").stat().st_mode) == 0o644
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "base").stat().st_mode) == 0o755
    for written in (home / ".ssh/config", home / ".ssh/config.d/git-identities"):
        assert stat.S_IMODE(written.stat().st_mode) == 0o600, written


def test_existing_target(cfg, tmp_path, bare_remote, capsys):  # 9
    cfg()
    bare = seeded(bare_remote, tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "keep").write_text("mine")
    rc, o, e = out(capsys, str(bare), str(dest), "-y")
    assert rc == 2
    assert f"✗ {dest} already exists" in e
    assert sorted(p.name for p in dest.iterdir()) == ["keep"]


def test_needs_terminal(cfg, tmp_path, bare_remote, capsys):  # 10
    cfg()
    bare = seeded(bare_remote, tmp_path)
    rc, _, e = out(capsys, str(bare), str(tmp_path / "dest"))
    assert rc == 2 and "no terminal to confirm on" in e
    assert not (tmp_path / "dest").exists()
    rc, o, _ = out(capsys, str(bare), str(tmp_path / "dest"), "--json")
    assert rc == 2 and "no terminal to confirm on" in json.loads(o)["error"]
    assert not (tmp_path / "dest").exists()


def test_declined(cfg, tmp_path, bare_remote, capsys, monkeypatch):
    cfg()
    bare = seeded(bare_remote, tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    from gitpro import view
    monkeypatch.setattr(view, "confirm", lambda q: False)
    rc, _, _ = out(capsys, str(bare), str(tmp_path / "dest"))
    assert rc == 1
    assert not (tmp_path / "dest").exists()


def test_dry_run(cfg, home, capsys):  # 11
    cfg(WORK)
    rc, o, _ = out(capsys, "https://github.com/corp-org/app", "--dry-run")
    assert rc == 0
    assert o.startswith("clone git@github-work:corp-org/app.git\n")
    assert "  into      ~/work/github.com/corp-org/app" in o
    assert o.rstrip().splitlines()[-1] == "→ gitpro clone https://github.com/corp-org/app -y"
    assert not (home / "work").exists()
    rc, o, _ = out(capsys, "https://github.com/corp-org/app", "--dry-run", "--json")
    data = json.loads(o)
    assert rc == 0 and data["dry_run"] is True and data["git_exit"] is None
    assert data["clone_url"] == "git@github-work:corp-org/app.git"
    assert data["dir"] == str(home / "work/github.com/corp-org/app")
    assert data["identity"] == "work-github" and data["auth"] == "ssh-alias"
    assert not (home / "work").exists()


def test_alias_clone_goes_through_alias(cfg, tmp_path, fake_bin, no_ssh_env, capsys):  # 12
    cfg(WORK)
    fake_bin.add("ssh", 'sys.stderr.write("Permission denied (publickey).\\n")\nsys.exit(255)')
    dest = tmp_path / "app"
    rc, o, e = out(capsys, "git@github.com:corp-org/app.git", str(dest), "-y")
    assert rc == 2
    assert "✗ git clone failed (128)" in e
    calls = fake_bin.calls("ssh")
    assert calls and any("git@github-work" in a for a in calls[0]), calls
    assert not dest.exists()


def test_alias_clone_argv(cfg, home, tmp_path, fake_bin, no_ssh_env, monkeypatch):  # 12
    cfg(WORK)
    fake_bin.add("ssh", "sys.exit(255)")
    seen = spy_clone(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert run("clone", "git@github.com:corp-org/app.git", "-y") == 2
    assert seen == [["git", "clone", "--", "git@github-work:corp-org/app.git",
                     str(home / "work/github.com/corp-org/app")]]


def test_warns_when_no_identity(cfg, tmp_path, bare_remote, capsys):  # 13
    cfg(NO_DEFAULT_EMAIL)
    bare = seeded(bare_remote, tmp_path)
    dest = tmp_path / "dest"
    rc, o, e = out(capsys, str(bare), str(dest), "-y")
    assert rc == 0
    assert "✓ cloned into" in o
    assert "⚠ no identity for" in e and "git will refuse to commit here" in e
    assert f"gitpro scan --suggest --repo {dest}" in e


def test_json_reports_git_failure(cfg, tmp_path, capsys):
    cfg()
    rc, o, _ = out(capsys, str(tmp_path / "missing.git"), str(tmp_path / "dest"), "--json", "-y")
    data = json.loads(o)
    assert rc == 2
    assert data["ok"] is False and data["git_exit"] == 128 and "git clone failed (128)" in data["error"]


def test_capability(capsys):
    assert run("--version", "--json") == 0
    assert "clone" in json.loads(capsys.readouterr().out)["capabilities"]


def test_qa_alias_url_unmatched_owner_layout_uses_real_host(cfg, home):
    """Spec 07 §2 step 4: {host} = real host (not alias). git@github-work:someone/x.git names github.com."""
    p = clone.plan("git@github-work:someone/x.git", load(cfg(WORK, apply=False)))
    assert p.dir == home / "work/github.com/someone/x" or p.dir.parts[-3] == "github.com"


# ---------------------------------------------------------------- QA follow-ups

def test_options_between_url_and_dir(cfg, tmp_path, capsys):
    cfg()
    dest = tmp_path / "dest"
    rc, o, e = out(capsys, "https://github.com/corp-org/app", "--ssh", str(dest), "--dry-run", "--json")
    assert rc == 0, e
    data = json.loads(o)
    assert data["dir"] == str(dest) and data["clone_url"] == "git@github.com:corp-org/app.git"
    rc, o, _ = out(capsys, "https://github.com/corp-org/app", "--dry-run", str(dest), "--", "--depth", "1")
    assert rc == 0
    assert o.rstrip().splitlines()[-1] == f"→ gitpro clone https://github.com/corp-org/app {dest} -y -- --depth 1"


def test_browser_query_and_fragment_are_dropped(cfg, home):
    c = load(cfg(WORK, apply=False))
    p = clone.plan("https://github.com/corp-org/app?tab=readme-ov-file#readme", c)
    assert p.clone_url == "git@github-work:corp-org/app.git"
    assert p.dir == home / "work/github.com/corp-org/app"
    p = clone.plan("https://github.com/corp-org/app.git#main", c, no_rewrite=True)
    assert p.clone_url == "https://github.com/corp-org/app.git" and p.repo == "app"


def test_preview_key_is_the_one_git_will_use(cfg, home):
    p = clone.plan("git@github.com:corp-org/app.git", load(cfg(apply=False)), identity="personal")
    assert p.identity.id == "personal" and p.key == "~/.ssh/id_work"
    assert any("git will use work-github" in w for w in p.warnings)
    assert "  ssh       key ~/.ssh/id_work" in clone.preview(p, home)


def test_failed_clone_removes_the_dirs_it_made(cfg, tmp_path, capsys):
    cfg()
    keep = tmp_path / "keep"
    keep.mkdir()
    dest = keep / "a/b/dest"
    rc, _, e = out(capsys, str(tmp_path / "missing.git"), str(dest), "-y")
    assert rc == 2 and "git clone failed" in e
    assert list(keep.iterdir()) == []      # dest, a/b and a are gone; keep existed before


def test_cleanup_spares_a_target_that_was_replaced(tmp_path):
    dest = tmp_path / "dest"
    made = clone._make_target(dest, None)
    os.rename(dest, tmp_path / "moved")    # someone else's directory now sits at dest
    dest.mkdir()
    (dest / "theirs").write_text("x")
    assert clone._cleanup(dest, made, []) is False
    assert (dest / "theirs").exists()
    with pytest.raises(config.ConfigError, match="already exists"):
        clone._make_target(dest, None)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:          # a zombie waiting for init counts as gone
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def test_sigterm_stops_git_and_ssh_and_removes_the_partial_clone(cfg, tmp_path):
    """VS Code's cancel: SIGTERM to gitpro stops git and the ssh it started (a fake one that hangs) and removes
    the target plus the parents gitpro made. Started without a terminal, like the extension starts it."""
    cfg()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    pidfile = tmp_path / "ssh.pid"
    ssh = bin_ / "ssh"
    ssh.write_text(f"#!/bin/sh\necho $$ > '{pidfile}'\nexec sleep 60\n")
    ssh.chmod(0o755)
    dest = tmp_path / "base/sub/app"
    env = dict(os.environ, PATH=f"{bin_}{os.pathsep}{os.environ['PATH']}", GIT_TERMINAL_PROMPT="0",
               GIT_SSH_COMMAND=str(ssh))                # never a real ssh
    proc = subprocess.Popen([sys.executable, "-m", "gitpro", "clone", "git@github.com:corp-org/app.git", str(dest),
                             "-y"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while not (pidfile.exists() and pidfile.read_text().strip()):
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "the fake ssh never started"
            time.sleep(0.05)
        pid = int(pidfile.read_text())
        assert dest.is_dir()
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    assert proc.returncode == 130, err
    assert f"removed the partial clone {dest}" in err.decode()
    assert not (tmp_path / "base").exists()
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _alive(pid):
        os.kill(pid, signal.SIGKILL)
        pytest.fail("the fake ssh outlived gitpro")


def test_credentials_in_the_url_are_not_shown(cfg, tmp_path, capsys):
    """A token typed into the URL stays out of the preview, the warnings and --json (git still gets the real URL)."""
    cfg()
    url = "https://user:ghp_xxx@github.com/o/r.git"
    dest = str(tmp_path / "out")
    rc, o, e = out(capsys, url, dest, "--dry-run", "--json")
    assert rc == 0 and "ghp_xxx" not in o + e
    data = json.loads(o)
    assert data["url"] == data["clone_url"] == "https://user:***@github.com/o/r.git"
    rc, o, e = out(capsys, url, dest, "--dry-run")
    assert rc == 0
    assert "clone https://user:***@github.com/o/r.git" in o
    assert "ghp_xxx" not in o.replace(f"gitpro clone {url}", "") + e   # only the re-run command repeats it
    p = clone.plan(url, config.load_all(*config.find(None, tmp_path / "home")), dir=dest)
    assert p.clone_url == url                                           # what git clones is unchanged
