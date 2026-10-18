"""Fixes from the 0.4.1 integrity review: secrets that reached output, git environment leaks, trailers read
differently by `why` than by the commit-msg hook, and writes that an interruption could truncate."""
import json

from conftest import DEFAULT_CONFIG, git, repo_with_remote, run
from gitpro import commitcheck, config, resolve, trailers

FAKE_URL = "https://github.com/corp-org/app.git?access_token=ghp_FAKEFAKEFAKE"


# ---------------------------------------------------------------- 1: why --json masks a URL token

def test_why_json_masks_a_token_in_the_remote_url(cfg, tmp_path, capsys):
    cfg()
    repo = repo_with_remote(tmp_path, "app", FAKE_URL)
    capsys.readouterr()
    run("why", "--json", "--committer", "me@personal.test", str(repo))
    out = capsys.readouterr().out
    assert "ghp_FAKEFAKEFAKE" not in out
    assert any(p["code"] == "committer" for p in json.loads(out)["commit"]["problems"])


# ---------------------------------------------------------------- 7: trailers as the commit-msg hook reads them

def test_check_commit_reads_trailers_like_the_hook(cfg, home, tmp_path):
    text = DEFAULT_CONFIG.replace('gh_account = "me-work"\n',
                                  'gh_account = "me-work"\n\n[identity.trailers]\nco_authored_by = "forbid"\n')
    path = cfg(text)
    repo = repo_with_remote(tmp_path, "app", "git@github.com:corp-org/app.git")
    msg = "subj\n\nbody\n---\n\nCo-Authored-By: Bot <bot@x.test>\n"
    rep = resolve.compare(repo, config.load_all(path, "user config"))
    cfg_ = config.load_all(path, "user config")
    hook = trailers.violations(trailers.parse_text(msg), trailers.policy_for(rep.expected.match.identity,
                                                                              cfg_.default))
    assert hook                                           # the hook blocks this message
    got = commitcheck.judge(rep, cfg_, message=msg)
    assert [p["code"] for p in got["problems"]] == ["trailer"]


# ---------------------------------------------------------------- 2: the MCP backstop masks every string

def test_mcp_redact_masks_a_query_token_without_an_at_sign():
    from gitpro import mcp
    url = "https://github.com/o/r.git?access_token=ghp_FAKEFAKEFAKE"
    got = mcp.redact({"a": [url], url: "plain text, 3 items"})
    assert "ghp_FAKEFAKEFAKE" not in json.dumps(got)
    assert got["a"] == ["https://github.com/o/r.git?access_token=***"]
    assert mcp.redact("nothing to hide: a/b c") == "nothing to hide: a/b c"
    assert "ghp_FAKEFAKEFAKE" not in mcp.Server.failed(f"cannot reach {url}")["content"][0]["text"]


# ---------------------------------------------------------------- 3: keys --health masks gh's stderr

import test_health as th                                  # noqa: E402  (its fake gh and config)

fakes = th.fakes
LEAKY_GH = th.GH.replace('if argv[:2] == ["ssh-key", "list"]:',
                         'if argv[:2] == ["ssh-key", "list"]:\n    sys.stderr.write("HTTP 500 boom token=" '
                         '+ env.get("GH_TOKEN", "") + "\\n"); sys.exit(1)\nif 0:')


def test_health_masks_the_token_in_a_failing_gh_listing(home, cfg, fakes, capsys):
    th.setup(cfg, home, th.ONLINE_CONFIG)
    fakes.add("gh", LEAKY_GH)
    assert run("keys", "--health", "--online", "--json") in (0, 1)
    out = capsys.readouterr()
    assert th.TOKEN not in out.out + out.err
    assert "HTTP 500 boom" in out.out


# ---------------------------------------------------------------- 4: scan --lost keeps secrets out of the duplicates key

def test_dup_key_has_no_userinfo_query_or_fragment():
    from gitpro import lost
    for url in ("https://x:PW_FAKE@example.com/", "https://example.com/?access_token=Q_FAKE",
                "https://example.com/#frag_FAKE", "https://x:PW_FAKE@example.com/?access_token=Q_FAKE"):
        key = lost.dup_key(url, "/tmp/r", None)
        assert not any(w in key.upper() for w in ("PW_FAKE", "Q_FAKE", "FRAG_FAKE", "ACCESS_TOKEN")), key
    same = "github.com/corp-org/app"          # a normal remote keeps the key it had
    assert lost.dup_key("git@github.com:Corp-Org/app.git", "/r", None) == same
    assert lost.dup_key("https://u:p@github.com/corp-org/app.git", "/r", None) == same
    assert lost.dup_key("../other", "/r/x", None) == "/r/other"


def test_lost_redact_masks_what_forge_redact_masks_with_its_own_mask():
    from gitpro import lost
    text = "see https://h.test/o/r?access_token=ghp_FAKEFAKEFAKE and gldt-FAKEFAKEFAKE and ghp_FAKEFAKEFAKE"
    got = lost.redact(text)
    assert "FAKEFAKEFAKE" not in got and "***" not in got and got.count("[REDACTED]") == 3
    assert lost.redact("https://x:pw@h.test/o") == "https://[REDACTED]@h.test/o"
    assert lost.redact("ssh://git@h.test/o") == "ssh://git@h.test/o"


# ---------------------------------------------------------------- 6: agent install --scope project ignores GIT_COMMON_DIR

def test_agent_install_project_scope_ignores_a_foreign_git_common_dir(home, tmp_path, monkeypatch, capsys):
    repo = repo_with_remote(tmp_path, "proj", "git@github.com:corp-org/app.git")
    other = repo_with_remote(tmp_path, "other", "git@github.com:me/other.git")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_COMMON_DIR", str(other / ".git"))   # as a hook of another repo would leave it
    before = (other / ".git/info/exclude").read_bytes() if (other / ".git/info/exclude").exists() else None
    assert run("agent", "install", "--scope", "project", "-y") == 0
    assert ".claude/settings.local.json" in (repo / ".git/info/exclude").read_text().splitlines()
    after = (other / ".git/info/exclude").read_bytes() if (other / ".git/info/exclude").exists() else None
    assert after == before


# ---------------------------------------------------------------- 8: interrupted writes

import os                                                 # noqa: E402
import stat                                               # noqa: E402
import subprocess                                         # noqa: E402
import sys                                                # noqa: E402

import pytest                                             # noqa: E402

from conftest import TIMEOUT                              # noqa: E402
from gitpro import files, incident                        # noqa: E402


@pytest.fixture
def synced(monkeypatch):
    """The kinds ('file' or 'dir') of what os.fsync was asked to flush, in order."""
    seen = []
    real = os.fsync

    def fsync(fd):
        seen.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real(fd)
    monkeypatch.setattr(os, "fsync", fsync)
    return seen


def test_atomic_write_syncs_the_file_then_the_directory(tmp_path, synced):
    files.atomic_write(tmp_path / "f.txt", "x", 0o600)
    assert synced == ["file", "dir"]


def test_atomic_write_writes_str_as_utf8_whatever_the_locale(tmp_path):
    target = tmp_path / "f.txt"
    code = ("import sys; from pathlib import Path; from gitpro import files; "
            "files.atomic_write(Path(sys.argv[1]), 'caf\\u00e9 \\u2192 ok', 0o600)")
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
           "PYTHONIOENCODING": "ascii"}
    r = subprocess.run([sys.executable, "-c", code, str(target)], capture_output=True, text=True, env=env,
                       timeout=TIMEOUT)
    assert r.returncode == 0, r.stderr
    assert target.read_bytes() == "café → ok".encode("utf-8")


def test_incident_write_is_synced(tmp_path, synced):
    incident._write(tmp_path / "last-crash.json", "{}")
    assert synced == ["file", "dir"] and oct((tmp_path / "last-crash.json").stat().st_mode & 0o777) == "0o600"


def test_a_backup_is_synced(tmp_path, synced):
    target = tmp_path / "f.txt"
    target.write_text("old")
    bak = files._backup(target)
    assert bak.read_text() == "old" and synced == ["file"]


def test_init_writes_the_config_atomically(home, monkeypatch, capsys):
    target = home / ".config" / "gitpro.toml"
    real = os.fsync

    def interrupted(fd):
        raise KeyboardInterrupt
    monkeypatch.setattr(os, "fsync", interrupted)
    assert run("init", "-y") != 0                       # interrupted
    assert not target.exists() and not [p for p in target.parent.iterdir()]   # no truncated config, no temp file
    monkeypatch.setattr(os, "fsync", real)
    assert run("init", "-y") == 0 and target.stat().st_size > 0
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    capsys.readouterr()
    assert run("init", "-y") == 0 and "already exists, left unchanged" in capsys.readouterr().out
