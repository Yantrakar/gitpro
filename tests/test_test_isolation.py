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
        "\n"
        "def test_injected_environment(home, tmp_path):\n"
        "    import os\n"
        "    assert 'GIT_CONFIG_COUNT' not in os.environ\n"
        "    assert 'GIT_AUTHOR_EMAIL' not in os.environ\n"
        "    assert 'GIT_DIR' not in os.environ\n"
        "    bare = tmp_path / 'bare.git'\n"
        "    subprocess.run(['git', 'init', '-q', '--bare', str(bare)], check=True)\n"
        "    result = subprocess.run(['git', '-C', str(bare), 'rev-parse', '--is-bare-repository'],\n"
        "                            capture_output=True, text=True, timeout=10)\n"
        "    assert result.returncode == 0 and result.stdout.strip() == 'true'\n"
    )
    exclude = repo / ".git/info/exclude"
    before = exclude.read_bytes()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    env.pop("GIT_CEILING_DIRECTORIES", None)
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.bareRepository",
               GIT_CONFIG_VALUE_0="explicit", GIT_AUTHOR_EMAIL="ambient@wrong.test",
               GIT_DIR=str(repo / ".git"))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--basetemp", str(repo / "scratch"), str(suite)],
        cwd=suite, env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (repo / ".claude").exists()
    assert exclude.read_bytes() == before
