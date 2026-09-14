"""Key health (`keys --health`), `gitpro auth` (ssh -T per host and alias) and `keys --upload`.

The private key is never opened by gitpro: it is only stat()ed, and `ssh-keygen -y -P ""` (stdin
/dev/null, 5 s timeout, stdout discarded) is the one passphrase probe. Public keys are read for their
type, fingerprint (`ssh-keygen -l`) and material. The network is used only by `auth` (ssh -T, its whole
purpose) and, with --online, through forge.run (gh/glab, token in the child's env only).

doctor (16) calls check_keys(cfg, online=..., announce=False) and auth(cfg, identities, timeout); both
return a dict with status/summary/problems/details next to their data, and print nothing.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import config, jsonout
from .args import capability, fail
from .config import ConfigError

PROBE_TIMEOUT = 5      # ssh-keygen -y, ssh-add -l
LOCAL_TIMEOUT = 10     # ssh-keygen -l
AUTH_TIMEOUT = 30      # ssh -T per host
GITHUB, GITLAB = "github.com", "gitlab.com"
SYM = {"ok": "✓", "info": "·", "warning": "⚠", "error": "✗"}
NEEDS_CLI = "needs gh or glab on PATH: install gh (https://cli.github.com) or glab (https://gitlab.com/gitlab-org/cli)"
ANNOUNCE_WHAT = "which SSH keys are registered on your accounts"
ANNOUNCE_SENDS = "only the list request; keys are compared locally"
_KEYTYPE_RE = re.compile(r"^(ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9]+|sk-[A-Za-z0-9.@-]+)$")
_FP_RE = re.compile(r"^(\d+)\s+(\S+)\s+(.*?)\s*\(([A-Za-z0-9-]+)\)\s*$")


# ---------------------------------------------------------------- small helpers

def _expand(p: str, home: Path) -> Path:
    return home / p[2:] if p.startswith("~/") else Path(p)


def _pub(key: Path) -> Path:
    return key.with_name(key.name + ".pub")


def _tilde(path: object, home: Path) -> str:
    p, h = str(path), str(home)
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def _sh(shown: str) -> str:
    """Shell-quote a displayed path, keeping a leading ~/."""
    return "~/" + shlex.quote(shown[2:]) if shown.startswith("~/") else shlex.quote(shown)


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _run(argv: List[str], timeout: float) -> Optional[subprocess.CompletedProcess]:
    """Captured text run with stdin /dev/null; None when the program is missing, rc 124 on timeout."""
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout:g} s")


def _check(level: str, message: str, fix: str = "", tag: str = "") -> dict:
    return {"level": level, "message": message, "fix": fix, "tag": tag}


# ---------------------------------------------------------------- local probes

def material(pub: Path) -> Optional[Tuple[str, str]]:
    """(type, base64) of a public key file, ignoring the comment; None when unreadable."""
    words = _pub_words(pub)
    return _material_in(words) if words is not None else None


def _pub_words(pub: Path) -> Optional[List[str]]:
    """The words of a public key file; None unless it is a readable regular file (a FIFO would hang)."""
    try:
        if not pub.is_file():
            return None
        return pub.read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return None


def _material_in(words: Sequence[str]) -> Optional[Tuple[str, str]]:
    for n, w in enumerate(words[:-1]):
        if _KEYTYPE_RE.match(w):
            return w, words[n + 1]
    return None


def key_comment(pub: Path) -> str:
    words = _pub_words(pub)
    if words is None:
        return ""
    for n, w in enumerate(words[:-1]):
        if _KEYTYPE_RE.match(w):
            return " ".join(words[n + 2:])
    return ""


def fingerprint(pub: Path) -> Tuple[Optional[int], Optional[str], Optional[str], str]:
    """(bits, fingerprint, TYPE, error) from `ssh-keygen -l -f <pub>`."""
    r = _run(["ssh-keygen", "-l", "-f", str(pub)], LOCAL_TIMEOUT)
    if r is None:
        return None, None, None, "ssh-keygen not found"
    m = _FP_RE.match(_first_line(r.stdout))
    if r.returncode != 0 or not m:
        return None, None, None, _first_line(r.stderr) or f"ssh-keygen -l exited {r.returncode}"
    return int(m[1]), m[2], m[4].upper(), ""


def classify(ktype: str, bits: int) -> Tuple[str, Optional[dict]]:
    """(type name for JSON, the type/size check or None when fine)."""
    t = ktype.upper()
    if t.endswith("-SK"):
        return t.lower(), _check("info", "hardware key", tag="hardware")
    if t == "ED25519":
        return "ed25519", None
    if t == "RSA":
        if bits < 2048:
            return "rsa", _check("error", f"RSA {bits} is too weak; generate a new key",
                                 "gitpro keys --generate after changing ssh_key", "weak")
        if bits < 3072:
            return "rsa", _check("warning", f"RSA {bits} is below the recommended 3072 bits; ed25519 is better",
                                 "gitpro keys --generate after changing ssh_key", "rsa-2048")
        return "rsa", None
    if t == "DSA":
        return "dsa", _check("error", "DSA is too weak (current OpenSSH refuses it); generate a new key",
                             "gitpro keys --generate after changing ssh_key", "weak")
    if t == "ECDSA":
        name = f"ecdsa-{bits}"
        return name, _check("warning", f"{name}: ed25519 is preferred", tag="ecdsa")
    return t.lower(), None


def probe_passphrase(private: Path) -> Tuple[Optional[bool], str]:
    """(True: has a passphrase, False: none, None: unknown, why). The private key is read by ssh-keygen
    only; stdin is /dev/null so it can never prompt, and its stdout (the public key) is discarded."""
    r = _run(["ssh-keygen", "-y", "-P", "", "-f", str(private)], PROBE_TIMEOUT)
    if r is None:
        return None, "ssh-keygen not found"
    if r.returncode == 0:
        return False, ""
    if "incorrect passphrase" in r.stderr.lower():
        return True, ""
    return None, _first_line(r.stderr) or f"ssh-keygen -y exited {r.returncode}"


def agent_fingerprints() -> Optional[set]:
    """Fingerprints `ssh-add -l` lists; None when there is no agent."""
    r = _run(["ssh-add", "-l"], PROBE_TIMEOUT)
    if r is None or r.returncode not in (0, 1):
        return None
    return {ln.split()[1] for ln in r.stdout.splitlines() if len(ln.split()) >= 2 and ln.split()[0].isdigit()}


def _mode_problems(private: Path, pub: Path, home: Path) -> List[dict]:
    out = []
    try:
        st = private.stat()
    except OSError:
        return out
    shown = _tilde(private, home)
    if st.st_mode & 0o077:
        out.append(_check("error", f"accessible by others ({st.st_mode & 0o777:04o})", f"chmod 600 {_sh(shown)}",
                          "perms"))
    if st.st_uid != os.getuid():
        out.append(_check("error", "owned by another user; ssh refuses it", _chown_fix(private, home), "perms"))
    why = pub_unsafe(pub)
    if why:
        ps = _tilde(pub, home)
        fix = _chown_fix(pub, home) if "owned" in why else f"chmod 644 {_sh(ps)}"
        out.append(_check("error" if "everyone" in why else "warning", f"{ps} {why}", fix, "pub-perms"))
    return out


def pub_unsafe(pub: Path) -> Optional[str]:
    """Why a .pub must not be uploaded as is (writable by others or owned by another user); None when fine."""
    try:
        st = pub.stat()
    except OSError:
        return None
    if st.st_mode & 0o002:
        return "is writable by everyone"
    if st.st_mode & 0o020:
        return "is writable by its group"
    if st.st_uid != os.getuid():
        return "is owned by another user"
    return None


def _chown_fix(path: Path, home: Path) -> str:
    """chown on the real file (never through a symlink); it belongs to someone else, so sudo."""
    import pwd
    try:
        me = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        me = str(os.getuid())
    real = _tilde(path.resolve(), home)
    return f"sudo chown {me} {_sh(real)} (check first why another user owns it)"


def ssh_dir_check(home: Path) -> Tuple[Optional[str], Optional[dict]]:
    """("0700", None) or the mode and its check for ~/.ssh."""
    try:
        mode = (home / ".ssh").stat().st_mode & 0o777
    except OSError:
        return None, None
    if mode & 0o022:
        return f"{mode:04o}", _check("error", f"~/.ssh is writable by others ({mode:04o})", "chmod 700 ~/.ssh",
                                     "perms")
    if mode & 0o077:
        return f"{mode:04o}", _check("warning", "~/.ssh is readable by others; ssh accepts it, 0700 is safer",
                                     "chmod 700 ~/.ssh", "ssh-dir")
    return f"{mode:04o}", None


# ---------------------------------------------------------------- who uses which key

@dataclass
class Use:
    who: str                   # identity id, or "default"
    host: str
    accounts: list             # forge.Account
    auth: bool
    sign: bool


def _accounts_of(host: str, gh: Optional[str], glab: Optional[str]) -> list:
    from . import forge
    out = []
    if gh:
        out.append(forge.Account(host, "gh", gh))
    if glab:
        out.append(forge.Account(host, "glab", glab))
    return out


def uses_by_key(cfg, home: Path) -> Dict[Path, List[Use]]:
    """Every private key path → the identities that use it, with the forge accounts they map to."""
    d = cfg.default
    out: Dict[Path, List[Use]] = {}
    if d.ssh_key:
        accs = _accounts_of(GITHUB, d.gh_account, None) + _accounts_of(GITLAB, None, d.glab_account)
        out.setdefault(_expand(d.ssh_key, home), []).append(
            Use("default", ", ".join(d.ssh_hosts) or "any host", accs, True, d.sign))
    for i in cfg.identities:
        key = i.ssh_key or (d.ssh_key if config.signs(i, d) else None)
        if key:
            out.setdefault(_expand(key, home), []).append(
                Use(i.id, i.host, _accounts_of(i.host, i.gh_account, i.glab_account), bool(i.ssh_key),
                    config.signs(i, d)))
    return out


def _forge_name(host: str, cli: str = "") -> str:
    if host == GITHUB or cli == "gh":
        return "GitHub"
    if host == GITLAB or cli == "glab":
        return "GitLab"
    return host


def shared_checks(cfg, home: Path) -> Dict[Path, List[dict]]:
    """⚠ for a key that two identities on one host use with different forge accounts."""
    out: Dict[Path, List[dict]] = {}
    groups: Dict[Tuple[Path, str], list] = {}
    for i in cfg.identities:
        if i.ssh_key:
            groups.setdefault((_expand(i.ssh_key, home), i.host), []).append(i)
    for (key, host), ids in groups.items():
        for attr, cli in (("gh_account", "gh"), ("glab_account", "glab")):
            logins = {getattr(i, attr) for i in ids if getattr(i, attr)}
            if len(logins) > 1:
                who = " and ".join(i.id for i in ids if getattr(i, attr))
                out.setdefault(key, []).append(_check(
                    "warning", f"shared by {who} on {host}; {_forge_name(host, cli)} allows a key on one account only",
                    "give each identity its own ssh_key", "shared"))
    return out


# ---------------------------------------------------------------- forge listings (online)

@dataclass
class Entry:
    material: Tuple[str, str]
    kinds: set                 # {"auth", "signing"}
    id: Optional[str] = None


@dataclass
class Listing:
    entries: List[Entry] = field(default_factory=list)
    signing_known: bool = True
    error: Optional[str] = None

    def found(self, mat: Tuple[str, str]) -> List[Entry]:
        return [e for e in self.entries if e.material == mat]


def parse_gh_keys(text: str) -> Listing:
    """`gh ssh-key list` (not a terminal: title, key, added, id, type; older gh: no id/type columns)."""
    out, typed = [], False
    for line in text.splitlines():
        cols = [c.strip() for c in (line.split("\t") if "\t" in line else line.split())]
        mat = _material_in(" ".join(cols).split())
        if not mat:
            continue
        kind = next((c.lower() for c in cols if c.lower() in ("authentication", "signing")), None)
        typed |= kind is not None
        kid = cols[3] if len(cols) >= 4 and cols[3].isdigit() else next((c for c in cols[1:] if c.isdigit()), None)
        out.append(Entry(mat, {"signing"} if kind == "signing" else {"auth"}, kid))
    return Listing(out, typed or not out)


_USAGE = {"auth": {"auth"}, "signing": {"signing"}, "auth_and_signing": {"auth", "signing"}}


def parse_glab_keys(text: str) -> Optional[Listing]:
    """`glab ssh-key list --output json`; None when the text is not that JSON."""
    import json
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    out, known = [], True
    for k in data:
        if not isinstance(k, dict):
            continue
        mat = _material_in(str(k.get("key") or "").split())
        if not mat:
            continue
        usage = k.get("usage_type")
        known &= usage in _USAGE
        out.append(Entry(mat, set(_USAGE.get(usage, {"auth"})), str(k["id"]) if k.get("id") is not None else None))
    return Listing(out, known)


def _scope_hint(acc, scopes: Sequence[str]) -> str:
    flags = " ".join(f"-s {s}" for s in scopes)
    return f"gh auth refresh -h {acc.host} {flags}"


def _is_scope_error(stderr: str) -> bool:
    return bool(re.search(r"HTTP 40[34]", stderr)) and ("scope" in stderr.lower() or "public_key" in stderr)


class Lister:
    """Key listings per forge account, cached for one run."""

    def __init__(self):
        self.cache: dict = {}

    def get(self, acc) -> Listing:
        if acc not in self.cache:
            self.cache[acc] = self._list(acc)
        return self.cache[acc]

    def forget(self, acc) -> None:
        self.cache.pop(acc, None)

    @staticmethod
    def _list(acc) -> Listing:
        from . import forge
        if acc.cli == "glab":
            wrong = glab_login_error(acc)
            if wrong:
                return Listing(error=wrong)
        try:
            if acc.cli == "gh":
                r = forge.run(acc, ["ssh-key", "list"])
            else:
                r = forge.run(acc, ["ssh-key", "list", "--output", "json"])
                got = parse_glab_keys(r.stdout) if r.returncode == 0 else None
                if got is not None:
                    return got
                if r.returncode != 127:
                    r = forge.run(acc, ["ssh-key", "list"])
        except ConfigError:
            return Listing(error=f"no {acc.cli} login for {acc.login} on {acc.host}; registered check skipped")
        if r.returncode == 127:
            return Listing(error=f"{acc.cli} not found; registered check skipped for {acc.host} ({acc.login})")
        if r.returncode != 0:
            if acc.cli == "gh" and _is_scope_error(r.stderr):
                return Listing(error=f"gh token for {acc.login} cannot list keys (read:public_key scope); run: "
                               + _scope_hint(acc, ("read:public_key", "read:ssh_signing_key")))
            return Listing(error=f"{acc.cli} ssh-key list failed for {acc.login} on {acc.host}: "
                           f"{_first_line(r.stderr) or 'exit ' + str(r.returncode)}")
        if acc.cli == "gh":
            return parse_gh_keys(r.stdout)
        text = parse_gh_keys(r.stdout)      # glab's text table: key material, no usage type
        return Listing(text.entries, False)


_GLAB_AS_RE = re.compile(r"Logged in to (\S+) as (\S+)", re.I)


def glab_login_error(acc) -> Optional[str]:
    """None when glab acts as acc.login on acc.host. glab has no per-login selection: every glab call acts
    as whoever `glab auth status` is logged in as, so another login must never be listed or uploaded to."""
    from . import forge
    r = forge.run(acc, ["auth", "status", "-h", acc.host])
    if r.returncode == 127:
        return None                         # the listing reports a missing glab itself
    text = f"{r.stdout}\n{r.stderr}"
    logins = [m[2].rstrip(".,") for m in _GLAB_AS_RE.finditer(text) if m[1].rstrip(".,:") == acc.host]
    if any(x.lower() == acc.login.lower() for x in logins):
        return None
    if not logins and forge.logged_in(acc):
        return None
    fix = f"glab auth login --hostname {acc.host}"
    if logins:
        return f"glab is logged in to {acc.host} as {logins[0]}, not {acc.login} → {fix}"
    return f"glab is not logged in to {acc.host} as {acc.login} → {fix}"


def _upload_hint(who: str) -> str:
    return "gitpro keys --upload --online" if who == "default" else f"gitpro keys --upload --identity {who} --online"


def _registration(mat: Tuple[str, str], uses: List[Use], lister: Lister, all_accounts: list) -> Tuple[list, list]:
    """(checks, registrations) of one key on every account its identities map to."""
    checks, regs, seen = [], [], {}
    for u in uses:
        for acc in u.accounts:
            w = seen.setdefault(acc, {"auth": False, "sign": False, "who": u.who})
            w["auth"] |= u.auth
            w["sign"] |= u.sign
    if not seen:
        return [_check("info", "registered: no gh_account/glab_account for this key; nothing to check online")], []
    for acc, want in seen.items():
        lst = lister.get(acc)
        where = f"{acc.host} ({acc.login})"
        if lst.error:
            checks.append(_check("warning", lst.error, tag="unchecked"))
            continue
        found = lst.found(mat)
        auth = any("auth" in e.kinds for e in found)
        signing = any("signing" in e.kinds for e in found) if lst.signing_known else None
        regs.append({"host": acc.host, "cli": acc.cli, "account": acc.login, "auth": auth, "signing": signing})
        hint = _upload_hint(want["who"])
        if not found:
            other = next((o for o in all_accounts if o != acc and o.host == acc.host and o.cli == acc.cli
                          and lister.get(o).found(mat)), None)
            if other is not None:
                checks.append(_check("warning", f"registered on {acc.host} ({other.login}) but identity {want['who']} "
                                     f"maps to {acc.login}", "give that identity its own key", "wrong-account"))
            else:
                checks.append(_check("error", f"not registered on {where}", hint, "unregistered"))
            if lst.signing_known or not want["sign"]:
                continue
        kinds = [k for k, v in (("auth", auth), ("signing", signing)) if v]
        if kinds:
            checks.append(_check("ok", f"registered on {where} as {' + '.join(kinds)}"))
        if want["auth"] and found and not auth:
            checks.append(_check("error", f"not registered on {where} as an auth key", hint, "unregistered"))
        if want["sign"] and signing is False and found:
            checks.append(_check("warning", f"not registered on {where} as a signing key; signed commits show "
                                 "as unverified", hint, "unregistered-signing"))
        if want["sign"] and signing is None:
            checks.append(_check("warning", f"{acc.cli} too old to list signing keys; signing registration on {where} "
                                 "unknown", f"upgrade {acc.cli}", "unchecked"))
    return checks, regs


# ---------------------------------------------------------------- keys --health

def _key_record(k, home: Path, uses: List[Use], agent: Optional[set], shared: List[dict], online: bool,
                lister: Optional[Lister], all_accounts: list) -> dict:
    private = _expand(k.path, home)
    pub = _pub(private)
    shown = _tilde(private, home)
    rec = {"path": str(private), "pub": str(pub), "exists": private.exists() and pub.exists(), "type": None,
           "bits": None, "fingerprint": None, "uses": list(k.uses), "sign": bool(k.sign), "passphrase": None,
           "perms_ok": None, "in_agent": None, "state": "ok", "weak": False, "registered": None, "problems": [],
           "fixes": [], "checks": []}
    checks: List[dict] = rec["checks"]
    if not private.exists():
        checks.append(_check("error", "missing", "gitpro keys --generate", "missing"))
    elif not pub.exists():
        ps = _tilde(pub, home)
        checks.append(_check("error", f"{ps} missing", f"ssh-keygen -y -f {_sh(shown)} > {_sh(ps)}", "missing"))
    if not rec["exists"]:
        rec["state"] = "missing"
        return _finish(rec)
    odd = [p for p in (private, pub) if not p.is_file()]
    if odd:     # a FIFO or device would hang ssh-keygen and the reads
        checks.extend(_check("error", f"{_tilde(p, home)} is not a regular file", tag="missing") for p in odd)
        rec["state"] = "missing"
        return _finish(rec)
    perms = _mode_problems(private, pub, home)
    rec["perms_ok"] = not any(c["level"] == "error" for c in perms)
    blocking = [c for c in perms if c["tag"] == "perms"]
    bits, fp, ktype, err = fingerprint(pub)
    if ktype is None:
        checks.append(_check("warning", f"could not read {_tilde(pub, home)}: {err}", tag="unreadable"))
    else:
        rec["type"], typecheck = classify(ktype, bits or 0)
        rec["bits"], rec["fingerprint"] = bits, fp
        if typecheck:
            checks.append(typecheck)
            rec["weak"] = typecheck["tag"] == "weak"
    checks.extend(perms)
    if not blocking:
        has, why = probe_passphrase(private)
        rec["passphrase"] = has
        if has is True:
            checks.append(_check("ok", "passphrase"))
        elif has is False and (rec["type"] or "").endswith("-sk"):
            checks.append(_check("info", "no passphrase (the hardware key guards it)"))
        elif has is False:
            checks.append(_check("warning", "no passphrase", f"ssh-keygen -p -f {_sh(shown)}", "no-passphrase"))
        else:
            checks.append(_check("warning", f"could not probe {shown}: {why}", tag="unprobed"))
        if not perms:
            checks.append(_check("ok", "perms"))
    if agent is not None and fp:
        rec["in_agent"] = fp in agent
        if rec["in_agent"]:
            checks.append(_check("info", "loaded in ssh-agent"))
        elif rec["passphrase"]:
            rec["state"] = "locked"
            checks.append(_check("warning", "has a passphrase and is not in ssh-agent: every git push will prompt, "
                                 "GUI clients and agents may fail", f"ssh-add {_sh(shown)}", "locked"))
        else:
            checks.append(_check("info", "not in ssh-agent"))
    checks.extend(shared)
    if online and lister is not None:
        mat = material(pub)
        if mat is None:
            checks.append(_check("warning", f"cannot read the key material of {_tilde(pub, home)}", tag="unchecked"))
        else:
            more, regs = _registration(mat, uses, lister, all_accounts)
            checks.extend(more)
            rec["registered"] = ({"auth": regs[0]["auth"], "signing": regs[0]["signing"],
                                  "account": regs[0]["account"], "host": regs[0]["host"]} if regs else None)
            rec["registrations"] = regs
    if rec["weak"]:
        rec["state"] = "weak"
    return _finish(rec)


def _finish(rec: dict) -> dict:
    for c in rec["checks"]:
        if c["level"] in ("warning", "error"):
            rec["problems"].append(c["message"])
            if c["fix"]:
                rec["fixes"].append(c["fix"])
    return rec


_TAG_TEXT = {"no-passphrase": "without passphrase", "weak": "weak", "locked": "locked", "missing": "missing",
             "perms": "with loose permissions", "pub-perms": "with loose permissions",
             "unregistered": "not registered", "unregistered-signing": "not registered for signing",
             "shared": "shared", "wrong-account": "on the wrong account"}


def check_keys(cfg, *, online: bool = False, announce: bool = True, home: Optional[Path] = None) -> dict:
    """Health of every configured key. With online, also asks gh/glab which keys each mapped account has
    (forge.announce first unless announce=False: doctor announces once itself). Prints nothing else."""
    from . import forge, keys
    home = Path(home) if home else Path.home()
    inv = [k for k in keys.inventory(cfg.default, list(cfg.identities)) if k.path]
    dir_mode, dir_check = ssh_dir_check(home)
    out = {"online": bool(online), "keys": [], "ssh_dir_mode": dir_mode, "agent": None, "general": [],
           "counts": {"fine": 0, "warnings": 0, "problems": 0}}
    if not inv:
        return {**out, "ok": True, "status": "ok", "summary": "no SSH keys configured", "problems": [],
                "details": []}
    if shutil.which("ssh-keygen") is None:
        msg = "ssh-keygen not found"
        return {**out, "ok": False, "status": "error", "summary": msg,
                "problems": [{"message": msg, "fix": "install OpenSSH", "level": "error"}], "details": []}
    general = out["general"]
    if dir_check:
        general.append(dir_check)
    agent = agent_fingerprints()
    out["agent"] = agent is not None
    if agent is None:
        general.append(_check("info", "no ssh-agent: a key with a passphrase prompts on every use"))
    all_accounts = forge.accounts(cfg) if online else []
    if online and announce and all_accounts:
        forge.announce(ANNOUNCE_WHAT, all_accounts, ANNOUNCE_SENDS)
    lister = Lister() if online else None
    uses, shared = uses_by_key(cfg, home), shared_checks(cfg, home)
    for k in inv:
        p = _expand(k.path, home)
        out["keys"].append(_key_record(k, home, uses.get(p, []), agent, shared.get(p, []), online, lister,
                                       all_accounts))
    return _summarise(out, home)


def _summarise(out: dict, home: Path) -> dict:
    problems, tally = [], {}
    for c in out["general"]:
        if c["level"] in ("warning", "error"):
            problems.append({"message": c["message"], "fix": c["fix"], "level": c["level"]})
    for rec in out["keys"]:
        bad = [c for c in rec["checks"] if c["level"] in ("warning", "error")]
        out["counts"]["fine"] += not bad
        for c in bad:
            problems.append({"message": f"{_tilde(rec['path'], home)}: {c['message']}", "fix": c["fix"],
                             "level": c["level"]})
            if c["tag"] in _TAG_TEXT:
                tally.setdefault(c["tag"], set()).add(rec["path"])
    out["counts"]["warnings"] = sum(p["level"] == "warning" for p in problems)
    out["counts"]["problems"] = sum(p["level"] == "error" for p in problems)
    parts = [f"{len(v)} {_TAG_TEXT[t]}" for t, v in tally.items()]
    n = len(out["keys"])
    summary = f"{_plural(n, 'key')} · " + (", ".join(parts) if parts else
                                            "all fine" if not problems else problems[0]["message"])
    status = "error" if out["counts"]["problems"] else "warning" if problems else "ok"
    details = [f"{_tilde(r['path'], home)}: {r['type'] or '?'}"
               f"{' ' + str(r['bits']) if r['type'] == 'rsa' else ''} · {r['fingerprint'] or '?'}"
               for r in out["keys"]]
    return {**out, "ok": not out["counts"]["problems"], "status": status, "summary": summary,
            "problems": problems, "details": details}


def render_health(data: dict, home: Path) -> str:
    lines = [_line(c) for c in data["general"]]
    width = max([len(_tilde(r["path"], home)) for r in data["keys"]] + [12])
    for r in data["keys"]:
        kind = "missing" if not r["exists"] else (r["type"] or "?") + (f" {r['bits']}" if r["type"] == "rsa" else "")
        role = "auth + signing" if r["sign"] else "auth"
        lines.append(f"{_tilde(r['path'], home):<{width}}  {kind:<8} {role}  →  {'; '.join(r['uses'])}")
        quiet = [c for c in r["checks"] if c["level"] in ("ok", "info")]
        lines += ["  " + _line(c) for c in r["checks"] if c["level"] in ("warning", "error")]
        if quiet:
            lines.append("  " + "  ".join(f"{SYM[c['level']]} {c['message']}" for c in quiet))
    if not data["online"]:
        lines.append("registered: run with --online to check via gh/glab")
    c = data["counts"]
    lines.append(f"{_plural(c['fine'], 'key')} fine, {_plural(c['warnings'], 'warning')}, "
                 f"{_plural(c['problems'], 'problem')}")
    return "\n".join(lines) + "\n"


def _line(c: dict) -> str:
    return f"{SYM[c['level']]} {c['message']}" + (f" → {c['fix']}" if c["fix"] else "")


def _health_json(data: dict) -> dict:
    keys = [{k: v for k, v in r.items()} for r in data["keys"]]
    for r in keys:
        r["checks"] = [{k: v for k, v in c.items() if k != "tag"} for c in r["checks"]]
    return {"ok": data["ok"], "online": data["online"], "keys": keys, "ssh_dir_mode": data["ssh_dir_mode"],
            "agent": data["agent"], "summary": data["counts"],
            "warnings": [p["message"] for p in data["problems"] if p["level"] == "warning"]}


# ---------------------------------------------------------------- gitpro auth

_GREETINGS = [
    ("gitea", re.compile(r"Hi there, (\S+?)!")),
    ("github", re.compile(r"Hi (\S+?)! You've successfully authenticated")),
    ("gitlab", re.compile(r"Welcome to GitLab, @(\S+?)!")),
    ("bitbucket", re.compile(r"logged in as (\S+?)\.?(?:\s|$)")),
    ("bitbucket-server", re.compile(r"authenticated via ssh key\.")),
    ("azure", re.compile(r"remote: Shell access is not supported")),
]


def parse_greeting(text: str) -> Tuple[Optional[str], Optional[str]]:
    """(forge, login) from what `ssh -T` printed; (None, None) when nothing is recognised."""
    forge_name, login, _line = _greeting(text)
    return forge_name, login


def _greeting(text: str) -> Tuple[Optional[str], Optional[str], str]:
    """(forge, login, the line that matched)."""
    for forge_name, rx in _GREETINGS:
        m = rx.search(text or "")
        if m:
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            line = text[start:end if end >= 0 else len(text)].strip()
            return forge_name, (m.group(1) if rx.groups else None), line
    return None, None, ""


@dataclass
class Probe:
    host: str                       # what ssh connects to: an alias or the host
    real_host: str
    user: str
    key: Optional[Path]             # the key ssh offers (None: ssh's own choice)
    port: Optional[int]
    use_i: bool                     # pass -i KEY -o IdentitiesOnly=yes (no alias / ssh_hosts block does it)
    who: str
    expected: Dict[str, Optional[str]]
    config: Optional[Path] = None   # -F: the ssh config of a --home that is not the login home

    def argv(self, timeout: float) -> List[str]:
        """BatchMode (never prompts), StrictHostKeyChecking=yes and UpdateHostKeys=no: a user config with
        accept-new or no can neither make auth trust an unknown host nor write known_hosts."""
        cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
               "-o", f"ConnectTimeout={max(1, min(10, int(timeout)))}"]
        if self.config:
            cmd += ["-F", str(self.config)]
        if self.use_i and self.key:
            cmd += ["-i", str(self.key), "-o", "IdentitiesOnly=yes"]
        if self.port and self.port != 22:
            cmd += ["-p", str(self.port)]
        return cmd + [f"{self.user}@{self.host}"]


def probes(cfg, identities: Sequence, *, include_default: bool = False, home: Optional[Path] = None) -> List[Probe]:
    """One probe per distinct (alias-or-host, key) pair that gitpro's ssh config produces."""
    home = Path(home) if home else Path.home()
    d = cfg.default
    dkey = _expand(d.ssh_key, home) if d.ssh_key else None
    out = []
    if include_default:
        for h in d.ssh_hosts:
            exp = {"gh": d.gh_account if h == GITHUB else None, "glab": d.glab_account if h == GITLAB else None}
            out.append(Probe(h, h, "git", dkey, None, False, "default", exp))
    for i in identities:
        exp = {"gh": i.gh_account, "glab": i.glab_account}
        key = _expand(i.ssh_key, home) if i.ssh_key else None
        if i.ssh_alias:
            out.append(Probe(i.ssh_alias, i.host, i.ssh_user, key, None, False, i.id, exp))
        elif key:
            out.append(Probe(i.host, i.host, i.ssh_user, key, i.ssh_port, True, i.id, exp))
        else:
            out.append(Probe(i.host, i.host, i.ssh_user, dkey if i.host in d.ssh_hosts else None, i.ssh_port,
                             False, i.id, exp))
    seen, uniq = set(), []
    conf = _ssh_config_for(home)
    for p in out:
        k = (p.user, p.host, p.port, str(p.key), p.use_i)
        if k not in seen:
            seen.add(k)
            p.config = conf
            uniq.append(p)
    return uniq


def _ssh_config_for(home: Path) -> Optional[Path]:
    """None when home is the login home (ssh reads its ~/.ssh/config itself); else that home's config,
    or /dev/null when it has none (never the login home's config for another home)."""
    import pwd
    try:
        login_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (KeyError, OSError):
        login_home = None
    if login_home is not None and Path(home).resolve() == login_home:
        return None
    conf = Path(home) / ".ssh" / "config"
    return conf if conf.is_file() else Path(os.devnull)


def _expected_login(p: Probe, forge_name: Optional[str]) -> Tuple[Optional[str], str]:
    if forge_name == "gitlab" or (forge_name is None and p.real_host == GITLAB):
        return p.expected.get("glab"), "glab_account"
    if forge_name == "github" or p.expected.get("gh"):
        return p.expected.get("gh"), "gh_account"
    return p.expected.get("glab"), "glab_account"


def _locked(key: Optional[Path]) -> bool:
    """A passphrase key that no agent holds (BatchMode cannot ask for the passphrase)."""
    if key is None or not key.exists() or shutil.which("ssh-keygen") is None:
        return False
    has, _why = probe_passphrase(key)
    if not has:
        return False
    _b, fp, _t, _e = fingerprint(_pub(key))
    agent = agent_fingerprints()
    return agent is None or fp not in agent


def _short(text: str) -> str:
    line = _first_line(text)
    cut = line.find("!")
    return line[:cut + 1] if 0 < cut < 60 else line[:60]


_UNKNOWN_HOST_RE = re.compile(r"Host key verification failed|No \S+ host key is known|host key for .* is not known",
                              re.I)
_NET_FAIL_RE = re.compile(r"Could not resolve hostname|Connection refused|Connection closed|kex_exchange|"
                          r"Network is unreachable|No route to host", re.I)
_TIMEOUT_RE = re.compile(r"Connection timed out|Operation timed out", re.I)


def _run_group(argv: List[str], timeout: float) -> subprocess.CompletedProcess:
    """Run argv in its own process group; on timeout kill the whole group (a ProxyCommand child too)
    and raise TimeoutExpired."""
    import signal
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:   # a child that left the group still holds the pipes
            proc.kill()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _line_with(text: str, needle: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if needle in ln), _first_line(text))


def probe_one(p: Probe, timeout: float, home: Path) -> dict:
    res = {"host": p.host, "real_host": p.real_host, "key": str(p.key) if p.key else None, "who": p.who,
           "login": None, "expected": None, "status": "error", "message": "", "fix": ""}
    try:
        r = _run_group(p.argv(timeout), timeout)
    except subprocess.TimeoutExpired:
        res.update(status="timeout", message=f"{p.real_host}: timed out after {timeout:g} s")
        return res
    except OSError as e:
        res.update(message=str(e))
        return res
    text = f"{r.stderr}\n{r.stdout}"
    forge_name, login, greeting = _greeting(text)
    expected, field_name = _expected_login(p, forge_name)
    res["expected"] = expected
    shown_key = _tilde(p.key, home) if p.key else "the key"
    if forge_name:
        res.update(login=login, message=greeting)
        if login and expected and login.lower() != expected.lower():
            res.update(status="mismatch", fix=f"register {shown_key} on {expected}, not {login}",
                       verdict=f"✗ {p.who} maps to {field_name} {expected}")
        else:
            res.update(status="ok", verdict=f"✓ matches {field_name} {expected}" if login and expected else "✓")
    elif _UNKNOWN_HOST_RE.search(text):
        changed = "HAS CHANGED" in text.upper()
        what = "has changed; compare it with the provider's published fingerprints before editing ~/.ssh/known_hosts" \
            if changed else "is not in ~/.ssh/known_hosts"
        res.update(status="unknown_host", message=_line_with(text, "Host key verification failed"),
                   verdict=f"✗ host key for {p.real_host} {what}",
                   fix="" if changed else f"connect once by hand: ssh -T {p.user}@{p.real_host} "
                   "(compare the fingerprint with the provider's published one)")
    elif "Permission denied" in text:
        if _locked(p.key):
            res.update(status="locked", message=_line_with(text, "Permission denied"), fix=f"ssh-add {_sh(shown_key)}",
                       verdict=f"✗ {shown_key} has a passphrase and is not in ssh-agent")
        else:
            hint = _upload_hint(p.who)
            res.update(status="denied", message=_line_with(text, "Permission denied"), fix=hint,
                       verdict=f"✗ key not registered on {p.real_host}")
    elif _TIMEOUT_RE.search(text):
        res.update(status="timeout", message=f"{p.real_host}: timed out ({_first_line(text)})",
                   verdict=f"✗ {p.real_host}: timed out")
    elif r.returncode in (0, 1) and not _NET_FAIL_RE.search(text):
        res.update(status="ok", message=_first_line(text) or "connected (login unknown)",
                   verdict="✓ connected (login unknown)")
    else:
        res.update(message=_first_line(text) or f"ssh exited {r.returncode}", verdict="✗ ssh failed")
    return res


def auth(cfg, identities: Sequence, timeout: float = AUTH_TIMEOUT, *, all: bool = False,
         home: Optional[Path] = None) -> dict:
    """ssh -T for every probe (in parallel); {"ok", "results", "status", "summary", "problems"}. Prints nothing."""
    home = Path(home) if home else Path.home()
    if shutil.which("ssh") is None:
        msg = "ssh not found"
        return {"ok": False, "results": [], "status": "error", "summary": msg,
                "problems": [{"message": msg, "fix": "install OpenSSH", "level": "error"}]}
    todo = probes(cfg, identities, include_default=all, home=home)
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(todo)))) as pool:
        results = list(pool.map(lambda p: probe_one(p, timeout, home), todo))
    problems = [{"message": f"{r['host']}: {r.get('verdict', '✗')[2:] or r['status']}"
                            + (f" ({r['message']})" if r["status"] in ("denied", "error") and r["message"] else ""),
                 "fix": r["fix"], "level": "error"} for r in results if r["status"] != "ok"]
    good = [f"{r['host']} → {r['login'] or '?'}" for r in results if r["status"] == "ok"]
    summary = ", ".join(good) if good and not problems else (problems[0]["message"] if problems else "no hosts")
    return {"ok": not problems, "results": results, "status": "error" if problems else "ok", "summary": summary,
            "problems": problems}


def render_auth(rep: dict, home: Path) -> str:
    rows = rep["results"]
    hw = max([len(r["host"]) for r in rows] + [12])
    kw = max([len(_tilde(r["key"], home)) if r["key"] else 1 for r in rows] + [10])
    lines = []
    for r in rows:
        key = _tilde(r["key"], home) if r["key"] else "-"
        said = _short(r["message"]) if r["status"] not in ("timeout",) else "(no answer)"
        verdict = r.get("verdict") or ("✓" if r["status"] == "ok" else "✗ " + r["message"])
        lines.append(f"  {r['host']:<{hw}} key {key:<{kw}}  →  {said:<30} {verdict}"
                     + (f" → {r['fix']}" if r["fix"] else ""))
    return "\n".join(lines) + ("\n" if lines else "")


def _select(cfg, ids: Optional[List[str]]) -> list:
    if not ids:
        return list(cfg.identities)
    by_id = {i.id: i for i in cfg.identities}
    unknown = [x for x in ids if x not in by_id]
    if unknown:
        have = ", ".join(by_id) or "none"
        raise ConfigError(f"no identity named {', '.join(unknown)} (identities: {have})")
    return [by_id[x] for x in dict.fromkeys(ids)]


def cmd_auth(args: argparse.Namespace) -> int:
    from . import cli
    cfg, env = cli._load_all(args)
    as_json = jsonout.wants_json(args)
    if shutil.which("ssh") is None:
        return fail("ssh not found; install OpenSSH", args)
    if args.timeout <= 0:
        return fail("--timeout must be a positive number of seconds", args)
    ids = _select(cfg, args.identity)
    todo = probes(cfg, ids, include_default=args.all, home=env.home)
    if not todo:
        if as_json:
            jsonout.emit("auth", {"results": []})
        else:
            print("· nothing to test: no identity (--all adds the ssh_hosts of [default])")
        return 0
    names = list(dict.fromkeys(p.host for p in todo))
    hosts = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
    print(f"online: connecting with ssh to {hosts} (ssh -T); sends your public key, nothing else",
          file=sys.stderr if as_json else sys.stdout, flush=True)
    rep = auth(cfg, ids, args.timeout, all=args.all, home=env.home)
    if as_json:
        results = [{k: v for k, v in r.items() if k != "verdict"} for r in rep["results"]]
        jsonout.emit("auth", {"ok": rep["ok"], "results": results})
    else:
        sys.stdout.write(render_auth(rep, env.home))
    return 0 if rep["ok"] else 1


# ---------------------------------------------------------------- keys --upload

@dataclass
class Target:
    who: str
    key: Path
    acc: object                     # forge.Account
    types: List[str]                # "auth", "signing" in that order


def upload_targets(cfg, ids: Optional[List[str]], home: Path) -> Tuple[List[Target], List[str]]:
    """(targets, warnings): every (account, key) pair the chosen identities map to, types merged."""
    d = cfg.default
    pairs: Dict[tuple, Target] = {}
    warnings = []

    def add(who: str, key: Path, accs: list, auth_: bool, sign: bool) -> None:
        for acc in accs:
            t = pairs.setdefault((acc, key), Target(who, key, acc, []))
            for kind, want in (("auth", auth_), ("signing", sign)):
                if want and kind not in t.types:
                    t.types.append(kind)
            t.types.sort(key=("auth", "signing").index)

    if not ids and d.ssh_key:
        add("default", _expand(d.ssh_key, home),
            _accounts_of(GITHUB, d.gh_account, None) + _accounts_of(GITLAB, None, d.glab_account), True, d.sign)
    for i in _select(cfg, ids):
        key = i.ssh_key or (d.ssh_key if config.signs(i, d) else None)
        accs = _accounts_of(i.host, i.gh_account, i.glab_account)
        if not key:
            warnings.append(f"{i.id}: no ssh_key; nothing to upload")
        elif not accs:
            warnings.append(f"{i.id}: no gh_account or glab_account; skipped (gitpro uploads only to a mapped account)")
        else:
            add(i.id, _expand(key, home), accs, bool(i.ssh_key), config.signs(i, d))
    return list(pairs.values()), warnings


def _add_argv(acc, pub: Path, title: str, kinds: List[str]) -> List[List[str]]:
    if acc.cli == "gh":
        return [["ssh-key", "add", str(pub)] + (["--type", "signing"] if k == "signing" else []) + ["--title", title]
                for k in kinds]
    usage = "auth_and_signing" if len(kinds) == 2 else kinds[0]
    return [["ssh-key", "add", str(pub), "--title", title, "--usage-type", usage]]


def _undo(acc, ids: List[str]) -> Optional[str]:
    if not ids:
        return None
    if acc.cli == "gh":
        return " && ".join(f"gh ssh-key delete {i} --yes" for i in ids)
    return " && ".join(f"glab ssh-key delete {i}" for i in ids)


def upload(cfg, ids: Optional[List[str]] = None, *, title: Optional[str] = None,
           confirm: Callable[[str], bool], say: Callable[[str], None] = print,
           warn: Callable[[str], None] = lambda m: print(m, file=sys.stderr), home: Optional[Path] = None) -> dict:
    """Add each chosen identity's public key to the account it maps to, for the uses it lacks there.
    Never uploads a key already present (compares key material) or to an unmapped account."""
    home = Path(home) if home else Path.home()
    targets, warnings = upload_targets(cfg, ids, home)
    for w in warnings:
        warn(f"⚠ {w}")
    lister = Lister()
    uploaded, present, failed, declined = [], [], 0, 0
    for t in targets:
        acc, pub = t.acc, _pub(t.key)
        tpub, where = _tilde(pub, home), f"{t.acc.host} ({t.acc.login})"
        mat = material(pub)
        if mat is None:
            warn(f"✗ {tpub} is missing or not a public key → gitpro keys --generate")
            failed += 1
            continue
        unsafe = pub_unsafe(pub)
        if unsafe:      # someone else could have swapped the key we would register
            fix = _chown_fix(pub, home) if "owned" in unsafe else f"chmod 644 {_sh(tpub)}"
            warn(f"✗ not uploading {tpub}: it {unsafe}; check its content, then → {fix}")
            failed += 1
            continue
        lst = lister.get(acc)
        if lst.error:
            warn(f"⚠ {lst.error}")
            failed += 1
            continue
        before = lst.found(mat)
        have = set().union(*(e.kinds for e in before)) if before else set()
        missing = [k for k in t.types if k not in have]
        if "signing" in missing and not lst.signing_known:
            warn(f"⚠ {acc.cli} too old to list signing keys; not adding {tpub} as a signing key on {where} "
                 f"(upgrade {acc.cli})")
            missing.remove("signing")
        if missing and before and acc.cli == "glab":
            kinds = " + ".join(k for k in ("auth", "signing") if k in have)
            warn(f"⚠ {tpub} is already on {where} as {kinds}; GitLab keeps one entry per key: change its usage type "
                 f"to auth_and_signing in GitLab (Preferences → SSH Keys) instead of adding it again")
            failed += 1
            continue
        if not missing:
            say(f"· {tpub} already on {where}: {', '.join(sorted(have, key=('auth', 'signing').index)) or 'auth'}")
            present.append({"key": str(pub), "host": acc.host, "account": acc.login, "types": sorted(have)})
            continue
        name = title or key_comment(pub) or f"gitpro {socket.gethostname()}"
        what = {("auth",): f'auth key "{name}"', ("signing",): f'signing key "{name}"',
                ("auth", "signing"): f'auth key "{name}" and signing key'}[tuple(missing)]
        if not confirm(f"upload {tpub} to {acc.host} account {acc.login} as {what}?"):
            say(f"✗ skipped {tpub} for {where}")
            declined += 1
            continue
        done = _add(acc, pub, name, missing, warn, where)
        if done is None:
            failed += 1
            continue
        lister.forget(acc)
        old = {e.id for e in before}
        new_ids = sorted({e.id for e in lister.get(acc).found(mat) if e.id and e.id not in old}, key=_num)
        undo = _undo(acc, new_ids)
        say(f"✓ uploaded {tpub} to {where}: {', '.join(done)}")
        say(f"↩ undo: {undo}" if undo else
            f"↩ undo: delete the key on {acc.host} (its id could not be read back from {acc.cli} ssh-key list)")
        uploaded.append({"key": str(pub), "host": acc.host, "account": acc.login, "who": t.who, "types": done,
                         "ids": new_ids, "undo": undo})
        if len(done) < len(missing):
            failed += 1
    return {"ok": not failed and not declined, "uploaded": uploaded, "present": present, "warnings": warnings,
            "failed": failed, "declined": declined}


def _num(s: str) -> Tuple[int, str]:
    return (int(s), s) if s.isdigit() else (0, s)


def _add(acc, pub: Path, title: str, kinds: List[str], warn, where: str) -> Optional[List[str]]:
    """Run the add calls; the kinds added (None when the first one failed)."""
    from . import forge
    done: List[str] = []
    calls = _add_argv(acc, pub, title, kinds)
    for argv, kind in zip(calls, kinds if acc.cli == "gh" else ["+".join(kinds)]):
        try:
            r = forge.run(acc, argv)
        except ConfigError as e:
            warn(f"✗ {e}")
            break
        if r.returncode != 0:
            if acc.cli == "gh" and _is_scope_error(r.stderr):
                warn(f"⚠ gh token for {acc.login} lacks the admin:public_key (and admin:ssh_signing_key) scope; run: "
                     + _scope_hint(acc, ("admin:public_key", "admin:ssh_signing_key")))
            else:
                why = _first_line(r.stderr) or f"exit {r.returncode}"
                warn(f"✗ {acc.cli} ssh-key add failed for {where}: {why}")
            break
        done += kind.split("+")
    return done or None


# ---------------------------------------------------------------- CLI glue

def _online(args, cfg) -> bool:
    return bool(args.online or getattr(getattr(cfg, "forge", None), "online", False))


def cmd_health(args: argparse.Namespace) -> int:
    from . import cli, forge, keys
    cfg, env = cli._load_all(args)
    as_json = jsonout.wants_json(args)
    if not keys.inventory(cfg.default, list(cfg.identities)):
        if as_json:
            jsonout.emit("keys", {"online": False, "keys": [], "ssh_dir_mode": ssh_dir_check(env.home)[0],
                                  "summary": {"fine": 0, "warnings": 0, "problems": 0}})
        else:
            print("no SSH keys configured (set ssh_key in [default] or an identity)")
        return 0
    if shutil.which("ssh-keygen") is None:
        return fail("ssh-keygen not found", args)
    online = _online(args, cfg)
    if online and not (forge.available("gh") or forge.available("glab")):
        if args.online:
            return fail(f"keys --health --online {NEEDS_CLI}", args)
        print(f"⚠ [forge] online = true, but the registered check {NEEDS_CLI}; checking offline only",
              file=sys.stderr)
        online = False
    if online:
        accs = forge.accounts(cfg)
        if accs:
            with _stdout_to(sys.stderr if as_json else sys.stdout):
                forge.announce(ANNOUNCE_WHAT, accs, ANNOUNCE_SENDS)
    data = check_keys(cfg, online=online, announce=False, home=env.home)
    if as_json:
        jsonout.emit("keys", _health_json(data))
    else:
        sys.stdout.write(render_health(data, env.home))
    return 1 if data["counts"]["problems"] else 0


def _stdout_to(stream):
    import contextlib
    return contextlib.redirect_stdout(stream)


def cmd_upload(args: argparse.Namespace) -> int:
    from . import cli, forge
    cfg, env = cli._load_all(args)
    as_json = jsonout.wants_json(args)
    accs = forge.accounts(cfg)
    if not _online(args, cfg):
        if as_json:
            return fail("keys --upload needs the network; re-run with --online", args)
        return forge.no_online_error("keys --upload", "which keys are registered, then adds the missing public keys",
                                     accs, "your public keys, after you confirm each one")
    if not (forge.available("gh") or forge.available("glab")):
        return fail(f"keys --upload {NEEDS_CLI}", args)
    if as_json and not args.yes:
        return fail("keys --upload --json needs -y (there is no question under --json)", args)
    if not args.yes and not sys.stdin.isatty():
        return fail("no terminal to confirm on; re-run with --yes", args)
    targets, _w = upload_targets(cfg, args.identity, env.home)
    shown = list(dict.fromkeys(t.acc for t in targets)) or accs
    with _stdout_to(sys.stderr if as_json else sys.stdout):
        forge.announce("which SSH keys are registered, then adds the missing ones", shown,
                       "the public key of each identity you confirm")

    def ask(question: str) -> bool:
        if args.yes:
            return True
        from rich.markup import escape
        from . import view
        return view.confirm(escape(question))

    out = sys.stderr if as_json else sys.stdout
    rep = upload(cfg, args.identity, title=args.title, confirm=ask, home=env.home,
                 say=lambda m: print(m, file=out), warn=lambda m: print(m, file=sys.stderr))
    if as_json:
        jsonout.emit("keys", {"ok": rep["ok"], "uploaded": rep["uploaded"], "present": rep["present"],
                              "warnings": rep["warnings"]})
    elif not rep["uploaded"] and not rep["failed"] and not rep["declined"]:
        print("✓ nothing to upload")
    return 0 if rep["ok"] else 1


def _add_key_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--health", action="store_true",
                   help="check each key: type and size, passphrase, file modes, ssh-agent (--online: registered)")
    p.add_argument("--upload", action="store_true",
                   help="add missing public keys to the gh/glab account each identity maps to (needs --online)")
    p.add_argument("--online", action="store_true", help="with --health/--upload: ask gh/glab (sends no key material "
                   "for --health)")
    p.add_argument("--identity", action="append", metavar="ID",
                   help="with --upload: only this identity (repeatable)").gitpro_values = "identities"
    p.add_argument("--title", metavar="T", help="with --upload: key title (default: the key comment)")
    p.add_argument("-y", "--yes", action="store_true", help="with --upload: don't ask (needed without a terminal)")
    p.add_argument("--json", action="store_true", help="with --health/--upload: machine-readable output")


def _keys_handler(args: argparse.Namespace) -> Optional[int]:
    if not (args.health or args.upload):
        extra = [f for f, v in (("--online", args.online), ("--identity", args.identity), ("--title", args.title),
                                ("--yes", args.yes), ("--json", args.json)) if v]
        return fail(f"keys {extra[0]} needs --health or --upload", args) if extra else None
    if args.health and args.upload:
        return fail("keys: use --health or --upload, not both", args)
    if args.generate or args.no_passphrase:
        return fail("keys: --generate cannot be combined with --health/--upload", args)
    if args.health:
        bad = [f for f, v in (("--identity", args.identity), ("--title", args.title), ("--yes", args.yes)) if v]
        if bad:
            return fail(f"keys {bad[0]} goes with --upload, not --health", args)
        return cmd_health(args)
    return cmd_upload(args)


def register(sub, common, confirm) -> None:
    sp = sub.add_parser("auth", parents=[common],
                        help="run ssh -T for every host and alias and show which login each one gets (online)")
    sp.add_argument("--identity", action="append", metavar="ID",
                    help="only this identity (repeatable)").gitpro_values = "identities"
    sp.add_argument("--all", action="store_true", help="also the ssh_hosts of [default]")
    sp.add_argument("--timeout", type=float, default=AUTH_TIMEOUT, metavar="S",
                    help=f"seconds per host (default: {AUTH_TIMEOUT})")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_auth)
    capability("auth")


def extend(subs) -> None:
    from .cli import extend_command
    extend_command(subs, "keys", _add_key_flags, _keys_handler)
    capability("keys.health", "keys.upload")
