// The Lost Work tree from `gitpro lost --json` (spec 03 §5): kind → repo → item. No vscode import: tested with
// plain node (lost.test.ts); lost.ts turns these nodes into TreeItems.
import { tilde } from "./scan";

export interface Unpushed {
  repo: string; branch: string | null; commits: number; upstream: string | null; ahead: number | null;
  detached: boolean; fix: string | null;
}
export interface MergedGone { repo: string; branch: string; commits: number }
export interface Stash { repo: string; ref: string; date: string; subject: string }
export interface Dirty { repo: string; modified: number; untracked: number }
export interface NoRemote { repo: string; commits: number }
export interface RemoteGone {
  repo: string; remote: string; url: string; status: "missing" | "renamed" | "unknown"; renamed_to: string | null;
  detail: string | null; fix: string | null; via: string | null;
}
export interface Duplicate { key: string; repos: string[]; diverged: boolean | null; detail: string | null }

/** The parts of the `lost --json` object the view uses (stale, empty and skipped are not shown). */
export interface LostJson {
  ok: boolean;
  roots?: string[];
  repos?: number;
  unpushed?: Unpushed[];
  merged_gone?: MergedGone[];
  stashes?: Stash[];
  dirty?: Dirty[];
  no_remote?: NoRemote[];
  remote_gone?: RemoteGone[];
  duplicates?: Duplicate[];
  stale?: { repo: string }[];
  warnings?: string[];
}

export interface ItemNode { type: "item"; repo: string; label: string; tooltip?: string; fix: string | null }
export interface RepoNode { type: "repo"; repo: string; label: string; items: ItemNode[] }
export interface KindNode { type: "kind"; kind: string; label: string; description: string; repos: RepoNode[] }
export type LostNode = KindNode | RepoNode | ItemNode;

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : word.endsWith("ch") ? "es" : "s"}`;
}

/** What the CLI prints after the branch name, e.g. "4 commits, no upstream". */
export function unpushedText(u: Unpushed): string {
  if (u.detached) {
    return `${plural(u.commits, "commit")} not on any branch`;
  }
  if (!u.upstream) {
    return `${plural(u.commits, "commit")}, no upstream`;
  }
  return u.ahead ? `${u.ahead} ahead of ${u.upstream}` : `${plural(u.commits, "commit")} not on any remote`;
}

function item(repo: string, label: string, fix: string | null = null, tooltip?: string): ItemNode {
  return { type: "item", repo, label, fix, tooltip: tooltip ?? (fix ? `Copy (quoted for a POSIX shell: sh, bash, zsh): ${fix}` : undefined) };
}

/** kind → repo → item; kinds in the CLI's order, repos sorted by path, empty kinds left out. */
export function lostTree(json: LostJson, home = ""): KindNode[] {
  const show = (p: string) => (home ? tilde(p, home) : p);
  const kinds: [string, string, ItemNode[], string][] = [];
  const add = (kind: string, label: string, items: ItemNode[], unit: string) => kinds.push([kind, label, items, unit]);
  add("unpushed", "Unpushed", (json.unpushed ?? []).map((u) =>
    item(u.repo, `${u.detached ? "(detached HEAD)" : u.branch} · ${unpushedText(u)}`, u.fix)), "branch");
  add("merged_gone", "Upstream gone", (json.merged_gone ?? []).map((m) =>
    item(m.repo, `${m.branch} · ${m.commits ? `${plural(m.commits, "commit")} not on any remote`
      : "merged, safe to delete"}`)), "branch");
  add("stashes", "Stashes", (json.stashes ?? []).map((s) =>
    item(s.repo, `${s.ref} · ${s.date.slice(0, 10)} ${s.subject}`)), "stash");
  add("dirty", "Uncommitted changes", (json.dirty ?? []).map((d) =>
    item(d.repo, `${d.modified} modified, ${d.untracked} untracked`)), "repo");
  add("no_remote", "No remote", (json.no_remote ?? []).map((n) =>
    item(n.repo, `${plural(n.commits, "commit")}, never pushed anywhere`)), "repo");
  add("remote_gone", "Remote gone", (json.remote_gone ?? []).filter((r) => r.status !== "unknown").map((r) =>
    item(r.repo, `${r.remote} · ${r.status === "renamed" ? `renamed to ${r.renamed_to}` : `missing via ${r.via}`}`,
      r.fix, r.url)), "remote");
  add("duplicates", "Duplicate clones", (json.duplicates ?? []).flatMap((g) => g.repos.map((repo) => {
    const others = g.repos.filter((o) => o !== repo).map(show).join(", ");
    const state = g.diverged ? `diverged: ${replacePaths(g.detail ?? "", g.repos, show)}`
      : g.diverged === null ? (g.detail ?? "could not compare") : "same HEAD";
    return item(repo, `${g.key.startsWith("/") ? show(g.key) : g.key} · also ${others} · ${state}`);
  })), "clone");
  const nodes: KindNode[] = [];
  for (const [kind, label, items, unit] of kinds) {
    if (!items.length) {
      continue;
    }
    const byRepo = new Map<string, ItemNode[]>();
    for (const i of items) {
      byRepo.set(i.repo, [...(byRepo.get(i.repo) ?? []), i]);
    }
    const repos = [...byRepo.keys()].sort().map((repo): RepoNode =>
      ({ type: "repo", repo, label: show(repo), items: byRepo.get(repo) ?? [] }));
    nodes.push({ type: "kind", kind, label, repos,
      description: kind === "duplicates" ? plural(json.duplicates?.length ?? 0, "group") : plural(items.length, unit) });
  }
  return nodes;
}

function replacePaths(text: string, paths: string[], show: (p: string) => string): string {
  return [...paths].sort((a, b) => b.length - a.length).reduce((t, p) => t.split(p).join(show(p)), text);
}

/** "5 items in 3 repos" for the view description (a duplicate group counts once per clone); "" when nothing is lost. */
export function lostSummary(nodes: KindNode[]): string {
  const items = nodes.reduce((n, k) => n + k.repos.reduce((m, r) => m + r.items.length, 0), 0);
  const repos = new Set(nodes.flatMap((k) => k.repos.map((r) => r.repo))).size;
  return items ? `${plural(items, "item")} in ${plural(repos, "repo")}` : "";
}
