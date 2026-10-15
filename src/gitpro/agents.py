"""AI agent hooks: `gitpro agent-check` (Claude Code PreToolUse, experimental Cursor/Gemini/generic) and
`gitpro agent install|uninstall|status`.

agent-check reads the agent's hook JSON on stdin, finds the git/gh/glab commands in the shell command
the agent is about to run, and judges each repo the way the git guard does. On a mismatch it denies
the call with a reason that tells the agent to ask the user. It never grants anything: silence means
"no opinion" and the agent's own permission flow applies. Any failure fails open (exit 0, a warning
on stderr), so a broken gitpro never stops an agent.

Hot path: module level imports only the standard library; config and resolve load only once a
relevant command is found, so the ignore path stays fast.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

PROG = "gitpro"
MARKER = "gitpro agent-check"           # how an installed hook entry is recognised
HOOK_TIMEOUT = 10                       # seconds, Claude Code's per-hook timeout
MAX_DEPTH = 4                           # sh -c / $(...) / eval nesting followed by the tokeniser
MAX_PARSE = 64 * 1024                   # longer commands go straight to the regex scan
MAX_SCAN = 1024 * 1024                  # the regex scan reads at most this much (the rest is not checked)
MAX_FOR = 20                            # `for x in w1 .. wN` with more words: the body is judged once, x unknown
MAX_VALUE = 4096                        # a longer expanded $VAR value counts as unknown
ALIAS_TIMEOUT = 3                       # seconds for the `git config --get alias.X` lookup on the hook path
TRIGGERS = ("ambiguous", "override", "pin_stale", "no_identity", "env_override")
SKIP_ENV = "GITPRO_SKIP"
BYPASS_HELP = (f"{SKIP_ENV}=1 in the environment the agent starts with turns the check off for that session; "
               "[agents] hook = \"warn\" or \"off\" in the config changes it for good. The deny message never "
               "mentions this: the agent is told to ask you.")

GIT_SUBS = {"commit", "push", "am", "cherry-pick", "rebase", "merge", "revert", "tag", "notes", "commit-tree"}
# git commands that are not aliases (an unknown subcommand may be a config alias, looked up at judge time)
GIT_BUILTINS = GIT_SUBS | {
    "add", "annotate", "apply", "archive", "bisect", "blame", "branch", "bugreport", "bundle", "cat-file",
    "check-attr", "check-ignore", "check-mailmap", "check-ref-format", "checkout", "checkout-index", "citool",
    "clean", "clone", "column", "config", "count-objects", "credential", "describe", "diff", "diff-files",
    "diff-index", "diff-tree", "difftool", "fast-export", "fast-import", "fetch", "fetch-pack", "filter-branch",
    "for-each-ref", "for-each-repo", "format-patch", "fsck", "gc", "get-tar-commit-id", "grep", "gui", "hash-object",
    "help", "hook", "http-backend", "index-pack", "init", "instaweb", "interpret-trailers", "lfs", "log",
    "ls-files", "ls-remote", "ls-tree", "mailinfo", "mailsplit", "maintenance", "merge-base", "merge-file",
    "merge-tree", "mergetool", "mktag", "mktree", "multi-pack-index", "mv", "name-rev", "pack-objects",
    "pack-refs", "prune", "prune-packed", "pull", "range-diff", "read-tree", "reflog", "remote", "repack",
    "replace", "request-pull", "rerere", "reset", "restore", "rev-list", "rev-parse", "rm", "send-email",
    "send-pack", "shortlog", "show", "show-branch", "show-index", "show-ref", "sparse-checkout", "stash",
    "status", "stripspace", "submodule", "switch", "symbolic-ref", "unpack-file", "unpack-objects",
    "update-index", "update-ref", "update-server-info", "var", "verify-commit", "verify-pack", "verify-tag",
    "version", "whatchanged", "worktree", "write-tree",
}
UNDO_FLAGS = {"--abort", "--quit", "--skip"}          # rebase/merge/... that create no commit
UNDOABLE = {"rebase", "merge", "cherry-pick", "am", "revert"}
GH_SUBS = {("pr", "create"), ("pr", "merge"), ("pr", "close"), ("release", "create"), ("repo", "create"),
           ("repo", "delete")}
GLAB_SUBS = {("mr", "create"), ("mr", "merge"), ("release", "create"), ("repo", "create")}
HUB_SUBS = {("pull-request",), ("create",), ("delete",), ("release", "create")}
HUB_COMMANDS = {"pull-request", "create", "delete", "fork", "pr", "issue", "release", "browse", "compare",
                "ci-status", "sync", "api", "gist", "alias"}
API_TAKES = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "-H", "--header", "--input", "-q", "--jq",
             "-t", "--template", "--cache", "-p", "--preview", "--hostname", "-R", "--repo"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
# shell reserved words skipped in command position (`for`/`select`/`case` drop the whole header)
RESERVED = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "{", "}", "!", "in", "esac"}
CMD_LEAD = {"if", "then", "else", "elif", "do", "while", "until", "{", "!", "time"}   # a command follows
# command wrappers skipped before the real program, with the options that take a value
WRAPPERS: Dict[str, set] = {
    "env": {"-u", "--unset", "-S", "--split-string"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-T", "-U", "--user", "--group", "--host",
             "--prompt", "--chdir", "--role", "--type", "--command-timeout", "--other-user", "--close-from"},
    "doas": {"-u", "-C"},
    "command": set(),
    "builtin": set(),
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid"},
    "time": {"-f", "--format", "-o", "--output"},
    "xargs": {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file", "--delimiter", "--max-args",
              "--max-procs", "--max-chars", "--max-lines"},
    "exec": {"-a"},
    "nohup": set(),
    "setsid": set(),
    "chronic": set(),
    "unbuffer": set(),
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"},
    "strace": {"-e", "-o", "-p", "-s", "-u", "-E", "-a", "-b", "-I", "-O", "-S", "-P", "-X", "--output",
               "--attach", "--user", "--env", "--string-limit"},
    "flock": {"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"},
    "watch": {"-n", "--interval", "-q", "--equexit"},
}
PUNCT = "();<>|&\n"
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SUB_RE = re.compile(r"__gitpro_sub(\d+)_(\d+)__")      # depth, index
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_HEREDOC_RE = re.compile(r"<<(?!<)-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_WRITE_API = r"api\b(?=[^\n;&|]{0,300}?(?:-X\s*|--method[=\s]+)(?:POST|PUT|PATCH|DELETE)\b)"
_FALLBACK = (
    # bounded option run and no `git` taken as an option value: linear even on `git -C git -C ...`
    ("git", re.compile(r"\bgit\b(?:\s+(?:-C\s+(?!git\b)(?P<dir>[^\s;&|]+)|-c\s+(?!git\b)\S+|--[\w-]+(?:=\S*)?"
                       r"|-(?![cC]\s)[\w-]+)){0,16}"
                       r"\s+(?P<sub>commit|push|am|cherry-pick|rebase|merge|revert|tag)\b")),
    ("gh", re.compile(r"\bgh\s+(?P<sub>pr\s+(?:create|merge|close)|release\s+create|repo\s+(?:create|delete)|"
                      + _WRITE_API + ")")),
    ("glab", re.compile(r"\bglab\s+(?P<sub>mr\s+(?:create|merge)|release\s+create|repo\s+create|"
                        + _WRITE_API + ")")),
)
AGENT_CHOICES = ("claude", "cursor", "gemini", "generic")
INSTALL_CHOICES = ("claude", "cursor", "gemini", "codex", "opencode")
NO_HOOK = ("codex", "opencode")
EXPERIMENTAL = ("cursor", "gemini")


# ---------------------------------------------------------------- tokeniser

@dataclass
class Call:
    """One simple command of the agent's shell line."""
    prog: str
    words: List[str]
    sub: Optional[str] = None            # git: "commit"; gh/glab: "pr create"
    dir: Optional[str] = None            # from cd / git -C / env -C / GIT_DIR, relative to cwd; None = cwd
    remote: Optional[str] = None         # git push <remote>
    author: Optional[str] = None         # email from --author
    config_email: Optional[str] = None   # git -c user.email=...
    env: Dict[str, str] = field(default_factory=dict)   # VAR=val prefixes and earlier `export`s
    unset: set = field(default_factory=set)             # `unset VAR` / `env -u VAR`: not inherited
    clear: bool = False                  # `env -i`: nothing inherited
    relevant: bool = False
    fallback: bool = False               # found by the regex scan after the tokeniser failed
    alias: bool = False                  # git: unknown subcommand, maybe a config alias
    args: List[str] = field(default_factory=list)       # git: the words after the subcommand

    @property
    def author_env(self) -> Optional[str]:
        return self.env.get("GIT_AUTHOR_EMAIL") or None

    @property
    def committer_env(self) -> Optional[str]:
        return self.env.get("GIT_COMMITTER_EMAIL") or None

    def repo_dir(self, cwd: str) -> str:
        """The directory to judge. A part the hook cannot know ($(...), an unset $VAR) cuts the path
        there: the judgement falls back to what is known (in the end the cwd), never to 'not a repo'."""
        if not self.dir:
            return os.path.normpath(cwd)
        parts = os.path.join(cwd, self.dir).split(os.sep)
        for k, part in enumerate(parts):
            if "$" in part or "`" in part or "__gitpro_sub" in part:
                parts = parts[:k]
                break
        known = os.sep.join(parts)
        return os.path.normpath(known) if known else os.path.normpath(cwd)


@dataclass(frozen=True)
class Ctx:
    """What earlier commands of the line change for later ones: the dir (cd, cd -, pushd/popd), exported
    variables (export/unset, env -i) and plain shell variables (NAME=value)."""
    dir: Optional[str] = None                           # None: the cwd
    env: Tuple[Tuple[str, Optional[str]], ...] = ()     # exported (name, value); None: unset
    clear: bool = False
    shvars: Tuple[Tuple[str, str], ...] = ()            # unexported shell variables set in the line
    oldpwd: Optional[str] = "$OLDPWD"                   # for `cd -`: None is the cwd, "$OLDPWD" unknown
    dirs: Tuple[Optional[str], ...] = ()                # the pushd stack

    def derive(self, d: Optional[str] = None, env: Optional[Dict[str, str]] = None, unset=(),
               clear: bool = False) -> "Ctx":
        merged: Dict[str, Optional[str]] = {} if clear else dict(self.env)
        for name in unset:
            merged[name] = None
        merged.update(env or {})
        return replace(self, dir=_join(self.dir, d), env=tuple(merged.items()), clear=self.clear or clear)

    def value(self, name: str) -> Optional[str]:
        """$name as the shell expands it here, or None when the hook cannot know it."""
        v = dict(self.shvars).get(name)
        if v is not None:
            return v
        env = dict(self.env)
        if name in env:
            return "" if env[name] is None else env[name]
        if self.clear or name in ("PWD", "OLDPWD"):
            return None
        return os.environ.get(name)

    def exported(self, name: str) -> bool:
        env = dict(self.env)
        if name in env:
            return env[name] is not None
        return name not in dict(self.shvars) and not self.clear and name in os.environ

    def expand(self, word: str) -> str:
        """word with each $NAME / ${NAME} the hook knows replaced by its value; the others stay, so a
        repo dir is cut there (Call.repo_dir)."""
        if "$" not in word:
            return word

        def one(m: "re.Match[str]") -> str:
            v = self.value(m.group(1) or m.group(2))
            return m.group(0) if v is None else v
        out = _VAR_RE.sub(one, word)
        return out if len(out) <= MAX_VALUE else word     # `R=$R$R` doubling: stays unknown

    def assign(self, name: str, value: str) -> "Ctx":
        """NAME=value: an exported variable stays exported."""
        if self.exported(name):
            return self.derive(env={name: value})
        return replace(self, shvars=_set(self.shvars, name, value))

    def export(self, name: str, value: Optional[str] = None) -> "Ctx":
        if value is None:
            value = dict(self.shvars).get(name)
            if value is None:
                return self                # exported already, or not known
        return replace(self.derive(env={name: value}), shvars=_drop(self.shvars, name))

    def unexport(self, name: str) -> "Ctx":
        v = self.value(name) if self.exported(name) else None
        c = self.derive(unset=[name])
        return replace(c, shvars=_set(c.shvars, name, v)) if v is not None else c

    def unset_var(self, name: str) -> "Ctx":
        return replace(self.derive(unset=[name]), shvars=_drop(self.shvars, name))

    def unknown(self, name: str) -> "Ctx":
        """name holds something the hook cannot know (a `for` it does not unroll)."""
        return replace(self, shvars=_set(self.shvars, name, "$" + name))

    def cd(self, target: str) -> "Ctx":
        return replace(self, dir=_join(self.dir, target), oldpwd=self.dir)

    def back(self) -> "Ctx":
        """`cd -`."""
        return replace(self, dir=self.oldpwd, oldpwd=self.dir)

    def seed(self, shell_vars: bool) -> "Ctx":
        """The context a nested parse starts from: a child shell sees the exported variables, an eval or
        a $(...) also the plain ones. The dir is applied afterwards (_rebase)."""
        return Ctx(env=self.env, clear=self.clear, shvars=self.shvars if shell_vars else ())


def _set(pairs: tuple, name: str, value) -> tuple:
    return _drop(pairs, name) + ((name, value),)


def _drop(pairs: tuple, name: str) -> tuple:
    return tuple((k, v) for k, v in pairs if k != name)


def _apply(c: Call, ctx: Ctx) -> Call:
    """c as run after the context: its dir relative to ctx's, ctx's env under its own."""
    c.dir = _join(ctx.dir, c.dir)
    if c.clear:
        return c
    outer = {k: v for k, v in ctx.env if k not in c.env and k not in c.unset}
    c.env = {**{k: v for k, v in outer.items() if v is not None}, **c.env}
    c.unset = c.unset | {k for k, v in outer.items() if v is None}
    c.clear = ctx.clear
    return c


def _join(base: Optional[str], d: Optional[str]) -> Optional[str]:
    """d (already variable-expanded by Ctx.expand) relative to base."""
    if d is None:
        return base
    d = os.path.expanduser(d)
    if base is None or os.path.isabs(d):
        return d
    return os.path.join(base, d)


def _here_word(body: str, quoted: bool) -> str:
    """A here-document body as one shell word: single-quoted when the delimiter was quoted (nothing
    expands), else double-quoted, so a $(...) or `...` in it is still found."""
    if quoted:
        return "'" + body.replace("'", "'\"'\"'") + "'"
    return '"' + body.replace('"', '\\"') + '"'


def _drop_heredocs(cmd: str) -> str:
    """Here-documents. Outside any parentheses `<<DELIM` and its body become a here-string `<<< 'body'`,
    which a shell reading stdin (`sh <<'EOF'`) runs as its script. Inside ( or $(...) the body is
    dropped and the `<<DELIM` stays (its delimiter is a redirection target). A `<<` inside quotes or a
    comment is not a here-document."""
    if "<<" not in cmd:
        return cmd
    out: List[str] = []
    pending: List[Tuple[str, bool, bool, int]] = []     # (delimiter, <<-, quoted, its index in out or -1)
    outer: List[str] = []                 # the quote state around each open ( or $(: quotes restart inside
    i, n, q = 0, len(cmd), ""
    while i < n:
        c = cmd[i]
        if q == "'":
            q = "" if c == "'" else q
        elif c == "\\":
            out.append(cmd[i:i + 2])
            i += 2
            continue
        elif cmd.startswith("$(", i) or (c == "(" and not q):
            outer.append(q)
            q = ""
            out.append(cmd[i:i + 2] if c == "$" else c)
            i += 2 if c == "$" else 1
            continue
        elif c == ")" and not q and outer:
            q = outer.pop()
        elif q == '"':
            q = "" if c == '"' else q
        elif c in "'\"":
            q = c
        elif c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|()"):
            j = cmd.find("\n", i)
            j = n if j < 0 else j
            out.append(cmd[i:j])
            i = j
            continue
        elif c == "<" and (i == 0 or cmd[i - 1] != "<"):
            m = _HEREDOC_RE.match(cmd, i)
            if m:
                inline = not outer
                pending.append((m.group(2), m.group(0)[2:3] == "-", bool(m.group(1)), len(out) if inline else -1))
                out.append("" if inline else m.group(0))
                i = m.end()
                continue
        elif c == "\n" and pending:
            out.append(c)
            i += 1
            for delim, dash, quoted, at in pending:
                lines: List[str] = []
                while i < n:
                    j = cmd.find("\n", i)
                    end = n if j < 0 else j
                    line, i = cmd[i:end], end + 1
                    line = line.lstrip("\t") if dash else line
                    if line.strip() == delim:
                        break
                    lines.append(line)
                if at >= 0:
                    out[at] = "<<< " + _here_word("".join(x + "\n" for x in lines), quoted)
            pending = []
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _parens(cmd: str) -> Dict[int, int]:
    """Position of each "(" -> its matching ")" (quotes ignored, as the $(...) scan always did)."""
    match: Dict[int, int] = {}
    stack: List[int] = []
    for k, ch in enumerate(cmd):
        if ch == "(":
            stack.append(k)
        elif ch == ")" and stack:
            match[stack.pop()] = k
    return match


def _prepare(cmd: str, depth: int = 0) -> Tuple[str, List[str]]:
    """(text with $(...) and `...` replaced by placeholder words, comments, line continuations and the
    fd number of a redirection (`2>`) removed, the inner command strings). Linear in len(cmd). The
    placeholders carry the depth, so a nested parse never mistakes an outer one for its own."""
    cmd = _drop_heredocs(cmd)
    out: List[str] = []
    inners: List[str] = []
    parens: Optional[Dict[int, int]] = None
    ticks = True                          # False once a ` has no partner: none further on has one either
    i, n, in_s, in_d = 0, len(cmd), False, False
    while i < n:
        c = cmd[i]
        if in_s:
            out.append(c)
            in_s = c != "'"
            i += 1
        elif c == "\\" and i + 1 < n:
            if cmd[i + 1] != "\n":
                out.append(cmd[i:i + 2])
            i += 2
        elif c == "'" and not in_d:
            in_s = True
            out.append(c)
            i += 1
        elif c == '"':
            in_d = not in_d
            out.append(c)
            i += 1
        elif c == "#" and not in_d and (i == 0 or cmd[i - 1] in " \t\n;&|()"):
            j = cmd.find("\n", i)
            i = n if j < 0 else j
        elif cmd.startswith("$(", i):
            if parens is None:
                parens = _parens(cmd)
            j = parens.get(i + 1, -1)
            if j < 0:
                out.append(c)
                i += 1
                continue
            inners.append(cmd[i + 2:j])
            out.append(f"__gitpro_sub{depth}_{len(inners) - 1}__")
            i = j + 1
        elif c == "`" and ticks:
            j = i + 1
            while j < n and cmd[j] != "`":
                j += 2 if cmd[j] == "\\" else 1
            if j >= n:
                ticks = False
                out.append(c)
                i += 1
                continue
            inners.append(cmd[i + 1:j].replace("\\`", "`"))
            out.append(f"__gitpro_sub{depth}_{len(inners) - 1}__")
            i = j + 1
        elif c.isdigit() and not in_d and (i == 0 or cmd[i - 1] in " \t\n;&|()"):
            j = i
            while j < n and cmd[j].isdigit():
                j += 1
            if not (j < n and cmd[j] in "<>"):
                out.append(cmd[i:j])      # a word, not the fd of a redirection
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out), inners


def _lex(text: str) -> List[str]:
    lex = shlex.shlex(text, posix=True, punctuation_chars=PUNCT)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _fallback(cmd: str) -> List[Call]:
    calls = []
    cmd = cmd[:MAX_SCAN]
    for prog, rx in _FALLBACK:
        for m in rx.finditer(cmd):
            sub = " ".join(m.group("sub").split())
            d = m.groupdict().get("dir")
            calls.append(Call(prog, [prog, *sub.split()], sub, dir=d.strip("'\"") if d else None, relevant=True,
                              fallback=True))
    return calls


def _rebase(calls: List[Call], ctx: Ctx) -> List[Call]:
    return [_apply(c, ctx) for c in calls]


def _is_sep(tok: str) -> bool:
    """A separator token (`;`, `&&`, `|`, `(`, a newline ...), not a redirection."""
    return bool(tok) and all(ch in PUNCT for ch in tok) and "<" not in tok and ">" not in tok


def _command_follows(tok: str) -> bool:
    """Whether the token after tok is in command position."""
    return _is_sep(tok) or tok in CMD_LEAD


def _plain(word: str) -> bool:
    """A `for` word that stands for exactly itself (no glob, brace expansion or $(...))."""
    return not (re.search(r"[*?\[]", word) or re.search(r"(?<!\$)\{[^}]*(?:,|\.\.)", word)
                or "__gitpro_sub" in word)


def _unroll(tokens: List[str], budget: int = 4096) -> List[str]:
    """`for NAME in W1 .. Wn; do BODY; done` (plain words, n <= MAX_FOR) becomes `NAME=W1; BODY; ...
    NAME=Wn; BODY;`, so the body is judged once per word with NAME known. Other loops stay as they are
    (the body is judged once, NAME unknown). Linear: `do`/`done` are matched in one pass."""
    if "for" not in tokens:
        return tokens
    n = len(tokens)
    at_cmd = [True] * n                   # the token is in command position
    for k in range(1, n):
        at_cmd[k] = _command_follows(tokens[k - 1])
    done_of: Dict[int, int] = {}
    opened: List[int] = []
    for k, t in enumerate(tokens):
        if at_cmd[k] and t == "do":
            opened.append(k)
        elif at_cmd[k] and t == "done" and opened:
            done_of[opened.pop()] = k

    def header(i: int) -> Optional[Tuple[str, List[str], int]]:
        """(NAME, words, index of `do`) of the plain `for` at tokens[i], or None."""
        if i + 2 >= n or not _NAME_RE.match(tokens[i + 1]) or tokens[i + 2] != "in":
            return None
        j = i + 3
        while j < n and not _is_sep(tokens[j]) and j - i <= MAX_FOR + 3:
            j += 1
        items = tokens[i + 3:j]
        if j >= n or set(tokens[j]) - set(";\n") or len(items) > MAX_FOR or not all(_plain(w) for w in items):
            return None
        while j < n and not set(tokens[j]) - set(";\n"):
            j += 1
        return (tokens[i + 1], items, j) if j < n and tokens[j] == "do" else None

    def emit(i: int, j: int, level: int) -> List[str]:
        out: List[str] = []
        while i < j:
            if at_cmd[i] and tokens[i] == "for" and level < MAX_DEPTH:
                h = header(i)
                end = done_of.get(h[2], n) if h else n
                if h and end < j:
                    name, items, do = h
                    body = emit(do + 1, end, level + 1)
                    if len(items) * (len(body) + 2) <= budget:
                        for w in items:
                            out += [f"{name}={w}", ";", *body, ";"]
                    else:                 # too big: the loop as written (its body's own loops unrolled)
                        out += [*tokens[i:do + 1], *body, tokens[end]]
                    i = end + 1           # each token is emitted once per nesting level: linear
                    continue
            out.append(tokens[i])
            i += 1
        return out

    out = emit(0, n, 0)
    return out if len(out) <= max(n, 1) * 4 + budget else tokens


def _reserved(words: List[str]) -> Tuple[List[str], List[str], bool, int]:
    """(the command after leading reserved words, the words dropped, whether a `case` opened, how many
    `esac`s closed)."""
    i, closes = 0, 0
    while i < len(words):
        w = words[i]
        if w in ("for", "select", "case"):
            return [], words, w == "case", closes
        if w == "function":
            i += 2
            continue
        if w not in RESERVED:
            break
        closes += w == "esac"
        i += 1
    return words[i:], words[:i], False, closes


def _pipe(sep: str) -> str:
    """"|" when the separator pipes the command into the next one, "&" when it runs it in the
    background, else ""."""
    s = sep.lstrip("\n")
    if s.startswith(("||", "&&")):
        return ""
    return s[:1] if s[:1] in ("|", "&") else ""


def parse_command(cmd: str, _depth: int = 0, _ctx: Optional[Ctx] = None) -> List[Call]:
    """Every simple command in a shell line, in order, with `.relevant` set on the identity-relevant
    ones. Never runs anything; never raises (an unbalanced quote or a huge command falls back to a regex
    scan)."""
    if _depth > MAX_DEPTH:
        return []
    if len(cmd) > MAX_PARSE:
        return _fallback(cmd)
    try:
        text, inners = _prepare(cmd, _depth)
        tokens = _unroll(_lex(text))
    except ValueError:
        return _fallback(cmd)
    calls: List[Call] = []
    words: List[str] = []
    stack: List[Ctx] = []
    cases: List[int] = []                 # len(stack) at each open `case`: its `pat)` closes no subshell
    cur = _ctx or Ctx()
    skip: Optional[str] = None            # the redirection whose target comes next
    herestr: Optional[str] = None         # `<<< text` (or a here-document) of the current command
    piped: Optional[str] = None           # what `echo/printf/cat TEXT |` feeds the next command
    after_pipe = False                    # the current command is the last of a pipeline

    def flush(sep: str) -> None:
        nonlocal cur, herestr, piped, after_pipe
        w = list(words)
        words.clear()
        stdin = herestr if herestr is not None else piped
        herestr = None
        pipe = _pipe(sep)
        if not w:
            after_pipe = after_pipe or pipe == "|"
            return
        sub = pipe or after_pipe          # each part of a pipeline, and a `&` job, runs in a subshell
        after_pipe = pipe == "|"
        rest, dropped, opened, closed = _reserved(w)
        for x in dropped:
            calls.extend(_substitutions(x, cur, inners, _depth))
        if dropped[:1] in (["for"], ["select"]) and len(dropped) > 1 and _NAME_RE.match(dropped[1]):
            cur = cur.unknown(dropped[1])  # a loop _unroll left alone: its variable is not known
        del cases[max(0, len(cases) - closed):]
        if opened:
            cases.append(len(stack))
        piped = None
        if rest:
            found, nxt = _simple(rest, cur, inners, _depth, stdin)
            calls.extend(found)
            if not sub:
                cur = nxt
            if pipe == "|":
                piped = _echo_text(rest, stdin)

    for tok in tokens:
        if skip is not None:              # a redirection target is not a command word
            if skip == "<<<":
                herestr = tok
            skip = None
            calls.extend(_substitutions(tok, cur, inners, _depth))
            continue
        if tok and all(ch in PUNCT for ch in tok):
            opens = tok.endswith("(") and len(tok) > 1 and tok[-2] in "<>"   # <(...) process substitution
            redirect = ("<" in tok or ">" in tok) and not opens
            seps = ";\n()" if redirect else PUNCT
            if any(ch in seps for ch in tok if ch not in "<>"):
                if tok[0] == ")" and cases and cases[-1] == len(stack) and words[:1] != ["esac"]:
                    words.clear()         # a case pattern `pat)`
                else:
                    flush(tok)
                for ch in tok:
                    if ch == "(":
                        stack.append(cur)
                    elif ch == ")" and not (cases and cases[-1] == len(stack)) and stack:
                        cur = stack.pop()
            skip = tok if redirect else None
            continue
        words.append(tok)
    flush("")
    return calls


def _substitutions(word: str, cur: Ctx, inners: List[str], depth: int) -> List[Call]:
    out: List[Call] = []
    for m in _SUB_RE.finditer(word):
        k = int(m.group(2))
        if int(m.group(1)) == depth and k < len(inners):    # else: an outer level's, handed on in a string
            out += _rebase(parse_command(inners[k], depth + 1, cur.seed(True)), cur)
    return out


@dataclass
class _Stripped:
    words: List[str]                      # from the real program on
    env: Dict[str, str]                   # VAR=val prefixes (also env's)
    dir: Optional[str] = None             # env -C
    unset: set = field(default_factory=set)
    clear: bool = False


def _strip(words: List[str], expand: Callable[[str], str] = lambda w: w) -> _Stripped:
    """Skip VAR=val prefixes and wrappers like env/sudo/nice/flock/watch; `flock F -c S` and `watch CMD`
    become `sh -c S`. expand: $VAR expansion for the prefix values and env -C."""
    s = _Stripped([], {})
    i = 0
    while True:
        while i < len(words) and _ASSIGN_RE.match(words[i]):
            k, _, v = words[i].partition("=")
            s.env[k] = expand(v)
            s.unset.discard(k)
            i += 1
        if i >= len(words):
            break
        name = os.path.basename(words[i])
        if name not in WRAPPERS:
            break
        i += 1
        takes = WRAPPERS[name]
        seen: List[str] = []
        while i < len(words) and words[i].startswith("-"):
            a = words[i]
            i += 1
            if a == "--":
                break
            seen.append(a)
            if name == "command" and a in ("-v", "-V"):
                return s                  # `command -v git` only looks git up
            if name == "env":
                if a in ("-", "-i", "--ignore-environment"):
                    s.env.clear()
                    s.unset.clear()
                    s.clear = True
                    continue
                var = (words[i] if a in ("-u", "--unset") and i < len(words) else
                       a.split("=", 1)[1] if a.startswith("--unset=") else
                       a[2:] if a.startswith("-u") and len(a) > 2 else None)
                if var is not None:
                    i += a in ("-u", "--unset")
                    s.env.pop(var, None)
                    s.unset.add(var)
                    continue
                if a in ("-C", "--chdir") and i < len(words):
                    s.dir = _join(s.dir, expand(words[i]))
                    i += 1
                    continue
                if a.startswith("--chdir="):
                    s.dir = _join(s.dir, expand(a.split("=", 1)[1]))
                    continue
                if a in ("-S", "--split-string") and i < len(words):
                    try:
                        words = words[:i] + shlex.split(words[i]) + words[i + 1:]
                    except ValueError:
                        i += 1
                    continue
            if a in takes:
                i += 1
        if name == "timeout" and i < len(words):
            i += 1                        # the duration
        elif name == "flock":
            i += 1                        # the lock file (or fd)
            while i < len(words) and words[i].startswith("-"):
                a = words[i]
                i += 1
                if a in ("-c", "--command"):
                    s.words = ["sh", "-c", words[i]] if i < len(words) else []
                    return s
                if a in takes:
                    i += 1
        elif name == "watch" and not any(a in ("-x", "--exec") for a in seen):
            s.words = ["sh", "-c", " ".join(words[i:])] if i < len(words) else []
            return s
    s.words = words[i:]
    return s


def _shell_options(args: List[str]) -> Tuple[int, str]:
    """(index of the first operand, the short option letters given) of a shell's arguments."""
    i, letters = 0, ""
    while i < len(args):
        a = args[i]
        if a == "--":
            return i + 1, letters
        if a in ("-o", "+o", "-O", "+O"):
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        if len(a) > 1 and a[0] in "-+":
            letters += a[1:] if a[0] == "-" else ""
            i += 1
            continue
        break
    return i, letters


def _shell_script(args: List[str], stdin: Optional[str] = None) -> Optional[str]:
    """The script of `sh -c SCRIPT` (also -lc, -ec, ...); with stdin (a here-string, a here-document or
    piped echo/cat), the script a shell without operands (or with -s) reads; else None."""
    i, letters = _shell_options(args)
    if "c" in letters:
        return args[i] if i < len(args) else None
    if stdin is not None and ("s" in letters or i >= len(args)):
        return stdin
    return None


def _echo_text(words: List[str], stdin: Optional[str] = None) -> Optional[str]:
    """The text `echo ...` / `printf ...` writes (roughly: escapes \\n and \\t applied), what `cat` passes
    on from its stdin, or None."""
    rest = _strip(words).words
    prog = os.path.basename(rest[0]) if rest else ""
    args = rest[1:]
    if prog == "cat":
        return stdin if all(a == "-" for a in args) else None
    if prog == "echo":
        while args and re.match(r"^-[neE]+$", args[0]):
            args = args[1:]
    elif prog == "printf":
        args = args[1:] if args[:1] == ["--"] else args
    else:
        return None
    return " ".join(args).replace("\\n", "\n").replace("\\t", "\t") if args else None


def _declare(prog: str, args: List[str], cur: Ctx) -> Ctx:
    """export / declare / typeset / local / readonly: -x exports, +x and `export -n` unexport."""
    export, unexport, names = prog == "export", False, []
    for w in args:
        if w[:1] in ("-", "+") and len(w) > 1 and w != "--":
            letters = w[1:]
            if set(letters) & set("fFp"):
                return cur                # functions, or only printing
            export = export or (w[0] == "-" and "x" in letters)
            unexport = unexport or (w[0] == "+" and "x" in letters) or (prog == "export" and "n" in letters)
        elif w != "--":
            names.append(w)
    for w in names:
        name, eq, value = w.partition("=")
        if not _NAME_RE.match(name):
            continue
        v = cur.expand(value) if eq else None
        if unexport:
            cur = cur.unexport(name)
            cur = cur.assign(name, v) if v is not None else cur
        elif export:
            cur = cur.export(name, v)
        elif v is not None:
            cur = cur.assign(name, v)
    return cur


def _simple(words: List[str], cur: Ctx, inners: List[str], depth: int, stdin: Optional[str] = None
            ) -> Tuple[List[Call], Ctx]:
    """The calls of one simple command, and the context for the commands after it."""
    out: List[Call] = []
    for w in words:
        out += _substitutions(w, cur, inners, depth)
    if all(_ASSIGN_RE.match(w) for w in words):        # NAME=value ... alone: shell variables
        for w in words:
            k, _, v = w.partition("=")
            cur = cur.assign(k, cur.expand(v))
        return out, cur
    s = _strip(words, cur.expand)
    rest = s.words
    if not rest:
        return out, cur
    prog = os.path.basename(rest[0])
    if prog in ("cd", "pushd", "popd"):
        out.append(Call(prog, rest, dir=cur.dir))
        targets = [cur.expand(w) for w in rest[1:] if not (w[:1] in ("-", "+") and w != "-")]
        turns = any(re.match(r"^[+-]\d+$", w) for w in rest[1:])
        if prog == "popd" or (prog == "pushd" and not targets):
            if turns or not cur.dirs:
                return out, cur           # `popd +N` drops an entry; an empty stack is an error
            top = cur.dirs[-1]
            dirs = cur.dirs[:-1] + ((cur.dir,) if prog == "pushd" else ())
            return out, replace(cur, dir=top, dirs=dirs, oldpwd=cur.dir)
        target = targets[0] if targets else "~"
        nxt = cur.back() if target == "-" else cur.cd(target)
        return out, (replace(nxt, dirs=cur.dirs + (cur.dir,)) if prog == "pushd" else nxt)
    if prog == "unset":
        if "-f" in rest[1:]:
            return out, cur
        for w in rest[1:]:
            cur = cur if w.startswith("-") else cur.unset_var(w)
        return out, cur
    if prog in ("export", "declare", "typeset", "local", "readonly"):
        return out, _declare(prog, rest[1:], cur)
    ctx = cur.derive(s.dir, s.env, s.unset, s.clear)
    script = None
    if prog in SHELLS:
        script = _shell_script(rest[1:], stdin)
        seed = ctx.seed(False)
    elif prog == "eval":
        script = " ".join(rest[1:])
        seed = ctx.seed(True)
    if script is not None:
        return out + _rebase(parse_command(script, depth + 1, seed), ctx), cur
    call = _apply(Call(prog, rest), ctx)
    if prog in ("git", "hub"):
        script = (_git_call if prog == "git" else _hub_call)(call, cur.expand)
        if script is not None:            # `git -c alias.x='!...' x`: the alias's shell command, parsed only
            return out + _rebase(parse_command(script, depth + 1, ctx.seed(False)), replace(ctx, dir=call.dir)), cur
    elif prog in ("gh", "glab"):
        _forge_call(call)
    out.append(call)
    return out, cur


def _email(value: str) -> Optional[str]:
    m = re.search(r"<([^<>]*)>", value)
    if m:
        return m.group(1).strip() or None
    return value.strip() if "@" in value else None


def _positionals(args: List[str], takes: set) -> List[str]:
    out, i = [], 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            out += args[i:]
            break
        if a.startswith("-") and a != "-":
            if a in takes:
                i += 1
            continue
        out.append(a)
    return out


def _git_call(call: Call, expand: Callable[[str], str] = lambda w: w) -> Optional[str]:
    """Read git's global options and the subcommand into call. Returns the shell command of a
    `-c alias.X='!...'` alias when the subcommand is one (to be parsed, never run), else None."""
    args = call.words[1:]
    i, d = 0, call.dir
    git_dir, work_tree = call.env.get("GIT_DIR"), None
    aliases: Dict[str, str] = {}
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        i += 1
        if a == "-C" and i < len(args):
            d = _join(d, expand(args[i]))
            i += 1
        elif a in ("--git-dir", "--work-tree") and i < len(args):
            v = expand(args[i])
            git_dir, work_tree = (v, work_tree) if a == "--git-dir" else (git_dir, v)
            i += 1
        elif a.startswith("--git-dir="):
            git_dir = expand(a.split("=", 1)[1])
        elif a.startswith("--work-tree="):
            work_tree = expand(a.split("=", 1)[1])
        elif a == "-c" and i < len(args):
            key, _, value = args[i].partition("=")
            if key.lower() == "user.email":
                call.config_email = value
            elif key.lower().startswith("alias."):
                aliases[key[len("alias."):].lower()] = value
            i += 1
        elif a in ("--namespace", "--config-env", "--super-prefix"):
            i += 1
    call.dir = _join(d, git_dir) if git_dir else _join(d, work_tree)
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1:]
    seen = set()
    while sub not in GIT_BUILTINS and sub.lower() in aliases and sub.lower() not in seen:
        seen.add(sub.lower())             # `git -c alias.ci=commit ci`
        value = aliases[sub.lower()].lstrip()
        if value.startswith("!"):
            return value[1:] + "".join(" " + shlex.quote(a) for a in rest)
        try:
            parts = shlex.split(value)
        except ValueError:
            break
        if not parts:
            break
        sub, rest = parts[0], parts[1:] + rest
    _git_sub(call, sub, rest)
    return None


# value-taking options of the identity-relevant git commands: (short letters taking a value, glued or
# the next word; short letters whose optional value is only ever glued on; long options taking the next
# word unless written --opt=value)
_GIT_OPTS: Dict[str, Tuple[str, str, set]] = {
    "commit": ("mFCct", "uS", {"--author", "--date", "--message", "--file", "--template", "--reuse-message",
                               "--reedit-message", "--fixup", "--squash", "--cleanup", "--trailer",
                               "--pathspec-from-file"}),
    "push": ("o", "", {"--push-option", "--receive-pack", "--exec", "--repo"}),
    "merge": ("mFsX", "S", {"--message", "--file", "--strategy", "--strategy-option", "--into-name",
                            "--cleanup"}),
    "rebase": ("sXx", "SC", {"--onto", "--strategy", "--strategy-option", "--exec", "--whitespace"}),
    "cherry-pick": ("mX", "S", {"--mainline", "--strategy", "--strategy-option", "--cleanup"}),
    "revert": ("mX", "S", {"--mainline", "--strategy", "--strategy-option", "--cleanup"}),
    "am": ("", "pCS", {"--directory", "--exclude", "--include", "--patch-format", "--whitespace"}),
    "tag": ("mFu", "n", {"--message", "--file", "--local-user", "--sort", "--format", "--contains",
                         "--no-contains", "--points-at", "--merged", "--no-merged", "--cleanup"}),
    "notes": ("", "", {"--ref"}),
}


def _git_opts(sub: str, args: List[str]) -> Tuple[List[Tuple[str, Optional[str]]], List[str]]:
    """(the options of `git SUB ARGS` as (name, value), the positionals). The value of an option that
    takes one is never read as an option (`commit -m --dry-run` commits); `--` ends the options."""
    takes, glued, longs = _GIT_OPTS.get(sub, ("", "", set()))
    opts: List[Tuple[str, Optional[str]]] = []
    pos: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            pos += args[i:]
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if eq:
                opts.append((name, value))
            elif name in longs:
                opts.append((name, args[i] if i < len(args) else None))
                i += 1
            else:
                opts.append((name, None))
        elif a.startswith("-") and len(a) > 1:
            for k in range(1, len(a)):
                ch = a[k]
                if ch in takes:
                    value: Optional[str] = a[k + 1:]
                    if not value:
                        value = args[i] if i < len(args) else None
                        i += 1
                    opts.append(("-" + ch, value))
                    break
                if ch in glued:
                    opts.append(("-" + ch, a[k + 1:] or None))
                    break
                opts.append(("-" + ch, None))
        else:
            pos.append(a)
    return opts, pos


def _git_sub(call: Call, sub: str, rest: List[str]) -> None:
    """Set sub/relevant/author/remote for `git SUB REST...`."""
    call.sub, call.args, call.relevant = sub, rest, False
    call.alias = sub not in GIT_BUILTINS and bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9-]*$", sub))
    if sub not in GIT_SUBS:
        return
    opts, pos = _git_opts(sub, rest)
    names = {n for n, _ in opts}
    if names & {"-h", "--help"}:
        return
    if sub in UNDOABLE and names & UNDO_FLAGS:
        return
    if sub == "commit" and "--dry-run" in names or sub == "push" and names & {"-n", "--dry-run"}:
        return
    if sub == "tag":
        call.relevant = not names & {"-l", "--list", "-d", "--delete", "-v", "--verify", "-n"} and bool(pos)
        return
    if sub == "notes":
        call.relevant = bool(pos) and pos[0] in ("add", "append", "copy", "edit", "merge")
        return
    call.relevant = True
    for name, value in opts:
        if name == "--author" and value is not None:
            call.author = _email(value)
    if sub == "push":
        call.remote = pos[0] if pos else None


def _field_values(args: List[str]) -> List[str]:
    """The key=value of every -f/-F/--field/--raw-field of `gh api` / `glab api`."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        i += 1
        if a in ("-f", "-F", "--field", "--raw-field") and i < len(args):
            out.append(args[i])
            i += 1
        elif re.match(r"^-[fF].", a):
            out.append(a[2:])
        elif a.startswith(("--field=", "--raw-field=")):
            out.append(a.split("=", 1)[1])
    return out


def _api_writes(args: List[str]) -> bool:
    pos = _positionals(args, API_TAKES)
    if len(pos) > 1 and pos[1] == "graphql":
        # always POST; a write only when the query is a mutation (unknown -> a write)
        queries = [v.split("=", 1)[1] for v in _field_values(args) if v.split("=", 1)[0] == "query"]
        if not queries or "--input" in args:
            return True
        return any(q.startswith("@") or q.lstrip().startswith("mutation") for q in queries)
    method, i = None, 0
    while i < len(args):
        a = args[i]
        i += 1
        if a in ("-X", "--method") and i < len(args):
            method = args[i].upper()
            i += 1
        elif a.startswith("--method="):
            method = a.split("=", 1)[1].upper()
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:].upper()
    fields_ = bool(_field_values(args)) or any(a == "--input" or a.startswith("--input=") for a in args)
    return method in WRITE_METHODS or (method is None and fields_)


def _forge_call(call: Call) -> None:
    args = call.words[1:]
    pos = _positionals(args, {"-R", "--repo"})
    if not pos:
        return
    if pos[0] == "api":
        call.sub, call.relevant = "api", _api_writes(args)
        return
    call.sub = " ".join(pos[:2])
    subs = GH_SUBS if call.prog == "gh" else GLAB_SUBS
    call.relevant = tuple(pos[:2]) in subs


def _hub_call(call: Call, expand: Callable[[str], str] = lambda w: w) -> Optional[str]:
    """hub wraps git and adds gh-like commands."""
    script = _git_call(call, expand)
    if script is not None or call.relevant:
        call.prog = "git"
        return script
    pos = _positionals(call.words[1:], {"-C", "-c", "--git-dir", "--work-tree"})
    if not pos or pos[0] not in HUB_COMMANDS:
        return None
    call.alias = False
    if pos[0] == "api":
        call.sub, call.relevant = "api", _api_writes(call.words[1:])
        return None
    for n in (2, 1):
        if tuple(pos[:n]) in HUB_SUBS:
            call.sub, call.relevant = " ".join(pos[:n]), True
            return None
    call.sub = pos[0]
    return None


# ---------------------------------------------------------------- adapters

class BadInput(ValueError):
    """The hook payload is not what the adapter expects."""


@dataclass(frozen=True)
class Decision:
    headline: str
    cause: str
    fix: str
    repo: str
    why: str = ""                        # the denial record's reason code (not `reason`: that is the text below)
    codes: Tuple[str, ...] = ()          # problem codes of the repo's report
    action: str = ""                     # the git subcommand or program

    @property
    def reason(self) -> str:
        # no bypass hint here (DECISIONS Q6): the agent asks the user, the user knows GITPRO_SKIP
        return (f"gitpro: {self.headline}. cause: {self.cause}. Ask the user before changing anything; "
                f"the fix is `{self.fix}`.")


def _payload_dict(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise BadInput("cannot parse hook input")
    return payload


def _cwd_of(payload: dict) -> Optional[str]:
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _tool_command(payload: object, tool: str) -> Optional[Tuple[str, Optional[str]]]:
    p = _payload_dict(payload)
    if p.get("tool_name") != tool:
        return None
    ti = p.get("tool_input")
    if not isinstance(ti, dict) or not isinstance(ti.get("command"), str):
        raise BadInput("cannot parse hook input")
    return ti["command"], _cwd_of(p)


def _flat_command(payload: object) -> Optional[Tuple[str, Optional[str]]]:
    p = _payload_dict(payload)
    if not isinstance(p.get("command"), str):
        raise BadInput("cannot parse hook input")
    return p["command"], _cwd_of(p)


def _deny_claude(d: Decision) -> Tuple[str, int]:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": d.reason}}, ensure_ascii=False), 0


def _deny_cursor(d: Decision) -> Tuple[str, int]:
    return json.dumps({"permission": "deny", "userMessage": f"gitpro: {d.headline}",
                       "agentMessage": d.reason}, ensure_ascii=False), 0


def _deny_gemini(d: Decision) -> Tuple[str, int]:
    return json.dumps({"decision": "deny", "reason": d.reason}, ensure_ascii=False), 0


@dataclass(frozen=True)
class Adapter:
    """One agent's hook envelope. verified=False: field names not yet checked against the tool's docs."""
    name: str
    title: str
    verified: bool
    extract: Callable[[object], Optional[Tuple[str, Optional[str]]]]   # -> (command, cwd) or None: ignore
    deny: Callable[[Decision], Tuple[str, int]]                         # -> (stdout, exit code)


ADAPTERS: Dict[str, Adapter] = {
    "claude": Adapter("claude", "Claude Code", True, lambda p: _tool_command(p, "Bash"), _deny_claude),
    "cursor": Adapter("cursor", "Cursor", False, _flat_command, _deny_cursor),
    "gemini": Adapter("gemini", "Gemini CLI", False, lambda p: _tool_command(p, "run_shell_command"),
                      _deny_gemini),
    "generic": Adapter("generic", "a generic agent", False, _flat_command, lambda d: ("", 1)),
}


# ---------------------------------------------------------------- judging

def relevant_calls(payload: object, agent: str = "claude", cwd: Optional[str] = None
                   ) -> Tuple[List[Call], str]:
    """(the identity-relevant calls of the payload's command, the cwd they run in). Raises BadInput."""
    found = ADAPTERS[agent].extract(payload)
    if found is None:
        return [], cwd or os.getcwd()
    command, payload_cwd = found
    calls = [c for c in parse_command(command) if c.relevant or c.alias]
    return calls, cwd or payload_cwd or os.getcwd()


def decide(payload: object, cfg, agent: str = "claude", cwd: Optional[str] = None) -> Optional[Decision]:
    """The deny decision for a hook payload, or None (no opinion). cfg is a Config, None, or a callable
    returning one (called only when a relevant command is found). [agents] hook = "off" and GITPRO_SKIP
    give None; "warn" vs "block" is the caller's business. Raises BadInput for a malformed payload."""
    calls, cwd = relevant_calls(payload, agent, cwd)
    if not calls or os.environ.get(SKIP_ENV):
        return None
    cfg = cfg() if callable(cfg) else cfg
    if cfg is None or cfg.agents.hook == "off":
        return None
    return judge(calls, cwd, cfg)


def judge(calls: List[Call], cwd: str, cfg) -> Optional[Decision]:
    """The first call whose repo is wrong for it, as a Decision; None when all are fine."""
    from . import resolve
    own = {e.lower() for e in resolve.own_emails(cfg)}
    for c in calls:
        top = resolve.is_repo(Path(c.repo_dir(cwd)))
        if top is None:
            continue                       # not a repo: git will say so itself
        if not c.relevant and not _expand_alias(c, top):
            continue
        env = {k: v for k, v in _environ(c).items() if not k.startswith("GIT_AUTHOR_")}
        env.update((k, v) for k, v in c.env.items() if k.startswith("GIT_COMMITTER_"))
        remote = _remote_arg(c, top)
        rep = resolve.compare(top, cfg, remote=remote, env=env)
        d = _judge_one(c, rep, cfg, own, pushed_to=remote)
        if d:
            return d
    return None


def _environ(c: Call) -> Dict[str, str]:
    """The environment c inherits: the hook's, minus what the command line unset."""
    if c.clear:
        return {}
    return {k: v for k, v in os.environ.items() if k not in c.unset}


def _expand_alias(c: Call, top: Path) -> bool:
    """Resolve an unknown git subcommand through the repo's alias.<name> (read-only `git config --get`;
    `!` shell aliases are not followed). True when the expanded command is relevant."""
    seen = set()
    while c.alias and not c.relevant and c.sub and c.sub.lower() not in seen and len(seen) < 5:
        seen.add(c.sub.lower())
        try:
            r = _git_out(top, "config", "--get", f"alias.{c.sub}", timeout=ALIAS_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return False                   # slow or missing git: no opinion
        value = r.stdout.strip() if r.returncode == 0 else ""
        try:
            parts = shlex.split(value)
        except ValueError:
            return False
        if not parts or value.startswith("!"):
            return False
        _git_sub(c, parts[0], parts[1:] + c.args)
    return c.relevant


def _remote_arg(c: Call, top: Path) -> Optional[str]:
    if c.prog != "git" or c.sub != "push" or not c.remote:
        return None
    if ":" in c.remote or "/" in c.remote:
        return c.remote                    # a url (or a path) pushed to directly
    from . import resolve
    return c.remote if c.remote in resolve.remotes(top) else None


def _norm(email: Optional[str]) -> Optional[str]:
    return email.strip().lower() if email and email.strip() else None


def _label(email: str, cfg) -> str:
    for i in cfg.identities:
        if _norm(i.email) == _norm(email):
            return i.id
    return "[default]"


def _who_is_expected(rep) -> str:
    e = rep.expected
    if e.id is None:
        return f"no identity matches this repo, so [default] <{e.email}> applies"
    from .resolve import redact_url
    url = redact_url(e.match.url)
    where = f"{e.match.remote} ({url})" if e.match.remote else (url or "the remote")
    return f"{where} belongs to identity {e.id} <{e.email}>"


def _what(c: Call) -> str:
    return c.sub if c.prog == "git" else f"{c.prog} {c.sub}"


def _judge_one(c: Call, rep, cfg, own: set, pushed_to: Optional[str] = None) -> Optional[Decision]:
    from .resolve import identity_conflicts
    exp = rep.expected
    want = _norm(exp.email)
    base, base_src = (c.config_email, "config") if c.config_email else (rep.effective.email.value, "effective")
    environ = _environ(c)
    committer, c_src = next(((v, s) for v, s in ((c.committer_env, "prefix"),
                                                 (environ.get("GIT_COMMITTER_EMAIL"), "environ")) if v),
                            (base, base_src))
    author, a_src = next(((v, s) for v, s in ((c.author, "author"), (c.author_env, "prefix"),
                                              (environ.get("GIT_AUTHOR_EMAIL"), "environ")) if v),
                         (base, base_src))
    c_bad, a_bad = identity_conflicts(want, own, _norm(author), _norm(committer) or "")
    if want is None:
        c_bad = _norm(committer) is not None
        a_bad = _norm(author) in own
    triggers = [p for p in rep.problems if p.code in TRIGGERS]
    if not (c_bad or a_bad or triggers):
        return None
    top = str(rep.repo)
    what = _what(c)
    commits = c.prog == "git" and c.sub != "push"
    codes = tuple(p.code for p in rep.problems)
    action = c.sub if c.prog == "git" else c.prog
    why = ("no_identity" if exp.email is None else "committer" if c_bad else "author" if a_bad else "problem")
    if exp.email is None:
        from .resolve import redact_url
        url = redact_url(next(iter(rep.effective.remotes.values()), None)) or top
        headline = f"no identity matches this repo ({url}) and [default] has no email"
    elif any(p.code == "ambiguous" for p in triggers):
        headline = f"this {what} runs in a repo whose remotes belong to different identities"
    elif c_bad or a_bad:
        email, verb = (committer, "committed") if c_bad else (author, "authored")
        email = email or "(no email)"
        lead = (f"this {what} would be {verb} as {email}" if commits else
                f"this push comes from a repo that commits as {email}" if c.prog == "git" else
                f"this {what} runs in a repo that commits as {email}")
        headline = f"{lead} but {_who_is_expected(rep)}"
    else:
        headline = f"this {what} would not use {exp.id or '[default]'} {exp.name or ''} <{exp.email}>"
        headline = headline.replace("  ", " ")
    if c_bad and c_src == "effective" and pushed_to and not triggers:
        # the repo's other problems were computed for this remote's identity; they would mislead here
        return Decision(headline, f"this repo's commits use {committer or '(no email)'} and {pushed_to} belongs "
                                  f"to {exp.id or '[default]'}", f"gitpro why {shlex.quote(top)}", top,
                        "pushed_to", codes, action)
    cause, fix = _cause(c, c_bad, c_src, committer, a_bad, a_src, author, rep, cfg, environ)
    return Decision(headline, cause, fix, top, why, codes, action)


def _command_text(c: Call, limit: int = 200) -> str:
    text = shlex.join(c.words)
    return text if len(text) <= limit else shlex.join(c.words[:3]) + " ..."


def _env_fix(c: Call, rep, environ: Dict[str, str], author: bool) -> str:
    """The command with the identity variables it inherits wrong set right as prefixes (never a bare
    `unset`: in a hook's shell that changes nothing for the user)."""
    exp = rep.expected
    want = {"GIT_COMMITTER_EMAIL": exp.email, "GIT_COMMITTER_NAME": exp.name,
            "GIT_AUTHOR_EMAIL": exp.email if author else None, "GIT_AUTHOR_NAME": None}
    assigns = dict(c.env)
    for var, value in want.items():
        have = assigns.get(var) or environ.get(var)
        if have and value and have != value:
            assigns[var] = value
    for var in ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        if environ.get(var) and var not in assigns:
            assigns[var] = "0" if var == "GIT_CONFIG_COUNT" else ""
    prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in assigns.items())
    return f"{prefix} {_command_text(c)}".strip()


_SHELL_NOTE = "; it comes from the user's shell, where only the user can remove it"


def _cause(c: Call, c_bad: bool, c_src: str, committer: Optional[str], a_bad: bool, a_src: str,
           author: Optional[str], rep, cfg, environ: Dict[str, str]) -> Tuple[str, str]:
    exp = rep.expected
    if c_bad and c_src == "prefix":
        return (f"the command sets GIT_COMMITTER_EMAIL={committer}",
                f"drop GIT_COMMITTER_EMAIL={committer} from the command")
    if c_bad and c_src == "environ":
        return (f"GIT_COMMITTER_EMAIL={committer} is set in the environment{_SHELL_NOTE}",
                _env_fix(c, rep, environ, a_bad and a_src == "environ"))
    if c_bad and c_src == "config":
        return f"the command sets -c user.email={committer}", f"drop -c user.email={committer} from the command"
    if not c_bad and a_bad and a_src != "effective":
        how = {"author": "--author", "prefix": "GIT_AUTHOR_EMAIL= on the command",
               "environ": "GIT_AUTHOR_EMAIL in the environment", "config": "-c user.email"}[a_src]
        fix = {"author": f"drop --author from the command, or use {exp.name} <{exp.email}>",
               "prefix": f"drop GIT_AUTHOR_EMAIL={author} from the command",
               "environ": _env_fix(c, rep, environ, True) if a_src == "environ" else "",
               "config": f"drop -c user.email={author} from the command"}[a_src]
        note = _SHELL_NOTE if a_src == "environ" else ""
        return f"the author {author} is your {_label(author or '', cfg)} email ({how}){note}", fix
    ordered = [p for p in rep.problems if p.code in TRIGGERS] + [p for p in rep.problems if p.fix]
    why = f"gitpro why {shlex.quote(str(rep.repo))}"
    if ordered:
        p = ordered[0]
        if p.code == "env_override":
            return p.message, _env_fix(c, rep, environ, False)
        return p.message, p.fix or why
    return f"git resolves user.email to {rep.effective.email.value or 'nothing'} here", why


# ---------------------------------------------------------------- agent-check

class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):              # never exit 2 from the hook: that would block the agent
        raise _ArgError(message)


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--agent", choices=AGENT_CHOICES, default="claude",
                   help="hook envelope (cursor, gemini, generic: experimental)")
    p.add_argument("--cwd", metavar="DIR", help="judge commands relative to DIR instead of the payload's cwd")
    p.set_defaults(func=_cmd_check)


def _warn(msg: str) -> None:
    print(f"⚠ gitpro agent-check: {msg}", file=sys.stderr)


def _load_config(explicit: Optional[Path], home: Path):
    from . import config
    try:
        path, how = config.find(explicit, Path(home).expanduser())
    except config.ConfigError:
        return None                        # no config: nothing to judge
    return config.load_all(path, how)


def _check(args: argparse.Namespace, text: str) -> int:
    adapter = ADAPTERS[args.agent]
    try:
        payload = json.loads(text)
        calls, cwd = relevant_calls(payload, args.agent, args.cwd)
    except ValueError:                     # bad JSON, BadInput
        _warn("cannot parse hook input")
        return 0
    if not calls:
        return 0
    if os.environ.get(SKIP_ENV):
        print(f"gitpro agent-check: skipped ({SKIP_ENV})", file=sys.stderr)
        return 0
    cfg = _load_config(args.config, args.home)
    if cfg is None or cfg.agents.hook == "off":
        return 0
    d = judge(calls, cwd, cfg)
    if d is None:
        return 0
    if cfg.agents.hook == "warn":
        msg = f"⚠ gitpro: {d.headline}; fix: {d.fix}"
        if args.agent == "claude":       # shown to the user; no permission decision: the normal flow goes on
            sys.stdout.write(json.dumps({"systemMessage": msg}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        print(msg, file=sys.stderr)
        return 0
    _record_denial(args.agent, d)
    out, rc = adapter.deny(d)
    if out:
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    print(f"✗ {d.reason}", file=sys.stderr)
    return rc


def _record_denial(agent: str, d: Decision) -> None:
    from . import incident              # lazy: stdlib only, but most checks never deny
    incident.record_denial("agent-check", agent, d.action, d.why, d.codes)


def _fail_open(func: Callable[[], int]) -> int:
    try:
        return func()
    except SystemExit as e:                # --help
        return 0 if not e.code else _fail_open_msg("bad arguments")
    except _ArgError as e:
        return _fail_open_msg(str(e))
    except Exception as e:                 # fail open: never stop the agent
        _record_crash(e)
        return _fail_open_msg(str(e) or type(e).__name__)


def _record_crash(e: Exception) -> None:
    try:
        from . import incident
        incident.record_crash("agent-check", e)
    except Exception:                      # noqa: BLE001
        pass


def _fail_open_msg(msg: str) -> int:
    try:
        _warn(msg)
    except Exception:
        pass
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    return _fail_open(lambda: _check(args, sys.stdin.read()))


def main(argv: List[str]) -> int:
    """Hot entry point: argv is ["agent-check", ...]. Always exits 0, except a generic-agent deny (1)."""
    def run() -> int:
        from . import args as gargs
        p = _Parser(prog=f"{PROG} agent-check", parents=[gargs.common()], epilog=BYPASS_HELP)
        add_arguments(p)
        return _check(p.parse_args(argv[1:]), sys.stdin.read())
    return _fail_open(run)


# ---------------------------------------------------------------- the hook command

def hook_command(abs_path: Optional[str] = None, agent: str = "claude") -> str:
    """The shell line an agent runs. gitpro from PATH first, then abs_path; without either it exits 0
    silently. gitpro's own exit status never reaches the agent (exit 2 would block it)."""
    run = f"gitpro agent-check --agent {agent}"
    if not abs_path:
        inner = f"command -v gitpro >/dev/null 2>&1 || exit 0; {run}; exit 0"
    else:
        q = shlex.quote(abs_path)
        inner = (f"command -v gitpro >/dev/null 2>&1 && {{ {run}; exit 0; }}; "
                 f"[ -x {q} ] && {{ {q} agent-check --agent {agent}; exit 0; }}; exit 0")
    return "sh -c " + shlex.quote(inner)


def _command_v(login: bool) -> Optional[str]:
    """Where `sh [-l]c 'command -v gitpro'` finds gitpro, or None."""
    try:
        r = subprocess.run(["sh", "-lc" if login else "-c", "command -v gitpro"], capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    paths = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("/")]
    return paths[-1] if paths else None


def gitpro_path() -> Optional[str]:
    """The absolute path the installed hook falls back to."""
    found = _command_v(False) or _command_v(True) or shutil.which("gitpro")
    if found:
        return found
    sibling = Path(sys.executable).parent / "gitpro"
    return str(sibling) if os.access(sibling, os.X_OK) else None


# ---------------------------------------------------------------- settings files

@dataclass(frozen=True)
class Target:
    agent: str
    path: Path
    list_key: str                     # hooks.<list_key>
    matcher: Optional[str]            # group matcher; None = a flat list of hook entries (Cursor)
    entry: dict


def _is_ours(h: object) -> bool:
    return isinstance(h, dict) and isinstance(h.get("command"), str) and MARKER in h["command"]


def target(agent: str, scope: str, home: Path, abs_path: Optional[str], repo: Optional[Path] = None) -> Target:
    cmd = hook_command(abs_path, agent)
    if agent == "claude":
        path = (repo / ".claude/settings.local.json") if scope == "project" and repo else \
            home / ".claude/settings.json"
        return Target(agent, path, "PreToolUse", "Bash", {"type": "command", "command": cmd,
                                                          "timeout": HOOK_TIMEOUT})
    if agent == "cursor":
        return Target(agent, home / ".cursor/hooks.json", "beforeShellExecution", None, {"command": cmd})
    if agent == "gemini":
        return Target(agent, home / ".gemini/settings.json", "BeforeTool", "run_shell_command",
                      {"type": "command", "command": cmd})
    raise ValueError(agent)


def _hook_lists(obj: dict, t: Target) -> List[list]:
    """Every list of hook entries under hooks.<list_key> (one per group, or the flat list)."""
    items = obj.get("hooks", {}).get(t.list_key, []) if isinstance(obj.get("hooks"), dict) else []
    if not isinstance(items, list):
        return []
    if t.matcher is None:
        return [items]
    return [g["hooks"] for g in items if isinstance(g, dict) and isinstance(g.get("hooks"), list)]


def installed(obj: object, t: Target) -> bool:
    return isinstance(obj, dict) and any(_is_ours(h) for lst in _hook_lists(obj, t) for h in lst)


def add_hook(obj: dict, t: Target) -> dict:
    """obj with exactly one gitpro entry (an existing one is rewritten in place)."""
    if t.agent == "cursor":
        obj.setdefault("version", 1)
    items = obj.setdefault("hooks", {}).setdefault(t.list_key, [])
    done = False

    def dedupe(lst: list) -> list:
        nonlocal done
        keep = []
        for h in lst:
            if not _is_ours(h):
                keep.append(h)
            elif not done:                 # the first one is rewritten in place, later ones dropped
                keep.append(copy.deepcopy(t.entry))
                done = True
        return keep

    if t.matcher is None:
        items[:] = dedupe(items)
    else:
        groups = []
        for g in items:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list) and any(_is_ours(h) for h in g["hooks"]):
                g["hooks"] = dedupe(g["hooks"])
                if not g["hooks"]:
                    continue               # emptied by removing a gitpro duplicate; the user's own stay
            groups.append(g)
        items[:] = groups
    if not done:
        items.append(copy.deepcopy(t.entry) if t.matcher is None else
                     {"matcher": t.matcher, "hooks": [copy.deepcopy(t.entry)]})
    return obj


def remove_hook(obj: dict, t: Target) -> dict:
    """obj without gitpro entries, and without the containers that leaves empty."""
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get(t.list_key), list):
        return obj
    items = hooks[t.list_key]
    if t.matcher is None:
        items[:] = [h for h in items if not _is_ours(h)]
    else:
        keep = []
        for g in items:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list) and any(_is_ours(h) for h in g["hooks"]):
                g["hooks"] = [h for h in g["hooks"] if not _is_ours(h)]
                if not g["hooks"]:
                    continue
            keep.append(g)
        items[:] = keep
    if not items:
        del hooks[t.list_key]
    if not hooks:
        del obj["hooks"]
    return obj


def _precheck(path: Path, t: Target, snippet: str) -> Optional[object]:
    """The file's JSON (None when missing); ConfigError for anything gitpro must not edit."""
    from . import jsonfile, undo
    from .config import ConfigError
    shown = undo.tilde(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        if jsonfile.strip_jsonc(text)[1]:
            raise ConfigError(f"{shown} has comments; gitpro cannot edit it without losing them"
                              f" — add this by hand: {snippet}") from None
        raise ConfigError(f"{shown} is not valid JSON; fix it by hand (nothing written)") from None
    if not isinstance(obj, dict):
        raise ConfigError(f"{shown} is not a JSON object; fix it by hand (nothing written)")
    hooks = obj.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ConfigError(f"{shown}: hooks is not an object; fix it by hand (nothing written)")
    if not isinstance(hooks.get(t.list_key, []), list):
        raise ConfigError(f"{shown}: hooks.{t.list_key} is not a list; fix it by hand (nothing written)")
    return obj


def _snippet(t: Target) -> str:
    entry = t.entry if t.matcher is None else {"matcher": t.matcher, "hooks": [t.entry]}
    return json.dumps({"hooks": {t.list_key: [entry]}}, ensure_ascii=False)


def _repo_top() -> Optional[Path]:
    from . import resolve
    return resolve.is_repo(Path.cwd())


def _git_out(top: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    return subprocess.run(["git", "-C", str(top), *args], capture_output=True, text=True, env=env,
                          stdin=subprocess.DEVNULL, timeout=timeout)


def _exclude_local(top: Path, script: Optional[Path]) -> bool:
    """Add .claude/settings.local.json to <git dir>/info/exclude unless git already ignores it; the undo
    line goes into `script` before the file changes. True when it was added."""
    from . import files, jsonfile
    rel = ".claude/settings.local.json"
    if _git_out(top, "check-ignore", "-q", rel).returncode != 1:
        return False                      # 0: ignored already; 128: git cannot tell, leave it alone
    r = _git_out(top, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    if r.returncode != 0 or not r.stdout.strip():
        return False
    exclude = Path(r.stdout.strip())
    try:
        old: Optional[str] = exclude.read_text(encoding="utf-8")
    except FileNotFoundError:
        old = None
    if old is not None:
        backup = jsonfile._backup(exclude, old)
        line = f"cp -p {shlex.quote(str(backup))} {shlex.quote(str(exclude))}"
    else:
        line = f"rm -f {shlex.quote(str(exclude))}"
    if script is not None:
        with open(script, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    text = (old or "")
    text += ("" if not text or text.endswith("\n") else "\n") + rel + "\n"
    mode = exclude.stat().st_mode & 0o777 if old is not None else 0o644
    files.atomic_write(exclude, text, mode)
    return True


def _confirm_with(args: argparse.Namespace, question: str) -> Callable[[str], bool]:
    def confirm(diff: str) -> bool:
        from .config import ConfigError
        if args.yes:
            return True
        if not sys.stdin.isatty():
            raise ConfigError("no terminal to confirm on; re-run with --yes")
        from . import view
        if diff:
            print(diff, end="" if diff.endswith("\n") else "\n")
        return view.confirm(question)
    return confirm


def _edit(args: argparse.Namespace, removing: bool) -> int:
    from . import args as gargs
    from . import jsonfile, jsonout, undo
    command = "agent uninstall" if removing else "agent install"
    if args.json and not args.yes and not gargs.preview_only(args):
        return gargs.fail("--write --json needs -y (there is no confirmation in JSON mode)", args)
    agent = args.agent
    say = not args.json
    if agent not in INSTALL_CHOICES:
        return gargs.fail(f"unknown agent {agent!r}; choose from {', '.join(INSTALL_CHOICES)}", args)
    if agent in NO_HOOK:
        msg = (f"{agent} has no pre-tool hook; use the git guard (gitpro guard install) and the MCP server "
               f"(gitpro mcp) instead")
        if args.json:                       # nothing was installed: a script must not read that as success
            return gargs.fail(msg, args)
        from . import view
        view.warn(msg)
        return 0
    title = {"claude": "Claude Code", "cursor": "Cursor", "gemini": "Gemini CLI"}[agent]
    if agent in EXPERIMENTAL and not removing:                # removing is always safe
        if say:
            from . import view
            view.warn(f"experimental: field names unverified; check the hook JSON against {title}'s docs")
        if not args.experimental:
            return gargs.fail(f"{agent} adapter is experimental; add --experimental", args)
    if args.scope == "project" and agent != "claude":
        return gargs.fail("--scope project is for Claude Code only", args)
    repo = None
    if args.scope == "project":
        repo = _repo_top()
        if repo is None or not (repo / ".git").exists():
            return gargs.fail("not a git repository (--scope project writes <repo>/.claude/settings.local.json)",
                              args)
    home = Path(args.home).expanduser()
    t = target(agent, args.scope, home, None if removing else gitpro_path(), repo)
    obj = _precheck(t.path, t, _snippet(t))
    before = obj if obj is not None else {}
    change = (lambda o: remove_hook(o, t)) if removing else (lambda o: add_hook(o, t))
    after = change(copy.deepcopy(before))
    shown = undo.tilde(t.path)
    if say and agent == "claude" and not removing:
        from . import view
        view.step("plugin users already have this hook (gitpro agent status shows it)")
    if after == before and (obj is not None or removing):
        if args.json:
            jsonout.emit(command, {"file": str(t.path), "changed": False, "backup": None, "undo": None})
        else:
            from . import view
            view.ok("not installed, nothing to do" if removing else "already installed, nothing to do")
        return 0
    if say:
        verb = "update" if obj is not None else "create"
        sign = "-" if removing else "+"
        print(f"  {verb:<7} {shown}   ({sign} hooks.{t.list_key}: {MARKER} --agent {agent})")
    if gargs.preview_only(args):
        return gargs.preview_result(args, command, {"file": str(t.path), "changed": False,
                                                  "action": "remove" if removing else "install",
                                                  "hook": _snippet(t), "backup": None, "undo": None})
    question = f"{'remove' if removing else 'install'} the {title} hook?"
    res = jsonfile.edit_json(t.path, change, command=command, create={},
                             confirm=_confirm_with(args, question), mkdir=True, snippet=_snippet(t))
    if not res.changed:
        from . import view
        view.aborted()
        return 1
    added = repo is not None and not removing and _exclude_local(repo, res.undo)
    if args.json:
        jsonout.emit(command, {"file": str(res.path), "changed": True,
                               "backup": str(res.backup) if res.backup else None,
                               "undo": str(res.undo) if res.undo else None})
        return 0
    from . import view
    if added:
        view.step("added .claude/settings.local.json to .git/info/exclude so it is not committed")
    view.ok(f"removed the gitpro hook from {shown}" if removing else
            f"installed; {title} picks it up on its next session")
    restores = f"restores from {undo.tilde(res.backup)}" if res.backup else f"removes {shown}"
    print(f"↩ undo: sh {undo.tilde(res.undo)}   ({restores})")
    return 0


HOOK_AGENTS = ("claude", "cursor", "gemini")


def plugin_enabled(home: Path) -> Optional[str]:
    """The enabled gitpro plugin in ~/.claude/settings.json (it ships the hook and the MCP server), or None."""
    t = target("claude", "user", home, None)
    return _plugin(_read_lenient(t.path)) if t.path.is_file() else None


def hook_change(agent: str, home: Path) -> Optional[str]:
    """For gitpro setup: what `agent install --agent AGENT` would do at user scope ("create PATH" or
    "update PATH"), or None when the hook is in place. ConfigError for a file gitpro must not edit."""
    from . import undo
    t = target(agent, "user", home, gitpro_path())
    obj = _precheck(t.path, t, _snippet(t))
    if obj is not None and add_hook(copy.deepcopy(obj), t) == obj:
        return None
    return f"{'update' if obj is not None else 'create'} {undo.tilde(t.path)}"


def install(args: argparse.Namespace) -> int:
    """`gitpro agent install`."""
    return _edit(args, removing=False)


def uninstall(args: argparse.Namespace) -> int:
    """`gitpro agent uninstall`."""
    return _edit(args, removing=True)


# ---------------------------------------------------------------- status

def _read_lenient(path: Path) -> Optional[object]:
    from . import jsonfile
    try:
        return jsonfile.read(path, jsonc=True)[0]
    except Exception:
        return None


def _plugin(obj: object) -> Optional[str]:
    plugins = obj.get("enabledPlugins") if isinstance(obj, dict) else None
    if isinstance(plugins, dict):
        for key, on in plugins.items():
            if isinstance(key, str) and key.startswith("gitpro@") and on:
                return key
    return None


def guard_installed() -> Optional[bool]:
    """True/False from gitpro.guard.status()["installed"]; None when the guard is not in this build."""
    import importlib
    try:
        guard = importlib.import_module("gitpro.guard")
        inst = guard.status().get("installed")
    except Exception:
        return None
    return inst not in ("none", "", None, False)


def status_data(home: Path, explicit_config: Optional[Path] = None) -> dict:
    """What `agent status --json` prints (without the envelope)."""
    from .config import ConfigError
    warnings: List[str] = []
    try:
        cfg = _load_config(explicit_config, home)
        mode = cfg.agents.hook if cfg is not None else "block"
    except ConfigError as e:
        mode = "block"
        warnings.append(str(e))
    repo = _repo_top()
    agents_: Dict[str, dict] = {}
    for agent in ("claude", "cursor", "gemini"):
        user = target(agent, "user", home, None)
        uobj = _read_lenient(user.path) if user.path.is_file() else None
        info = {"user": str(user.path) if installed(uobj, user) else None, "project": None, "plugin": None,
                "experimental": agent in EXPERIMENTAL}
        if agent == "claude":
            pobj = None
            if repo is not None:
                proj = target(agent, "project", home, None, repo)
                pobj = _read_lenient(proj.path) if proj.path.is_file() else None
                info["project"] = str(proj.path) if installed(pobj, proj) else None
            info["plugin"] = _plugin(uobj) or _plugin(pobj)
        agents_[agent] = info
    plain, login = _command_v(False), _command_v(True)
    if not plain and login:
        warnings.append(f"gitpro is not on PATH for sh (found by login shell at {login}); the hook exits "
                        f"silently without it → gitpro agent install --agent claude rewrites the entry with "
                        f"the absolute path")
    return {"mode": mode, "agents": agents_, "guard_installed": guard_installed(),
            "gitpro_path": {"sh": plain, "login": login}, "warnings": warnings}


def status(args: argparse.Namespace) -> int:
    """`gitpro agent status` (also bare `gitpro agent`)."""
    from . import jsonout, undo
    home = Path(getattr(args, "home", None) or Path.home()).expanduser()
    data = status_data(home, getattr(args, "config", None))
    if getattr(args, "json", False):
        jsonout.emit("agent status", data)
        return 0
    from . import view
    for w in data["warnings"]:
        view.warn(w)
    print(f"agents:   [agents] hook = {data['mode']}")
    c = data["agents"]["claude"]
    where = [f"user {undo.tilde(Path(c['user']))}"] if c["user"] else []
    where += [f"project {undo.tilde(Path(c['project']))}"] if c["project"] else []
    hook = f"hook: {', '.join(where)}" if where else "hook: not installed → gitpro agent install"
    plugin = f"plugin: enabled ({c['plugin']})" if c["plugin"] else "plugin: not enabled"
    print(f"  {'claude':<8} {hook}; {plugin}")
    for agent in EXPERIMENTAL:
        a = data["agents"][agent]
        line = (f"hook: user {undo.tilde(Path(a['user']))} (experimental)" if a["user"] else
                f"not installed (experimental) → gitpro agent install --agent {agent} --experimental")
        print(f"  {agent:<8} {line}")
    g = data["guard_installed"]
    guard = {True: "installed", False: "not installed → gitpro guard install", None: "not in this build"}[g]
    for agent in NO_HOOK:
        print(f"  {agent:<8} no pre-tool hook; covered by the git guard ({guard}) and MCP")
    print(f"  bypass:  {SKIP_ENV}=1 in the shell that starts the agent; or [agents] hook = \"warn\"|\"off\"")
    if any(a["user"] or a["project"] for a in data["agents"].values()):
        view.step("gitpro uninstall leaves agent hooks in place; gitpro agent uninstall removes them")
    return 0


# ---------------------------------------------------------------- cli

def register(sub, common, confirm) -> None:
    from .args import capability
    p = sub.add_parser("agent-check", parents=[common], epilog=BYPASS_HELP,
                       help="agent hook: read a tool call on stdin, deny it when the repo's identity is wrong")
    add_arguments(p)
    a = sub.add_parser("agent", parents=[common], epilog=BYPASS_HELP,
                       help="install the Claude Code hook; show agent hook status")
    a.set_defaults(func=status, command="agent status", json=False)
    asub = a.add_subparsers(dest="agent_command", metavar="ACTION")
    for name, func, what in (("install", install, "add"), ("uninstall", uninstall, "remove")):
        sp = asub.add_parser(name, parents=[common, confirm], help=f"{what} the agent's pre-tool hook")
        sp.add_argument("--agent", default="claude", metavar="{" + ",".join(INSTALL_CHOICES) + "}",
                        help="default: claude")
        sp.add_argument("--scope", choices=("user", "project"), default="user",
                        help="user: ~/.claude/settings.json; project: <repo>/.claude/settings.local.json")
        sp.add_argument("--experimental", action="store_true", help="allow the unverified cursor/gemini adapters")
        sp.add_argument("--json", action="store_true", help="machine-readable output (needs -y)")
        sp.set_defaults(func=func, command=f"agent {name}")
    sp = asub.add_parser("status", parents=[common], help="which agents have the hook")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=status, command="agent status")
    capability("agent-check", "agent.install", "agent.status")
