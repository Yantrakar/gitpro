// The Repo Overrides view (scan, Fix Checked) and the no-identity status.
// Runs last (name order): it leaves the test repo without any identity.
import * as assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import type { Api } from "../../extension";
import { env, withEnv, withUi } from "./helpers";

export async function run(api: Api): Promise<void> {
  const ids = await vscode.commands.getCommands(true);
  for (const c of ["which", "pin", "unpin", "setup", "openConfig", "refresh",
    "scan", "scanFix", "openRepoConfig"]) {
    assert.ok(ids.includes(`gitpro.${c}`), `command gitpro.${c} not registered`);
  }
  await setupLauncher(api);
  // The global identity the scanned repo's own user.email hides.
  await withEnv({ GIT_CONFIG_GLOBAL: env("GI_GLOBAL") }, () => overrides(api));
  await noIdentity(api);
  await withEnv({ GIT_CONFIG_GLOBAL: env("GI_GLOBAL") }, () => sessionRoots(api));
}

async function setupLauncher(api: Api): Promise<void> {
  const file = path.join(env("GI_HOME"), "setup custom [draft].toml");
  const cfg = vscode.workspace.getConfiguration("gitpro");
  const info = api.ui.info;

  async function launch(command: string): Promise<void> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let subscription: vscode.Disposable | undefined;
    let terminal: vscode.Terminal | undefined;
    const opened = new Promise<vscode.Terminal>((resolve, reject) => {
      subscription = vscode.window.onDidOpenTerminal((t) => {
        if (t.name === "gitpro setup") {
          resolve(t);
        }
      });
      timer = setTimeout(() => reject(new Error("setup terminal did not open")), 10000);
    });
    try {
      await vscode.commands.executeCommand(command);
      terminal = await opened;
      const options = terminal.creationOptions;
      assert.ok("shellPath" in options);
      assert.equal(options.shellPath, env("GI_GITPRO"));
      assert.deepEqual(options.shellArgs, ["setup", "-c", file]);
      assert.equal(typeof await terminal.processId, "number");
      assert.equal(fs.existsSync(file), false, "launching setup must not write a config");
    } finally {
      clearTimeout(timer);
      subscription?.dispose();
      terminal?.dispose();
    }
  }

  try {
    await withEnv({ GITPRO_CONFIG: file }, async () => {
      await api.reloadCapabilities();
      assert.ok(api.caps.has("setup"));
      await launch("gitpro.setup");
      api.ui.info = async (_msg, ...choices) => {
        assert.deepEqual(choices, ["Set Up", "Create with Discovery", "Create from Example"]);
        return "Set Up";
      };
      await launch("gitpro.openConfig");
      api.ui.info = info;

      await cfg.update("path", env("GI_GITPRO_OLD"), vscode.ConfigurationTarget.Workspace);
      await api.reloadCapabilities();
      await withUi(api, {}, async (seen) => {
        const count = vscode.window.terminals.length;
        await vscode.commands.executeCommand("gitpro.setup");
        assert.equal(vscode.window.terminals.length, count);
        assert.match(seen.warn.join("\n"), /setup.*update/s);
      });
      api.ui.info = async (_msg, ...choices) => {
        assert.deepEqual(choices, ["Create from Example"]);
        return undefined;
      };
      await vscode.commands.executeCommand("gitpro.openConfig");
    });
  } finally {
    api.ui.info = info;
    await cfg.update("path", env("GI_GITPRO"), vscode.ConfigurationTarget.Workspace);
    await api.reloadCapabilities();
  }
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

  const scanRepo = env("GI_SCAN_REPO");
  assert.equal(spawnSync("git", ["-C", scanRepo, "config", "--local", "--get", "user.email"]).status, 0);

  let asked = "";
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
      assert.match(s.tooltip, /gitpro setup /);
    });
  });
}
