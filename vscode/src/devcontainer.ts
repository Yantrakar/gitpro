// Spec 13 §5: "gitpro: Set up devcontainer pin" and the one-per-session nudge. devcontainer.json is a team file:
// the extension shows the line and copies it; it writes only when the user picks Write, and edits the file
// itself (0.4 dropped `gitpro devcontainer --write`): the one key, nothing else touched, and never a file with
// comments (JSONC), which it cannot edit without losing them. Commands run through Cli as argv arrays.
// The vscode and ./cli modules are loaded inside registerDevcontainer, so the pure parts below load without
// VS Code (the node:test unit tests import this file).
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type * as vscode from "vscode";
import type { Cli } from "./cli";
import { settingsCall } from "./capabilities";

/** The initializeCommand, line for line as gitpro's devcontainer.LINE (tests/test_devcontainer.py checks it). */
export const LINE = [
  "command true || exit 0",
  "# runs 'gitpro pin' on hosts that have gitpro, else nothing (line 1 stops Windows cmd)",
  "command -v gitpro >/dev/null 2>&1 || exit 0",
  "IFS= read -r dir <<'GITPRO_PATH'; gitpro pin -y \"$dir\" || true; exit 0",
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
export type NudgeResult = "shown" | "seen" | "off" | "no-file" | "has-pin" | "no-identity";
const PIN_RE = /gitpro\s+pin\b[^\n;|&]*?\s(?:-y|--yes)(?=\s|[;|&]|$)/;

/** What Set up devcontainer pin works out for one devcontainer.json (the shape `devcontainer --json` had). */
export interface Proposal {
  ok: boolean;
  file: string | null;
  tracked: boolean;
  has_pin: boolean;
  existing_command: unknown;
  proposed: unknown;
  /** Comments or trailing commas (JSONC), or text that is not JSON: the file is not edited, the line is shown. */
  has_comments: boolean;
  written: boolean;
  /** The user's string or array command moved into the object form (runs in parallel with the pin). */
  merged?: boolean;
  /** Why the file cannot be edited or read (shown with the line to paste). */
  error?: string;
}

/** Text without comments and trailing commas, and whether it had any (strings are left alone). */
export function stripJsonc(text: string): { text: string; jsonc: boolean } {
  let out = "";
  let jsonc = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') {
      let j = i + 1;
      while (j < text.length && text[j] !== '"') {
        j += text[j] === "\\" ? 2 : 1;
      }
      out += text.slice(i, j + 1);
      i = j;
    } else if (c === "/" && text[i + 1] === "/") {
      jsonc = true;
      while (i < text.length && text[i] !== "\n") {
        i++;
      }
      out += "\n";
    } else if (c === "/" && text[i + 1] === "*") {
      jsonc = true;
      const end = text.indexOf("*/", i + 2);
      i = end < 0 ? text.length : end + 1;
    } else if (c === ",") {
      const next = /^\s*([}\]])/.exec(stripLeading(text.slice(i + 1)));
      if (next) {
        jsonc = true;
      } else {
        out += c;
      }
    } else {
      out += c;
    }
  }
  return { text: out, jsonc };
}

/** Skips whitespace and comments at the start of s (what may sit between a comma and a closing bracket). */
function stripLeading(s: string): string {
  let t = s;
  for (;;) {
    const m = /^(\s+|\/\/[^\n]*|\/\*[\s\S]*?\*\/)/.exec(t);
    if (!m) {
      return t;
    }
    t = t.slice(m[0].length);
  }
}

/** Reads a devcontainer.json's text: what it has now, what to add, and whether it can be edited in place.
 * Never throws: a file that is not an object (or not JSON at all) comes back with `error` and the line to add. */
export function inspectText(file: string, text: string): Proposal {
  const base = { ok: true, file, tracked: false, has_pin: false, existing_command: null as unknown, proposed: LINE as unknown,
    has_comments: false, written: false };
  const { text: plain, jsonc } = stripJsonc(text.replace(/^\uFEFF/, ""));
  let obj: unknown;
  try {
    obj = JSON.parse(plain);
  } catch (e) {
    return { ...base, ok: false, has_comments: true, error: `${file} cannot be parsed (${(e as Error).message})` };
  }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
    return { ...base, ok: false, has_comments: true, error: `${file}: the top level is not an object` };
  }
  const existing = (obj as Record<string, unknown>).initializeCommand;
  const pinned = hasPin(existing);
  let proposed: unknown;
  try {
    proposed = proposeLine(existing);
  } catch (e) {
    return { ...base, ok: false, has_comments: true, existing_command: existing ?? null, has_pin: pinned,
      error: (e as Error).message };
  }
  return { ...base, has_pin: pinned, existing_command: existing ?? null, proposed, has_comments: jsonc,
    merged: !pinned && (typeof existing === "string" || Array.isArray(existing)) && proposed !== LINE };
}

/** The text with initializeCommand set to `value`, everything else byte for byte: a new key is inserted before
 * the closing brace (the file's own indent, trailing newline kept); an existing key is re-serialized with the
 * file's indent. Only for text without comments (inspectText says so) whose top level is an object. */
export function withInitializeCommand(text: string, value: unknown): string {
  const indent = /^([ \t]+)"/m.exec(text)?.[1] ?? "  ";
  const bom = text.startsWith("\uFEFF") ? "\uFEFF" : "";
  const body = text.slice(bom.length);
  const obj = JSON.parse(body) as Record<string, unknown>;
  if (Object.prototype.hasOwnProperty.call(obj, "initializeCommand")) {
    obj.initializeCommand = value;
    return bom + JSON.stringify(obj, null, indent) + (body.endsWith("\n") ? "\n" : "");
  }
  const close = body.lastIndexOf("}");
  const before = body.slice(0, close).replace(/\s+$/, "");
  const comma = before.endsWith("{") ? "" : ",";
  const entry = `"initializeCommand": ${JSON.stringify(value, null, indent).replace(/\n/g, "\n" + indent)}`;
  return `${bom}${before}${comma}\n${indent}${entry}\n${body.slice(close)}`;
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
  if (existing === LINE.replace('gitpro pin -y "$dir"', 'gitpro pin "$dir"')) {
    return LINE;
  }
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
    lines.push(p.error ? `${p.error}.` : "The file has comments, which writing would lose.",
      "Copy the key and add it by hand.", "");
  } else if (p.tracked) {
    lines.push("The file is committed and shared with your team.", "");
  }
  lines.push(NOTE);
  return lines.join("\n");
}

function tildeOf(p: string, home: string): string {
  return p === home || p.startsWith(home + path.sep) ? "~" + p.slice(home.length) : p;
}

/** Why the file must not be written (a symlink out of the repo, read-only), else undefined. */
export function editRefusal(file: string, repo: string): string | undefined {
  try {
    const real = fs.realpathSync(file);
    const top = fs.realpathSync(repo);
    if (real !== top && !real.startsWith(top + path.sep)) {
      return `${file} is a symlink to ${real}, outside the repo`;
    }
    fs.accessSync(real, fs.constants.W_OK);
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === "EACCES" ? `${file} is read-only` : `${file}: ${String(e)}`;
  }
  return undefined;
}

/** Replaces `file` with `next`, unless its content is no longer `shown` (what the dialogs asked about): written to
 * a temporary file in the same directory, then renamed, so an interruption leaves the old file or the new one.
 * Returns why it did not write, else undefined. A symlink is written through, as editRefusal checked. */
export function replaceIfUnchanged(file: string, shown: string, next: string): string | undefined {
  let tmp: string | undefined;
  try {
    const real = fs.realpathSync(file);
    if (fs.readFileSync(real, "utf8") !== shown) {
      return `${file} changed since it was shown; nothing written`;
    }
    tmp = path.join(path.dirname(real), `.${path.basename(real)}.${process.pid}.tmp`);
    const fd = fs.openSync(tmp, "wx", fs.statSync(real).mode & 0o777);
    try {
      fs.writeFileSync(fd, next);
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(tmp, real);
    return undefined;
  } catch (e) {
    if (tmp) {
      fs.rmSync(tmp, { force: true });
    }
    return String(e);
  }
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
    const dir = typeof target === "string" ? target : cli.dir();
    if (!dir) {
      void ui.error("Open a file in a git repository first.");
      return undefined;
    }
    const file0 = locate(dir);
    if (!file0) {
      void ui.error(`No devcontainer.json in ${tildeOf(dir, home)} (looked at ${CANDIDATES.join(", ")}); create one with `
        + "the Dev Containers extension first.");
      return undefined;
    }
    let text: string;
    try {
      text = fs.readFileSync(file0, "utf8");
    } catch (e) {
      void ui.error(`Cannot read ${tildeOf(file0, home)}: ${String(e)}`);
      return undefined;
    }
    const p = inspectText(file0, text);
    const file = tildeOf(file0, home);
    if (p.has_pin) {
      void ui.info(`Already set up: ${file} runs gitpro pin in initializeCommand.`);
      return "already";
    }
    const tracked = (await cli.git(["ls-files", "--error-unmatch", "--", file0], dir, { quiet: true })).code === 0;
    p.tracked = tracked;
    const refusal = p.error ?? editRefusal(file0, dir);
    const pick = await ui.modal(question(refusal ? { ...p, error: p.error ?? refusal, has_comments: true } : p, home),
      ...(p.has_comments || refusal ? ["Copy"] : ["Write", "Copy"]));
    if (pick === "Copy") {
      await vs.env.clipboard.writeText(snippet(p.proposed));
      void ui.info(`Copied the initializeCommand line: paste it into ${file}.`);
      return "copied";
    }
    if (pick !== "Write") {
      return undefined;
    }
    if (tracked) {
      const sure = await ui.modal("This file is shared with your team. Write anyway?\n\nThe line pins only on hosts "
        + "that have gitpro: every teammate who opens this container needs gitpro on their host to get the right "
        + "identity (without it, the line does nothing).", "Write");
      if (sure !== "Write") {
        return undefined;
      }
    }
    const failed = replaceIfUnchanged(file0, text, withInitializeCommand(text, p.proposed));
    if (failed) {
      void ui.error(`Cannot write ${file}: ${failed}`);
      return undefined;
    }
    cli.log.info(`✓ ${file}: initializeCommand runs gitpro pin on the next container start`);
    await vs.window.showTextDocument(vs.Uri.file(file0));
    const merged = p.merged ? ` Note: ${MERGED}.` : "";
    void ui.info(`Written: the next container start runs gitpro pin on the host.${merged} `
      + (tracked ? "Undo: git checkout -- the file." : "Undo: remove the initializeCommand key."));
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
    nudged.add(folder);
    type Which = { settings?: Record<string, { value: unknown }> };
    const call = settingsCall(cli.caps, folder);
    const w = await cli.json<Which>(call.argv, { cwd: folder, quiet: true, accept: call.accept });
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
