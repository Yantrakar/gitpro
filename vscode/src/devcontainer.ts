// Spec 13 §5: "gitpro: Set up devcontainer pin" and the one-per-session nudge. devcontainer.json is a team file:
// the extension shows the line and copies it; it writes only when the user picks Write (through the CLI's
// --write, which backs up and leaves an undo script). Commands run through Cli as argv arrays, never a shell.
// The vscode and ./cli modules are loaded inside registerDevcontainer, so the pure parts below load without
// VS Code (the node:test unit tests import this file).
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type * as vscode from "vscode";
import type { Cli } from "./cli";

/** The initializeCommand, line for line as gitpro's devcontainer.LINE (tests/test_devcontainer.py checks it). */
export const LINE = [
  "command true || exit 0",
  "# runs 'gitpro pin' on hosts that have gitpro, else nothing (line 1 stops Windows cmd)",
  "command -v gitpro >/dev/null 2>&1 || exit 0",
  "IFS= read -r dir <<'GITPRO_PATH'; gitpro pin \"$dir\" || true; exit 0",
  "${localWorkspaceFolder}",
  "GITPRO_PATH",
].join("\n");

/** gitpro's devcontainer.NOTE: ends the Show view, as it ends the printed snippet. */
export const NOTE = "initializeCommand runs on the host before the container starts. On Linux, macOS and WSL this line runs gitpro pin when gitpro is installed and does nothing otherwise; on Windows (cmd.exe) its first line exits 0, so a colleague without gitpro is not affected and the container still starts (the build log then shows \"'command' is not recognized as an internal or external command\", which is harmless).";

/** gitpro's devcontainer.MERGED: said when the user's string or array command moves into the object form. */
export const MERGED = "your command now runs alongside gitpro pin (in parallel); its output appears when it finishes";

export const CANDIDATES = [".devcontainer/devcontainer.json", ".devcontainer.json"];
export const NUDGE = "This repo has a devcontainer; add gitpro pin to initializeCommand so the container keeps your identity";
const DONT_ASK = "gitpro.devcontainer.dontAsk";
/** An internal command (not contributed): run the nudge check now; resolves to one NudgeResult per folder. */
export const NUDGE_COMMAND = "gitpro.devcontainerNudge";
export type NudgeResult = "shown" | "seen" | "off" | "no-file" | "has-pin" | "old-gitpro" | "no-identity";
const PIN_RE = /gitpro\s+pin\b/;

/** What `gitpro devcontainer --json` prints (with --write: written, backup, undo). */
export interface Proposal {
  ok: boolean;
  file: string | null;
  tracked: boolean;
  has_pin: boolean;
  existing_command: unknown;
  proposed: unknown;
  has_comments: boolean;
  written: boolean;
  /** The user's string or array command moved into the object form (runs in parallel with the pin). */
  merged?: boolean;
  backup?: string;
  undo?: string;
  error?: string;
}

/** value (string, argv array or object form) already runs `gitpro pin`; mirrors devcontainer.has_pin. */
export function hasPin(value: unknown): boolean {
  if (typeof value === "string") {
    return PIN_RE.test(value);
  }
  if (Array.isArray(value)) {
    return hasPin(value.map(String).join(" "));
  }
  if (value && typeof value === "object") {
    return Object.values(value).some(hasPin);
  }
  return false;
}

/** The initializeCommand to propose given the current one (undefined/null: absent); mirrors devcontainer.propose.
 * An existing command is kept verbatim: a string or array moves into the object form next to ours. */
export function proposeLine(existing: unknown): unknown {
  if (hasPin(existing)) {
    return existing;
  }
  if (existing === undefined || existing === null || (Array.isArray(existing) && existing.length === 0)
    || (typeof existing === "string" && !existing.trim())) {
    return LINE;
  }
  if (typeof existing === "string") {
    const word = existing.trim().split(/\s+/)[0];
    return { gitpro: LINE, [word === "gitpro" ? "cmd" : word]: existing };
  }
  if (Array.isArray(existing)) {
    return { gitpro: LINE, cmd: existing };
  }
  if (typeof existing === "object") {
    const key = ["gitpro", "gitpro-pin"].find((k) => !(k in existing));
    if (!key) {
      throw new Error('initializeCommand already has keys "gitpro" and "gitpro-pin" that do not run gitpro pin');
    }
    return { [key]: LINE, ...existing };
  }
  throw new Error(`initializeCommand is ${JSON.stringify(existing)}, not a string, array or object`);
}

/** `"initializeCommand": <value>`, as the CLI prints it. */
export function snippet(value: unknown): string {
  return `"initializeCommand": ${JSON.stringify(value, null, 2)}`;
}

/** DIR/.devcontainer/devcontainer.json, else DIR/.devcontainer.json, else undefined. */
export function locate(dir: string): string | undefined {
  for (const name of CANDIDATES) {
    const p = path.join(dir, name);
    try {
      if (fs.statSync(p).isFile()) {
        return p;
      }
    } catch {
      // not there
    }
  }
  return undefined;
}

/** The file's text mentions `gitpro pin` (a cheap check for the nudge; comments count, which errs on silence). */
export function fileHasPin(file: string): boolean {
  try {
    return PIN_RE.test(fs.readFileSync(file, "utf8"));
  } catch {
    return true;
  }
}

/** The modal's text: the file, the proposed key, and the host note last. */
export function question(p: Proposal, home = os.homedir()): string {
  const file = tildeOf(p.file ?? "devcontainer.json", home);
  const lines = [`Add initializeCommand to ${file}?`, "", snippet(p.proposed), ""];
  if (p.merged) {
    lines.push(`Note: ${MERGED}.`, "");
  }
  if (p.has_comments) {
    lines.push("The file has comments, which writing would lose: copy the key and add it by hand.", "");
  } else if (p.tracked) {
    lines.push("The file is committed and shared with your team.", "");
  }
  lines.push(NOTE);
  return lines.join("\n");
}

function tildeOf(p: string, home: string): string {
  return p === home || p.startsWith(home + path.sep) ? "~" + p.slice(home.length) : p;
}

/** Registers gitpro.devcontainerSetup and the nudge for workspace folders. */
export function registerDevcontainer(context: vscode.ExtensionContext, cli: Cli): void {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const vs = require("vscode") as typeof import("vscode");
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { ui } = require("./cli") as typeof import("./cli");
  const home = os.homedir();
  const nudged = new Set<string>();

  async function setup(target?: string): Promise<string | undefined> {
    if (!cli.require("devcontainer")) {
      return undefined;
    }
    const dir = typeof target === "string" ? target : cli.dir();
    if (!dir) {
      void ui.error("Open a file in a git repository first.");
      return undefined;
    }
    const p = await cli.json<Proposal>(["devcontainer", "--json", dir], { cwd: dir });
    if (!p || !p.file) {
      return undefined;
    }
    const file = tildeOf(p.file, home);
    if (p.has_pin) {
      void ui.info(`Already set up: ${file} runs gitpro pin in initializeCommand.`);
      return "already";
    }
    const pick = await ui.modal(question(p, home), ...(p.has_comments ? ["Copy"] : ["Write", "Copy"]));
    if (pick === "Copy") {
      await vs.env.clipboard.writeText(snippet(p.proposed));
      void ui.info(`Copied the initializeCommand line: paste it into ${file}.`);
      return "copied";
    }
    if (pick !== "Write") {
      return undefined;
    }
    if (p.tracked) {
      const sure = await ui.modal("This file is shared with your team. Write anyway?\n\nThe line pins only on hosts "
        + "that have gitpro: every teammate who opens this container needs gitpro on their host to get the right "
        + "identity (without it, the line does nothing).", "Write");
      if (sure !== "Write") {
        return undefined;
      }
    }
    // A refusal (comments appeared, a parse error) exits 2: cli.json shows the CLI's error and returns undefined.
    const w = await cli.json<Proposal>(["devcontainer", "--write", "-y", "--json", dir], { cwd: dir });
    if (!w?.written) {
      return undefined;
    }
    cli.log.info(`✓ ${file}: initializeCommand runs gitpro pin on the next container start`);
    const undo = w.undo ? `sh ${tildeOf(w.undo, home)}` : "(none)";
    cli.log.info(`↩ undo: ${undo}`);
    await vs.window.showTextDocument(vs.Uri.file(p.file));
    const merged = p.merged ? ` Note: ${MERGED}.` : "";
    void ui.info(`Written: the next container start runs gitpro pin on the host.${merged} Undo: ${undo}`);
    return "written";
  }

  /** One message per workspace folder per session, for a devcontainer.json without gitpro pin in a repo that
   * has an identity. Never writes: Show runs the setup command, which asks. Resolves to why it did or did not
   * show (NudgeResult) once the message has been answered. */
  async function nudge(folder: string): Promise<NudgeResult> {
    if (context.workspaceState.get<boolean>(`${DONT_ASK}:${folder}`)) {
      return "off";
    }
    if (nudged.has(folder)) {
      return "seen";
    }
    const file = locate(folder);
    if (!file || fileHasPin(file)) {
      return file ? "has-pin" : "no-file";
    }
    if (!cli.caps.has("devcontainer")) {
      return "old-gitpro";
    }
    nudged.add(folder);
    type Which = { settings?: Record<string, { value: unknown }> };
    const w = await cli.json<Which>(["which", "--json", folder], { cwd: folder, quiet: true });
    if (!w?.settings?.["user.email"]?.value) {
      return "no-identity";
    }
    const pick = await ui.info(NUDGE, "Show", "Don't ask again");
    if (pick === "Show") {
      await setup(folder);
    } else if (pick === "Don't ask again") {
      await context.workspaceState.update(`${DONT_ASK}:${folder}`, true);
    }
    return "shown";
  }

  function folders(): string[] {
    return (vs.workspace.workspaceFolders ?? []).filter((f) => f.uri.scheme === "file").map((f) => f.uri.fsPath);
  }

  function nudgeAll(): void {
    for (const f of folders()) {
      nudge(f).catch((e: unknown) => cli.log.debug(`devcontainer nudge: ${String(e)}`));
    }
  }

  context.subscriptions.push(
    vs.commands.registerCommand("gitpro.devcontainerSetup", (dir?: string) => setup(dir)),
    // Not in package.json (no palette entry): the nudge check again, for the given folder or every workspace
    // folder; the integration test drives the nudge through it.
    vs.commands.registerCommand(NUDGE_COMMAND, (dir?: string) =>
      Promise.all((typeof dir === "string" ? [dir] : folders()).map(nudge))),
    vs.workspace.onDidChangeWorkspaceFolders(() => nudgeAll()),
  );
  nudgeAll();
}
