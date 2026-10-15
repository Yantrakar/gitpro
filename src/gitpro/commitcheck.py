"""The guard's judgement of one commit, for `gitpro why --json` and the MCP tools: is the committer and
author the repo's identity, does the repo have a problem the guard blocks on, and does the message follow
the Co-Authored-By rules."""
from __future__ import annotations

import inspect
import shlex
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

GUARD_CODES = ("ambiguous", "override", "pin_stale", "no_identity", "env_override")   # 10 §2 step 5; agents.TRIGGERS
CAUSE_CODES = ("url_form", "override", "pin_stale", "global_override", "not_applied", "env_override")


def _problem(code: str, message: str, fix: Optional[str]) -> dict:
    return {"code": code, "message": message, "fix": fix}


def _who(rep) -> str:
    e = rep.expected
    return f"identity {e.id or '[default]'} <{e.email}>"


def _where(rep) -> str:
    m = rep.expected.match
    return f"{m.remote} ({m.url})" if m.remote and m.url else str(rep.repo)


def identity_problems(rep, cfg, committer_email: Optional[str] = None,
                      author_email: Optional[str] = None) -> List[dict]:
    """The guard's committer/author judgement (06 §2): committer must be the expected email; the author
    only counts when it is one of the user's own emails."""
    from . import resolve
    expected = rep.expected.email
    committer = committer_email or rep.effective.email.value or ""
    author = author_email or committer
    if expected is None:
        return []
    out = []
    bad_committer, bad_author = resolve.identity_conflicts(expected, resolve.own_emails(cfg), author, committer)
    if bad_committer:
        cause = next((p for p in rep.problems if p.code in CAUSE_CODES), None)
        msg = (f"this commit would be committed as {committer or '(no email)'} but {_where(rep)} belongs to "
               f"{_who(rep)}" + (f". cause: {cause.message}" if cause else ""))
        fix = resolve._shown_fix(cause) if cause else f"gitpro why {shlex.quote(str(rep.repo))}"
        out.append(_problem("committer", msg, fix))
    a = author.casefold()
    if bad_author and a != committer.casefold():   # same as the committer: reported above
        who = f"{rep.expected.name} <{expected}>"
        out.append(_problem("author", f"the author would be {author}, your own other email; {_where(rep)} belongs "
                                      f"to {_who(rep)}", f"git commit --author={shlex.quote(who)} ..."))
    return out


def trailer_problems(repo: Path, rep, cfg, message: str, warnings: List[str]) -> List[dict]:
    """Co-Authored-By policy for the message (08's trailers module); a warning when it cannot be checked."""
    try:
        from . import trailers
    except ImportError:
        warnings.append("message not checked: trailer rules need gitpro's audit package")
        return []
    try:
        r = subprocess.run(["git", "-C", str(repo), "interpret-trailers", "--parse"], input=message,
                           capture_output=True, text=True, timeout=10)
        pairs = [tuple(s.strip() for s in ln.split(":", 1)) for ln in r.stdout.splitlines() if ":" in ln]
        # 08 §11: trailers.policy_for(identity, default), trailers.check(trailers, policy) -> list[str]
        policy = call_spec(trailers.policy_for, [("identity", rep.expected.match.identity),
                                                  ("default", cfg.default)])
        if hasattr(trailers, "violations"):   # 08: (message, fix) pairs, so a require violation gets its own fix
            found = [(str(m), str(f)) for m, f in trailers.violations(pairs, policy)]
        else:
            found = [(str(m), "remove the trailer from the message")
                     for m in call_spec(trailers.check, [("trailers", pairs), ("policy", policy)]) or []]
    except Exception as e:  # noqa: BLE001 - another package's checker must not break the tool
        warnings.append(f"message not checked: {type(e).__name__}")
        return []
    return [_problem("trailer", m, f) for m, f in found]


def call_spec(fn: Callable, values: List[Tuple[str, object]]):
    """fn called with values, which follow the spec's positional order (name, value). Tolerant of how the
    other package declares them: keyword-only parameters get their value by name, and positional ones by
    name when every name matches, else by position."""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return fn(*[v for _, v in values])
    named = dict(values)
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    kwargs = {p.name: named[p.name] for p in params if p.kind == p.KEYWORD_ONLY and p.name in named}
    if all(p.name in named for p in positional):
        return fn(*[named[p.name] for p in positional], **kwargs)
    rest = [v for k, v in values if k not in kwargs]
    return fn(*rest[:len(positional)], **kwargs)


def judge(rep, cfg, *, committer_email: Optional[str] = None, author_email: Optional[str] = None,
          message: Optional[str] = None) -> dict:
    """{would_block, mode, problems, reason, fix[, warnings]} for a commit in rep.repo."""
    from . import resolve
    probs = identity_problems(rep, cfg, committer_email, author_email)
    probs += [_problem(p.code, p.message, resolve._shown_fix(p) or None) for p in rep.problems
              if p.code in GUARD_CODES]
    warnings: List[str] = []
    if message is not None:
        probs += trailer_problems(rep.repo, rep, cfg, message, warnings)
    mode = cfg.agents.hook
    out = {"would_block": bool(probs) and mode == "block", "mode": mode, "problems": probs,
           "reason": probs[0]["message"] if probs else None, "fix": probs[0]["fix"] if probs else None}
    if warnings:
        out["warnings"] = warnings
    return out
