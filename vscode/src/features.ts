// Every feature package's VS Code surface, registered once. Packages 02, 03, 07 and 13 replace their own
// file (roots.ts, lost.ts, clone.ts, devcontainer.ts); nobody else edits this file, extension.ts or package.json.
import * as os from "node:os";
import * as vscode from "vscode";
import type { Capabilities } from "./capabilities";
import { Cli, failure, ui } from "./cli";
import { registerClone } from "./clone";
import { registerDevcontainer } from "./devcontainer";
import { registerLost } from "./lost";
import { registerRoots, RootsSeam } from "./roots";
import { tilde } from "./scan";

export function registerFeatures(context: vscode.ExtensionContext, cli: Cli, caps: Capabilities,
  rescan: () => void): RootsSeam {
  const roots = registerRoots(context, cli, rescan); // 02: returned for the Repo Overrides view's scan
  registerLost(context, cli, caps);
  registerClone(context, cli);
  registerDevcontainer(context, cli);
  context.subscriptions.push(
    vscode.commands.registerCommand("gitpro.copyEnv", () => copyEnv(cli)), // 12
    vscode.commands.registerCommand("gitpro.doctor", () => runDoctor(cli)), // 16
  );
  return roots;
}

function repoDir(cli: Cli): string | undefined {
  const dir = cli.dir();
  if (!dir) {
    void ui.error("Open a file in a git repository first.");
  }
  return dir;
}

/** 12: copy `gitpro env --shell sh` for this repo (token *commands*, never values) to the clipboard. */
export async function copyEnv(cli: Cli): Promise<string | undefined> {
  const dir = cli.require("env") ? repoDir(cli) : undefined;
  if (!dir) {
    return undefined;
  }
  const r = await cli.run(["env", "--shell", "sh", dir], { cwd: dir });
  if (!r) {
    return undefined;
  }
  if (r.code !== 0) {
    void ui.error(failure(r), "Show Log").then((pick) => pick && cli.log.show());
    return undefined;
  }
  await vscode.env.clipboard.writeText(r.stdout);
  void ui.info(`Copied gitpro env for ${tilde(dir, os.homedir())}: paste it into a terminal (or eval "$(gitpro env)").`);
  return r.stdout;
}

/** 16: run `gitpro doctor` into the log and show it (exit 1 means it found something, not that it failed). */
export async function runDoctor(cli: Cli): Promise<void> {
  if (!cli.require("doctor")) {
    return;
  }
  const dir = cli.dir();
  await cli.show(dir ? ["doctor", dir] : ["doctor"], { cwd: dir ?? os.homedir(), accept: [0, 1] });
}
