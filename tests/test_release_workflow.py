"""release.yml, pages.yml and scripts/versions.py (spec 20, sections 4, 15 and 16). releasepro runs the
release; these tests read the files and touch no network."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


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
    first = (ROOT / "src/gitpro/VERSION").read_text(encoding="utf-8").split("\n", 1)[0].split()[0]
    assert r.returncode == 0 and r.stdout.strip() == first


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


def test_the_site_goes_last_and_mcp_registry_needs_only_pypi():
    """Spec 20 section 15: gitpro.dev is deployed once every channel has the version; a skipped channel
    doesn't hold it back, a failed one does. The MCP Registry checks the package on PyPI, so it waits for
    pypi alone."""
    jobs = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8").split("\njobs:\n", 1)[1]
    block = {m.group(1): m.group(2) for m in re.finditer(r"^  ([a-z-]+):\n((?:    .*\n|\n)*)", jobs, re.M)}
    assert "    needs: [pypi, marketplace, openvsx, mcp-registry]\n" in block["site"]
    assert ("    if: ${{ !cancelled() && !failure() && needs.pypi.result == 'success' "
            "&& needs.mcp-registry.result == 'success' }}\n") in block["site"]
    assert "    needs: [check, pypi]\n" in block["mcp-registry"]


def test_the_release_notes_come_from_the_changelog():
    """Spec 20 section 16: check stops a version with no CHANGELOG.md section; the GitHub release gets the
    notes as its body, also on a re-run, and ANNOUNCEMENT.md as an asset; GitHub's generated notes are
    not used. releasepro.toml lists ANNOUNCEMENT.md among the assets that verify checks."""
    jobs = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8").split("\njobs:\n", 1)[1]
    block = {m.group(1): m.group(2) for m in re.finditer(r"^  ([a-z-]+):\n((?:    .*\n|\n)*)", jobs, re.M)}
    assert 'releasepro notes --check "$v"' in block["check"]
    assert 'releasepro notes "$VERSION" --write notes' in block["build"]
    assert "--generate-notes" not in jobs
    assert block["github"].count("--notes-file notes/RELEASE_NOTES.md") == 2
    assert block["github"].count("notes/ANNOUNCEMENT.md") == 2
    head = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8").split("\njobs:\n", 1)[0]
    assert re.search(r"^env:\n  RELEASEPRO: releasepro==\d+\.\d+\.\d+ ", head, re.M)
    assert re.findall(r"pip install \S*releasepro\S*", jobs) == []
    assert jobs.count('pip install "$RELEASEPRO"') == 2
    toml = (ROOT / "releasepro.toml").read_text(encoding="utf-8")
    assert '"ANNOUNCEMENT.md"' in toml and 'changelog = "CHANGELOG.md"' in toml
