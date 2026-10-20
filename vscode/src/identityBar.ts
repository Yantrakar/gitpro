// The identity status bar as pure functions over `gitpro why --json`: text, tooltip, quick actions.
// No vscode import, so they can be tested with plain node; statusBar.ts puts them on screen.
import type { Capabilities } from "./capabilities";
import { UPDATE } from "./capabilities";
import { tilde } from "./scan";

export interface SettingJson {
  value: string | null;
  origin: string | null;
  scope: string | null;
}

export interface ProblemJson {
  code: string;
  message: string;
  fix: string | null;
  fix_argv?: string[] | null;
}

/** `gitpro why --json` (01 §4.4); `guard` (06) and `policy` (14) appear when those packages are installed. */
export interface WhyJson {
  ok: boolean;
  repo: string;
  config?: { path: string; how: string };
  remotes: Record<string, string>;
  expected: {
    id: string | null;
    name: string | null;
    email: string | null;
    ssh_key: string | null;
    sign: boolean;
    file: string | null;
    match: { remote: string | null; url: string | null; pattern: string | null; rule: string };
  };
  effective: {
    name: SettingJson;
    email: SettingJson;
    signingkey: SettingJson;
    gpgsign: SettingJson;
    sshcommand: SettingJson;
    pinned: boolean;
  };
  problems: ProblemJson[];
  guard?: unknown;
  policy?: unknown;
}

export type Background = "error" | "warning" | undefined;

export interface BarState {
  kind: "match" | "problem" | "none";
  text: string;
  background: Background;
}

/** Same list as 13's devcontainer.in_container(): any of these env vars, or /.dockerenv. */
export const CONTAINER_ENV = ["REMOTE_CONTAINERS", "CODESPACES", "DEVCONTAINER"];
export const DOCKERENV = "/.dockerenv";

export function inContainer(env: Record<string, string | undefined>, fileExists: (p: string) => boolean): boolean {
  return CONTAINER_ENV.some((k) => !!env[k]) || fileExists(DOCKERENV);
}

/** The path after `-i` in an ssh command (`ssh -i ~/.ssh/id_work -o ...`), quotes removed. */
export function sshKeyOf(command: string | null | undefined): string | undefined {
  // Shell-like words: runs of unquoted, '...' and "..." parts (-i'/a b/k' is one word).
  const words = (command ?? "").match(/(?:"[^"]*"|'[^']*'|[^\s"']+)+/g) ?? [];
  const unq = (w: string) => w.replace(/"([^"]*)"|'([^']*)'/g, (_m, d: string | undefined, q: string | undefined) =>
    d ?? q ?? "");
  for (let n = 0; n < words.length; n++) {
    if (words[n] === "-i" && n + 1 < words.length) {
      return unq(words[n + 1]);
    }
    if (words[n].startsWith("-i") && words[n].length > 2) {
      return unq(words[n].slice(2));
    }
  }
  return undefined;
}

function basename(p: string): string {
  return p.replace(/\/+$/, "").split("/").pop() ?? p;
}

/** The SSH key in play: core.sshcommand's -i, else the one the identity expects. */
export function keyOf(json: WhyJson): string | undefined {
  return sshKeyOf(json.effective.sshcommand.value) ?? json.expected.ssh_key ?? undefined;
}

/** Who the repo should commit as: the identity id, else (the [default]) its email. */
export function expectedLabel(json: WhyJson): string {
  return json.expected.id ?? json.expected.email ?? "[default]";
}

export function stateFrom(json: WhyJson, showKey: boolean): BarState {
  const email = json.effective.email.value;
  if (json.problems.some((p) => p.code === "no_identity")) {
    return { kind: "none", text: email ? `$(warning) ${email} · no identity` : "$(warning) no git identity",
      background: "warning" };
  }
  if (json.problems.length) {
    return { kind: "problem", text: `$(error) ${email || "no identity"} ≠ ${expectedLabel(json)}`, background: "error" };
  }
  const key = showKey ? keyOf(json) : undefined;
  const who = json.expected.id ?? email ?? json.expected.email ?? "?";
  return { kind: "match", text: key ? `$(account) ${who} · ${basename(key)}` : `$(account) ${who}`, background: undefined };
}

/** Markdown-escape a value for a tooltip (MarkdownString is not trusted; no HTML). */
export function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/[\\`*_{}[\]()#+!|~]/g, "\\$&").replace(/\r?\n/g, " ");
}

/** Another workspace folder's identity, for the multi-root line. */
export interface OtherFolder {
  folder: string;
  text: string; // a status bar text such as "$(account) personal"
}

export interface TooltipOptions {
  home?: string;
  inContainer?: boolean;
  others?: OtherFolder[];
}

function who(name: string | null, email: string | null): string {
  return email ? `${name ?? "(no name)"} <${email}>` : "(unset)";
}

/** A small key: value summary of an optional block (guard, policy) whose shape belongs to another package. */
function brief(v: unknown): string {
  if (v === null || v === undefined) {
    return "";
  }
  if (typeof v !== "object") {
    return String(v);
  }
  if (Array.isArray(v)) {
    return v.map(brief).join(", ");
  }
  return Object.entries(v as Record<string, unknown>)
    .filter(([, x]) => x === null || typeof x !== "object")
    .map(([k, x]) => `${k}: ${x}`).join(", ");
}

/** Strip the codicon from a status text for use inside a tooltip. */
function plain(text: string): string {
  return text.replace(/^\$\([\w-]+\)\s*/, "");
}

export function containerFix(repo: string): string {
  return `on the host, run: gitpro pin ${repo} — or set it up once: the command "gitpro: Set up devcontainer pin"`;
}

export function tooltipFrom(json: WhyJson, opts: TooltipOptions = {}): string {
  const home = opts.home ?? "";
  const t = (p: string) => (home ? tilde(p, home) : p);
  const e = json.effective;
  const x = json.expected;
  const rows: [string, string][] = [];
  rows.push(["expected", `${x.id ?? "(default)"}  ${who(x.name, x.email)} · ${x.match.rule}`]);
  const from = e.email.origin ? ` · ${e.email.scope ?? ""} ${t(e.email.origin.replace(/^file:/, ""))}` : "";
  rows.push(["effective", `${who(e.name.value, e.email.value)}${from}`]);
  const key = sshKeyOf(e.sshcommand.value);
  rows.push(["key", key ? `${t(key)} via core.sshcommand` : x.ssh_key ? `(none; expected ${t(x.ssh_key)})` : "(none)"]);
  const signing = /^(true|yes|on|1)$/i.test(e.gpgsign.value ?? "");
  rows.push(["signing", signing ? `on ${t(e.signingkey.value ?? "")}`.trim() : "off"]);
  const remotes = Object.entries(json.remotes).map(([n, u]) => `${n} ${u}`);
  rows.push(["remotes", remotes.length ? remotes.join(", ") : "(none)"]);
  rows.push(["pinned", e.pinned ? "yes (.git/git-identities)" : "no"]);
  if (json.guard !== undefined) {
    rows.push(["guard", brief(json.guard)]);
  }
  if (json.policy !== undefined) {
    rows.push(["policy", brief(json.policy)]);
  }
  const parts = [`**gitpro** · ${esc(t(json.repo))}`,
    "| | |\n|---|---|\n" + rows.map(([k, v]) => `| ${k} | ${esc(v)} |`).join("\n")];
  for (const p of json.problems) {
    parts.push(`✗ ${esc(p.message)}`);
    const fix = p.code === "no_identity" && opts.inContainer ? containerFix(json.repo) : p.fix;
    if (fix) {
      parts.push(`→ ${esc(fix)}`);
    }
  }
  if (!json.problems.length) {
    parts.push("✓ identity matches");
  }
  if (opts.others?.length) {
    parts.push("Other folders:\n\n" + opts.others.map((o) => `- ${esc(t(o.folder))}: ${esc(plain(o.text))}`).join("\n"));
  }
  return parts.join("\n\n");
}

/** Tooltip for the `gitpro?` state: gitpro missing, or too old to have `why`. */
export function updateTooltip(version: string): string {
  const what = version === "?" ? "gitpro was not found (or did not answer)." : `gitpro ${esc(version)} has no 'why'.`;
  return `${what}\n\n→ ${esc(UPDATE)}\n\nSet **gitpro.path** if it is installed somewhere else.`;
}

export type ActionKind = "argv" | "command" | "explain" | "log";

export interface Action {
  label: string;
  kind: ActionKind;
  /** kind argv: exactly the fix_argv `why --json` proposed. */
  argv?: string[];
  /** kind command: a VS Code command id. */
  command?: string;
  detail?: string;
}

function labelFor(p: ProblemJson, home: string | undefined): Action | undefined {
  const a = p.fix_argv;
  if (p.code === "no_identity") {
    return { label: "Set up…", kind: "command", command: "gitpro.setup", detail: p.message };
  }
  if (!a || !a.length) {
    // 0.4.2+ CLIs: setup needs a terminal, so a fix that is setup has no argv
    return p.fix === "gitpro setup"
      ? { label: "Set up…", kind: "command", command: "gitpro.setup", detail: p.message } : undefined;
  }
  if (a[0] === "gitpro" && (a[1] === "apply" || a[1] === "plan" || a[1] === "doctor") && a.length === 2) {
    // 0.3 to 0.4.1 CLIs propose these; they are deprecated, and Set Up does the same job
    return { label: "Set up…", kind: "command", command: "gitpro.setup", detail: p.message };
  }
  if (a[0] === "gitpro" && a[1] === "pin" && a.includes("--remote")) {
    // the remotes belong to different identities: pin the one this repo's work goes to
    return { label: `Pin ${a[a.indexOf("--remote") + 1]}'s identity`, kind: "argv", argv: a, detail: p.message };
  }
  if (a[0] === "gitpro" && a[1] === "pin") {
    return { label: "Re-pin this repo", kind: "argv", argv: a, detail: p.message };
  }
  if (a[0] === "git" && a.includes("--unset")) {
    // `git config --file <path> --unset key` (01's fix for a global key/sign mismatch) names its file.
    const file = a.indexOf("--file");
    const where = file >= 0 && a[file + 1] ? (home ? tilde(a[file + 1], home) : a[file + 1])
      : a.includes("--worktree") ? "config.worktree" : ".git/config";
    return { label: `Unset ${a[a.length - 1]} in ${where}`, kind: "argv", argv: a, detail: p.message };
  }
  if (a[0] === "git" && a.includes("set-url")) {
    return { label: `Set ${a[a.length - 2]} URL to ${a[a.length - 1]}`, kind: "argv", argv: a, detail: p.message };
  }
  return { label: `Run ${a.join(" ")}`, kind: "argv", argv: a, detail: p.message };
}

/** The click menu: one fix per problem (only argv the CLI proposed), then the always-items. home: for labels. */
export function actionsFor(json: WhyJson | undefined, caps: Capabilities, home?: string): Action[] {
  const out: Action[] = [];
  for (const p of json?.problems ?? []) {
    const a = labelFor(p, home);
    if (a && !out.some((o) => o.label === a.label)) {
      out.push(a);
    }
  }
  out.push({ label: "Explain (gitpro why)", kind: "explain" });
  if (caps.has("env")) {
    out.push({ label: "Copy env for terminal", kind: "command", command: "gitpro.copyEnv" });
  }
  out.push({ label: "Open config", kind: "command", command: "gitpro.openConfig" },
    { label: "Show Log", kind: "log" });
  return out;
}

/** The status bar's directory: the active editor's folder, else the last active workspace folder, else the first. */
export function pickFolder(activeEditor: string | undefined, lastActive: string | undefined,
  folders: readonly string[]): string | undefined {
  if (activeEditor) {
    const dir = activeEditor.replace(/\/[^/]*$/, "");
    return dir || "/";
  }
  if (lastActive && folders.includes(lastActive)) {
    return lastActive;
  }
  return folders[0];
}

/** The repo folder a Source Control menu passes (a SourceControl with a file rootUri), if any. */
export function scmRoot(arg: unknown): string | undefined {
  const uri = (arg as { rootUri?: { scheme?: unknown; fsPath?: unknown } } | undefined)?.rootUri;
  return uri?.scheme === "file" && typeof uri.fsPath === "string" ? uri.fsPath : undefined;
}

/** The `undo` path a command printed (`↩ undo: sh <path>` or JSON `"undo": "<path>"`), if any. The path runs to
 * the end of the line (it may contain spaces); shell quotes around it are removed. */
export function undoPath(stdout: string): string | undefined {
  const line = /↩ undo: (?:sh )?(.+)$/m.exec(stdout);
  if (line) {
    const p = line[1].trim();
    const q = /^'(.*)'$/.exec(p) ?? /^"(.*)"$/.exec(p);
    return q ? q[1].replace(/'\\''/g, "'") : p;
  }
  try {
    const u = (JSON.parse(stdout) as { undo?: unknown }).undo;
    return typeof u === "string" ? u : undefined;
  } catch {
    return undefined;
  }
}
