import * as assert from "node:assert/strict";
import { test } from "node:test";
import { LostJson, lostSummary, lostTree } from "../lostTree";

const json: LostJson = {
  ok: false,
  unpushed: [
    { repo: "/h/work/tool", branch: "main", commits: 2, upstream: null, ahead: null, detached: false,
      fix: "git -C /h/work/tool push -u origin main" },
    { repo: "/h/src/app", branch: "feature/login", commits: 4, upstream: null, ahead: null, detached: false, fix: null },
    { repo: "/h/src/app", branch: "hotfix", commits: 1, upstream: "origin/hotfix", ahead: 1, detached: false, fix: null },
    { repo: "/h/src/app", branch: null, commits: 1, upstream: null, ahead: null, detached: true, fix: null },
  ],
  merged_gone: [{ repo: "/h/src/app", branch: "pr-212", commits: 0 }, { repo: "/h/src/app", branch: "spike", commits: 3 }],
  stashes: [{ repo: "/h/src/app", ref: "stash@{0}", date: "2026-03-01T10:00:00+00:00", subject: "WIP on main: fix" }],
  dirty: [{ repo: "/h/notes", modified: 3, untracked: 2 }],
  no_remote: [{ repo: "/h/scratch/parser", commits: 7 }],
  remote_gone: [
    { repo: "/h/src/tool", remote: "origin", url: "git@github.com:me/tool.git", status: "renamed", renamed_to: "me/tool-cli",
      detail: null, fix: "git -C /h/src/tool remote set-url origin git@github.com:me/tool-cli.git", via: "gh" },
    { repo: "/h/src/x", remote: "origin", url: "git@github.com:me/x.git", status: "unknown", renamed_to: null,
      detail: "no gh login", fix: null, via: null },
  ],
  duplicates: [{ key: "github.com/me/app", repos: ["/h/old/app", "/h/src/app"], diverged: true,
    detail: "/h/old/app has 2 commits not in /h/src/app" }],
  stale: [{ repo: "/h/ancient" }],
};

test("groups by kind in the CLI's order, leaving out empty kinds, stale and unknown remotes", () => {
  const nodes = lostTree(json, "/h");
  assert.deepEqual(nodes.map((n) => n.kind),
    ["unpushed", "merged_gone", "stashes", "dirty", "no_remote", "remote_gone", "duplicates"]);
  assert.deepEqual(nodes.map((n) => n.description),
    ["4 branches", "2 branches", "1 stash", "1 repo", "1 repo", "1 remote", "1 group"]);
  assert.deepEqual(nodes[5].repos.map((r) => r.repo), ["/h/src/tool"]);
  assert.deepEqual(lostTree({ ok: true }), []);
});

test("sorts repos by path and labels items like the CLI", () => {
  const [unpushed, merged] = lostTree(json, "/h");
  assert.deepEqual(unpushed.repos.map((r) => r.label), ["~/src/app", "~/work/tool"]);
  assert.deepEqual(unpushed.repos[0].items.map((i) => i.label), [
    "feature/login · 4 commits, no upstream",
    "hotfix · 1 ahead of origin/hotfix",
    "(detached HEAD) · 1 commit not on any branch",
  ]);
  assert.equal(unpushed.repos[1].items[0].label, "main · 2 commits, no upstream");
  assert.equal(unpushed.repos[1].items[0].fix, "git -C /h/work/tool push -u origin main");
  assert.equal(unpushed.repos[0].items[0].fix, null);
  assert.deepEqual(merged.repos[0].items.map((i) => i.label),
    ["pr-212 · merged, safe to delete", "spike · 3 commits not on any remote"]);
});

test("other kinds' labels; paths shown with ~ only when a home is given", () => {
  const nodes = lostTree(json, "/h");
  const label = (kind: string) => nodes.find((n) => n.kind === kind)!.repos[0].items[0].label;
  assert.equal(label("stashes"), "stash@{0} · 2026-03-01 WIP on main: fix");
  assert.equal(label("dirty"), "3 modified, 2 untracked");
  assert.equal(label("no_remote"), "7 commits, never pushed anywhere");
  assert.equal(label("remote_gone"), "origin · renamed to me/tool-cli");
  assert.equal(label("duplicates"), "github.com/me/app · also ~/src/app · diverged: ~/old/app has 2 commits not in ~/src/app");
  assert.equal(lostTree(json)[0].repos[0].label, "/h/src/app");
});

test("summary counts the tree's items and repos", () => {
  assert.equal(lostSummary(lostTree(json, "/h")), "12 items in 6 repos");
  assert.equal(lostSummary([]), "");
});
