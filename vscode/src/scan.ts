// Pure helpers for the Repo Overrides view: `gitpro scan --json` in, tree model and CLI args out. No vscode import.

export interface Pair {
  name: string;
  email: string;
}

export interface Override {
  path: string;
  short: string;
  remote: string;
  owner: string;
  now: Pair;
  after: Pair; // once the fix removed what it can
  settings: { key: string; value: string; without: string | null }[];
  fixable: string[]; // empty: nothing else sets an identity, so the fix keeps it
}

export interface Suggestion {
  id: string;
  toml: string;
}

/** The parts of `gitpro scan --json` the view uses. */
export interface Report {
  roots: string[];
  repos: number;
  /** reason groups them ("owned by root"); error says what is wrong with this one. */
  unreadable: { path: string; reason: string; error: string }[];
  overrides: Override[];
  suggestions: Suggestion[];
  unmatched: string[];
}

/** Reject malformed CLI reports before they become a tree or a fix plan. Extra envelope fields are allowed. */
export function isReport(value: unknown): value is Report {
  const object = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
  const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every((s) => typeof s === "string");
  const pair = (v: unknown): boolean => object(v) && typeof v.name === "string" && typeof v.email === "string";
  return object(value) && strings(value.roots) && typeof value.repos === "number" &&
    Number.isInteger(value.repos) && value.repos >= 0 &&
    Array.isArray(value.unreadable) && value.unreadable.every((v: unknown) =>
      object(v) && typeof v.path === "string" && typeof v.reason === "string" && typeof v.error === "string") &&
    Array.isArray(value.overrides) && value.overrides.every((v: unknown) =>
      object(v) && typeof v.path === "string" && typeof v.short === "string" && typeof v.remote === "string" &&
      typeof v.owner === "string" && pair(v.now) && pair(v.after) && strings(v.fixable) &&
      Array.isArray(v.settings) && v.settings.every((s: unknown) =>
        object(s) && typeof s.key === "string" && typeof s.value === "string" &&
        (s.without === null || typeof s.without === "string"))) &&
    Array.isArray(value.suggestions) && value.suggestions.every((v: unknown) =>
      object(v) && typeof v.id === "string" && typeof v.toml === "string") && strings(value.unmatched);
}

/** What `gitpro scan --fix --json` prints. */
export interface Fixed {
  removed: { path: string; key: string; value: string }[];
  undo: string | null;
}

export interface Repo {
  path: string;
  label: string;
  description: string;
  tooltip: string;
  fixable: boolean;
  now: string;
  after: string;
  settings: number; // how many the fix removes
  changes: string[]; // "user.email: old → new", one per setting
  keys: string[]; // the settings the fix removes
}

export interface Before {
  label: string; // "from Name <email>", "unchanged", or the identity itself under Kept
  repos: Repo[];
}

export interface Group {
  label: string; // identity after the fix, or KEPT
  description: string;
  children: Before[];
}

export const KEPT = "Kept";

export function who(p: Pair): string {
  return p.email ? `${p.name} <${p.email}>`.trim() : "(no email)";
}

function repos(n: number): string {
  return `${n} repo${n === 1 ? "" : "s"}`;
}

export function tilde(p: string, home: string): string {
  return p === home ? "~" : p.startsWith(home + "/") ? "~" + p.slice(home.length) : p;
}

/** after the fix -> now -> repos, sorted like the CLI: Kept last, unchanged last within a group. */
export function tree(overrides: Override[]): Group[] {
  const groups = new Map<string, Map<string, Override[]>>();
  for (const o of overrides) {
    const after = o.fixable.length ? who(o.after) : "";
    const befores = groups.get(after) ?? new Map<string, Override[]>();
    groups.set(after, befores);
    befores.set(who(o.now), [...(befores.get(who(o.now)) ?? []), o]);
  }
  const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);
  return [...groups].sort(([a], [b]) => Number(!a) - Number(!b) || cmp(a, b)).map(([after, befores]) => {
    const n = [...befores.values()].reduce((s, rs) => s + rs.length, 0);
    return {
      label: after || KEPT,
      description: after ? repos(n) : `${repos(n)} · nothing else sets an identity`,
      children: [...befores]
        .sort(([a], [b]) => Number(a === after) - Number(b === after) || cmp(a, b))
        .map(([before, rs]) => ({
          label: !after ? before : before === after ? "unchanged" : `from ${before}`,
          repos: rs.map(repo),
        })),
    };
  });
}

function repo(o: Override): Repo {
  const lines = o.settings.map((s) => `${s.key}: ${s.value} → ${s.without ?? "kept (nothing else sets it)"}`);
  return {
    path: o.path,
    label: o.short,
    description: o.fixable.length ? o.owner : `${o.owner} · kept`,
    tooltip: [o.path, ...lines].join("\n"),
    fixable: o.fixable.length > 0,
    now: who(o.now),
    after: who(o.after),
    settings: o.fixable.length,
    changes: lines,
    keys: o.fixable,
  };
}

export interface Plan {
  paths: string[];
  settings: number;
  identities: string[]; // what the fixed repos use afterwards
}

/** The fixable repos the user left checked. */
export function plan(groups: Group[], unchecked: ReadonlySet<string>): Plan {
  const rs = groups.flatMap((g) => g.children.flatMap((b) => b.repos)).filter((r) => r.fixable && !unchecked.has(r.path));
  return {
    paths: rs.map((r) => r.path),
    settings: rs.reduce((s, r) => s + r.settings, 0),
    identities: [...new Set(rs.map((r) => r.after))],
  };
}

export function question(p: Plan): string {
  const use = p.identities.length === 1 ? p.identities[0] : "the identity they are listed under";
  return `Remove ${p.settings} setting${p.settings === 1 ? "" : "s"} from ${repos(p.paths.length)}? ` +
    `They'll use ${use}.`;
}

export function scanArgs(dirs: string[]): string[] {
  return ["scan", "--json", ...dirs];
}

export function fixArgs(paths: string[]): string[] {
  return ["scan", "--fix", "--yes", "--json", ...paths.flatMap((p) => ["--repo", p])];
}

export function fixedMessage(f: Fixed): string {
  const n = new Set(f.removed.map((r) => r.path)).size;
  return `Removed ${f.removed.length} setting${f.removed.length === 1 ? "" : "s"} from ${repos(n)}`;
}

/** "30 skipped: 26 owned by root, 4 with a broken .git link", most common reason first; "" for none. */
export function skipped(bad: Report["unreadable"]): string {
  if (!bad.length) {
    return "";
  }
  const counts = new Map<string, number>();
  bad.forEach((b) => counts.set(b.reason, (counts.get(b.reason) ?? 0) + 1));
  const parts = [...counts].sort((a, b) => b[1] - a[1]).map(([why, n]) => `${n} ${why}`);
  return `${bad.length} skipped: ${parts.join(", ")}`;
}

/** Shown above the tree: what was scanned and what git could not read. */
export function summary(r: Report, home: string): string {
  const bad = r.unreadable.length ? ` · ${skipped(r.unreadable)}` : "";
  return `${repos(r.repos)} under ${r.roots.map((p) => tilde(p, home)).join(", ")}${bad}`;
}
