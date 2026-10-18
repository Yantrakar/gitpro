// The Lost Work view (spec 03 §5): `gitpro scan --lost --json` (0.3: `lost --json --offline`) in the home dir (the TOML picks the roots), shown as
// kind → repo → item. Read-only: the extension never pushes; lostCopyFix only copies the CLI's suggested command.
// features.ts calls registerLost(context, cli, caps) (three arguments: caps decides the startup search).
// The view never goes online (always --offline, whatever [forge] online says). Copied fixes are quoted for a
// POSIX shell (sh, bash, zsh), not PowerShell or cmd.
import * as os from "node:os";
import * as vscode from "vscode";
import { Capabilities, lostBody, lostCall } from "./capabilities";
import { Cli, ui } from "./cli";
import { KindNode, LostJson, LostNode, lostSummary, lostTree } from "./lostTree";
import { tilde } from "./scan";

export type { ItemNode, KindNode, LostJson, LostNode, RepoNode } from "./lostTree";

const TIMEOUT = 600_000;

export function registerLost(context: vscode.ExtensionContext, cli: Cli, caps: Capabilities): void {
  let nodes: KindNode[] = [];
  let running: Promise<KindNode[] | undefined> | undefined;
  const changed = new vscode.EventEmitter<LostNode | undefined>();
  const provider: vscode.TreeDataProvider<LostNode> = {
    onDidChangeTreeData: changed.event,
    getChildren: (n?: LostNode) => (!n ? nodes : n.type === "kind" ? n.repos : n.type === "repo" ? n.items : []),
    getTreeItem: treeItem,
  };
  const view = vscode.window.createTreeView("gitpro.lost", { treeDataProvider: provider });
  context.subscriptions.push(view, changed);

  const state = (s: string) => vscode.commands.executeCommand("setContext", "gitpro.lostState", s);

  async function search(quiet: boolean): Promise<KindNode[] | undefined> {
    await state("scanning");
    const home = os.homedir();
    const raw = await vscode.window.withProgress({ location: { viewId: "gitpro.lost" } }, () =>
      cli.json<unknown>(lostCall(cli.caps), { cwd: home, timeout: TIMEOUT, accept: [0, 1], quiet }));
    const json = raw === undefined ? undefined : lostBody<LostJson>(cli.caps, raw);
    if (!json) {
      nodes = []; // drop the last result so the "search failed" welcome text shows
      view.description = undefined;
      changed.fire(undefined);
      await state("failed");
      return undefined;
    }
    for (const w of json.warnings ?? []) {
      cli.log.warn(`  ⚠ ${w}`);
    }
    nodes = lostTree(json, home);
    view.description = lostSummary(nodes) || undefined;
    changed.fire(undefined);
    await state("done");
    return nodes;
  }

  /** One search at a time: a refresh while one runs gets the same result, except that a user refresh during
   * the quiet startup search runs again (not quiet, so failures show) once that one ends. */
  let runningQuiet = false;
  let queued: Promise<KindNode[] | undefined> | undefined;
  function refresh(quiet = false): Promise<KindNode[] | undefined> {
    if (running && runningQuiet && !quiet) {
      queued ??= running.then(() => {
        queued = undefined;
        return refresh(false);
      });
      return queued;
    }
    if (!running) {
      runningQuiet = quiet;
      running = search(quiet).finally(() => (running = undefined));
    }
    return running;
  }

  context.subscriptions.push(
    // Returns the tree (the integration test reads it); undefined when the CLI lacks `lost` or the search failed.
    vscode.commands.registerCommand("gitpro.lostRefresh", () => (cli.caps.has("scan.lost") || cli.require("lost") ? refresh() : undefined)),
    vscode.commands.registerCommand("gitpro.lostOpenRepo", (n?: LostNode) => openRepo(n)),
    vscode.commands.registerCommand("gitpro.lostCopyFix", (n?: LostNode) => copyFix(n)),
  );

  if (vscode.workspace.getConfiguration("gitpro").get<boolean>("lost.showOnStartup") && (caps.has("scan.lost") || caps.has("lost"))) {
    void refresh(true).then((found) => {
      if (found?.length) {
        void vscode.commands.executeCommand("gitpro.lost.focus");
      }
    });
  }
}

function treeItem(n: LostNode): vscode.TreeItem {
  if (n.type === "kind") {
    const t = new vscode.TreeItem(n.label, vscode.TreeItemCollapsibleState.Expanded);
    t.description = n.description;
    t.contextValue = "lostKind";
    return t;
  }
  if (n.type === "repo") {
    const t = new vscode.TreeItem(n.label, vscode.TreeItemCollapsibleState.Expanded);
    t.iconPath = new vscode.ThemeIcon("repo");
    t.tooltip = n.repo;
    t.contextValue = "lostRepo";
    return t;
  }
  const t = new vscode.TreeItem(n.label, vscode.TreeItemCollapsibleState.None);
  t.tooltip = n.tooltip;
  if (n.fix) {
    t.contextValue = "lostItem";
    t.iconPath = new vscode.ThemeIcon("lightbulb");
  }
  return t;
}

/** Reveal the repo when it is in a workspace folder, else open it in a new window. */
async function openRepo(n?: LostNode): Promise<string | undefined> {
  if (!n || n.type === "kind") {
    return undefined;
  }
  const uri = vscode.Uri.file(n.repo);
  if (vscode.workspace.getWorkspaceFolder(uri)) {
    await vscode.commands.executeCommand("revealInExplorer", uri);
    return "revealed";
  }
  await vscode.commands.executeCommand("vscode.openFolder", uri, { forceNewWindow: true });
  return "opened";
}

/** Copies the item's suggested command to the clipboard (never runs it). */
async function copyFix(n?: LostNode): Promise<string | undefined> {
  if (!n || n.type !== "item" || !n.fix) {
    return undefined;
  }
  await vscode.env.clipboard.writeText(n.fix);
  void ui.info(`Copied: ${n.fix.split(n.repo).join(tilde(n.repo, os.homedir()))}`);
  return n.fix;
}
