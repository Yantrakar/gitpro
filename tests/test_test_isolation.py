"""The shared fixtures must isolate tests even when scratch directories are inside a checkout."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from conftest import TIMEOUT, repo_with_remote


def test_home_fixture_does_not_discover_enclosing_checkout(tmp_path):
    repo = repo_with_remote(tmp_path, "enclosing", None)
    suite = repo / "tests"
    suite.mkdir()
    shutil.copyfile(Path(__file__).with_name("conftest.py"), suite / "conftest.py")
    (suite / "test_probe.py").write_text(
        "import subprocess\n"
        "from conftest import run\n\n"
        "def test_project_install(home):\n"
        "    assert run('agent', 'install', '--scope', 'project', '-y') == 2\n\n"
        "def test_plain_directory(home, tmp_path):\n"
        "    plain = tmp_path / 'plain'\n"
        "    plain.mkdir()\n"
        "    result = subprocess.run(['git', '-C', str(plain), 'rev-parse', '--show-toplevel'],\n"
        "                            capture_output=True, timeout=10)\n"
        "    assert result.returncode != 0\n"
    )
    exclude = repo / ".git/info/exclude"
    before = exclude.read_bytes()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    env.pop("GIT_CEILING_DIRECTORIES", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--basetemp", str(repo / "scratch"), str(suite)],
        cwd=suite, env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (repo / ".claude").exists()
    assert exclude.read_bytes() == before
