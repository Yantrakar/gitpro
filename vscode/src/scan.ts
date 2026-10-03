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

/** The fixable repos in the order the tree lists them: what Review One by One steps through. */
export function steps(groups: Group[]): Repo[] {
  return groups.flatMap((g) => g.children.flatMap((b) => b.repos)).filter((r) => r.fixable);
}

/** A step's answer; undefined (Escape) cancels the review. */
export type Choice = "fix" | "skip" | "back" | "fixRest" | "skipRest";

/**
 * Asks about each repo in turn, recording fix (true) or skip (false) by path in `fix`.
 * true once every repo is decided; false if cancelled, leaving `fix` as far as it got.
 */
export async function walk(rs: Repo[], fix: Map<string, boolean>, choose: (i: number) => Promise<Choice | undefined>):
  Promise<boolean> {
  for (let i = 0; i < rs.length;) {
    const c = await choose(i);
    if (!c) {
      return false;
    }
    if (c === "back") {
      i = Math.max(0, i - 1);
    } else if (c === "fixRest" || c === "skipRest") {
      rs.slice(i).forEach((r) => fix.set(r.path, c === "fixRest"));
      return true;
    } else {
      fix.set(rs[i].path, c === "fix");
      i++;
    }
  }
  return true;
}

/** What a step says about fixing or skipping its repo. */
export function stepText(r: Repo, home: string): { where: string; fix: string; removes: string; skip: string } {
  return {
    where: `${tilde(r.path, home)} · ${r.description}`,
    fix: r.now === r.after ? `already uses ${r.after}` : `${r.now} → ${r.after}`,
    removes: `removes ${r.keys.join(" and ")} from its .git/config${r.now === r.after ? ": a duplicate, nothing changes" : ""}`,
    skip: `keeps ${r.now}`,
  };
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

/** The untitled TOML document gitpro.scanSuggest opens. */
export function suggestions(r: Report, home: string): string {
  const head = "# paste the blocks you want into ~/.config/gitpro.toml, run gitpro apply, then Fix\n" +
    "# an identity applies to every repo of its owners\n";
  const unmatched = r.unmatched.map((p) => {
    const o = r.overrides.find((x) => x.path === p);
    const tail = o ? ` (${o.owner}): no identity can match it; after fixing it uses ${who(o.after)}` : "";
    return `# unmatched: ${tilde(p, home)}${tail}\n`;
  });
  return [head, ...r.suggestions.map((s) => s.toml + "\n"), ...(unmatched.length ? [unmatched.join("")] : [])]
    .join("\n");
}
