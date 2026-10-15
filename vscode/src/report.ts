// 19: `gitpro: Report an Issue`. No vscode import: the editor is reached through ReportIo, so plain node can test it.
// The CLI builds the redacted draft and the prefilled URL (spec 19 section 3); this module only shows them and never
// sends anything: no --online from VS Code, ever.

/** The crash line `gitpro` prints on an unexpected exception (spec 19 section 8.1). */
const CRASH_LINE = /✗ internal error \(.*\): this is a gitpro bug/;

/** True when a CLI call's stderr holds the crash line. */
export function isCrash(stderr: string): boolean {
  return CRASH_LINE.test(stderr);
}

/** What `gitpro report --json --print` prints (spec 19 section 3). */
export interface ReportJson {
  ok: boolean;
  title?: string;
  body?: string;
  url?: string;
  blocked?: { kind: string; section: string }[];
}

/** Everything the flow needs from the editor and the CLI. */
export interface ReportIo {
  /** The extension's version, for --client. */
  version: string;
  /** The active workspace folder (the repo), else undefined. */
  dir(): string | undefined;
  /** Run gitpro with argv in cwd; undefined when gitpro is not installed (the caller already told the user). */
  run(args: string[], cwd: string): Promise<{ code: number | string; stdout: string; stderr: string } | undefined>;
  /** Open text in an untitled Markdown document. */
  preview(body: string): Promise<void>;
  /** A modal question; resolves to the button picked, undefined when cancelled. */
  modal(msg: string, ...items: string[]): Promise<string | undefined>;
  error(msg: string): void;
  info(msg: string): void;
  /** Open a URL in the user's browser. */
  openExternal(url: string): Promise<unknown>;
  copy(text: string): Promise<void>;
}

export const OPEN = "Open in Browser";
export const COPY = "Copy Draft";

/** The argv of the report call. `crash` adds --crash. Never --online. */
export function reportArgs(version: string, crash: boolean): string[] {
  return ["report", "--json", "--print", "--client", `vscode-gitpro/${version}`, ...(crash ? ["--crash"] : [])];
}

/** The error text for a blocked draft: kinds and sections only, never a value. */
export function blockedMessage(blocked: { kind: string; section: string }[]): string {
  const hits = blocked.map((b) => `${String(b.kind)} in ${String(b.section)}`);
  return `gitpro did not draft the report: it found ${hits.join(", ") || "something sensitive"}. Nothing was opened or copied.`;
}

/** Run the flow; resolves to what happened, for the tests. */
export async function reportIssue(io: ReportIo, opts: { crash?: boolean } = {}):
  Promise<"opened" | "copied" | "cancelled" | "failed"> {
  const dir = io.dir();
  if (!dir) {
    io.error("Open a folder first.");
    return "failed";
  }
  const r = await io.run(reportArgs(io.version, opts.crash === true), dir);
  if (!r) {
    return "failed";
  }
  let data: ReportJson | undefined;
  try {
    data = JSON.parse(r.stdout) as ReportJson;
  } catch {
    data = undefined;
  }
  if (data && data.ok === false) {
    io.error(blockedMessage(Array.isArray(data.blocked) ? data.blocked : []));
    return "failed";
  }
  if (r.code !== 0 || !data || typeof data.body !== "string" || typeof data.url !== "string") {
    const last = r.stderr.trim().split("\n").pop()?.replace(/^✗ /, "") || `gitpro exited with ${r.code}`;
    io.error(`gitpro report failed: ${last}`);
    return "failed";
  }
  await io.preview(data.body);
  const pick = await io.modal("Open the prefilled GitHub issue form?", OPEN, COPY);
  if (pick === OPEN) {
    await io.openExternal(data.url);
    return "opened";
  }
  if (pick === COPY) {
    await io.copy(data.body);
    io.info("Copied the draft.");
    return "copied";
  }
  return "cancelled";
}
