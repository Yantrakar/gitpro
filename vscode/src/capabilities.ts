// What the installed gitpro can do, from `gitpro --version --json`. No vscode import: tested with plain node.
// There is no minimum version (DECISIONS Q12): each feature asks has("<cap>") and degrades.
import type { Cli } from "./cli";

export type Capabilities = { version: string; has(cap: string): boolean };

export const UPDATE = "pipx install --force git+https://github.com/sandeepshelke/gitpro.git";

/** `{"version": "0.2.0", "capabilities": ["why", ...]}` → Capabilities; anything else (an old CLI's text) → null. */
export function parseCapabilities(stdout: string): Capabilities | null {
  let data: unknown;
  try {
    data = JSON.parse(stdout);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object" || !Array.isArray((data as { capabilities?: unknown }).capabilities)) {
    return null;
  }
  const d = data as { version?: unknown; capabilities: unknown[] };
  const set = new Set(d.capabilities.filter((c): c is string => typeof c === "string"));
  return { version: typeof d.version === "string" ? d.version : "?", has: (cap) => set.has(cap) };
}

/** A CLI from before `--version --json` prints `gitpro 0.1.0`: that version, no capabilities. */
export function fromText(stdout: string): Capabilities | null {
  const m = /^gitpro (\S+)/m.exec(stdout);
  return m ? { version: m[1], has: () => false } : null;
}

export const NONE: Capabilities = { version: "?", has: () => false };

/** The message a command shows when the CLI lacks what it needs. */
export function missing(caps: Capabilities, cap: string): string {
  return caps.version === "?"
    ? `gitpro not found or not working (needs '${cap}'); install: ${UPDATE}`
    : `gitpro ${caps.version} has no '${cap}'; update: ${UPDATE}`;
}

/** Runs `gitpro --version --json` once; on failure version "?" and has() false. */
export async function loadCapabilities(cli: Pick<Cli, "run">): Promise<Capabilities> {
  const r = await cli.run(["--version", "--json"], { quiet: true, timeout: 15_000 });
  if (!r || r.code !== 0) {
    return NONE;
  }
  return parseCapabilities(r.stdout) ?? fromText(r.stdout) ?? NONE;
}

/** One object everyone holds; set() swaps what it answers (gitpro.path changed, CLI updated). */
export class LiveCapabilities implements Capabilities {
  private current: Capabilities = NONE;
  get version(): string {
    return this.current.version;
  }
  has(cap: string): boolean {
    return this.current.has(cap);
  }
  set(caps: Capabilities): void {
    this.current = caps;
  }
}
