import * as assert from "node:assert/strict";
import { test } from "node:test";
import type { Capabilities } from "../capabilities";
import { actionsFor, containerFix, esc, inContainer, pickFolder, scmRoot, sshKeyOf, stateFrom, tooltipFrom,
  undoPath, WhyJson } from "../identityBar";

const unset = { value: null, origin: null, scope: null };
const gen = "file:/home/me/.gitconfig-work-github";

/** `gitpro why --json` for ~/work/app, as 01 prints it (captured from a scratch HOME). */
function match(): WhyJson {
  return {
    ok: true,
    repo: "/home/me/work/app",
    config: { path: "/home/me/.config/gitpro.toml", how: "user config" },
    remotes: { origin: "git@github.com:corp-org/app.git" },
    expected: {
      id: "work-github", name: "Work Me", email: "me@corp.test", ssh_key: "/home/me/.ssh/id_work", sign: false,
      file: "/home/me/.gitconfig-work-github",
      match: { remote: "origin", url: "git@github.com:corp-org/app.git", pattern: "git@github.com:corp-org/**",
        rule: "matched origin by pattern git@github.com:corp-org/**" },
    },
    effective: {
      name: { value: "Work Me", origin: gen, scope: "generated" },
      email: { value: "me@corp.test", origin: gen, scope: "generated" },
      signingkey: unset, gpgsign: unset,
      sshcommand: { value: "ssh -i ~/.ssh/id_work -o IdentitiesOnly=yes", origin: gen, scope: "generated" },
      pinned: false,
    },
    problems: [],
  };
}

const OVERRIDE_ARGV = ["git", "-C", "/home/me/work/app", "config", "--unset", "user.email"];

function override(): WhyJson {
  const j = match();
  j.ok = false;
  j.effective.email = { value: "old@personal.test", origin: "file:/home/me/work/app/.git/config", scope: "repo" };
  j.problems = [{ code: "override",
    message: "user.email is set in the repo's .git/config and overrides the work-github identity",
    fix: "git -C /home/me/work/app config --unset user.email", fix_argv: OVERRIDE_ARGV }];
  return j;
}

function noIdentity(): WhyJson {
  const j = match();
  j.ok = false;
  j.remotes = { origin: "git@gitlab.com:someone/foo.git" };
  j.expected = { id: null, name: null, email: null, ssh_key: null, sign: false, file: "/home/me/.gitconfig-identities",
    match: { remote: null, url: null, pattern: null, rule: "no identity pattern matches git@gitlab.com:someone/foo.git" } };
  j.effective.name = unset;
  j.effective.email = unset;
  j.effective.sshcommand = unset;
  j.problems = [{ code: "no_identity", message: "no identity for git@gitlab.com:someone/foo.git",
    fix: "add an identity: gitpro scan --suggest --repo /home/me/work/app",
    fix_argv: ["gitpro", "scan", "--suggest", "--repo", "/home/me/work/app"] }];
  return j;
}

function caps(...names: string[]): Capabilities {
  return { version: "0.2.0", has: (c) => names.includes(c) };
}

test("stateFrom: match shows identity and key, red on a problem, warning without identity", () => {
  assert.deepEqual(stateFrom(match(), true), { kind: "match", text: "$(account) work-github · id_work",
    background: undefined });
  assert.equal(stateFrom(match(), false).text, "$(account) work-github");

  const def = match();
  def.expected.id = null;
  def.expected.ssh_key = null;
  def.effective.sshcommand = unset;
  assert.equal(stateFrom(def, true).text, "$(account) me@corp.test");

  assert.deepEqual(stateFrom(override(), true), { kind: "problem", text: "$(error) old@personal.test ≠ work-github",
    background: "error" });
  const none = stateFrom(noIdentity(), true);
  assert.equal(none.kind, "none");
  assert.equal(none.text, "$(warning) no git identity");
  assert.equal(none.background, "warning");
});

test("sshKeyOf reads -i in its forms", () => {
  assert.equal(sshKeyOf("ssh -i ~/.ssh/id_work -o IdentitiesOnly=yes"), "~/.ssh/id_work");
  assert.equal(sshKeyOf("ssh -i'/a b/k'"), "/a b/k");
  assert.equal(sshKeyOf("ssh -o X=y"), undefined);
  assert.equal(sshKeyOf(null), undefined);
});

test("tooltipFrom: expected, effective, problems with fixes, pinned; values escaped", () => {
  const ok = tooltipFrom(match(), { home: "/home/me" });
  for (const s of ["expected", "effective", "key", "signing", "remotes", "pinned", "✓ identity matches"]) {
    assert.ok(ok.includes(s), `missing ${s}`);
  }
  assert.match(ok, /~\/\.ssh\/id\\_work via core\.sshcommand/);

  const bad = override();
  bad.effective.name = { value: "Evil <b>*bold*</b> my_name", origin: "file:/x", scope: "repo" };
  bad.effective.pinned = true;
  const t = tooltipFrom(bad);
  assert.match(t, /✗ user\.email is set in the repo's \.git\/config/);
  assert.match(t, /→ git -C \/home\/me\/work\/app config --unset user\.email/);
  assert.match(t, /\| pinned \| yes/);
  assert.ok(!t.includes("<b>"), "raw < in the tooltip");
  assert.ok(t.includes("&lt;b&gt;"));
  assert.ok(t.includes("\\*bold\\*"), "* not escaped");
  assert.ok(t.includes("my\\_name"), "_ not escaped");
  assert.ok(!t.includes("identity matches"));
  assert.equal(esc("a|b"), "a\\|b");
});

test("tooltipFrom: guard and policy when present, other folders, container fix line", () => {
  const j = match();
  j.guard = { installed: true, hooks: "pre-commit, pre-push" };
  j.policy = "ok";
  const t = tooltipFrom(j, { others: [{ folder: "/home/me/src/b", text: "$(account) personal" }], home: "/home/me" });
  assert.match(t, /\| guard \| installed: true, hooks: pre-commit, pre-push \|/);
  assert.match(t, /\| policy \| ok \|/);
  assert.match(t, /- \\~\/src\/b: personal/);

  const inside = tooltipFrom(noIdentity(), { inContainer: true });
  assert.ok(inside.includes(esc(containerFix("/home/me/work/app"))));
  assert.match(tooltipFrom(noIdentity()), /→ add an identity: gitpro scan --suggest --repo/);
});

test("actionsFor: one fix per problem using the CLI's argv, then the always-items", () => {
  const always = ["Explain (gitpro why)", "Open config", "Show Log"];
  assert.deepEqual(actionsFor(match(), caps()).map((a) => a.label), always);

  const o = actionsFor(override(), caps());
  assert.equal(o[0].label, "Unset user.email in .git/config");
  assert.equal(o[0].kind, "argv");
  assert.deepEqual(o[0].argv, OVERRIDE_ARGV);

  const stale = match();
  stale.problems = [{ code: "pin_stale", message: "the repo's pin still says ...", fix: "gitpro pin /home/me/work/app",
    fix_argv: ["gitpro", "pin", "/home/me/work/app"] },
  { code: "not_applied", message: "not applied", fix: "gitpro apply", fix_argv: ["gitpro", "apply"] },
  { code: "global_override", message: "beats", fix: "gitpro apply", fix_argv: ["gitpro", "apply"] },
  { code: "env_override", message: "env", fix: "unset GIT_AUTHOR_EMAIL", fix_argv: null }];
  const s = actionsFor(stale, caps());
  assert.deepEqual(s.map((a) => a.label), ["Re-pin this repo", "Apply config", ...always]);
  assert.deepEqual(s[0].argv, ["gitpro", "pin", "/home/me/work/app"]);
  assert.equal(s[1].command, "gitpro.apply");

  assert.equal(actionsFor(noIdentity(), caps())[0].label, "Suggest an identity…");

  // remotes of different identities: the pin names the remote it settles on
  const two = match();
  const pArgv = ["gitpro", "pin", "-y", "--remote", "origin", "/home/me/work/app"];
  two.problems = [{ code: "ambiguous", message: "the remotes belong to different identities", fix: pArgv.join(" "),
    fix_argv: pArgv }];
  const t = actionsFor(two, caps());
  assert.equal(t[0].label, "Pin origin's identity");
  assert.deepEqual(t[0].argv, pArgv);

  // 01's fix for a global key/sign mismatch edits ~/.gitconfig, not the repo: the label names the file.
  const global = match();
  const gArgv = ["git", "config", "--file", "/home/me/.gitconfig", "--unset", "commit.gpgsign"];
  global.problems = [{ code: "sign_mismatch", message: "commit.gpgsign in ~/.gitconfig beats the identity",
    fix: "git config --file /home/me/.gitconfig --unset commit.gpgsign", fix_argv: gArgv }];
  const g = actionsFor(global, caps(), "/home/me");
  assert.equal(g[0].label, "Unset commit.gpgsign in ~/.gitconfig");
  assert.deepEqual(g[0].argv, gArgv);
  assert.equal(actionsFor(global, caps())[0].label, "Unset commit.gpgsign in /home/me/.gitconfig");

  const all = actionsFor(match(), caps("env", "doctor")).map((a) => a.label);
  assert.deepEqual(all, ["Explain (gitpro why)", "Copy env for terminal", "Run doctor", "Open config", "Show Log"]);
  assert.ok(!actionsFor(match(), caps("doctor")).some((a) => a.label === "Copy env for terminal"));
  assert.deepEqual(actionsFor(undefined, caps()).map((a) => a.label), always);
});

test("scmRoot: a Source Control repo's file root, nothing else", () => {
  assert.equal(scmRoot({ rootUri: { scheme: "file", fsPath: "/w/app" } }), "/w/app");
  assert.equal(scmRoot({ rootUri: { scheme: "vscode-vfs", fsPath: "/w/app" } }), undefined);
  assert.equal(scmRoot({ rootUri: undefined }), undefined);
  assert.equal(scmRoot(undefined), undefined);
  assert.equal(scmRoot("not a repo"), undefined);
});

test("pickFolder: active editor's folder, else last active, else first", () => {
  const folders = ["/w/a", "/w/b"];
  assert.equal(pickFolder("/w/b/src/x.ts", "/w/a", folders), "/w/b/src");
  assert.equal(pickFolder(undefined, "/w/b", folders), "/w/b");
  assert.equal(pickFolder(undefined, "/w/gone", folders), "/w/a");
  assert.equal(pickFolder(undefined, undefined, folders), "/w/a");
  assert.equal(pickFolder(undefined, undefined, []), undefined);
});

test("inContainer: each env var, or /.dockerenv", () => {
  const no = () => false;
  for (const k of ["REMOTE_CONTAINERS", "CODESPACES", "DEVCONTAINER"]) {
    assert.equal(inContainer({ [k]: "true" }, no), true, k);
  }
  assert.equal(inContainer({}, (p) => p === "/.dockerenv"), true);
  assert.equal(inContainer({ HOME: "/h" }, no), false);
});

test("undoPath finds the undo script in text or JSON output", () => {
  assert.equal(undoPath("✓ removed\n↩ undo: sh /s/undo.sh\n"), "/s/undo.sh");
  assert.equal(undoPath('{"ok": true, "undo": "/s/u.sh"}'), "/s/u.sh");
  assert.equal(undoPath("✓ removed\n↩ undo: sh /s/my undo/u.sh\nnext\n"), "/s/my undo/u.sh");
  assert.equal(undoPath("↩ undo: sh '/s/it'\\''s/u.sh'\n"), "/s/it's/u.sh");
  assert.equal(undoPath("nothing"), undefined);
});
