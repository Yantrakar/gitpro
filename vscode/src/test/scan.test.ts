import * as assert from "node:assert/strict";
import { test } from "node:test";
import { Choice, fixArgs, fixedMessage, KEPT, Override, plan, question, Report, scanArgs, stepText, steps, summary, suggestions,
  skipped, tree, walk } from "../scan";

const ss = { name: "ss", email: "ss@new.test" };

function over(short: string, now: { name: string; email: string }, after = ss, fixable = ["user.email"]): Override {
  return {
    path: `/h/src/${short}`, short, remote: `git@github.com:me/${short}.git`, owner: "github.com/me", now, after,
    settings: [{ key: "user.email", value: now.email, without: fixable.length ? after.email : null }], fixable,
  };
}

const old = { name: "Sandeep", email: "old@x.test" };
const own = { name: "Own", email: "own@x.test" };
const overrides = [
  over("same", ss),
  over("kept", own, own, []),
  over("a", old),
  over("b", old),
  over("other", ss, { name: "Abe", email: "abe@x.test" }),
];

test("tree groups by identity after the fix, then now; Kept and unchanged last", () => {
  const t = tree(overrides);
  assert.deepEqual(t.map((g) => [g.label, g.description]), [
    ["Abe <abe@x.test>", "1 repo"],
    ["ss <ss@new.test>", "3 repos"],
    [KEPT, "1 repo · nothing else sets an identity"],
  ]);
  assert.deepEqual(t[1].children.map((b) => [b.label, b.repos.map((r) => r.label)]), [
    ["from Sandeep <old@x.test>", ["a", "b"]],
    ["unchanged", ["same"]],
  ]);
  assert.equal(t[0].children[0].label, "from ss <ss@new.test>");
  assert.equal(t[2].children[0].label, "Own <own@x.test>");
});

test("repo leaves: owner, kept note, tooltip with every setting", () => {
  const t = tree(overrides);
  const a = t[1].children[0].repos[0];
  assert.deepEqual([a.description, a.fixable], ["github.com/me", true]);
  assert.equal(a.tooltip, "/h/src/a\nuser.email: old@x.test → ss@new.test");
  const kept = t[2].children[0].repos[0];
  assert.deepEqual([kept.description, kept.fixable], ["github.com/me · kept", false]);
  assert.match(kept.tooltip, /own@x\.test → kept/);
});

test("plan takes checked fixable repos only", () => {
  const t = tree(overrides);
  const all = plan(t, new Set());
  assert.deepEqual(all.paths, ["/h/src/other", "/h/src/a", "/h/src/b", "/h/src/same"]);
  assert.equal(all.settings, 4);
  assert.match(question(all), /^Remove 4 settings from 4 repos\? They'll use the identity they are listed under\.$/);
  const one = plan(t, new Set(["/h/src/other", "/h/src/b", "/h/src/same"]));
  assert.equal(question(one), "Remove 1 setting from 1 repo? They'll use ss <ss@new.test>.");
  assert.deepEqual(plan(t, new Set(all.paths)).paths, []);
});

test("review steps through fixable repos in tree order: back, skip, fix the rest, cancel", async () => {
  const rs = steps(tree(overrides));
  assert.deepEqual(rs.map((r) => r.label), ["other", "a", "b", "same"]);
  const answer = (cs: (Choice | undefined)[]) => {
    const asked: number[] = [];
    return { asked, choose: async (i: number) => (asked.push(i), cs.shift()) };
  };
  let fix = new Map<string, boolean>();
  let a = answer(["fix", "back", "skip", "fix", "fixRest"]);
  assert.equal(await walk(rs, fix, a.choose), true);
  assert.deepEqual(a.asked, [0, 1, 0, 1, 2]);
  assert.deepEqual([...fix], [["/h/src/other", false], ["/h/src/a", true], ["/h/src/b", true], ["/h/src/same", true]]);
  fix = new Map();
  a = answer(["back", "fix", "skipRest"]);
  assert.equal(await walk(rs, fix, a.choose), true);
  assert.deepEqual(a.asked, [0, 0, 1]);
  assert.deepEqual([...fix.values()], [true, false, false, false]);
  fix = new Map();
  assert.equal(await walk(rs, fix, answer(["skip", undefined]).choose), false);
  assert.deepEqual([...fix], [["/h/src/other", false]]);
  assert.deepEqual(stepText(rs[1], "/h"), { where: "~/src/a · github.com/me",
    fix: "Sandeep <old@x.test> → ss <ss@new.test>", removes: "removes user.email from its .git/config",
    skip: "keeps Sandeep <old@x.test>" });
  assert.deepEqual([stepText(rs[3], "/h").fix, stepText(rs[3], "/h").removes], ["already uses ss <ss@new.test>",
    "removes user.email from its .git/config: a duplicate, nothing changes"]);
});

test("CLI args", () => {
  assert.deepEqual(scanArgs([]), ["scan", "--json"]);
  assert.deepEqual(scanArgs(["/a", "/b"]), ["scan", "--json", "/a", "/b"]);
  assert.deepEqual(fixArgs(["/a", "/b c"]), ["scan", "--fix", "--yes", "--json", "--repo", "/a", "--repo", "/b c"]);
});

test("messages", () => {
  assert.equal(fixedMessage({ removed: [
    { path: "/a", key: "user.name", value: "x" }, { path: "/a", key: "user.email", value: "y" },
    { path: "/b", key: "user.email", value: "y" }], undo: "/u.sh" }), "Removed 3 settings from 2 repos");
  const r: Report = { roots: ["/h"], repos: 159, unreadable: [
    { path: "/h/x", reason: "with a broken .git link", error: "broken .git: points to missing ../.git/modules/x" },
    { path: "/h/y", reason: "owned by root", error: "owned by root: ..." },
    { path: "/h/z", reason: "owned by root", error: "owned by root: ..." }], overrides,
    suggestions: [{ id: "me", toml: "[[identity]]\nid = \"me\"" }], unmatched: ["/h/src/kept"] };
  assert.equal(summary(r, "/h"), "159 repos under ~ · 3 skipped: 2 owned by root, 1 with a broken .git link");
  assert.equal(skipped([]), "");
  const doc = suggestions(r, "/h");
  assert.match(doc, /^# paste the blocks you want into ~\/\.config\/gitpro\.toml, run gitpro apply, then Fix\n/);
  assert.match(doc, /\n\[\[identity\]\]\nid = "me"\n/);
  assert.match(doc, /# unmatched: ~\/src\/kept \(github\.com\/me\): no identity can match it; after fixing it uses Own <own@x\.test>\n$/);
});
