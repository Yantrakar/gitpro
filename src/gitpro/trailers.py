"""Trailer policy per identity (`co_authored_by = allow | forbid | require`, `forbid_emails`) and the
guard's commit-msg entry `hook_message(ctx)`.

Hot path: the commit-msg hook imports this module, so it imports only the standard library and
gitpro.config at module level (resolve is imported inside hook_message, where guard has loaded it already).
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .config import ConfigError

BLOCK = 3                 # guard.BLOCK; not imported, guard is not needed to judge a message
GIT_TIMEOUT = 5           # guard.GIT_TIMEOUT: every git call inside a hook
CO_AUTHOR = "co-authored-by"
VALUES = ("allow", "forbid", "require")

Trailer = Tuple[str, str]


@dataclass(frozen=True)
class Policy:
    """The trailer policy that applies to one identity, and where each part came from (for messages)."""
    co_authored_by: str = "allow"            # "allow" | "forbid" | "require"
    forbid_emails: Tuple[str, ...] = ()
    who: str = "the [default] identity"      # "identity work-github" | "the [default] identity"
    table: str = ""                          # "[identity.trailers]" | "[default.trailers]" | "" (built-in allow)
    emails_table: str = ""                   # the same, for forbid_emails

    def active(self) -> bool:
        """False when nothing can be a violation (allow and no forbid_emails): no need to read trailers."""
        return self.co_authored_by != "allow" or bool(self.forbid_emails)


def _trailers_of(obj: object) -> object:
    """An Identity / Default's `trailers`, or obj itself when it already is a Trailers (or None)."""
    return getattr(obj, "trailers", obj)


def policy_for(identity: object, default: object) -> Policy:
    """identity's [identity.trailers] value, else [default.trailers], else allow / no forbidden emails.
    identity: config.Identity or None (no identity matched); default: config.Default (or its Trailers)."""
    it, dt = _trailers_of(identity), _trailers_of(default)
    who = f"identity {identity.id}" if getattr(identity, "id", None) else "the [default] identity"
    co, table = "allow", ""
    for t, name in ((it, "[identity.trailers]"), (dt, "[default.trailers]")):
        if t is not None and getattr(t, "co_authored_by", None):
            co, table = t.co_authored_by, name
            break
    emails, etable = (), ""
    for t, name in ((it, "[identity.trailers]"), (dt, "[default.trailers]")):
        if t is not None and getattr(t, "forbid_emails", None) is not None:
            emails, etable = tuple(t.forbid_emails), name
            break
    return Policy(co, emails, who, table, etable)


# ---------------------------------------------------------------- matching

def glob_match(pattern: str, text: str) -> bool:
    """Case-insensitive glob with `*` and `?`; `[` is always a literal character, never a class:
    "*[bot]@users.noreply.github.com" matches "dependabot[bot]@users.noreply.github.com" but not
    "123+scott@users.noreply.github.com"."""
    return fnmatch.fnmatchcase(text.casefold(), pattern.casefold().replace("[", "[[]"))


def email_of(value: str) -> str:
    """The address in a trailer value "Name <email>", else the value itself."""
    v = value.strip()
    lt, gt = v.rfind("<"), v.rfind(">")
    return v[lt + 1:gt].strip() if 0 <= lt < gt else v


def is_co_author(key: str) -> bool:
    return key.strip().casefold() == CO_AUTHOR


def split_lines(text: str) -> List[Trailer]:
    """(key, value) per "Key: value" line, as `git interpret-trailers --parse` and %(trailers:only,unfold)
    print them."""
    out: List[Trailer] = []
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out.append((key.strip(), value.strip()))
    return out


# ---------------------------------------------------------------- checking

def violations(trailers: Sequence[Trailer], policy: Policy) -> List[Tuple[str, str]]:
    """(message, fix) per violation of policy by a commit message carrying `trailers`."""
    out: List[Tuple[str, str]] = []
    co = [(k, v) for k, v in trailers if is_co_author(k)]
    for k, v in co:
        shown = f"{k}: {v}"
        if policy.co_authored_by == "forbid":
            out.append((f"{shown} is not allowed for {policy.who} "
                        f"({policy.table} co_authored_by = \"forbid\")", "remove the trailer from the message"))
            continue
        hit = next((g for g in policy.forbid_emails if glob_match(g, email_of(v))), None)
        if hit is not None:
            out.append((f"{shown} is not allowed for {policy.who} ({policy.emails_table} forbid_emails has "
                        f"\"{hit}\")", "remove the trailer from the message"))
    if policy.co_authored_by == "require" and not co:
        out.append((f"{policy.who} requires a Co-Authored-By trailer on every commit",
                    "end the message with a trailer line: Co-Authored-By: Name <email>"))
    return out


def check(trailers: Sequence[Trailer], policy: Policy) -> List[str]:
    """The violation messages (empty when the trailers satisfy the policy)."""
    return [m for m, _ in violations(trailers, policy)]


# ---------------------------------------------------------------- reading trailers

def _interpret(argv: List[str], stdin: Optional[str] = None) -> List[Trailer]:
    try:
        r = subprocess.run(["git", "interpret-trailers", "--parse", "--no-divider", *argv], input=stdin,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=GIT_TIMEOUT, **({} if stdin is not None else {"stdin": subprocess.DEVNULL}))
    except FileNotFoundError:
        raise ConfigError("git not found") from None
    except subprocess.TimeoutExpired:
        raise ConfigError(f"git interpret-trailers timed out after {GIT_TIMEOUT}s") from None
    if r.returncode:
        raise ConfigError(f"git interpret-trailers failed: {r.stderr.strip() or r.returncode}")
    return split_lines(r.stdout)


def parse(file: object) -> List[Trailer]:
    """The trailers of the commit message in `file`, as git sees them (`git interpret-trailers --parse`:
    comment lines and a scissors line are ignored, folded values unfolded)."""
    return _interpret([str(file)])


def parse_text(message: str) -> List[Trailer]:
    """parse() for a message held in a string."""
    return _interpret([], message)


# ---------------------------------------------------------------- commit-msg entry (01 §5.6)

def _quiet_already(ctx) -> bool:
    """guard's warn-once rule: pre-commit of this git command already printed a fail-open warning."""
    f, pid = os.environ.get("GITPRO_WARNED_FILE"), os.environ.get("GITPRO_GIT_PID")
    if not (f and pid and ctx.hook == "commit-msg"):
        return False
    try:
        return Path(f).read_text(encoding="utf-8", errors="replace").strip() == pid
    except OSError:
        return False


def _in_merge() -> bool:
    """A merge commit is being made (git merge runs commit-msg too); merges need no Co-Authored-By."""
    try:
        r = subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _policy(ctx) -> Policy:
    report = getattr(ctx, "report", None)
    if report is not None:
        exp = report.expected
    else:
        from . import resolve
        exp = resolve.expected(Path(ctx.repo), ctx.cfg)
    return policy_for(exp.match.identity, ctx.cfg.default)


def _hook_message(ctx) -> int:
    if not ctx.args:
        raise ConfigError("commit-msg called without a message file")
    policy = _policy(ctx)
    if not policy.active():
        return 0
    if policy.co_authored_by == "require" and _in_merge():
        policy = replace(policy, co_authored_by="allow")
        if not policy.active():
            return 0
    found = violations(parse(ctx.args[0]), policy)
    if not found:
        return 0
    warn = getattr(getattr(ctx.cfg, "guard", None), "on_mismatch", "block") != "block"
    for msg, _ in found:
        ctx.say(("⚠" if warn else "✗") + f" gitpro: {msg}")
    for fix in dict.fromkeys(f for _, f in found):
        ctx.say(f"  fix:   {fix}")
    if warn:
        ctx.say('  (not blocking: [guard] on_mismatch = "warn")')
    return BLOCK


def hook_message(ctx) -> int:
    """guard.HOOKS["commit-msg"] entry: BLOCK (3) when the message's trailers break the trailer policy of
    the repo's identity, else 0. Never raises: any failure is one ⚠ line and 0 (fail open)."""
    try:
        return _hook_message(ctx)
    except KeyboardInterrupt:
        raise
    except BaseException as e:   # noqa: BLE001 -- a broken gitpro must never block a commit
        msg = str(e) if isinstance(e, ConfigError) else (f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
        try:
            if not _quiet_already(ctx):
                ctx.say(f"⚠ gitpro trailers: {msg}; not blocking")
        except Exception:   # noqa: BLE001
            pass
        return 0
