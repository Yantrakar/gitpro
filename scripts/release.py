#!/usr/bin/env python3
"""Start the Release workflow on main, approve its gates as one account, and check the result.

    python scripts/release.py --as LOGIN [--yes] [--run ID]

Spec: docs/spec/20-release.md, section 6. Every gh call runs as LOGIN through gitpro's
forge.run, so the global gh account is never switched and no token is printed. The
marketplace job publishes the VSIX; when it is skipped (the Azure variables unset), the
script has you upload it by hand. Either way it waits for the Marketplace to list the version.
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

sys.path.insert(0, str(ROOT / "scripts"))
import versions as _versions  # noqa: E402

REPO = "Yantrakar/gitpro"
WORKFLOW = "release.yml"
EXTENSION = "yantrakar.gitpro"
MCP_NAME = "io.github.Yantrakar/gitpro"
PUBLISHER_URL = "https://marketplace.visualstudio.com/manage/publishers/yantrakar"
SITE_URL = "https://gitpro.dev/"
MARKETPLACE_QUERY = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
REGISTRY = "https://registry.modelcontextprotocol.io"
OPENVSX_API = "https://open-vsx.org/api/yantrakar/gitpro"

POLL = 15                 # seconds between looks at the run
HEARTBEAT = 120           # seconds without a progress line before follow() says what it still waits for
FIND_TRIES = 12           # looks for the new run after starting it, POLL / 3 apart
MARKETPLACE_POLL = 30
MARKETPLACE_WAIT = 60 * 60
MARKETPLACE_REMIND = 5 * 60
SITE_POLL = 30
SITE_WAIT = 10 * 60       # GitHub Pages sends Cache-Control: max-age=600

GATES = {
    "release": "creates tag v{v} and the GitHub release (wheel, sdist, VSIX, SHA256SUMS)",
    "pypi": "publishes gitpro {v} to PyPI (a version can never be uploaded again)",
    "marketplace": "publishes the VSIX to the VS Code Marketplace",
    "openvsx": "publishes the VSIX to Open VSX",
    "mcp-registry": "publishes server.json {v} to the MCP Registry (it cannot be changed later)",
}
FAILED = {"failure", "cancelled", "timed_out", "startup_failure"}
MARKS = {"success": "✓", "skipped": "–", **{c: "✗" for c in FAILED}}


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


def mins(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}m{s % 60:02d}s"


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


def release_version() -> str:
    """The release version: the content of src/gitpro/VERSION, the only place it is written."""
    try:
        return _versions.version(ROOT)
    except ValueError as e:
        raise Stop(str(e)) from None


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
    try:
        return json.loads(text)
    except ValueError:
        raise Stop(f"PyPI answered 200 for gitpro {v}, but not with JSON") from None


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
    """Step 1, the checks that hold for a new run and a resumed one: (version, HEAD sha), or Stop with
    the reason. Reads only."""
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
    check_login(acc)
    r = cmd(["gitpro", "why"])
    if r.returncode != 0:
        raise Stop(f"gitpro why exits {r.returncode}: {why_failed(r)}")
    return v, head


def published(acc: forge.Account, v: str) -> Optional[str]:
    """What of release v already exists (the tag here or on GitHub, PyPI's upload), or None. Reads only."""
    tag = f"v{v}"
    if cmd(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"]).returncode == 0:
        return f"tag {tag} already exists here"
    r = gh(acc, ["api", f"repos/{REPO}/git/ref/tags/{tag}"])
    if r.returncode == 0:
        return f"tag {tag} already exists on GitHub"
    if "HTTP 404" not in (r.stderr + r.stdout):
        raise Stop(f"could not check tag {tag} on GitHub: {why_failed(r)}")
    if on_pypi(v) is not None:
        return f"PyPI already has gitpro {v}"
    return None


def dispatch_runs(acc: forge.Account) -> List[dict]:
    return gh_json(acc, ["run", "list", "-R", REPO, "--workflow", WORKFLOW, "--event", "workflow_dispatch",
                         "--limit", "20", "--json", "databaseId,headSha,createdAt,status,conclusion"], "gh run list")


def latest_run(acc: forge.Account, head: str) -> Optional[dict]:
    """The newest Release run started by hand on commit head, or None."""
    mine = [r for r in dispatch_runs(acc) if r["headSha"] == head]
    return max(mine, key=lambda r: r["createdAt"]) if mine else None


def run_attempt(acc: forge.Account, run_id: int) -> int:
    """The run's attempt number, from the REST API (gh run view has no attempt field before gh 2.47)."""
    n = gh_json(acc, ["api", f"repos/{REPO}/actions/runs/{run_id}"], "gh api runs").get("run_attempt")
    return n if isinstance(n, int) else 1


def pick_up(acc: forge.Account, run_id: int, yes: bool) -> bool:
    """For a run that exists: True to follow it. A run that failed or was cancelled gets its failed jobs
    re-run first, when the user says yes (False when they say no). Every job is safe to run again
    (section 8): the tag and the GitHub release are kept, PyPI and the Marketplace skip what they have,
    and mcp-registry skips a version the registry has."""
    run = gh_json(acc, ["run", "view", str(run_id), "-R", REPO, "--json", "status,conclusion,jobs"],
                  "gh run view")
    if run.get("status") != "completed" or run.get("conclusion") == "success":
        return True
    failed = [j.get("name") or "?" for j in run.get("jobs") or [] if j.get("conclusion") in FAILED]
    say(f"Run {run_id} ended {run.get('conclusion')}" + (f" at {', '.join(failed)}" if failed else "")
        + f": {run_url(run_id)}")
    if not (yes or ask("Re-run its failed jobs?")):
        say(f"Nothing re-run. Re-run them on {run_url(run_id)}, or run this script again.")
        return False
    attempt = run_attempt(acc, run_id)
    gh_ok(acc, ["run", "rerun", str(run_id), "--failed", "-R", REPO], "gh run rerun")
    for _ in range(FIND_TRIES):
        sleep(POLL / 3)
        n = run_attempt(acc, run_id)
        if n > attempt:
            say(f"✓ re-running the failed jobs (attempt {n})")
            return True
    raise Stop(f"asked GitHub to re-run the failed jobs, but run {run_id} has no new attempt; see {run_url(run_id)}")


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


def marketplace_job(jobs: List[dict]) -> Optional[dict]:
    """The run's marketplace job (`marketplace / publish`, from the reusable workflow), if it is there yet."""
    return next((j for j in jobs if (j.get("name") or "").split(" / ")[0] == "marketplace"), None)


def vsix_due(jobs: List[dict]) -> bool:
    """Step 4 is due once github has succeeded and the marketplace job has finished: published, or skipped
    because the Azure variables are unset. A failed one stops follow() before this is asked."""
    market = marketplace_job(jobs)
    return (any(j.get("name") == "github" and j.get("conclusion") == "success" for j in jobs)
            and market is not None and market.get("status") == "completed")


def vsix(acc: forge.Account, v: str, run_id: int, jobs: List[dict]) -> None:
    """Step 4: when the marketplace job published, wait until the Marketplace lists v. When it was skipped,
    download and check the VSIX for an upload by hand first."""
    listed = marketplace_versions()
    if listed is not None and v in listed:
        say(f"✓ the Marketplace lists {v}")
        return
    market = marketplace_job(jobs)
    if market is not None and market.get("conclusion") == "success":
        say(f"The marketplace job published {v}. Waiting for the Marketplace to list it…")
        wait_listed(v, run_id, None)
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
    wait_listed(v, run_id, path)


def wait_listed(v: str, run_id: int, path: Optional[Path]) -> None:
    """Poll until the Marketplace lists v, or Stop after MARKETPLACE_WAIT. path: the VSIX to upload by hand,
    None when the marketplace job published it."""
    todo = f"Upload {path} if you haven't." if path else f"Check the extension at {PUBLISHER_URL}."
    began = now()
    deadline, remind = began + MARKETPLACE_WAIT, began + MARKETPLACE_REMIND
    while now() < deadline:
        sleep(MARKETPLACE_POLL)
        listed = marketplace_versions()
        if listed is not None and v in listed:
            say(f"✓ the Marketplace lists {v}")
            return
        if now() >= remind:
            remind += MARKETPLACE_REMIND
            asked = "it could not be asked" if listed is None else f"it lists {', '.join(listed[:3]) or 'nothing'}"
            say(f"… {mins(now() - began)}: the Marketplace doesn't list {v} yet ({asked}); waiting up to "
                f"{MARKETPLACE_WAIT // 60} minutes. {todo}")
    what = f"upload {path} at {PUBLISHER_URL}" if path else f"check the extension at {PUBLISHER_URL}"
    raise Stop(f"the Marketplace doesn't list {v} after {MARKETPLACE_WAIT // 60} minutes; {what}, "
               f"then run again with --run {run_id}")


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


def settle(acc: forge.Account, run_id: int, v: str, yes: bool, approved: set) -> List[dict]:
    """Read the pending gates and approve each one that is due now; return the mcp-registry ones, which wait
    for the extension."""
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
    return later


class Progress:
    """What follow() prints about the run, each line led by the time since it began following: a line
    when a job changes state or starts another step, and after HEARTBEAT seconds without one, a line
    naming what is still running or queued."""

    def __init__(self) -> None:
        self.start = self.last = now()
        self.seen: Dict[str, Tuple[str, Optional[str]]] = {}

    def line(self, text: str) -> None:
        say(f"  {mins(now() - self.start):>7}  {text}")
        self.last = now()

    def update(self, jobs: List[dict]) -> None:
        for job in jobs:
            name = job.get("name") or "?"
            done = job.get("status") == "completed"
            state = (job.get("conclusion") or "done") if done else (job.get("status") or "queued")
            step = None if done else next((s.get("name") for s in job.get("steps") or []
                                           if s.get("status") == "in_progress"), None)
            old = self.seen.get(name)
            if old == (state, step):
                continue
            self.seen[name] = (state, step)
            if old is None or old[0] != state:
                self.line(f"{MARKS.get(state, '·')} {name}: {state.replace('_', ' ')}" + (f", {step}" if step else ""))
            elif step:
                self.line(f"  {name}: {step}")
        if now() - self.last >= HEARTBEAT:
            busy = [f"{n} ({st.replace('_', ' ')}{', ' + s if s else ''})" for n, (st, s) in self.seen.items()
                    if st not in MARKS and st != "done"]
            self.line("… still waiting on " + ", ".join(busy) if busy else "… waiting for the next job to start")


def follow(acc: forge.Account, run_id: int, v: str, yes: bool) -> List[dict]:
    """Step 3 (and step 4 when its turn comes): until the run succeeds, or Stop; returns its jobs."""
    approved, vsix_done = set(), False
    progress = Progress()
    while True:
        run = gh_json(acc, ["run", "view", str(run_id), "-R", REPO, "--json", "status,conclusion,jobs"],
                      "gh run view")
        jobs = run.get("jobs") or []
        progress.update(jobs)
        for job in jobs:
            if job.get("conclusion") in FAILED:
                raise Stop(f"job {job.get('name')} {job['conclusion']}: {run_url(run_id)}\n"
                           f"  gh run view {run_id} -R {REPO} --log-failed\n"
                           "Fix it, then run this script again: it offers to re-run the failed jobs.")
        if run.get("status") == "completed":
            if run.get("conclusion") == "success":
                if not vsix_done:      # the run finished without this script (approved on the page)
                    vsix(acc, v, run_id, jobs)
                return jobs
            raise Stop(f"the run ended {run.get('conclusion')}: {run_url(run_id)}\n"
                       "Run this script again: it offers to re-run the failed jobs.")
        later = settle(acc, run_id, v, yes, approved)
        if vsix_due(jobs) and not vsix_done:
            # vsix() blocks for minutes: approve what has become pending since the read above first
            seen = {g["environment"]["name"]: g for g in later}
            seen.update({g["environment"]["name"]: g for g in settle(acc, run_id, v, yes, approved)})
            later = list(seen.values())
            vsix(acc, v, run_id, jobs)
            vsix_done = True
            progress.last = now()
        for gate in later:
            if vsix_done:
                approve(acc, run_id, gate, v, yes)
                approved.add(gate["environment"]["name"])
        sleep(POLL)


def verify(acc: forge.Account, v: str, openvsx: bool) -> bool:
    """Step 5: one line per place; True when every place has v. Open VSX is checked only when the run's
    openvsx job published (`openvsx`); the job is skipped while the variable OPENVSX is unset."""
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
    if openvsx:
        checks.append((f"Open VSX lists {v}", http(f"{OPENVSX_API}/{v}")[0] == 200))
    else:
        checks.append(("Open VSX: the openvsx job didn't run; not checked", None))
    deadline, site, told = now() + SITE_WAIT, False, False
    while True:
        status, text = http(SITE_URL)
        site = status == 200 and f'"softwareVersion": "{v}"' in text
        if site or now() >= deadline:
            break
        if not told:
            say(f"… gitpro.dev doesn't name {v} yet; looking every {SITE_POLL} s for up to "
                f"{SITE_WAIT // 60} minutes (GitHub Pages caches for 10)")
            told = True
        sleep(SITE_POLL)
    checks.append((f"gitpro.dev names {v}", site))
    for text, ok in checks:
        say({True: "✓ ", False: "✗ ", None: "– "}[ok] + text)
    return all(ok is not False for _, ok in checks)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="scripts/release.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--as", dest="login", required=True, metavar="LOGIN",
                   help="the GitHub account that starts the run and approves its gates")
    p.add_argument("--yes", action="store_true", help="answer yes to every question")
    p.add_argument("--run", type=int, metavar="ID", help="follow an existing Release run instead of starting one "
                   "(without it, an unfinished run on HEAD is offered first)")
    args = p.parse_args(argv)
    acc = forge.Account(forge.GITHUB, "gh", args.login)
    run_id = args.run
    try:
        if args.run is None:
            v, head = preflight(acc)
            prev = latest_run(acc, head)
            if prev and prev.get("status") != "completed":
                # never a second run while one for this commit is going or waits at a gate
                say(f"Run {prev['databaseId']} for {head[:7]} is {str(prev.get('status')).replace('_', ' ')}: "
                    f"{run_url(prev['databaseId'])}")
                if not (args.yes or ask("Follow it?")):
                    say("Nothing started.")
                    return 0
                run_id = prev["databaseId"]
            elif (done := published(acc, v)) is not None:
                if prev is None:
                    raise Stop(f"{done}, and no Release run was started on {head[:7]}")
                run_id = prev["databaseId"]
                say(f"{done}: picking up run {run_id}")
                if not pick_up(acc, run_id, args.yes):
                    return 0
            else:
                run_id = start(acc, v, head, args.yes)
                if run_id is None:
                    say("Nothing started.")
                    return 0
        else:
            v = release_version()
            check_login(acc)
            run_id = args.run
            info = gh_json(acc, ["run", "view", str(run_id), "-R", REPO, "--json", "workflowName,headBranch,headSha"],
                           "gh run view")
            if info.get("workflowName") != "Release" or info.get("headBranch") != "main":
                raise Stop(f"run {run_id} is {info.get('workflowName')} on {info.get('headBranch')}, "
                           "not Release on main")
            head = git_out("rev-parse", "HEAD")
            if info.get("headSha") != head:
                raise Stop(f"run {run_id} is for commit {str(info.get('headSha'))[:7]}, but HEAD is {head[:7]}; "
                           "check out the commit the run was started from")
            if not pick_up(acc, run_id, args.yes):
                return 0
        say(f"Release v{v}: {run_url(run_id)}")
        jobs = follow(acc, run_id, v, args.yes)
        say(f"✓ the run succeeded: {run_url(run_id)}")
        openvsx = any(j.get("name") == "openvsx" and j.get("conclusion") == "success" for j in jobs)
        return 0 if verify(acc, v, openvsx) else 1
    except Stop as e:
        print(clean(str(e)), file=sys.stderr, flush=True)
        return e.code
    except KeyboardInterrupt:
        again = f" (or with --run {run_id})" if run_id else ""
        print(f"\nStopped. Run this script again to pick the run up{again}.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
