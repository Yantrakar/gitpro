"""devcontainer: the initializeCommand line that runs `gitpro pin` on the host (spec 13 §10). Never docker."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG, git, repo_with_remote, run
from gitpro import devcontainer as dc

GH = "git@github.com:corp-org/app.git"
ROOT = Path(__file__).resolve().parent.parent
LINE = dc.LINE
ORIGINAL = '{\n    "name": "x",\n    "image": "y"\n}\n'


@pytest.fixture(autouse=True)
def on_host(monkeypatch, tmp_path):
    """Tests run 'on the host' unless they say otherwise, even when the test machine is a container."""
    for var in dc.CONTAINER_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(dc, "DOCKERENV", tmp_path / "no-dockerenv")


@pytest.fixture
def repo(cfg, tmp_path):
    cfg()
    return repo_with_remote(tmp_path, "app", GH)


def devfile(repo, text=ORIGINAL, *, commit=True, name=".devcontainer/devcontainer.json"):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if commit:
        git(repo, "add", name)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "--no-gpg-sign", "-m", "dc"], check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@x",
                            "GIT_COMMITTER_NAME": "A", "GIT_COMMITTER_EMAIL": "a@x"})
    return path


def out(capsys, *argv):
    rc = run("devcontainer", *argv)
    c = capsys.readouterr()
    return rc, c.out, c.err


def js(capsys, *argv):
    rc, o, _ = out(capsys, *argv, "--json")
    return rc, json.loads(o)


def state(home):
    d = home / ".local/state/gitpro"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


# ---------------------------------------------------------------- 1: no devcontainer.json

def test_no_devcontainer_json(repo, home, capsys):  # 1
    rc, o, e = out(capsys, str(repo))
    assert rc == 2
    assert "✗ no devcontainer.json in " in e
    assert ".devcontainer/devcontainer.json, .devcontainer.json" in e
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 2 and "no devcontainer.json" in e
    assert not (repo / ".devcontainer").exists() and not (repo / ".devcontainer.json").exists()
    rc, j = js(capsys, str(repo))
    assert rc == 2 and j["ok"] is False and j["file"] is None and "no devcontainer.json" in j["error"]
    assert j["proposed"] == LINE


def test_not_a_repo(home, tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    rc, _, e = out(capsys, str(plain))
    assert rc == 2 and f"✗ {plain}: not a git repository" in e


# ---------------------------------------------------------------- 2-3: print by default, --write on request

def test_default_prints_and_does_not_write(repo, home, capsys):  # 2
    path = devfile(repo)
    rc, o, e = out(capsys, str(repo))
    assert rc == 0
    assert f'"initializeCommand": {json.dumps(LINE, ensure_ascii=False)}' in o
    assert "(tracked: committed to the repo)" in o
    assert "shared with your team" in o and "gitpro devcontainer --write" in o
    assert "'gitpro which' shows the pinned identity" in o
    assert dc.NOTE in o
    assert path.read_text() == ORIGINAL
    rc, j = js(capsys, str(repo))
    assert rc == 0
    assert j == {"ok": True, "command": "devcontainer", "file": str(path), "tracked": True, "has_pin": False,
                 "existing_command": None, "proposed": LINE, "merged": False, "has_comments": False,
                 "written": False}
    assert path.read_text() == ORIGINAL and state(home) == []


def test_write_tracked_backup_undo(repo, home, capsys):  # 2
    path = devfile(repo)
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 0, e
    assert f"⚠ {dc._tilde(path)} is tracked by git and shared with your team" in e
    assert "every teammate" in e and "gitpro on their host" in e
    assert "✓ written; the next container start runs gitpro pin on the host" in o
    assert "↩ undo: sh " in o and f"→ also pin now: gitpro pin {dc._tilde(repo)}" in o
    text = path.read_text()
    data = json.loads(text)
    assert data == {"name": "x", "image": "y", "initializeCommand": LINE}
    assert text.startswith('{\n    "name": "x",\n    "image": "y",\n    "initializeCommand": ')
    names = state(home)
    backups = [n for n in names if n.startswith("backup-")]
    undos = [n for n in names if n.startswith("devcontainer-undo-")]
    assert len(backups) == 1 and len(undos) == 1
    bak = home / ".local/state/gitpro" / backups[0] / "devcontainer.json"
    assert bak.read_text() == ORIGINAL
    assert not list(repo.glob("**/*.bak*")) and git(repo, "status", "--porcelain") == "M .devcontainer/devcontainer.json"
    subprocess.run(["sh", str(home / ".local/state/gitpro" / undos[0])], check=True)
    assert path.read_text() == ORIGINAL


def test_write_json(repo, home, capsys):
    path = devfile(repo)
    rc, j = js(capsys, str(repo), "--write", "-y")
    assert rc == 0
    assert j["written"] is True and j["file"] == str(path) and j["proposed"] == LINE
    assert Path(j["backup"]).read_text() == ORIGINAL
    assert Path(j["undo"]).name.startswith("devcontainer-undo-")
    assert json.loads(path.read_text())["initializeCommand"] == LINE


def test_write_untracked(repo, home, capsys):  # 3
    path = devfile(repo, commit=False)
    rc, o, e = out(capsys, str(repo))
    assert rc == 0 and "tracked" not in o.splitlines()[0].rsplit("  (", 1)[1]   # the tmp path says "untracked"
    assert js(capsys, str(repo))[1]["tracked"] is False
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 0, e
    assert "is tracked by git" not in e
    assert json.loads(path.read_text())["initializeCommand"] == LINE


def test_second_location(repo, home, capsys):
    path = devfile(repo, commit=False, name=".devcontainer.json")
    assert dc.locate(repo) == path
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 0, e
    assert json.loads(path.read_text())["initializeCommand"] == LINE


def test_write_needs_terminal_or_yes(repo, home, capsys):
    path = devfile(repo)
    rc, o, e = out(capsys, str(repo), "--write")
    assert rc == 2 and "no terminal to confirm on" in e
    assert path.read_text() == ORIGINAL
    assert not [n for n in state(home) if n.startswith(("backup-", "devcontainer-undo"))]


# ---------------------------------------------------------------- 4: existing initializeCommand

def test_propose_cases():  # 4
    assert dc.propose(None) == LINE
    assert dc.propose("npm ci") == {"gitpro": LINE, "npm": "npm ci"}
    assert dc.propose("gitpro-helper x") == {"gitpro": LINE, "gitpro-helper": "gitpro-helper x"}
    assert dc.propose("gitpro status") == {"gitpro": LINE, "cmd": "gitpro status"}
    assert dc.propose(["npm", "ci"]) == {"gitpro": LINE, "cmd": ["npm", "ci"]}
    got = dc.propose({"npm": "npm ci", "x": ["a"]})
    assert got == {"gitpro": LINE, "npm": "npm ci", "x": ["a"]} and list(got) == ["gitpro", "npm", "x"]
    for done in ("gitpro pin -y .", ["sh", "-c", "gitpro pin -y \"$1\"", "sh", "x"], {"g": "gitpro  pin -y ."}, LINE):
        assert dc.has_pin(done) and dc.propose(done) == done
    assert not dc.has_pin("npm ci") and not dc.has_pin(None)
    assert dc.propose({"gitpro": "gitpro doctor"}) == {"gitpro-pin": LINE, "gitpro": "gitpro doctor"}


def test_both_gitpro_keys_taken(repo, home, capsys):
    path = devfile(repo, json.dumps({"initializeCommand": {"gitpro": "a", "gitpro-pin": "b"}}) + "\n")
    before = path.read_text()
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 2 and 'already has keys "gitpro" and "gitpro-pin"' in e and "under another key" in e
    assert "aborted" not in o + e and path.read_text() == before and state(home) == []


def test_existing_string_written_as_object(repo, home, capsys):  # 4
    path = devfile(repo, '{"image": "y", "initializeCommand": "npm ci"}\n')
    rc, j = js(capsys, str(repo))
    assert j["existing_command"] == "npm ci" and j["proposed"] == {"gitpro": LINE, "npm": "npm ci"}
    assert j["merged"] is True
    rc, o, e = out(capsys, str(repo))
    assert '"initializeCommand": "npm ci"' in o and f"· {dc.MERGED}" in o
    assert "runs alongside gitpro pin (in parallel); its output appears when it finishes" in dc.MERGED
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 0, e
    assert f"· {dc.MERGED}" in o
    data = json.loads(path.read_text())
    assert list(data) == ["image", "initializeCommand"]
    assert data["initializeCommand"] == {"gitpro": LINE, "npm": "npm ci"}


@pytest.mark.parametrize("value", ["gitpro pin -y .", ["sh", "-c", "gitpro pin -y \"$1\"", "sh", "x"], {"a": "gitpro pin -y ."}])
def test_already_set_up(repo, home, capsys, value):  # 4
    text = json.dumps({"image": "y", "initializeCommand": value}) + "\n"
    path = devfile(repo, text)
    rc, o, e = out(capsys, str(repo))
    assert rc == 0 and "✓ already set up" in o
    rc, j = js(capsys, str(repo))
    assert j["has_pin"] is True and j["proposed"] == value and j["existing_command"] == value
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 0 and "✓ already set up" in o
    assert path.read_text() == text and state(home) == []


# ---------------------------------------------------------------- 5-6: comments, unparsable

COMMENTED = '{\n  // the base image\n  "image": "y", /* inline */\n  "name": "x",\n}\n'


def test_comments_print_but_refuse_write(repo, home, capsys):  # 5
    path = devfile(repo, COMMENTED)
    rc, o, e = out(capsys, str(repo))
    assert rc == 0 and f'"initializeCommand": {json.dumps(LINE, ensure_ascii=False)}' in o
    assert "has comments" in o
    rc, j = js(capsys, str(repo))
    assert j["has_comments"] is True and j["written"] is False
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 2
    assert f"✗ {dc._tilde(path)} has comments; add the key by hand (printed above)" in e
    assert f'"initializeCommand": {json.dumps(LINE, ensure_ascii=False)}' in o
    assert path.read_bytes() == COMMENTED.encode()
    assert not [n for n in state(home) if n.startswith(("backup-", "devcontainer-undo"))]
    rc, j = js(capsys, str(repo), "--write", "-y")
    assert rc == 2 and j["ok"] is False and j["has_comments"] is True and "has comments" in j["error"]
    assert path.read_bytes() == COMMENTED.encode()


def test_jsonc_without_comments_is_written(repo, home, capsys):
    path = devfile(repo, '{\n\t"image": "y",\n}\n')
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 0, e
    assert path.read_text().startswith('{\n\t"image": "y",\n\t"initializeCommand"')


@pytest.mark.parametrize("text", ['{"a": }\n', '["not", "an", "object"]\n'])
def test_unparsable(repo, home, capsys, text):  # 6
    path = devfile(repo, text)
    for extra in ((), ("--write", "-y")):
        rc, o, e = out(capsys, str(repo), *extra)
        assert rc == 2
        assert f"{path}: cannot parse (" in e and "add the key by hand" in e and '"initializeCommand"' in e
        assert path.read_text() == text
    assert state(home) == []


# ---------------------------------------------------------------- 7-8: status

def test_status_pinned_and_set_up(repo, home, capsys):  # 7
    devfile(repo, json.dumps({"image": "y", "initializeCommand": LINE}))
    assert run("pin", "-y", str(repo)) == 0
    capsys.readouterr()
    rc, o, e = out(capsys, "status", str(repo))
    assert rc == 0, o + e
    lines = o.splitlines()
    assert lines[0] == dc._tilde(repo)
    assert len(lines) == 4  # the repo, then three lines
    assert lines[1].startswith("  devcontainer.json  .devcontainer/devcontainer.json")
    assert lines[1].endswith("initializeCommand runs gitpro pin ✓")
    assert lines[2].startswith("  pin  ") and ".git/git-identities" in lines[2]
    assert "Work Me <me@corp.test>" in lines[2] and lines[2].endswith("up to date ✓")
    assert lines[3].split() == ["inside", "container", "no"]
    rc, j = js(capsys, "status", str(repo))
    assert rc == 0
    assert j["command"] == "devcontainer status" and j["has_pin"] is True and j["pinned"] is True
    assert j["pin_stale"] is False and j["in_container"] is False and j["problems"] == []
    assert j["file"] == str(repo / ".devcontainer/devcontainer.json")


def test_status_in_container_without_pin(repo, home, capsys, monkeypatch):  # 7
    monkeypatch.setenv("REMOTE_CONTAINERS", "true")
    rc, o, e = out(capsys, "status", str(repo))
    assert rc == 1
    assert "✗ not pinned" in e and f"gitpro pin {dc._tilde(repo)}" in e
    rc, j = js(capsys, "status", str(repo))
    assert rc == 1 and j["in_container"] is True and j["pinned"] is False and j["file"] is None
    assert any("gitpro pin" in p["message"] for p in j["problems"])


def test_status_in_container_no_config(home, tmp_path, capsys, monkeypatch):
    repo = repo_with_remote(tmp_path, "app", GH)
    monkeypatch.setenv("DEVCONTAINER", "1")
    rc, o, e = out(capsys, "status", str(repo))
    assert rc == 1 and "✗ not pinned and no host config: run on the host: gitpro pin" in e


def test_status_broken_config_says_so(home, tmp_path, capsys, monkeypatch):
    repo = repo_with_remote(tmp_path, "app", GH)
    (home / ".config").mkdir(parents=True, exist_ok=True)
    (home / ".config/gitpro.toml").write_text("[[identity]\nname = \n")
    monkeypatch.setenv("DEVCONTAINER", "1")
    rc, o, e = out(capsys, "status", str(repo))
    assert rc == 1 and "the config cannot be parsed" in e and "no host config" not in e
    assert "✗ not pinned: run on the host: gitpro pin" in e


def test_status_flags_between_words(repo, home, capsys):
    devfile(repo)
    rc, j = js(capsys, "status", str(repo))
    rc2, o, _ = out(capsys, "status", "--json", str(repo))
    assert (rc, rc2) == (0, 0) and json.loads(o) == j and j["command"] == "devcontainer status"


def test_status_on_host_unpinned_is_fine(repo, home, capsys):
    devfile(repo)
    rc, o, e = out(capsys, "status", str(repo))
    assert rc == 0, e
    assert "does not run gitpro pin" in o and "not pinned" in o


def test_in_container(monkeypatch, tmp_path):  # 7
    assert dc.in_container() is False
    for var in dc.CONTAINER_ENV:
        monkeypatch.setenv(var, "true")
        assert dc.in_container() is True
        monkeypatch.delenv(var)
    marker = tmp_path / "dockerenv"
    marker.write_text("")
    monkeypatch.setattr(dc, "DOCKERENV", marker)
    assert dc.in_container() is True
    assert dc.CONTAINER_ENV == ("REMOTE_CONTAINERS", "CODESPACES", "DEVCONTAINER")


def test_container_list_matches_the_extension():
    ts = (ROOT / "vscode/src/identityBar.ts").read_text()
    assert 'CONTAINER_ENV = ["REMOTE_CONTAINERS", "CODESPACES", "DEVCONTAINER"]' in ts
    assert 'DOCKERENV = "/.dockerenv"' in ts
    assert 'DOCKERENV = Path("/.dockerenv")' in (ROOT / "src/gitpro/devcontainer.py").read_text()


def test_status_stale_pin(repo, home, capsys, cfg):  # 8
    assert run("pin", "-y", str(repo)) == 0
    cfg(DEFAULT_CONFIG.replace("me@corp.test", "new@corp.test"))
    capsys.readouterr()
    rc, o, e = out(capsys, "status", str(repo))
    assert rc == 1 and f"⚠ pin is stale → gitpro pin {dc._tilde(repo)}" in e
    rc, j = js(capsys, "status", str(repo))
    assert rc == 1 and j["pin_stale"] is True and j["pinned"] is True


# ---------------------------------------------------------------- 9: the line itself

def expand(value, folder):
    """What the devcontainer CLI does: ${localWorkspaceFolder} replaced by the path, as text."""
    return value.replace("${localWorkspaceFolder}", folder)


SHELLS = [s for s in ("sh", "dash", "bash", "busybox") if shutil.which(s)]
NASTY = "/tmp/my work/it's \"q\" $HOME `id` \\x $(id) end "


@pytest.mark.parametrize("shell", SHELLS)
def test_line_runs_pin_with_the_exact_path(fake_bin, tmp_path, shell):  # 9
    fake_bin.add("gitpro", "sys.exit(3)")  # a failing pin must not fail the container start
    argv = [shutil.which(shell)] + (["sh"] if shell == "busybox" else []) + ["-c", expand(LINE, NASTY)]
    r = subprocess.run(argv, capture_output=True, text=True, env={"PATH": os.environ["PATH"]}, timeout=30)
    assert r.returncode == 0, r.stderr
    assert fake_bin.calls("gitpro") == [["pin", "-y", NASTY]]
    empty = tmp_path / "empty-path"
    empty.mkdir()
    r = subprocess.run(argv, capture_output=True, text=True, env={"PATH": str(empty)}, timeout=30)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_line_is_one_string_for_every_host_shell():
    """A string runs in /bin/sh -c on Linux/macOS/WSL and cmd.exe /c on Windows; line 1 ends cmd.exe with 0."""
    assert isinstance(LINE, str)
    first, *rest = LINE.split("\n")
    assert first == "command true || exit 0"
    assert "${localWorkspaceFolder}" in rest and LINE.count("${") == 1
    # pin and exit sit on the read line: nothing after the here-document's first line can ever run
    assert """IFS= read -r dir <<'GITPRO_PATH'; gitpro pin -y "$dir" || true; exit 0""" in rest
    assert rest[-2:] == ["${localWorkspaceFolder}", "GITPRO_PATH"]
    assert "'command' is not recognized" in dc.NOTE


def test_written_line_round_trips(repo, home, capsys):  # 9
    path = devfile(repo)
    assert run("devcontainer", str(repo), "--write", "-y") == 0
    with open(path) as f:
        assert json.load(f)["initializeCommand"] == LINE


def test_typescript_mirrors_the_line():
    ts = (ROOT / "vscode/src/devcontainer.ts").read_text()
    assert "STUB" not in ts
    body = ts.split("export const LINE = [", 1)[1].split("].join(", 1)[0]
    assert json.loads("[" + body.strip().rstrip(",") + "]") == LINE.split("\n")
    assert json.dumps(dc.MERGED) in ts
    assert json.dumps(dc.NOTE) in ts


# ---------------------------------------------------------------- help, capabilities

def test_help_has_the_host_note(capsys):
    with pytest.raises(SystemExit):
        run("devcontainer", "--help")
    o = " ".join(capsys.readouterr().out.split())
    assert " ".join(dc.NOTE.split()) in o


def test_capabilities(home, capsys):
    assert run("--version", "--json") == 0
    caps = json.loads(capsys.readouterr().out)["capabilities"]
    assert "devcontainer" in caps and "devcontainer.write" in caps


# ---------------------------------------------------------------- QA fixes: refusals and line endings

def test_json_write_needs_yes(repo, home, capsys):
    path = devfile(repo)
    rc, o, e = out(capsys, str(repo), "--write", "--json")
    j = json.loads(o)
    assert rc == 2 and j["ok"] is False and "needs -y" in j["error"] and e == ""
    assert path.read_text() == ORIGINAL and state(home) == []


def test_symlink_inside_repo_is_written(repo, home, capsys):
    real = repo / "config/dev.json"
    real.parent.mkdir()
    real.write_text(ORIGINAL)
    (repo / ".devcontainer").mkdir()
    (repo / ".devcontainer/devcontainer.json").symlink_to("../config/dev.json")
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 0, e
    assert (repo / ".devcontainer/devcontainer.json").is_symlink()
    assert json.loads(real.read_text())["initializeCommand"] == LINE


def test_symlink_out_of_repo_is_refused(repo, home, tmp_path, capsys):
    outside = tmp_path / "elsewhere.json"
    outside.write_text(ORIGINAL)
    (repo / ".devcontainer").mkdir()
    (repo / ".devcontainer/devcontainer.json").symlink_to(outside)
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 2 and "outside the repo" in e and "add the key by hand (printed above)" in e
    assert f'"initializeCommand": {json.dumps(LINE, ensure_ascii=False)}' in o
    assert outside.read_text() == ORIGINAL and state(home) == []
    rc, o, _ = out(capsys, str(repo), "--write", "-y", "--json")
    assert rc == 2 and "outside the repo" in json.loads(o)["error"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write a 0444 file")
def test_read_only_file(repo, home, capsys):
    path = devfile(repo)
    path.chmod(0o444)
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 2 and "is read-only" in e
    assert path.read_text() == ORIGINAL and state(home) == []


def test_failed_write_leaves_no_backup_or_undo(repo, home, capsys, monkeypatch):
    from gitpro import files

    def boom(*a, **k):
        raise OSError("disk full")
    path = devfile(repo)
    monkeypatch.setattr(files, "atomic_write", boom)
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 2 and "disk full" in e
    assert path.read_text() == ORIGINAL
    assert [n for n in state(home) if n.startswith(("backup-", "devcontainer-undo-"))] == []


def test_crlf_file_stays_crlf(repo, home, capsys):  # jsonfile (01): line endings kept, backup byte-exact
    path = repo / ".devcontainer/devcontainer.json"
    path.parent.mkdir()
    original = b'{\r\n    "name": "x",\r\n    "image": "y"\r\n}\r\n'
    path.write_bytes(original)
    rc, o, e = out(capsys, str(repo), "--write", "-y")
    assert rc == 0, e
    data = path.read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n") == 5
    assert json.loads(data)["initializeCommand"] == LINE
    backup = next((home / ".local/state/gitpro").glob("backup-*/devcontainer.json"))
    assert backup.read_bytes() == original
    assert "\r" not in o   # the preview diff is shown with plain line ends


def test_lf_file_stays_lf(repo, home, capsys):
    path = devfile(repo)
    assert run("devcontainer", str(repo), "--write", "-y") == 0
    assert b"\r" not in path.read_bytes()
