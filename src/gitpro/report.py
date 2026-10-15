"""gitpro report (spec 19): a redacted bug report, crash report or denial report, as a prefilled GitHub issue.

Three layers keep private values out of a draft:
  1. allowlist: a section is built from typed fields, never from raw text (sections below);
  2. Redactor: every value that is kept as a shape gets a stable placeholder (email-1, host-2, ...);
  3. scan: the finished draft is checked against every value gitpro knows and a list of patterns. A hit
     blocks the report (exit 2); the scan never rewrites, and its answer names kinds and sections, never values.
"""
from __future__ import annotations

import os
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PROG = "gitpro"
REPO = "Yantrakar/gitpro"
ISSUES_NEW = f"https://github.com/{REPO}/issues/new"
ADVISORY_URL = f"https://github.com/{REPO}/security/advisories/new"
KNOWN_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "git.sr.ht", "gitea.com")
SECTIONS = ("title", "versions", "why", "crash", "denial", "message")
MAX_URL = 7000          # bytes; a longer prefilled url is cut at a section boundary
MAX_MESSAGE = 2000
CLIENT_RE = re.compile(r"^[a-z][a-z0-9-]{0,20}/[0-9A-Za-z.+-]{1,32}$")
# The report's own fixed words (labels, enum values, this project's name). A private value equal to one of
# them is not searched for (5.3 a): finding it shows nothing private, and searching would block every report
# from a repo named "gitpro" or an identity named "local". Values that only contain such a word still are.
PUBLIC_WORDS = frozenset("""
    yantrakar gitpro github.com gitlab.com bitbucket.org codeberg.org git.sr.ht gitea.com
    versions python linux darwin install pip pipx client unknown
    why redacted true false remotes origin upstream https ssh scp file other none expected default name email
    ssh_key exists such sign match rule matched no_match remote url pattern unset effective signingkey
    sshcommand gpgsign scope system global local worktree command repo pin generated line include pinned
    problems config user
    crash time version exception note recorded this report from frames innermost last inside outside
    denial source guard agent-check agent action reason codes committer author no_identity pushed_to problem
    push the above repository's state now not the denial commit pre-commit pre-push
    bug security what happened git override url_form unavailable usable could read repository
""".split())


class ReportError(Exception):
    """A problem the user can act on (no crash recorded, a bad argument). The text is fixed wording."""

# ---------------------------------------------------------------- layer 2: placeholders


def _norm(kind: str, value: str) -> str:
    if kind in ("email", "host"):
        return value.strip().lower()
    if kind in ("path", "file", "key"):
        import os
        return os.path.normpath(os.path.expanduser(value.strip()))
    return value.strip()


class Redactor:
    """P(kind, value) -> a placeholder, numbered per kind in order first seen; equal values share one.
    watch(kind, value) names a value to rewrite out of free text without numbering it until it appears."""

    def __init__(self, home: Optional[Path] = None, includes: Iterable[str] = ()) -> None:
        self.home = Path(home) if home is not None else Path.home()
        self.includes = {os.path.normpath(i) for i in includes}      # gitpro's generated include files
        self._seen: Dict[Tuple[str, str], str] = {}
        self._count: Dict[str, int] = {}
        self._values: List[Tuple[str, str]] = []        # (kind, original value) of every P() call
        self._watched: List[Tuple[str, str]] = []

    def P(self, kind: str, value: str) -> str:
        key = (kind, _norm(kind, value))
        if key not in self._seen:
            n = self._count[kind] = self._count.get(kind, 0) + 1
            noreply = kind == "email" and key[1].endswith("@users.noreply.github.com")
            self._seen[key] = f"email-{n}@users.noreply.github.com" if noreply else f"{kind}-{n}"
            self._values.append((kind, value))
        return self._seen[key]

    def watch(self, kind: str, value: Optional[str]) -> None:
        if value and len(value.strip()) >= 3 and value.strip().lower() not in PUBLIC_WORDS:
            self._watched.append((kind, value.strip()))

    def values(self) -> List[Tuple[str, str]]:
        """Every (kind, value) this redactor replaced or was told to watch."""
        return list(self._values) + list(self._watched)

    def rewrite(self, text: str) -> str:
        """text with every known value (longest first, case-insensitive, at word boundaries) replaced."""
        cands = {v: k for k, v in self.values() if len(v) >= 3 and v.lower() not in PUBLIC_WORDS}
        if not cands:
            return text
        order = sorted(cands, key=len, reverse=True)
        rx = re.compile("(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(v) for v in order) + ")(?![A-Za-z0-9_])",
                        re.I)
        lower = {v.lower(): k for v, k in cands.items()}

        def sub(m: "re.Match[str]") -> str:
            return self.P(lower[m.group(0).lower()], m.group(0))
        return rx.sub(sub, text)


# ---------------------------------------------------------------- layer 3: the scan

_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_SSH_KEY_RE = re.compile(r"ssh-(?:ed25519|rsa|dss)\s+AAAA|ecdsa-sha2-|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-")
_CLOUD_RE = re.compile(r"AKIA[0-9A-Z]{16}|xox[abprs]-")
_LONG_RE = re.compile(r"(?<![A-Za-z0-9+])[A-Za-z0-9+]{40,}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PLACEHOLDER_EMAIL_RE = re.compile(r"email-\d+@users\.noreply\.github\.com")
_PATH_RE = re.compile(r"(?<![\w/.-])/(?:home|Users|root|tmp)(?![\w-])")
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4_RE = re.compile(rf"(?<![\d.])(?:{_OCTET}\.){{3}}{_OCTET}(?![\d])")


def _pattern_kinds(text: str) -> List[str]:
    from . import forge, lost, resolve
    kinds: List[str] = []
    if forge._SECRET_RE.search(text) or forge._HEX_TOKEN_RE.search(text) or lost._TOKEN_RE.search(text):
        kinds.append("token")
    if any(resolve.redact_url(u) != u for u in forge._URL_RE.findall(text)):
        kinds.append("url-credentials")
    if _PRIVATE_KEY_RE.search(text):
        kinds.append("private-key")
    if _SSH_KEY_RE.search(text):
        kinds.append("ssh-key")
    if _CLOUD_RE.search(text):
        kinds.append("cloud-key")
    if _LONG_RE.search(text):
        kinds.append("long-run")
    if any(not _PLACEHOLDER_EMAIL_RE.fullmatch(m) for m in _EMAIL_RE.findall(text)):
        kinds.append("email")
    if _PATH_RE.search(text):
        kinds.append("path")
    if _IPV4_RE.search(text):
        kinds.append("ip")
    return kinds


def _known_rx(known: Iterable[Tuple[str, str]], public: Iterable[str] = ()
              ) -> List[Tuple[str, "re.Pattern[str]"]]:
    skip = PUBLIC_WORDS | {w.lower() for w in public}
    out = []
    for kind, value in known:
        value = (value or "").strip()
        if len(value) >= 3 and value.lower() not in skip:
            out.append((kind, re.compile(r"(?<![A-Za-z0-9_])" + re.escape(value) + r"(?![A-Za-z0-9_])", re.I)))
    return out


def _decoded_url(url: str) -> str:
    """The title and body of a prefilled-issue url, percent-decoded (the second look at what leaves)."""
    query = urllib.parse.urlsplit(url).query
    parts = urllib.parse.parse_qs(query, keep_blank_values=True)
    return "\n".join(v for vals in parts.values() for v in vals)


def scan(sections: Mapping[str, str], known: Iterable[Tuple[str, str]], *, url: str = "",
         public: Iterable[str] = ()) -> List[Dict[str, str]]:
    """Hits as [{"kind", "section"}] (empty: clean). `known` is (kind, value) pairs; `url` is a finished
    prefilled-issue url, whose title and body are decoded and scanned again as section "url". A known value
    equal to a word of PUBLIC_WORDS or `public` (gitpro's own identifiers in this draft) is not searched.
    Never returns, prints or raises with a matched value."""
    rxs = _known_rx(known, public)
    hits: List[Dict[str, str]] = []

    def check(name: str, text: str) -> None:
        kinds = _pattern_kinds(text) + [k for k, rx in rxs if rx.search(text)]
        for kind in dict.fromkeys(kinds):
            hits.append({"kind": kind, "section": name})
    for name, text in sections.items():
        check(name, text or "")
    if url:
        check("url", _decoded_url(url))
    return hits


def block_text(hits: Sequence[Mapping[str, str]]) -> str:
    """The message printed when the scan found something: kinds and sections only."""
    kinds = ", ".join(dict.fromkeys(h["kind"] for h in hits))
    sections = list(dict.fromkeys(h["section"] for h in hits))
    lines = [f"✗ report not sent: the draft contains {kinds} in {', '.join(sections)}"]
    if sections == ["message"]:
        lines.append("→ remove it from -m")
    else:
        lines.append('→ this is a gitpro bug; please report it without the draft: '
                     'gitpro report --print -m "report blocked"')
    return "\n".join(lines)


# ---------------------------------------------------------------- layer 1: sections

_VERSION_RE = re.compile(r"^[A-Za-z0-9_.+ ()-]{1,60}$")
_TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\dZ$")
_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,80}$")
_FUNC_RE = re.compile(r"^[A-Za-z_<?][A-Za-z0-9_<>]{0,80}$")
_FRAME_PATH_RE = re.compile(r"^gitpro/[A-Za-z0-9_/]+\.py$")
_COMMAND_RE = re.compile(r"^[a-z][a-z0-9 _.-]{0,60}$")
_WORD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_CODE_RE = re.compile(r"^[a-z_]{1,32}$")
_REASONS = ("committer", "author", "no_identity", "pushed_to", "problem", "push")
_SCOPES = ("system", "global", "local", "worktree", "command", "repo", "pin", "generated")
_CONFIG_HOW = ("--config", "$GITPRO_CONFIG", "user config")
_RULES = (("matched ", "matched"), ("no identity pattern matches", "no_match"),
          ("no remote url", "no_remote"))


def _plain(value: Any, rx: "re.Pattern[str]", default: str = "unknown") -> str:
    return value if isinstance(value, str) and rx.match(value) else default


def versions_section(client: Optional[str] = None) -> str:
    """Versions of gitpro, python, the OS, git and ssh, how gitpro was installed, and the --client string."""
    from . import __version__, doctor
    ctx = doctor.Ctx(Path.home())
    try:
        summary = doctor.check_versions(ctx)["summary"]
    except Exception:   # noqa: BLE001 - versions are a courtesy
        summary = ""
    parts = summary.split(" · ")
    ssh = parts[1] if len(parts) == 3 and parts[1] != "no ssh" else "unknown"
    install = "pipx" if "/pipx/venvs/" in sys.prefix.replace("\\", "/") else "pip"
    rows = [("gitpro", __version__), ("python", ctx.versions.get("python")), ("os", ctx.versions.get("os")),
            ("git", ctx.versions.get("git")), ("ssh", ssh), ("install", install)]
    lines = ["### Versions"] + [f"- {k}: {_plain(v, _VERSION_RE)}" for k, v in rows]
    if client:
        lines.append(f"- client: {client}")
    return "\n".join(lines)


_URL_FORMS = (
    ("https", re.compile(r"^https?://(?:[^/@]*@)?([^/:]+)(?::\d+)?(?:/(.*))?$", re.I)),
    ("ssh", re.compile(r"^ssh://(?:[^/@]*@)?([^/:]+)(?::\d+)?(?:/(.*))?$", re.I)),
    ("scp", re.compile(r"^(?:[^@/\s]+@)?([A-Za-z0-9._-]+):(?!//)(.*)$")),
)


def _split(text: str) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """(form, host, owner, repo) of a remote url or an identity url pattern; form is https|ssh|scp|file|other."""
    t = (text or "").strip()
    if t.startswith(("/", "./", "../", "~")) or t.lower().startswith("file://"):
        return "file", None, None, None
    for form, rx in _URL_FORMS:
        m = rx.match(t)
        if m:
            segs = [x for x in (m.group(2) or "").split("/") if x.strip("*")]
            owner = segs[0].replace("*", "") if segs else None
            repo = re.sub(r"\.git$", "", segs[1].replace("*", "")) if len(segs) > 1 else None
            return form, m.group(1), owner or None, repo or None
    return "other", None, None, None


def _shape(text: Optional[str], red: Redactor) -> str:
    """form + host + owner/repo, each private part as a placeholder; known hosts keep their name."""
    if not text:
        return "none"
    form, host, owner, repo = _split(text)
    if form in ("file", "other") or host is None:
        return f"{form} {red.P('path', text)}"
    out = [form, host.lower() if host.lower() in KNOWN_HOSTS else red.P("host", host)]
    if owner:
        out.append(red.P("owner", owner) + (f"/{red.P('repo', repo)}" if repo else ""))
    return " ".join(out)


def _origin(origin: Any, red: Redactor) -> str:
    if origin is None:
        return "unset"
    if origin == "command line:":
        return "command line"
    if not isinstance(origin, str) or not origin.startswith("file:"):
        return "other"
    path = os.path.normpath(origin[5:])
    home = red.home
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    p = Path(path)
    if p == home / ".gitconfig":
        return "~/.gitconfig"
    if p == xdg / "git" / "config":
        return "$XDG_CONFIG_HOME/git/config"
    if path.endswith("/.git/config"):
        return "repo .git/config"
    if path in red.includes or (p.parent == home and p.name.startswith(".gitconfig-")):
        return "gitpro include"
    return red.P("file", path)


def _scope(scope: Any) -> str:
    return "unset" if scope is None else scope if scope in _SCOPES else "other"


def _setting(j: Mapping[str, Any], kind: Optional[str], red: Redactor) -> str:
    value = j.get("value")
    if value is None:
        shown = "unset"
    elif kind is None:
        shown = "true" if value in (True, "true") else "false" if value in (False, "false") else "other"
    else:
        shown = red.P(kind, str(value))
    return f"{shown} (scope {_scope(j.get('scope'))}, origin {_origin(j.get('origin'), red)})"


def why_section(rep: Any, red: Redactor) -> str:
    """The redacted `why`: a field-by-field projection of Report.to_json() (spec 19, 4.2)."""
    j = rep.to_json()
    e, eff = j["expected"], j["effective"]
    match = e.get("match") or {}
    lines = ["### Why (redacted)", f"- ok: {'true' if j.get('ok') is True else 'false'}"]
    remotes = j.get("remotes") or {}
    lines.append(f"- remotes: {len(remotes)}")
    for name, url in remotes.items():
        shown = name if name in ("origin", "upstream") else red.P("remote", name)
        lines.append(f"  - {shown}: {_shape(url, red)}")
    key = e.get("ssh_key")
    exists = bool(key) and Path(os.path.expanduser(key)).exists()
    sign = e.get("sign")
    efile = e.get("file")
    rule = next((v for prefix, v in _RULES if str(match.get("rule") or "").startswith(prefix)), "other")
    lines += [
        f"- expected id: {red.P('id', e['id']) if e.get('id') else '[default]'}",
        f"- expected name: {red.P('name', e['name']) if e.get('name') else 'unset'}",
        f"- expected email: {red.P('email', e['email']) if e.get('email') else 'unset'}",
        f"- expected ssh_key: {red.P('key', key) + (' (file exists)' if exists else ' (no such file)') if key else 'unset'}",
        f"- expected sign: {str(sign).lower() if isinstance(sign, bool) else 'other'}",
        f"- expected file: {_origin('file:' + efile, red) if isinstance(efile, str) else 'unset'}",
        f"- match rule: {rule}",
        f"- match remote: {(match.get('remote') if match.get('remote') in ('origin', 'upstream') else red.P('remote', match['remote'])) if match.get('remote') else 'none'}",
        f"- match url: {_shape(match.get('url'), red)}",
        f"- match pattern: {_shape(match.get('pattern'), red)}",
    ]
    for label, field_name, kind in (("name", "name", "name"), ("email", "email", "email"),
                                    ("signingkey", "signingkey", "key"), ("sshcommand", "sshcommand", "sshcommand"),
                                    ("gpgsign", "gpgsign", None)):
        lines.append(f"- effective {label}: {_setting(eff.get(field_name) or {}, kind, red)}")
    lines.append(f"- pinned: {'true' if eff.get('pinned') is True else 'false'}")
    codes = [p.get("code") if _CODE_RE.match(str(p.get("code"))) else "other" for p in j.get("problems") or []]
    lines.append("- problems: " + (", ".join(codes) if codes else "none"))
    return "\n".join(lines)


def config_line(how: Any) -> str:
    return f"- config: {how if how in _CONFIG_HOW else 'other'}"


def _clean_crash(rec: Mapping[str, Any]) -> Dict[str, Any]:
    frames = []
    for f in rec.get("frames") if isinstance(rec.get("frames"), list) else []:
        if (isinstance(f, list) and len(f) == 3 and isinstance(f[0], str) and _FRAME_PATH_RE.match(f[0])
                and isinstance(f[1], int) and not isinstance(f[1], bool) and f[1] >= 0
                and isinstance(f[2], str) and _FUNC_RE.match(f[2])):
            frames.append((f[0], f[1], f[2]))
    outside = rec.get("outside")
    return {"time": _plain(rec.get("time"), _TIME_RE), "version": _plain(rec.get("version"), _VERSION_RE),
            "command": _plain(rec.get("command"), _COMMAND_RE), "type": _plain(rec.get("type"), _TYPE_RE, "Exception"),
            "frames": frames[-30:], "outside": outside if isinstance(outside, int) and outside >= 0 else 0}


def crash_section(rec: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    from . import __version__
    c = _clean_crash(rec)
    lines = ["### Crash", f"- time: {c['time']}", f"- version: {c['version']}", f"- command: {c['command']}",
             f"- exception: {c['type']}"]
    if c["version"] != __version__:
        lines.append(f"- note: recorded by gitpro {c['version']}; this report is from {__version__}")
    lines.append("- frames (innermost last):")
    lines += [f"  - {p}:{n} in {fn}" for p, n, fn in c["frames"]] or ["  - none inside gitpro"]
    lines.append(f"- frames outside gitpro: {c['outside']}")
    return "\n".join(lines), c


def _clean_denial(rec: Mapping[str, Any]) -> Dict[str, Any]:
    codes = rec.get("codes") if isinstance(rec.get("codes"), list) else []
    agent = rec.get("agent")
    return {"time": _plain(rec.get("time"), _TIME_RE), "version": _plain(rec.get("version"), _VERSION_RE),
            "source": rec.get("source") if rec.get("source") in ("guard", "agent-check") else "unknown",
            "agent": agent if isinstance(agent, str) and _WORD_RE.match(agent) else "none",
            "action": _plain(rec.get("action"), _WORD_RE, "other"),
            "reason": rec.get("reason") if rec.get("reason") in _REASONS else "problem",
            "codes": [c if isinstance(c, str) and _CODE_RE.match(c) else "other" for c in codes][:20]}


def denial_section(rec: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    d = _clean_denial(rec)
    lines = ["### Denial", f"- time: {d['time']}", f"- source: {d['source']}", f"- agent: {d['agent']}",
             f"- action: {d['action']}", f"- reason: {d['reason']}",
             "- codes: " + (", ".join(d["codes"]) if d["codes"] else "none"),
             "- note: the why above is the repository's state now, not at the time of the denial"]
    return "\n".join(lines), d


def message_section(text: str, red: Redactor) -> str:
    from . import forge
    clean = red.rewrite(text)
    return "### What happened\n" + "\n".join(forge.safe(line) for line in clean.splitlines())


def make_title(kind: str, message: Optional[str], crash: Optional[Mapping[str, Any]],
               denial: Optional[Mapping[str, Any]]) -> str:
    """The issue title; `message` is the already rewritten -m text."""
    from . import __version__
    first = next((ln.strip() for ln in (message or "").splitlines() if ln.strip()), "")
    if first:
        from . import forge
        return f"[{kind}] {forge.safe(first)}"[:80]
    if kind == "crash" and crash:
        return f"[crash] {crash['type']} in {crash['command']}"[:80]
    if kind == "denial" and denial:
        return f"[denial] {denial['reason']} on {denial['action']}"[:80]
    if kind == "security":
        return f"[security] gitpro {__version__}"
    return f"[{kind}] gitpro {__version__}: "


# ---------------------------------------------------------------- the draft

@dataclass
class Draft:
    kind: str                                   # bug | crash | denial | security
    title: str
    sections: Dict[str, str]                    # name -> text, as scanned
    body: str                                   # what goes in the url (possibly cut)
    full_body: str                              # everything, for the terminal
    url: str
    truncated: bool = False
    hits: List[Dict[str, str]] = field(default_factory=list)


def _known_values(cfg: Any, repo: Optional[Path], red: Redactor) -> List[Tuple[str, str]]:
    """Every private value gitpro knows, as (kind, value): the scan looks for these in the finished draft."""
    import getpass
    import socket
    known = list(red.values())
    home = str(Path.home())
    known.append(("path", home))
    if str(red.home) != home:
        known.append(("path", str(red.home)))
    try:
        known.append(("user", getpass.getuser()))
    except Exception:   # noqa: BLE001
        pass
    try:
        host = socket.gethostname()
        known += [("machine", host), ("machine", host.split(".")[0])]
    except Exception:   # noqa: BLE001
        pass
    if repo is not None:
        known.append(("path", str(repo)))
    if cfg is not None:
        people = [cfg.default] + list(cfg.identities)
        for who in people:
            known += [("name", getattr(who, "name", None) or ""), ("email", getattr(who, "email", None) or ""),
                      ("account", getattr(who, "gh_account", None) or ""),
                      ("account", getattr(who, "glab_account", None) or "")]
            key = getattr(who, "ssh_key", None)
            if key:
                known += [("key", key), ("key", os.path.expanduser(key)), ("key", os.path.basename(key))]
            git = getattr(who, "git", None) or {}
            if isinstance(git, dict) and git.get("user.signingkey"):
                known.append(("key", str(git["user.signingkey"])))
        for i in cfg.identities:
            known += [("id", i.id), ("host", i.host if i.host.lower() not in KNOWN_HOSTS else ""),
                      ("host", getattr(i, "ssh_alias", None) or "")]
            known += [("owner", o) for o in i.owners]
            if getattr(i, "file", None):
                known.append(("file", os.path.expanduser(i.file)))
    return [(k, v) for k, v in known if isinstance(v, str) and len(v.strip()) >= 3]


def _watched_values(cfg: Any, repo: Optional[Path], red: Redactor) -> None:
    """Tell the redactor which values to rewrite out of the -m text."""
    kind_map = {"user": "account", "machine": "host"}
    for kind, value in _known_values(cfg, repo, red):
        red.watch(kind_map.get(kind, kind), value)


def _load_config(config: Optional[Path], home: Path) -> Tuple[Any, Optional[str]]:
    from . import config as cfgmod
    try:
        path, how = cfgmod.find(config, home)
        return cfgmod.load_all(path, how), how
    except cfgmod.ConfigError:
        return None, None


def _includes(cfg: Any, home: Path) -> List[str]:
    out = [str(home / ".gitconfig-identities")]
    if cfg is not None:
        for i in cfg.identities:
            out.append(str(home / f".gitconfig-{i.id}"))
            if getattr(i, "file", None):
                out.append(os.path.expanduser(i.file))
    return out


def _fit(kind: str, title: str, parts: List[Tuple[str, str]]) -> Tuple[str, str, bool]:
    """(body, url, truncated): sections dropped (why, crash, denial, message; never versions) until the
    url fits in MAX_URL."""
    parts = [p for p in parts if p[1]]
    truncated = False
    while True:
        body = "\n\n".join(t for _, t in parts)
        url = ISSUES_NEW + "?" + urllib.parse.urlencode({"title": title, "body": body}, quote_via=urllib.parse.quote)
        if len(url.encode("utf-8")) <= MAX_URL:
            return body, url, truncated
        for name in ("why", "crash", "denial", "message"):
            if any(n == name for n, _ in parts):
                parts = [p for p in parts if p[0] != name]
                truncated = True
                break
        else:
            return body, url, truncated


def build_draft(kind: str, *, message: Optional[str] = None, repo: Optional[Path] = None, why_cwd: bool = False,
                client: Optional[str] = None, config: Optional[Path] = None, home: Optional[Path] = None) -> Draft:
    """Collect, redact and scan one draft. Raises ReportError (no record, bad argument). A scan hit does not
    raise: the hits are in Draft.hits and the caller must not send the draft."""
    from . import incident, resolve
    home = Path(home) if home is not None else Path.home()
    if client is not None and not CLIENT_RE.match(client):
        raise ReportError("--client must look like name/1.2.3")
    if message is not None and len(message) > MAX_MESSAGE:
        raise ReportError(f"-m is longer than {MAX_MESSAGE} characters")
    cfg, how = _load_config(config, home)
    red = Redactor(home, _includes(cfg, home))
    crash = denial = None
    sections: Dict[str, str] = {}
    crash_c = denial_c = None
    if kind == "crash":
        rec = incident.read_crash()
        if rec is None:
            raise ReportError("no crash recorded")
        crash, crash_c = crash_section(rec)
    elif kind == "denial":
        recs = incident.read_denials()
        if not recs:
            raise ReportError("no denial recorded")
        denial, denial_c = denial_section(recs[-1])
    sections["versions"] = versions_section(client)
    where = repo if repo is not None else (Path.cwd() if why_cwd else None)
    top: Optional[Path] = None
    if where is not None:
        sections["why"] = _why_for(where, cfg, how, red, home)
        top = _repo_top(where)
    if crash:
        sections["crash"] = crash
    if denial:
        sections["denial"] = denial
    rewritten = ""
    if message:
        _watched_values(cfg, top or where, red)
        rewritten = red.rewrite(message)
        sections["message"] = message_section(message, red)
    title = make_title(kind, rewritten, crash_c, denial_c)
    sections = {"title": title, **sections}
    order = [("message", sections.get("message", "")), ("versions", sections["versions"]),
             ("why", sections.get("why", "")), ("crash", sections.get("crash", "")),
             ("denial", sections.get("denial", ""))]
    full_body = "\n\n".join(t for _, t in order if t)
    if kind == "security":
        body, url, truncated = full_body, ADVISORY_URL, False
        scan_url = ""
    else:
        body, url, truncated = _fit(kind, title, order)
        scan_url = url
    known = _known_values(cfg, top or where, red)
    public = [w for p, _, fn in (crash_c or {}).get("frames", ()) for w in (fn, Path(p).stem)]
    if crash_c:
        public += [crash_c["type"], *crash_c["command"].split()]
    if denial_c:
        public += [denial_c["action"], denial_c["agent"], denial_c["reason"], *denial_c["codes"]]
    hits = scan(sections, known, url=scan_url, public=public)
    return Draft(kind, title, sections, body, full_body, url, truncated, hits)


def _repo_top(where: Path) -> Optional[Path]:
    from . import resolve
    try:
        return resolve.is_repo(where)
    except Exception:   # noqa: BLE001
        return None


def _why_for(where: Path, cfg: Any, how: Optional[str], red: Redactor, home: Path) -> str:
    """The why section for `where`, or a fixed line saying why there is none (never an exception text)."""
    from . import resolve
    if cfg is None:
        return "### Why (redacted)\n- unavailable: no usable config"
    try:
        top = resolve.is_repo(where)
        if not top:
            return "### Why (redacted)\n- unavailable: not in a git repository"
        rep = resolve.compare(top, cfg, env=None, home=home)
        return why_section(rep, red) + "\n" + config_line(how)
    except Exception:   # noqa: BLE001 - never let an error text into the draft
        return "### Why (redacted)\n- unavailable: gitpro could not read the repository"


# ---------------------------------------------------------------- the command

NOTE_PUBLIC = "→ anything you add on GitHub is public; do not paste tokens, keys or private paths"


def _tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(question: str) -> bool:
    try:
        answer = input(f"{question} ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _can_open() -> bool:
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return True
    return sys.platform.startswith("linux") and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _open_browser(url: str) -> bool:
    if not _can_open():
        return False
    try:
        import webbrowser
        return bool(webbrowser.open(url, new=2))
    except Exception:   # noqa: BLE001
        return False


def _kind(args: Any) -> str:
    return "crash" if args.crash else "denial" if args.denial else "security" if args.security else "bug"


def _preview(d: Draft, *, show_url: bool, login: Optional[str] = None) -> None:
    from . import forge
    if d.kind == "security":
        print(d.full_body)
        print()
        print("→ security reports go to the private advisory form on github.com/Yantrakar/gitpro "
              "(nothing is filed until you submit)")
        print("→ paste the draft above into the form; do not paste tokens, keys or private paths")
    else:
        print(forge.safe(d.title))
        print(d.full_body)
        print()
        if login:
            print(f"→ files as {forge.safe(login)} on github.com, public")
        else:
            print("→ prefilled issue: github.com/Yantrakar/gitpro (you still press Submit)")
        if d.truncated and not login:
            print("→ the draft is long; the rest was printed above for you to paste")
        print(NOTE_PUBLIC)
    if show_url:
        print("→ open this URL in your browser")
        print(d.url)


def _json(d: Draft, sent: Optional[Mapping[str, str]] = None) -> None:
    from . import jsonout
    jsonout.emit("report", {"kind": d.kind, "title": d.title, "body": d.body, "url": d.url,
                            "truncated": d.truncated, "sent": dict(sent) if sent else None})


ISSUE_RE = re.compile(r"^https://github\.com/Yantrakar/gitpro/issues/\d+$")


def _pick_account(args: Any) -> Tuple[Any, Any]:
    """(session, account) for --online: the one github.com gh login that files the issue. Raises ReportError."""
    from . import config as cfgmod, forge
    try:
        path, how = cfgmod.find(args.config, args.home)
        cfg = cfgmod.load_all(path, how)
        session = forge.Session(cfg, args.home, ids=[args.as_] if args.as_ else None, as_json=bool(args.json))
    except cfgmod.ConfigError as e:
        raise ReportError(str(e)) from None
    session.targets = [t for t in session.targets if t.acc.cli == "gh" and t.acc.host == "github.com"]
    session.warnings, session._queue = [], []     # "identity X has no account" is noise here, and names an id
    accs = session.accounts()
    if not accs:
        raise ReportError("report --online: no identity has a github.com gh_account; drop --online to use the browser")
    if len(accs) > 1:
        ids = ", ".join(dict.fromkeys(t.id for t in session.targets))
        raise ReportError(f"report --online: more than one gh login could file this ({ids}); pick one with --as ID")
    return session, accs[0]


def _file_issue(d: Draft, session: Any, acc: Any, args: Any) -> int:
    """gh issue create with the full body on stdin; prints (or emits) the issue url."""
    from . import forge
    try:
        env = session.env(acc)
    except Exception as e:   # noqa: BLE001 - a ConfigError names the fix, never the token
        return _fail(forge.redact(str(e), session.secrets()), args)
    r = forge._exec(acc, ["issue", "create", "--repo", REPO, "--title", d.title, "--body-file", "-"], env,
                    stdin=d.full_body)
    if r.returncode != 0:
        err = forge.safe(forge.redact(r.stderr.strip(), session.secrets()))
        return _fail(f"gh issue create failed ({r.returncode}): {err}", args)
    url = (r.stdout or "").strip()
    if not ISSUE_RE.match(url):
        return _fail("gh issue create printed something that is not an issue url on "
                     "github.com/Yantrakar/gitpro; check the issue list yourself", args)
    if args.json:
        _json(d, {"issue": url})
    else:
        print(f"✓ filed {url}")
    return 0


def cmd_report(args) -> int:
    from . import jsonout
    kind = _kind(args)
    as_json = bool(args.json)
    online = bool(getattr(args, "online", False))
    if online and args.security:
        return _fail("security reports go to the private form only; run gitpro report --security", args)
    if getattr(args, "as_", None) and not online:
        return _fail("--as only goes with --online", args)
    try:
        d = build_draft(kind, message=args.message, repo=args.repo, why_cwd=kind in ("bug", "denial"),
                        client=args.client, config=args.config, home=args.home)
        session = acc = None
        if online:
            session, acc = _pick_account(args)
    except ReportError as e:
        return _fail(str(e), args)
    if d.hits:
        if as_json:
            jsonout.emit("report", {"blocked": d.hits}, ok=False)
        else:
            print(block_text(d.hits), file=sys.stderr)
        return 2
    printing = args.print or (as_json and not (online and args.yes))
    if not printing and not args.yes and not _tty():
        printing = True
    if printing:
        if as_json:
            _json(d)
        else:
            _preview(d, show_url=True)
        return 0
    if online:
        code = session.gate("report", "to file a public issue on Yantrakar/gitpro", "the draft shown above",
                            explicit=True, args=args)
        if code is not None:
            return code
        if not as_json:
            _preview(d, show_url=False, login=acc.login)
        if not args.yes and not _ask(f"file this as a public issue on github.com/{REPO} as "
                                     f"{_safe(acc.login)}? [y/N]"):
            return 0
        return _file_issue(d, session, acc, args)
    if not as_json:
        _preview(d, show_url=False)
    question = ("open the private security advisory form?" if d.kind == "security"
                else "open the prefilled issue form in your browser?")
    if not args.yes and not _ask(question + " [y/N]"):
        return 0
    if _open_browser(d.url):
        return 0
    if not as_json:
        print("→ open this URL in your browser")
        print(d.url)
    else:
        _json(d)
    return 0


def _safe(text: str) -> str:
    from . import forge
    return forge.safe(text)


def _fail(msg: str, args: Any) -> int:
    from .args import fail
    return fail(msg, args)


def register(sub, common, confirm) -> None:
    from .args import capability, guarded
    sp = sub.add_parser("report", parents=[common],
                        help="a redacted draft of a bug, crash or denial report (nothing is sent unless you say so)")
    kinds = sp.add_mutually_exclusive_group()
    kinds.add_argument("--crash", action="store_true", help="report the last recorded crash")
    kinds.add_argument("--denial", action="store_true", help="report the last recorded guard or agent denial")
    kinds.add_argument("--security", action="store_true",
                       help="draft for the private security advisory form (never a public issue)")
    sp.add_argument("-m", "--message", metavar="TEXT", help="what happened (at most 2000 characters; redacted)")
    sp.add_argument("--repo", type=Path, metavar="PATH", help="include the redacted why of this repository")
    sp.add_argument("--client", metavar="NAME/VERSION", help="the tool that asks (e.g. vscode/0.3.3)")
    sp.add_argument("--print", action="store_true", help="print the draft and the url; open nothing")
    sp.add_argument("--online", action="store_true",
                    help="file the issue with gh (as one of your github.com gh_accounts) instead of the browser")
    sp.add_argument("--as", dest="as_", metavar="ID", help="the identity whose gh_account files it (with --online)")
    sp.add_argument("-y", "--yes", action="store_true", help="answer yes to the question")
    sp.add_argument("--json", action="store_true", help="machine-readable output (implies --print)")
    sp.set_defaults(func=lambda a: guarded(cmd_report, a))
    capability("report")
