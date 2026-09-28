"""`gitpro doctor`: run every check gitpro has and list what to fix, worst first in the summary line.

SECTIONS is a list of (name, fn, needs): fn(ctx) returns a section dict, needs names an optional module
(None for built-ins). An optional module may define doctor_section(ctx) -> dict to own its row; otherwise
the adapter here calls its status function. A section that raises becomes `⚠ <name>: check failed (...)`;
a missing optional module `· <name>: not installed (optional)`. Reports only: never fixes, never prints
secret values. Module level imports only the standard library (cli builds the parser without plan/view).
"""
from __future__ import annotations

import argparse
import importlib
import os
import platform
import re
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

STATUSES = ("ok", "warning", "error", "skipped", "missing")
SYMBOL = {"ok": "✓", "warning": "⚠", "error": "✗", "skipped": "·", "missing": "·"}
GIT_HINT = "install git 2.36+ (Ubuntu: ppa:git-core/ppa, macOS: brew install git)"
ENV_EXACT = ("GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
             "GH_TOKEN", "GH_ENTERPRISE_TOKEN",
             "GITLAB_TOKEN")
ENV_PREFIXES = ("GIT_AUTHOR_", "GIT_COMMITTER_")
TIMEOUT = 10
NAME_W, SUMMARY_W = 9, 55


class Skip(Exception):
    """Raised by a section that cannot run (no config, not in a repo, offline): status "skipped"."""


@dataclass
class Ctx:
    home: Path
    config_path: Optional[Path] = None   # -c
    dir: Optional[Path] = None
    online: bool = False
    verbose: bool = False
    cfg: Any = None
    cfg_error: Optional[str] = None
    versions: Dict[str, str] = field(default_factory=dict)

    def need_cfg(self):
        if self.cfg is None:
            raise Skip("needs a config")
        return self.cfg

    def env(self):
        from . import plan
        return plan.Env(self.home)

    def tilde(self, path: object) -> str:
        p, h = str(path), str(self.home)
        return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def section(status: str = "ok", summary: str = "", problems=(), details=(), quiet: bool = False) -> dict:
    """A section result; status "ok" is raised to the worst problem level."""
    probs = [_problem(p) for p in problems]
    if status == "ok" and probs:
        status = "error" if any(p["level"] == "error" for p in probs) else "warning"
    return {"status": status, "summary": summary, "problems": probs, "details": list(details), "quiet": quiet}


def problem(message: str, fix: str = "", level: str = "warning") -> dict:
    return {"message": message, "fix": fix, "level": level}


def _problem(p: object, level: str = "warning") -> dict:
    """Normalise a problem: a dict, a string, or an object with message/fix (resolve.Problem)."""
    if isinstance(p, dict):
        return problem(str(p.get("message", "")), str(p.get("fix") or ""), p.get("level") or level)
    if isinstance(p, str):
        return problem(p, "", level)
    fix = getattr(p, "fix", "") or ""
    if getattr(p, "fix_argv", None):
        from . import resolve
        fix = resolve._shown_fix(p)
    return problem(str(getattr(p, "message", p)), fix, getattr(p, "level", None) or level)


def _run(argv: List[str], **kw) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, **kw)
    except (OSError, subprocess.SubprocessError):
        return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------- built-in sections

def check_versions(ctx: Ctx) -> dict:
    from . import __version__
    ctx.versions.update(gitpro=__version__, python=platform.python_version(), os=platform.system().lower())
    probs = []
    r = _run(["git", "--version"])
    m = re.search(r"(\d+)\.(\d+)(?:\.\d+)?\S*", r.stdout) if r and r.returncode == 0 else None
    if m is None:
        ctx.versions["git"] = ""
        probs.append(problem("git not found", GIT_HINT, "error"))
    else:
        ctx.versions["git"] = m[0]
        if (int(m[1]), int(m[2])) < (2, 36):
            probs.append(problem(f"git {m[1]}.{m[2]} is too old; includeIf hasconfig needs 2.36+", GIT_HINT, "error"))
    r = _run(["ssh", "-V"])
    ssh = (r.stderr or r.stdout).strip().split(",")[0] if r else ""
    if not ssh:
        probs.append(problem("ssh not found", "install OpenSSH"))
    summary = f"git {ctx.versions['git'] or '?'} · {ssh or 'no ssh'} · python {ctx.versions['python']}"
    return section("ok", summary, probs, quiet=True)


def check_config(ctx: Ctx) -> dict:
    from . import config
    try:
        path, how = config.find(ctx.config_path, ctx.home)
        ctx.cfg = config.load_all(path, how)
    except config.ConfigError as e:
        msg, sep, hint = str(e).partition("; create one with: ")
        ctx.cfg_error = str(e)
        fix = hint.strip() if sep else "fix the config file (gitpro plan shows the error)"
        return section("error", msg, [problem(msg, fix, "error")])
    n = len(ctx.cfg.identities)
    where = f"{ctx.tilde(ctx.cfg.path)} ({ctx.cfg.how})" if ctx.cfg.how else ctx.tilde(ctx.cfg.path)
    return section("ok", f"{where}  {n} identit{'y' if n == 1 else 'ies'}")


def check_applied(ctx: Ctx) -> dict:
    from . import files, plan
    changes = files.diff(*plan.build(ctx.need_cfg(), ctx.env()))
    if not changes:
        return section("ok", "up to date")
    n = len(changes)
    details = [f"{c.action} {ctx.tilde(c.path)}" for c in changes]
    return section("ok", f"{n} pending change{'s' if n != 1 else ''}", [problem(
        f"{n} pending change{'s' if n != 1 else ''}", "gitpro apply", "error")], details)


def _block(text: str):
    from . import render
    return render._BLOCK_RE.search(text)


def _meaningful(text: str) -> bool:
    """Any line that is not blank or a comment?"""
    return any(ln.strip() and not ln.lstrip().startswith(("#", ";")) for ln in text.splitlines())


def check_files(ctx: Ctx) -> dict:
    from . import plan, render
    cfg = ctx.need_cfg()
    env = ctx.env()
    probs, parts = [], []
    gc, sc = ctx.home / ".gitconfig", ctx.home / ".ssh/config"
    m = _block(_read(gc))
    if m is None:
        probs.append(problem("~/.gitconfig has no gitpro block", "gitpro apply", "error"))
    elif _meaningful(_read(gc)[m.end():]):
        probs.append(problem("~/.gitconfig: gitpro block not last (settings after it win)", "gitpro apply", "error"))
    else:
        parts.append("~/.gitconfig block last ✓")
    m = _block(_read(sc))
    if m is None:
        probs.append(problem("~/.ssh/config has no gitpro block", "gitpro apply", "error"))
    elif _meaningful(_read(sc)[:m.start()]):
        probs.append(problem("~/.ssh/config: gitpro block not first (Host blocks above it win)", "gitpro apply",
                             "error"))
    else:
        parts.append("~/.ssh/config block first ✓")
    generated = [ctx.home / plan.MANAGED_GITCONFIG, ctx.home / ".ssh" / plan.MANAGED_SSH]
    generated += [env.expand(i.file) for i in cfg.identities if i.file]
    for p in generated:
        if not render.is_generated(_read(p)):
            probs.append(problem(f"{ctx.tilde(p)} missing", "gitpro apply", "error"))
    return section("ok", "  ".join(parts) if not probs else (probs[0]["message"]), probs,
                   [f"generated: {ctx.tilde(p)}" for p in generated])


_STRAY_RE = re.compile(r"also sets these directly")


def check_lint(ctx: Ctx) -> dict:
    from . import lint
    cfg = ctx.need_cfg()
    probs = []
    for msg in lint.warnings(cfg.default, list(cfg.identities), ctx.env()):
        if _STRAY_RE.search(msg):
            for line in msg.splitlines()[1:]:
                key = line.split()[0] if line.split() else ""
                if key and key != "core.sshcommand":      # the env section reports core.sshCommand
                    probs.append(problem(f"~/.gitconfig also sets {key} directly", "remove it from ~/.gitconfig"))
            continue
        text, _sep, fix = msg.partition(" -- ")
        fix = fix[len("run: "):] if fix.startswith("run: ") else fix
        probs.append(problem(text.replace(str(ctx.home), "~"), fix.replace(str(ctx.home), "~")))
    return section("ok", probs[0]["message"] if probs else "no warnings", probs)


def _hosts(cfg) -> List[Tuple[str, Optional[int]]]:
    out = [(h, None) for h in cfg.default.ssh_hosts]
    out += [(i.host, i.ssh_port) for i in cfg.identities if i.host]
    return list(dict.fromkeys(out))


def check_ssh(ctx: Ctx) -> dict:
    from . import plan
    cfg = ctx.need_cfg()
    ssh_dir = ctx.home / ".ssh"
    probs, parts, details = [], [], []
    if ssh_dir.is_dir():
        mode = ssh_dir.stat().st_mode & 0o777
        if mode & 0o077:
            probs.append(problem(f"~/.ssh is {mode:04o}; ssh wants 0700", "chmod 700 ~/.ssh"))
        else:
            parts.append(f"~/.ssh {mode:04o} ✓")
    managed = ssh_dir / plan.MANAGED_SSH
    if managed.is_file():
        mode = managed.stat().st_mode & 0o777
        if mode & 0o077:
            probs.append(problem(f"~/.ssh/{plan.MANAGED_SSH} is {mode:04o}", f"chmod 600 ~/.ssh/{plan.MANAGED_SSH}"))
        else:
            parts.append(f"{plan.MANAGED_SSH} {mode:04o} ✓")
        text = _read(managed)
        if "Host " in text and "IdentitiesOnly yes" not in text:
            probs.append(problem(f"~/.ssh/{plan.MANAGED_SSH} lacks IdentitiesOnly", "gitpro apply"))
    known = ssh_dir / "known_hosts"
    if shutil.which("ssh-keygen"):
        for host, port in _hosts(cfg):
            name = f"[{host}]:{port}" if port and port != 22 else host
            r = _run(["ssh-keygen", "-F", name, "-f", str(known)])
            if r is None or r.returncode != 0:
                probs.append(problem(f"{name} not in ~/.ssh/known_hosts",
                                     "connect once and compare the host key with the provider's published one"))
            else:
                details.append(f"known host: {name}")
    for p in (ctx.home / ".gitconfig", ssh_dir / "config", ctx.home / plan.MANAGED_GITCONFIG, managed):
        old = re.search(r"^# (?:BEGIN|END|Generated by) (git-identities|git_identities\.py)\b", _read(p), re.M)
        if old:
            probs.append(problem(f"{ctx.tilde(p)} has an old {old[1]} block", "gitpro apply"))
    return section("ok", "  ".join(parts) or "no ~/.ssh", probs, details)


def check_repo(ctx: Ctx) -> dict:
    from . import resolve
    cfg = ctx.need_cfg()
    where = Path(ctx.dir) if ctx.dir else Path.cwd()
    top = resolve.is_repo(where)
    if top is None:
        raise Skip("not in a git repository")
    rep = resolve.compare(where, cfg)
    exp = rep.expected
    who = exp.id or ("default" if exp.email else "no identity")
    return section("ok", f"{ctx.tilde(top)} → {who}", [_problem(p, "error") for p in rep.problems])


def _sshcommand_outside_block(text: str) -> bool:
    from . import render
    sect = ""
    for line in render.without_block(text).splitlines():
        s = line.strip()
        m = re.match(r"^\[\s*([A-Za-z0-9.-]+)", s)
        if m:
            sect = m[1].lower()
        elif sect == "core" and re.match(r"(?i)^sshcommand\s*=", s):
            return True
    return False


def check_env(ctx: Ctx) -> dict:
    from . import prompt
    names = sorted(k for k in os.environ if k.startswith(ENV_PREFIXES) and not k.endswith("_DATE"))
    names += [k for k in ENV_EXACT if k in os.environ]
    probs = [problem(f"{k} is set in this shell", f"unset {k}") for k in dict.fromkeys(names)]
    if _sshcommand_outside_block(_read(ctx.home / ".gitconfig")):
        probs.append(problem("~/.gitconfig sets core.sshCommand outside the gitpro block (it overrides the per-"
                             "identity key)", "remove it from ~/.gitconfig"))
    cache = prompt.cache_path(ctx.home)
    details = [f"prompt cache: {ctx.tilde(cache)} ({'present' if cache.exists() else 'absent'}; safe to delete)"]
    return section("ok", probs[0]["message"] if probs else "no identity overrides in the environment", probs,
                   details)


# ---------------------------------------------------------------- optional sections (adapters)

def _adapt(obj: object, level: str = "warning") -> dict:
    """A section from whatever a status function returns: a section-like dict, a list of problems, a str."""
    if isinstance(obj, dict):
        probs = [_problem(p, level) for p in obj.get("problems") or ()]
        if obj.get("error"):
            probs.append(problem(str(obj["error"]), str(obj.get("fix") or ""), level))
        status = obj.get("status") if obj.get("status") in STATUSES else "ok"
        if obj.get("ok") is False and not probs and status == "ok":
            status = level
        summary = obj.get("summary")
        if not summary:
            simple = [f"{k} {v}" for k, v in obj.items()
                      if k not in ("problems", "details", "status", "ok", "error", "fix", "summary")
                      and isinstance(v, (str, int)) and not isinstance(v, bool)]
            summary = " · ".join(simple)
        return section(status, str(summary or (probs[0]["message"] if probs else "ok")), probs,
                       [str(d) for d in obj.get("details") or ()])
    if isinstance(obj, (list, tuple)):
        probs = [_problem(p, level) for p in obj]
        return section("ok", probs[0]["message"] if probs else "no problems", probs)
    return section("ok", str(obj))


def _needs_cli(ctx: Ctx) -> Optional[dict]:
    if not (shutil.which("gh") or shutil.which("glab")):
        return problem("needs gh or glab", "install gh (GitHub) or glab (GitLab), then re-run with --online")
    return None


def _accepts(fn: Callable, name: str) -> bool:
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def check_keys(ctx: Ctx, health) -> dict:
    cfg = ctx.need_cfg()
    missing = _needs_cli(ctx) if ctx.online else None
    # doctor announces the online checks once (run); health must not repeat it mid-report
    kw = {"announce": False} if _accepts(health.check_keys, "announce") else {}
    sec = _adapt(health.check_keys(cfg, online=bool(ctx.online and not missing), **kw))
    if missing:
        sec = section(sec["status"], sec["summary"], [*sec["problems"], missing], sec["details"])
    return sec


def check_guard(ctx: Ctx, guard) -> dict:
    st = guard.status()
    if not isinstance(st, dict) or "installed" not in st:
        return _adapt(st)
    probs = [problem(str(st["error"]), str(st.get("fix") or "gitpro guard install"))] if st.get("error") else []
    installed = st["installed"]     # 06: "global" | "none" (older stubs: bool)
    if installed not in (None, False, "", "none"):
        where = f" ({installed})" if isinstance(installed, str) and installed != "global" else ""
        summary = f"installed{where}, {st.get('on_mismatch') or 'block'}"
    else:
        summary = "not installed (optional: gitpro guard install)"
        cfg = ctx.cfg
        if cfg is not None and getattr(getattr(cfg, "guard", None), "hooks", False) and not probs:
            probs.append(problem("[guard] hooks = true but the hooks are not installed", "gitpro guard install"))
    return section("ok", summary, probs)


def _agents_section(data: object) -> dict:
    """10's status_data() dict -> a section; anything else goes through _adapt."""
    if not isinstance(data, dict) or not isinstance(data.get("agents"), dict):
        return _adapt(data)
    claude = data["agents"].get("claude") or {}
    hook = "claude hook installed" if claude.get("user") or claude.get("project") else \
        "claude hook not installed (optional: gitpro agent install)"
    parts = [hook] + (["plugin enabled"] if claude.get("plugin") else [])
    probs = []
    for w in data.get("warnings") or ():
        msg, _sep, fix = str(w).partition(" → ")
        probs.append(problem(msg, fix))
    details = [f"[agents] hook = {data['mode']}"] if data.get("mode") else []
    details += [f"{a}: {i.get('user') or 'not installed'}" for a, i in data["agents"].items() if isinstance(i, dict)]
    return section("ok", " · ".join(parts), probs, details)


def check_agents(ctx: Ctx, agents) -> dict:
    # agents.status(args) is 10's CLI command; status_data(home, explicit_config) is the data
    data_fn = getattr(agents, "status_data", None)
    if data_fn is None:
        return section("ok", "status unavailable", [problem("gitpro.agents has no status_data()")])
    sec = _agents_section(data_fn(ctx.home, ctx.config_path))
    mcp = _optional("gitpro.mcp")
    mcp_hook = getattr(mcp, "doctor_section", None) if mcp is not None else None
    mcp_data = getattr(mcp, "status_data", None) if mcp is not None else None
    if mcp_hook or mcp_data:
        extra = _adapt(mcp_hook(ctx) if mcp_hook else mcp_data(ctx.home))
        sec = section("ok" if sec["status"] == "ok" else sec["status"], f"{sec['summary']} · {extra['summary']}",
                      [*sec["problems"], *extra["problems"]], sec["details"] + extra["details"])
    return sec


def check_policy(ctx: Ctx, policy) -> dict:
    probs = [_problem(p, "error") for p in policy.check_machine(ctx.need_cfg()) or ()]
    return section("ok", probs[0]["message"] if probs else "machine meets the team policy", probs)


def check_forge(ctx: Ctx, health) -> dict:
    if not ctx.online:
        sec = section("skipped", "offline (--online checks gh/glab sign-in)")
        sec["quiet"] = True
        return sec
    cfg = ctx.need_cfg()
    missing = _needs_cli(ctx)
    if missing:
        return section("ok", "needs gh or glab", [missing])
    return _adapt(health.auth(cfg, list(cfg.identities), TIMEOUT))


def _optional(name: str):
    """The module, or None when it is not installed (only a missing module of that exact name counts)."""
    if name in sys.modules and sys.modules[name] is None:
        return None
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name == name:
            return None
        raise


def _with(adapter: Callable, name: str) -> Callable[[Ctx], dict]:
    """fn(ctx) for an optional module: its doctor_section(ctx) when it has one, else adapter(ctx, mod)."""
    def fn(ctx: Ctx) -> dict:
        mod = sys.modules.get(name)
        hook = getattr(mod, "doctor_section", None)
        return _adapt(hook(ctx)) if hook else adapter(ctx, mod)
    fn.__name__ = f"check_{name.rsplit('.', 1)[-1]}"
    return fn


SECTIONS: List[Tuple[str, Callable[[Ctx], dict], Optional[str]]] = [
    ("versions", check_versions, None),
    ("config", check_config, None),
    ("applied", check_applied, None),
    ("files", check_files, None),
    ("lint", check_lint, None),
    ("keys", _with(check_keys, "gitpro.health"), "gitpro.health"),
    ("ssh", check_ssh, None),
    ("guard", _with(check_guard, "gitpro.guard"), "gitpro.guard"),
    ("agents", _with(check_agents, "gitpro.agents"), "gitpro.agents"),
    ("repo", check_repo, None),
    ("policy", _with(check_policy, "gitpro.policy"), "gitpro.policy"),
    ("env", check_env, None),
    ("forge", _with(check_forge, "gitpro.health"), "gitpro.health"),
]


# ---------------------------------------------------------------- running and printing

ONLINE_ONLY = ("forge",)   # rows that only mean something with --online


def _one(ctx: Ctx, name: str, fn: Callable[[Ctx], dict], needs: Optional[str]) -> dict:
    try:
        if needs and _optional(needs) is None:
            quiet = name in ONLINE_ONLY and not ctx.online
            return {"name": name, **section("missing", "not installed (optional)", quiet=quiet)}
        sec = fn(ctx)
    except Skip as e:
        sec = section("skipped", f"skipped ({e})")
    except Exception as e:  # a broken check never stops the report
        last = traceback.format_exception_only(type(e), e)[-1].strip()
        sec = section("warning", f"check failed ({e})", [problem(f"check failed ({e})")], [last])
        sec["failed"] = True
    if sec.get("status") not in STATUSES:
        sec = _adapt(sec)
    return {"name": name, **sec}


def announce(ctx: Ctx, out=None) -> None:
    """forge.announce once, before any online check; out is stderr under --json (never the JSON)."""
    import contextlib

    from . import config, forge
    try:
        path, how = config.find(ctx.config_path, ctx.home)
        accs = forge.accounts(config.load_all(path, how))
    except Exception:   # the config section reports it
        accs = []
    with contextlib.redirect_stdout(out or sys.stdout):
        forge.announce("whether your keys are registered and you are signed in", accs,
                       "your account names and public key fingerprints")


def run(cfg_path: Optional[Path], dir: Optional[Path], online: bool, *, verbose: bool = False,
        home: Optional[Path] = None, announce_to=None) -> dict:
    """The whole report as the --json object (ok, command, versions, sections, summary). With online,
    prints forge's announcement line first, to announce_to (default stdout)."""
    ctx = Ctx(Path(home) if home else Path.home(), Path(cfg_path) if cfg_path else None,
              Path(dir) if dir else None, online, verbose)
    if online:
        announce(ctx, announce_to)
    sections = [_one(ctx, name, fn, needs) for name, fn, needs in SECTIONS]
    errors = sum(s["status"] == "error" for s in sections)
    warnings = sum(s["status"] == "warning" for s in sections)
    versions = {k: ctx.versions.get(k, "") for k in ("gitpro", "python", "git", "os")}
    if not versions["gitpro"]:
        from . import __version__
        versions.update(gitpro=__version__, python=platform.python_version(), os=platform.system().lower())
    return {"ok": errors == 0, "command": "doctor", "versions": versions, "sections": sections,
            "summary": {"problems": errors, "warnings": warnings}}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _fixtext(p: dict) -> str:
    return f" → {p['fix']}" if p.get("fix") else ""


def render_text(rep: dict, verbose: bool = False) -> str:
    v = rep["versions"]
    lines = [f"gitpro {v['gitpro']}  python {v['python']}  git {v['git'] or '?'}  {v['os']}"]
    pad = " " * (NAME_W + 1)
    for s in rep["sections"]:
        sym = SYMBOL[s["status"]]
        if s.get("quiet") and s["status"] in ("ok", "skipped", "missing") and not verbose:
            continue
        if s.get("failed") or s["status"] == "missing":
            lines.append(f"{sym} {s['name']}: {s['summary']}")
        else:
            head = f"{s['name']:<{NAME_W}} {s['summary']:<{SUMMARY_W}} {sym}"
            probs = list(s["problems"])
            if probs:
                first = probs.pop(0)
                if first["message"] != s["summary"]:
                    head += f" {first['message']}"
                head += _fixtext(first)
            lines.append(head)
            lines += [f"{pad}{SYMBOL.get(p['level'], '⚠')} {p['message']}{_fixtext(p)}" for p in probs]
        if verbose:
            lines += [f"{pad}· {d}" for d in s["details"]]
    n, w = rep["summary"]["problems"], rep["summary"]["warnings"]
    if n or w:
        tail = " → fix the ✗ lines first" if n else ""
        lines.append(f"{_plural(n, 'problem')}, {_plural(w, 'warning')}{tail}")
    else:
        lines.append("✓ all checks passed")
    return "\n".join(lines) + "\n"


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import jsonout
    as_json = jsonout.wants_json(args)
    rep = run(args.config, args.dir, args.online, verbose=args.verbose, home=args.home,
              announce_to=sys.stderr if as_json else sys.stdout)
    if as_json:
        out = dict(rep)
        out["sections"] = [{k: v for k, v in s.items() if k not in ("quiet", "failed")} for s in rep["sections"]]
        for s in out["sections"]:
            s["problems"] = [{"message": p["message"], "fix": p["fix"], "level": p["level"]} for p in s["problems"]]
        jsonout.emit("doctor", out)
    else:
        sys.stdout.write(render_text(rep, args.verbose))
    return 0 if rep["ok"] else 1


def register(sub, common, confirm) -> None:
    from .args import capability
    sp = sub.add_parser("doctor", parents=[common], help="run every check and list what to fix")
    sp.add_argument("dir", nargs="?", type=Path, metavar="DIR", help="repo to check (default: the current directory)")
    sp.add_argument("--online", action="store_true", help="also ask gh/glab whether keys and sign-ins are valid")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.add_argument("-v", "--verbose", action="store_true", help="detail lines under each section")
    sp.set_defaults(func=cmd_doctor)
    capability("doctor")
