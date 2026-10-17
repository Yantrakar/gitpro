#!/usr/bin/env python3
"""Start the Release workflow on main, approve its gates as one account, and check the result.

    python scripts/release.py --as LOGIN [--yes] [--run ID]

Spec: docs/spec/20-release.md, section 6. Every gh call runs as LOGIN through gitpro's
forge.run, so the global gh account is never switched and no token is printed. The VSIX
upload stays manual; the script waits for the Marketplace to list the version.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gitpro import forge  # noqa: E402
from gitpro.config import ConfigError  # noqa: E402

REPO = "Yantrakar/gitpro"
WORKFLOW = "release.yml"
EXTENSION = "yantrakar.gitpro"
MCP_NAME = "io.github.Yantrakar/gitpro"
PUBLISHER_URL = "https://marketplace.visualstudio.com/manage/publishers/yantrakar"
SITE_URL = "https://gitpro.dev/"
MARKETPLACE_QUERY = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
REGISTRY = "https://registry.modelcontextprotocol.io"

POLL = 15                 # seconds between looks at the run
FIND_TRIES = 12           # looks for the new run after starting it, POLL / 3 apart
MARKETPLACE_POLL = 30
MARKETPLACE_WAIT = 60 * 60
SITE_POLL = 30
SITE_WAIT = 10 * 60       # GitHub Pages sends Cache-Control: max-age=600

GATES = {
    "release": "creates tag v{v} and the GitHub release (wheel, sdist, VSIX, SHA256SUMS)",
    "pypi": "publishes gitpro {v} to PyPI (a version can never be uploaded again)",
    "marketplace": "publishes the VSIX to the VS Code Marketplace",
    "mcp-registry": "publishes server.json {v} to the MCP Registry (it cannot be changed later)",
}
FAILED = {"failure", "cancelled", "timed_out", "startup_failure"}


class Stop(Exception):
    """Ends the script with `message` on stderr and exit code `code`."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ calls the tests replace

def cmd(argv: List[str]) -> subprocess.CompletedProcess:
    """argv in the checkout, captured text; 127 when the program is missing."""
    try:
        return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=120)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: command not found\n")


def gh(acc: forge.Account, argv: List[str], stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    return forge.run(acc, argv, timeout=120, **({"stdin": stdin} if stdin is not None else {}))


def http(url: str, body: Optional[dict] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """(status, text) of a GET, or of a JSON POST when body is given; status 0 when unreachable."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "gitpro-release", **(headers or {})})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError):
        return 0, ""


def open_url(url: str) -> bool:
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if not shutil.which(opener):
        return False
    try:
        subprocess.Popen([opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def ask(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def now() -> float:
    return time.monotonic()


def downloads() -> Path:
    return Path.home() / "Downloads"


# ------------------------------------------------------------------ helpers

def clean(text: str) -> str:
    """text for the terminal: tokens masked, control characters escaped, line by line."""
    return "\n".join(forge.safe(forge.redact(line)) for line in text.split("\n"))


def say(text: str = "") -> None:
    print(clean(text), flush=True)


def why_failed(r: subprocess.CompletedProcess) -> str:
    text = (r.stderr or r.stdout or "").strip().splitlines()
    return forge.redact(text[-1] if text else f"exit {r.returncode}")


def gh_ok(acc: forge.Account, argv: List[str], what: str, stdin: Optional[str] = None) -> str:
    try:
        r = gh(acc, argv, stdin)
    except ConfigError as e:
        raise Stop(str(e)) from None
    if r.returncode != 0:
        raise Stop(f"{what}: {why_failed(r)}")
    return r.stdout


def gh_json(acc: forge.Account, argv: List[str], what: str):
    out = gh_ok(acc, argv, what)
    try:
        return json.loads(out)
    except ValueError:
        raise Stop(f"{what}: gh did not return JSON") from None


def versions() -> Dict[str, str]:
    """{place: version} for every file that names the release version."""
    def load(rel: str) -> dict:
        return json.loads((ROOT / rel).read_text(encoding="utf-8"))
    init = (ROOT / "src/gitpro/__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([^"]+)"', init, re.M)
    server = load("server.json")
    out = {"src/gitpro/__init__.py": m[1] if m else "",
           "vscode/package.json": load("vscode/package.json").get("version", ""),
           "server.json version": server.get("version", ""),
           ".claude-plugin/plugin.json": load(".claude-plugin/plugin.json").get("version", "")}
    for i, p in enumerate(server.get("packages", [])):
        out[f"server.json packages[{i}]"] = p.get("version", "")
    for i, p in enumerate(load(".claude-plugin/marketplace.json").get("plugins", [])):
        out[f".claude-plugin/marketplace.json plugins[{i}]"] = p.get("version", "")
    return out


def release_version() -> str:
    found = versions()
    distinct = set(found.values())
    if len(distinct) != 1 or "" in distinct:
        raise Stop("the versions don't match: " + ", ".join(f"{k} {v or '(none)'}" for k, v in found.items()))
    v = distinct.pop()
    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        raise Stop(f"version {v} is not X.Y.Z")
    return v


def git_out(*args: str) -> str:
    r = cmd(["git", *args])
    if r.returncode != 0:
        raise Stop(f"git {args[0]}: {why_failed(r)}")
    return r.stdout.strip()


def check_login(acc: forge.Account) -> None:
    login = gh_ok(acc, ["api", "user", "--jq", ".login"], f"gh as {acc.login}").strip()
    if login.lower() != acc.login.lower():
        raise Stop(f"gh api user names {login or 'no one'}, not {acc.login}")


def on_pypi(v: str) -> Optional[dict]:
    status, text = http(f"https://pypi.org/pypi/gitpro/{v}/json")
    if status == 404:
        return None
    if status != 200:
        raise Stop(f"PyPI answered {status or 'nothing'} for gitpro {v}")
    return json.loads(text)


def marketplace_versions() -> Optional[List[str]]:
    """The versions the Marketplace lists for the extension; None when it can't be asked."""
    status, text = http(MARKETPLACE_QUERY,
                        {"filters": [{"criteria": [{"filterType": 7, "value": EXTENSION}]}], "flags": 1},
                        {"Accept": "application/json;api-version=7.2-preview.1"})
    if status != 200:
        return None
    try:
        exts = json.loads(text)["results"][0]["extensions"]
        return [x["version"] for x in exts[0]["versions"]] if exts else []
    except (ValueError, KeyError, IndexError, TypeError):
        return None


# ------------------------------------------------------------------ steps

def preflight(acc: forge.Account) -> Tuple[str, str]:
    """Step 1: (version, HEAD sha), or Stop with the reason. Reads only."""
    branch = git_out("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        raise Stop(f"on branch {branch}, not main")
    if git_out("status", "--porcelain", "--untracked-files=no"):
        raise Stop("tracked files have changes; commit or stash them")
    git_out("fetch", "--quiet", "origin", "main")
    head, upstream = git_out("rev-parse", "HEAD"), git_out("rev-parse", "origin/main")
    if head != upstream:
        raise Stop(f"HEAD {head[:7]} is not origin/main {upstream[:7]}")
    v = release_version()
    tag = f"v{v}"
    if cmd(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"]).returncode == 0:
        raise Stop(f"tag {tag} already exists here")
    check_login(acc)
    r = gh(acc, ["api", f"repos/{REPO}/git/ref/tags/{tag}"])
    if r.returncode == 0:
        raise Stop(f"tag {tag} already exists on GitHub")
    if "HTTP 404" not in (r.stderr + r.stdout):
        raise Stop(f"could not check tag {tag} on GitHub: {why_failed(r)}")
    if on_pypi(v) is not None:
        raise Stop(f"PyPI already has gitpro {v}")
    r = cmd(["gitpro", "why"])
    if r.returncode != 0:
        raise Stop(f"gitpro why exits {r.returncode}: {why_failed(r)}")
    return v, head


def dispatch_runs(acc: forge.Account) -> List[dict]:
    return gh_json(acc, ["run", "list", "-R", REPO, "--workflow", WORKFLOW, "--event", "workflow_dispatch",
                         "--limit", "20", "--json", "databaseId,headSha,createdAt"], "gh run list")


def start(acc: forge.Account, v: str, head: str, yes: bool) -> Optional[int]:
    """Step 2: the new run's ID, or None when the user says no."""
    if not (yes or ask(f"Start release v{v} from {head[:7]}?")):
        return None
    before = {r["databaseId"] for r in dispatch_runs(acc)}
    gh_ok(acc, ["workflow", "run", WORKFLOW, "-R", REPO, "--ref", "main"], "gh workflow run")
    for _ in range(FIND_TRIES):
        sleep(POLL / 3)
        new = [r for r in dispatch_runs(acc) if r["databaseId"] not in before and r["headSha"] == head]
        if new:
            return max(new, key=lambda r: r["createdAt"])["databaseId"]
    raise Stop(f"started {WORKFLOW}, but found no new run on {head[:7]}; "
               f"see https://github.com/{REPO}/actions/workflows/{WORKFLOW}")


def run_url(run_id: int) -> str:
    return f"https://github.com/{REPO}/actions/runs/{run_id}"


def vsix(acc: forge.Account, v: str, run_id: int) -> None:
    """Step 4: download and check the VSIX, then wait until the Marketplace lists v."""
    listed = marketplace_versions()
    if listed is not None and v in listed:
        say(f"✓ the Marketplace lists {v}")
        return
    dest = downloads() / f"gitpro-{v}"
    dest.mkdir(parents=True, exist_ok=True)
    name = f"gitpro-{v}.vsix"
    gh_ok(acc, ["release", "download", f"v{v}", "-R", REPO, "--dir", str(dest), "--clobber",
                "--pattern", name, "--pattern", "SHA256SUMS"], "gh release download")
    sums = {}
    for line in (dest / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0].lower()
    got = hashlib.sha256((dest / name).read_bytes()).hexdigest()
    if sums.get(name) != got:
        raise Stop(f"{name} does not match SHA256SUMS; do not upload it")
    path = dest / name
    say(f"✓ {path} matches SHA256SUMS")
    if not open_url(PUBLISHER_URL):
        say(f"Open {PUBLISHER_URL}")
    say(f"Upload {path} with ⋯ → Update. Waiting for the Marketplace to list {v}…")
    deadline = now() + MARKETPLACE_WAIT
    while now() < deadline:
        sleep(MARKETPLACE_POLL)
        listed = marketplace_versions()
        if listed is not None and v in listed:
            say(f"✓ the Marketplace lists {v}")
            return
    raise Stop(f"the Marketplace doesn't list {v} after {MARKETPLACE_WAIT // 60} minutes; upload {path} "
               f"at {PUBLISHER_URL}, then run again with --run {run_id}")


def approve(acc: forge.Account, run_id: int, gate: dict, v: str, yes: bool) -> None:
    env = gate["environment"]["name"]
    if not gate.get("current_user_can_approve"):
        raise Stop(f"{acc.login} cannot approve {env}; approve it at {run_url(run_id)}")
    say(f"{env}: {GATES[env].format(v=v)}")
    if not (yes or ask(f"Approve {env}?")):
        raise Stop(f"not approved; the run waits at {run_url(run_id)} (finish it there, "
                   f"or later with --run {run_id})", 0)
    body = {"environment_ids": [gate["environment"]["id"]], "state": "approved", "comment": "scripts/release.py"}
    gh_ok(acc, ["api", "-X", "POST", f"repos/{REPO}/actions/runs/{run_id}/pending_deployments", "--input", "-"],
          f"approving {env}", stdin=json.dumps(body))
    say(f"✓ approved {env}")


def follow(acc: forge.Account, run_id: int, v: str, yes: bool) -> None:
    """Step 3 (and step 4 when its turn comes): until the run succeeds, or Stop."""
    approved, vsix_done = set(), False
    while True:
        run = gh_json(acc, ["run", "view", str(run_id), "-R", REPO, "--json", "status,conclusion,jobs"],
                      "gh run view")
        jobs = run.get("jobs") or []
        for job in jobs:
            if job.get("conclusion") in FAILED:
                raise Stop(f"job {job.get('name')} {job['conclusion']}: {run_url(run_id)}\n"
                           f"  gh run view {run_id} -R {REPO} --log-failed")
        if run.get("status") == "completed":
            if run.get("conclusion") == "success":
                return
            raise Stop(f"the run ended {run.get('conclusion')}: {run_url(run_id)}")
        pending = gh_json(acc, ["api", f"repos/{REPO}/actions/runs/{run_id}/pending_deployments"],
                          "pending deployments")
        later = []
        for gate in pending:
            env = gate["environment"]["name"]
            if env not in GATES:
                raise Stop(f"unknown gate {env}; nothing approved. See {run_url(run_id)}")
            if env in approved:
                continue
            if env == "mcp-registry":
                later.append(gate)
                continue
            approve(acc, run_id, gate, v, yes)
            approved.add(env)
        github_done = any(j.get("name") == "github" and j.get("conclusion") == "success" for j in jobs)
        if github_done and not vsix_done:
            vsix(acc, v, run_id)
            vsix_done = True
        for gate in later:
            if vsix_done:
                approve(acc, run_id, gate, v, yes)
                approved.add(gate["environment"]["name"])
        sleep(POLL)


def verify(acc: forge.Account, v: str) -> bool:
    """Step 5: one line per place; True when all five have v."""
    checks = []
    try:
        files = {u["packagetype"] for u in (on_pypi(v) or {}).get("urls", [])}
        checks.append(("PyPI lists gitpro " + v + " with a wheel and an sdist",
                       {"bdist_wheel", "sdist"} <= files))
    except Stop:
        checks.append((f"PyPI lists gitpro {v}", False))
    r = gh(acc, ["release", "view", f"v{v}", "-R", REPO, "--json", "assets"])
    names = set()
    if r.returncode == 0:
        try:
            names = {a["name"] for a in json.loads(r.stdout)["assets"]}
        except (ValueError, KeyError, TypeError):
            pass
    want = {f"gitpro-{v}-py3-none-any.whl", f"gitpro-{v}.tar.gz", f"gitpro-{v}.vsix", "SHA256SUMS"}
    checks.append((f"the GitHub release v{v} has the wheel, the sdist, the VSIX and SHA256SUMS", want <= names))
    status, _ = http(f"{REGISTRY}/v0.1/servers/{urllib.parse.quote(MCP_NAME, safe='')}/versions/{v}")
    checks.append((f"the MCP Registry has {MCP_NAME} {v}", status == 200))
    listed = marketplace_versions()
    checks.append((f"the Marketplace lists {v}", listed is not None and v in listed))
    deadline, site = now() + SITE_WAIT, False
    while True:
        status, text = http(SITE_URL)
        site = status == 200 and f'"softwareVersion": "{v}"' in text
        if site or now() >= deadline:
            break
        sleep(SITE_POLL)
    checks.append((f"gitpro.dev names {v}", site))
    for text, ok in checks:
        say(("✓ " if ok else "✗ ") + text)
    return all(ok for _, ok in checks)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="scripts/release.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--as", dest="login", required=True, metavar="LOGIN",
                   help="the GitHub account that starts the run and approves its gates")
    p.add_argument("--yes", action="store_true", help="answer yes to every question")
    p.add_argument("--run", type=int, metavar="ID", help="follow an existing Release run instead of starting one")
    args = p.parse_args(argv)
    acc = forge.Account(forge.GITHUB, "gh", args.login)
    try:
        if args.run is None:
            v, head = preflight(acc)
            run_id = start(acc, v, head, args.yes)
            if run_id is None:
                say("Nothing started.")
                return 0
        else:
            v = release_version()
            check_login(acc)
            run_id = args.run
            info = gh_json(acc, ["run", "view", str(run_id), "-R", REPO, "--json", "workflowName,headBranch"],
                           "gh run view")
            if info.get("workflowName") != "Release" or info.get("headBranch") != "main":
                raise Stop(f"run {run_id} is {info.get('workflowName')} on {info.get('headBranch')}, "
                           "not Release on main")
        say(f"Release v{v}: {run_url(run_id)}")
        follow(acc, run_id, v, args.yes)
        say(f"✓ the run succeeded: {run_url(run_id)}")
        return 0 if verify(acc, v) else 1
    except Stop as e:
        print(clean(str(e)), file=sys.stderr, flush=True)
        return e.code
    except KeyboardInterrupt:
        print("\nStopped. Pick the run up again with --run ID.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
