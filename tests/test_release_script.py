"""scripts/release.py (spec 20, sections 6 and 10). Every gh, git, HTTP and browser call is faked;
no test touches the network."""
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_script", ROOT / "scripts" / "release.py")
rel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rel)
REAL_RELEASE_VERSION = rel.release_version

LOGIN = "releaser"
V = "9.8.7"
HEAD = "a" * 40
OTHER = "b" * 40
VSIX = b"the extension"
GATE_IDS = {"release": 11, "pypi": 12, "marketplace": 13, "mcp-registry": 14, "github-pages": 15, "surprise": 16}
WRITES = (["workflow", "run"], ["api", "-X", "POST"])


def done(name):
    return {"name": name, "status": "completed", "conclusion": "success"}


def waiting(name):
    return {"name": name, "status": "waiting", "conclusion": ""}


def gate(env, can=True):
    return {"environment": {"id": GATE_IDS[env], "name": env}, "current_user_can_approve": can}


def cp(out="", rc=0, err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class World:
    """The repo, GitHub, PyPI, the Marketplace, the registry and the site, as the script sees them.
    `steps` is the run as a list of (run view, pending deployments); the last step repeats."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.git = {"branch": "main", "status": "", "head": HEAD, "upstream": HEAD, "local_tag": False}
        self.why_rc = 0
        self.login = LOGIN
        self.remote_tag = "absent"            # absent | present | error
        self.on_pypi = False
        self.before = [{"databaseId": 5, "headSha": HEAD, "createdAt": "2026-10-17T10:00:00Z"}]
        self.after = [{"databaseId": 7, "headSha": HEAD, "createdAt": "2026-10-17T12:00:00Z"},
                      {"databaseId": 6, "headSha": OTHER, "createdAt": "2026-10-17T12:00:01Z"}]
        self.dispatched = False
        self.steps = [({"status": "completed", "conclusion": "success", "jobs": [done("github")]}, [])]
        self.step = 0
        self.market = []                      # versions listed; becomes [V] after market_after looks
        self.market_after = 0
        self.market_looks = 0
        self.vsix = VSIX
        self.sums_vsix = VSIX
        self.answers = []
        self.asked = []
        self.calls = []
        self.events = []
        self.clock = 0.0
        self.run_info = {"workflowName": "Release", "headBranch": "main", "headSha": HEAD}

    # -- seams
    def cmd(self, argv):
        self.calls.append(("cmd", list(argv)))
        g = self.git
        if argv[:2] == ["gitpro", "why"]:
            return cp(rc=self.why_rc, err="✗ identity mismatch" if self.why_rc else "")
        a = argv[1:]
        if a == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return cp(g["branch"] + "\n")
        if a[:1] == ["status"]:
            return cp(g["status"])
        if a[:1] == ["fetch"]:
            return cp()
        if a == ["rev-parse", "HEAD"]:
            return cp(g["head"] + "\n")
        if a == ["rev-parse", "origin/main"]:
            return cp(g["upstream"] + "\n")
        if a[:3] == ["rev-parse", "-q", "--verify"]:
            return cp(rc=0 if g["local_tag"] else 1)
        raise AssertionError(f"unexpected command {argv}")

    def gh(self, acc, argv, stdin=None):
        assert acc.login == LOGIN and acc.cli == "gh"
        self.calls.append(("gh", list(argv)))
        if argv[:2] == ["api", "user"]:
            return cp(self.login + "\n")
        if argv[:1] == ["api"] and "/git/ref/tags/" in argv[1]:
            return {"absent": cp(rc=1, err="gh: Not Found (HTTP 404)"), "present": cp('{"ref": "x"}'),
                    "error": cp(rc=1, err="gh: Server Error (HTTP 502)")}[self.remote_tag]
        if argv[:2] == ["run", "list"]:
            return cp(json.dumps(self.before + (self.after if self.dispatched else [])))
        if argv[:2] == ["workflow", "run"]:
            self.dispatched = True
            return cp()
        if argv[:2] == ["run", "view"]:
            if argv[-1] == "workflowName,headBranch,headSha":
                return cp(json.dumps(self.run_info))
            view, _ = self.steps[min(self.step, len(self.steps) - 1)]
            return cp(json.dumps(view))
        if argv[:3] == ["api", "-X", "POST"]:
            self.events.append(("post", json.loads(stdin)))
            return cp()
        if argv[:1] == ["api"] and argv[1].endswith("/pending_deployments"):
            _, pending = self.steps[min(self.step, len(self.steps) - 1)]
            self.step += 1
            return cp(json.dumps(pending))
        if argv[:2] == ["release", "download"]:
            dest = Path(argv[argv.index("--dir") + 1])
            (dest / f"gitpro-{V}.vsix").write_bytes(self.vsix)
            (dest / "SHA256SUMS").write_text(f"{hashlib.sha256(self.sums_vsix).hexdigest()}  gitpro-{V}.vsix\n")
            return cp()
        if argv[:2] == ["release", "view"]:
            names = [f"gitpro-{V}-py3-none-any.whl", f"gitpro-{V}.tar.gz", f"gitpro-{V}.vsix", "SHA256SUMS"]
            return cp(json.dumps({"assets": [{"name": n} for n in names]}))
        raise AssertionError(f"unexpected gh call {argv}")

    def http(self, url, body=None, headers=None):
        if url.startswith("https://pypi.org/"):
            if not (self.on_pypi or self.dispatched):
                return 404, ""
            return 200, json.dumps({"urls": [{"packagetype": "bdist_wheel"}, {"packagetype": "sdist"}]})
        if "extensionquery" in url:
            self.market_looks += 1
            listed = self.market + ([V] if self.market_looks > self.market_after else [])
            self.events.append(("market", list(listed)))
            return 200, json.dumps({"results": [{"extensions": [{"versions": [{"version": x} for x in listed]}]}]})
        if url.startswith(rel.REGISTRY):
            return 200, "{}"
        if url == rel.SITE_URL:
            return 200, f'<script>{{"softwareVersion": "{V}"}}</script>'
        raise AssertionError(f"unexpected URL {url}")

    def ask(self, question):
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else False

    def sleep(self, seconds):
        self.clock += seconds
        assert self.clock < 24 * 3600, "the script waited a day of fake time: a loop that never ends"

    # -- reading what happened
    def writes(self):
        return [c for kind, c in self.calls if kind == "gh" and any(c[:len(w)] == w for w in WRITES)]

    def posts(self):
        return [body for kind, body in self.events if kind == "post"]

    def approved(self):
        ids = {v: k for k, v in GATE_IDS.items()}
        return [ids[b["environment_ids"][0]] for b in self.posts()]


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(rel, "cmd", w.cmd)
    monkeypatch.setattr(rel, "gh", w.gh)
    monkeypatch.setattr(rel, "http", w.http)
    monkeypatch.setattr(rel, "ask", w.ask)
    monkeypatch.setattr(rel, "sleep", w.sleep)
    monkeypatch.setattr(rel, "now", lambda: w.clock)
    monkeypatch.setattr(rel, "open_url", lambda url: w.events.append(("open", url)) or True)
    monkeypatch.setattr(rel, "downloads", lambda: tmp_path / "Downloads")
    monkeypatch.setattr(rel, "release_version", lambda: V)
    return w


def release(*args):
    return rel.main(["--as", LOGIN, *args])


def full_run(w):
    """A run that passes every gate: release, pypi, then mcp-registry, with the Marketplace
    listing the version on its second look."""
    w.market_after = 1
    w.steps = [
        ({"status": "in_progress", "conclusion": "", "jobs": [done("check"), waiting("github")]}, [gate("release")]),
        ({"status": "in_progress", "conclusion": "", "jobs": [done("github"), waiting("pypi")]}, [gate("pypi")]),
        ({"status": "in_progress", "conclusion": "", "jobs": [done("github"), done("pypi")]}, [gate("mcp-registry")]),
        ({"status": "completed", "conclusion": "success", "jobs": [done("github"), done("pypi")]}, []),
    ]


# ------------------------------------------------------------------ preflight

PREFLIGHT = {
    "branch": (lambda w: w.git.update(branch="feature"), "not main"),
    "tracked change": (lambda w: w.git.update(status=" M README.md\n"), "tracked files have changes"),
    "behind origin": (lambda w: w.git.update(upstream=OTHER), "is not origin/main"),
    "local tag": (lambda w: w.git.update(local_tag=True), "already exists here"),
    "tag on GitHub": (lambda w: setattr(w, "remote_tag", "present"), "already exists on GitHub"),
    "tag check fails": (lambda w: setattr(w, "remote_tag", "error"), "could not check tag"),
    "on PyPI": (lambda w: setattr(w, "on_pypi", True), "PyPI already has"),
    "gitpro why": (lambda w: setattr(w, "why_rc", 1), "gitpro why exits 1"),
    "other login": (lambda w: setattr(w, "login", "someone-else"), "not releaser"),
}


@pytest.mark.parametrize("case", sorted(PREFLIGHT))
def test_preflight_stops_before_any_question_or_write(world, capsys, case):
    change, reason = PREFLIGHT[case]
    change(world)
    world.answers = [True] * 5
    assert release() == 1
    assert reason in capsys.readouterr().err
    assert world.asked == [] and world.writes() == []


def test_release_version_is_the_one_in_the_version_file():
    sys.path.insert(0, str(ROOT / "scripts"))
    import versions
    text = (ROOT / "src/gitpro/VERSION").read_text(encoding="utf-8").split()[0]
    assert rel.release_version() == versions.version(ROOT) == text


def test_no_at_the_start_starts_nothing(world, capsys):
    world.answers = [False]
    assert release() == 0
    assert world.asked == [f"Start release v{V} from {HEAD[:7]}?"]
    assert world.writes() == [] and "Nothing started" in capsys.readouterr().out


# ------------------------------------------------------------------ the run

def test_the_new_run_is_found_by_sha_and_not_an_older_one(world):
    world.answers = [True]
    assert release() == 0
    views = [c for kind, c in world.calls if kind == "gh" and c[:2] == ["run", "view"]]
    assert views and all(c[2] == "7" for c in views)      # 5 is older, 6 is another commit


def test_no_new_run_found_stops(world, capsys):
    world.answers = [True]
    world.after = [{"databaseId": 6, "headSha": OTHER, "createdAt": "2026-10-17T12:00:01Z"}]
    assert release() == 1
    assert "found no new run" in capsys.readouterr().err


def test_a_full_release_with_yes(world, capsys):
    full_run(world)
    assert release("--yes") == 0
    out = capsys.readouterr().out
    assert world.asked == []
    assert world.approved() == ["release", "pypi", "mcp-registry"]
    assert out.count("✓ ") >= 5 and "✗" not in out
    assert f"https://github.com/{rel.REPO}/actions/runs/7" in out
    assert ("open", rel.PUBLISHER_URL) in world.events


def test_yes_at_a_gate_posts_exactly_the_approval(world):
    full_run(world)
    world.answers = [True] * 4
    assert release() == 0
    assert world.asked[1:] == ["Approve release?", "Approve pypi?", "Approve mcp-registry?"]
    assert world.posts()[0] == {"environment_ids": [11], "state": "approved", "comment": "scripts/release.py"}


def test_no_at_a_gate_exits_0_without_a_post(world, capsys):
    full_run(world)
    world.answers = [True, False]
    assert release() == 0
    assert world.posts() == []
    assert "--run 7" in capsys.readouterr().err


def test_a_gate_this_account_cannot_approve_stops_without_a_post(world, capsys):
    world.steps = [({"status": "in_progress", "conclusion": "", "jobs": []}, [gate("release", can=False)])]
    assert release("--yes") == 1
    assert world.posts() == [] and "cannot approve release" in capsys.readouterr().err


def test_an_unknown_gate_is_never_approved(world, capsys):
    world.steps = [({"status": "in_progress", "conclusion": "", "jobs": []}, [gate("surprise"), gate("release")])]
    assert release("--yes") == 1
    assert world.posts() == [] and "unknown gate surprise" in capsys.readouterr().err


def test_a_failed_job_exits_1_and_approves_nothing_after_it(world, capsys):
    failed = {"name": "ci / test (macos-latest, 3.13)", "status": "completed", "conclusion": "failure"}
    world.steps = [({"status": "in_progress", "conclusion": "", "jobs": [failed]}, [gate("release")])]
    assert release("--yes") == 1
    err = capsys.readouterr().err
    assert world.posts() == []
    assert "ci / test (macos-latest, 3.13) failure" in err and "--log-failed" in err


def test_mcp_registry_waits_until_the_marketplace_lists_the_version(world):
    full_run(world)
    world.market_after = 4
    assert release("--yes") == 0
    mcp = world.events.index(("post", {"environment_ids": [14], "state": "approved",
                                       "comment": "scripts/release.py"}))
    looks = [e for e in world.events[:mcp] if e[0] == "market"]
    assert looks[-1] == ("market", [V]) and all(V not in e[1] for e in looks[:-1])


def test_a_resumed_run_at_the_mcp_registry_gate_waits_for_the_marketplace(world):
    """With --run after pypi: the gate is pending on the first look, before the VSIX step."""
    world.on_pypi = True
    world.market_after = 3
    world.steps = [
        ({"status": "in_progress", "conclusion": "", "jobs": [done("github"), done("pypi")]}, [gate("mcp-registry")]),
        ({"status": "completed", "conclusion": "success", "jobs": [done("github"), done("pypi")]}, []),
    ]
    assert release("--run", "42", "--yes") == 0
    mcp = world.events.index(("post", {"environment_ids": [14], "state": "approved",
                                       "comment": "scripts/release.py"}))
    assert ("market", [V]) in world.events[:mcp]


def test_the_marketplace_not_listing_in_time_stops_before_mcp_registry(world, capsys):
    full_run(world)
    world.market_after = 10 ** 6
    assert release("--yes") == 1
    assert world.approved() == ["release", "pypi"]
    assert "--run 7" in capsys.readouterr().err


def test_a_vsix_that_does_not_match_sha256sums_stops(world, capsys):
    full_run(world)
    world.vsix = b"tampered"
    assert release("--yes") == 1
    assert world.approved() == ["release", "pypi"]
    assert "does not match SHA256SUMS" in capsys.readouterr().err


def test_an_already_listed_version_skips_the_upload(world):
    full_run(world)
    world.market = [V]
    assert release("--yes") == 0
    assert not any(c[:2] == ["release", "download"] for kind, c in world.calls if kind == "gh")


def test_verify_reports_what_is_missing(world, monkeypatch, capsys):
    real = world.http
    monkeypatch.setattr(rel, "http", lambda url, *a: (404, "") if url.startswith(rel.REGISTRY) else real(url, *a))
    assert release("--yes") == 1
    out = capsys.readouterr().out
    assert f"✗ the MCP Registry has {rel.MCP_NAME} {V}" in out and f"✓ gitpro.dev names {V}" in out


# ------------------------------------------------------------------ --run

def test_run_id_follows_that_run_without_starting_one(world):
    world.on_pypi = True                   # that run has published already
    assert release("--run", "42") == 0
    assert world.writes() == []
    views = [c for kind, c in world.calls if kind == "gh" and c[:2] == ["run", "view"]]
    assert views and all(c[2] == "42" for c in views)
    assert [c for kind, c in world.calls if kind == "cmd"] == [["git", "rev-parse", "HEAD"]]


def test_run_id_of_another_workflow_stops(world, capsys):
    world.run_info = {"workflowName": "CI", "headBranch": "main", "headSha": HEAD}
    assert release("--run", "42") == 1
    assert "not Release on main" in capsys.readouterr().err and world.writes() == []


# ------------------------------------------------------------------ the token

def test_nothing_printed_contains_the_token(tmp_path, monkeypatch, fake_bin, capsys):
    """The real gh seam (forge.run) against a fake gh whose error repeats the token."""
    token = "gho_" + "Q" * 36
    fake_bin.add("gh", f'''
token = {token!r}
if argv[:2] == ["auth", "token"]:
    print(token); sys.exit(0)
if argv[:2] == ["api", "user"]:
    print({LOGIN!r}); sys.exit(0)
print("HTTP 502: proxy said Authorization: token " + token, file=sys.stderr)
sys.exit(1)
''')
    w = World(tmp_path)
    monkeypatch.setattr(rel, "cmd", w.cmd)
    monkeypatch.setattr(rel, "http", w.http)
    monkeypatch.setattr(rel, "release_version", lambda: V)
    assert release() == 1
    out, err = capsys.readouterr()
    assert "could not check tag" in err
    assert token not in out + err and "Q" * 20 not in out + err
    assert any(c[:2] == ["api", "user"] for c in fake_bin.calls("gh"))


def test_versions_script_prints_the_version_and_refuses_a_bad_one(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import versions
    (tmp_path / "src/gitpro").mkdir(parents=True)
    vfile = tmp_path / "src/gitpro/VERSION"
    with pytest.raises(ValueError, match="src/gitpro/VERSION"):
        versions.version(tmp_path)
    for text, why in (("1.2  note\n", "is not X.Y.Z"), ("\n", "is not X.Y.Z"), ("v1.2.3 x\n", "is not X.Y.Z")):
        vfile.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match=why):
            versions.version(tmp_path)
    r = subprocess.run([sys.executable, str(ROOT / "scripts/versions.py")], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == rel.release_version()


# ------------------------------------------------------------------ run id, order, and the checks that must bite

def test_run_for_another_commit_is_refused(world, capsys):
    world.run_info = {"workflowName": "Release", "headBranch": "main", "headSha": OTHER}
    assert release("--run", "42") == 1
    err = capsys.readouterr().err
    assert "run 42 is for commit bbbbbbb, but HEAD is aaaaaaa" in err and world.writes() == []


def test_run_on_another_branch_is_refused(world, capsys):
    world.run_info = {"workflowName": "Release", "headBranch": "dev", "headSha": HEAD}
    assert release("--run", "42") == 1
    assert "not Release on main" in capsys.readouterr().err and world.writes() == []


def test_pypi_gate_that_shows_up_while_waiting_is_approved_before_the_vsix_wait(world, monkeypatch):
    """github is done on the poll where pypi is not yet pending; the VSIX wait blocks for minutes, so the
    gate that appears next is approved first."""
    world.on_pypi = True
    world.steps = [
        ({"status": "in_progress", "conclusion": "", "jobs": [done("github"), waiting("pypi")]}, []),
        ({"status": "in_progress", "conclusion": "", "jobs": [done("github"), waiting("pypi")]}, [gate("pypi")]),
        ({"status": "completed", "conclusion": "success", "jobs": [done("github"), done("pypi")]}, []),
    ]
    monkeypatch.setattr(rel, "vsix", lambda acc, v, run_id: world.events.append(("vsix", v)))
    assert release("--run", "42", "--yes") == 0
    kinds = [e[0] for e in world.events]
    assert world.approved() == ["pypi"]
    assert kinds.index("post") < kinds.index("vsix")


def test_a_marketplace_gate_is_approved_and_named(world, capsys):
    world.market_after = 0                 # already listed: no upload wait
    world.on_pypi = True
    world.steps = [
        ({"status": "in_progress", "conclusion": "", "jobs": [done("github")]}, [gate("marketplace")]),
        ({"status": "completed", "conclusion": "success", "jobs": [done("github")]}, []),
    ]
    assert release("--run", "42", "--yes") == 0
    assert world.approved() == ["marketplace"]
    assert "publishes the VSIX to the VS Code Marketplace" in capsys.readouterr().out


def test_ctrl_c_exits_130_and_names_the_run(world, monkeypatch, capsys):
    def interrupted(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(rel, "follow", interrupted)
    assert release("--run", "42") == 130
    assert "--run 42" in capsys.readouterr().err


def test_a_version_that_is_not_x_y_z_stops(world, monkeypatch, capsys):
    monkeypatch.setattr(rel, "release_version", REAL_RELEASE_VERSION)
    monkeypatch.setattr(rel._versions, "version", lambda root=None: (_ for _ in ()).throw(
        ValueError("version 1.2 is not X.Y.Z")))
    assert release("--yes") == 1
    assert "version 1.2 is not X.Y.Z" in capsys.readouterr().err and world.writes() == []


def test_pypi_without_a_wheel_and_an_sdist_fails_the_check(world, monkeypatch, capsys):
    full_run(world)
    real = world.http
    def http(url, body=None, headers=None):
        if url.startswith("https://pypi.org/") and world.dispatched:
            return 200, json.dumps({"urls": [{"packagetype": "bdist_wheel"}]})
        return real(url, body, headers)
    monkeypatch.setattr(rel, "http", http)
    assert release("--yes") == 1
    assert "✗ PyPI lists gitpro" in capsys.readouterr().out


def test_a_release_missing_an_asset_fails_the_check(world, monkeypatch, capsys):
    full_run(world)
    real = world.gh
    def gh(acc, argv, stdin=None):
        if argv[:2] == ["release", "view"]:
            return cp(json.dumps({"assets": [{"name": f"gitpro-{V}.vsix"}]}))
        return real(acc, argv, stdin)
    monkeypatch.setattr(rel, "gh", gh)
    assert release("--yes") == 1
    out = capsys.readouterr().out
    assert "✗" in out and "SHA256SUMS" in out


def test_pypi_answering_200_without_json_stops_cleanly(monkeypatch):
    monkeypatch.setattr(rel, "http", lambda url, body=None, headers=None: (200, "<html>maintenance</html>"))
    with pytest.raises(rel.Stop, match="not with JSON"):
        rel.on_pypi("1.2.3")


def test_workflows_are_safe_to_run_again():
    """release.yml: an existing tag at this commit or an existing release is kept, another sha stops; PyPI
    skips files it has. pages.yml keeps no git credentials."""
    release_yml = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "python3 scripts/versions.py" in release_yml
    assert "skip-existing: true" in release_yml
    assert "gh release upload" in release_yml and "--clobber" in release_yml
    assert "gh release create" in release_yml and "--verify-tag" in release_yml
    assert 'git/ref/tags/$TAG' in release_yml and '"$sha" = "$GITHUB_SHA"' in release_yml
    pages = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    assert "persist-credentials: false" in pages
