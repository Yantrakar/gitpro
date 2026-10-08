// The Repo Overrides view (scan, Suggest, Review One by One, Fix Checked) and the no-identity status.
// Runs last (name order): it leaves the test repo without any identity.
import * as assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import type { Api } from "../../extension";
import type { Choice } from "../../scan";
import { env, withEnv, withUi } from "./helpers";

export async function run(api: Api): Promise<void> {
  const ids = await vscode.commands.getCommands(true);
  for (const c of ["which", "plan", "apply", "pin", "unpin", "openConfig", "refresh",
    "scan", "scanFix", "scanReview", "scanSuggest", "openRepoConfig"]) {
    assert.ok(ids.includes(`gitpro.${c}`), `command gitpro.${c} not registered`);
  }
  // The global identity the scanned repo's own user.email hides.
  await withEnv({ GIT_CONFIG_GLOBAL: env("GI_GLOBAL") }, () => overrides(api));
  await noIdentity(api);
  await withEnv({ GIT_CONFIG_GLOBAL: env("GI_GLOBAL") }, () => sessionRoots(api));
}

/** 02: Scan Roots… → Use for This Session rescans the view with those roots in place of gitpro.scanDirs. */
async function sessionRoots(api: Api): Promise<void> {
  const scanDir = env("GI_SCAN_DIR");
  const other = path.join(path.dirname(scanDir), "session");
  const repo = path.join(other, "app2");
  execFileSync("git", ["init", "-q", repo]);
  execFileSync("git", ["-C", repo, "remote", "add", "origin", "git@github.com:x/z.git"]);
  execFileSync("git", ["-C", repo, "config", "user.email", "old2@x.test"]);
  await withUi(api, { pick: (labels) => labels.includes(other) ? other : "Use for This Session" }, async (seen) => {
    // From the explorer on `other`: it joins gitpro.scanDirs' root; the fake picker checks only `other`.
    const chosen = await vscode.commands.executeCommand<string[]>("gitpro.scanRoots", vscode.Uri.file(other));
    assert.deepEqual(chosen, [other]);
    assert.ok(seen.pick[0].includes(scanDir), JSON.stringify(seen.pick[0]));
    for (let i = 0; i < 300 && !(api.scanState() === "done" && api.overrides().length); i++) {
      await new Promise((r) => setTimeout(r, 100));
    }
    const model = api.overrides().map((g) => [g.label, g.children.map((b) => [b.label, b.repos.map((r) => r.label)])]);
    assert.deepEqual(model, [["Global Me <me@global.test>", [["from Global Me <old2@x.test>", ["app2"]]]]]);
    const msg = api.scanMessage() ?? "";
    assert.ok(msg.includes(`under ${other}`) && !msg.includes(scanDir), msg);
  });
}

async function overrides(api: Api): Promise<void> {
  // Repo Overrides: scan finds the repo's own user.email, the fix removes it.
  const cfg = vscode.workspace.getConfiguration("gitpro");
  await cfg.update("scanDirs", [env("GI_SCAN_DIR")], vscode.ConfigurationTarget.Workspace);
  assert.equal(api.scanState(), "");
  // A missing gitpro leaves the view in its failed state, not an empty "no overrides".
  await cfg.update("path", "/nonexistent/gitpro", vscode.ConfigurationTarget.Workspace);
  await vscode.commands.executeCommand("gitpro.scan");
  assert.equal(api.scanState(), "failed");
  assert.deepEqual(api.overrides(), []);
  await malformedReports(api);
  await cfg.update("path", env("GI_GITPRO"), vscode.ConfigurationTarget.Workspace);
  await api.reloadCapabilities();
  await vscode.commands.executeCommand("gitpro.scan");
  assert.equal(api.scanState(), "done");
  const model = api.overrides().map((g) => [g.label, g.description,
    g.children.map((b) => [b.label, b.repos.map((r) => [r.label, r.description, r.fixable])])]);
  assert.deepEqual(model, [["Global Me <me@global.test>", "1 repo",
    [["from Global Me <old@x.test>", [["app", "github.com/x", true]]]]]]);

  await vscode.commands.executeCommand("gitpro.scanSuggest");
  const doc = vscode.window.activeTextEditor?.document.getText() ?? "";
  assert.match(doc, /^# paste the blocks you want into/);
  assert.match(doc, /email = "old@x\.test"/);
  await vscode.commands.executeCommand("workbench.action.revertAndCloseActiveEditor");

  // Review One by One: Escape changes nothing, Skip leaves nothing to fix, Fix ends in the usual question.
  const scanRepo = env("GI_SCAN_REPO");
  let asked = "";
  api.setConfirm(async (q) => {
    asked = q;
    return false;
  });
  const shown: string[] = [];
  const answers: (Choice | undefined)[] = [undefined, "skip", "fix"];
  api.setReview(async (rs, i, fix) => {
    shown.push(`${rs[i].label} ${i + 1}/${rs.length} ${fix ? "fix" : "skip"}`);
    return answers.shift();
  });
  for (let n = 0; n < 3; n++) {
    await vscode.commands.executeCommand("gitpro.scanReview");
  }
  assert.deepEqual(shown, ["app 1/1 fix", "app 1/1 fix", "app 1/1 skip"]);
  assert.equal(asked, "Remove 1 setting from 1 repo? They'll use Global Me <me@global.test>.");
  assert.equal(spawnSync("git", ["-C", scanRepo, "config", "--local", "--get", "user.email"]).status, 0);

  asked = "";
  api.setConfirm(async (q) => {
    asked = q;
    return true;
  });
  await vscode.commands.executeCommand("gitpro.scanFix");
  assert.equal(asked, "Remove 1 setting from 1 repo? They'll use Global Me <me@global.test>.");
  const local = spawnSync("git", ["-C", scanRepo, "config", "--local", "--get", "user.email"]);
  assert.equal(local.status, 1, "the repo's own user.email is still there");
  assert.deepEqual(api.overrides(), []);
}

async function malformedReports(api: Api): Promise<void> {
  const cfg = vscode.workspace.getConfiguration("gitpro");
  const wrapper = path.join(env("GI_HOME"), "gitpro-malformed");
  const payload = path.join(env("GI_HOME"), "scan-response.json");
  const sq = (s: string) => `'${s.replace(/'/g, "'\\''")}'`;
  fs.writeFileSync(wrapper, `#!/bin/sh\nif [ "$1" = scan ]; then\ncat ${sq(payload)}\nexit 0\nfi\n`
    + `exec ${sq(env("GI_GITPRO"))} "$@"\n`, { mode: 0o700 });
  try {
    await cfg.update("path", wrapper, vscode.ConfigurationTarget.Workspace);
    await api.reloadCapabilities();
    for (const body of ["{", "null", "{}", JSON.stringify({
      roots: [], repos: 1, unreadable: [], overrides: [{}], suggestions: [], unmatched: [],
    })]) {
      fs.writeFileSync(payload, body);
      await withUi(api, {}, async (seen) => {
        await vscode.commands.executeCommand("gitpro.scan");
        assert.equal(api.scanState(), "failed", body);
        assert.deepEqual(api.overrides(), [], body);
        assert.equal(api.scanMessage(), undefined);
        assert.equal(seen.error.length, 1);
        assert.match(seen.error[0], body === "{" ? /no JSON/ : /invalid scan report/);
      });
    }
  } finally {
    await cfg.update("path", env("GI_GITPRO"), vscode.ConfigurationTarget.Workspace);
    await api.reloadCapabilities();
    fs.rmSync(wrapper, { force: true });
    fs.rmSync(payload, { force: true });
  }
}

async function noIdentity(api: Api): Promise<void> {
  // No email anywhere: git would refuse to commit. gitpro knows what it should be, so the item is red...
  const repo = env("GI_TEST_REPO");
  spawnSync("git", ["-C", repo, "config", "--unset", "user.email"]);
  execFileSync("git", ["-C", repo, "config", "user.useConfigOnly", "true"]);
  await withEnv({ GIT_CONFIG_GLOBAL: "/dev/null" }, async () => {
    await api.refresh();
    assert.ok(api.status().text.startsWith("$(error) no identity ≠ work-github"), api.status().text);
    assert.equal(api.status().background, "statusBarItem.errorBackground");
    // ...and without a gitpro config it falls back to `which`: a warning, with the way to add one.
    await withEnv({ GITPRO_CONFIG: "/nonexistent/gitpro.toml" }, async () => {
      await api.refresh();
      const s = api.status();
      assert.equal(s.text, "$(warning) no git identity");
      assert.equal(s.background, "statusBarItem.warningBackground");
      assert.match(s.tooltip, /gitpro scan --suggest --repo /);
    });
  });
}
