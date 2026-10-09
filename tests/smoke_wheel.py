"""Exercise the distributable outside the checkout, using a disposable HOME and operator policy."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def smoke(wheel: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="gitpro-wheel-") as temporary:
        root = Path(temporary).resolve()
        package, home = root / "package", root / "home"
        home.mkdir(mode=0o700)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(package)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and
               k not in ("GITPRO_CONFIG", "XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        env.update(HOME=str(home), PYTHONPATH=str(package), GIT_CONFIG_NOSYSTEM="1")
        imported = subprocess.run([sys.executable, "-c", "import gitpro; print(gitpro.__file__)"],
                                  cwd=root, env=env, capture_output=True, text=True, check=True, timeout=30)
        assert Path(imported.stdout.strip()) == package / "gitpro/__init__.py", \
            "Smoke test imported the checkout instead of the wheel"

        def cli(*args: str, text: str = "", code: int = 0) -> dict:
            result = subprocess.run([sys.executable, "-m", "gitpro", *args], cwd=root, env=env,
                                    input=text, capture_output=True, text=True, timeout=30)
            assert result.returncode == code, (args, result.stdout, result.stderr)
            return json.loads(result.stdout)

        version = cli("--version", "--json")
        assert {"preflight.v1", "run.v1", "approval-policy.v1", "operation-reconcile.v1"} <= set(version["capabilities"])
        schema = cli("change", "propose", "--schema")
        assert "approval_policy" in schema
        proposal = cli("change", "propose", "--request", "-", text=json.dumps({
            "version": 1, "kind": "configure",
            "toml": '[default]\nname = "Fixture"\nemail = "fixture@local.test"\n'}))
        assert proposal["decision"] == "needs_approval"
        assert cli("change", "apply", "--plan-id", proposal["plan_id"], "-y", code=2)["decision"] == "needs_approval"
        assert not (home / ".gitconfig").exists()
        source, policy = root / "proposal.json", root / "operator.json"
        source.write_text(json.dumps(proposal))
        policy.write_text(json.dumps({"version": 1, "rules": [{
            "id": "ci-smoke", "kinds": ["configure", "undo"],
            "config_paths": [proposal["request"]["config_path"]], "repo_roots": [],
            "write_paths": [c["path"] for c in proposal["changes"]], "write_roots": []}]}))
        source.chmod(0o600)
        policy.chmod(0o600)
        args = ("change", "apply", "--plan-id", proposal["plan_id"], "--proposal", str(source),
                "--approval-policy", str(policy), "-y")
        applied = cli(*args)
        assert applied["status"] == "applied" and applied["authority"]["kind"] == "policy"
        assert cli(*args)["replayed"]
        assert cli("change", "status", "--plan-id", proposal["plan_id"])["authority"] == applied["authority"]
        assert cli("change", "propose", "--request", "-", text=json.dumps({
            "version": 1, "kind": "regenerate"}))["status"] == "no_changes"
        undo = cli("change", "propose", "--request", "-", text=json.dumps({
            "version": 1, "kind": "undo", "change_id": proposal["plan_id"]}))
        source.write_text(json.dumps(undo))
        assert cli("change", "apply", "--plan-id", undo["plan_id"], "--proposal", str(source),
                   "--approval-policy", str(policy), "-y")["status"] == "applied"
        assert not (home / ".gitconfig").exists()
        message = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        default = {t["name"] for t in cli("mcp", text=message)["result"]["tools"]}
        enabled = {t["name"] for t in cli("mcp", "--allow-execution", text=message)["result"]["tools"]}
        assert {"propose_change", "change_status", "reconcile_operation"} <= default
        assert not {"run_operation", "apply_change", "recover_change"} & default
        assert {"run_operation", "apply_change", "recover_change"} <= enabled
        assert not any("approve" in name for name in enabled)
        print(f"Wheel {version['version']}: isolated policy/apply/replay/undo and MCP gates passed.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python tests/smoke_wheel.py PATH.whl")
    smoke(Path(sys.argv[1]).resolve())
