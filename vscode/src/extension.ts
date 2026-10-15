import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";
import { Capabilities, LiveCapabilities, loadCapabilities, unpinCall } from "./capabilities";
import { Cli, exec, killAll, makeCli, ui } from "./cli";
import { registerFeatures } from "./features";
import type { RootsSeam } from "./roots";
import { Before, fixArgs, Fixed, fixedMessage, Group, isReport, plan, question, Repo, Report, scanArgs, summary, tilde,
  tree } from "./scan";
import { containerHere, registerIdentityBar, trackFolder } from "./statusBar";

type Node = Group | Before | Repo;

/** Returned for tests: the status bar item's current state, the Repo Overrides view, replaceable dialogs. */
export interface Api {
  status(): { text: string; visible: boolean; tooltip: string; background: string | undefined };
  refresh(): Promise<void>;
  overrides(): Group[];
  /** "" before the first scan, then "scanning", "failed" or "done": picks the view's welcome text. */
  scanState(): string;
  /** 02: the line above the Repo Overrides tree (what was scanned), undefined when none. */
  scanMessage(): string | undefined;
  /** Replaces the modal question before a fix. */
  setConfirm(ask: (question: string) => Promise<boolean>): void;
  /** The message boxes and pickers every command uses; tests replace its functions. */
  ui: typeof ui;
  /** What the configured gitpro can do (reloaded when gitpro.path changes). */
  caps: Capabilities;
  /** Re-reads `gitpro --version --json` and refreshes the status bar. */
  reloadCapabilities(): Promise<void>;
}

export async function activate(context: vscode.ExtensionContext): Promise<Api> {
  // A log channel: timestamped, kept across commands; Developer: Set Log Level... > gitpro shows more (trace: raw JSON).
  const out = vscode.window.createOutputChannel("gitpro", { log: true });
  context.subscriptions.push(out);
  const caps = new LiveCapabilities();
  const cli = makeCli(context, out, caps, trackFolder(context));

  async function loadCaps(): Promise<void> {
    caps.set(await loadCapabilities(cli));
    if (caps.version === "?") {
      out.warn(`✗ ${cli.executable()} --version --json failed: no gitpro features until it is installed`);
    } else {
      out.info(`gitpro ${caps.version} at ${cli.executable()}${caps.has("why") ? "" : " (no 'why': update it)"}`);
    }
  }
  await loadCaps();

  registerCore(context, cli);
  const overrides = repoOverrides(context, out, cli, () => roots); // 02: roots is set below, before any scan
  const bar = registerIdentityBar(context, cli, caps);
  const roots = registerFeatures(context, cli, caps, () => void overrides.rescan());
  context.subscriptions.push(vscode.commands.registerCommand("gitpro.refresh", () => bar.refresh()));

  async function reloadCapabilities(): Promise<void> {
    await loadCaps();
    await bar.refresh();
  }
  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((e) => {
    if (e.affectsConfiguration("gitpro.path")) {
      void reloadCapabilities();
    }
  }));
  void bar.refresh();
  return { status: bar.status, refresh: bar.refresh, overrides: overrides.overrides, scanState: overrides.scanState,
    scanMessage: overrides.scanMessage, setConfirm: overrides.setConfirm, ui, caps, reloadCapabilities };
}

function configFile(): string {
  return process.env.GITPRO_CONFIG || path.join(os.homedir(), ".config", "gitpro.toml");
}

/** setup, pin, unpin, openConfig: the commands that predate the feature packages. */
function registerCore(context: vscode.ExtensionContext, cli: Cli): void {
  const refresh = () => vscode.commands.executeCommand("gitpro.refresh");

  function setup(): void {
    if (!cli.require("setup")) {
      return;
    }
    // `gitpro setup` asks questions (scan, review, confirm), so it runs in a terminal, on the config
    // this window uses.
    const file = configFile();
    cli.log.info(`$ ${cli.executable()} setup -c ${file}    (in a terminal)`);
    vscode.window.createTerminal({ name: "gitpro setup", shellPath: cli.executable(), shellArgs: ["setup", "-c", file],
      cwd: cli.dir() || os.homedir() }).show();
  }

  function repoDir(): string | undefined {
    const dir = cli.dir();
    if (!dir) {
      void ui.error("Open a file in a git repository first.");
    }
    return dir;
  }

  const commands: Record<string, () => Promise<unknown>> = {
    "gitpro.setup": async () => setup(),
    "gitpro.pin": async () => {
      if (containerHere()) {
        void ui.warn("Pin on the host, where your identities are set up: run 'gitpro pin' in this repo there.");
        return;
      }
      const dir = repoDir();
      if (dir) {
        await cli.show(["pin", ...(cli.caps.has("dry-run-defaults.v1") ? ["-y"] : []), dir], { cwd: dir });
        await refresh();
      }
    },
    "gitpro.unpin": async () => {
      const dir = repoDir();
      if (dir) {
        await cli.show(unpinCall(cli.caps, dir), { cwd: dir });
        await refresh();
      }
    },
    "gitpro.openConfig": async () => {
      const file = configFile();
      if (!fs.existsSync(file)) {
        const wizard = cli.caps.has("setup") ? ["Set Up"] : [];
        const discover = cli.caps.has("init.discover") ? ["Create with Discovery"] : [];
        const pick = await ui.info(`${file} does not exist.`, ...wizard, ...discover, "Create from Example");
        if (pick === "Set Up") {
          setup();
          return;
        }
        if (pick === "Create with Discovery") {
          // It may ask questions, so it runs in a terminal; closing the terminal refreshes the status bar.
          cli.log.info(`$ ${cli.executable()} init --discover ${file}    (in a terminal)`);
          vscode.window.createTerminal({ name: "gitpro init --discover", shellPath: cli.executable(),
            shellArgs: ["init", "--discover", file] }).show();
          return;
        }
        if (pick !== "Create from Example") {
          return;
        }
        await cli.show(["init", file]);
        if (!fs.existsSync(file)) {
          return;
        }
      }
      await vscode.window.showTextDocument(vscode.Uri.file(file));
    },
  };
  for (const [id, fn] of Object.entries(commands)) {
    context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  }
}

/** The Repo Overrides view: `gitpro scan` results, with checkboxes for what Fix Checked removes. */
function repoOverrides(context: vscode.ExtensionContext, out: vscode.LogOutputChannel, cli: Cli, roots: () => RootsSeam):
  Pick<Api, "overrides" | "scanState" | "scanMessage" | "setConfirm"> &
  { scan(): Promise<void>; rescan(): Promise<void> } {
  const home = os.homedir();
  let report: Report | undefined;
  let groups: Group[] = [];
  const parents = new Map<Node, Node>(); // for reveal()
  function setGroups(g: Group[]): void {
    groups = g;
    parents.clear();
    for (const group of g) {
      for (const b of group.children) {
        parents.set(b, group);
        b.repos.forEach((r) => parents.set(r, b));
      }
    }
  }
  const unchecked = new Set<string>(); // repo paths
  const changed = new vscode.EventEmitter<void>();
  let ask = async (q: string) => (await ui.modal(q, "Remove")) === "Remove";

  const provider: vscode.TreeDataProvider<Node> = {
    onDidChangeTreeData: changed.event,
    getParent: (n: Node) => parents.get(n),
    getChildren: (n?: Node) => (!n ? groups : "children" in n ? n.children : "repos" in n ? n.repos : []),
    getTreeItem(n: Node): vscode.TreeItem {
      if (!("path" in n)) {
        const t = new vscode.TreeItem(n.label, vscode.TreeItemCollapsibleState.Expanded);
        if ("children" in n) {
          t.description = n.description;
          t.iconPath = new vscode.ThemeIcon("account");
        }
        return t;
      }
      const t = new vscode.TreeItem(n.label);
      t.description = n.description;
      t.tooltip = n.tooltip;
      t.iconPath = new vscode.ThemeIcon("repo");
      t.command = { command: "gitpro.openRepoConfig", title: "Open .git/config", arguments: [n.path] };
      if (n.fixable) {
        t.checkboxState = unchecked.has(n.path)
          ? vscode.TreeItemCheckboxState.Unchecked : vscode.TreeItemCheckboxState.Checked;
      }
      return t;
    },
  };
  const view = vscode.window.createTreeView("gitpro.overrides", { treeDataProvider: provider,
    manageCheckboxStateManually: true });

  // Picks the welcome text while the tree is empty: scanning, failed, or done (no overrides).
  let scanState = "";
  const state = (s: "scanning" | "failed" | "done") =>
    vscode.commands.executeCommand("setContext", "gitpro.scanState", (scanState = s));
  let scanning: Promise<void> | undefined;
  function scan(): Promise<void> {
    scanning ??= Promise.resolve(vscode.window.withProgress({ location: { viewId: "gitpro.overrides" } }, async () => {
      // 02: this session's roots, when chosen, replace gitpro.scanDirs
      const dirs = roots().scanDirs(vscode.workspace.getConfiguration("gitpro").get<string[]>("scanDirs") ?? []);
      report = undefined;
      setGroups([]);
      view.message = undefined;
      changed.fire();
      await state("scanning");
      const r = await cli.json<unknown>(scanArgs(dirs), { timeout: 600_000 }); // a whole home directory takes seconds
      if (r === undefined) {
        out.error("✗ scan failed");
        await state("failed");
        return;
      }
      if (!isReport(r)) {
        throw new Error("gitpro printed an invalid scan report; is it up to date?");
      }
      report = r;
      roots().scanned(r); // 02: logs the roots, offers workspace folders outside them
      setGroups(tree(r.overrides));
      view.message = r.overrides.length ? summary(r, home) : undefined;
      out.info(`✓ ${summary(r, home)} · ${r.overrides.length} set their own identity`);
      r.unreadable.forEach((u) => out.debug(`  ⚠ skipped ${tilde(u.path, home)}: ${u.error}`));
      for (const o of r.overrides) {
        for (const s of o.settings) {
          out.info(`  ${tilde(o.path, home)}  (${o.owner})  ${s.key} ${s.value} → ${s.without ?? "kept: nothing else sets it"}`);
        }
      }
      await state("done");
      changed.fire();
    })).catch(async (error: unknown) => {
      report = undefined;
      setGroups([]);
      view.message = undefined;
      const message = `Scan failed: ${error instanceof Error ? error.message : String(error)}`;
      out.error(`✗ ${message}`);
      await state("failed");
      changed.fire();
      void ui.error(message, "Show Log").then((pick) => pick && out.show());
    }).finally(() => (scanning = undefined));
    return scanning;
  }
  // 02: new scan roots chosen during a scan apply to one more scan after it, not to the one running.
  let queued: Promise<void> | undefined;
  function rescan(): Promise<void> {
    if (!scanning) {
      return scan();
    }
    const next = () => ((queued = undefined), scan());
    return (queued ??= scanning.then(next, next));
  }

  async function fix(): Promise<void> {
    const p = plan(groups, unchecked);
    if (!p.paths.length) {
      void vscode.window.showInformationMessage("Nothing checked to fix.");
      return;
    }
    out.info(`fix: ${question(p)}`);
    if (!(await ask(question(p)))) {
      out.info("✗ cancelled, nothing changed");
      return;
    }
    const f = await cli.json<Fixed>(fixArgs(p.paths));
    if (!f) {
      return;
    }
    f.removed.forEach((x) => out.info(`  ✓ removed ${x.key} ${x.value} from ${tilde(x.path, home)}`));
    const undo = f.undo;
    if (undo) {
      out.info(`  ↩ undo: sh ${undo}`);
    }
    const msg = undo ? `${fixedMessage(f)}. Undo script: ${tilde(undo, home)}` : fixedMessage(f);
    void vscode.window.showInformationMessage(msg, ...(undo ? ["Undo"] : []), "Show Log").then(async (pick) => {
      if (pick === "Show Log") {
        out.show();
      } else if (pick === "Undo" && undo) {
        out.info(`↩ $ sh ${undo}`);
        const r = await exec("sh", [undo], { cwd: home });
        if (r.code === 0) {
          out.info("  ✓ undone");
        } else {
          (r.stdout + r.stderr).trimEnd().split("\n").forEach((l) => out.error(`  ${l}`));
          void vscode.window.showErrorMessage(`Undo failed: ${r.stderr.trim().split("\n").pop() || r.code}`, "Show Log")
            .then((p) => p && out.show());
        }
        await scan();
      }
    });
    await scan();
  }

  async function openRepoConfig(repo: string, preserveFocus = false): Promise<void> {
    // .git is a file in worktrees and submodules; git knows where their config lives.
    const r = await cli.git(["rev-parse", "--git-path", "config"], repo);
    const file = r.code === 0 ? path.resolve(repo, r.stdout.trim()) : path.join(repo, ".git", "config");
    await vscode.window.showTextDocument(vscode.Uri.file(file), { preserveFocus, preview: true });
  }

  context.subscriptions.push(changed, view,
    view.onDidChangeCheckboxState((e) => {
      for (const [n, state] of e.items) {
        if ("path" in n && state === vscode.TreeItemCheckboxState.Checked) {
          unchecked.delete(n.path);
          out.debug(`☑ ${n.path}`);
        } else if ("path" in n) {
          unchecked.add(n.path);
          out.debug(`☐ ${n.path}`);
        }
      }
    }),
    vscode.commands.registerCommand("gitpro.scan", scan),
    vscode.commands.registerCommand("gitpro.scanFix", fix),
    vscode.commands.registerCommand("gitpro.openRepoConfig", openRepoConfig),
  );
  return { scan, rescan, overrides: () => groups, scanState: () => scanState, scanMessage: () => view.message,
    setConfirm: (fn) => (ask = fn) };
}

/** Ends the gitpro processes still running (their whole process groups), e.g. a clone, when the window closes. */
export function deactivate(): Promise<void> {
  return killAll();
}
