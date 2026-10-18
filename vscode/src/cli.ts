// The one way the extension runs gitpro and git: argv arrays, logged, never a shell.
// Feature modules (roots, lost, clone, devcontainer) receive a Cli and use nothing else to run commands.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";
import type { Capabilities } from "./capabilities";
import { missing } from "./capabilities";
import { exec, redactText, redactUrl, Result, RunOptions } from "./exec";
import { isCrash } from "./report";
import { tilde } from "./scan";

export const INSTALL = "pipx install gitpro";

export { exec, killAll, redactText, redactUrl, within } from "./exec";
export type { Child, Result, RunOptions } from "./exec";

export interface Cli {
  readonly log: vscode.LogOutputChannel;
  /** What the installed gitpro can do (live: follows gitpro.path changes). */
  readonly caps: Capabilities;
  /** gitpro.path with ~ expanded (the checkout's .venv in development). */
  executable(): string;
  /** The repo directory the status bar shows: active editor's folder, else last active / first workspace folder. */
  dir(): string | undefined;
  /** True when the CLI has `cap`; else shows the update message and returns false. */
  require(cap: string): boolean;
  /** Run gitpro; logs the command, exit, stderr. undefined when gitpro is not installed (message shown). */
  run(args: string[], opts?: RunOptions): Promise<Result | undefined>;
  /** run() and put stdout in the log, reveal it; error message (Show Log) on failure. */
  show(args: string[], opts?: RunOptions): Promise<Result | undefined>;
  /** run() and parse stdout as JSON; error message on failure or unparseable output. */
  json<T>(args: string[], opts?: RunOptions): Promise<T | undefined>;
  /** Plain git (never through a shell), logged at debug. */
  git(args: string[], cwd: string, opts?: RunOptions): Promise<Result>;
}

/** Message boxes and pickers, replaceable by the integration tests (Api.ui). Never await info()/warn():
 * a notification resolves only when dismissed. */
export const ui = {
  info: (msg: string, ...items: string[]): Thenable<string | undefined> =>
    vscode.window.showInformationMessage(msg, ...items),
  warn: (msg: string, ...items: string[]): Thenable<string | undefined> =>
    vscode.window.showWarningMessage(msg, ...items),
  error: (msg: string, ...items: string[]): Thenable<string | undefined> =>
    vscode.window.showErrorMessage(msg, ...items),
  /** A modal question; resolves to the button picked, undefined when cancelled. */
  modal: (msg: string, ...items: string[]): Thenable<string | undefined> =>
    vscode.window.showWarningMessage(msg, { modal: true }, ...items),
  pick: <T extends vscode.QuickPickItem>(items: T[], options: vscode.QuickPickOptions): Thenable<T | undefined> =>
    vscode.window.showQuickPick(items, options),
};

/** An argument as a shell would need it, for the log. */
export function shq(a: string): string {
  return /^[\w@%+=:,./~-]+$/.test(a) ? a : `'${a.replace(/'/g, "'\\''")}'`;
}

/** The error a failed run should show: the JSON envelope's `error`, else the last stderr line. */
export function failure(r: Result): string {
  try {
    const e = (JSON.parse(r.stdout) as { error?: unknown }).error;
    if (typeof e === "string" && e) {
      return redactText(e);
    }
  } catch {
    // not JSON
  }
  return redactText(r.stderr.trim().split("\n").pop()?.replace(/^✗ /, "") || `gitpro exited with ${r.code}`);
}

export function makeCli(context: vscode.ExtensionContext, log: vscode.LogOutputChannel, caps: Capabilities,
  dir: () => string | undefined): Cli {
  function executable(): string {
    const set = vscode.workspace.getConfiguration("gitpro").get<string>("path") || "gitpro";
    if (set === "gitpro" && context.extensionMode === vscode.ExtensionMode.Development) {
      const venv = path.join(context.extensionPath, "..", ".venv", "bin", "gitpro");
      if (fs.existsSync(venv)) {
        return venv;
      }
    }
    return set.replace(/^~(?=\/|$)/, os.homedir());
  }

  function failed(r: Result): void {
    const items = isCrash(r.stderr) ? ["Show Log", "Report an Issue"] : ["Show Log"]; // 19
    void ui.error(failure(r), ...items).then((pick) => {
      if (pick === "Report an Issue") {
        void vscode.commands.executeCommand("gitpro.reportIssue", { crash: true });
      } else if (pick) {
        log.show();
      }
    });
  }

  async function run(args: string[], opts: RunOptions = {}): Promise<Result | undefined> {
    const exe = executable();
    const cwd = opts.cwd ?? os.homedir();
    const say = opts.quiet ? log.debug.bind(log) : log.info.bind(log);
    say(`$ ${[exe, ...args].map((a) => shq(redactUrl(a))).join(" ")}    (in ${tilde(cwd, os.homedir())})`);
    const start = Date.now();
    const r = await exec(exe, args, { ...opts, cwd });
    const took = `${((Date.now() - start) / 1000).toFixed(1)}s`;
    if (r.code === "ABORT_ERR") {
      log.debug(`  aborted after ${took} (superseded)`);
      return r;
    }
    const lines = redactText(r.stderr).trimEnd().split("\n").filter(Boolean);
    const okCodes = opts.accept ?? [0];
    if (typeof r.code === "number" && okCodes.includes(r.code)) {
      say(`  exit ${r.code} after ${took}`);
      lines.forEach((l) => (opts.quiet ? log.debug(`  ${l}`) : log.warn(`  ${l}`)));
    } else {
      const bad = opts.quiet ? log.warn.bind(log) : log.error.bind(log);
      bad(`  ✗ exit ${r.code} after ${took}`);
      lines.forEach((l) => bad(`  ${l}`));
    }
    log.trace(`  stdout:\n${redactText(r.stdout)}`);
    if (r.code === "ENOENT") {
      log.warn(`  ✗ ${exe} not found: set gitpro.path, or install it with ${INSTALL}`);
      if (!opts.quiet) {
        // Not awaited: the caller (e.g. a scan showing progress) must not wait for the message to be dismissed.
        void ui.error(`'${exe}' not found. Install it with: ${INSTALL}`, "Open Settings")
          .then((pick) => pick && vscode.commands.executeCommand("workbench.action.openSettings", "gitpro.path"));
      }
      return undefined;
    }
    return r;
  }

  async function show(args: string[], opts: RunOptions = {}): Promise<Result | undefined> {
    const r = await run(args, { cwd: dir() ?? os.homedir(), ...opts });
    if (!r) {
      return undefined;
    }
    redactText(r.stdout).trimEnd().split("\n").forEach((l) => log.info(l));
    log.show(true);
    if (!(typeof r.code === "number" && (opts.accept ?? [0]).includes(r.code)) && !opts.quiet) {
      failed(r);
    }
    return r;
  }

  async function json<T>(args: string[], opts: RunOptions = {}): Promise<T | undefined> {
    const r = await run(args, opts);
    if (!r) {
      return undefined;
    }
    if (!(typeof r.code === "number" && (opts.accept ?? [0]).includes(r.code))) {
      if (!opts.quiet) {
        failed(r);
      }
      return undefined;
    }
    try {
      return JSON.parse(r.stdout) as T;
    } catch {
      log.error(`  ✗ no JSON in the output; is gitpro up to date?\n${redactText(r.stdout.slice(0, 4000))}`);
      if (!opts.quiet) {
        void ui.error("gitpro printed no JSON; is it up to date?", "Show Log").then((pick) => pick && log.show());
      }
      return undefined;
    }
  }

  async function git(args: string[], cwd: string, opts: RunOptions = {}): Promise<Result> {
    log.debug(`$ git ${args.map((a) => shq(redactUrl(a))).join(" ")}    (in ${tilde(cwd, os.homedir())})`);
    const r = await exec("git", args, { ...opts, cwd });
    if (r.code !== 0) {
      log.debug(`  exit ${r.code}: ${redactText(r.stderr.trim().split("\n").pop() ?? "")}`);
    }
    return r;
  }

  function needs(cap: string): boolean {
    if (caps.has(cap)) {
      return true;
    }
    const msg = missing(caps, cap);
    log.warn(`✗ ${msg}`);
    void ui.warn(msg, "Copy Command").then((pick) => {
      if (pick) {
        void vscode.env.clipboard.writeText(msg.slice(msg.lastIndexOf(": ") + 2));
      }
    });
    return false;
  }

  return { log, caps, executable, dir, require: needs, run, show, json, git };
}
