// The identity status bar on screen: `gitpro why --json` for the current folder, red on a problem,
// a click offers the fixes the CLI proposed. The text/tooltip/actions logic is in identityBar.ts (pure).
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";
import type { Capabilities } from "./capabilities";
import { settingsCall, UPDATE } from "./capabilities";
import { Cli, exec, failure, Result, shq, ui } from "./cli";
import { parseOrigin, status as whichStatus } from "./identity";
import { Action, actionsFor, BarState, inContainer, OtherFolder, pickFolder, scmRoot, stateFrom, tooltipFrom,
  undoPath, updateTooltip, WhyJson } from "./identityBar";
import { tilde } from "./scan";

const WHY_TIMEOUT = 5_000;
const OTHERS_TTL = 30_000;

/** The folder the status bar and the repo commands work on; remembers the last active workspace folder. */
export function trackFolder(context: vscode.ExtensionContext): () => string | undefined {
  let lastActive: string | undefined;
  const note = (e: vscode.TextEditor | undefined) => {
    const uri = e?.document.uri;
    const f = uri?.scheme === "file" ? vscode.workspace.getWorkspaceFolder(uri) : undefined;
    if (f) {
      lastActive = f.uri.fsPath;
    }
  };
  note(vscode.window.activeTextEditor);
  context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(note));
  return () => {
    const doc = vscode.window.activeTextEditor?.document;
    const folders = (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath);
    let dir = pickFolder(doc?.uri.scheme === "file" ? doc.uri.fsPath : undefined, lastActive, folders);
    // A file whose directory was deleted (or a folder removed on disk): the nearest directory that exists.
    while (dir && !fs.existsSync(dir) && path.dirname(dir) !== dir) {
      dir = path.dirname(dir);
    }
    return dir;
  };
}

export function containerHere(): boolean {
  return inContainer(process.env, fs.existsSync) ||
    vscode.env.remoteName === "dev-container" || vscode.env.remoteName === "attached-container";
}

export interface IdentityBar {
  refresh(): Promise<void>;
  status(): { text: string; visible: boolean; tooltip: string; background: string | undefined };
}

type Mode = "hidden" | "why" | "which" | "gitpro";

export function registerIdentityBar(context: vscode.ExtensionContext, cli: Cli, caps: Capabilities): IdentityBar {
  const log = cli.log;
  const home = os.homedir();
  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 0);
  item.command = "gitpro.which";
  context.subscriptions.push(item);

  let mode: Mode = "hidden";
  let why: WhyJson | undefined;
  let shownDir: string | undefined; // the directory the item currently describes
  let tooltip = "";
  let background: string | undefined;
  const warned = new Set<string>(); // dirs whose `why` failed, warned once each
  // Other workspace folders' bar texts for the tooltip; the promise is cached while in flight, so refreshes in a
  // burst share one `why` per folder instead of starting one each.
  const others = new Map<string, { at: number; text: Promise<string> }>();

  const settings = () => vscode.workspace.getConfiguration("gitpro.statusBar");

  function show(text: string, tip: string, bg: "error" | "warning" | undefined, m: Mode, dir?: string): void {
    mode = m;
    shownDir = dir;
    item.text = text;
    tooltip = tip;
    item.tooltip = new vscode.MarkdownString(tip); // not trusted: no command links, no HTML
    background = bg ? `statusBarItem.${bg}Background` : undefined;
    item.backgroundColor = background ? new vscode.ThemeColor(background) : undefined;
    item.show();
  }

  function hide(): void {
    mode = "hidden";
    why = undefined;
    shownDir = undefined;
    item.hide();
  }

  function gitproMissing(): void {
    why = undefined;
    show("$(account) gitpro?", updateTooltip(caps.version), "warning", "gitpro");
  }

  // The config file `why` read: refresh when it changes. RelativePattern so files outside the workspace work.
  let watched: string | undefined;
  let configWatcher: vscode.FileSystemWatcher | undefined;
  function watchConfig(file: string | undefined): void {
    if (!file || file === watched) {
      return;
    }
    configWatcher?.dispose();
    watched = file;
    configWatcher = vscode.workspace.createFileSystemWatcher(
      new vscode.RelativePattern(vscode.Uri.file(path.dirname(file)), path.basename(file)));
    configWatcher.onDidChange(refresh);
    configWatcher.onDidCreate(refresh);
    configWatcher.onDidDelete(refresh);
  }
  context.subscriptions.push({ dispose: () => configWatcher?.dispose() });

  function parseWhy(r: Result): WhyJson | undefined {
    try {
      const j = JSON.parse(r.stdout) as WhyJson;
      return j && j.expected && j.effective ? j : undefined;
    } catch {
      return undefined;
    }
  }

  /** The other workspace folders' identities for the tooltip, one `why --json` each, cached 30 s. */
  async function otherFolders(dir: string): Promise<OtherFolder[]> {
    const folders = vscode.workspace.workspaceFolders ?? [];
    if (folders.length < 2) {
      return [];
    }
    const mine = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(dir))?.uri.fsPath;
    const showKey = settings().get<boolean>("showKey", true);
    return Promise.all(folders.map((f) => f.uri.fsPath).filter((f) => f !== mine).map(async (folder) => {
      let hit = others.get(folder);
      if (!hit || Date.now() - hit.at >= OTHERS_TTL) {
        // Shared by every refresh that wants it, so not tied to one refresh's AbortSignal.
        const entry = { at: Date.now(), text: Promise.resolve("?") };
        entry.text = (async () => {
          const r = await cli.run(["why", "--json", folder], { cwd: folder, quiet: true, timeout: WHY_TIMEOUT,
            accept: [0, 1] });
          const j = r ? parseWhy(r) : undefined;
          if (!j && others.get(folder) === entry) {
            others.delete(folder); // do not keep a failure for 30 s
          }
          return j ? stateFrom(j, showKey).text : r && /not a git repository/.test(failure(r)) ? "not a git repo" : "?";
        })();
        hit = entry;
        others.set(folder, hit);
      }
      return { folder, text: await hit.text };
    }));
  }

  /** Effective email only (`why --json`'s `settings` with `why.commit`, else `which --json`), for a repo `why`
   * cannot judge (no config). */
  async function fallback(dir: string, mine: number, signal: AbortSignal): Promise<void> {
    const call = settingsCall(caps, dir);
    const r = await cli.run(call.argv, { cwd: dir, quiet: true, timeout: WHY_TIMEOUT, signal, accept: call.accept });
    if (mine !== generation) {
      return latest;
    }
    if (!r || typeof r.code !== "number" || !call.accept.includes(r.code)) {
      if (r && /not a git repository/.test(failure(r))) {
        hide();
      } else {
        gitproMissing();
      }
      return;
    }
    type W = { error?: string; repo?: string; remotes: Record<string, string>;
      settings?: Record<string, { value: string | null; origin: string | null }> };
    let w: W;
    try {
      w = JSON.parse(r.stdout) as W;
    } catch {
      log.error(`✗ gitpro ${call.argv.slice(0, 2).join(" ")} printed no JSON for ${dir}`);
      gitproMissing();
      return;
    }
    if (!w.settings) {
      // `why --json` without a config has `repo` and `settings` only for a git repository
      if (!w.repo || /not a git repository/.test(w.error ?? failure(r))) {
        hide();
      } else {
        gitproMissing();
      }
      return;
    }
    const set = (k: string) => {
      const v = w.settings?.[k];
      return parseOrigin(v?.value ? `${v.origin ?? ""}\t${v.value}` : "");
    };
    const remotes = Object.entries(w.remotes ?? {}).map(([n, u]) => `remote.${n}.url ${u}`);
    const s = whichStatus(set("user.name"), set("user.email"), remotes, containerHere());
    const tip = s.warning && !containerHere() ? `${s.tooltip}\n\n→ gitpro setup ${dir}` : s.tooltip;
    why = undefined;
    show(s.text, tip, s.warning ? "warning" : undefined, "which", dir);
  }

  let generation = 0;
  let latest: Promise<void> = Promise.resolve();
  // Resolves once the item reflects the newest refresh, even if another one started meanwhile.
  let running: AbortController | undefined;
  function refresh(): Promise<void> {
    running?.abort(); // a superseded refresh's gitpro/git child is killed, not left to finish
    running = new AbortController();
    latest = update(++generation, running.signal);
    return latest;
  }

  async function update(mine: number, signal: AbortSignal): Promise<void> {
    const dir = cli.dir();
    if (!dir) {
      hide();
      return;
    }
    if (!caps.has("why")) {
      const top = await cli.git(["rev-parse", "--show-toplevel"], dir, { signal });
      if (mine !== generation) {
        return latest;
      }
      return top.code === 0 ? gitproMissing() : hide();
    }
    const r = await cli.run(["why", "--json", dir], { cwd: dir, quiet: true, timeout: WHY_TIMEOUT, accept: [0, 1],
      signal });
    if (mine !== generation) {
      return latest;
    }
    if (!r) {
      return gitproMissing();
    }
    if (r.code === "TIMEOUT") {
      if (shownDir === dir) {
        log.warn(`⚠ gitpro why took more than ${WHY_TIMEOUT / 1000}s for ${tilde(dir, home)}; keeping the status`);
        return;
      }
      // The state on screen is another folder's: never show it for this one.
      log.warn(`⚠ gitpro why took more than ${WHY_TIMEOUT / 1000}s for ${tilde(dir, home)}`);
      why = undefined;
      return show("$(account) ?", `gitpro why took more than ${WHY_TIMEOUT / 1000}s for ${tilde(dir, home)}; ` +
        "it is tried again on the next refresh", "warning", "which", dir);
    }
    if (r.code === 0 || r.code === 1) {
      const j = parseWhy(r);
      if (!j) {
        log.error(`✗ gitpro why --json printed no JSON for ${tilde(dir, home)}; is gitpro up to date?`);
        return gitproMissing();
      }
      watchConfig(j.config?.path);
      const extra = await otherFolders(dir);
      if (mine !== generation) {
        return latest;
      }
      const s: BarState = stateFrom(j, settings().get<boolean>("showKey", true));
      why = j;
      return show(s.text, tooltipFrom(j, { home, inContainer: containerHere(), others: extra }), s.background, "why",
        dir);
    }
    const err = failure(r);
    if (/not a git repository/.test(err)) {
      return hide();
    }
    if (!/no config found/.test(err) && !warned.has(dir)) {
      warned.add(dir);
      log.warn(`⚠ gitpro why failed for ${tilde(dir, home)}: ${err}; showing the effective email only`);
    }
    return fallback(dir, mine, signal);
  }

  // ---------------------------------------------------------------- click: quick actions

  async function runArgv(argv: string[], dir: string): Promise<void> {
    const cmd = argv.map(shq).join(" ");
    if ((await ui.modal(`Run this command?\n\n${cmd}`, "Run")) !== "Run") {
      log.info(`✗ cancelled: ${cmd}`);
      return;
    }
    log.info(`→ ${cmd}`);
    let r: Result | undefined;
    if (argv[0] === "git") {
      r = await cli.git(argv.slice(1), dir);
      (r.stdout + r.stderr).trimEnd().split("\n").filter(Boolean).forEach((l) => log.info(`  ${l}`));
    } else if (argv[0] === "gitpro") {
      r = await cli.show(argv.slice(1), { cwd: dir, quiet: false });
    } else {
      log.error(`✗ refusing to run ${argv[0]}: the extension runs only git and gitpro`);
      return;
    }
    if (!r) {
      return;
    }
    if (r.code !== 0) {
      log.error(`  ✗ exit ${r.code}`);
      if (argv[0] === "git") {
        void ui.error(failure(r), "Show Log").then((p) => p && log.show());
      }
    } else {
      log.info("  ✓ done");
      const undo = undoPath(r.stdout);
      if (undo) {
        log.info(`  ↩ undo: sh ${undo}`);
        void ui.info(`Done. Undo script: ${tilde(undo, home)}`, "Undo").then(async (pick) => {
          if (pick === "Undo") {
            log.info(`↩ $ sh ${undo}`);
            const u = await exec("sh", [undo], { cwd: home });
            log.info(u.code === 0 ? "  ✓ undone" : `  ✗ undo failed: ${failure(u)}`);
            await refresh();
          }
        });
      }
    }
    await refresh();
  }

  async function perform(a: Action, dir: string): Promise<void> {
    log.info(`status bar: ${a.label}`);
    switch (a.kind) {
      case "argv":
        return runArgv(a.argv ?? [], dir);
      case "command":
        await vscode.commands.executeCommand(a.command!);
        return;
      case "explain":
        await cli.show(["why", dir], { cwd: dir, accept: [0, 1] });
        return;
      case "log":
        log.show();
    }
  }

  type ActionItem = vscode.QuickPickItem & { action: Action };

  async function choose(dir: string, j: WhyJson | undefined, placeHolder: string): Promise<void> {
    const items: ActionItem[] = actionsFor(j, caps, home).map((action) => ({ label: action.label, action,
      description: action.argv ? action.argv.map(shq).join(" ") : undefined, detail: action.detail }));
    const pick = await ui.pick(items, { title: `gitpro: ${tilde(dir, home)}`, placeHolder });
    if (!pick) {
      log.info("status bar: cancelled");
      return;
    }
    await perform(pick.action, dir);
  }

  /** The Source Control button: the same actions for the repo whose row was clicked, the bar left as it is. */
  async function clickRepo(dir: string): Promise<void> {
    const r = await cli.run(["why", "--json", dir], { cwd: dir, quiet: true, timeout: WHY_TIMEOUT, accept: [0, 1] });
    const j = r && (r.code === 0 || r.code === 1) ? parseWhy(r) : undefined;
    if (!j) {
      log.warn(`⚠ gitpro why gave no identity for ${tilde(dir, home)}; offering the generic actions`);
    }
    const text = j ? stateFrom(j, settings().get<boolean>("showKey", true)).text : "";
    await choose(dir, j, text.replace(/^\$\([\w-]+\)\s*/, ""));
  }

  async function click(arg?: unknown): Promise<void> {
    const repo = caps.has("why") ? scmRoot(arg) : undefined;
    if (repo) {
      return clickRepo(repo);
    }
    await refresh();
    const dir = cli.dir();
    if (!dir || mode === "hidden") {
      void ui.error("Open a file in a git repository first.");
      return;
    }
    if (mode === "gitpro") {
      const msg = caps.version === "?" ? `gitpro not found. Install it with: ${UPDATE}`
        : `gitpro ${caps.version} is too old for the identity bar. Update: ${UPDATE}`;
      void ui.warn(msg, "Copy Command", "Open Settings").then((p) => p === "Copy Command"
        ? vscode.env.clipboard.writeText(UPDATE)
        : p && vscode.commands.executeCommand("workbench.action.openSettings", "gitpro.path"));
      return;
    }
    // Only fixes for this repo: a result kept from another folder (e.g. after a timeout) offers none.
    const mine = shownDir === dir ? why : undefined;
    await choose(dir, mine, item.text.replace(/^\$\([\w-]+\)\s*/, ""));
  }

  // ---------------------------------------------------------------- triggers

  let timer: ReturnType<typeof setInterval> | undefined;
  function schedule(): void {
    if (timer) {
      clearInterval(timer);
    }
    const s = settings().get<number>("refreshSeconds", 0);
    timer = s > 0 ? setInterval(() => void refresh(), s * 1000) : undefined;
  }
  schedule();

  const gitConfig = vscode.workspace.createFileSystemWatcher("**/.git/config");
  const pin = vscode.workspace.createFileSystemWatcher("**/.git/git-identities");
  context.subscriptions.push(gitConfig, pin,
    { dispose: () => timer && clearInterval(timer) },
    gitConfig.onDidChange(refresh), gitConfig.onDidCreate(refresh),
    pin.onDidChange(refresh), pin.onDidCreate(refresh), pin.onDidDelete(refresh),
    vscode.window.onDidChangeActiveTextEditor(refresh),
    vscode.window.onDidChangeWindowState((s) => s.focused && refresh()),
    vscode.window.onDidCloseTerminal(refresh),
    vscode.workspace.onDidChangeWorkspaceFolders(() => {
      others.clear();
      return refresh();
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("gitpro.statusBar")) {
        schedule();
        others.clear();
        void refresh();
      }
    }),
    vscode.commands.registerCommand("gitpro.which", click),
  );

  return {
    refresh,
    status: () => ({ text: item.text, visible: mode !== "hidden", tooltip, background }),
  };
}
