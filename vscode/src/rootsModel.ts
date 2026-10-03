// Pure helpers for scan roots (spec 02 §5): `gitpro scan --roots --json` in, roots, messages and the TOML
// snippet out. No vscode import, so they are tested with plain node; roots.ts puts them on screen.
import * as os from "node:os";
import type { Cli } from "./cli";
import { tilde } from "./scan";

/** The capability `scan --roots` and the [scan] table come with. */
export const ROOTS_CAP = "scan.roots";

/** What `gitpro scan --roots --json` prints (spec 02 §2.3). */
export interface RootsJson {
  roots: string[];
  source: string; // "config" | "args" | "default"
  exclude?: string[];
  config?: string | null; // the config file the roots came from or would go to
  missing?: string[];
}

/** The effective roots; source is "config", "default", or the label of the directories passed in. */
export interface Roots {
  roots: string[];
  source: string;
  exclude: string[];
  config: string | null;
  missing: string[];
}

export function rootsArgs(dirs: readonly string[]): string[] {
  return ["scan", "--roots", "--json", ...dirs];
}

function expand(p: string, home: string): string {
  return p === "~" ? home : p.startsWith("~/") ? home + p.slice(1) : p;
}

/**
 * The roots the Repo Overrides view scans. `dirs` (gitpro.scanDirs, or this session's roots) win over the
 * TOML and are reported as `label`; empty, the CLI's own choice ([scan] roots, else ~). undefined when
 * gitpro failed (it showed why). A gitpro without `scan --roots` scans dirs, else home.
 */
export async function effectiveRoots(cli: Pick<Cli, "caps" | "json">, dirs: readonly string[] = [],
  label = "gitpro.scanDirs", home = os.homedir()): Promise<Roots | undefined> {
  if (!cli.caps.has(ROOTS_CAP)) {
    return { roots: dirs.length ? dirs.map((d) => expand(d, home)) : [home], source: dirs.length ? label : "default",
      exclude: [], config: null, missing: [] };
  }
  const r = await cli.json<RootsJson>(rootsArgs(dirs));
  if (!r || !Array.isArray(r.roots)) {
    return undefined;
  }
  return { roots: r.roots, source: dirs.length ? label : r.source, exclude: r.exclude ?? [], config: r.config ?? null,
    missing: r.missing ?? [] };
}

function strip(p: string): string {
  return p.length > 1 ? p.replace(/\/+$/, "") : p;
}

/** True when `dir` is `root` or below it, by whole path segments (/home/me/src is not under /home/me/s). */
export function isUnder(dir: string, root: string): boolean {
  const d = strip(dir);
  const r = strip(root);
  return d === r || d.startsWith(r === "/" ? "/" : r + "/");
}

/** The workspace folders no root covers, in their order. */
export function foldersOutside(roots: readonly string[], folders: readonly string[]): string[] {
  return folders.filter((f) => !roots.some((r) => isUnder(f, r)));
}

/** "2 workspace folders are outside the scan roots" */
export function outsideMessage(n: number): string {
  return `${n} workspace folder${n === 1 ? " is" : "s are"} outside the scan roots`;
}

/** The log line after a scan: "roots: ~, /mnt/d/work (config)". */
export function rootsLog(roots: readonly string[], source: string, home: string): string {
  return `roots: ${roots.map((r) => tilde(r, home)).join(", ")} (${source})`;
}

/** The roots as a TOML array, home as ~ (what [scan] roots accepts): ["~", "/mnt/d/work"]. */
export function tomlRoots(roots: readonly string[], home: string): string {
  // JSON's escapes are TOML's, except that TOML also forbids a raw U+007F.
  return `[${roots.map((r) => JSON.stringify(tilde(r, home)).replace(/\x7f/g, "\\u007f")).join(", ")}]`;
}

/** The untitled document Show TOML Snippet opens; gitpro never edits the config itself. */
export function tomlSnippet(roots: readonly string[], home: string, config: string | null): string {
  const where = config ? tilde(config, home) : "~/.config/gitpro.toml";
  return `# paste into ${where}; if it already has a [scan] table, put only the roots line in it\n` +
    `[scan]\nroots = ${tomlRoots(roots, home)}\n`;
}

/** The directories the Repo Overrides view scans, and how its log line names them: this session's roots
 * win over gitpro.scanDirs; neither leaves the choice to the CLI ([scan] roots, else ~; source undefined). */
export function viewScan(configured: readonly string[], session: readonly string[] | undefined):
  { dirs: string[]; source: string | undefined } {
  if (session?.length) {
    return { dirs: [...session], source: "session" };
  }
  return { dirs: [...configured], source: configured.length ? "gitpro.scanDirs" : undefined };
}
