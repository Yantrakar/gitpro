"""`gitpro completion bash|zsh|fish`: tab completion generated at run time from the argparse tree.

generate(parser, shell) walks every subparser (including commands and flags that other packages add
through register()/extend()), so the script always matches the installed gitpro. Option values come
from argparse `choices`, or from `gitpro completion --values identities|kinds|hooks|shells` at
completion time for the options in DYNAMIC (or any action carrying a `gitpro_values` attribute:
`p.add_argument("--x").gitpro_values = "identities"`). Module level imports only the standard library.
"""
from __future__ import annotations

import argparse
import importlib
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

PROG = "gitpro"
SHELLS = ("bash", "zsh", "fish")
VALUE_KINDS = ("identities", "kinds", "hooks", "shells")
HOOK_NAMES = ("pre-commit", "pre-push", "commit-msg")
# (command path, option or positional dest) -> --values kind; "*" matches any command.
DYNAMIC: Dict[Tuple[str, str], str] = {
    ("*", "--identity"): "identities",
    ("lost", "--only"): "kinds",
    ("*", "hook"): "hooks",
    ("completion", "shell"): "shells",
}
# (command path, option) -> (module, attribute, extra words): static values read from the module that owns
# the option when the script is generated (R-067: env --only keys, env --shell shells).
STATIC: Dict[Tuple[str, str], Tuple[str, str, Tuple[str, ...]]] = {
    ("env", "--only"): ("gitpro.envcmd", "KEYS", ()),
    ("env", "--shell"): ("gitpro.envcmd", "SHELLS", ("bash", "zsh")),
}
INSTALL = {
    "bash": '# add to ~/.bashrc: eval "$(gitpro completion bash)"',
    "zsh": '# add to ~/.zshrc (after compinit): eval "$(gitpro completion zsh)"',
    "fish": "# add to ~/.config/fish/config.fish: gitpro completion fish | source",
}
_WORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@=,-]*$")   # safe inside the generated scripts unquoted
_FLAG_RE = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---------------------------------------------------------------- the tree

@dataclass
class Opt:
    flags: List[str]
    takes_value: bool
    values: List[str] = field(default_factory=list)   # static choices
    dynamic: Optional[str] = None                     # a VALUE_KINDS entry
    help: str = ""


@dataclass
class Node:
    path: List[str]                                    # ["guard", "install"]; [] for gitpro itself
    help: str = ""
    opts: List[Opt] = field(default_factory=list)
    subs: Dict[str, "Node"] = field(default_factory=dict)
    pos_values: List[str] = field(default_factory=list)
    pos_dynamic: Optional[str] = None
    pos_files: bool = False                            # a positional without known values (a path)

    @property
    def key(self) -> str:
        return " ".join([PROG, *self.path])


def _safe(words, pattern=_WORD_RE) -> List[str]:
    """The words that can go into a script unquoted; others are dropped (never completed)."""
    return [str(w) for w in words if pattern.match(str(w))]


def _dynamic(path: List[str], action: argparse.Action) -> Optional[str]:
    kind = getattr(action, "gitpro_values", None)
    if kind in VALUE_KINDS:
        return kind
    cmd = path[0] if path else ""
    names = list(action.option_strings) or [action.dest]
    for n in names:
        for scope in (" ".join(path), cmd, "*"):
            if (scope, n) in DYNAMIC:
                return DYNAMIC[(scope, n)]
    return None


def _static(path: List[str], action: argparse.Action) -> List[str]:
    """Values for an option without argparse choices: an action's `gitpro_choices`, else STATIC."""
    words = list(getattr(action, "gitpro_choices", None) or ())
    for flag in action.option_strings:
        spec = STATIC.get((" ".join(path), flag))
        if spec and not words:
            mod = sys.modules.get(spec[0])
            if mod is None:
                try:
                    mod = importlib.import_module(spec[0])
                except ImportError:
                    continue
            words = [*(getattr(mod, spec[1], None) or ()), *spec[2]]
    return _safe(dict.fromkeys(words))


def tree(parser: argparse.ArgumentParser, path: Optional[List[str]] = None, help: str = "") -> Node:
    """The completion tree of parser: options, positionals and subcommands, recursively."""
    node = Node(list(path or []), help)
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            helps = {c.dest: c.help or "" for c in a._choices_actions}
            for name, sp in a.choices.items():   # hidden and deprecated commands are marked (cli._hide)
                if _WORD_RE.match(name) and not getattr(sp, "gitpro_hidden", False):
                    node.subs[name] = tree(sp, node.path + [name], helps.get(name, ""))
            continue
        if a.help == argparse.SUPPRESS:
            continue
        dyn = _dynamic(node.path, a)
        values = _safe(a.choices) if a.choices is not None else _static(node.path, a)
        if a.option_strings:
            node.opts.append(Opt(_safe(a.option_strings, _FLAG_RE), a.nargs != 0, values, dyn, a.help or ""))
        elif values or dyn:
            node.pos_values += values
            node.pos_dynamic = node.pos_dynamic or dyn
        else:
            node.pos_files = True
    return node


def _walk(node: Node):
    yield node
    for s in node.subs.values():
        yield from _walk(s)


def values(kind: str, config_path=None) -> List[str]:
    """Completion values of one kind, only shell-safe words (bash's compgen -W expands $(...));
    [] on any error (no config, bad config, package missing)."""
    return _safe(_values(kind, config_path))


def _values(kind: str, config_path=None) -> List[str]:
    try:
        if kind == "identities":
            from pathlib import Path

            from . import config
            path, how = config.find(config_path, Path.home())
            return [i.id for i in config.load_all(path, how).identities]
        if kind == "kinds":
            try:
                import importlib
                lost = importlib.import_module("gitpro.lost")   # 03; honours sys.modules (unlike `from . import`)
            except ImportError:
                return []
            kinds = getattr(lost, "KINDS", None) or getattr(lost, "ALL_KINDS", None) or ()
            return [str(k) for k in kinds]
        if kind == "hooks":
            return list(HOOK_NAMES)
        if kind == "shells":
            return list(SHELLS)
    except Exception:
        return []
    return []


# ---------------------------------------------------------------- bash (zsh reuses the helpers)

def _case(entries: List[Tuple[str, str]], default: str = "", subject: str = "$1") -> List[str]:
    """`case "SUBJECT" in "pattern") body ;; esac` lines; patterns are already shell-safe keys."""
    out = [f'    case "{subject}" in']
    out += [f'        "{pat}") {body} ;;' for pat, body in entries]
    if default:
        out.append(f"        *) {default} ;;")
    out.append("    esac")
    return out


def _dyn_cmd(kind: str) -> str:
    return f"{PROG} completion --values {kind} 2>/dev/null"


def _helpers(root: Node) -> List[str]:
    """Shell functions shared by bash and zsh (POSIX-ish syntax both accept). Each prints words or
    returns a status; the keys are 'gitpro sub sub'."""
    nodes = list(_walk(root))
    lines = ["# _gitpro_child KEY WORD: the key of subcommand WORD under KEY, or fail",
             "_gitpro_child() {"]
    lines += _case([(f"{n.key} {s}", f"echo '{n.key} {s}'") for n in nodes for s in n.subs], "return 1", "$1 $2")
    lines += ["}", "", "# _gitpro_subs KEY: subcommand names", "_gitpro_subs() {"]
    lines += _case([(n.key, f"echo '{' '.join(n.subs)}'") for n in nodes if n.subs])
    lines += ["}", "", "# _gitpro_opts KEY: option names", "_gitpro_opts() {"]
    lines += _case([(n.key, f"echo '{' '.join(f for o in n.opts for f in o.flags)}'") for n in nodes if n.opts])
    lines += ["}", "", "# _gitpro_takes KEY OPT: does OPT take a value?", "_gitpro_takes() {"]
    takes = [(f"{n.key} {f}", "return 0") for n in nodes for o in n.opts if o.takes_value for f in o.flags]
    lines += _case(takes, "return 1", "$1 $2")
    lines += ["}", "",
              "# _gitpro_optvals KEY OPT: values for OPT; status 1 means complete file names",
              "_gitpro_optvals() {"]
    vals = []
    for n in nodes:
        for o in n.opts:
            body = _values_body(o.values, o.dynamic)
            if body:
                vals += [(f"{n.key} {f}", body) for f in o.flags]
    lines += _case(vals, "return 1", "$1 $2")
    lines += ["}", "", "# _gitpro_posvals KEY: values for positionals; status 1 means complete file names",
              "_gitpro_posvals() {"]
    pos = [(n.key, _values_body(n.pos_values, n.pos_dynamic)) for n in nodes if n.pos_values or n.pos_dynamic]
    files_ok = [n.key for n in nodes if n.pos_files]
    lines += _case(pos + [(k, "return 1") for k in files_ok if k not in dict(pos)], "return 2")
    lines += ["}", "",
              "# _gitpro_key WORDS...: walk the words before the cursor to the deepest subcommand key",
              "_gitpro_key() {",
              f"    local key='{PROG}' skip=0 w next",
              "    for w in \"$@\"; do",
              "        if [ \"$skip\" = 1 ]; then skip=0; continue; fi",
              "        case \"$w\" in",
              "            --*=*) continue ;;",
              "            -*) _gitpro_takes \"$key\" \"$w\" && skip=1; continue ;;",
              "        esac",
              "        next=$(_gitpro_child \"$key\" \"$w\") && key=$next",
              "    done",
              "    echo \"$key\"",
              "}"]
    return lines


def _values_body(static: List[str], dynamic: Optional[str]) -> str:
    parts = []
    if static:
        parts.append(f"echo '{' '.join(static)}'")
    if dynamic:
        parts.append(_dyn_cmd(dynamic))
    return "; ".join(parts)


_BASH_MAIN = r"""
_gitpro_files() {
    compopt -o filenames 2>/dev/null
    COMPREPLY=($(compgen -f -- "$cur"))
}

_gitpro() {
    local cur="${COMP_WORDS[COMP_CWORD]}" prev="" key words pos rc
    [ "$COMP_CWORD" -gt 1 ] && prev="${COMP_WORDS[COMP_CWORD-1]}"
    key=$(_gitpro_key "${COMP_WORDS[@]:1:COMP_CWORD-1}")
    COMPREPLY=()
    if [[ $prev == -* ]] && _gitpro_takes "$key" "$prev"; then
        if words=$(_gitpro_optvals "$key" "$prev"); then
            COMPREPLY=($(compgen -W "$words" -- "$cur"))
        else
            _gitpro_files
        fi
        return 0
    fi
    if [[ $cur == -* ]]; then
        COMPREPLY=($(compgen -W "$(_gitpro_opts "$key")" -- "$cur"))
        return 0
    fi
    words=$(_gitpro_subs "$key")
    pos=$(_gitpro_posvals "$key"); rc=$?
    [ $rc = 0 ] && words="$words $pos"
    if [ -n "${words// /}" ]; then
        COMPREPLY=($(compgen -W "$words" -- "$cur"))
    elif [ $rc = 1 ]; then
        _gitpro_files
    fi
    return 0
}
complete -F _gitpro gitpro
"""


def _bash(root: Node) -> str:
    head = f"# bash completion for {PROG} (generated by `{PROG} completion bash`; regenerate after upgrading)"
    return "\n".join([head, *_helpers(root)]) + "\n" + _BASH_MAIN


def _zsh(root: Node) -> str:
    lines = [f"#compdef {PROG}",
             f"# zsh completion for {PROG} (generated by `{PROG} completion zsh`; regenerate after upgrading)",
             *_helpers(root), "",
             "_gitpro() {",
             "    local cur=${words[CURRENT]} prev=\"\" key vals pos rc",
             "    (( CURRENT > 2 )) && prev=${words[CURRENT-1]}",
             "    key=$(_gitpro_key \"${(@)words[2,CURRENT-1]}\")",
             "    if [[ $prev == -* ]] && _gitpro_takes \"$key\" \"$prev\"; then",
             "        if vals=$(_gitpro_optvals \"$key\" \"$prev\"); then",
             "            compadd -- ${=vals}",
             "        else",
             "            _files",
             "        fi",
             "        return 0",
             "    fi",
             "    if [[ $cur == -* ]]; then",
             "        compadd -- ${=$(_gitpro_opts \"$key\")}",
             "        return 0",
             "    fi",
             "    vals=$(_gitpro_subs \"$key\")",
             "    pos=$(_gitpro_posvals \"$key\"); rc=$?",
             "    (( rc == 0 )) && vals=\"$vals $pos\"",
             "    if [[ -n ${vals// /} ]]; then",
             "        compadd -- ${=vals}",
             "    elif (( rc == 1 )); then",
             "        _files",
             "    fi",
             "    return 0",
             "}",
             "",
             "if [[ ${zsh_eval_context[-1]} == loadautofunc ]]; then",
             "    _gitpro \"$@\"",
             "elif (( $+functions[compdef] )); then",
             f"    compdef _gitpro {PROG}",
             "fi", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- fish

def _fq(text: str) -> str:
    """A fish single-quoted string."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _first_line(text: str) -> str:
    return " ".join(text.split())[:80]


def _fish(root: Node) -> str:
    lines = [f"# fish completion for {PROG} (generated by `{PROG} completion fish`; regenerate after upgrading)",
             f"complete -c {PROG} -e", ""]
    for n in _walk(root):
        if n.path:
            cond = "__fish_seen_subcommand_from " + n.path[-1]
            if len(n.path) > 1:
                cond = " ; and ".join(f"__fish_seen_subcommand_from {p}" for p in n.path)
        else:
            cond = ""
        if n.subs:
            names = " ".join(n.subs)
            here = (cond + " ; and " if cond else "") + f"not __fish_seen_subcommand_from {names}"
            for name, s in n.subs.items():
                d = f" -d {_fq(_first_line(s.help))}" if s.help else ""
                lines.append(f"complete -c {PROG} -f -n {_fq(here)} -a {name}{d}")
        for o in n.opts:
            spec = []
            for f in o.flags:
                if f.startswith("--"):
                    spec.append(f"-l {f[2:]}")
                elif len(f) == 2:
                    spec.append(f"-s {f[1]}")
                else:
                    spec.append(f"-o {f[1:]}")
            args = ""
            if o.takes_value:
                vals = " ".join(o.values)
                if o.dynamic:
                    vals = (vals + " " if vals else "") + f"({PROG} completion --values {o.dynamic} 2>/dev/null)"
                args = f" -x -a {_fq(vals)}" if vals else " -r"
            n_ = f" -n {_fq(cond)}" if cond else ""
            d = f" -d {_fq(_first_line(o.help))}" if o.help else ""
            lines.append(f"complete -c {PROG}{n_} {' '.join(spec)}{args}{d}")
        if n.path and (n.pos_values or n.pos_dynamic):
            vals = " ".join(n.pos_values)
            if n.pos_dynamic:
                vals = (vals + " " if vals else "") + f"({PROG} completion --values {n.pos_dynamic} 2>/dev/null)"
            lines.append(f"complete -c {PROG} -f -n {_fq(cond)} -a {_fq(vals)}")
    return "\n".join(lines) + "\n"


def generate(parser: argparse.ArgumentParser, shell: str) -> str:
    """The completion script for shell ("bash", "zsh" or "fish") covering every command in parser."""
    root = tree(parser)
    if shell == "bash":
        return _bash(root)
    if shell == "zsh":
        return _zsh(root)
    if shell == "fish":
        return _fish(root)
    raise ValueError(f"unknown shell {shell!r} ({', '.join(SHELLS)})")


# ---------------------------------------------------------------- command

def cmd_completion(args) -> int:
    from .args import fail
    if args.values:
        for v in values(args.values, args.config):
            print(v)
        return 0
    if not args.shell:
        return fail(f"name a shell: {PROG} completion {'|'.join(SHELLS)}", args)
    if args.shell not in SHELLS:
        return fail(f"unknown shell {args.shell!r} ({', '.join(SHELLS)})", args)
    from . import cli
    parser, _subs = cli.build_parser()
    sys.stdout.write(generate(parser, args.shell))
    print(INSTALL[args.shell], file=sys.stderr)
    return 0


def register(sub, common, confirm) -> None:
    from .args import capability
    sp = sub.add_parser("completion", parents=[common], help="print a shell completion script (bash, zsh, fish)")
    sp.add_argument("shell", nargs="?", metavar="SHELL", help="bash, zsh or fish")
    sp.add_argument("--values", choices=VALUE_KINDS, help="print completion values, one per line (used by the scripts)")
    sp.set_defaults(func=cmd_completion)
    capability("completion")
