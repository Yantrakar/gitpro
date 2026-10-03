// Scan roots (spec 02 §5): the Scan Roots… picker, session roots for the Repo Overrides view, and a hint when
// workspace folders lie outside the roots. The extension never writes gitpro.scanDirs or the TOML: it keeps
// roots for this session, or opens a TOML snippet for the user to paste. Pure logic is in rootsModel.ts.
import * as fs from "node:fs";
import * as os from "node:os";
import * as vscode from "vscode";
import { Cli, ui } from "./cli";
import { effectiveRoots, foldersOutside, outsideMessage, ROOTS_CAP, rootsLog, tomlSnippet, viewScan } from "./rootsModel";
import { Report, tilde } from "./scan";

export { effectiveRoots, foldersOutside } from "./rootsModel";

type Action = "open" | "addFolder" | "addWorkspace" | "session" | "toml";

interface Item extends vscode.QuickPickItem {
  root?: string;
  action?: Action;
}

/** How the Repo Overrides view's scan (extension.ts) uses the roots: it asks scanDirs() for the directories to
 * pass, and hands each finished report to scanned(). */
export interface RootsSeam {
  /** gitpro.scanDirs in, the directories to scan out (this session's roots, when chosen). */
  scanDirs(configured: string[]): string[];
  /** Logs the roots and offers to add workspace folders outside them. */
  scanned(r: Report): void;
}

/** The path with symlinks resolved, as gitpro reports roots; the path itself when it cannot be resolved. */
function real(p: string): string {
  try {
    return fs.realpathSync.native(p);
  } catch {
    return p;
  }
}

/** Registers gitpro.scanRoots. rescan() re-runs the Repo Overrides scan after the roots change. */
export function registerRoots(context: vscode.ExtensionContext, cli: Cli, rescan: () => void): RootsSeam {
  const home = real(os.homedir()); // report roots are resolved: compare and abbreviate against the real home
  let session: string[] | undefined; // roots chosen with "Use for This Session"; gone on reload
  const hinted = new Set<string>(); // outside-folder sets already mentioned this session

  const settingDirs = () => vscode.workspace.getConfiguration("gitpro").get<string[]>("scanDirs") ?? [];
  // Resolved like the roots, so a folder opened through a symlink is not "outside" them.
  const folders = () => (vscode.workspace.workspaceFolders ?? []).filter((f) => f.uri.scheme === "file")
    .map((f) => real(f.uri.fsPath));
  let viewSource: string | undefined; // how the last view scan's directories were chosen

  function scanDirs(configured: string[]): string[] {
    const v = viewScan(configured, session);
    viewSource = v.source;
    return v.dirs;
  }

  function scanned(rep: Report): void {
    const roots = rep.roots ?? [];
    const cliSource = (rep as Report & { roots_source?: string }).roots_source;
    cli.log.info(rootsLog(roots, viewSource ?? cliSource ?? "default", home));
    const outside = foldersOutside(roots, folders());
    const key = outside.join("\n");
    if (!outside.length || hinted.has(key)) {
      return;
    }
    hinted.add(key);
    cli.log.info(`${outsideMessage(outside.length)}: ${outside.map((f) => tilde(f, home)).join(", ")}`);
    void ui.info(`gitpro: ${outsideMessage(outside.length)}.`, "Add Them for This Session").then((pick) => {
      if (pick) {
        useForSession([...roots, ...outside]);
      }
    });
  }

  function useForSession(roots: string[]): void {
    session = roots;
    cli.log.info(rootsLog(roots, "session", home));
    rescan();
  }

  async function chooseRoots(uri?: vscode.Uri): Promise<string[] | undefined> {
    if (!cli.require(ROOTS_CAP)) {
      return undefined;
    }
    const eff = await effectiveRoots(cli, session ?? settingDirs(), session ? "session" : "gitpro.scanDirs", home);
    if (!eff) {
      return undefined;
    }
    const roots = [...eff.roots];
    let checked = new Set(roots);
    const add = (ps: string[]) => ps.forEach((p) => {
      if (!roots.includes(p)) {
        roots.push(p);
      }
      checked.add(p);
    });
    const here = uri instanceof vscode.Uri && uri.scheme === "file" ? real(uri.fsPath) : undefined;
    if (here) {
      add([here]);
    }
    const sep = (label: string): Item => ({ label, kind: vscode.QuickPickItemKind.Separator });
    for (;;) {
      const outside = foldersOutside([...checked], folders());
      const items: Item[] = [
        sep(eff.config ? `Config: ${tilde(eff.config, home)}` : "No config file: gitpro scans ~"),
        { label: "$(go-to-file) Open Config", action: "open" },
        sep(`Roots (${eff.source})`),
        ...roots.map((r): Item => ({ label: tilde(r, home), root: r, picked: checked.has(r),
          description: eff.missing.includes(r) ? "missing" : r === here ? "this folder" : undefined })),
        sep("Add"),
        { label: "$(folder-opened) Add Folder…", action: "addFolder" },
        ...(outside.length ? [{ label: "$(root-folder) Add Workspace Folders", action: "addWorkspace" as const,
          description: outside.map((f) => tilde(f, home)).join(", ") }] : []),
      ];
      // canPickMany resolves to an array; the tests' ui.pick answers one item.
      const got = (await ui.pick(items, { canPickMany: true, title: "gitpro: Scan Roots",
        placeHolder: "Check the roots to scan, then OK" })) as unknown as Item[] | Item | undefined;
      if (!got) {
        return undefined;
      }
      const picked = Array.isArray(got) ? got : [got];
      const actions = new Set(picked.map((i) => i.action));
      checked = new Set(picked.flatMap((i) => (i.root ? [i.root] : [])));
      if (actions.has("open")) {
        await (eff.config ? vscode.window.showTextDocument(vscode.Uri.file(eff.config))
          : vscode.commands.executeCommand("gitpro.openConfig"));
        return undefined;
      }
      if (actions.has("addFolder")) {
        const uris = await vscode.window.showOpenDialog({ canSelectFiles: false, canSelectFolders: true,
          canSelectMany: true, openLabel: "Add to Scan Roots" });
        add((uris ?? []).filter((u) => u.scheme === "file").map((u) => real(u.fsPath)));
        continue;
      }
      if (actions.has("addWorkspace")) {
        add(outside);
        continue;
      }
      break;
    }
    const chosen = roots.filter((r) => checked.has(r));
    if (!chosen.length) {
      void ui.warn("gitpro: no scan root checked; nothing changed.");
      return undefined;
    }
    const where = eff.config ? tilde(eff.config, home) : "~/.config/gitpro.toml";
    const how = await ui.pick<Item>([
      { label: "Use for This Session", action: "session",
        description: "the Repo Overrides view scans these until reload" },
      { label: "Show TOML Snippet", action: "toml", description: `[scan] roots to paste into ${where}` },
    ], { title: `gitpro: Scan ${chosen.map((r) => tilde(r, home)).join(", ")}` });
    if (how?.action === "session") {
      useForSession(chosen);
    } else if (how?.action === "toml") {
      const language = (await vscode.languages.getLanguages()).includes("toml") ? "toml" : "plaintext";
      const doc = await vscode.workspace.openTextDocument({ language, content: tomlSnippet(chosen, home, eff.config) });
      await vscode.window.showTextDocument(doc);
    } else {
      return undefined;
    }
    return chosen;
  }

  context.subscriptions.push(vscode.commands.registerCommand("gitpro.scanRoots", chooseRoots));
  return { scanDirs, scanned };
}
