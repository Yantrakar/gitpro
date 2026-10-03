// Spec 15 §9 tests 7-12: the identity status bar against the real gitpro, in a two-folder workspace.
import * as assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import type { Result } from "../../cli";
import type { Api } from "../../extension";
import { env, withEnv, withUi } from "./helpers";

const WORK = "$(account) work-github · id_work";
const PERSONAL = "$(account) personal · id_personal";

/** Every command spec 15 §2.1 declares for the feature packages. */
const FEATURE_COMMANDS = ["scanRoots", "lostRefresh", "lostOpenRepo", "lostCopyFix", "clone", "copyEnv",
  "devcontainerSetup", "doctor"];

/** The stubs and their commands; a file another package has replaced (no STUB export) is skipped. */
const STUBS: [string, string[]][] = [["roots", ["scanRoots"]], ["lost", ["lostRefresh", "lostOpenRepo", "lostCopyFix"]],
  ["clone", ["clone"]], ["devcontainer", ["devcontainerSetup"]]];

async function closeEditors(): Promise<void> {
  await vscode.commands.executeCommand("workbench.action.closeAllEditors");
}

async function open(file: string): Promise<void> {
  await vscode.window.showTextDocument(vscode.Uri.file(file));
}

export async function run(api: Api): Promise<void> {
  const repo = env("GI_TEST_REPO");
  const repoB = env("GI_REPO_B");
  await closeEditors();
  assert.ok(api.caps.has("why"), `gitpro ${api.caps.version} has no 'why'`);

  // 7: identity and key; a repo-local user.email turns it red, and the tooltip names the fix.
  await api.refresh();
  let s = api.status();
  assert.equal(s.text, WORK);
  assert.equal(s.visible, true);
  assert.equal(s.background, undefined);
  assert.match(s.tooltip, /✓ identity matches/);
  execFileSync("git", ["-C", repo, "config", "user.email", "wrong@x"]);
  await api.refresh();
  s = api.status();
  assert.ok(s.text.startsWith("$(error)"), s.text);
  assert.equal(s.text, "$(error) wrong@x ≠ work-github");
  assert.equal(s.background, "statusBarItem.errorBackground");
  assert.ok(s.tooltip.includes("config --unset"), s.tooltip);

  // 9: the click offers the CLI's fix; picking it runs exactly its fix_argv (a git on PATH logs it).
  const gitLog = env("GI_GIT_LOG");
  fs.rmSync(gitLog, { force: true });
  await withEnv({ PATH: `${env("GI_GIT_BIN")}${path.delimiter}${process.env.PATH ?? ""}` }, () =>
    withUi(api, { modal: "Run", pick: (labels) => labels.find((l) => l.startsWith("Unset ")) }, async (seen) => {
      await vscode.commands.executeCommand("gitpro.which");
      assert.equal(seen.pick.length, 1);
      assert.equal(seen.pick[0][0], "Unset user.email in .git/config");
      assert.ok(seen.pick[0].includes("Explain (gitpro why)"));
      assert.equal(seen.modal.length, 1);
      assert.match(seen.modal[0], /^Run this command\?\n\ngit -C \S+ config --unset user\.email$/);
    }));
  assert.ok(fs.readFileSync(gitLog, "utf8").split("\n").includes(`-C ${repo} config --unset user.email`),
    "the fix did not run through git");
  assert.equal(spawnSync("git", ["-C", repo, "config", "--local", "--get", "user.email"]).status, 1);
  assert.equal(api.status().text, WORK);

  // 10: apply with nothing to change says so and never asks.
  await withUi(api, { modal: "Apply" }, async (seen) => {
    await vscode.commands.executeCommand("gitpro.apply");
    assert.deepEqual(seen.info, ["gitpro: up to date, nothing to change."]);
    assert.deepEqual(seen.modal, []);
  });

  // 8: a gitpro without the 'why' capability: "gitpro?", the old commands still run, everything is registered.
  const cfg = vscode.workspace.getConfiguration("gitpro");
  await cfg.update("path", env("GI_GITPRO_OLD"), vscode.ConfigurationTarget.Workspace);
  await api.reloadCapabilities();
  assert.equal(api.caps.version, "0.1.9");
  assert.equal(api.caps.has("why"), false);
  s = api.status();
  assert.equal(s.text, "$(account) gitpro?");
  assert.equal(s.background, "statusBarItem.warningBackground");
  const plan = await vscode.commands.executeCommand<Result | undefined>("gitpro.plan");
  assert.equal(plan?.code, 0);
  await withUi(api, {}, async (seen) => {
    assert.equal(await vscode.commands.executeCommand("gitpro.copyEnv"), undefined);
    assert.equal(seen.warn.length, 1);
    assert.match(seen.warn[0], /^gitpro 0\.1\.9 has no 'env'; update: pipx install --force /);
  });
  const ids = new Set(await vscode.commands.getCommands(true));
  const declared = (vscode.extensions.getExtension("sandeepshelke.gitpro")?.packageJSON.contributes.commands ?? [])
    .map((c: { command: string }) => c.command) as string[];
  for (const c of [...FEATURE_COMMANDS.map((n) => `gitpro.${n}`), ...declared]) {
    assert.ok(declared.includes(c), `${c} is not declared in package.json`);
    assert.ok(ids.has(c), `${c} is not registered`);
  }
  await cfg.update("path", env("GI_GITPRO"), vscode.ConfigurationTarget.Workspace);
  await api.reloadCapabilities();
  assert.equal(api.caps.has("why"), true);
  assert.equal(api.status().text, WORK);

  // 11: multi-root. The active editor's folder wins; without one, the last active folder; the tooltip lists both.
  await open(path.join(repoB, "README.md"));
  await api.refresh();
  assert.equal(api.status().text, PERSONAL);
  await closeEditors();
  await api.refresh();
  s = api.status();
  assert.equal(s.text, PERSONAL);
  assert.ok(s.tooltip.includes("repo-b"), s.tooltip);
  assert.ok(s.tooltip.includes("Other folders:"), s.tooltip);
  assert.ok(s.tooltip.includes("repo-a"), s.tooltip);
  assert.ok(s.tooltip.includes("work-github"), s.tooltip);
  await open(path.join(repo, "README.md"));
  await closeEditors(); // leaves repo-a the last active folder for the tests after this one
  await api.refresh();
  assert.equal(api.status().text, WORK);

  // 12: the stubs' commands answer "not built yet" and do not throw.
  for (const [file, commands] of STUBS) {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = require(`../../${file}`) as { STUB?: boolean };
    if (!mod.STUB) {
      continue;
    }
    for (const c of commands) {
      await withUi(api, {}, async (seen) => {
        const said = await vscode.commands.executeCommand<string>(`gitpro.${c}`);
        assert.match(said, /not built yet/, c);
        assert.deepEqual(seen.info, [said]);
      });
    }
  }
}
