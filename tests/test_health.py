"""Package 09: keys --health, gitpro auth, keys --upload (spec 09 §10). Fake ssh/ssh-keygen/ssh-add/gh only."""
from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

from conftest import run

TOKEN = "gho_FAKE_TOKEN_must_never_show"
MAT = {"id_work": "AAAAC3WORKKEYMATERIAL", "id_personal": "AAAAC3PERSONALKEYMAT", "id_nopass": "AAAAC3NOPASSMATERIAL",
       "id_old": "AAAAB3OLDRSAMATERIAL", "id_ecdsa": "AAAAE2ECDSAMATERIAL", "id_shared": "AAAAC3SHAREDMATERIAL",
       "id_lab": "AAAAC3LABKEYMATERIAL", "id_sk": "AAAAGnNrSKKEYMATERIAL"}

KEYGEN = r'''
import json
here = os.path.dirname(os.path.abspath(sys.argv[0]))
if "-F" in argv:
    sys.exit(1)
base = os.path.basename(argv[argv.index("-f") + 1])
if "-l" in argv:
    base = base[:-4] if base.endswith(".pub") else base
    table = {"id_old": "1024 SHA256:old c (RSA)", "id_ecdsa": "256 SHA256:ec c (ECDSA)",
             "id_sk": "256 SHA256:sk c (ED25519-SK)"}
    print(table.get(base, "256 SHA256:%s %s@test (ED25519)" % (base, base)))
    sys.exit(0)
if "-y" in argv:
    with open(os.path.join(here, "keygen-y.log"), "a") as f:
        f.write("%s devnull=%s\n" % (base, os.path.samestat(os.fstat(0), os.stat(os.devnull))))
    if base in ("id_nopass", "id_sk"):
        print("ssh-ed25519 AAAA")
        sys.exit(0)
    sys.stderr.write("Load key \"x\": incorrect passphrase supplied to decrypt private key\n")
    sys.exit(255)
sys.exit(1)
'''

SSH_ADD = r'''
here = os.path.dirname(os.path.abspath(sys.argv[0]))
path = os.path.join(here, "agent.txt")
if not os.path.exists(path):
    sys.stderr.write("Could not open a connection to your authentication agent.\n")
    sys.exit(2)
fps = open(path).read().split()
for fp in fps:
    print("256 %s comment (ED25519)" % fp)
sys.exit(0 if fps else 1)
'''

SSH = r'''
import json
here = os.path.dirname(os.path.abspath(sys.argv[0]))
if "-V" in argv:
    sys.stderr.write("OpenSSH_9.6p1\n")
    sys.exit(0)
answers = json.load(open(os.path.join(here, "ssh.json")))
rc, err = answers.get(argv[-1], [255, "ssh: Could not resolve hostname x\n"])
sys.stderr.write(err)
sys.exit(rc)
'''

GH = r'''
here = os.path.dirname(os.path.abspath(sys.argv[0]))
table = os.path.join(here, "gh_keys.txt")
if argv[:2] == ["auth", "token"]:
    login = argv[argv.index("-u") + 1]
    if os.path.exists(os.path.join(here, "gh_nologin_" + login)):
        sys.stderr.write("no oauth token found for github.com account " + login + "\n")
        sys.exit(1)
    print("''' + TOKEN + r'''~" + login)
    sys.exit(0)
login = env.get("GH_TOKEN", "~").split("~", 1)[1]
if os.path.exists(os.path.join(here, "gh_keys_%s.txt" % login)):
    table = os.path.join(here, "gh_keys_%s.txt" % login)
if argv[:2] == ["auth", "status"]:
    sys.exit(0)
if argv[:2] == ["ssh-key", "list"]:
    if os.path.exists(table):
        sys.stdout.write(open(table).read())
    sys.exit(0)
if argv[:2] == ["ssh-key", "add"]:
    if os.path.exists(os.path.join(here, "gh_add_fail")):
        sys.stderr.write('HTTP 404: Not Found (https://api.github.com/user/keys)\n'
                         'This API operation needs the "admin:public_key" scope.\n')
        sys.exit(1)
    pub = open(argv[2]).read().split()
    kind = "signing" if "--type" in argv else "authentication"
    title = argv[argv.index("--title") + 1]
    lines = open(table).read().splitlines() if os.path.exists(table) else []
    with open(table, "a") as f:
        f.write("%s\t%s %s\t2026-10-02T00:00:00Z\t%d\t%s\n" % (title, pub[0], pub[1], 900 + len(lines), kind))
    sys.exit(0)
sys.exit(1)
'''


def set_pub(home, name, mat=None, comment=None):
    pub = home / ".ssh" / (name + ".pub")
    pub.write_text(f"ssh-ed25519 {mat or MAT[name]} {comment or name + '@test'}\n")
    pub.chmod(0o644)


def make_key(home, name):
    priv = home / ".ssh" / name
    priv.parent.mkdir(mode=0o700, exist_ok=True)
    priv.write_text("")
    priv.chmod(0o600)
    set_pub(home, name)


@pytest.fixture
def fakes(home, fake_bin, monkeypatch, tmp_path):
    for v in ("GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITLAB_TOKEN", "SSH_AUTH_SOCK"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.chdir(tmp_path)
    fake_bin.add("ssh-keygen", KEYGEN)
    fake_bin.add("ssh-add", SSH_ADD)
    fake_bin.add("ssh", SSH)
    (fake_bin.path / "ssh.json").write_text("{}")
    return fake_bin


def no_cli(monkeypatch, *names):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: None if n in names else real(n, *a, **k))


def with_gh(fake_bin, rows=""):
    fake_bin.add("gh", GH)
    (fake_bin.path / "gh_keys.txt").write_text(rows)


def gh_row(name, kind="authentication", kid=1, title="t"):
    return f"{title}\tssh-ed25519 {MAT[name]}\t2026-01-01T00:00:00Z\t{kid}\t{kind}\n"


def setup(cfg, home, text=None):
    cfg(text, apply=False)
    for name in MAT:
        if (home / ".ssh" / name).exists():
            set_pub(home, name)


def health_json(capsys, *extra):
    rc = run("keys", "--health", "--json", *extra)
    out = capsys.readouterr()
    return rc, json.loads(out.out), out.err


def by_name(data):
    return {os.path.basename(k["path"]): k for k in data["keys"]}


NOPASS_CONFIG = """\
[default]
name = "Default Me"
email = "me@default.test"

[[identity]]
id = "work-github"
name = "Work Me"
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
ssh_key = "~/.ssh/id_work"
gh_account = "me-work"

[[identity]]
id = "lab"
name = "Lab Me"
email = "me@lab.test"
host = "github.com"
owners = ["lab-org"]
ssh_key = "~/.ssh/id_nopass"
"""


# ---------------------------------------------------------------- 1-5: local health

def test_passphrase_probe(home, cfg, fakes, capsys):
    setup(cfg, home, NOPASS_CONFIG)
    rc = run("keys", "--health")
    out = capsys.readouterr().out
    assert "⚠ no passphrase → ssh-keygen -p -f ~/.ssh/id_nopass" in out
    assert "✓ passphrase" in out and "registered: run with --online" in out
    assert rc == 0
    rc, data, _err = health_json(capsys)
    k = by_name(data)
    assert k["id_nopass"]["passphrase"] is False and k["id_work"]["passphrase"] is True
    assert all(e["stdin"] == b"" for e in fakes.entries("ssh-keygen") if "-y" in e["argv"])
    log = (fakes.path / "keygen-y.log").read_text().splitlines()
    assert log and all(line.endswith("devnull=True") for line in log)
    assert data["summary"] == {"fine": 1, "warnings": 1, "problems": 0}
    # the private key is never read by gitpro: only ssh-keygen's -y probe names it
    for e in fakes.entries("ssh-keygen"):
        if "-l" in e["argv"]:
            assert e["argv"][-1].endswith(".pub")


def test_weak_and_ecdsa(home, cfg, fakes, capsys):
    text = NOPASS_CONFIG.replace("id_nopass", "id_old").replace("id_work", "id_ecdsa")
    setup(cfg, home, text)
    rc = run("keys", "--health")
    out = capsys.readouterr().out
    assert rc == 1 and "✗ RSA 1024 is too weak" in out
    assert "⚠ ecdsa-256" in out
    rc, data, _ = health_json(capsys)
    k = by_name(data)
    assert rc == 1 and data["ok"] is False
    assert k["id_old"]["weak"] is True and k["id_old"]["state"] == "weak" and k["id_old"]["bits"] == 1024
    assert k["id_ecdsa"]["weak"] is False and k["id_ecdsa"]["type"] == "ecdsa-256"


def test_permissions(home, cfg, fakes, capsys):
    setup(cfg, home)
    (home / ".ssh/id_work").chmod(0o644)
    (home / ".ssh/id_personal").chmod(0o400)
    rc = run("keys", "--health")
    out = capsys.readouterr().out
    assert rc == 1 and "✗ accessible by others (0644) → chmod 600 ~/.ssh/id_work" in out
    _rc, data, _ = health_json(capsys)
    k = by_name(data)
    assert k["id_work"]["perms_ok"] is False and k["id_work"]["passphrase"] is None
    assert k["id_personal"]["perms_ok"] is True
    # no probe of a key with bad modes
    assert not any("id_work" in line for line in (fakes.path / "keygen-y.log").read_text().splitlines())
    (home / ".ssh/id_work").chmod(0o600)
    (home / ".ssh").chmod(0o755)
    rc = run("keys", "--health")
    out = capsys.readouterr().out
    assert rc == 0 and "⚠ ~/.ssh is readable by others; ssh accepts it, 0700 is safer → chmod 700 ~/.ssh" in out
    (home / ".ssh").chmod(0o777)
    rc, data, _ = health_json(capsys)
    assert rc == 1 and data["ssh_dir_mode"] == "0777"
    (home / ".ssh").chmod(0o700)


def test_agent(home, cfg, fakes, capsys):
    setup(cfg, home)
    (fakes.path / "agent.txt").write_text("SHA256:id_work\n")
    rc, data, _ = health_json(capsys)
    k = by_name(data)
    assert k["id_work"]["in_agent"] is True and k["id_work"]["state"] == "ok"
    assert k["id_personal"]["in_agent"] is False and k["id_personal"]["state"] == "locked"
    assert any("ssh-add ~/.ssh/id_personal" in f for f in k["id_personal"]["fixes"])
    assert rc == 0
    run("keys", "--health")
    out = capsys.readouterr().out
    assert "⚠ has a passphrase and is not in ssh-agent" in out and "→ ssh-add ~/.ssh/id_personal" in out
    (fakes.path / "agent.txt").unlink()
    rc = run("keys", "--health")
    out = capsys.readouterr().out
    assert rc == 0 and "· no ssh-agent" in out
    _rc, data, _ = health_json(capsys)
    assert data["agent"] is False and all(k["in_agent"] is None for k in data["keys"])


SHARED_CONFIG = """\
[default]
name = "Default Me"
email = "me@default.test"

[[identity]]
id = "work-github"
name = "Work Me"
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
ssh_key = "~/.ssh/id_shared"
gh_account = "me-work"

[[identity]]
id = "personal"
name = "Personal Me"
email = "me@personal.test"
host = "github.com"
owners = ["me"]
ssh_key = "~/.ssh/id_shared"
gh_account = "me"
"""


def test_shared_key(home, cfg, fakes, capsys):
    setup(cfg, home, SHARED_CONFIG)
    rc = run("keys", "--health")
    out = capsys.readouterr().out
    assert "⚠ shared by work-github and personal on github.com" in out and rc == 0


# ---------------------------------------------------------------- 6-7: --online

ONLINE_CONFIG = """\
[default]
name = "Default Me"
email = "me@default.test"

[[identity]]
id = "work-github"
name = "Work Me"
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
ssh_key = "~/.ssh/id_work"
gh_account = "me-work"

[[identity]]
id = "personal"
name = "Personal Me"
email = "me@personal.test"
host = "github.com"
owners = ["me"]
ssh_key = "~/.ssh/id_personal"
gh_account = "me"
"""


def test_online_registered(home, cfg, fakes, capsys):
    setup(cfg, home, ONLINE_CONFIG)
    with_gh(fakes, gh_row("id_work"))
    rc, data, err = health_json(capsys, "--online")
    k = by_name(data)
    assert k["id_work"]["registered"] == {"auth": True, "signing": False, "account": "me-work", "host": "github.com"}
    assert k["id_personal"]["registered"]["auth"] is False
    assert any("not registered on github.com (me)" in p for p in k["id_personal"]["problems"])
    assert "gitpro keys --upload --identity personal --online" in k["id_personal"]["fixes"]
    assert rc == 1 and err.count("online: asking") == 1
    assert TOKEN not in json.dumps(data) and TOKEN not in err
    lists = [e for e in fakes.entries("gh") if e["argv"][:2] == ["ssh-key", "list"]]
    assert lists and all(e["GH_TOKEN"] == "set" for e in lists)
    assert all(TOKEN not in " ".join(e["argv"]) for e in fakes.entries("gh"))
    rc = run("keys", "--health", "--online")
    out = capsys.readouterr().out
    assert out.startswith("online: asking github.com via gh")
    assert "✓ registered on github.com (me-work) as auth" in out
    assert "✗ not registered on github.com (me) → gitpro keys --upload --identity personal --online" in out
    assert TOKEN not in out


def test_online_old_gh_without_type_column(home, cfg, fakes, capsys):
    setup(cfg, home, ONLINE_CONFIG.replace('gh_account = "me-work"', 'gh_account = "me-work"\nsign = true'))
    with_gh(fakes, f"t\tssh-ed25519 {MAT['id_work']}\t2026-01-01T00:00:00Z\n")
    _rc, data, _ = health_json(capsys, "--online")
    k = by_name(data)
    assert k["id_work"]["registered"]["signing"] is None and k["id_work"]["registered"]["auth"] is True
    assert any("gh too old to list signing keys" in p for p in k["id_work"]["problems"])


def test_online_without_gh_or_glab(home, cfg, fakes, capsys, monkeypatch):
    setup(cfg, home)
    no_cli(monkeypatch, "gh", "glab")
    rc = run("keys", "--health", "--online")
    err = capsys.readouterr().err
    assert rc == 2 and "needs gh or glab" in err


# ---------------------------------------------------------------- 8-10: auth

AUTH_CONFIG = """\
[default]
name = "Default Me"
email = "me@default.test"

[[identity]]
id = "work-github"
name = "Work Me"
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
ssh_key = "~/.ssh/id_work"
ssh_alias = "github-work"
gh_account = "me-work"

[[identity]]
id = "lab"
name = "Lab Me"
email = "me@lab.test"
host = "gitlab.lab.test"
owners = ["lab"]
ssh_key = "~/.ssh/id_lab"
ssh_alias = "gitlab-lab"
ssh_user = "gitlab"
glab_account = "jane.doe"
"""

HI = "Hi me-work! You've successfully authenticated, but GitHub does not provide shell access.\n"


def ssh_says(fakes, answers):
    (fakes.path / "ssh.json").write_text(json.dumps(answers))


def auth_json(capsys, *extra):
    rc = run("auth", "--json", *extra)
    out = capsys.readouterr()
    data = json.loads(out.out)
    return rc, data, {r["who"]: r for r in data["results"]}, out.err


def test_auth_ok_mismatch_and_user(home, cfg, fakes, capsys):
    setup(cfg, home, AUTH_CONFIG)
    ssh_says(fakes, {"git@github-work": [1, HI],
                     "gitlab@gitlab-lab": [0, "Welcome to GitLab, @jane.doe!\n"]})
    rc = run("auth")
    cap = capsys.readouterr()
    out = cap.out
    assert rc == 0, cap.err
    assert out.startswith("online: connecting with ssh to github-work and gitlab-lab (ssh -T)")
    assert "✓ matches gh_account me-work" in out and "✓ matches glab_account jane.doe" in out
    rc, data, r, err = auth_json(capsys)
    assert rc == 0 and data["ok"] is True and "online: connecting" in err
    assert r["work-github"]["status"] == "ok" and r["work-github"]["login"] == "me-work"
    assert r["lab"]["login"] == "jane.doe"
    targets = [c[-1] for c in fakes.calls("ssh")]
    assert "gitlab@gitlab-lab" in targets and "git@github-work" in targets
    setup(cfg, home, AUTH_CONFIG.replace('gh_account = "me-work"', 'gh_account = "other"'))
    rc, data, r, _ = auth_json(capsys, "--identity", "work-github")
    assert rc == 1 and r["work-github"]["status"] == "mismatch" and list(r) == ["work-github"]


def test_auth_denied_unknown_host_locked(home, cfg, fakes, capsys):
    setup(cfg, home, AUTH_CONFIG)
    (fakes.path / "agent.txt").write_text("SHA256:id_work SHA256:id_lab\n")
    ssh_says(fakes, {"git@github-work": [255, "git@github.com: Permission denied (publickey).\n"],
                     "gitlab@gitlab-lab": [255, "Host key verification failed.\n"]})
    rc, _data, r, _ = auth_json(capsys)
    assert rc == 1
    assert r["work-github"]["status"] == "denied"
    assert r["work-github"]["fix"] == "gitpro keys --upload --identity work-github --online"
    assert r["lab"]["status"] == "unknown_host" and "ssh -T gitlab@gitlab.lab.test" in r["lab"]["fix"]
    run("auth")
    out = capsys.readouterr().out
    assert "is not in ~/.ssh/known_hosts" in out
    (fakes.path / "agent.txt").write_text("SHA256:id_lab\n")
    _rc, _data, r, _ = auth_json(capsys)
    assert r["work-github"]["status"] == "locked" and r["work-github"]["fix"] == "ssh-add ~/.ssh/id_work"
    for argv in fakes.calls("ssh"):
        if "-T" in argv:
            joined = " ".join(argv)
            assert "StrictHostKeyChecking=no" not in joined and "-o BatchMode=yes" in joined


def test_auth_without_alias_uses_identity_key(home, cfg, fakes, capsys):
    setup(cfg, home)
    ssh_says(fakes, {"git@github.com": [1, HI]})
    rc, _data, r, _ = auth_json(capsys, "--identity", "work-github")
    argv = fakes.calls("ssh")[-1]
    assert argv[argv.index("-i") + 1] == str(home / ".ssh/id_work") and "IdentitiesOnly=yes" in argv
    assert rc == 0 and r["work-github"]["status"] == "ok"


def test_auth_timeout_and_unknown_identity(home, cfg, fakes, capsys):
    setup(cfg, home, AUTH_CONFIG)
    fakes.add("ssh", "import time\ntime.sleep(5)")
    rc, _data, r, _ = auth_json(capsys, "--identity", "lab", "--timeout", "0.5")
    assert rc == 1 and r["lab"]["status"] == "timeout"
    assert r["lab"]["message"] == "gitlab.lab.test: timed out after 0.5 s"
    assert run("auth", "--identity", "nope") == 2
    assert "no identity named nope" in capsys.readouterr().err


def test_auth_without_ssh(home, cfg, fakes, capsys, monkeypatch):
    setup(cfg, home)
    no_cli(monkeypatch, "ssh")
    assert run("auth") == 2 and "ssh not found" in capsys.readouterr().err


def test_parse_greeting():
    from gitpro.health import parse_greeting
    assert parse_greeting(HI) == ("github", "me-work")
    assert parse_greeting("Welcome to GitLab, @jane.doe!") == ("gitlab", "jane.doe")
    assert parse_greeting("authenticated via ssh key.\n\nYou can use git to connect to Bitbucket. "
                          "Shell access is disabled.\nlogged in as jane.\n") == ("bitbucket", "jane")
    assert parse_greeting("logged in as jane") == ("bitbucket", "jane")
    assert parse_greeting("Hi there, bob! You've successfully authenticated with the key named x") == ("gitea", "bob")
    assert parse_greeting("remote: Shell access is not supported.") == ("azure", None)
    assert parse_greeting("Permission denied (publickey).") == (None, None)


# ---------------------------------------------------------------- 11-12: upload

UPLOAD_CONFIG = ONLINE_CONFIG.replace('gh_account = "me-work"', 'gh_account = "me-work"\nsign = true')


def adds(fakes):
    return [c for c in fakes.calls("gh") if c[:2] == ["ssh-key", "add"]]


def test_upload_missing_key(home, cfg, fakes, capsys):
    setup(cfg, home, UPLOAD_CONFIG)
    with_gh(fakes, gh_row("id_personal", kid=7))
    rc = run("keys", "--upload", "--online", "-y", "--identity", "work-github")
    out = capsys.readouterr()
    pub = str(home / ".ssh/id_work.pub")
    assert adds(fakes) == [["ssh-key", "add", pub, "--title", "id_work@test"],
                           ["ssh-key", "add", pub, "--type", "signing", "--title", "id_work@test"]]
    assert rc == 0, out.err
    assert "✓ uploaded ~/.ssh/id_work.pub to github.com (me-work): auth, signing" in out.out
    assert "↩ undo: gh ssh-key delete 901 --yes && gh ssh-key delete 902 --yes" in out.out
    assert out.out.startswith("online: asking github.com via gh (account me-work)")
    assert TOKEN not in out.out + out.err
    assert all(e["GH_TOKEN"] == "set" for e in fakes.entries("gh") if e["argv"][0] == "ssh-key")
    # now registered: nothing more to add
    rc = run("keys", "--upload", "--online", "-y", "--identity", "work-github")
    assert rc == 0 and len(adds(fakes)) == 2 and "already on github.com (me-work)" in capsys.readouterr().out


def test_upload_already_registered_and_title(home, cfg, fakes, capsys):
    setup(cfg, home, UPLOAD_CONFIG)
    with_gh(fakes, gh_row("id_personal", kid=7))
    assert run("keys", "--upload", "--online", "-y", "--identity", "personal") == 0
    assert adds(fakes) == []
    assert "already on github.com (me)" in capsys.readouterr().out
    rc = run("keys", "--upload", "--online", "-y", "--identity", "work-github", "--title", "laptop", "--json")
    out = capsys.readouterr()
    data = json.loads(out.out)
    assert rc == 0 and data["ok"] is True and data["uploaded"][0]["types"] == ["auth", "signing"]
    assert all(c[c.index("--title") + 1] == "laptop" for c in adds(fakes))
    assert "online: asking" in out.err


def test_upload_scope_error(home, cfg, fakes, capsys):
    setup(cfg, home, UPLOAD_CONFIG)
    with_gh(fakes, "")
    (fakes.path / "gh_add_fail").write_text("")
    rc = run("keys", "--upload", "--online", "-y", "--identity", "work-github")
    err = capsys.readouterr().err
    assert rc == 1
    assert ("⚠ gh token for me-work lacks the admin:public_key (and admin:ssh_signing_key) scope; run: "
            "gh auth refresh -h github.com -s admin:public_key -s admin:ssh_signing_key") in err
    assert len(adds(fakes)) == 1


def test_upload_refusals(home, cfg, fakes, capsys):
    setup(cfg, home, UPLOAD_CONFIG)
    with_gh(fakes, "")
    assert run("keys", "--upload") == 2
    assert "keys --upload needs the network" in capsys.readouterr().err
    assert run("keys", "--upload", "--online") == 2
    assert "no terminal to confirm on" in capsys.readouterr().err
    assert fakes.calls("gh") == []


def test_upload_declined(home, cfg, fakes, capsys, monkeypatch):
    setup(cfg, home, UPLOAD_CONFIG)
    with_gh(fakes, "")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    from gitpro import view
    asked = []
    monkeypatch.setattr(view, "confirm", lambda q, *a, **k: asked.append(q) or False)
    rc = run("keys", "--upload", "--online", "--identity", "work-github")
    assert rc == 1 and adds(fakes) == []
    assert asked == ['upload ~/.ssh/id_work.pub to github.com account me-work as auth key "id_work@test" '
                     'and signing key?']


# ---------------------------------------------------------------- 13: keys unchanged, flags, wiring

def test_keys_plain_and_help(home, cfg, fakes, capsys):
    setup(cfg, home)
    assert run("keys") == 0
    out = capsys.readouterr().out
    assert "~/.ssh/id_work" in out and "registered" not in out
    with pytest.raises(SystemExit):
        run("keys", "--help")
    assert "--health" in capsys.readouterr().out
    assert run("keys", "--online") == 2
    assert "needs --health or --upload" in capsys.readouterr().err
    assert run("keys", "--health", "--upload") == 2


def test_capabilities(home):
    from gitpro import args, cli
    cli.build_parser()
    assert {"auth", "keys.health", "keys.upload"} <= set(args.CAPABILITIES)


def test_no_keys_configured(home, cfg, fakes, capsys):
    cfg('[default]\nname = "Me"\nemail = "me@x.test"\n', apply=False)
    assert run("keys", "--health") == 0
    assert "no SSH keys configured" in capsys.readouterr().out


# ---------------------------------------------------------------- doctor (16) with the real module

def test_doctor_uses_real_health(home, cfg, fakes, capsys):
    import gitpro.health as health
    setup(cfg, home, NOPASS_CONFIG)
    data = health.check_keys(_load(home), online=False, announce=False, home=home)
    assert data["summary"] == "2 keys · 1 without passphrase" and data["status"] == "warning"
    assert "command" not in data
    rc = run("doctor", "--json")
    out = capsys.readouterr()
    s = {x["name"]: x for x in json.loads(out.out)["sections"]}
    assert s["keys"]["status"] == "warning" and "1 without passphrase" in s["keys"]["summary"]
    assert any("~/.ssh/id_nopass: no passphrase" in json.dumps(p) for p in s["keys"]["problems"])
    assert s["forge"]["status"] == "skipped"
    assert rc in (0, 1)


def test_doctor_online_announces_once(home, cfg, fakes, capsys):
    setup(cfg, home, ONLINE_CONFIG)
    with_gh(fakes, gh_row("id_work") + gh_row("id_personal"))
    ssh_says(fakes, {"git@github.com": [1, HI]})
    run("doctor", "--json", "--online")
    out = capsys.readouterr()
    s = {x["name"]: x for x in json.loads(out.out)["sections"]}
    assert (out.err + out.out).count("online: asking") <= 1
    assert isinstance(s["keys"]["summary"], str) and isinstance(s["forge"]["summary"], str)
    assert TOKEN not in out.out + out.err


def _load(home):
    from gitpro import config
    return config.load_all(home / ".config/gitpro.toml")


# ---------------------------------------------------------------- QA follow-ups (mutation survivors, review fixes)

@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660])
def test_private_key_mask(home, cfg, fakes, capsys, mode):
    setup(cfg, home)
    (home / ".ssh/id_work").chmod(mode)
    rc, data, _ = health_json(capsys)
    assert rc == 1 and by_name(data)["id_work"]["perms_ok"] is False
    assert any(f"accessible by others ({mode:04o})" in p for p in by_name(data)["id_work"]["problems"])


def test_group_writable_ssh_dir(home, cfg, fakes, capsys):
    setup(cfg, home)
    (home / ".ssh").chmod(0o720)
    rc = run("keys", "--health")
    out = capsys.readouterr().out
    (home / ".ssh").chmod(0o700)
    assert rc == 1 and "✗ ~/.ssh is writable by others (0720) → chmod 700 ~/.ssh" in out


def test_pub_permissions_and_upload_refusal(home, cfg, fakes, capsys):
    setup(cfg, home, UPLOAD_CONFIG)
    (home / ".ssh/id_work.pub").chmod(0o664)
    rc, data, _ = health_json(capsys)
    k = by_name(data)["id_work"]
    assert rc == 0 and k["perms_ok"] is True and "~/.ssh/id_work.pub is writable by its group" in k["problems"]
    assert k["passphrase"] is True          # a loose .pub does not stop the probe
    (home / ".ssh/id_work.pub").chmod(0o666)
    rc, data, _ = health_json(capsys)
    assert rc == 1 and "~/.ssh/id_work.pub is writable by everyone" in by_name(data)["id_work"]["problems"]
    (home / ".ssh/id_work.pub").chmod(0o664)
    with_gh(fakes, "")
    rc = run("keys", "--upload", "--online", "-y", "--identity", "work-github")
    err = capsys.readouterr().err
    assert rc == 1 and "✗ not uploading ~/.ssh/id_work.pub: it is writable by its group" in err
    assert adds(fakes) == []


class _OtherOwner:
    """health's view of os with another uid, so every file looks owned by someone else."""

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def getuid():
        return os.getuid() + 4242


def test_other_owner(home, cfg, fakes, capsys, monkeypatch):
    import gitpro.health as health
    setup(cfg, home)
    real = home / "keys/real_work"
    real.parent.mkdir()
    (home / ".ssh/id_work").rename(real)
    (home / ".ssh/id_work").symlink_to(real)
    monkeypatch.setattr(health, "os", _OtherOwner())
    rc, data, _ = health_json(capsys)
    k = by_name(data)["id_work"]
    assert rc == 1 and "owned by another user; ssh refuses it" in k["problems"]
    fix = next(f for f in k["fixes"] if "chown" in f)
    assert fix.startswith("sudo chown ") and "~/keys/real_work" in fix and ".ssh/id_work" not in fix


def test_registered_on_other_account(home, cfg, fakes, capsys):
    setup(cfg, home, ONLINE_CONFIG)
    with_gh(fakes, "")
    (fakes.path / "gh_keys_me-work.txt").write_text(gh_row("id_work") + gh_row("id_personal", kid=2))
    (fakes.path / "gh_keys_me.txt").write_text("")
    _rc, data, _ = health_json(capsys, "--online")
    k = by_name(data)["id_personal"]
    assert "registered on github.com (me-work) but identity personal maps to me" in k["problems"]
    assert not any(p.startswith("not registered") for p in k["problems"])


def test_no_gh_login_for_account(home, cfg, fakes, capsys):
    setup(cfg, home, ONLINE_CONFIG)
    with_gh(fakes, gh_row("id_work"))
    (fakes.path / "gh_nologin_me").write_text("")
    rc, data, _ = health_json(capsys, "--online")
    k = by_name(data)
    assert "no gh login for me on github.com; registered check skipped" in k["id_personal"]["problems"]
    assert k["id_work"]["registered"]["auth"] is True and rc == 0


def test_ssh_keygen_missing(home, cfg, fakes, capsys, monkeypatch):
    setup(cfg, home)
    no_cli(monkeypatch, "ssh-keygen")
    assert run("keys", "--health") == 2
    assert "ssh-keygen not found" in capsys.readouterr().err


def test_hardware_key(home, cfg, fakes, capsys):
    make_key(home, "id_sk")
    setup(cfg, home, NOPASS_CONFIG.replace("id_nopass", "id_sk"))
    rc = run("keys", "--health")
    out = capsys.readouterr().out
    assert rc == 0 and "· hardware key" in out and "· no passphrase (the hardware key guards it)" in out
    assert "⚠ no passphrase" not in out


def test_forge_online_config_without_cli_falls_back(home, cfg, fakes, capsys, monkeypatch):
    setup(cfg, home, DEFAULT_CONFIG_ONLINE)
    no_cli(monkeypatch, "gh", "glab")
    rc = run("keys", "--health")
    out = capsys.readouterr()
    assert rc == 0 and "checking offline only" in out.err and "registered: run with --online" in out.out
    assert run("keys", "--health", "--online") == 2


def test_fifo_pub_does_not_hang(home, cfg, fakes, capsys):
    setup(cfg, home)
    pub = home / ".ssh/id_work.pub"
    pub.unlink()
    os.mkfifo(pub)
    rc, data, _ = health_json(capsys)
    assert rc == 1 and "~/.ssh/id_work.pub is not a regular file" in by_name(data)["id_work"]["problems"]


def test_auth_argv_message_and_config(home, cfg, fakes, capsys):
    setup(cfg, home, AUTH_CONFIG)
    ssh_says(fakes, {"git@github-work": [1, "Warning: banner text\n" + HI]})
    _rc, _data, r, _ = auth_json(capsys, "--identity", "work-github")
    assert r["work-github"]["message"].startswith("Hi me-work!")
    argv = fakes.calls("ssh")[-1]
    for opt in ("BatchMode=yes", "StrictHostKeyChecking=yes", "UpdateHostKeys=no"):
        assert argv[argv.index(opt) - 1] == "-o"
    assert argv[argv.index("-F") + 1] == os.devnull        # the temp HOME is not the login home
    (home / ".ssh/config").write_text("Host *\n")
    auth_json(capsys, "--identity", "work-github")
    argv = fakes.calls("ssh")[-1]
    assert argv[argv.index("-F") + 1] == str(home / ".ssh/config")


def test_auth_timeout_kills_process_group(home, cfg, fakes, capsys):
    import time
    setup(cfg, home, AUTH_CONFIG)
    pidfile = fakes.path / "child.pid"
    fakes.add("ssh", f"""
import subprocess, time
child = subprocess.Popen(["sleep", "30"])
open({str(pidfile)!r}, "w").write(str(child.pid))
time.sleep(30)
""")
    rc, _data, r, _ = auth_json(capsys, "--identity", "lab", "--timeout", "1")
    assert rc == 1 and r["lab"]["status"] == "timeout"
    pid = int(pidfile.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("the ProxyCommand-like child outlived the timeout")


DEFAULT_CONFIG_ONLINE = "[forge]\nonline = true\n\n" + ONLINE_CONFIG

GLAB = r'''
import json
here = os.path.dirname(os.path.abspath(sys.argv[0]))
who = open(os.path.join(here, "glab_user")).read().strip()
path = os.path.join(here, "glab_keys.json")
keys = json.load(open(path)) if os.path.exists(path) else []
if argv[:2] == ["auth", "status"]:
    sys.stderr.write("gitlab.com\n  ✓ Logged in to gitlab.com as %s (keyring)\n" % who)
    sys.exit(0)
if argv[:2] == ["ssh-key", "list"]:
    print(json.dumps(keys) if "--output" in argv else "")
    sys.exit(0)
if argv[:2] == ["ssh-key", "add"]:
    pub = open(argv[2]).read().split()
    usage = argv[argv.index("--usage-type") + 1] if "--usage-type" in argv else "auth_and_signing"
    keys.append({"id": 40 + len(keys), "title": argv[argv.index("--title") + 1], "key": pub[0] + " " + pub[1],
                 "usage_type": usage})
    json.dump(keys, open(path, "w"))
    sys.exit(0)
sys.exit(1)
'''

GLAB_CONFIG = """\
[default]
name = "Default Me"
email = "me@default.test"

[[identity]]
id = "gl-work"
name = "Work Me"
email = "me@corp.test"
host = "gitlab.com"
owners = ["corp"]
ssh_key = "~/.ssh/id_work"
glab_account = "me-work"
sign = true
"""


def with_glab(fakes, user="me-work", keys=None):
    fakes.add("glab", GLAB)
    (fakes.path / "glab_user").write_text(user)
    if keys is not None:
        (fakes.path / "glab_keys.json").write_text(json.dumps(keys))


def glab_adds(fakes):
    return [c for c in fakes.calls("glab") if c[:2] == ["ssh-key", "add"]]


def test_glab_upload_and_registered(home, cfg, fakes, capsys, monkeypatch):
    setup(cfg, home, GLAB_CONFIG)
    no_cli(monkeypatch, "gh")
    with_glab(fakes)
    rc = run("keys", "--upload", "--online", "-y")
    out = capsys.readouterr()
    pub = str(home / ".ssh/id_work.pub")
    assert rc == 0, out.err
    assert glab_adds(fakes) == [["ssh-key", "add", pub, "--title", "id_work@test", "--usage-type",
                                 "auth_and_signing"]]
    assert "✓ uploaded ~/.ssh/id_work.pub to gitlab.com (me-work): auth, signing" in out.out
    assert "↩ undo: glab ssh-key delete 40" in out.out
    assert all(e["GITLAB_TOKEN"] == "unset" for e in fakes.entries("glab"))
    _rc, data, _ = health_json(capsys, "--online")
    assert by_name(data)["id_work"]["registered"] == {"auth": True, "signing": True, "account": "me-work",
                                                      "host": "gitlab.com"}
    assert ["ssh-key", "list", "--output", "json"] in fakes.calls("glab")


def test_glab_auth_only_key_is_not_added_again(home, cfg, fakes, capsys, monkeypatch):
    setup(cfg, home, GLAB_CONFIG)
    no_cli(monkeypatch, "gh")
    with_glab(fakes, keys=[{"id": 5, "key": f"ssh-ed25519 {MAT['id_work']}", "usage_type": "auth"}])
    rc = run("keys", "--upload", "--online", "-y")
    err = capsys.readouterr().err
    assert rc == 1 and glab_adds(fakes) == []
    assert "change its usage type to auth_and_signing in GitLab" in err
    _rc, data, _ = health_json(capsys, "--online")
    reg = by_name(data)["id_work"]["registered"]
    assert reg["auth"] is True and reg["signing"] is False


def test_glab_wrong_login_message(home, cfg, fakes, capsys, monkeypatch):
    setup(cfg, home, GLAB_CONFIG)
    no_cli(monkeypatch, "gh")
    with_glab(fakes, user="someone-else")
    rc = run("keys", "--upload", "--online", "-y")
    err = capsys.readouterr().err
    assert rc == 1 and glab_adds(fakes) == []
    assert ("glab is logged in to gitlab.com as someone-else, not me-work → glab auth login --hostname gitlab.com"
            in err)
