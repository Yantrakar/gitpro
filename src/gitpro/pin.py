"""Copy a repo's identity into its own .git directory, for containers that don't see ~/.gitconfig's includes.

VS Code Dev Containers copy ~/.gitconfig into the container but not the files it includes, so the
includeIf identities are lost there. The repo's .git directory is mounted with the workspace, so a
plain [user] file included from .git/config carries the name and email along.

A repo whose remotes belong to different identities (origin a work org, upstream a personal fork)
gets every one of their includes, and the later one wins. `pin --remote R` settles it: the pin file
holds all of R's identity settings (name, email, SSH key, signing), and turns off what the other
identities' includes would still set.
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from . import files, render, resolve
from .config import Config, ConfigError, Identity, check

FILE = "git-identities"  # in the repo's common git dir; .git/config includes it by this relative path


def git(repo: str, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode not in ok:
        raise ConfigError(r.stderr.strip() or f"git {' '.join(args)} failed ({r.returncode})")
    return r.stdout.strip()


def _pin_file(repo: str) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / FILE


def _resolved(repo: str, key: str, pin_path: Path, base: Path) -> str:
    """key's value, if it comes from a file we generated (not, say, a hand-written ~/.gitconfig in a container)."""
    entries = git(repo, "config", "-z", "--show-origin", "--get-all", key, ok=(0, 1)).split("\0")
    origin, value, src = "", "", None
    for found_origin, found_value in zip(entries[::2], entries[1::2]):
        found_src = (base / found_origin[len("file:"):]).resolve() if found_origin.startswith("file:") else None
        if found_src == pin_path.resolve():
            continue
        origin, value, src = found_origin, found_value, found_src
    check(value, f"git resolves no {key} for this repo; nothing to pin")
    check(src is not None and src.is_file() and render.is_generated(src.read_text()),
          f"{key} comes from {origin}, not from gitpro; run 'gitpro setup', "
          "and run pin on the host, not inside the container")
    return value


def unpin(repo: str, *, dry_run: bool = False) -> bool:
    """Remove the pin; return whether there was one."""
    path = _pin_file(repo)
    # --get-all exits 1 when the value is absent: not pinned.
    had = git(repo, "config", "--local", "--fixed-value", "--get-all", "include.path", FILE, ok=(0, 1)) != ""
    if dry_run:
        return had or (path.is_file() and render.is_generated(path.read_text()))
    if had:
        git(repo, "config", "--local", "--fixed-value", "--unset-all", "include.path", FILE)
        if not git(repo, "config", "--local", "--get-regexp", r"^include\.", ok=(0, 1)):
            # Older git leaves the emptied [include] header behind; newer git already dropped it (exit 128).
            git(repo, "config", "--local", "--remove-section", "include", ok=(0, 128))
    if path.is_file() and render.is_generated(path.read_text()):
        path.unlink()
        had = True
    return had


def pin(repo: str, *, dry_run: bool = False) -> tuple[str, str, Path]:
    """Write the identity git resolves for repo now (name, email) into the repo; return them."""
    path = _pin_file(repo)
    check(not path.is_symlink(), f"{path}: pin file must not be a symlink; replace it with a regular file")
    previous = path.read_bytes() if path.exists() else None
    check(previous is None or render.is_generated(previous.decode()), f"{path} exists and was not written by us")
    base = resolve._need_layout(Path(repo)).base
    name, email = (_resolved(repo, key, path, base) for key in ("user.name", "user.email"))
    if dry_run:
        return name, email, path
    _write(repo, path, previous, f"{render.HEADER}\n# Re-run 'gitpro pin' after changing the config.\n"
           f"[user]\n{render.INDENT}name = {render.gval(name)}\n{render.INDENT}email = {render.gval(email)}\n")
    return name, email, path


# What another identity's include sets and the chosen one doesn't goes back to what git would use
# with the chosen identity alone: the [default] value, else the value that turns it off. Keys that
# only matter while signing (user.signingkey, gpg.*) are moot once gpgsign is off.
_OFF = {("core", "sshcommand"): "ssh", ("commit", "gpgsign"): False, ("tag", "gpgsign"): False}


def _flat(sections: dict) -> dict:
    """{(section, key): value} with git's case-insensitive names; a subsection as section.sub."""
    out = {}
    for sec, body in sections.items():
        for k, v in body.items():
            if isinstance(v, dict):
                out.update({(f"{sec.lower()}.{k}", kk.lower()): vv for kk, vv in v.items()})
            else:
                out[(sec.lower(), k.lower())] = v
    return out


def remote_settings(cfg: Config, chosen: Identity, others: list[Identity], expand) -> tuple[dict, list[str]]:
    """chosen's settings, plus what undoes the others' settings that chosen doesn't have; and a
    warning for each such setting gitpro cannot undo."""
    mine = render.identity_settings(chosen, cfg.default, expand)
    have = _flat(mine)
    base = _flat(render.default_settings(cfg.default, expand))
    extra: dict = {}
    left: list[str] = []
    for other in others:
        for sec, key in _flat(render.identity_settings(other, cfg.default, expand)):
            if (sec, key) in have:
                continue
            if (sec, key) in base and "." not in sec:
                value = base[(sec, key)]
            elif (sec, key) in _OFF:
                value = _OFF[(sec, key)]
            elif ((sec, key) == ("user", "signingkey") or sec == "gpg" or sec.startswith("gpg.")) \
                    and (sec, key) not in base:
                continue
            else:
                if f"{sec}.{key}" not in left:
                    left.append(f"{sec}.{key}")
                continue
            extra.setdefault(sec, {})[key] = value
    return render.merge(mine, extra), [f"{k} from {', '.join(o.id for o in others)} still applies; "
                                       f"set it in [identity.git] of {chosen.id} to override it" for k in left]


def pin_remote(repo: str, cfg: Config, remote: str, expand, *, dry_run: bool = False
               ) -> tuple[Identity, Path, list[str]]:
    """Pin the identity of remote's URLs, with all its settings; return it, the pin file and warnings."""
    path = _pin_file(repo)
    check(not path.is_symlink(), f"{path}: pin file must not be a symlink; replace it with a regular file")
    previous = path.read_bytes() if path.exists() else None
    check(previous is None or render.is_generated(previous.decode()), f"{path} exists and was not written by us")
    urls = resolve._remote_urls(Path(repo))
    check(any(n == remote for n, _ in urls), f"no remote named {remote!r} in this repo")
    rows = resolve.by_remote(cfg, urls)
    mine = [i for n, i, _ in rows if n == remote]
    check(bool(mine), f"no identity in the config matches {remote}'s URL")
    chosen = mine[0]
    others = list({i.id: i for _, i, _ in rows if i.id != chosen.id}.values())
    settings, warnings = remote_settings(cfg, chosen, others, expand)
    if not dry_run:
        _write(repo, path, previous, "\n".join([
            render.HEADER, f"# Re-run 'gitpro pin --remote {remote}' after changing the config.",
            f"{resolve.PIN_MARK}{chosen.id}",
            *render.git_lines(settings, f"identity {chosen.id!r}.git")]) + "\n")
    return chosen, path, warnings


def _write(repo: str, path: Path, previous: bytes | None, text: str) -> None:
    included = git(repo, "config", "--local", "--fixed-value", "--get-all", "include.path", FILE, ok=(0, 1)) != ""
    previous_mode = stat.S_IMODE(path.stat().st_mode) if previous is not None else 0o600
    mode = previous_mode & 0o755
    files.atomic_write(path, text, mode)
    if not included:
        try:
            git(repo, "config", "--local", "--add", "include.path", FILE)
        except BaseException:
            if previous is None:
                path.unlink()
            else:
                files.atomic_write(path, previous, previous_mode)
            raise
