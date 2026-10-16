// Spec 13 §5: proposeLine mirrors gitpro's devcontainer.propose (tests/test_devcontainer.py has the same cases).
// The module loads without vscode: its vscode and ./cli imports are type-only until registerDevcontainer runs.
import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { editRefusal, fileHasPin, hasPin, inspectText, LINE, locate, MERGED, NOTE, Proposal, proposeLine, question, snippet,
  stripJsonc, withInitializeCommand } from "../devcontainer";

/** [existing initializeCommand, what gitpro proposes]. */
const CASES: [unknown, unknown][] = [
  [undefined, LINE],
  [null, LINE],
  ["", LINE],
  [[], LINE],
  ["npm ci", { gitpro: LINE, npm: "npm ci" }],
  ["gitpro-helper x", { gitpro: LINE, "gitpro-helper": "gitpro-helper x" }],
  ["gitpro status", { gitpro: LINE, cmd: "gitpro status" }],
  [["sh", "-c", "make init"], { gitpro: LINE, cmd: ["sh", "-c", "make init"] }],
  [{ deps: "npm ci" }, { gitpro: LINE, deps: "npm ci" }],
  [{ gitpro: "gitpro doctor" }, { "gitpro-pin": LINE, gitpro: "gitpro doctor" }],
];

/** Values that already run gitpro pin come back unchanged. */
const PINNED: unknown[] = [
  LINE,
  "gitpro pin -y ${localWorkspaceFolder}",
  ["gitpro", "pin", "-y", "${localWorkspaceFolder}"],
  { a: "npm ci", b: "gitpro  pin -y ." },
];

test("proposeLine: each existing form becomes the object form, the old command kept verbatim", () => {
  for (const [existing, want] of CASES) {
    assert.deepEqual(proposeLine(existing), want, JSON.stringify(existing));
  }
});

test("proposeLine: an object keeps its keys after ours", () => {
  assert.deepEqual(Object.keys(proposeLine({ b: "x", a: "y" }) as object), ["gitpro", "b", "a"]);
});

test("proposeLine: already pinned is left alone", () => {
  for (const v of PINNED) {
    assert.equal(hasPin(v), true, JSON.stringify(v));
    assert.equal(proposeLine(v), v);
  }
  assert.equal(hasPin("gitpro pinned-thing"), false);
  assert.equal(hasPin(42), false);
});

test("proposeLine: upgrade the old generated preview-only pin command", () => {
  const old = LINE.replace('gitpro pin -y "$dir"', 'gitpro pin "$dir"');
  assert.equal(hasPin(old), false);
  assert.equal(proposeLine(old), LINE);
});

test("proposeLine: a number, or an object whose gitpro and gitpro-pin keys are both taken, is refused", () => {
  assert.throws(() => proposeLine(42), /not a string, array or object/);
  assert.throws(() => proposeLine({ gitpro: "a", "gitpro-pin": "b" }), /already has keys "gitpro" and "gitpro-pin"/);
});

test("LINE: one string whose first line ends cmd.exe, the workspace path read as data", () => {
  const lines = LINE.split("\n");
  assert.equal(lines[0], "command true || exit 0");
  assert.ok(lines.includes("${localWorkspaceFolder}"));
  assert.equal(LINE.split("${").length, 2);
  // pin and exit sit on the read line, so a newline in the path can never start a command
  assert.equal(lines[3], "IFS= read -r dir <<'GITPRO_PATH'; gitpro pin -y \"$dir\" || true; exit 0");
  assert.deepEqual(lines.slice(4), ["${localWorkspaceFolder}", "GITPRO_PATH"]);
  assert.match(NOTE, /'command' is not recognized/);
});

test("snippet and question: the key as JSON, and the host note last", () => {
  assert.deepEqual(JSON.parse(`{${snippet(LINE)}}`), { initializeCommand: LINE });
  const p: Proposal = { ok: true, file: "/home/me/app/.devcontainer/devcontainer.json", tracked: true,
    has_pin: false, existing_command: null, proposed: LINE, has_comments: false, written: false };
  const q = question(p, "/home/me");
  assert.ok(q.startsWith("Add initializeCommand to ~/app/.devcontainer/devcontainer.json?\n"), q);
  assert.ok(q.includes("shared with your team"));
  assert.ok(q.endsWith(NOTE));
  assert.ok(question({ ...p, has_comments: true }, "/home/me").includes("has comments"));
  assert.ok(!q.includes(MERGED));
  assert.ok(question({ ...p, merged: true }, "/home/me").includes(MERGED));
});

test("locate and fileHasPin: the two places, in order; a file with gitpro pin is not nudged", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gitpro-dc-"));
  try {
    assert.equal(locate(dir), undefined);
    fs.writeFileSync(path.join(dir, ".devcontainer.json"), '{"image": "x"}');
    assert.equal(locate(dir), path.join(dir, ".devcontainer.json"));
    fs.mkdirSync(path.join(dir, ".devcontainer"));
    const file = path.join(dir, ".devcontainer", "devcontainer.json");
    fs.writeFileSync(file, '{"image": "x"}');
    assert.equal(locate(dir), file);
    assert.equal(fileHasPin(file), false);
    fs.writeFileSync(file, JSON.stringify({ image: "x", initializeCommand: LINE }));
    assert.equal(fileHasPin(file), true);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("stripJsonc: comments and trailing commas are found, strings are left alone", () => {
  assert.deepEqual(stripJsonc('{"a": "x // y", "b": 1}'), { text: '{"a": "x // y", "b": 1}', jsonc: false });
  assert.equal(stripJsonc('{"a": 1, // note\n "b": 2}').jsonc, true);
  assert.equal(stripJsonc('{"a": 1, /* note */ "b": 2}').jsonc, true);
  assert.equal(stripJsonc('{"a": [1, 2,], }').jsonc, true);
  assert.deepEqual(JSON.parse(stripJsonc('{"a": [1, 2,], // x\n}').text), { a: [1, 2] });
  assert.deepEqual(JSON.parse(stripJsonc('{"a": "q\\" // not a comment"}').text), { a: 'q" // not a comment' });
});

test("inspectText: absent key, existing command, already pinned, comments, not JSON", () => {
  const f = "/r/.devcontainer.json";
  const none = inspectText(f, '{"image": "x"}');
  assert.deepEqual([none.ok, none.has_pin, none.has_comments, none.proposed, none.existing_command],
    [true, false, false, LINE, null]);
  const merged = inspectText(f, '{"initializeCommand": "npm ci"}');
  assert.deepEqual(merged.proposed, { gitpro: LINE, npm: "npm ci" });
  assert.equal(merged.merged, true);
  assert.equal(inspectText(f, JSON.stringify({ initializeCommand: LINE })).has_pin, true);
  const jsonc = inspectText(f, '{\n  // base image\n  "image": "x"\n}');
  assert.deepEqual([jsonc.ok, jsonc.has_comments, jsonc.proposed], [true, true, LINE]);
  const bad = inspectText(f, "not json");
  assert.deepEqual([bad.ok, bad.has_comments], [false, true]);
  assert.match(bad.error ?? "", /cannot be parsed/);
  assert.match(inspectText(f, "[1]").error ?? "", /not an object/);
  assert.match(inspectText(f, '{"initializeCommand": {"gitpro": "a", "gitpro-pin": "b"}}').error ?? "", /already has keys/);
  assert.match(question({ ...jsonc, tracked: false }), /has comments/);
  assert.match(question(bad), /cannot be parsed/);
});

test("withInitializeCommand: a new key is added and every other byte stays", () => {
  const text = '{\n    "image": "x",\n    "features": {"a": [1,2]}\n}\n';
  const out = withInitializeCommand(text, LINE);
  assert.ok(out.startsWith('{\n    "image": "x",\n    "features": {"a": [1,2]},\n    "initializeCommand": "'), out);
  assert.ok(out.endsWith('"\n}\n'));
  assert.deepEqual(JSON.parse(out), { image: "x", features: { a: [1, 2] }, initializeCommand: LINE });
  assert.equal(JSON.parse(out).initializeCommand, LINE);
  // an empty object, tabs, no trailing newline
  assert.deepEqual(JSON.parse(withInitializeCommand("{}", LINE)), { initializeCommand: LINE });
  assert.ok(withInitializeCommand('{\n\t"a": 1\n}', LINE).includes('\n\t"initializeCommand"'));
  // an object value is indented under the key
  const obj = { gitpro: LINE, npm: "npm ci" };
  assert.deepEqual(JSON.parse(withInitializeCommand('{"a": 1}', obj)).initializeCommand, obj);
});

test("withInitializeCommand: an existing key is replaced, the other keys and their order kept", () => {
  const text = '{\n  "a": 1,\n  "initializeCommand": "npm ci",\n  "b": [2]\n}\n';
  const out = withInitializeCommand(text, proposeLine("npm ci"));
  const parsed = JSON.parse(out);
  assert.deepEqual(Object.keys(parsed), ["a", "initializeCommand", "b"]);
  assert.deepEqual(parsed.initializeCommand, { gitpro: LINE, npm: "npm ci" });
  assert.deepEqual([parsed.a, parsed.b], [1, [2]]);
  assert.ok(out.endsWith("}\n"));
});

test("editRefusal: a normal file may be written, a symlink out of the repo may not", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gitpro-dc-"));
  try {
    const repo = path.join(dir, "repo");
    fs.mkdirSync(repo);
    const file = path.join(repo, ".devcontainer.json");
    fs.writeFileSync(file, "{}");
    assert.equal(editRefusal(file, repo), undefined);
    const outside = path.join(dir, "shared.json");
    fs.writeFileSync(outside, "{}");
    fs.rmSync(file);
    fs.symlinkSync(outside, file);
    assert.match(editRefusal(file, repo) ?? "", /outside the repo/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
