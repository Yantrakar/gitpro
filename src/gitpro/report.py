"""gitpro report (spec 19): a redacted bug report, crash report or denial report, as a prefilled GitHub issue.

Three layers keep private values out of a draft:
  1. allowlist: a section is built from typed fields, never from raw text (sections below);
  2. Redactor: every value that is kept as a shape gets a stable placeholder (email-1, host-2, ...);
  3. scan: the finished draft is checked against every value gitpro knows and a list of patterns. A hit
     blocks the report (exit 2); the scan never rewrites, and its answer names kinds and sections, never values.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PROG = "gitpro"
REPO = "Yantrakar/gitpro"
ISSUES_NEW = f"https://github.com/{REPO}/issues/new"
ADVISORY_URL = f"https://github.com/{REPO}/security/advisories/new"
KNOWN_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "git.sr.ht", "gitea.com")
SECTIONS = ("title", "versions", "why", "crash", "denial", "message")

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

    def __init__(self) -> None:
        self._seen: Dict[Tuple[str, str], str] = {}
        self._count: Dict[str, int] = {}
        self._values: List[Tuple[str, str]] = []        # (kind, original value) of every P() call
        self._watched: List[Tuple[str, str]] = []

    def P(self, kind: str, value: str) -> str:
        key = (kind, _norm(kind, value))
        if key not in self._seen:
            n = self._count[kind] = self._count.get(kind, 0) + 1
            self._seen[key] = f"email-{n}@users.noreply.github.com" if kind == "email" else f"{kind}-{n}"
            self._values.append((kind, value))
        return self._seen[key]

    def watch(self, kind: str, value: Optional[str]) -> None:
        if value and len(value.strip()) >= 3:
            self._watched.append((kind, value.strip()))

    def values(self) -> List[Tuple[str, str]]:
        """Every (kind, value) this redactor replaced or was told to watch."""
        return list(self._values) + list(self._watched)

    def rewrite(self, text: str) -> str:
        """text with every known value (longest first, case-insensitive, at word boundaries) replaced."""
        cands = {v: k for k, v in self.values() if len(v) >= 3}
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


def _known_rx(known: Iterable[Tuple[str, str]]) -> List[Tuple[str, "re.Pattern[str]"]]:
    out = []
    for kind, value in known:
        value = (value or "").strip()
        if len(value) >= 3:
            out.append((kind, re.compile(r"(?<![A-Za-z0-9_])" + re.escape(value) + r"(?![A-Za-z0-9_])", re.I)))
    return out


def _decoded_url(url: str) -> str:
    """The title and body of a prefilled-issue url, percent-decoded (the second look at what leaves)."""
    query = urllib.parse.urlsplit(url).query
    parts = urllib.parse.parse_qs(query, keep_blank_values=True)
    return "\n".join(v for vals in parts.values() for v in vals)


def scan(sections: Mapping[str, str], known: Iterable[Tuple[str, str]], *, url: str = "") -> List[Dict[str, str]]:
    """Hits as [{"kind", "section"}] (empty: clean). `known` is (kind, value) pairs; `url` is a finished
    prefilled-issue url, whose title and body are decoded and scanned again as section "url". Never
    returns, prints or raises with a matched value."""
    rxs = _known_rx(known)
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
