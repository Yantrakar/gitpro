"""Remote URL forms of common git hosts, offline: real git in a disposable HOME after `gitpro apply`.

For each URL: the identity git picks (includeIf hasconfig:remote.*.url) must equal resolve.match_url and the
expected identity; a URL no pattern covers must get `gitpro why`'s url_form problem with the expected fix
(the set-url URL, or the fix text when there is no argv). See docs/spec/18-url-form-followups.md.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

# (id, host, owners, ssh_port, ssh_user)
IDENTITIES = [
    ("github", "github.com", ["acme"], None, None),
    ("ghe", "github.corp.test", ["corp"], None, None),
    ("gitlab", "gitlab.com", ["grp"], None, None),
    ("gitlab-self", "gitlab.corp.test", ["team"], 2222, None),
    ("bitbucket", "bitbucket.org", ["ws"], None, None),
    ("bb-server", "bitbucket.corp.test", ["PROJ", "scm/PROJ"], 7999, None),
    ("azure-ssh", "ssh.dev.azure.com", ["v3/org"], None, None),
    ("azure-https", "dev.azure.com", ["org"], None, None),
    ("vsts-ssh", "vs-ssh.visualstudio.com", ["v3/org"], None, "org"),
    ("vsts-https", "org.visualstudio.com", [], None, None),
    ("codeberg", "codeberg.org", ["me"], None, None),
    ("sourcehut", "git.sr.ht", ["~me"], None, None),
    ("gitea", "git.self.test", ["team"], 2222, None),
    ("gitee", "gitee.com", ["me"], None, None),
    ("hf", "huggingface.co", ["me"], None, None),
    ("hf-ssh", "hf.co", ["me"], None, None),
    ("codecommit", "git-codecommit.eu-west-1.amazonaws.com", ["v1/repos"], None, None),
    ("launchpad", "git.launchpad.net", ["~me"], None, "me"),
]

SET_ID = 'set ssh_user = "<your SSH key ID>" on identity codecommit in the gitpro config, then run: gitpro setup'

# (url, identity git and gitpro pick, url_form fix for a URL that falls to the default: the set-url URL or the
# fix text). None as the fix: no url_form expected.
CASES: List[Tuple[str, str, Optional[str]]] = [
    ("git@github.com:acme/r.git", "github", None),
    ("https://github.com/acme/r.git", "github", None),
    ("ssh://git@github.com/acme/r.git", "github", None),
    ("https://github.com/acme/r", "github", None),
    ("git@github.com:ACME/r.git", "default", "git@github.com:acme/r.git"),
    ("ssh://git@github.com:22/acme/r.git", "default", "ssh://git@github.com/acme/r.git"),
    ("ssh://git@github.com:2200/acme/r.git", "default",
     "set ssh_port = 2200 on identity github in the gitpro config, then run: gitpro setup"),
    ("git@github.com:other/r.git", "default", None),
    ("git@github.corp.test:corp/r.git", "ghe", None),
    ("https://github.corp.test/corp/r.git", "ghe", None),
    ("git@gitlab.com:grp/sub/deeper/r.git", "gitlab", None),
    ("https://gitlab.com/grp/sub/r.git", "gitlab", None),
    ("git@gitlab.com:grpx/r.git", "default", None),
    ("ssh://git@gitlab.corp.test:2222/team/r.git", "gitlab-self", None),
    ("https://gitlab.corp.test/team/r.git", "gitlab-self", None),
    ("git@bitbucket.org:ws/r.git", "bitbucket", None),
    ("https://bitbucket.org/ws/r.git", "bitbucket", None),
    ("https://jdoe@bitbucket.org/ws/r.git", "default", "https://bitbucket.org/ws/r.git"),
    ("ssh://git@bitbucket.corp.test:7999/PROJ/r.git", "bb-server", None),
    ("https://bitbucket.corp.test/scm/PROJ/r.git", "bb-server", None),
    ("https://jdoe@bitbucket.corp.test/scm/PROJ/r.git", "default", "https://bitbucket.corp.test/scm/PROJ/r.git"),
    ("git@ssh.dev.azure.com:v3/org/proj/r", "azure-ssh", None),
    ("https://dev.azure.com/org/proj/_git/r", "azure-https", None),
    ("https://org@dev.azure.com/org/proj/_git/r", "default", "https://dev.azure.com/org/proj/_git/r"),
    ("org@vs-ssh.visualstudio.com:v3/org/proj/r", "vsts-ssh", None),
    ("https://org.visualstudio.com/proj/_git/r", "vsts-https", None),
    ("git@codeberg.org:me/r.git", "codeberg", None),
    ("https://codeberg.org/me/r.git", "codeberg", None),
    ("git@git.sr.ht:~me/r", "sourcehut", None),
    ("https://git.sr.ht/~me/r", "sourcehut", None),
    ("ssh://git@git.sr.ht/~me/r", "sourcehut", None),
    ("git@git.sr.ht:~other/r", "default", None),
    ("ssh://git@git.self.test:2222/team/r.git", "gitea", None),
    ("https://git.self.test/team/r.git", "gitea", None),
    ("https://git.self.test:3000/team/r.git", "default", "switch origin to an ssh URL for gitea"),
    ("git@gitee.com:me/r.git", "gitee", None),
    ("https://gitee.com/me/r.git", "gitee", None),
    ("git@hf.co:me/r", "hf-ssh", None),
    ("https://huggingface.co/me/r", "hf", None),
    ("ssh://git-codecommit.eu-west-1.amazonaws.com/v1/repos/r", "default", SET_ID),
    ("https://git-codecommit.eu-west-1.amazonaws.com/v1/repos/r", "codecommit", None),
    ("ssh://me@git.launchpad.net/~me/r", "launchpad", None),
    ("https://git.launchpad.net/~me/r", "launchpad", None),
    ("git+ssh://me@git.launchpad.net/~me/r", "default", "ssh://me@git.launchpad.net/~me/r"),
    ("git+ssh://git.launchpad.net/~me/r", "default", "ssh://me@git.launchpad.net/~me/r"),
]


def config_text() -> str:
    out = '[default]\nname = "Default"\nemail = "default@x.test"\n'
    for i, host, owners, port, user in IDENTITIES:
        out += f'\n[[identity]]\nid = "{i}"\nname = "{i}"\nemail = "{i}@x.test"\nhost = "{host}"\n'
        if owners:
            out += "owners = [" + ", ".join(f'"{o}"' for o in owners) + "]\n"
        if port:
            out += f"ssh_port = {port}\n"
        if user:
            out += f'ssh_user = "{user}"\n'
    return out


def smoke(verbose: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="gitpro-providers-") as temporary:
        home = Path(temporary).resolve() / "home"
        (home / ".config").mkdir(parents=True, mode=0o700)
        cfg = home / ".config/gitpro.toml"
        cfg.write_text(config_text())
        cfg.chmod(0o600)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITPRO_")) and
               k not in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "FORCE_COLOR")}
        env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1", NO_COLOR="1")
        applied = subprocess.run([sys.executable, "-m", "gitpro", "apply", "-y"], env=env,
                                 capture_output=True, text=True, timeout=60)
        if applied.returncode:
            raise SystemExit(f"gitpro apply failed ({applied.returncode}):\n{applied.stdout}{applied.stderr}")
        os.environ.clear()
        os.environ.update(env)
        from gitpro import config, resolve
        loaded = config.load_all(cfg)

        failures = 0
        for n, (url, want, want_fix) in enumerate(CASES):
            repo = home / f"repo{n}"
            git = ["git", "-C", str(repo)]
            subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True, timeout=30)
            subprocess.run(git + ["remote", "add", "origin", url], env=env, check=True, timeout=30)
            email = subprocess.run(git + ["config", "user.email"], env=env, capture_output=True, text=True,
                                   timeout=30).stdout.strip()
            by_git = email[:-len("@x.test")] if email.endswith("@x.test") else f"? {email}"
            hit = resolve.match_url(url, loaded)[0]
            by_gitpro = hit.id if hit else "default"
            got_fix = None
            if by_gitpro == "default":
                why = subprocess.run([sys.executable, "-m", "gitpro", "why", "--json"], cwd=repo, env=env,
                                     capture_output=True, text=True, timeout=60)
                try:
                    problems = json.loads(why.stdout).get("problems", [])
                except ValueError:
                    problems = []
                form = [p for p in problems if p.get("code") == "url_form"]
                if form:
                    got_fix = form[0]["fix_argv"][-1] if form[0].get("fix_argv") else form[0].get("fix")
            wrong = []
            if by_git != by_gitpro:
                wrong.append(f"git picks {by_git}, gitpro {by_gitpro}")
            elif by_git != want:
                wrong.append(f"picks {by_git}, expected {want}")
            if got_fix != want_fix:
                wrong.append(f"url_form fix {got_fix!r}, expected {want_fix!r}")
            failures += bool(wrong)
            if wrong or verbose:
                print(f"{'FAIL' if wrong else 'ok':4} {url}  -> {by_git}" + (f"  (fix: {got_fix})" if got_fix else "")
                      + "".join(f"\n       {w}" for w in wrong))
        reported = sum(1 for _, w, f in CASES if w == "default" and f)
        print(f"{len(CASES) - failures}/{len(CASES)} provider URL cases pass "
              f"({len(IDENTITIES)} identities; {reported} URLs reported as url_form with a fix).")
        return 1 if failures else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args not in ([], ["-v"]):
        raise SystemExit("usage: python tests/smoke_providers.py [-v]")
    raise SystemExit(smoke(args == ["-v"]))
