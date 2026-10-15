"""apply against a throwaway $HOME, then ask real git what it resolves."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG, commit, git, repo_with_remote, run, write_config
from gitpro import files, pin, plan

CONFIG = """
[default]
name = "Personal Me"
email = "me@personal.test"
ssh_key = "~/.ssh/id_personal"
ssh_hosts = ["github.com"]

[[identity]]
id = "work-gitlab"
name = "Work Me"
email = "me@corp.test"
host = "gitlab.corp.test"
owners = ["corp"]

[[identity]]
id = "work-github"
name = "Work Me"
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
ssh_key = "~/.ssh/id_work"
ssh_alias = "github-work"
rewrite_https = true
"""


def test_git_picks_identity_from_remote(home, tmp_path):
    write_config(home, CONFIG)
    assert run("apply", "-y") == 0

    cases = {
        "git@gitlab.corp.test:corp/app.git": "me@corp.test",
        "https://gitlab.corp.test/corp/sub/app.git": "me@corp.test",
        "https://github.com/corp-org/app.git": "me@corp.test",
        "git@github-work:corp-org/app.git": "me@corp.test",
        "git@github.com:someone-else/app.git": "me@personal.test",
        "git@gitlab.corp.test:other/app.git": "me@personal.test",
        None: "me@personal.test",
    }
    for n, (url, email) in enumerate(cases.items()):
        repo = repo_with_remote(tmp_path, f"r{n}", url)
        assert git(repo, "config", "user.email") == email, url

    work = repo_with_remote(tmp_path, "work", "git@github.com:corp-org/app.git")
    assert "id_work" in git(work, "config", "core.sshCommand")
    # URLs are rewritten through the alias, whose ssh Host offers only the work key.
    assert git(work, "ls-remote", "--get-url", "git@github.com:corp-org/x.git") == "git@github-work:corp-org/x.git"
    assert git(work, "ls-remote", "--get-url", "https://github.com/corp-org/x") == "git@github-work:corp-org/x"

    ssh = (home / ".ssh/config.d/git-identities").read_text()
    assert "Host github-work\n    HostName github.com" in ssh
    assert (home / ".ssh/config").stat().st_mode & 0o777 == 0o600


def test_second_apply_changes_nothing(home, capsys):
    write_config(home, CONFIG)
    assert run("apply", "-y") == 0
    capsys.readouterr()
    assert run("apply", "-y") == 0
    assert "up to date" in capsys.readouterr().out
    assert run("check") == 0


def test_check_reports_drift(home):
    write_config(home, CONFIG)
    run("apply", "-y")
    (home / ".gitconfig-work-gitlab").write_text("tampered\n")
    assert run("check") == 1


@pytest.mark.parametrize("target", [
    ".gitconfig-identities", ".gitconfig", ".ssh/config", ".ssh/config.d/git-identities",
    ".config/git/allowed_signers", ".gitconfig-gitpro-guard", ".config/gitpro/hooks/pre-commit",
    ".config/gitpro/hooks/README", ".config/gitpro.toml", ".ssh", ".gitconfig-identities/nested",
])
def test_identity_output_conflicts_write_nothing(home, capsys, target):
    write_config(home, CONFIG)
    assert run("apply", "-y") == 0
    text = CONFIG.replace('id = "work-github"', f'id = "work-github"\nfile = "~/{target}"')
    write_config(home, text)
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    capsys.readouterr()
    for argv in (("plan",), ("apply", "-y"), ("check",)):
        assert run(*argv) == 2
        assert "conflicts with" in capsys.readouterr().err
        assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("identity", ["identities", "gitpro-guard"])
def test_reserved_identity_filename_is_rejected(home, capsys, identity):
    write_config(home, CONFIG.replace('id = "work-github"', f'id = "{identity}"'))
    assert run("apply", "-y") == 2
    assert "conflicts with" in capsys.readouterr().err
    assert not (home / plan.MANAGED_GITCONFIG).exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_identity_output_paths_are_compared_after_resolution(home, capsys, symlink):
    first = home / "shared"
    second = home / "linked" if symlink else first
    if symlink:
        second.symlink_to(first)
    text = CONFIG.replace('id = "work-gitlab"', 'id = "work-gitlab"\nfile = "~/shared"')
    text = text.replace('id = "work-github"', f'id = "work-github"\nfile = "{second}"')
    write_config(home, text)
    assert run("apply", "-y") == 2
    assert "conflicts with identity 'work-gitlab'" in capsys.readouterr().err
    assert not first.exists()
    assert not (home / plan.MANAGED_GITCONFIG).exists()


def test_identity_symlink_cannot_alias_managed_file(home, capsys):
    target = home / plan.MANAGED_GITCONFIG
    (home / "linked").symlink_to(target)
    write_config(home, CONFIG.replace('id = "work-github"', 'id = "work-github"\nfile = "~/linked"'))
    assert run("apply", "-y") == 2
    assert "conflicts with" in capsys.readouterr().err
    assert (home / "linked").is_symlink() and not target.exists()


def test_reserved_identity_id_with_distinct_custom_file_is_valid(home, tmp_path):
    text = CONFIG.replace('id = "work-github"', 'id = "identities"\nfile = "~/work-identity"')
    write_config(home, text)
    assert run("apply", "-y") == 0
    repo = repo_with_remote(tmp_path, "personal", "git@github.com:someone/app.git")
    assert git(repo, "config", "user.email") == "me@personal.test"
    assert run("check") == 0


@pytest.mark.parametrize("directory_alias", [False, True])
@pytest.mark.parametrize("update_identity", [False, True])
def test_identity_alias_relocation_keeps_live_output(cfg, home, tmp_path, directory_alias, update_identity):
    text = DEFAULT_CONFIG.replace('id = "work-github"',
                                  'id = "work-github"\nfile = "~/identities/work"')
    cfg(text)
    original = home / "identities/work"
    alias = home / "work-alias"
    alias.symlink_to(original.parent if directory_alias else original)
    destination = "~/work-alias/work" if directory_alias else "~/work-alias"
    text = text.replace("~/identities/work", destination)
    email = "new@corp.test" if update_identity else "me@corp.test"
    cfg(text.replace("me@corp.test", email), apply=False)
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")

    assert run("apply", "-y") == 0
    assert original.is_file() and alias.is_symlink()
    assert git(repo, "config", "user.email") == email
    assert run("check") == 0
    assert run("apply", "-y") == 0
    assert original.is_file()


def test_hand_written_files_are_kept_and_backed_up(home):
    original = "[core]\n\teditor = vim\n"
    (home / ".gitconfig").write_text(original)
    write_config(home, CONFIG)
    run("apply", "-y")
    text = (home / ".gitconfig").read_text()
    assert text.startswith(original) and "[include]" in text
    backups = list(home.glob(".gitconfig.bak.*"))
    assert len(backups) == 1 and backups[0].read_text() == original


def test_removed_identity_file_is_deleted(home):
    write_config(home, CONFIG)
    run("apply", "-y")
    assert (home / ".gitconfig-work-gitlab").exists()
    write_config(home, CONFIG.split("[[identity]]\nid = \"work-gitlab\"")[0])
    run("apply", "-y")
    assert not (home / ".gitconfig-work-gitlab").exists()
    assert not (home / ".gitconfig-work-github").exists()


def test_apply_without_terminal_needs_yes(home, capsys):
    write_config(home, CONFIG)
    assert run("apply") == 0
    assert "Dry run" in capsys.readouterr().out
    assert not (home / ".gitconfig").exists()


def test_uninstall_restores_hand_written_files(home, tmp_path):
    original = "[core]\n\teditor = vim\n"
    ssh_original = "Host example-box\n    User me\n"
    (home / ".gitconfig").write_text(original)
    (home / ".ssh").mkdir(mode=0o700)
    (home / ".ssh/config").write_text(ssh_original)
    write_config(home, CONFIG)
    run("apply", "-y")

    assert run("uninstall", "-y") == 0
    assert (home / ".gitconfig").read_text() == original
    assert (home / ".ssh/config").read_text() == ssh_original
    for name in (".gitconfig-identities", ".gitconfig-work-gitlab", ".gitconfig-work-github",
                 ".ssh/config.d/git-identities"):
        assert not (home / name).exists(), name
    assert (home / ".config/gitpro.toml").exists()
    assert run("uninstall", "-y") == 0  # nothing left to do


def test_uninstall_removes_files_apply_created(home):
    write_config(home, CONFIG)
    run("apply", "-y")
    assert run("uninstall", "-y") == 0
    assert not (home / ".gitconfig").exists()
    assert not (home / ".ssh/config").exists()


def test_signing(home, tmp_path):
    (home / ".ssh").mkdir(mode=0o700)
    (home / ".ssh/id_personal").write_text("private\n")
    (home / ".ssh/id_personal").chmod(0o600)
    (home / ".ssh/id_personal.pub").write_text("ssh-ed25519 AAAAPERSONAL me@laptop\n")
    write_config(home, CONFIG.replace('ssh_hosts = ["github.com"]', 'ssh_hosts = ["github.com"]\nsign = true'))
    run("apply", "-y")

    signers = (home / ".config/git/allowed_signers").read_text()
    assert 'me@personal.test namespaces="git" ssh-ed25519 AAAAPERSONAL' in signers
    repo = repo_with_remote(tmp_path, "r", "git@github.com:someone/x.git")
    assert git(repo, "config", "gpg.format") == "ssh"
    assert git(repo, "config", "commit.gpgsign") == "true"
    assert git(repo, "config", "user.signingkey") == str(home / ".ssh/id_personal.pub")


def test_scripts_from_before_packaging_are_migrated(home):
    hand = "[core]\n\teditor = vim\n"
    (home / ".gitconfig").write_text(
        hand + "\n# BEGIN git_identities.py -- keep last\n[include]\n\tpath = ~/.gitconfig-identities\n"
               "# END git_identities.py\n")
    (home / ".gitconfig-identities").write_text("# Generated by git_identities.py -- old\n"
                                                "# managed: ~/.gitconfig-old\n")
    (home / ".gitconfig-old").write_text("# Generated by git_identities.py -- old\n")
    write_config(home, CONFIG)
    run("apply", "-y")

    text = (home / ".gitconfig").read_text()
    assert "git_identities.py" not in text and text.count("# BEGIN gitpro") == 1
    assert not (home / ".gitconfig-old").exists()
    assert not list(home.glob(".gitconfig.bak.*"))  # only our block changed


def test_init_writes_example_that_must_be_edited(home, capsys):
    assert run("init", "-y") == 0
    path = home / ".config/gitpro.toml"
    assert path.stat().st_mode & 0o777 == 0o600
    assert run("plan") == 2
    assert "placeholder" in capsys.readouterr().err
    assert run("init", "-y") == 0  # never overwrites


def test_clone_uses_identity_key_without_alias(home, tmp_path, monkeypatch):
    """includeIf hasconfig:remote.*.url applies while git clone runs, so no alias is needed to pick the key."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "ssh.log"
    (bin_dir / "ssh").write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 1\n')
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    write_config(home, CONFIG.replace('ssh_alias = "github-work"\nrewrite_https = true\n', ""))
    run("apply", "-y")

    subprocess.run(["git", "clone", "-q", "git@github.com:corp-org/app.git", str(tmp_path / "app")],
                   capture_output=True)
    assert f"-i {home}/.ssh/id_work" in log.read_text()
    assert "git@github.com" in log.read_text()


def test_pin_carries_identity_into_a_container(home, tmp_path, monkeypatch):
    write_config(home, CONFIG)
    run("apply", "-y")
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    before = (repo / ".git/config").read_text()
    assert run("pin", "-y", str(repo)) == 0
    assert run("pin", "-y", str(repo)) == 0  # re-pinning replaces, never stacks
    assert git(repo, "config", "--local", "--get-all", "include.path") == "git-identities"

    # What VS Code Dev Containers do: copy ~/.gitconfig alone into a fresh home.
    container = tmp_path / "container-home"
    container.mkdir()
    (container / ".gitconfig").write_text((home / ".gitconfig").read_text())
    monkeypatch.setenv("HOME", str(container))
    assert git(repo, "config", "user.email") == "me@corp.test"
    assert git(repo, "config", "user.name") == "Work Me"

    monkeypatch.setenv("HOME", str(home))
    assert run("unpin", "-y", str(repo)) == 0
    assert (repo / ".git/config").read_text() == before
    assert not (repo / ".git/git-identities").exists()
    assert run("unpin", "-y", str(repo)) == 0  # nothing to do


def test_pin_refuses_without_identity(home, tmp_path, capsys):
    write_config(home, CONFIG.split("[[identity]]")[0].replace('name = "Personal Me"\nemail = "me@personal.test"\n', ""))
    run("apply", "-y")
    repo = repo_with_remote(tmp_path, "app", "git@github.com:someone/app.git")
    assert run("pin", "-y", str(repo)) == 2
    assert "nothing to pin" in capsys.readouterr().err
    assert not (repo / ".git/git-identities").exists()


def test_pin_refuses_identity_not_from_us(home, tmp_path, capsys):
    """Inside a container only a copied ~/.gitconfig is left; pinning its hand-written [user] would be wrong."""
    (home / ".gitconfig").write_text('[user]\n\tname = Old\n\temail = old@corp.test\n')
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    assert run("pin", "-y", str(repo)) == 2
    assert "not from gitpro" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["override", "container", "write"])
def test_failed_repin_keeps_existing_pin(cfg, home, tmp_path, monkeypatch, capsys, failure):
    cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    assert run("pin", "-y", str(repo)) == 0
    path = repo / ".git" / pin.FILE
    if failure == "override":
        git(repo, "config", "user.email", "other@corp.test")
    elif failure == "container":
        container = tmp_path / "container"
        container.mkdir()
        (container / ".gitconfig").write_text((home / ".gitconfig").read_text())
        monkeypatch.setenv("HOME", str(container))
    else:
        def fail_write(*args):
            raise OSError("pin write failed")
        monkeypatch.setattr(files, "atomic_write", fail_write)
    before = path.read_bytes(), path.stat().st_mode, (repo / ".git/config").read_bytes()
    capsys.readouterr()
    assert run("pin", "-y", str(repo)) == 2
    assert capsys.readouterr().err
    assert (path.read_bytes(), path.stat().st_mode, (repo / ".git/config").read_bytes()) == before


@pytest.mark.parametrize("existing_file", [False, True])
def test_failed_pin_include_rolls_back_file(cfg, tmp_path, capsys, existing_file):
    cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    path = repo / ".git" / pin.FILE
    if existing_file:
        assert run("pin", "-y", str(repo)) == 0
        git(repo, "config", "--local", "--unset-all", "include.path")
        path.write_text(path.read_text().replace("me@corp.test", "old@corp.test"))
    previous = path.read_bytes() if existing_file else None
    before = (repo / ".git/config").read_bytes()
    (repo / ".git/config.lock").write_text("another git process\n")
    capsys.readouterr()
    assert run("pin", "-y", str(repo)) == 2
    assert "lock" in capsys.readouterr().err
    assert (path.read_bytes() if path.exists() else None) == previous
    assert (repo / ".git/config").read_bytes() == before


@pytest.mark.parametrize("crlf", [False, True])
@pytest.mark.parametrize("mode", [0o600, 0o660])
def test_failed_pin_include_preserves_bytes_and_mode(cfg, tmp_path, crlf, mode):
    cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    assert run("pin", "-y", str(repo)) == 0
    path = repo / ".git" / pin.FILE
    git(repo, "config", "--local", "--unset-all", "include.path")
    if crlf:
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    path.chmod(mode)
    before = path.read_bytes(), path.stat().st_mode, (repo / ".git/config").read_bytes()
    (repo / ".git/config.lock").write_text("another git process\n")

    assert run("pin", "-y", str(repo)) == 2
    assert (path.read_bytes(), path.stat().st_mode, (repo / ".git/config").read_bytes()) == before


def test_pin_refuses_symlink_without_changing_its_target(cfg, home, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    target = home / plan.MANAGED_GITCONFIG
    path = repo / ".git" / pin.FILE
    path.symlink_to(target)
    before = target.read_bytes(), (repo / ".git/config").read_bytes()
    assert run("pin", "-y", str(repo)) == 2
    assert "pin file must not be a symlink" in capsys.readouterr().err
    assert path.is_symlink()
    assert (target.read_bytes(), (repo / ".git/config").read_bytes()) == before


@pytest.mark.parametrize("worktree", [False, True])
def test_repin_from_subdir_refreshes_without_changing_config(cfg, tmp_path, worktree):
    config_path = cfg()
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    commit(repo, "initial")
    if worktree:
        linked = tmp_path / "linked"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--quiet", "--detach", str(linked)],
                       check=True, capture_output=True, timeout=60)
        repo = linked
    assert run("pin", "-y", str(repo)) == 0
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    before = (common / "config").read_bytes()
    cfg(config_path.read_text().replace("me@corp.test", "new@corp.test"))
    subdir = repo / "sub/deep"
    subdir.mkdir(parents=True)
    assert run("pin", "-y", str(subdir)) == 0
    assert git(repo, "config", "user.email") == "new@corp.test"
    assert (common / "config").read_bytes() == before


def test_scan_names_each_override_by_its_path(home, capsys):
    """The tree names a repo by its path, not the shortest unique tail: "unchanged: edu" said which repo only with -v."""
    write_config(home, CONFIG)
    run("apply", "-y")
    edu = repo_with_remote(home / "prayog", "edu", "git@github.com:someone/edu.git")
    git(edu, "config", "user.name", "Personal Me")
    capsys.readouterr()

    assert run("scan", str(home / "prayog")) == 0
    lines = [line.rstrip() for line in capsys.readouterr().out.splitlines()]
    assert "└── unchanged: ~/prayog/edu" in lines


def test_scan_reports_and_removes_repo_overrides(home, tmp_path, capsys):
    write_config(home, CONFIG)
    run("apply", "-y")
    root = tmp_path / "src"
    work = repo_with_remote(root, "work", "git@github.com:corp-org/app.git")
    stale = repo_with_remote(root, "stale", "git@github.com:someone/app.git")
    git(stale, "config", "user.email", "old@personal.test")
    git(stale, "config", "user.signingkey", "/k.pub")  # not ours to touch
    same = repo_with_remote(root, "same", "git@gitlab.corp.test:corp/app.git")
    git(same, "config", "user.name", "Work Me")
    nested = repo_with_remote(work, "nested", None)
    repo_with_remote(root / ".hidden", "skipped", None)
    repo_with_remote(root / "node_modules", "skipped", None)
    capsys.readouterr()

    assert run("scan", str(root)) == 0
    out = capsys.readouterr().out
    assert out.startswith("4 repos under")
    lines = [line.rstrip() for line in out.splitlines()]
    assert "    1  Work Me <me@corp.test>          ~/.gitconfig-work-github" in lines
    assert "    1  see below                       set in the repo" in lines  # counted by where the email comes from
    assert "Personal Me <me@personal.test>  (1 repo)" in lines
    flat = " ".join(out.split())  # a long temp path wraps onto its own line
    assert f"└── from Personal Me <old@personal.test>: {stale.resolve()} " in flat
    assert f"└── unchanged: {same.resolve()} " in flat
    assert "dry run, nothing changed. Remove 2 settings from 2 repos: gitpro scan --fix " + str(root) in lines
    assert git(stale, "config", "user.email") == "old@personal.test"  # scan alone writes nothing

    assert run("scan", "--fix", "-y", str(root)) == 0
    out = capsys.readouterr().out
    assert "✓ removed 2 settings from 2 repos\n↩ undo: sh ~/.local/state/gitpro/scan-undo-" in out
    assert git(stale, "config", "user.email") == "me@personal.test"
    assert git(stale, "config", "--local", "user.signingkey") == "/k.pub"
    assert "[user]" not in (same / ".git/config").read_text()
    assert git(nested, "config", "user.email") == "me@personal.test"

    undo = home / out.split("undo: sh ~/")[1].split()[0]
    subprocess.run(["sh", str(undo)], check=True)
    assert git(stale, "config", "user.email") == "old@personal.test"
    assert git(same, "config", "--local", "user.name") == "Work Me"

    assert run("scan", "--fix", "-y", "--repo", str(same)) == 0  # only that one
    assert git(same, "config", "--local", "user.name") == ""
    assert git(stale, "config", "--local", "user.email") == "old@personal.test"


def test_scan_json(home, tmp_path, capsys):
    write_config(home, CONFIG)
    run("apply", "-y")
    root = tmp_path / "src"
    stale = repo_with_remote(root, "stale", "git@github.com:someone/app.git")
    git(stale, "config", "user.email", "old@personal.test")
    repo_with_remote(root / "a", "app", "git@github.com:corp-org/app.git")
    repo_with_remote(root / "b", "app", "git@github.com:corp-org/app2.git")
    capsys.readouterr()

    assert run("scan", "--json", str(root)) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["repos"] == 3 and rep["roots"] == [str(root)]
    assert rep["identities"][0] == {"name": "Work Me", "email": "me@corp.test", "source": "~/.gitconfig-work-github",
                                    "repos": 2, "owners": {"github.com/corp-org": 2}}
    assert rep["overrides"] == [{
        "path": str(stale), "short": "stale", "remote": "git@github.com:someone/app.git", "owner": "github.com/someone",
        "now": {"name": "Personal Me", "email": "old@personal.test"},
        "after": {"name": "Personal Me", "email": "me@personal.test"},
        "settings": [{"key": "user.email", "value": "old@personal.test", "without": "me@personal.test"}],
        "fixable": ["user.email"]}]
    assert rep["suggestions"][0]["owners"] == ["someone"]

    assert run("scan", "--fix", "--json", "--repo", str(stale)) == 0  # --json needs no terminal and no -y
    done = json.loads(capsys.readouterr().out)
    assert done["removed"] == [{"path": str(stale), "key": "user.email", "value": "old@personal.test"}]
    assert Path(done["undo"]).is_file()


def test_scan_fix_keeps_the_only_identity(home, tmp_path, capsys):
    """Without the repo's own email nothing would set one; removing it would leave git unable to commit."""
    write_config(home, CONFIG.split("[[identity]]")[0].replace('name = "Personal Me"\nemail = "me@personal.test"\n', ""))
    run("apply", "-y")
    repo = repo_with_remote(tmp_path / "src", "app", None)
    git(repo, "config", "user.email", "me@here.test")
    assert run("scan", "--fix", "-y", str(tmp_path / "src")) == 0
    out = capsys.readouterr().out
    assert "their own: nothing else sets one, so fix keeps it  (1 repo)" in out and "nothing to fix" in out
    assert git(repo, "config", "user.email") == "me@here.test"


def test_scan_owner_of_urls():
    from gitpro.scan import owner
    assert owner("git@github.com:corp-org/app.git") == "github.com/corp-org"
    assert owner("ssh://git@gitlab.corp.test:2222/corp/sub/app.git") == "gitlab.corp.test/corp"
    assert owner("https://user@gitlab.corp.test/corp/app") == "gitlab.corp.test/corp"
    assert owner("/srv/git/app.git") == "/srv/git/app.git"
    assert owner("") == "no remote"


def test_scan_suggestion_keeps_identity_through_fix(home, tmp_path, capsys):
    """Paste the suggested block, apply, fix: the repo commits as before, now from gitpro."""
    cfg = write_config(home, CONFIG)
    run("apply", "-y")
    root = tmp_path / "src"
    old = repo_with_remote(root, "old", "git@gitlab.side.test:side/app.git")
    git(old, "config", "user.email", "me@side.test")
    other = repo_with_remote(root, "other", "https://gitlab.side.test/side/web.git")
    loose = repo_with_remote(root, "loose", None)
    git(loose, "config", "user.email", "me@loose.test")
    twin = repo_with_remote(root, "twin", "git@gitlab.side.test:side/twin.git")
    git(twin, "config", "user.email", "me@twin.test")
    capsys.readouterr()

    assert run("scan", "--suggest", str(root)) == 0
    assert "# side is in another block too: an identity matches by owner, so keep only one" in capsys.readouterr().out
    subprocess.run(["rm", "-rf", str(twin)], check=True)

    assert run("scan", str(root)) == 0
    assert "to keep some of these identities instead: gitpro scan --suggest" in capsys.readouterr().out
    assert run("scan", "--suggest", str(root)) == 0
    out = capsys.readouterr().out
    block = out[out.index("[[identity]]"):].split("\n\n")[0]
    assert block == "\n".join([
        "[[identity]]", 'id = "side"', 'name = "Personal Me"', 'email = "me@side.test"', 'host = "gitlab.side.test"',
        'owners = ["side"]   # repos with this setting: side 1',
        "# also changes 1 other repo(s) of these owners to this email"])
    assert "loose (no remote): no identity can match it; after fixing it uses Personal Me <me@personal.test>" in out
    assert "dry run, nothing changed. Remove 2 settings from 2 repos" in out

    cfg.write_text(cfg.read_text() + "\n" + block + "\n")
    assert run("apply", "-y") == 0
    assert run("scan", "--fix", "-y", str(root)) == 0
    assert git(old, "config", "--local", "user.email") == ""
    assert git(old, "config", "user.email") == "me@side.test"
    assert git(other, "config", "user.email") == "me@side.test"
    assert git(loose, "config", "user.email") == "me@personal.test"


def test_scan_tui_ticks_and_fixes(home, tmp_path):
    """The interactive list: untick one repo, fix the rest after confirming."""
    pytest.importorskip("textual")
    import asyncio

    from gitpro import scan
    from gitpro.tui import ScanApp

    write_config(home, CONFIG)
    run("apply", "-y")
    root = tmp_path / "src"
    for name in ("a", "b"):
        git(repo_with_remote(root, name, f"git@github.com:someone/{name}.git"), "config", "user.email", "old@x.test")
    repos, failed = scan.inspect_all(scan.find([root]))
    rep = scan.report([root], repos, failed, str)

    async def drive() -> list:
        app = ScanApp(rep, str)
        async with app.run_test() as pilot:
            assert app.sub_title == "2 repos ticked, 2 settings to remove"
            await pilot.press("space")      # untick a
            assert app.sub_title == "1 repo ticked, 1 setting to remove"
            await pilot.press("enter", "n")  # ask, then cancel
            assert app.return_value is None and app.is_running
            await pilot.press("enter", "y")
        return app.return_value

    assert asyncio.run(drive()) == [str(root / "b")]
