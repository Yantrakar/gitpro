// The extensionTestsPath module: activates the extension once, then runs every `<name>.integration.js` in this
// directory in name order. Each exports `run(api: Api): Promise<void>`; a thrown error fails the whole run.
import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import type { Api } from "../../extension";

export async function run(): Promise<void> {
  const ext = vscode.extensions.getExtension<Api>("yantrakar.gitpro");
  assert.ok(ext, "extension not found");
  const api = await ext.activate();
  const files = fs.readdirSync(__dirname).filter((f) => f.endsWith(".integration.js")).sort();
  assert.ok(files.length, "no *.integration.js files");
  for (const f of files) {
    console.log(`integration: ${f}`);
    const mod = require(path.join(__dirname, f)) as { run(api: Api): Promise<void> };
    await mod.run(api);
  }
  console.log("integration: ok");
}
