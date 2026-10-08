"""Team policy (spec 14): an org's policy TOML (allowed email domains per remote owner, signing, trailer
rules), checked on this machine (`gitpro check --policy`, `gitpro team`), in the guard's hooks
(`hook_commit`, `hook_push`, `hook_message`, named by guard.HOOKS) and in CI (`gitpro check --policy FILE
--range A..B`, wrapped by the composite action.yml at the repo root).

Hot path: the guard's hooks import this module, so it imports only the standard library, gitpro.config,
gitpro.trailers and gitpro.resolve (loaded already by the guard; needed for resolve.EXTRA_CHECKS) at
module level. audit, guard, health, forge and agents are imported inside functions.
"""
from __future__ import annotations

import argparse
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import config, resolve, trailers
from .config import ConfigError

PROG = "gitpro"
BLOCK = 3                  # guard.BLOCK
GIT_TIMEOUT = 5            # guard.GIT_TIMEOUT: every git call inside a hook
RANGE_TIMEOUT = 120        # --range: one git log over a PR's commits (signature checks included)
REPO_POLICY = ".gitpro-policy.toml"
FORMATS = ("ssh", "gpg", "x509")
TOP_KEYS = ("policy", "rule")
POLICY_KEYS = ("name", "version", "contact", "allowed_signers")
RULE_KEYS = ("host", "owners", "allowed_email_domains", "allowed_emails", "require_signing", "signing_formats",
             "co_authored_by", "forbid_emails", "require_verified_email")
VERSION = 1                # the newest policy format this gitpro understands
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+$")
REL_PATH_RE = re.compile(r"^[A-Za-z0-9._@+-][A-Za-z0-9._/@+-]*$")
SIGNED = ("G", "U", "X", "Y", "E", "R")
WARNINGS = {"X": "expired", "Y": "expired key", "E": "unverifiable here"}
GOOD = {"G": "good signature", "U": "good signature, unknown validity"}
_RANK = {"allow": 0, "require": 1, "forbid": 2}     # rules disagree: forbid wins over require over allow


# ---------------------------------------------------------------- data

@dataclass(frozen=True)
class Rule:
    host: str
    owners: Tuple[str, ...] = ()
    allowed_email_domains: Tuple[str, ...] = ()
    allowed_emails: Tuple[str, ...] = ()
    require_signing: bool = False
    signing_formats: Tuple[str, ...] = ()
    co_authored_by: str = "allow"
    forbid_emails: Tuple[str, ...] = ()
    require_verified_email: bool = False
    index: int = 1                       # position in the file, from 1

    def to_json(self) -> dict:
        return {"index": self.index, "host": self.host, "owners": list(self.owners),
                "allowed_email_domains": list(self.allowed_email_domains),
                "allowed_emails": list(self.allowed_emails), "require_signing": self.require_signing,
                "signing_formats": list(self.signing_formats), "co_authored_by": self.co_authored_by,
                "forbid_emails": list(self.forbid_emails), "require_verified_email": self.require_verified_email}

    def emails(self) -> bool:
        return bool(self.allowed_email_domains or self.allowed_emails)

    def signing(self) -> bool:
        return self.require_signing or bool(self.signing_formats)


@dataclass(frozen=True)
class Policy:
    name: str
    path: str                            # the file, ~ expanded
    rules: Tuple[Rule, ...]
    version: int = 1
    contact: Optional[str] = None
    allowed_signers: Optional[str] = None   # absolute path of the ssh allowed_signers file it ships

    def cause(self) -> str:
        return f"policy: {self.name}" + (f" ({self.contact})" if self.contact else "")


Found = List[Tuple[Policy, List[Rule]]]   # policies that apply, each with the rules that match


# ---------------------------------------------------------------- loading and validation

_CACHE: Dict[str, Tuple[Tuple[int, int], Policy]] = {}


def _tilde(path: object) -> str:
    return resolve._tilde(str(path))


def _strings(v: object, where: str, key: str, errs: List[str], pattern: Optional["re.Pattern[str]"] = None,
             allowed: Sequence[str] = ()) -> Tuple[str, ...]:
    if v is None:
        return ()
    if not isinstance(v, list) or not all(isinstance(s, str) and s for s in v):
        errs.append(f"{where}: {key} must be a list of strings")
        return ()
    for s in v:
        if config.CONTROL_RE.search(s):
            errs.append(f"{where}: {key} contains a control character")
        elif pattern is not None and not pattern.match(s):
            errs.append(f"{where}: {key} has an invalid entry {s!r}")
        elif allowed and s not in allowed:
            errs.append(f"{where}: {key} has {s!r} (allowed: {', '.join(allowed)})")
    return tuple(v)


def _bool(v: object, where: str, key: str, errs: List[str]) -> bool:
    if v is None:
        return False
    if not isinstance(v, bool):
        errs.append(f"{where}: {key} must be true or false")
        return False
    return v


def _text(v: object, where: str, key: str, errs: List[str]) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip() or config.CONTROL_RE.search(v):
        errs.append(f"{where}: {key} must be a non-empty string without control characters")
        return None
    return v


def _unknown(table: dict, allowed: Sequence[str], where: str, errs: List[str]) -> None:
    extra = sorted(set(table) - set(allowed))
    if extra:
        errs.append(f"{where}: unknown key(s) {', '.join(extra)} (allowed: {', '.join(allowed)})")


def _within(path: object, root: object) -> bool:
    """Does path, symlinks resolved, stay inside root (symlinks resolved)?"""
    real, top = os.path.realpath(str(path)), os.path.realpath(str(root))
    return real == top or real.startswith(top.rstrip(os.sep) + os.sep)


def _signers(v: object, path: Path, errs: List[str], *, base: Optional[Path] = None,
             inside: Optional[Path] = None) -> Optional[str]:
    """The allowed_signers file: relative to base (default: the policy's directory), or ~/... or /...
    With inside (a policy that comes from a repo), it must be relative and stay inside that directory."""
    if v is None:
        return None
    ok = isinstance(v, str) and (config.PATH_RE.match(v) or REL_PATH_RE.match(v)) and ".." not in v.split("/")
    if not ok:
        errs.append(f"{path}: [policy]: allowed_signers must be a path relative to the policy file, or start "
                    f"with ~/ or /")
        return None
    assert isinstance(v, str)
    if inside is not None and (v.startswith("~/") or v.startswith("/")):
        errs.append(f"{path}: [policy]: allowed_signers must be a path inside the repo, relative to the policy file "
                    f"(got {v})")
        return None
    p = Path(os.path.expanduser(v)) if v.startswith("~/") else (
        Path(v) if v.startswith("/") else (base or path.parent) / v)
    if inside is not None and not _within(p, inside):
        errs.append(f"{path}: [policy]: allowed_signers {v} leaves the repo (a symlink?); refused")
        return None
    if not p.is_file():
        errs.append(f"{path}: allowed_signers {p} not found")
    return str(p)


def _rule(r: object, n: int, path: Path, errs: List[str]) -> Optional[Rule]:
    where = f"{path}: [[rule]] {n}"
    if not isinstance(r, dict):
        errs.append(f"{where}: must be a table")
        return None
    _unknown(r, RULE_KEYS, where, errs)
    host = r.get("host")
    if not isinstance(host, str) or not config.HOST_RE.match(host):
        errs.append(f"{where}: host must be a host name like github.com" if host is not None
                    else f"{where}: host is required")
        host = ""
    co = r.get("co_authored_by", "allow")
    if co not in trailers.VALUES:
        errs.append(f"{where}: co_authored_by must be one of {', '.join(trailers.VALUES)} (got {co!r})")
        co = "allow"
    return Rule(host=host,
                owners=_strings(r.get("owners"), where, "owners", errs, config.OWNER_RE),
                allowed_email_domains=_strings(r.get("allowed_email_domains"), where, "allowed_email_domains", errs,
                                               DOMAIN_RE),
                allowed_emails=_strings(r.get("allowed_emails"), where, "allowed_emails", errs),
                require_signing=_bool(r.get("require_signing"), where, "require_signing", errs),
                signing_formats=_strings(r.get("signing_formats"), where, "signing_formats", errs, allowed=FORMATS),
                co_authored_by=co,
                forbid_emails=_strings(r.get("forbid_emails"), where, "forbid_emails", errs),
                require_verified_email=_bool(r.get("require_verified_email"), where, "require_verified_email", errs),
                index=n)


def parse(data: dict, path: Path, *, signers_base: Optional[Path] = None,
          inside: Optional[Path] = None) -> Tuple[Optional[Policy], List[str]]:
    """(Policy, []) for a valid policy table, else (None, every error as "<path>: <where>: <problem>").
    signers_base: where a relative allowed_signers is looked up (default: path's directory); inside: the
    repo a repo policy comes from (its allowed_signers must stay inside)."""
    errs: List[str] = []
    _unknown(data, TOP_KEYS, str(path), errs)
    head = data.get("policy", {})
    if not isinstance(head, dict):
        errs.append(f"{path}: [policy]: must be a table")
        head = {}
    _unknown(head, POLICY_KEYS, f"{path}: [policy]", errs)
    name = _text(head.get("name"), f"{path}: [policy]", "name", errs) or path.name
    contact = _text(head.get("contact"), f"{path}: [policy]", "contact", errs)
    version = head.get("version", VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errs.append(f"{path}: [policy]: version must be a whole number (1)")
        version = VERSION
    elif version > VERSION:
        errs.append(f"{path}: [policy]: version {version} is newer than this gitpro understands ({VERSION}); "
                    f"upgrade gitpro")
    signers = _signers(head.get("allowed_signers"), path, errs, base=signers_base, inside=inside)
    raw = data.get("rule")
    rules: List[Rule] = []
    if raw is None:
        errs.append(f"{path}: [[rule]]: at least one [[rule]] is required")
    elif not isinstance(raw, list):
        errs.append(f"{path}: rule: write rules as [[rule]]")
    else:
        for n, r in enumerate(raw, 1):
            rule = _rule(r, n, path, errs)
            if rule is not None:
                rules.append(rule)
    if errs:
        return None, errs
    return Policy(name, str(path), tuple(rules), version, contact, signers), []


def _toml(text: str, path: Path) -> dict:
    """config._load_toml, with the parser's own failures on hostile input (a TOML nested 100 000 deep
    exhausts the recursion limit) as ConfigError too, so they exit 2 like any invalid policy."""
    try:
        return config._load_toml(text, path)
    except (RecursionError, MemoryError):
        raise ConfigError(f"{path}: invalid TOML: nested too deeply") from None
    except (ValueError, TypeError) as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None


def _read(path: Path, *, check_perms: bool, who: str) -> Tuple[os.stat_result, Path]:
    try:
        st = path.stat()
    except FileNotFoundError:
        raise ConfigError(f"policy {_tilde(path)}: not found" + (f" ({who})" if who else "")) from None
    except OSError as e:
        raise ConfigError(f"policy {_tilde(path)}: {e.strerror or e}") from None
    if check_perms:
        config.check(st.st_uid == os.getuid() and not st.st_mode & 0o022,
                     f"{path}: must be owned by you and not group/world-writable (chmod 600 {path})")
    return st, path


def load(path: object, *, check_perms: bool = True, who: str = "", inside: Optional[Path] = None) -> Policy:
    """The validated policy in path; ConfigError when it is missing, invalid or (check_perms, local use)
    group/world-writable or not the user's. Cached by (path, mtime_ns, size). who names the referrer
    ("identity work-github") in the not-found message. inside: a repo's own policy, which (and whose
    allowed_signers) must not leave that directory through a symlink."""
    p = Path(os.path.expanduser(str(path)))
    if inside is not None and p.exists() and not _within(p, inside):
        raise ConfigError(f"{p} leaves the repo (a symlink?); refused")
    st, _ = _read(p, check_perms=check_perms, who=who)
    key = (st.st_mtime_ns, st.st_size)
    ckey = str(p) if inside is None else f"{p}\0{inside}"
    hit = _CACHE.get(ckey)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeError:
        raise ConfigError(f"{p}: not UTF-8 text") from None
    except OSError as e:
        raise ConfigError(f"policy {_tilde(p)}: {e.strerror or e}") from None
    data = _toml(text, p)
    pol, errs = parse(data, p, inside=inside)
    if pol is None:
        raise ConfigError(errs[0])
    _CACHE[ckey] = (key, pol)
    return pol


def lint(path: object) -> Tuple[Optional[Policy], List[str]]:
    """(policy, every error) for `policy lint`; no permission check, no cache."""
    p = Path(os.path.expanduser(str(path)))
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [f"policy {_tilde(p)}: not found"]
    except (OSError, UnicodeError) as e:
        return None, [f"{p}: {e}"]
    try:
        data = _toml(text, p)
    except ConfigError as e:
        return None, [str(e)]
    return parse(data, p)


# ---------------------------------------------------------------- which rules apply

def _glob(pattern: str, text: str) -> bool:
    """An owner glob, case-insensitive, with gitpro's wildmatch rules: "*", "?" and [...] stay inside one
    path segment; "**" as a whole segment crosses "/"."""
    return resolve.wildmatch(pattern.casefold(), text.casefold())


def _owner_ok(globs: Sequence[str], owner: Optional[str]) -> bool:
    """owner (maybe nested: group/sub) matches one of the globs, itself or through a parent group (a rule
    for a GitLab group covers its subgroups). "*" never matches "/"."""
    if not globs:
        return True
    if not owner:
        return False
    parts = owner.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    return any(_glob(g, pre) for g in globs for pre in prefixes)


def _where(url: str, cfg: object = None) -> Tuple[Optional[str], Optional[str]]:
    """(host, owner) of a remote url; an identity's ssh_alias host counts as the identity's real host."""
    host, owner, _ = resolve.parse_url(url)
    if host and cfg is not None:
        for i in getattr(cfg, "identities", ()):
            if i.ssh_alias and i.ssh_alias.casefold() == host.casefold():
                host = i.host
    return host, owner


def rule_matches(rule: Rule, url: str, cfg: object = None) -> bool:
    host, owner = _where(url, cfg)
    return bool(host) and rule.host.casefold() == host.casefold() and _owner_ok(rule.owners, owner)


def _overlaps(rule: Rule, ident: object) -> bool:
    """Could rule apply to a repo of this identity (same host, owners overlapping)?"""
    if rule.host.casefold() != str(getattr(ident, "host", "")).casefold():
        return False
    owners = tuple(getattr(ident, "owners", ()) or ())
    if not rule.owners or not owners:
        return True
    return any(_owner_ok(rule.owners, o) or any(_glob(o, g) for g in rule.owners) for o in owners)


def match(policies: Sequence[Policy], urls: Sequence[str], cfg: object = None) -> Found:
    out: Found = []
    for pol in policies:
        rules = [r for r in pol.rules if any(rule_matches(r, u, cfg) for u in urls)]
        if rules:
            out.append((pol, rules))
    return out


def sources(cfg: object, identity: object = None, extra: Sequence[str] = ()) -> List[Tuple[str, str]]:
    """(path, who) of every policy file that may apply: extra (--policy), [policy].files, identity.policy."""
    out = [(str(p), "--policy") for p in extra]
    out += [(f, "[policy] files") for f in getattr(getattr(cfg, "policy", None), "files", ()) or ()]
    if identity is not None and getattr(identity, "policy", None):
        out.append((identity.policy, f"identity {identity.id}"))
    seen, uniq = set(), []
    for path, who in out:
        key = os.path.realpath(os.path.expanduser(path))
        if key not in seen:
            seen.add(key)
            uniq.append((path, who))
    return uniq


def _load_all(srcs: Sequence[Tuple[str, str]], check_perms: bool) -> List[Policy]:
    return [load(p, check_perms=check_perms, who="" if who == "--policy" else who) for p, who in srcs]


def any_policy(cfg: object) -> bool:
    """Does the config name a policy at all (no file is read)?"""
    return bool(getattr(getattr(cfg, "policy", None), "files", ())) or \
        any(getattr(i, "policy", None) for i in getattr(cfg, "identities", ()))


def _repo_urls(remotes: Dict[str, str], matched: Optional[str] = None) -> List[str]:
    """The url the rules judge: the one that matched the identity, else origin, else every remote's."""
    if matched:
        return [matched]
    if "origin" in remotes:
        return [remotes["origin"]]
    return list(remotes.values())


def _looks_like_url(s: str) -> bool:
    return not os.path.isdir(s) and ("://" in s or bool(re.match(r"^[^/:]+:", s)))


def applicable(repo_or_url: object, cfg: object, *, extra: Sequence[str] = (), check_perms: bool = True) -> Found:
    """The policies (with their matching rules) for a repo directory or a remote url: [policy].files plus
    the matched identity's policy (plus extra paths, `--policy`). Rules match on the remote's host and
    owner; an empty owners list matches every owner on the host."""
    s = str(repo_or_url)
    if _looks_like_url(s):
        ident = resolve.match_url(s, cfg)[0] if cfg is not None else None
        urls = [s]
    else:
        repo = Path(s)
        ident, matched = None, None
        if cfg is not None:
            exp = resolve.expected(repo, cfg)
            ident, matched = exp.match.identity, exp.match.url
        urls = _repo_urls(resolve.remotes(repo), matched)
    if not urls:
        return []
    return match(_load_all(sources(cfg, ident, extra), check_perms), urls, cfg)


# ---------------------------------------------------------------- the rules themselves

def email_problem(rules: Sequence[Rule], email: Optional[str]) -> Optional[Tuple[str, str]]:
    """(rule key, message) for the first rule this author/committer email breaks, else None."""
    if not email:
        return None
    for r in rules:
        hit = next((g for g in r.forbid_emails if trailers.glob_match(g, email)), None)
        if hit is not None:
            return "forbid_emails", f"email {email} is forbidden (forbid_emails has \"{hit}\")"
    domain = email.rpartition("@")[2].casefold()
    for r in rules:
        if not r.emails():
            continue
        if domain in {d.casefold() for d in r.allowed_email_domains}:
            continue
        if any(trailers.glob_match(g, email) for g in r.allowed_emails):
            continue
        shown = ", ".join(r.allowed_email_domains) or "only the addresses in allowed_emails"
        return "allowed_email_domains", f"email domain {domain} not allowed ({shown})"
    return None


def _domains(rules: Sequence[Rule]) -> List[str]:
    return list(dict.fromkeys(d for r in rules for d in r.allowed_email_domains))


def trailer_policy(pol: Policy, rules: Sequence[Rule]) -> trailers.Policy:
    """The trailer rules of these policy rules as a trailers.Policy (forbid > require > allow)."""
    co = max((r.co_authored_by for r in rules), key=lambda v: _RANK[v], default="allow")
    emails = tuple(dict.fromkeys(g for r in rules for g in r.forbid_emails))
    return trailers.Policy(co, emails, f"policy {pol.name}", "[[rule]]", "[[rule]]")


def trailer_problems(pol: Policy, rules: Sequence[Rule], found: Sequence[trailers.Trailer],
                     merge: bool = False) -> List[Tuple[str, str]]:
    """(rule key, message) per trailer violation; a merge needs no Co-Authored-By."""
    tp = trailer_policy(pol, rules)
    co = "allow" if merge and tp.co_authored_by == "require" else tp.co_authored_by
    out = [("co_authored_by", m) for m in trailers.check(found, replace(tp, co_authored_by=co, forbid_emails=()))]
    if co != "forbid" and tp.forbid_emails:
        out += [("forbid_emails", m) for m in trailers.check(found, replace(tp, co_authored_by="allow"))]
    return out


def _sig_format(body: bytes) -> Optional[str]:
    """ssh | gpg | x509 | unknown from a raw commit's gpgsig header, None when it carries no signature."""
    head = body.split(b"\n\n", 1)[0]
    for line in head.split(b"\n"):
        if line.startswith(b"gpgsig ") or line.startswith(b"gpgsig-sha256 "):
            first = line.split(b" ", 1)[1]
            if b"SSH SIGNATURE" in first:
                return "ssh"
            if b"PGP SIGNATURE" in first:
                return "gpg"
            if b"SIGNED MESSAGE" in first:
                return "x509"
            return "unknown"
    return None


def classify(git_status: str, fmt: Optional[str], signer: str) -> str:
    """The %G? status we judge: git says N for a signed commit when no allowed signers are configured and
    U with no signer for an ssh key that is not in them; both are E (cannot check) here."""
    if fmt is None:
        return "N"
    if git_status == "N":
        return "E"
    if git_status == "U" and fmt == "ssh" and not signer:
        return "E"
    return git_status or "E"


SSH_KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ssh-dss", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                 "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")


def _ssh_fields(line: str) -> List[str]:
    """Whitespace-separated fields of an allowed_signers line; double quotes group (and are removed)."""
    out: List[str] = []
    cur: List[str] = []
    quoted = started = False
    for ch in line:
        if ch == '"':
            quoted, started = not quoted, True
        elif ch.isspace() and not quoted:
            if started:
                out.append("".join(cur))
            cur, started = [], False
        else:
            cur.append(ch)
            started = True
    if started:
        out.append("".join(cur))
    return out


def _ssh_options(text: str) -> Dict[str, str]:
    """name (lowercased) -> value of an options field: comma-separated, quotes may hold commas."""
    out: Dict[str, str] = {}
    cur: List[str] = []
    quoted = False
    for ch in text + ",":
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            item = "".join(cur)
            if item:
                k, _, v = item.partition("=")
                out[k.strip().lower()] = v
            cur = []
        else:
            cur.append(ch)
    return out


def ssh_fingerprint(blob_b64: str) -> Optional[str]:
    """OpenSSH's SHA256 fingerprint of a base64 public key blob ("SHA256:" + unpadded base64)."""
    import base64
    import binascii
    import hashlib
    try:
        blob = base64.b64decode(blob_b64.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeError):
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def signer_principals(files: Sequence[str]) -> Dict[str, List[str]]:
    """key fingerprint -> the principal patterns of every allowed_signers line for that key (lines whose
    namespaces option excludes "git" and cert-authority lines are skipped). git records only the first
    principal that verifies as %GS, so the committer is checked against this union instead."""
    out: Dict[str, List[str]] = {}
    for name in dict.fromkeys(files):
        try:
            text = Path(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            f = _ssh_fields(line)
            if len(f) < 3:
                continue
            if f[1] in SSH_KEY_TYPES:
                opts, blob = {}, f[2]
            elif len(f) >= 4 and f[2] in SSH_KEY_TYPES:
                opts, blob = _ssh_options(f[1]), f[3]
            else:
                continue
            if "cert-authority" in opts:
                continue
            ns = opts.get("namespaces")
            if ns is not None and not _pattern_list_ok(ns.split(","), "git", fold=False):
                continue
            fp = ssh_fingerprint(blob)
            if fp:
                out.setdefault(fp, []).extend(p.strip() for p in f[0].split(",") if p.strip())
    return out


def _pattern_list_ok(pats: Sequence[str], text: str, *, fold: bool = True) -> bool:
    """OpenSSH pattern-list rules: a matching "!pattern" denies, else any matching pattern allows."""
    match = trailers.glob_match if fold else (lambda g, t: resolve.wildmatch(g, t))
    pats = [p.strip() for p in pats if p.strip()]
    if any(p.startswith("!") and match(p[1:], text) for p in pats):
        return False
    return any(not p.startswith("!") and match(p, text) for p in pats)


def principal_ok(signer: object, email: Optional[str]) -> bool:
    """Does the committer email match the signer's principal patterns (a list, or one comma-separated
    string; ssh pattern-list rules, "!" negates; case-insensitive)? git itself does not compare them: a key
    listed for alice@ verifies a commit whose committer claims to be bob@ as G."""
    raw = signer.split(",") if isinstance(signer, str) else list(signer or ())
    pats = [p.strip() for p in raw if p and p.strip()]
    if not email or not pats:
        return False
    if any(p.startswith("!") and trailers.glob_match(p[1:], email) for p in pats):
        return False
    return any(not p.startswith("!") and trailers.glob_match(p, email) for p in pats)


def signing_problems(rules: Sequence[Rule], status: str, fmt: Optional[str], ships_signers: bool,
                     merge: bool = False, *, signer: str = "", committer: str = "",
                     principals: Optional[Sequence[str]] = None) -> List[Tuple[str, str]]:
    """(rule key, message) per signing violation of one commit. With the policy's allowed_signers, an ssh
    signature must also come from a principal matching the committer email (rule allowed_signers):
    principals are the patterns of the key's allowed_signers lines (signer_principals), else %GS alone."""
    out: List[Tuple[str, str]] = []
    required = any(r.require_signing for r in rules)
    if required:
        if status == "N":
            out.append(("require_signing", "not signed (require_signing)"))
        elif status == "B":
            out.append(("require_signing", "bad signature (require_signing)"))
        elif status == "R":
            out.append(("require_signing", "signed with a revoked key"))
        elif status == "E" and ships_signers:
            if fmt == "ssh":
                out.append(("require_signing", "signed by a key that is not in the policy's allowed_signers"))
            else:
                out.append(("require_signing", f"{fmt or 'unknown'} signature cannot be checked: the policy's "
                                               "allowed_signers lists ssh keys only"))
    pats = list(principals) if principals else ([signer] if signer else [])
    if ships_signers and fmt == "ssh" and status in ("G", "U") and pats and not principal_ok(pats, committer):
        out.append(("allowed_signers", f"signed by {','.join(pats)} (allowed_signers), but committed as {committer}"))
    if fmt is not None:
        for r in rules:
            if r.signing_formats and fmt not in r.signing_formats:
                out.append(("signing_formats", f"format {fmt} not allowed ({', '.join(r.signing_formats)})"))
                break
    return out


# ---------------------------------------------------------------- git

def _git(repo: Path, *argv: str, pre: Sequence[str] = (), stdin: Optional[bytes] = None,
         timeout: int = GIT_TIMEOUT, what: str = "") -> bytes:
    try:
        r = subprocess.run(["git", *pre, "-C", str(repo), *argv], input=stdin, capture_output=True,
                           env=resolve._child_env(), timeout=timeout,
                           **({} if stdin is not None else {"stdin": subprocess.DEVNULL}))
    except FileNotFoundError:
        raise ConfigError("git not found") from None
    except subprocess.TimeoutExpired:
        raise ConfigError(f"{what or 'git ' + argv[0]} timed out after {timeout}s") from None
    if r.returncode:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ConfigError(f"{what or 'git ' + argv[0]} failed: {err[-1] if err else r.returncode}")
    return r.stdout


def _formats(repo: Path, shas: Sequence[str], timeout: int) -> Dict[str, Optional[str]]:
    """sha -> signature format from the raw commit headers (one `git cat-file --batch`)."""
    if not shas:
        return {}
    out = _git(repo, "cat-file", "--batch", stdin="".join(s + "\n" for s in shas).encode(), timeout=timeout)
    res: Dict[str, Optional[str]] = {}
    pos = 0
    while pos < len(out):
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        head = out[pos:nl].split()
        pos = nl + 1
        if len(head) < 3:
            continue
        size = int(head[2])
        res[head[0].decode()] = _sig_format(out[pos:pos + size])
        pos += size + 1
    return res


@dataclass
class _Commit:
    sha: str
    status: str
    key: str
    signer: str
    author_email: str
    committer_email: str
    date: str
    parents: Tuple[str, ...]
    trailers: List[trailers.Trailer]
    subject: str = ""
    fmt: Optional[str] = None


def _signers_args(found: Found, tmp: List[str]) -> Tuple[List[str], bool]:
    """-c gpg.ssh.allowedSignersFile=... for the allowed_signers the policies ship (several: one temp file)."""
    files = list(dict.fromkeys(p.allowed_signers for p, _ in found if p.allowed_signers))
    if not files:
        return [], False
    path = files[0]
    if len(files) > 1:
        fd, path = tempfile.mkstemp(prefix="gitpro-signers-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for name in files:
                f.write(Path(name).read_text(encoding="utf-8", errors="replace").rstrip("\n") + "\n")
        tmp.append(path)
    return ["-c", f"gpg.ssh.allowedSignersFile={path}"], True


_FIELDS = ("%H", "%G?", "%GK", "%GS", "%ae", "%ce", "%cs", "%P", "%s", "%(trailers:only,unfold)")


def _log(repo: Path, revs: Sequence[str], *, signing: bool, pre: Sequence[str], timeout: int) -> List[_Commit]:
    # Count and identify records independently of untrusted commit text.
    shas = _git(repo, "rev-list", *revs, "--", timeout=timeout).decode("ascii").splitlines()
    if not shas:
        return []
    fields = [f if signing or f not in ("%G?", "%GK", "%GS") else "-" for f in _FIELDS]
    fmt = "%x00".join(fields)
    raw = _git(repo, "log", "-z", "--no-color", "--no-show-signature", f"--format={fmt}",
               "--no-walk=unsorted", "--stdin", pre=pre, stdin="".join(s + "\n" for s in shas).encode(),
               timeout=timeout, what=f"git log {' '.join(revs)}")
    parts = raw.split(b"\0")
    if parts[-1] != b"" or len(parts) != len(shas) * len(fields) + 1:
        raise ConfigError("cannot check policy: malformed commit records from git log")
    out: List[_Commit] = []
    for n, sha in enumerate(shas):
        f = [p.decode("utf-8", "replace") for p in parts[n * len(fields):(n + 1) * len(fields)]]
        if f[0] != sha:
            raise ConfigError(f"cannot check policy: git log did not return expected commit {sha}")
        out.append(_Commit(f[0], f[1] if signing else "", f[2] if signing else "", f[3] if signing else "",
                           f[4], f[5], f[6], tuple(f[7].split()), trailers.split_lines(f[9]), f[8]))
    if signing:
        fmts = _formats(repo, [c.sha for c in out], timeout)
        for c in out:
            c.fmt = fmts.get(c.sha)
            c.status = classify(c.status, c.fmt, c.signer)
    return out


def _judge(c: _Commit, found: Found, signing: bool, ships: bool,
           by_key: Optional[Dict[str, List[str]]] = None) -> List[dict]:
    """Every violation of one commit: {"policy", "rule", "which", "message"}. by_key: signer_principals."""
    out: List[dict] = []
    merge = len(c.parents) > 1
    for pol, rules in found:
        a = email_problem(rules, c.author_email)
        k = email_problem(rules, c.committer_email)
        if a and k and a == k:
            out.append({"policy": pol.name, "rule": a[0], "which": "both", "message": a[1]})
        else:
            if a:
                out.append({"policy": pol.name, "rule": a[0], "which": "author", "message": a[1]})
            if k:
                out.append({"policy": pol.name, "rule": k[0], "which": "committer", "message": "committer " + k[1]})
        if signing:
            for key, msg in signing_problems(rules, c.status, c.fmt, ships, merge, signer=c.signer,
                                             committer=c.committer_email,
                                             principals=(by_key or {}).get(c.key)):
                out.append({"policy": pol.name, "rule": key, "which": "signature", "message": msg})
        for key, msg in trailer_problems(pol, rules, c.trailers, merge):
            out.append({"policy": pol.name, "rule": key, "which": "trailer", "message": msg})
    return out


# ---------------------------------------------------------------- CI mode: --range

def _empty_range(text: str) -> bool:
    return text.strip() in ("", "..", "...")


def check_range(repo: Path, rng: str, found: Found, *, timeout: int = RANGE_TIMEOUT) -> dict:
    """The commits of rng in repo judged against found: {"commits", "violations", "signing", "rows"}."""
    from . import audit
    if _empty_range(rng):
        raise ConfigError(f"commit range is empty ({rng!r}); pass --range BASE..HEAD")
    revs = audit.range_tokens(rng)
    signing = any(r.signing() for _, rules in found for r in rules)
    tmp: List[str] = []
    try:
        pre, ships = _signers_args(found, tmp) if signing else ([], False)
        commits = _log(repo, revs, signing=signing, pre=pre, timeout=timeout)
    finally:
        for t in tmp:
            try:
                os.unlink(t)
            except OSError:
                pass
    by_key = signer_principals([p.allowed_signers for p, _ in found if p.allowed_signers]) if ships else {}
    violations, sig, rows = [], [], []
    for c in commits:
        found_v = _judge(c, found, signing, ships, by_key)
        for v in found_v:
            violations.append({"sha": c.sha, "author_email": c.author_email, "committer_email": c.committer_email,
                               **v})
        if signing:
            sig.append({"sha": c.sha, "status": c.status, "format": c.fmt, "signed": c.status in SIGNED,
                        "warning": WARNINGS.get(c.status)})
        rows.append((c, found_v))
    unverifiable = sum(1 for c in commits if signing and c.status == "E")
    return {"commits": len(commits), "violations": violations, "signing": sig, "rows": rows,
            "signing_checked": signing, "unverifiable": unverifiable}


def _ok_text(c: _Commit, signing: bool) -> str:
    if not signing or c.status not in SIGNED:
        return "✓"
    what = c.fmt or "unknown format"
    if c.status in GOOD:
        return f"✓ signed ({what}, {GOOD[c.status]})"
    return f"✓ signed ({what}) ⚠ {WARNINGS.get(c.status, c.status)}"


def _render_range(data: dict, names: str, where: str, rng: str, contact: str) -> List[str]:
    n = data["commits"]
    lines = [f"policy {names} for {where}, {n} commit{'s' if n != 1 else ''} in {rng}"]
    pad = " " * (2 + 7 + 2 + 10 + 2 + 22)
    bad = 0
    for c, found_v in data["rows"]:
        lead = f"  {c.sha[:7]}  {c.date:<10}  {c.author_email:<21} "
        if not found_v:
            lines.append(lead + _ok_text(c, data["signing_checked"]))
            continue
        bad += 1
        lines.append(lead + f"✗ {found_v[0]['message']}")
        lines += [pad + f"✗ {v['message']}" for v in found_v[1:]]
    tail = f"; contact {contact}" if contact else ""
    if bad:
        lines.append(f"{bad} of {n} commit{'s' if n != 1 else ''} violate{'s' if n == 1 else ''} the policy{tail}")
    else:
        lines.append(f"✓ {n} commit{'s' if n != 1 else ''} meet{'s' if n == 1 else ''} the policy")
    k = data.get("unverifiable", 0)
    if k:
        lines.append(f"⚠ {k} signature{'s' if k != 1 else ''} could not be verified in this environment (key not "
                     f"known here; gpg/x509 use this machine's keyring, ssh the policy's allowed_signers)")
    return lines


def _config_or_none(args, warnings: Optional[List[str]] = None) -> Optional[object]:
    """The user's config, None when there is none. A broken one raises, or with warnings (range mode)
    is noted there and skipped."""
    try:
        path, how = config.find(getattr(args, "config", None), Path(getattr(args, "home", None) or Path.home()))
    except ConfigError:
        return None
    if not path.is_file() and how != "--config":
        return None
    try:
        return config.load_all(path, how)
    except ConfigError as e:
        if warnings is None:
            raise
        warnings.append(f"config not used: {e}")
        return None


def _need_repo(path: object) -> Path:
    top = resolve.is_repo(Path(path))
    if top is None:
        raise ConfigError(f"{path}: not a git repository")
    return top


def _show_where(url: Optional[str], repo: Path) -> str:
    if not url:
        return _tilde(repo)
    host, owner, name = resolve.parse_url(url)
    return "/".join(p for p in (host, owner, name) if p) or (resolve.redact_url(url) or "")


def _range_base(repo: Path, revs: Sequence[str]) -> Optional[str]:
    """The one commit the range excludes (A of A..B, the merge base of A...B), else None."""
    out = _git(repo, "rev-parse", "--revs-only", *revs, what="git rev-parse").decode("utf-8", "replace").split()
    neg = list(dict.fromkeys(x[1:] for x in out if x.startswith("^")))
    return neg[0] if len(neg) == 1 else None


def _base_blob(repo: Path, base: str, rel: str) -> Optional[bytes]:
    """The file rel as committed in base; None when base has no such path. A symlink or a directory there
    is refused (a repo policy must not point outside the repo)."""
    out = _git(repo, "ls-tree", "-z", base, "--", rel).decode("utf-8", "replace")
    entries = [e for e in out.split("\0") if e]
    entry = next((e for e in entries if e.split("\t", 1)[-1] == rel), None)
    if entry is None:
        return None
    mode = entry.split(None, 1)[0]
    if mode == "120000":
        raise ConfigError(f"{rel} is a symlink in {base[:7]}; a repo policy file must be a regular file inside the repo")
    if not mode.startswith("100"):
        raise ConfigError(f"{rel} in {base[:7]} is not a file")
    return _git(repo, "cat-file", "blob", f"{base}:{rel}")


class _RangePolicies:
    """Loads the policies of a --range run. A policy file inside the repo (and the allowed_signers it
    names) is read from the range's base commit, never from the checkout the range produced: a pull
    request cannot widen the policy that judges it."""

    def __init__(self, repo: Path, revs: Sequence[str], tmp: str, warnings: List[str]):
        self.repo, self.revs, self.tmp, self.warnings = repo, list(revs), Path(tmp), warnings
        self._base: Optional[str] = None
        self.added = False

    def base(self, rel: str) -> str:
        if self._base is None:
            self._base = _range_base(self.repo, self.revs)
            if self._base is None:
                raise ConfigError(f"the range {' '.join(self.revs)} has no single base commit to read {rel} from; use "
                                  f"BASE..HEAD, or pass --policy FILE from outside the repo")
        return self._base

    def rel(self, path: object) -> Optional[str]:
        """Resolve external aliases only until entering the repo; checkout symlinks cannot rename a
        policy's protected-base entry."""
        p = os.path.abspath(os.path.expanduser(str(path)))
        tops = tuple(dict.fromkeys((os.path.abspath(self.repo), os.path.realpath(self.repo))))
        for _ in range(40):
            for top in tops:
                if p.startswith(top.rstrip(os.sep) + os.sep):
                    return Path(os.path.relpath(p, top)).as_posix()
            current = Path(p)
            for ancestor in (*reversed(current.parents), current):
                if ancestor.is_symlink():
                    try:
                        target = Path(os.readlink(ancestor))
                    except OSError as e:
                        raise ConfigError(f"cannot resolve policy path {path}: {e}") from None
                    if not target.is_absolute():
                        target = ancestor.parent / target
                    p = os.path.abspath(target / current.relative_to(ancestor))
                    break
            else:
                return None
        raise ConfigError(f"cannot resolve policy path {path}: too many symlinks")

    def changed(self, rels: Sequence[str], base: str) -> List[str]:
        """Which of rels the range touches (a commit in it, merges included against their first parent, or
        the checkout differs from base)."""
        out = _git(self.repo, "log", "--format=", "--name-only", "--diff-merges=first-parent", *self.revs, "--",
                   *rels, timeout=RANGE_TIMEOUT)
        hit = {x.strip() for x in out.decode("utf-8", "replace").splitlines() if x.strip()}
        for rel in rels:
            if rel in hit:
                continue
            wt = self.repo / rel
            try:
                now = wt.read_bytes() if wt.is_file() and not wt.is_symlink() else None
            except OSError:
                now = None
            if now is not None and now != _base_blob(self.repo, base, rel):
                hit.add(rel)
        return [r for r in rels if r in hit]

    def load(self, path: object, who: str = "", *, optional: bool = False) -> Optional[Policy]:
        rel = self.rel(path)
        if rel is None:
            return load(path, check_perms=False, who=who)
        try:
            base = self.base(rel)
        except ConfigError:
            if optional and not (self.repo / rel).exists():
                return None
            raise
        shown = self.repo / rel
        blob = _base_blob(self.repo, base, rel)
        if blob is None:
            if (self.repo / rel).exists() or self.changed([rel], base):
                self.warnings.append(f"{rel}: policy added in this range; not enforced until merged")
                self.added = True
                return None
            if optional:
                return None
            raise ConfigError(f"policy {rel}: not found in the range base {base[:7]}" + (f" ({who})" if who else ""))
        try:
            text = blob.decode("utf-8")
        except UnicodeError:
            raise ConfigError(f"{shown}: not UTF-8 text (at {base[:7]})") from None
        data = _toml(text, shown)
        root = self.tmp / f"base{len(list(self.tmp.iterdir()))}"
        here = posixpath.dirname(rel)
        head = data.get("policy")
        v = head.get("allowed_signers") if isinstance(head, dict) else None
        srel = None
        if isinstance(v, str) and REL_PATH_RE.match(v) and ".." not in v.split("/"):
            srel = posixpath.normpath(posixpath.join(here, v))
            sblob = _base_blob(self.repo, base, srel)
            if sblob is None:
                raise ConfigError(f"{shown}: allowed_signers {self.repo / srel} not found in the range base {base[:7]}")
            dst = root / srel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(sblob)
        root.mkdir(parents=True, exist_ok=True)
        pol, errs = parse(data, shown, signers_base=root / here if here else root, inside=root)
        if pol is None:
            raise ConfigError(errs[0])
        for f in self.changed([rel] + ([srel] if srel else []), base):
            self.warnings.append(f"this range changes {f}; it is judged by the base version ({base[:7]})")
        return pol


def _cmd_range(args) -> int:
    from . import audit, jsonout
    if _empty_range(args.range):
        raise ConfigError(f"commit range is empty ({args.range!r}); pass --range BASE..HEAD")
    revs = audit.range_tokens(args.range)
    repo = _need_repo(args.repo or os.getcwd())
    rems = resolve.remotes(repo)
    urls = _repo_urls(rems)
    url = urls[0] if urls else None
    shown = resolve.redact_url(url) if url else _tilde(repo)
    hint = "; pass --policy FILE or add .gitpro-policy.toml"
    warnings: List[str] = []
    with tempfile.TemporaryDirectory(prefix="gitpro-policy-") as tmp:
        src = _RangePolicies(repo, revs, tmp, warnings)
        if args.policy_files:
            pols = [p for p in (src.load(f) for f in args.policy_files) if p is not None]
            found = match(pols, urls)
            if pols and not found:
                raise ConfigError(f"no policy applies to {shown} (no rule of {', '.join(args.policy_files)} matches)")
        else:
            found = []
            cfg = _config_or_none(args, warnings)
            if cfg is not None and urls:
                ident = resolve.match_url(url, cfg)[0]
                pols = [p for p in (src.load(f, who) for f, who in sources(cfg, ident)) if p is not None]
                found = match(pols, urls, cfg)
            if not found:
                pol = src.load(repo / REPO_POLICY, optional=True)
                if pol is not None:
                    found = match([pol], urls)
                if not found and not src.added:
                    raise ConfigError(f"no policy applies to {shown}{hint}")
        for w in warnings:
            print(f"⚠ {w}", file=sys.stderr)
        data = check_range(repo, args.range, found)
    contact = ", ".join(dict.fromkeys(p.contact for p, _ in found if p.contact))
    if getattr(args, "json", False):
        jsonout.emit("check", {
            "ok": not data["violations"], "mode": "policy", "repo": str(repo), "remote": shown,
            "policies": [{"name": p.name, "file": p.path, "rules": len(rules)} for p, rules in found],
            "range": args.range, "commits": data["commits"], "violations": data["violations"],
            "signing": data["signing"], "unverifiable": data["unverifiable"], "contact": contact or None,
            "warnings": warnings})
    elif not data["commits"]:
        print(f"✓ no commits in {args.range}")
    elif not found:
        print(f"no policy enforced for {data['commits']} commit{'s' if data['commits'] != 1 else ''} in {args.range}")
    else:
        names = ", ".join(p.name for p, _ in found)
        print("\n".join(_render_range(data, names, _show_where(url, repo), args.range, contact)))
    return 1 if data["violations"] else 0


# ---------------------------------------------------------------- machine mode

@dataclass(frozen=True)
class _Person:
    id: str
    label: str                      # "identity work-github" | "[default]"
    email: Optional[str]
    sign: bool
    ssh_key: Optional[str]
    trailers: trailers.Policy
    table: str                      # where a trailers fix goes


def _person(cfg, ident) -> _Person:
    d = cfg.default
    tp = trailers.policy_for(ident, d)
    if ident is None:
        return _Person("(default)", "[default]", d.email, bool(d.sign), d.ssh_key, tp, "[default.trailers]")
    return _Person(ident.id, f"identity {ident.id}", ident.email, config.signs(ident, d), ident.ssh_key or d.ssh_key,
                   tp, "[identity.trailers]")


def _check(name: str, label: str, level: str, message: str, fix: str = "") -> dict:
    return {"name": name, "label": label, "ok": level != "error", "level": level, "message": message, "fix": fix}


_VERB = {"allow": "allows", "forbid": "forbids", "require": "requires"}


def identity_checks(cfg, person: _Person, pol: Policy, rules: Sequence[Rule], keys: Optional[dict] = None,
                    online: bool = False, verified: Optional[Dict[str, Optional[bool]]] = None,
                    verified_why: str = "") -> List[dict]:
    """The rows of `check --policy` for one identity and policy. keys: pub path -> registered (True/False/None)
    from `team --online`; verified: email -> verified (True/False/None, missing = unknown); verified_why: why
    the forge could not be asked."""
    out: List[dict] = []
    if any(r.emails() or r.forbid_emails for r in rules):
        email = person.email or "(no email)"
        prob = email_problem(rules, person.email) if person.email else ("allowed_email_domains", "no email set")
        if prob:
            doms = _domains(rules)
            fix = (f"use an address in {', '.join(doms)}" if doms else "use an allowed address") + \
                (f", or ask {pol.contact} for an exception" if pol.contact else "")
            out.append(_check("email domain", email, "error", prob[1], fix))
        else:
            dom = (person.email or "").rpartition("@")[2]
            hit = next((d for d in _domains(rules) if d.casefold() == dom.casefold()), None)
            out.append(_check("email domain", email, "ok", hit or "allowed_emails" if any(r.emails() for r in rules)
                              else "not forbidden"))
    if any(r.signing() for r in rules):
        required = any(r.require_signing for r in rules)
        fmts = list(dict.fromkeys(f for r in rules for f in r.signing_formats))
        label = "required" if required else "formats " + ", ".join(fmts)
        if required and not person.sign:
            out.append(_check("signing", label, "error", f"{person.label} has sign = false",
                              f"set sign = true, run {PROG} apply"))
        elif person.sign and fmts and "ssh" not in fmts:
            out.append(_check("signing", label, "error", f"{PROG} signs with ssh but the policy allows {', '.join(fmts)}",
                              "sign with an allowed format outside gitpro"))
        else:
            out.append(_check("signing", label, "ok", "sign = true" if person.sign else "not required"))
        if required:
            if not person.ssh_key:
                out.append(_check("signing key", "(none)", "error", f"{person.label} has no ssh_key to sign with",
                                  "set ssh_key in the identity"))
            else:
                pub = os.path.expanduser(person.ssh_key) + ".pub"
                shown = _tilde(pub)
                reg = (keys or {}).get(pub) if online else None
                if not os.path.isfile(pub):
                    out.append(_check("signing key", shown, "error", f"{shown} not found", f"{PROG} keys --generate"))
                elif reg is True:
                    out.append(_check("signing key", shown, "ok", "registered as a signing key"))
                elif reg is False:
                    out.append(_check("signing key", shown, "error", "not registered as a signing key",
                                      f"{PROG} keys --upload"))
                elif online:
                    out.append(_check("signing key", shown, "warning", "could not check that it is registered"))
                else:
                    out.append(_check("signing key", shown, "warning",
                                      "not checked (run gitpro team --online to verify it is registered)"))
    tp = trailer_policy(pol, rules)
    if tp.co_authored_by != "allow":
        mine = person.trailers
        if mine.co_authored_by == tp.co_authored_by:
            out.append(_check("trailers", tp.co_authored_by, "ok",
                              f'{mine.table} co_authored_by = "{mine.co_authored_by}"'))
        else:
            out.append(_check("trailers", tp.co_authored_by, "warning",
                              f"policy {_VERB[tp.co_authored_by]} Co-Authored-By but the "
                              f"{'identity' if person.id != '(default)' else '[default]'} "
                              f"{_VERB[mine.co_authored_by]} it",
                              f'set {person.table} co_authored_by = "{tp.co_authored_by}"'))
    if any(r.require_verified_email for r in rules):
        state = (verified or {}).get((person.email or "").casefold()) if online else None
        if state is True:
            out.append(_check("verified email", person.email or "", "ok", "verified on the forge"))
        elif state is False:
            out.append(_check("verified email", person.email or "", "error", "not verified on the forge",
                              "verify the address in the forge's email settings"))
        else:
            out.append(_check("verified email", person.email or "", "warning",
                              "not checked (needs --online)" if not online else
                              "could not check" + (f" ({verified_why})" if verified_why else "")))
    return out


def machine_data(cfg, repo: Optional[Path] = None, extra: Sequence[str] = (), *, check_perms: bool = True,
                 keys: Optional[dict] = None, online: bool = False,
                 verified: Optional[Dict[str, Optional[bool]]] = None) -> List[dict]:
    """One entry per (identity, policy) that applies: {"id", "policy", "file", "contact", "checks"}.
    With repo: the identity git uses there and the rules matching its remote. Without: every identity with
    a policy (its own, [policy].files, extra), with the rules for its host and owners."""
    out: List[dict] = []

    def add(ident, found: Found) -> None:
        person = _person(cfg, ident)
        for pol, rules in found:
            out.append({"id": person.id, "policy": pol.name, "file": pol.path, "contact": pol.contact,
                        "checks": identity_checks(cfg, person, pol, rules, keys, online, verified)})

    if repo is not None:
        exp = resolve.expected(repo, cfg)
        ident = exp.match.identity
        urls = _repo_urls(resolve.remotes(repo), exp.match.url)
        if urls:
            add(ident, match(_load_all(sources(cfg, ident, extra), check_perms), urls, cfg))
        return out
    for ident in cfg.identities:
        pols = _load_all(sources(cfg, ident, extra), check_perms)
        found = [(p, [r for r in p.rules if _overlaps(r, ident)]) for p in pols]
        add(ident, [(p, rules) for p, rules in found if rules])
    return out


def check_machine(cfg, repo: Optional[Path] = None, extra: Sequence[str] = ()) -> List[resolve.Problem]:
    """What this machine violates, as resolve.Problem (code "policy"); a policy that cannot be loaded is
    one problem too. Never raises ConfigError (doctor's policy row)."""
    try:
        entries = machine_data(cfg, repo, extra)
    except ConfigError as e:
        return [resolve.Problem("policy", str(e), None)]
    return [resolve.Problem("policy", f"policy {e['policy']}: {c['message']}", c["fix"] or None)
            for e in entries for c in e["checks"] if c["level"] == "error"]


def check_repo(repo: Path, cfg, extra: Sequence[str] = ()) -> List[dict]:
    """machine_data for one repo (`check --policy --repo DIR`)."""
    return machine_data(cfg, _need_repo(repo), extra)


_MARK = {"ok": "✓", "warning": "⚠", "error": "✗"}


def _render_machine(entries: List[dict]) -> List[str]:
    lines: List[str] = []
    for e in entries:
        lines.append(f"policy {e['policy']} ({_tilde(e['file'])}) for {e['id']}")
        for c in e["checks"]:
            text = f"{_MARK[c['level']]} {c['message']}" + (f" → {c['fix']}" if c["fix"] and c["level"] != "ok" else "")
            lines.append(f"  {c['name']:<14} {c['label']:<18} {text}")
    return lines


def _cmd_machine(args) -> int:
    from . import jsonout
    path, how = config.find(args.config, Path(args.home))
    cfg = config.load_all(path, how)
    repo = _need_repo(args.repo) if args.repo else resolve.is_repo(Path(os.getcwd()))
    entries = machine_data(cfg, repo, args.policy_files or ())
    problems = sum(1 for e in entries for c in e["checks"] if c["level"] == "error")
    if getattr(args, "json", False):
        jsonout.emit("check", {"ok": problems == 0, "mode": "machine", "repo": str(repo) if repo else None,
                               "identities": [{**e, "checks": [{k: c[k] for k in ("name", "ok", "level", "message",
                                                                                 "fix")} for c in e["checks"]]}
                                              for e in entries], "problems": problems})
        return 1 if problems else 0
    if not entries:
        where = f"to {_tilde(repo)}" if repo else "to any identity (set policy = \"~/…\" in an identity, or pass " \
            "--policy FILE)"
        print(f"no policy applies {where}")
        return 0
    lines = _render_machine(entries)
    lines.append(f"{problems} problem{'s' if problems != 1 else ''}" if problems else "✓ this machine meets the policy")
    print("\n".join(lines))
    return 1 if problems else 0


def _cmd_check(args) -> int:
    resolve.require_git()
    if args.range is not None:
        return _cmd_range(args)
    return _cmd_machine(args)


# ---------------------------------------------------------------- resolve.EXTRA_CHECKS

def check_report(report) -> List[resolve.Problem]:
    """The identity's own policy against what resolve expects (email, signing); code "policy". Only the
    identity's `policy` is visible here (the callback gets the Report, not the config)."""
    ident = report.expected.match.identity
    if ident is None or not getattr(ident, "policy", None):
        return []
    try:
        pol = load(ident.policy, who=f"identity {ident.id}")
    except ConfigError as e:
        return [resolve.Problem("policy", str(e), None)]
    urls = _repo_urls(report.effective.remotes, report.expected.match.url)
    rules = [r for r in pol.rules if any(rule_matches(r, u) for u in urls)]
    if not rules:
        return []
    out: List[resolve.Problem] = []
    prob = email_problem(rules, report.expected.email)
    if prob:
        doms = _domains(rules)
        out.append(resolve.Problem("policy", f"policy {pol.name}: identity {ident.id} email {report.expected.email}: "
                                             f"{prob[1]}",
                                   f"use an address in {', '.join(doms)}" if doms else None))
    if any(r.require_signing for r in rules) and not report.expected.sign:
        out.append(resolve.Problem("policy", f"policy {pol.name} requires signed commits but identity {ident.id} has "
                                             f"sign = false", f"set sign = true, run {PROG} apply"))
    return out


resolve.EXTRA_CHECKS[:] = [f for f in resolve.EXTRA_CHECKS
                           if not (getattr(f, "__module__", None) == __name__
                                   and getattr(f, "__qualname__", None) == "check_report")]
resolve.EXTRA_CHECKS.append(check_report)


# ---------------------------------------------------------------- guard entries (01 §5.6)

def _hook_found(ctx, url: Optional[str] = None) -> Found:
    """The policies for the hook's repo; [] at once (no git, no file read) when the config names none."""
    cfg = ctx.cfg
    if not any_policy(cfg):
        return []
    rep = getattr(ctx, "report", None)
    if rep is not None:
        ident = rep.expected.match.identity
        urls = [url] if url else _repo_urls(rep.effective.remotes, rep.expected.match.url)
    else:
        exp = resolve.expected(Path(ctx.repo), cfg)
        ident = exp.match.identity
        urls = [url] if url else _repo_urls(resolve.remotes(Path(ctx.repo)), exp.match.url)
    if not urls:
        return []
    srcs = sources(cfg, ident)
    if not srcs:
        return []
    return match(_load_all(srcs, check_perms=True), urls, cfg)


def _block(ctx, pol: Policy, head: str, lines: Sequence[str], fix: str) -> int:
    warn = getattr(getattr(ctx.cfg, "guard", None), "on_mismatch", "block") != "block"
    ctx.say(("⚠" if warn else "✗") + f" gitpro: {head}")
    for line in lines:
        ctx.say("  " + line)
    ctx.say(f"  cause: {pol.cause()}")
    if fix:
        ctx.say(f"  fix:   {fix}")
    if warn:
        ctx.say('  (not blocking: [guard] on_mismatch = "warn")')
    return BLOCK


def _safe(ctx, fn) -> int:
    try:
        return fn(ctx)
    except KeyboardInterrupt:
        raise
    except BaseException as e:   # noqa: BLE001 -- a broken policy must never block a commit
        msg = str(e) if isinstance(e, ConfigError) else (f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
        try:
            ctx.say(f"⚠ gitpro policy: {msg}; not blocking")
        except Exception:   # noqa: BLE001
            pass
        return 0


def _email_fix(rules: Sequence[Rule], key: str) -> str:
    doms = _domains(rules)
    if key == "allowed_email_domains" and doms:
        return f"commit as an address in {', '.join(doms)} (git config user.email …)"
    return "commit as another address (git config user.email …)"


def _hook_commit(ctx) -> int:
    found = _hook_found(ctx)
    for pol, rules in found:
        for which, email in (("committed", ctx.committer_email), ("authored", ctx.author_email)):
            prob = email_problem(rules, email)
            if prob:
                return _block(ctx, pol, f"this commit would be {which} as {email}: {prob[1]}", (),
                              _email_fix(rules, prob[0]))
    return 0


def hook_commit(ctx) -> int:
    """pre-commit: the committer and author emails against the repo's policies. 3 to refuse, else 0."""
    return _safe(ctx, _hook_commit)


def _outgoing(ctx, signing: bool, pre: Sequence[str]) -> List[_Commit]:
    """The commits this push sends that no remote-tracking ref has (the listing guard uses)."""
    seen, out = set(), []
    for line in (ctx.stdin or "").splitlines():
        parts = line.split()
        if len(parts) != 4 or set(parts[1]) == {"0"}:
            continue
        local, remote = parts[1], parts[3]
        revs = [local]
        if set(remote) != {"0"}:
            try:
                _git(ctx.repo, "cat-file", "-e", f"{remote}^{{commit}}")
                revs.append(f"^{remote}")
            except ConfigError:
                pass
        for c in _log(ctx.repo, [*revs, "--not", "--remotes"], signing=signing, pre=pre, timeout=GIT_TIMEOUT):
            if c.sha not in seen:
                seen.add(c.sha)
                out.append(c)
    return out


def _hook_push(ctx) -> int:
    url = ctx.args[1] if len(ctx.args) > 1 else None
    found = _hook_found(ctx, url)
    if not found:
        return 0
    signing = any(r.signing() for _, rules in found for r in rules)
    tmp: List[str] = []
    try:
        pre, ships = _signers_args(found, tmp) if signing else ([], False)
        commits = _outgoing(ctx, signing, pre)
    finally:
        for t in tmp:
            try:
                os.unlink(t)
            except OSError:
                pass
    for pol, rules in found:
        bad = []
        for c in commits:
            for v in _judge(c, [(pol, rules)], signing, ships):
                bad.append(f"{c.sha[:7]} {c.author_email} {v['message']}")
        if bad:
            n = len(bad)
            shown = bad[:5] + ([f"… and {n - 5} more"] if n > 5 else [])
            return _block(ctx, pol, f"this push breaks policy {pol.name} ({n} problem{'s' if n != 1 else ''})", shown,
                          "rewrite the listed commits (git rebase -i; sign with git commit --amend -S), then push")
    return 0


def hook_push(ctx) -> int:
    """pre-push: every outgoing commit against the remote's policies (emails, trailers, and %G? when a rule
    requires signing). 3 to refuse, else 0."""
    return _safe(ctx, _hook_push)


def _in_merge() -> bool:
    fn = getattr(trailers, "_in_merge", None)
    return bool(fn()) if fn else False


def _hook_message(ctx) -> int:
    if not any_policy(ctx.cfg):
        return 0
    if not ctx.args:
        raise ConfigError("commit-msg called without a message file")
    found = _hook_found(ctx)
    if not found:
        return 0
    msg = Path(ctx.args[0])
    if not msg.is_absolute() and not msg.exists():
        msg = Path(ctx.repo) / msg
    parsed = trailers.parse(msg)
    merge = _in_merge()
    for pol, rules in found:
        probs = trailer_problems(pol, rules, parsed, merge)
        if probs:
            head, rest = probs[0][1], [f"also: {m}" for _, m in probs[1:]]
            fix = "remove the trailer from the message" if not any(
                "requires" in m for _, m in probs) else "end the message with a trailer line: Co-Authored-By: Name <email>"
            return _block(ctx, pol, head, rest, fix)
    return 0


def hook_message(ctx) -> int:
    """commit-msg: the message's trailers against the repo's policies. 3 to refuse, else 0."""
    return _safe(ctx, _hook_message)


# ---------------------------------------------------------------- team

def _optional(name: str):
    import importlib
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _registered(cfg, home: Path) -> Dict[str, Optional[bool]]:
    """pub path -> registered as a signing key (09 health.check_keys --online); {} without health. The
    caller has announced already (the forge session's gate), so check_keys prints nothing."""
    health = _optional("gitpro.health")
    if health is None or not hasattr(health, "check_keys"):
        return {}
    out: Dict[str, Optional[bool]] = {}
    data = health.check_keys(cfg, online=True, announce=False, home=home)
    for k in data.get("keys", []):
        reg = k.get("registered")
        pub = k.get("pub") or (str(k.get("path")) + ".pub")
        out[os.path.expanduser(str(pub))] = None if not isinstance(reg, dict) or reg.get("signing") is None \
            else bool(reg.get("signing"))
    return out


TEAM_WHAT = "which SSH keys are registered on your accounts"
TEAM_WHAT_EMAILS = TEAM_WHAT + " and which of your emails they have verified"
TEAM_SENDS = "only the list requests; keys and emails are compared locally"


def _session_ids(cfg) -> List[str]:
    """The forge targets of `team --online`: [default] when it has an account (health.check_keys asks it
    too), then every identity."""
    d = getattr(cfg, "default", None)
    return (["default"] if getattr(d, "gh_account", None) or getattr(d, "glab_account", None) else []) + \
        [i.id for i in cfg.identities]


def _mentions(text: str, word: str) -> bool:
    return bool(word) and re.search(rf"(?<![\w.-]){re.escape(word)}(?![\w-])", text) is not None


def _verified(cfg, home: Path, as_json: bool, session: object = None, why: Optional[List[str]] = None,
              reasons: Optional[Dict[str, str]] = None) -> Dict[str, Optional[bool]]:
    """casefolded email -> verified on the forge: True / False / None (unknown; an email missing from the
    map is unknown too). session: the gated forge.Session of the run, else one is made and gated here.
    A failure is kept as unknown, its reason appended to why (and warned through the session). reasons
    gets, per casefolded email and per identity id left unknown, the forge warnings that name its account
    or identity."""
    from . import forge
    make, emails = getattr(forge, "Session", None), getattr(forge, "emails", None)
    if make is None or emails is None:
        if why is not None:
            why.append("this gitpro has no forge support")
        return {}
    s = session
    try:
        if s is None:
            s = make(cfg, home, _session_ids(cfg), as_json=as_json)
            if hasattr(s, "gate"):
                rc = s.gate("team", TEAM_WHAT_EMAILS, TEAM_SENDS, explicit=True,
                            args=argparse.Namespace(json=as_json, command="team"))
                if rc is not None:
                    raise ConfigError("the forge could not be asked")
        seen = len(getattr(s, "warnings", None) or [])
        got = emails(s) or ()
        new = list(getattr(s, "warnings", None) or [])[seen:]
    except (ConfigError, OSError, subprocess.SubprocessError, UnicodeError, ValueError) as e:
        msg = f"verified email: could not ask the forge: {e}"
        if why is not None:
            why.append(str(e))
        if hasattr(s, "warn"):
            s.warn(msg)
        else:
            print(f"⚠ {msg}", file=sys.stderr)
        return {}
    out: Dict[str, Optional[bool]] = {}
    for e in got:
        if isinstance(e, dict) and e.get("email"):
            v = e.get("verified")
            key = str(e["email"]).casefold()
            if v is True or v is False:
                out[key] = False if out.get(key) is False else v     # two accounts disagree: not verified
            else:
                out.setdefault(key, None)
                said = [w for w in new if _mentions(w, str(e.get("account") or "")) or _mentions(w, str(e.get("id") or ""))]
                if said and reasons is not None:
                    reasons.setdefault(key, "; ".join(said))
    if reasons is not None:
        for i in getattr(cfg, "identities", ()) or ():
            said = [w for w in new if _mentions(w, i.id)]
            if said and (i.email or "").casefold() not in out:
                reasons.setdefault(i.id, "; ".join(said))
    return out


def _worst(checks: Sequence[dict], *names: str) -> Optional[str]:
    levels = [c["level"] for c in checks if c["name"] in names]
    for lv in ("error", "warning", "ok"):
        if lv in levels:
            return lv
    return None


def _cell(level: Optional[str], online: bool = True) -> str:
    if level is None:
        return "·"
    if level == "warning" and not online:
        return "⚠ (--online)"
    return _MARK[level]


def _needs_verified(cfg) -> bool:
    """Does any rule that applies to an identity require a verified email? (Unreadable policies count as no:
    the rows report them.)"""
    for ident in cfg.identities:
        for path, who in sources(cfg, ident):
            try:
                pol = load(path, who=who)
            except ConfigError:
                continue
            if any(r.require_verified_email and _overlaps(r, ident) for r in pol.rules):
                return True
    return False


def team_data(cfg, home: Path, *, online: bool = False, as_json: bool = False,
              explicit_config: Optional[Path] = None, session: object = None,
              needs_verified: Optional[bool] = None) -> dict:
    """The rows of `team`. online: session is the gated forge.Session (cmd_team opens it)."""
    keys = _registered(cfg, home) if online else None
    if needs_verified is None:
        needs_verified = online and _needs_verified(cfg)
    why: List[str] = []
    reasons: Dict[str, str] = {}
    verified = _verified(cfg, home, as_json, session, why, reasons) if online and needs_verified else None
    ids = []
    for ident in cfg.identities:
        pols = _load_all(sources(cfg, ident), check_perms=True)
        found = [(p, [r for r in p.rules if _overlaps(r, ident)]) for p in pols]
        found = [(p, rules) for p, rules in found if rules]
        person = _person(cfg, ident)
        checks = [c for p, rules in found
                  for c in identity_checks(cfg, person, p, rules, keys, online, verified,
                                           reasons.get((person.email or "").casefold()) or reasons.get(ident.id)
                                           or "; ".join(why))]
        ids.append({"id": ident.id, "policy": ", ".join(p.name for p, _ in found) or None,
                    "policies": [{"name": p.name, "file": p.path, "contact": p.contact} for p, _ in found],
                    "ok": not any(c["level"] == "error" for c in checks),
                    "checks": [{k: c[k] for k in ("name", "ok", "level", "message", "fix")} for c in checks]})
    guard = _optional("gitpro.guard")
    agents = _optional("gitpro.agents")
    g = a = None
    if guard is not None and hasattr(guard, "status"):
        try:
            g = guard.status(home)
        except Exception as e:   # noqa: BLE001
            g = {"installed": "none", "error": str(e)}
    if agents is not None and hasattr(agents, "status_data"):
        try:
            a = agents.status_data(home, explicit_config)
        except Exception as e:   # noqa: BLE001
            a = {"error": str(e)}
    return {"ok": all(i["ok"] for i in ids), "identities": ids, "guard": g, "agents": a,
            "warnings": list(getattr(session, "warnings", None) or [])}


def _shell_path(path: str) -> str:
    """path for a command line the user can paste: ~/rest when under home, quoted where needed."""
    import shlex
    shown = _tilde(path)
    return "~/" + shlex.quote(shown[2:]) if shown.startswith("~/") else shlex.quote(shown)


def check_hint(ids: Sequence[dict]) -> str:
    """The command that shows the details of failing identities: `gitpro check --policy FILE` per policy
    file they name (machine mode, every identity with a policy)."""
    files = list(dict.fromkeys(p["file"] for i in ids if not i["ok"] for p in i.get("policies") or () if p.get("file")))
    return " ".join([PROG, "check"] + [f"--policy {_shell_path(f)}" for f in files])


def _render_team(data: dict, online: bool) -> List[str]:
    ids = data["identities"]
    w = max([len("identity")] + [len(i["id"]) for i in ids]) + 2
    lines = ["team status (this machine)",
             f"  {'identity':<{w}}{'policy':<29}{'email':<7}{'signing':<9}{'key registered':<16}"
             f"{'verified':<14}trailers"]
    for i in ids:
        if not i["policy"]:
            lines.append(f"  {i['id']:<{w}}{'(none)':<29}{'·':<7}{'·':<9}{'·':<16}{'·':<14}·")
            continue
        name = i["policy"] if len(i["policy"]) <= 27 else i["policy"][:26] + "…"
        c = i["checks"]
        lines.append(f"  {i['id']:<{w}}{name:<29}{_cell(_worst(c, 'email domain')):<7}"
                     f"{_cell(_worst(c, 'signing')):<9}{_cell(_worst(c, 'signing key'), online):<16}"
                     f"{_cell(_worst(c, 'verified email'), online):<14}{_cell(_worst(c, 'trailers'))}")
    g = data["guard"]
    if g is None:
        guard = "guard: · not installed"
    elif g.get("installed", "none") != "none":
        guard = f"guard: installed ({g.get('on_mismatch', 'block')})"
    else:
        guard = "guard: not installed"
    a = data["agents"]
    if a is None:
        agent = "agent hook: · not installed"
    else:
        marks = []
        for name, v in (a.get("agents") or {}).items():
            on = isinstance(v, dict) and any(v.get(k) for k in ("user", "project", "plugin"))
            marks.append(f"{name} {'✓' if on else '·'}")
        agent = "agent hook: " + (" ".join(marks) or "·")
    lines.append(f"{guard:<29}{agent}")
    bad = sum(1 for i in ids if not i["ok"])
    if bad:
        lines.append(f"{bad} identit{'y violates its' if bad == 1 else 'ies violate their'} policy → "
                     f"{check_hint(ids)}")
    elif any(i["policy"] for i in ids):
        lines.append("✓ every identity meets its policy")
    else:
        lines.append("no identity has a policy (set policy = \"~/…\" in an identity)")
    return lines


def cmd_team(args) -> int:
    from . import jsonout
    resolve.require_git()
    home = Path(args.home)
    path, how = config.find(args.config, home)
    cfg = config.load_all(path, how)
    as_json = getattr(args, "json", False)
    session = None
    needs = False
    if args.online:
        from . import forge
        if not (forge.available("gh") or forge.available("glab")):
            raise ConfigError("team --online needs gh or glab on PATH (signed in) to ask the forge which keys and "
                              "emails are registered")
        # one announce line for every forge call of this run (key lists, verified emails): first on stdout,
        # on stderr under --json, through the session's gate
        needs = _needs_verified(cfg)
        session = forge.Session(cfg, home, _session_ids(cfg), as_json=as_json)
        rc = session.gate("team", TEAM_WHAT_EMAILS if needs else TEAM_WHAT, TEAM_SENDS, explicit=True, args=args)
        if rc is not None:
            return rc
    data = team_data(cfg, home, online=args.online, as_json=as_json, explicit_config=args.config,
                     session=session, needs_verified=needs)
    if getattr(args, "json", False):
        jsonout.emit("team", data)
    else:
        print("\n".join(_render_team(data, args.online)))
    return 0 if data["ok"] else 1


# ---------------------------------------------------------------- policy show / lint

def _show_lines(found: Found) -> List[str]:
    lines: List[str] = []
    for pol, rules in found:
        lines.append(f"policy {pol.name} ({_tilde(pol.path)})" + (f", contact {pol.contact}" if pol.contact else ""))
        for r in rules:
            lines.append(f"  rule {r.index}: {r.host}/{', '.join(r.owners) if r.owners else '*'}")
            if r.emails():
                lines.append(f"    email domains   {', '.join(r.allowed_email_domains) or '(none)'}"
                             + (f"; also {', '.join(r.allowed_emails)}" if r.allowed_emails else ""))
            if r.signing():
                lines.append(f"    signing         {'required' if r.require_signing else 'optional'}"
                             + (f" ({', '.join(r.signing_formats)})" if r.signing_formats else ""))
            lines.append(f"    co_authored_by  {r.co_authored_by}")
            if r.forbid_emails:
                lines.append(f"    forbid_emails   {', '.join(r.forbid_emails)}")
            if r.require_verified_email:
                lines.append("    verified email  required (checked by gitpro team --online)")
    return lines


def cmd_show(args) -> int:
    from . import jsonout
    resolve.require_git()
    repo = _need_repo(args.dir)
    cfg = _config_or_none(args)
    found: Found = applicable(repo, cfg) if cfg is not None else []
    if not found and (repo / REPO_POLICY).is_file():
        found = match([load(repo / REPO_POLICY, check_perms=False, inside=repo)], _repo_urls(resolve.remotes(repo)),
                      cfg)
    if getattr(args, "json", False):
        jsonout.emit("policy", {"repo": str(repo), "policies": [
            {"name": p.name, "file": p.path, "version": p.version, "contact": p.contact,
             "allowed_signers": p.allowed_signers, "rules": [r.to_json() for r in rules]} for p, rules in found]})
        return 0
    if not found:
        print(f"no policy applies to {_tilde(repo)}")
        return 0
    print("\n".join(_show_lines(found)))
    return 0


def cmd_lint(args) -> int:
    from . import jsonout
    pol, errs = lint(args.file)
    if getattr(args, "json", False):
        jsonout.emit("policy", {"ok": not errs, "file": str(args.file), "errors": errs,
                                "name": pol.name if pol else None, "rules": len(pol.rules) if pol else None})
        return 2 if errs else 0
    if errs:
        for e in errs:
            print(f"✗ {e}", file=sys.stderr)
        return 2
    assert pol is not None
    print(f"✓ {_tilde(args.file)}: policy {pol.name}, {len(pol.rules)} rule{'s' if len(pol.rules) != 1 else ''}")
    return 0


# ---------------------------------------------------------------- doctor

def doctor_section(ctx) -> dict:
    """16's policy row: does this machine meet the policies its config names?"""
    cfg = ctx.need_cfg()
    if not any_policy(cfg):
        return {"status": "ok", "summary": "no policy configured", "problems": [], "details": []}
    probs = check_machine(cfg)
    return {"status": "error" if probs else "ok",
            "summary": probs[0].message if probs else "machine meets the team policy",
            "problems": [{"message": p.message, "fix": p.fix or "", "level": "error"} for p in probs],
            "details": []}


# ---------------------------------------------------------------- CLI

def add_check_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--policy", dest="policy_files", action="append", metavar="FILE",
                   help="check against a team policy file (repeatable; default: the policies the config names)")
    p.add_argument("--range", metavar="A..B", help="CI mode: check the commits of this range against the policy")
    p.add_argument("--repo", metavar="DIR", help="the repo to check (default: the current directory)")


def check_handler(args) -> Optional[int]:
    if not (getattr(args, "policy_files", None) or getattr(args, "range", None) is not None
            or getattr(args, "repo", None)):
        return None
    return _cmd_check(args)


def register(sub, common, confirm) -> None:
    from .args import capability
    sp = sub.add_parser("team", parents=[common], help="does this machine meet each identity's team policy?")
    sp.add_argument("--online", action="store_true", help="also ask gh/glab which keys and emails are registered")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_team)
    pp = sub.add_parser("policy", help="show or lint team policy files")
    ps = pp.add_subparsers(dest="policy_command", metavar="ACTION")
    ps.required = True
    s = ps.add_parser("show", parents=[common], help="the policy rules that apply to a repo")
    s.add_argument("dir", nargs="?", default=".", help="repo directory (default: .)")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_show)
    s = ps.add_parser("lint", help="validate a policy file (every error, no permission check)")
    s.add_argument("file", type=Path, help="the policy TOML")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_lint)
    capability("policy", "team")


def extend(subs) -> None:
    from .args import capability
    from .cli import extend_command
    extend_command(subs, "check", add_check_flags, check_handler)
    capability("check.policy")
