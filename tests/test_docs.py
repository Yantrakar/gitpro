"""Spec 17 §5: README.md, SKILL.md, the plugin manifests and the version agree with the code as built."""
import argparse
import dataclasses
import importlib
import json
import os
import re
import shlex
from pathlib import Path

import pytest

import gitpro
from gitpro import cli, config

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
SKILL = (ROOT / "skills/gitpro/SKILL.md").read_text(encoding="utf-8")
VERSION = "0.2.0"
INSTALL = "git+https://github.com/sandeepshelke/gitpro.git"

# Spec 17 §5: the commands that write, each of which the skill's table must mark as writing.
WRITING = ["apply", "scan --fix", "pin", "unpin", "uninstall", "guard install", "guard uninstall",
           "agent install", "agent uninstall", "mcp install", "mcp uninstall", "devcontainer --write",
           "clone", "audit --rewrite", "keys --upload", "keys --generate", "init"]


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
                 "auth", "team", "scan", "agent-check", "guard check"):
        assert path in PATHS, path


# ---------------------------------------------------------------- README and SKILL name every command

def _mentions(text: str, path: str) -> bool:
    words = path.split()
    if len(words) == 1:
        return bool(re.search(rf"(`|gitpro ){re.escape(path)}(?![\w-])", text))
    return bool(re.search(r"(?<![\w-])" + r"\s+".join(map(re.escape, words)) + r"(?![\w-])", text))


@pytest.mark.parametrize("path", sorted(PATHS))
def test_readme_names_every_command(path):
    assert _mentions(README, path), f"README.md never mentions `{path}`"


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


@pytest.mark.parametrize("name", ["README.md", "SKILL.md"])
def test_documented_flags_exist(name):
    text = {"README.md": README, "SKILL.md": SKILL}[name]
    seen, bad = 0, []
    for path, opts in _invocations(text):
        seen += 1
        known = _options(PATHS[path])
        bad += [f"gitpro {path} {o}" for o in opts if o not in known]
    assert seen > 10, f"{name}: found too few gitpro command lines; the scanner is broken"
    assert not bad, f"{name} mentions options that do not exist: {sorted(set(bad))}"


def test_version_json_flag_documented_and_real():
    assert "gitpro --version --json" in README
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


def _readme_inputs_table() -> dict:
    section = README.split("**GitHub Actions.**", 1)[1].split("**GitLab CI.**", 1)[0]
    rows = re.findall(r"^\| `([\w-]+)` \| ([^|]+) \|", section, re.M)
    assert rows, "README has no Action inputs table"
    return {name: default.strip() for name, default in rows}


def test_readme_documents_every_action_input():
    table = _readme_inputs_table()
    assert set(table) == set(_action_inputs())
    for name, default in _action_inputs().items():
        if "${{" not in default:  # the range default is an expression; the README describes it
            assert table[name].strip("`").strip('"') == default, name


def test_readme_action_with_keys_are_inputs():
    blocks = [b for b in re.findall(r"```yaml\n(.*?)```", README, re.S) if "uses: sandeepshelke/gitpro@" in b]
    assert blocks, "README has no workflow that uses the Action"
    for block in blocks:
        after = block.split("uses: sandeepshelke/gitpro@", 1)[1]
        keys = re.findall(r"^\s+#?\s*([\w-]+):", after.split("with:", 1)[1], re.M)
        assert keys and set(keys) <= set(_action_inputs()), keys


def test_action_json_report_path():
    path = "$RUNNER_TEMP/gitpro-policy.json"
    assert f'tee "{path}"' in ACTION
    assert f"`{path}`" in README


def _ci_invocations(text: str):
    """(command path, [options]) for each `…/bin/gitpro <cmd>` line: the CI form, which _invocations skips."""
    for m in re.finditer(r"bin/gitpro\"?[ \t]+([a-z][\w-]*)([^\n|']*)", text):
        yield m.group(1), [o.split("=", 1)[0] for o in re.findall(r"(?<!\S)(--?[A-Za-z][\w-]*(?:=\S*)?)", m.group(2))]


@pytest.mark.parametrize("name", ["action.yml", "README.md"])
def test_ci_invocations_use_real_options(name):
    text = {"action.yml": ACTION, "README.md": README}[name]
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
    job = README.split("**GitLab CI.**", 1)[1].split("\n### ", 1)[0]
    assert "${CI_MERGE_REQUEST_TARGET_BRANCH_SHA:-$CI_MERGE_REQUEST_DIFF_BASE_SHA}..$CI_COMMIT_SHA" in job
    assert f'"{INSTALL}@<ref>"' in job
    assert 'GIT_DEPTH: "0"' in job
    assert "set -o pipefail" in job  # `| tee` must not hide the exit code
    assert "useradd" in job          # gitpro refuses to run as root


# ---------------------------------------------------------------- outputs the README shows

def test_team_header_matches_the_code():
    from gitpro import policy
    header = policy._render_team({"identities": [], "guard": None, "agents": None}, False)[1]
    columns = re.split(r"\s{2,}", header.strip())
    shown = [line for line in README.splitlines() if line.lstrip().startswith("identity ") and "verified" in line]
    assert shown, "README shows no gitpro team table"
    for line in shown:
        assert re.split(r"\s{2,}", line.strip()) == columns


def test_team_hint_is_a_real_command():
    for m in re.finditer(r"violates its policy → (gitpro .*)", README):
        words = shlex.split(m.group(1))
        assert words[1] in PATHS and "--policy" in _options(PATHS[words[1]])
        assert len(words) == 4 and words[2] == "--policy", m.group(1)  # --policy needs its FILE


def test_plain_check_is_documented_as_apply_check():
    row = next(line for line in README.splitlines() if line.startswith("| `check` |"))
    assert "`apply`" in row and "--repo" in row
    assert "Plain `gitpro check`" in README and "never looks at policies" in README
    assert "Plain `check`" in SKILL and "never" in SKILL.split("Plain `check`", 1)[1][:80]


def test_redaction_examples_match_the_code():
    from gitpro.resolve import redact_url
    rows = re.findall(r"^\| `([^`]+)` \| `([^`]+)` \|$", README.split("Remote URLs can carry secrets", 1)[1], re.M)
    assert len(rows) >= 3, "README lost its redaction examples"
    for raw, shown in rows:
        assert redact_url(raw) == shown, raw


# ---------------------------------------------------------------- the config reference

def _readme_toml() -> str:
    blocks = re.findall(r"```toml\n(.*?)```", README, re.S)
    full = [b for b in blocks if b.startswith("[default]")]
    assert full, "README.md has no ```toml block that starts with [default]"
    return max(full, key=len)


TABLES = {"default": config.Default, "identity": config.Identity, "scan": config.Scan, "guard": config.Guard,
          "clone": config.Clone, "forge": config.Forge, "agents": config.Agents, "policy": config.Policy}


def test_readme_config_block_has_every_key():
    data = config._load_toml(_readme_toml(), "README.md")
    assert set(data) == set(config.TOP_LEVEL)
    trailer_keys = {f.name for f in dataclasses.fields(config.Trailers)}
    for table, cls in TABLES.items():
        entries = data[table] if isinstance(data[table], list) else [data[table]]
        present = set().union(*(e.keys() for e in entries))
        for f in dataclasses.fields(cls):
            assert f.name in present, f"README config block: [{table}] lacks {f.name}"
            if f.name == "git":
                assert any(isinstance(e.get("git"), dict) and e["git"] for e in entries), table
            if f.name == "trailers":
                for e in entries:
                    if "trailers" in e:
                        assert set(e["trailers"]) <= trailer_keys
                assert trailer_keys <= set().union(*(e.get("trailers", {}).keys() for e in entries)), table


def test_readme_config_block_loads(home):
    path = home / ".config" / "gitpro.toml"
    path.parent.mkdir(parents=True)
    path.write_text(_readme_toml(), encoding="utf-8")
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


def test_manifests_parse():
    plugin = _json(".claude-plugin/plugin.json")
    assert plugin["name"] == "gitpro"
    assert "hooks" in _json("hooks/hooks.json")
    assert _json(".mcp.json")["mcpServers"]["gitpro"]["args"] == ["mcp"]
    market = _json(".claude-plugin/marketplace.json")
    assert [p["name"] for p in market["plugins"]] == ["gitpro"]
    # the plugin's hook and server need gitpro on the plain sh PATH; the description says so
    assert "PATH" in plugin["description"] and INSTALL in plugin["description"]


def test_versions_are_the_release():
    assert gitpro.__version__ == VERSION
    assert _json("vscode/package.json")["version"] == VERSION
    assert _json(".claude-plugin/plugin.json")["version"] == VERSION
    assert [p["version"] for p in _json(".claude-plugin/marketplace.json")["plugins"]] == [VERSION]
    lock = _json("vscode/package-lock.json")
    assert lock["version"] == VERSION and lock["packages"][""]["version"] == VERSION


def test_version_json_reports_the_release(home, capsys):
    assert cli.main(["--version", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == VERSION


def test_vsix_name_matches_version():
    assert f"gitpro-{VERSION}.vsix" in README
    assert not re.search(r"gitpro-\d+\.\d+\.\d+\.vsix", README.replace(f"gitpro-{VERSION}.vsix", ""))


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

@pytest.mark.parametrize("name", ["README.md", "skills/gitpro/SKILL.md", "vscode/README.md"])
def test_install_from_git_only(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert f"pipx install {INSTALL}" in text, name
    assert not re.search(r"pipx? install (--\S+ )*gitpro(\[\w+\])?(\s|$|`)", text), f"{name}: bare PyPI install"
