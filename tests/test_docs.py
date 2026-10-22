"""The landing page, full guide, skill and manifests agree with the code as built."""
import argparse
import dataclasses
import importlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import gitpro
from gitpro import cli, config

ROOT = Path(__file__).resolve().parent.parent
GUIDE = (ROOT / "docs/guide.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
SKILL = (ROOT / "skills/gitpro/SKILL.md").read_text(encoding="utf-8")
VERSION = gitpro.__version__
GIT_URL = "git+https://github.com/Yantrakar/gitpro.git"
INSTALL = "pipx install gitpro"

# Spec 17 §5: the commands that write, each of which the skill's table must mark as writing.
WRITING = ["apply", "scan --fix", "pin", "unpin", "uninstall", "guard install", "guard uninstall",
           "agent install", "agent uninstall", "mcp install", "mcp uninstall", "devcontainer --write",
           "clone", "audit --rewrite", "keys --upload", "keys --generate", "init", "setup",
           "change approve", "change apply", "change recover"]


# ---------------------------------------------------------------- the parser

def _subparsers(parser: argparse.ArgumentParser) -> dict:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _paths() -> dict:
    """Every command path ("guard install", ...) → the chain of parsers from the top-level command down."""
    top, _ = cli.build_parser()
    out = {}

    def walk(parser, path, chain):
        for name, sub in _subparsers(parser).items():
            out[" ".join(path + [name])] = chain + [sub]
            walk(sub, path + [name], chain + [sub])
    walk(top, [], [])
    return out


PATHS = _paths()


def _options(chain) -> set:
    """The option strings of a command, its parent commands included (`gitpro guard -y install` works)."""
    return {o for p in chain for a in p._actions for o in a.option_strings}


def test_parser_has_every_package():
    # a sanity check that the walk works and every package registered its commands
    for path in ("why", "guard install", "agent status", "forge similar", "mcp install", "policy lint",
                 "devcontainer", "prompt", "completion", "doctor", "lost", "audit", "clone", "env", "discover",
                 "auth", "team", "scan", "agent-check", "guard check", "setup", "report"):
        assert path in PATHS, path


# ---------------------------------------------------------------- guide and SKILL name every command

def _mentions(text: str, path: str) -> bool:
    words = path.split()
    if len(words) == 1:
        return bool(re.search(rf"(`|gitpro ){re.escape(path)}(?![\w-])", text))
    return bool(re.search(r"(?<![\w-])" + r"\s+".join(map(re.escape, words)) + r"(?![\w-])", text))


@pytest.mark.parametrize("path", sorted(PATHS))
def test_guide_names_every_command(path):
    assert _mentions(GUIDE, path), f"docs/guide.md never mentions `{path}`"


def _skill_table() -> list:
    """The rows of SKILL.md's command table: (list of `command` items in the first cell, the other cells)."""
    section = SKILL.split("\n## Commands", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append((re.findall(r"`([^`]+)`", cells[0]), cells[1:]))
    assert rows, "SKILL.md has no command table"
    return rows


def test_skill_table_has_the_spec_columns():
    section = SKILL.split("\n## Commands", 1)[1]
    assert "| command | writes? | needs yes? | network |" in section


@pytest.mark.parametrize("path", sorted(PATHS))
def test_skill_table_names_every_command(path):
    items = [i for cmds, _ in _skill_table() for i in cmds]
    assert any(i == path or i.startswith(path + " ") for i in items), f"SKILL.md's command table lacks `{path}`"


@pytest.mark.parametrize("command", WRITING)
def test_skill_marks_writing_commands(command):
    rows = [cells for cmds, cells in _skill_table() if command in cmds]
    assert rows, f"SKILL.md's command table has no row for `{command}`"
    for cells in rows:
        assert not cells[0].lower().startswith("no"), f"`{command}` is in a row whose writes? cell says no"


def test_skill_table_flags_exist():
    for cmds, _ in _skill_table():
        for item in cmds:
            words = item.split()
            names = [w for w in words if not w.startswith("-")]
            path = next((" ".join(names[:n]) for n in (2, 1) if " ".join(names[:n]) in PATHS), None)
            assert path, f"SKILL.md table: `{item}` is not a command"
            # a word after the command path must be a positional (`devcontainer status`), not a typo'd subcommand
            assert len(names) == len(path.split()) or not _subparsers(PATHS[path][-1]), item
            flags = {w for w in words if w.startswith("-")}
            assert flags <= _options(PATHS[path]), f"SKILL.md table: `{item}` names an unknown option"


# ---------------------------------------------------------------- every `gitpro <cmd> --flag` is real

STOP = re.compile(r"[|;&)`\"'#<>]")


def _code_spans(text: str) -> list:
    """Inline code and the lines of fenced code blocks."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            out.append(line)
        else:
            out.extend(re.findall(r"`([^`]+)`", line))
    return out


def _invocations(text: str):
    """(command path, [options]) for every `gitpro …` command line in the code of `text`."""
    for span in _code_spans(text):
        for m in re.finditer(r"(?<![\w/.@:-])gitpro\s+([^\n]*)", span):
            rest = STOP.split(m.group(1), 1)[0]
            try:
                words = shlex.split(rest)
            except ValueError:
                words = rest.split()
            if not words or words[0] not in {p.split()[0] for p in PATHS}:
                continue  # prose such as "gitpro does not edit it", or `gitpro --version`
            path = words[0]
            if len(words) > 1 and f"{path} {words[1]}" in PATHS:
                path = f"{path} {words[1]}"
            opts = []
            for w in words[1:]:
                if w == "--":
                    break  # the rest goes to another program (clone -- GIT_CLONE_ARGS)
                if w.startswith("-") and w != "-":
                    opts.append(w.split("=", 1)[0])
            yield path, opts


@pytest.mark.parametrize("name", ["docs/guide.md", "SKILL.md", "README.md"])
def test_documented_flags_exist(name):
    text = {"docs/guide.md": GUIDE, "SKILL.md": SKILL, "README.md": README}[name]
    seen, bad = 0, []
    for path, opts in _invocations(text):
        seen += 1
        known = _options(PATHS[path])
        bad += [f"gitpro {path} {o}" for o in opts if o not in known]
    assert seen > (3 if name == "README.md" else 10), f"{name}: found too few gitpro command lines"
    assert not bad, f"{name} mentions options that do not exist: {sorted(set(bad))}"


def test_version_json_flag_documented_and_real():
    assert "gitpro --version --json" in GUIDE
    top, _ = cli.build_parser()
    assert {"--version", "--json"} <= {o for a in top._actions for o in a.option_strings}


# ---------------------------------------------------------------- CI: the Action and the GitLab job

ACTION = (ROOT / "action.yml").read_text(encoding="utf-8")


def _action_inputs() -> dict:
    """action.yml's inputs → their default (the text after `default:`), read without a YAML library."""
    block = ACTION.split("\ninputs:\n", 1)[1].split("\nruns:\n", 1)[0]
    out, name = {}, None
    for line in block.splitlines():
        m = re.match(r"^  ([\w-]+):\s*$", line)
        if m:
            name = out.setdefault(m.group(1), None) or m.group(1)
            continue
        m = re.match(r"^    default:\s*(.*)$", line)
        if m and name:
            out[name] = m.group(1).strip().strip('"')
    return out


def test_action_inputs_parse():
    assert set(_action_inputs()) == {"policy", "range", "comment", "working-directory"}


def _guide_inputs_table() -> dict:
    section = GUIDE.split("**GitHub Actions.**", 1)[1].split("**GitLab CI.**", 1)[0]
    rows = re.findall(r"^\| `([\w-]+)` \| ([^|]+) \|", section, re.M)
    assert rows, "guide has no Action inputs table"
    return {name: default.strip() for name, default in rows}


def test_guide_documents_every_action_input():
    table = _guide_inputs_table()
    assert set(table) == set(_action_inputs())
    for name, default in _action_inputs().items():
        if "${{" not in default:  # the range default is an expression; the guide describes it
            assert table[name].strip("`").strip('"') == default, name


def test_guide_action_with_keys_are_inputs():
    blocks = [b for b in re.findall(r"```yaml\n(.*?)```", GUIDE, re.S) if "uses: Yantrakar/gitpro@" in b]
    assert blocks, "guide has no workflow that uses the Action"
    for block in blocks:
        after = block.split("uses: Yantrakar/gitpro@", 1)[1]
        keys = re.findall(r"^\s+#?\s*([\w-]+):", after.split("with:", 1)[1], re.M)
        assert keys and set(keys) <= set(_action_inputs()), keys


def test_action_json_report_path():
    path = "$RUNNER_TEMP/gitpro-policy.json"
    assert f'tee "{path}"' in ACTION
    assert f"`{path}`" in GUIDE


def _ci_invocations(text: str):
    """(command path, [options]) for each `…/bin/gitpro <cmd>` line: the CI form, which _invocations skips."""
    for m in re.finditer(r"bin/gitpro\"?[ \t]+([a-z][\w-]*)([^\n|']*)", text):
        yield m.group(1), [o.split("=", 1)[0] for o in re.findall(r"(?<!\S)(--?[A-Za-z][\w-]*(?:=\S*)?)", m.group(2))]


@pytest.mark.parametrize("name", ["action.yml", "docs/guide.md"])
def test_ci_invocations_use_real_options(name):
    text = {"action.yml": ACTION, "docs/guide.md": GUIDE}[name]
    found = list(_ci_invocations(text))
    assert any(path == "check" for path, _ in found), f"{name}: no CI gitpro check line found"
    for path, opts in found:
        assert path in PATHS, f"{name}: gitpro {path} is not a command"
        assert opts and set(opts) <= _options(PATHS[path]), f"{name}: gitpro {path} {opts}"
        if path == "check":
            assert {"--policy", "--range", "--json"} <= set(opts), f"{name}: the CI check needs --policy, --range, --json"


def test_action_refuses_shallow_and_empty_range():
    assert "is-shallow-repository" in ACTION and "commit range is empty" in ACTION
    assert "${{ inputs." not in "".join(re.findall(r"run: \|\n(.*?)(?=\n    - |\Z)", ACTION, re.S))


def test_gitlab_job_uses_the_target_branch_base():
    job = GUIDE.split("**GitLab CI.**", 1)[1].split("\n### ", 1)[0]
    assert "${CI_MERGE_REQUEST_TARGET_BRANCH_SHA:-$CI_MERGE_REQUEST_DIFF_BASE_SHA}..$CI_COMMIT_SHA" in job
    assert f'"{GIT_URL}@<ref>"' in job
    assert 'GIT_DEPTH: "0"' in job
    assert "set -o pipefail" in job  # `| tee` must not hide the exit code
    assert "useradd" in job          # gitpro refuses to run as root


# ---------------------------------------------------------------- outputs the guide shows

def test_team_header_matches_the_code():
    from gitpro import policy
    header = policy._render_team({"identities": [], "guard": None, "agents": None}, False)[1]
    columns = re.split(r"\s{2,}", header.strip())
    shown = [line for line in GUIDE.splitlines() if line.lstrip().startswith("identity ") and "verified" in line]
    assert shown, "guide shows no gitpro team table"
    for line in shown:
        assert re.split(r"\s{2,}", line.strip()) == columns


def test_team_hint_is_a_real_command():
    for m in re.finditer(r"violates its policy → (gitpro .*)", GUIDE):
        words = shlex.split(m.group(1))
        assert words[1] in PATHS and "--policy" in _options(PATHS[words[1]])
        assert len(words) == 4 and words[2] == "--policy", m.group(1)  # --policy needs its FILE


def test_plain_check_is_documented_as_apply_check():
    row = next(line for line in GUIDE.splitlines() if line.startswith("| `check` |"))
    assert "`apply`" in row and "--repo" in row
    assert "Plain `gitpro check`" in GUIDE and "never looks at policies" in GUIDE
    assert "Plain `check`" in SKILL and "never" in SKILL.split("Plain `check`", 1)[1][:80]


def test_redaction_examples_match_the_code():
    from gitpro.resolve import redact_url
    rows = re.findall(r"^\| `([^`]+)` \| `([^`]+)` \|$", GUIDE.split("Remote URLs can carry secrets", 1)[1], re.M)
    assert len(rows) >= 3, "guide lost its redaction examples"
    for raw, shown in rows:
        assert redact_url(raw) == shown, raw


# ---------------------------------------------------------------- the config reference

def _guide_toml() -> str:
    blocks = re.findall(r"```toml\n(.*?)```", GUIDE, re.S)
    full = [b for b in blocks if b.startswith("[default]")]
    assert full, "docs/guide.md has no ```toml block that starts with [default]"
    return max(full, key=len)


TABLES = {"default": config.Default, "identity": config.Identity, "scan": config.Scan, "guard": config.Guard,
          "clone": config.Clone, "forge": config.Forge, "agents": config.Agents, "policy": config.Policy}


def test_guide_config_block_has_every_key():
    data = config._load_toml(_guide_toml(), "docs/guide.md")
    assert set(data) == set(config.TOP_LEVEL)
    trailer_keys = {f.name for f in dataclasses.fields(config.Trailers)}
    for table, cls in TABLES.items():
        entries = data[table] if isinstance(data[table], list) else [data[table]]
        present = set().union(*(e.keys() for e in entries))
        for f in dataclasses.fields(cls):
            assert f.name in present, f"guide config block: [{table}] lacks {f.name}"
            if f.name == "git":
                assert any(isinstance(e.get("git"), dict) and e["git"] for e in entries), table
            if f.name == "trailers":
                for e in entries:
                    if "trailers" in e:
                        assert set(e["trailers"]) <= trailer_keys
                assert trailer_keys <= set().union(*(e.get("trailers", {}).keys() for e in entries)), table


def test_guide_config_block_loads(home):
    path = home / ".config" / "gitpro.toml"
    path.parent.mkdir(parents=True)
    path.write_text(_guide_toml(), encoding="utf-8")
    os.chmod(path, 0o600)
    cfg = config.load_all(path, "test")
    assert [i.id for i in cfg.identities] == ["work-gitlab", "work-github"]
    assert cfg.identities[1].trailers.co_authored_by == "forbid"


def _concrete(text: str) -> str:
    """Replace the example's placeholder domains, which load_all rejects on purpose."""
    text = re.sub(r"([\w.-]+)\.example\b", r"\1.test", text)
    return re.sub(r"\bexample\.(com|net|org)\b", r"sample.\1", text)


def test_example_toml_loads(home):
    text = _concrete((ROOT / "src/gitpro/example.toml").read_text(encoding="utf-8"))
    path = home / ".config" / "gitpro.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    cfg = config.load_all(path, "test")
    assert cfg.default.email and cfg.identities


def test_example_toml_placeholders_are_refused(home):
    path = home / ".config" / "gitpro.toml"
    path.parent.mkdir(parents=True)
    path.write_text((ROOT / "src/gitpro/example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(config.ConfigError):
        config.load_all(path, "test")


# ---------------------------------------------------------------- manifests and version

def _json(rel: str):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


ISSUES = "https://github.com/Yantrakar/gitpro/issues"


def test_users_are_told_where_to_report_problems():
    top, _ = cli.build_parser()
    assert ISSUES in top.format_help()
    assert ISSUES in (ROOT / "README.md").read_text(encoding="utf-8") and ISSUES in GUIDE
    assert f'Issues = "{ISSUES}"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    vsc = _json("vscode/package.json")
    assert vsc["bugs"]["url"] == ISSUES and vsc["qna"] == ISSUES


def test_manifests_parse():
    plugin = _json(".claude-plugin/plugin.json")
    assert plugin["name"] == "gitpro"
    assert "hooks" in _json("hooks/hooks.json")
    assert _json(".mcp.json")["mcpServers"]["gitpro"]["args"] == ["mcp"]
    market = _json(".claude-plugin/marketplace.json")
    assert [p["name"] for p in market["plugins"]] == ["gitpro"]
    # the plugin's hook and server need gitpro on the plain sh PATH; the description says so
    assert "PATH" in plugin["description"] and INSTALL in plugin["description"]


def _versions_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import versions
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    return versions


# Files that must not carry the release version: it is written in src/gitpro/VERSION only. CHANGELOG, the
# site's release history list and docs that name a past release on purpose are not in this list; the site's
# version fields are placeholders (test_site_structured_data_is_the_release).
NO_VERSION_COPIES = ["src/gitpro/__init__.py", "vscode/package.json", "vscode/package-lock.json", "server.json",
                     ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"]


def test_version_is_written_in_one_place():
    versions = _versions_module()
    assert versions.version(ROOT) == VERSION
    for rel in NO_VERSION_COPIES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert VERSION not in text, f"{rel} holds the release version {VERSION}; only src/gitpro/VERSION may"
    # VS Code loads no extension without a version (the integration test runs before --stamp): a placeholder
    assert _json("vscode/package.json")["version"] == "0.0.0" and "version" not in _json("server.json")
    assert not any("version" in p for p in _json("server.json")["packages"])
    assert "version" not in _json("vscode/package-lock.json")
    assert "version" not in _json("vscode/package-lock.json")["packages"][""]
    guide_numbers = set(re.findall(r"gitpro-(\d+\.\d+\.\d+)(?:\.vsix|-py3)", GUIDE))
    assert not guide_numbers, f"docs/guide.md names a build by number: {guide_numbers}"
    assert not re.search(r'^  "version": "\d', GUIDE, re.M), "the sample --version output has a number"


def _version_lines():
    return (ROOT / "src/gitpro/VERSION").read_text(encoding="utf-8").splitlines()


def test_dunder_version_is_the_version_file():
    assert gitpro.__version__ == _version_lines()[0].split()[0]


def test_version_file_lines_are_version_and_comment():
    lines = _version_lines()
    assert lines and all(re.fullmatch(r"\d+\.\d+\.\d+(\s.*)?", ln) for ln in lines)


def test_version_file_is_strictly_descending():
    nums = [tuple(map(int, ln.split()[0].split("."))) for ln in _version_lines()]
    assert nums == sorted(set(nums), reverse=True)


def test_version_file_versions_are_changelog_headings():
    headings = set(re.findall(r"^## (\d+\.\d+\.\d+)\b", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M))
    assert {ln.split()[0] for ln in _version_lines()} <= headings


def test_stamp_writes_the_version_and_nothing_else(tmp_path):
    versions = _versions_module()
    for rel in ("src/gitpro/VERSION", "vscode/package.json", "vscode/package-lock.json", "server.json"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    (tmp_path / "src/gitpro/VERSION").write_text("7.6.5\n", encoding="utf-8")
    before = {rel: (tmp_path / rel).read_text(encoding="utf-8") for rel in ("vscode/package.json", "server.json")}
    placeholder = '  "version": "0.0.0",\n'
    assert versions.stamp(tmp_path) == ["vscode/package.json", "server.json"]
    for rel, old in before.items():
        new = (tmp_path / rel).read_text(encoding="utf-8")
        assert new != old and new.endswith("\n")
        # taking the version lines out gives the committed bytes back: nothing else moved
        assert re.sub(r'^ *"version": "7\.6\.5",\n', "", new, flags=re.M) == old.replace(placeholder, "")
    assert json.loads((tmp_path / "vscode/package.json").read_text(encoding="utf-8"))["version"] == "7.6.5"
    server = json.loads((tmp_path / "server.json").read_text(encoding="utf-8"))
    assert server["version"] == "7.6.5" and [p["version"] for p in server["packages"]] == ["7.6.5"]
    again = {rel: (tmp_path / rel).read_bytes() for rel in before}
    versions.stamp(tmp_path)
    assert {rel: (tmp_path / rel).read_bytes() for rel in before} == again      # stamping twice changes nothing


def test_server_json_names_the_pypi_package():
    # the registry accepts a PyPI package only when its README carries the server name
    server = _json("server.json")
    assert [(p["registryType"], p["identifier"]) for p in server["packages"]] == [("pypi", "gitpro")]
    assert f"<!-- mcp-name: {server['name']} -->" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_version_json_reports_the_release(home, capsys):
    assert cli.main(["--version", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == VERSION


# What `gitpro --version --json` listed in the last release (0.3.4). Clients (the VS Code extension, agents, scripts)
# decide what to call from this list, so a capability that has shipped is not taken away without editing this set in
# the same change. After a release, replace the set with what that release printed.
RELEASED_CAPABILITIES = frozenset({
    'agent-check', 'agent.install', 'agent.status', 'approval-policy.v1', 'audit', 'audit.rewrite', 'auth',
    'change-apply.v1', 'change-approve.v1', 'change-propose.v1', 'change-recover.v1', 'change-status.v1',
    'check.json', 'check.policy', 'clone', 'completion', 'devcontainer', 'devcontainer.write', 'discover',
    'doctor', 'dry-run-defaults.v1', 'env', 'forge', 'forge.similar', 'guard', 'init.discover', 'keys.health',
    'keys.upload', 'lost', 'lost.online', 'mcp', 'mcp.install', 'operation-reconcile.v1',
    'operation-status.v1', 'policy', 'preflight.v1', 'prompt', 'report', 'run.v1', 'scan.roots', 'setup',
    'team', 'trailers', 'which.json', 'why', 'why.config',
})


def test_capabilities_only_grow_and_the_changelog_names_the_new_ones(tmp_path):
    # A fresh interpreter, as a user runs it: other tests add capabilities to this process and never take them away.
    out = subprocess.run([sys.executable, "-m", "gitpro", "--version", "--json"], capture_output=True, text=True,
                         check=True, timeout=60, env=dict(os.environ, HOME=str(tmp_path)))
    caps = set(json.loads(out.stdout)["capabilities"])
    assert RELEASED_CAPABILITIES <= caps, f"capabilities taken away: {sorted(RELEASED_CAPABILITIES - caps)}"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unnamed = sorted(c for c in caps - RELEASED_CAPABILITIES if f"`{c}`" not in changelog)
    assert not unnamed, f"CHANGELOG.md does not name the new capabilities {unnamed}"


def test_vsix_name_is_a_placeholder():
    assert "gitpro-<version>.vsix" in GUIDE
    assert not re.search(r"gitpro-\d+\.\d+\.\d+\.vsix", GUIDE)


# ---------------------------------------------------------------- the skill

def test_skill_front_matter():
    m = re.match(r"---\n(.*?)\n---\n", SKILL, re.S)
    assert m, "SKILL.md has no front matter"
    fields = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    assert fields["name"].strip() == "gitpro"
    assert fields["description"].strip()


def test_skill_lists_the_mcp_tools():
    from gitpro import mcp
    section = SKILL.split("\n## MCP server", 1)[1].split("\n## ", 1)[0]
    for name in mcp.TOOLS:
        assert f"`{name}`" in section, name


# ---------------------------------------------------------------- the release has every package

@pytest.mark.parametrize("name", cli.EXTENSIONS)
def test_every_extension_imports(name):
    importlib.import_module(name)  # a ModuleNotFoundError here means a package is missing from the release


# ---------------------------------------------------------------- install line

@pytest.mark.parametrize("name", ["README.md", "docs/guide.md", "skills/gitpro/SKILL.md", "vscode/README.md",
                                  "site/index.html"])
def test_install_from_pypi(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert INSTALL in text, name
    assert "git+https://" not in text.replace(f'"{GIT_URL}@<ref>"', ""), f"{name}: install from PyPI, not git"


def test_readme_is_a_short_onboarding_page():
    assert len(README.splitlines()) <= 100
    assert len(README.split()) <= 650
    assert "gitpro setup" in README and "docs/guide.md" in README
    assert "pipx install gitpro" in README and "gitpro scan" in README and "gitpro why" in README
    assert "setup --write" not in README
    assert "```toml" not in README and "```yaml" not in README


@pytest.mark.parametrize("name", ["README.md", "docs/guide.md", "vscode/README.md"])
def test_documentation_relative_links_and_anchors(name):
    source = ROOT / name
    for link in re.findall(r"\]\(([^)]+)\)", source.read_text(encoding="utf-8")):
        if "://" in link or link.startswith("mailto:"):
            continue
        rel, _, anchor = link.partition("#")
        target = source.parent / rel if rel else source
        assert target.exists(), f"{name}: broken link {link}"
        if anchor:
            headings = re.findall(r"^#{1,6} (.+)$", target.read_text(encoding="utf-8"), re.M)
            anchors = {re.sub(r"[^\w -]", "", h.lower()).replace(" ", "-") for h in headings}
            assert anchor in anchors, f"{name}: missing anchor {link}"


# ---------------------------------------------------------------- gitpro.dev for search engines and agents

SITE = (ROOT / "site/index.html").read_text(encoding="utf-8")
LLMS = (ROOT / "site/llms.txt").read_text(encoding="utf-8")


def test_site_title_and_description_fit_search_results():
    title = re.search(r"<title>(.+?)</title>", SITE)[1]
    description = re.search(r'<meta name="description" content="([^"]+)">', SITE)[1]
    assert title.startswith("gitpro") and len(title) <= 60, title
    assert 140 <= len(description) <= 155, len(description)
    assert f'<meta name="twitter:title" content="{title}">' in SITE


def test_site_structured_data_is_the_release():
    data = json.loads(re.search(r'<script type="application/ld\+json">(.+?)</script>', SITE, re.S)[1])
    app = next(n for n in data["@graph"] if n["@type"] == "SoftwareApplication")
    assert app["softwareVersion"] == "0.0.0"      # the committed copy holds a placeholder; deploy stamps it
    stamped = _site_meta().with_version(SITE, VERSION)
    assert f'"softwareVersion": "{VERSION}"' in stamped and f"· v{VERSION}</p>" in stamped
    assert not {"aggregateRating", "review"} & set(app), "gitpro has no ratings or reviews"
    assert "FAQPage" not in {n["@type"] for n in data["@graph"]}


def test_site_shipped_list_follows_the_changelog():
    # spec 20, section 17: a release adds its line, dated as its changelog section
    shipped = SITE[SITE.index('<p class="label-h">Shipped</p>'):]
    shipped = shipped[:shipped.index("</ol>")]
    lines = re.findall(r'<span class="tag">([\d.]+)(?: · (\d{4}-\d{2}-\d{2}))?</span>', shipped)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    dated = dict(re.findall(r"^## (\d+\.\d+\.\d+) \((\d{4}-\d{2}-\d{2})\)$", changelog, re.M))
    newest = re.search(r"^## (\d+\.\d+\.\d+)", changelog, re.M)[1]
    assert lines and lines[0][0] == newest, (lines[:1], newest)
    assert [(v, d) for v, d in lines if d and v in dated and dated[v] != d] == []
    assert all(re.fullmatch(r"\d+\.\d+(\.\d+)?", v) for v, _ in lines)


def test_site_organisation_is_yantrakar_coms():
    data = json.loads(re.search(r'<script type="application/ld\+json">(.+?)</script>', SITE, re.S)[1])
    org = next(n for n in data["@graph"] if n["@type"] == "Organization")
    assert org["@id"] == "https://yantrakar.com/#org"      # the id yantrakar.com gives itself
    assert set(re.findall(r'"@id": "(https://yantrakar\.com/[^"]*)"', SITE)) == {org["@id"]}

def test_llms_txt_lists_the_default_mcp_tools_and_real_docs():
    from gitpro import mcp
    assert LLMS.startswith("# gitpro\n\n> ")
    listed = set(re.search(r"Tools: (.+?)\. Register", LLMS)[1].replace("`", "").split(", "))
    assert listed == {name for name, tool in mcp.TOOLS.items() if not tool.execution}
    for rel in re.findall(r"raw\.githubusercontent\.com/Yantrakar/gitpro/main/([^)]+)\)", LLMS):
        assert (ROOT / rel).exists(), rel
    assert INSTALL in LLMS and 'href="/llms.txt"' in SITE



def test_site_loads_nothing_from_other_hosts():
    pages = SITE + (ROOT / "site/404.html").read_text(encoding="utf-8")
    css = (ROOT / "site/styles.css").read_text(encoding="utf-8")
    fetched = [re.search(r'href="([^"]+)"', link)[1] for link in re.findall(r"<link\b[^>]*>", pages)
               if re.search(r'rel="(?:stylesheet|preload|preconnect|icon|apple-touch-icon)"', link)]
    fetched += re.findall(r'<(?:script|img)\b[^>]*\bsrc="([^"]+)"', pages) + re.findall(r'url\("?([^")]+)', css)
    assert fetched and not [u for u in fetched if "//" in u], fetched
    for url in fetched:
        assert (ROOT / "site" / url.lstrip("/")).is_file(), url


def _site_meta():
    import importlib.util
    spec = importlib.util.spec_from_file_location("site_meta", ROOT / "design/site_meta.py")
    meta = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(meta)
    return meta


def test_site_meta_writes_version_and_git_dates():
    meta = _site_meta()
    assert meta.release() == VERSION
    assert meta.with_version(SITE, "0.0.0") == SITE      # the committed placeholder is what deploy replaces
    bumped = meta.with_version(SITE, "9.8.7")
    assert '"softwareVersion": "9.8.7"' in bumped and "· v9.8.7</p>" in bumped
    sitemap = (ROOT / "site/sitemap.xml").read_text(encoding="utf-8")
    dated = meta.with_dates(sitemap, lambda path: f"date of {path.relative_to(ROOT).as_posix()}")
    assert dated.count("<lastmod>") == sitemap.count("<lastmod>") == 1
    assert "<lastmod>date of site/index.html</lastmod>" in dated
