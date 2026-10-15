import * as assert from "node:assert/strict";
import { test } from "node:test";
import { COPY, isCrash, OPEN, reportArgs, reportIssue, ReportIo } from "../report";

const TITLE = "[bug] gitpro 0.3.4: wrong identity & more";
const BODY = "### Versions\n- gitpro 0.3.4\n- why: email-1 vs email-2 (100% 50%) #1 + a&b=c?d\n";
const URL_ = "https://github.com/Yantrakar/gitpro/issues/new?" +
  `title=${encodeURIComponent(TITLE)}&body=${encodeURIComponent(BODY)}`;
const OK = JSON.stringify({ ok: true, command: "report", kind: "bug", title: TITLE, body: BODY, url: URL_,
  truncated: false, sent: null });
const BLOCKED = JSON.stringify({ ok: false, command: "report", blocked: [{ kind: "email", section: "message" },
  { kind: "path", section: "why" }] });

interface Calls {
  argv: string[][];
  cwd: string[];
  opened: string[];
  copied: string[];
  previews: string[];
  errors: string[];
  modals: string[][];
}

function fake(stdout: string, pick: string | undefined, code = 0): { io: ReportIo; calls: Calls } {
  const calls: Calls = { argv: [], cwd: [], opened: [], copied: [], previews: [], errors: [], modals: [] };
  const io: ReportIo = {
    version: "0.3.3",
    dir: () => "/repo",
    run: async (args, cwd) => {
      calls.argv.push(args);
      calls.cwd.push(cwd);
      return { code, stdout, stderr: "" };
    },
    preview: async (b) => void calls.previews.push(b),
    modal: async (msg, ...items) => {
      calls.modals.push([msg, ...items]);
      return pick;
    },
    error: (m) => void calls.errors.push(m),
    info: () => undefined,
    openExternal: async (u) => void calls.opened.push(u),
    copy: async (t) => void calls.copied.push(t),
  };
  return { io, calls };
}

test("the URI given to openExternal decodes to the CLI's title and body (nothing encoded twice)", async () => {
  const { io, calls } = fake(OK, OPEN);
  assert.equal(await reportIssue(io), "opened");
  assert.equal(calls.opened.length, 1);
  const u = new URL(calls.opened[0]);
  assert.equal(u.origin + u.pathname, "https://github.com/Yantrakar/gitpro/issues/new");
  assert.equal(u.searchParams.get("title"), TITLE);
  assert.equal(u.searchParams.get("body"), BODY);
  assert.deepEqual(calls.previews, [BODY]);
  assert.deepEqual(calls.modals, [["Open the prefilled GitHub issue form?", OPEN, COPY]]);
  assert.deepEqual(calls.copied, []);
});

test("Copy Draft puts exactly the body on the clipboard and opens nothing", async () => {
  const { io, calls } = fake(OK, COPY);
  assert.equal(await reportIssue(io), "copied");
  assert.deepEqual(calls.copied, [BODY]);
  assert.deepEqual(calls.opened, []);
});

test("cancelling the modal opens and copies nothing", async () => {
  const { io, calls } = fake(OK, undefined);
  assert.equal(await reportIssue(io), "cancelled");
  assert.deepEqual([calls.opened, calls.copied], [[], []]);
});

test("a blocked result names kinds and sections, opens nothing, copies nothing, shows no preview", async () => {
  const { io, calls } = fake(BLOCKED, OPEN, 2);
  assert.equal(await reportIssue(io), "failed");
  assert.deepEqual([calls.opened, calls.copied, calls.previews, calls.modals], [[], [], [], []]);
  assert.equal(calls.errors.length, 1);
  assert.match(calls.errors[0], /email in message/);
  assert.match(calls.errors[0], /path in why/);
});

test("a CLI failure without JSON is an error, not a draft", async () => {
  const { io, calls } = fake("", OPEN, 2);
  assert.equal(await reportIssue(io), "failed");
  assert.equal(calls.errors.length, 1);
  assert.deepEqual([calls.opened, calls.copied, calls.previews], [[], [], []]);
});

test("the argv is exactly the spec's, in the workspace folder, and never has --online", async () => {
  const { io, calls } = fake(OK, COPY);
  await reportIssue(io);
  assert.deepEqual(calls.argv[0], ["report", "--json", "--print", "--client", "vscode-gitpro/0.3.3"]);
  assert.deepEqual(calls.cwd, ["/repo"]);
  assert.deepEqual(reportArgs("1.2.3", true), ["report", "--json", "--print", "--client", "vscode-gitpro/1.2.3", "--crash"]);
  for (const crash of [false, true]) {
    assert.ok(!reportArgs("1", crash).includes("--online"));
  }
});

test("the crash flow adds --crash", async () => {
  const { io, calls } = fake(OK, OPEN);
  await reportIssue(io, { crash: true });
  assert.deepEqual(calls.argv[0].slice(-1), ["--crash"]);
});

test("no workspace folder: an error, no CLI call", async () => {
  const { io, calls } = fake(OK, OPEN);
  io.dir = () => undefined;
  assert.equal(await reportIssue(io), "failed");
  assert.deepEqual(calls.argv, []);
  assert.equal(calls.errors.length, 1);
});

test("only the crash line offers Report an Issue", () => {
  const crash = "✗ internal error (KeyError): this is a gitpro bug\n→ draft a report with: gitpro report --crash\n";
  assert.ok(isCrash(crash));
  assert.ok(isCrash(`noise\n${crash}`));
  assert.ok(!isCrash("✗ no identity matches this repo\n"));
  assert.ok(!isCrash("✗ internal error (KeyError)\n"));
  assert.ok(!isCrash(""));
});
