// Spec 02 §5: Scan Roots… against the real gitpro. Runs before overrides.integration.ts, which expects no scan
// yet, so nothing here picks "Use for This Session" (that rescans).
import * as assert from "node:assert/strict";
import * as vscode from "vscode";
import type { Api } from "../../extension";
import { tilde } from "../../scan";
import { env, withUi } from "./helpers";

export async function run(api: Api): Promise<void> {
  const home = env("GI_HOME");
  const repoB = env("GI_REPO_B");
  assert.ok(api.caps.has("scan.roots"), `gitpro ${api.caps.version} has no 'scan.roots'`);
  assert.equal(api.scanState(), "");

  // No [scan] table: the root is ~; keep it and open the snippet to paste into the config.
  await withUi(api, { pick: (labels) => labels.includes("~") ? "~" : "Show TOML Snippet" }, async (seen) => {
    const chosen = await vscode.commands.executeCommand<string[]>("gitpro.scanRoots");
    assert.deepEqual(chosen, [home]);
    assert.equal(seen.pick.length, 2, JSON.stringify(seen.pick));
    assert.ok(seen.pick[0].includes(`Config: ${tilde(env("GI_CONFIG"), home)}`), JSON.stringify(seen.pick[0]));
    assert.ok(seen.pick[0].includes("$(go-to-file) Open Config"));
    assert.deepEqual(seen.pick[1], ["Use for This Session", "Show TOML Snippet"]);
    const text = vscode.window.activeTextEditor?.document.getText() ?? "";
    assert.ok(text.includes('[scan]\nroots = ["~"]'), text);
    assert.deepEqual([...seen.warn, ...seen.error], []);
  });
  await vscode.commands.executeCommand("workbench.action.revertAndCloseActiveEditor");

  // From the explorer: the folder joins the list, checked; cancelling the second question changes nothing.
  await withUi(api, { pick: (labels) => labels.includes(repoB) ? repoB : undefined }, async (seen) => {
    const chosen = await vscode.commands.executeCommand<string[] | undefined>("gitpro.scanRoots", vscode.Uri.file(repoB));
    assert.equal(chosen, undefined);
    assert.ok(seen.pick[0].includes("~") && seen.pick[0].includes(repoB), JSON.stringify(seen.pick[0]));
    assert.equal(seen.pick.length, 2);
  });
  assert.equal(api.scanState(), "", "Scan Roots must not scan unless asked to");
}
