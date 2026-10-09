"""Opt-in packaged-CLI GitHub qualification; retain private evidence for inspection and cleanup."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True, help="new private directory; retained, never overwritten")
    parser.add_argument("--gh-config-dir", type=Path, required=True, help="operator-selected isolated gh login")
    parser.add_argument("--repo", required=True, help="authorized owner/repository")
    parser.add_argument("--branch", required=True, help="new gitpro-e2e-* branch; never an existing branch")
    parser.add_argument("--account", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("-y", "--yes", action="store_true", help="authorize local fixture writes and live push/draft PR")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("repo must be owner/repository")
    if not re.fullmatch(r"gitpro-e2e-[A-Za-z0-9-]+", args.branch):
        parser.error("use a new gitpro-e2e-* branch")
    if not args.yes:
        print(json.dumps({"dry_run": True, "repo": args.repo, "branch": args.branch,
                          "effects": ["private clone/configuration", "candidate commit", "push", "draft PR"],
                          "cleanup": "After inspecting CI, close this PR and delete only this branch."}, indent=2))
        return
    os.umask(0o077)
    root = args.work_dir.resolve()
    root.mkdir(mode=0o700)
    home, package, repo = root / "home", root / "package", root / "repo"
    home.mkdir(mode=0o700)
    with zipfile.ZipFile(args.wheel.resolve()) as archive:
        archive.extractall(package)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in (
        "GITPRO_CONFIG", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
        "GH_HOST", "GH_REPO", "GH_DEBUG", "XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    env.update(HOME=str(home), GH_CONFIG_DIR=str(args.gh_config_dir.resolve()), PYTHONPATH=str(package),
               GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")

    def call(argv: list[str], *, cwd: Path = root, text: str = "") -> str:
        result = subprocess.run(argv, cwd=cwd, env=env, input=text, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {result.stdout}\n{result.stderr}")
        return result.stdout

    def cli(*argv: str, text: str = "") -> dict:
        return json.loads(call([sys.executable, "-m", "gitpro", *argv], text=text))

    def save(name: str, data: dict) -> None:
        (root / (name + ".json")).write_text(json.dumps(data, indent=2) + "\n")

    user = json.loads(call(["gh", "api", "user"]))
    if user["login"].casefold() != args.account.casefold():
        raise RuntimeError("The selected gh profile authenticates as another account.")
    refs = json.loads(call(["gh", "api", f"repos/{args.repo}/git/matching-refs/heads/{args.branch}"]))
    if refs:
        raise RuntimeError("The verification branch already exists; inspect it rather than retrying writes.")
    source = args.source.resolve()
    call(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(repo)])
    call(["git", "switch", "--quiet", "-c", args.branch], cwd=repo)
    call(["git", "remote", "set-url", "origin", f"https://github.com/{args.repo}.git"], cwd=repo)
    diff = call(["git", "diff", "--binary", "HEAD"], cwd=source)
    if diff:
        call(["git", "apply", "--binary", "-"], cwd=repo, text=diff)
    for relative in call(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=source).split("\0"):
        if relative:
            original, target = source / relative, repo / relative
            if original.is_symlink():
                raise RuntimeError("Inspect untracked symlinks before packaging the candidate.")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
    toml = "[default]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in {
        "name": args.name, "email": args.email, "gh_account": args.account}.items())
    proposal = cli("change", "propose", "--request", "-", text=json.dumps({
        "version": 1, "kind": "configure", "toml": toml}))
    save("configuration-proposal", proposal)
    policy = root / "operator.json"
    policy.write_text(json.dumps({"version": 1, "rules": [{
        "id": "fixture-only", "kinds": ["configure"], "config_paths": [proposal["request"]["config_path"]],
        "repo_roots": [], "write_paths": [c["path"] for c in proposal["changes"]], "write_roots": []}]}))
    configured = cli("change", "apply", "--plan-id", proposal["plan_id"],
                     "--proposal", str(root / "configuration-proposal.json"), "--approval-policy", str(policy), "-y")
    assert configured["status"] == "applied", configured
    why = cli("why", str(repo), "--json")
    assert why["ok"] and not why["problems"], why
    call(["git", "add", "-A"], cwd=repo)
    summary = {"repo": args.repo, "branch": args.branch, "work_dir": str(root), "operations": {}}
    save("summary", summary)

    def operation(kind: str, **fields: object) -> dict:
        request = {"version": 1, "operation": kind, "repo": str(repo), **fields}
        key = args.branch + ":" + kind
        save(kind + "-request", request)
        online = ["--online"] if kind != "commit" else []
        before = call(["git", "rev-parse", "HEAD"], cwd=repo)
        preview = cli("run", "--request", "-", *online, text=json.dumps(request))
        assert preview["command"] == "preflight" and preview["decision"] == "ready", preview
        assert call(["git", "rev-parse", "HEAD"], cwd=repo) == before
        save(kind + "-preflight", preview)
        argv = ["run", "--request", "-", "--plan-token", preview["plan_token"],
                "--idempotency-key", key, "-y", *online]
        executed = cli(*argv, text=json.dumps(request))
        save(kind + "-result", executed)
        replay = cli(*argv, text=json.dumps(request))
        assert replay["replayed"] and replay["outcome"] == executed["outcome"], replay
        observed = cli("operation-reconcile", "--idempotency-key", key, *online)
        assert observed["status"] == "observed" and observed["retry_safe"] is False, observed
        save(kind + "-observation", observed)
        summary["operations"][kind] = executed["outcome"]
        save("summary", summary)
        print(f"{kind}: executed once, replayed, observed", flush=True)
        return executed["outcome"]

    operation("commit", message="feat: harden agent execution and previews\n\n"
              "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>")
    operation("push", remote="origin", branch=args.branch)
    pr = operation("pr-create", remote="origin", base="main", head=args.branch,
                   title="Temporary gitpro end-to-end qualification", draft=True,
                   body="## Summary of Changes\nTemporary candidate for packaged CLI qualification.\n\n"
                        "## Impacted Systems\nCLI, MCP, VS Code, approval policy and operation journal.\n\n"
                        "## Verification Checklist\n- Commit/push/PR execution, replay and read-only reconciliation\n"
                        "- Repository CI matrix\n\nDo not merge. Close and delete this temporary branch after inspection.")
    summary["head"] = call(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    summary["pr_url"] = pr["pr_url"]
    save("summary", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
