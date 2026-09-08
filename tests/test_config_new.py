"""The schema added by package 01: new keys and tables, frozen objects, load_all / load_scan."""
import re

import pytest

from conftest import write_config
from gitpro import config

D = '[default]\nname = "Me"\nemail = "me@home.test"\n'
I = '[[identity]]\nid = "w"\nname = "Me"\nemail = "me@corp.test"\nhost = "git.corp.test"\n'


def load_all(home, text):
    return config.load_all(write_config(home, text), "user config")


def bad(home, text, message):
    with pytest.raises(config.ConfigError, match=re.escape(message)):
        load_all(home, text)


def test_defaults_of_new_tables(home):
    c = load_all(home, D + I)
    assert c.scan == config.Scan(roots=("~",), exclude=(), stale_days=180)
    assert c.guard.hooks is False and c.guard.on_mismatch == "block" and c.guard.chain_to == ""
    assert c.clone.dir is None and c.clone.layout == "{host}/{owner}/{repo}"
    assert c.forge.online is False and c.forge.similar is False
    assert c.agents.hook == "block" and "noreply@anthropic.com" in c.agents.bot_emails
    assert c.policy.files == ()
    assert c.how == "user config" and c.path.name == "gitpro.toml"
    assert c.identities[0].file == "~/.gitconfig-w"


def test_scan_roots(home):  # 1
    c = load_all(home, D + '[scan]\nroots = ["~", "/srv"]\nexclude = ["**/vendor"]\nstale_days = 30\n')
    assert c.scan.roots == ("~", "/srv") and isinstance(c.scan.roots, tuple)
    assert c.scan.exclude == ("**/vendor",) and c.scan.stale_days == 30
    bad(home, D + '[scan]\nroots = ["src"]\n', "[scan]: roots must start with ~/ or /")
    bad(home, D + '[scan]\nstale_days = 0\n', "[scan]: stale_days must be a positive integer")
    bad(home, D + '[scan]\nstale_days = true\n', "[scan]: stale_days must be a positive integer")
    bad(home, D + '[scan]\nroots = "~"\n', "[scan]: roots must be a list")


def test_guard(home):  # 2
    assert load_all(home, D + '[guard]\nhooks = true\non_mismatch = "warn"\nchain_to = "~/.githooks"\n').guard \
        == config.Guard(hooks=True, on_mismatch="warn", chain_to="~/.githooks")
    bad(home, D + '[guard]\non_mismatch = "ask"\n', '[guard]: on_mismatch must be "block" or "warn"')
    bad(home, D + '[guard]\nchain_to = "~/.config/gitpro/hooks"\n', "[guard]: chain_to")
    bad(home, D + '[guard]\nchain_to = "hooks"\n', "[guard]: chain_to must start with ~/ or /")
    bad(home, D + '[guard]\nhooks = "yes"\n', "[guard].hooks: must be true or false")


def test_guard_chain_to_through_a_symlink_is_refused(home):
    (home / ".config/gitpro/hooks").mkdir(parents=True)
    (home / "link").symlink_to(home / ".config/gitpro/hooks")
    bad(home, D + '[guard]\nchain_to = "~/link"\n', "[guard]: chain_to must not be gitpro's own hooks dir")


@pytest.mark.parametrize("layout, ok", [
    ("{host}/{owner}/{repo}", True), ("{id}/{repo}", True), ("src/{owner}-{repo}", True),
    ("{user}/{repo}", False), ("../x", False), ("/abs/{repo}", False), ("a b/{repo}", False), ("{repo}/..", False),
])
def test_clone_layout(home, layout, ok):  # 3
    text = D + f'[clone]\ndir = "~/src"\nlayout = "{layout}"\n'
    if ok:
        assert load_all(home, text).clone.layout == layout
    else:
        bad(home, text, "[clone]: layout may use only {host} {owner} {repo} {id} and [A-Za-z0-9._/-]")


def test_trailers_and_hashable(home):  # 4
    c = load_all(home, D + '[default.trailers]\nco_authored_by = "forbid"\nforbid_emails = ["*@evil.test"]\n' + I)
    assert c.default.trailers.co_authored_by == "forbid"
    assert c.default.trailers.forbid_emails == ("*@evil.test",)
    assert c.identities[0].trailers.co_authored_by is None and c.identities[0].trailers.forbid_emails is None
    hash(c)
    hash(c.default)
    hash(c.identities[0])
    assert isinstance(c.identities, tuple) and isinstance(c.identities[0].owners, tuple)
    with pytest.raises(Exception):
        c.identities[0].email = "x@y.test"
    bad(home, D + '[default.trailers]\nco_authored_by = "maybe"\n',
        '[default.trailers]: co_authored_by must be "allow", "forbid" or "require"')
    bad(home, D + I + '[identity.trailers]\nco_authored_by = "no"\n',
        "identity 'w'.trailers: co_authored_by must be")
    bad(home, D + '[default.trailers]\nfoo = 1\n', "[default.trailers]: unknown key(s) foo")


def test_identity_trailers(home):
    c = load_all(home, D + I + '[identity.trailers]\nco_authored_by = "require"\n')
    assert c.identities[0].trailers == config.Trailers(co_authored_by="require")


def test_accounts_and_paths(home):  # 5
    c = load_all(home, D.replace("[default]\n", '[default]\ngh_account = "janedoe"\nclone_dir = "~/src"\n')
                 + I + 'gh_account = "jane-work"\nglab_account = "jane.doe"\nclone_dir = "~/work"\n'
                 + 'policy = "~/.config/gitpro/p.toml"\n')
    assert c.default.gh_account == "janedoe" and c.default.clone_dir == "~/src"
    i = c.identities[0]
    assert (i.gh_account, i.glab_account, i.clone_dir, i.policy) == \
        ("jane-work", "jane.doe", "~/work", "~/.config/gitpro/p.toml")
    bad(home, D + I + 'gh_account = "bad name"\n', "identity 'w': gh_account must match [A-Za-z0-9][A-Za-z0-9._-]*")
    bad(home, D + I + 'glab_account = "-x"\n', "identity 'w': glab_account must match")
    bad(home, D + I + 'policy = "p.toml"\n', "identity 'w': policy must start with ~/ or /")
    bad(home, D + I + 'clone_dir = "work"\n', "identity 'w': clone_dir must start with ~/ or /")
    bad(home, D + '[policy]\nfiles = ["p.toml"]\n', "[policy]: files must start with ~/ or /")
    bad(home, D + '[clone]\ndir = "src"\n', "[clone]: dir must start with ~/ or /")


def test_control_chars_in_globs(home):  # 6
    bad(home, D + '[agents]\nbot_emails = ["x\\u0001"]\n', "control characters not allowed")
    bad(home, D + '[agents]\nhook = "deny"\n', '[agents]: hook must be "block", "warn" or "off"')
    bad(home, D + '[agents]\nbot_emails = [1]\n', "[agents]: bot_emails must be a list of strings")
    assert load_all(home, D + '[agents]\nhook = "off"\nbot_emails = []\n').agents == config.Agents("off", ())


def test_unknown_table(home):  # 7
    bad(home, D + "[foo]\nx = 1\n",
        "unknown key(s) foo (allowed: agents, clone, default, forge, guard, identity, policy, scan)")
    bad(home, D + "[forge]\nonline = 1\n", "[forge].online: must be true or false")
    bad(home, D + "[forge]\nofline = true\n", "[forge]: unknown key(s) ofline (allowed: online, similar)")


def test_load_keeps_old_shape(home):  # 8
    d, ids = config.load(write_config(home, D + I))
    assert isinstance(d, config.Default) and isinstance(ids, list) and isinstance(ids[0], config.Identity)


def test_load_scan_is_lenient(home):  # 9
    path = write_config(home, '[default]\nemail = "jane@personal.example"\n[scan]\nroots = ["/srv"]\n')
    with pytest.raises(config.ConfigError):
        config.load_all(path)
    assert config.load_scan(path).roots == ("/srv",)
    assert config.load_scan(write_config(home, D)) == config.Scan()
    with pytest.raises(config.ConfigError, match=re.escape("[scan]: roots must start with ~/ or /")):
        config.load_scan(write_config(home, D + '[scan]\nroots = ["x"]\n'))
    with pytest.raises(config.ConfigError):
        config.load_scan(write_config(home, "[default\n"))


def test_config_wraps_old_callers():
    c = config.Config(config.Default(), [config.Identity(id="w", name="n", email="e@x.test", host="h.test")])
    assert isinstance(c.identities, tuple) and c.scan == config.Scan()
