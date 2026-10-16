// Every feature package's VS Code surface, registered once. Packages 02, 03, 07 and 13 replace their own
// file (roots.ts, lost.ts, clone.ts, devcontainer.ts); nobody else edits this file, extension.ts or package.json.
import * as os from "node:os";
import * as vscode from "vscode";
import type { Capabilities } from "./capabilities";
import { Cli, failure, ui } from "./cli";
import { registerClone } from "./clone";
import { registerDevcontainer } from "./devcontainer";
import { registerLost } from "./lost";
import { reportIssue } from "./report";
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
    vscode.commands.registerCommand("gitpro.reportIssue", (opts?: { crash?: boolean }) =>
      runReport(context, cli, opts?.crash === true)), // 19
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

/** 19: draft a redacted issue with `gitpro report --json --print`, show it, and offer the prefilled form. */
export async function runReport(context: vscode.ExtensionContext, cli: Cli, crash: boolean): Promise<void> {
  const dir = cli.dir() ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  await reportIssue({
    version: String((context.extension.packageJSON as { version?: string }).version ?? "0"),
    dir: () => dir,
    // exit 2 is a blocked draft: its JSON says which kinds and sections
    run: (args, cwd) => cli.run(args, { cwd, accept: [0, 2], quiet: true }),
    preview: async (body) => {
      const doc = await vscode.workspace.openTextDocument({ language: "markdown", content: body });
      await vscode.window.showTextDocument(doc, { preview: true });
    },
    modal: (msg, ...items) => Promise.resolve(ui.modal(msg, ...items)),
    error: (msg) => void ui.error(msg),
    info: (msg) => void ui.info(msg),
    openExternal: (url) => Promise.resolve(vscode.env.openExternal(vscode.Uri.parse(url, true))),
    copy: (text) => Promise.resolve(vscode.env.clipboard.writeText(text)),
  }, { crash });
}
