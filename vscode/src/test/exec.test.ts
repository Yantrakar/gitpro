import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { after, test } from "node:test";
import { Child, exec, KILL_GRACE, killAll, liveCount, redactUrl, within } from "../exec";

const node = process.execPath;
const posix = process.platform !== "win32";

const dirs: string[] = [];

function tmp(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gitpro-exec-"));
  dirs.push(dir);
  return dir;
}

after(() => {
  for (const dir of dirs) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
  } catch {
    return false;
  }
  try { // a zombie not yet reaped counts as gone
    return fs.readFileSync(`/proc/${pid}/stat`, "utf8").split(")").pop()!.trim().split(" ")[0] !== "Z";
  } catch {
    return true;
  }
}

async function until(ok: () => boolean, ms = 5000): Promise<void> {
  const end = Date.now() + ms;
  while (!ok()) {
    assert.ok(Date.now() < end, "timed out waiting");
    await new Promise((r) => setTimeout(r, 20));
  }
}

test("output, exit code, env removal, missing cwd and program", async () => {
  process.env.GITPRO_EXEC_TEST = "set";
  const js = "console.log('out'); console.error('err:' + (process.env.GITPRO_EXEC_TEST ?? 'gone')); process.exit(3)";
  let spawned: Child | undefined;
  const r = await exec(node, ["-e", js], { env: { GITPRO_EXEC_TEST: undefined }, onSpawn: (c) => (spawned = c) });
  assert.deepEqual(r, { code: 3, stdout: "out\n", stderr: "err:gone\n" });
  assert.ok(spawned?.pid);
  assert.equal(await spawned!.exited, 3);
  delete process.env.GITPRO_EXEC_TEST;

  assert.equal((await exec(node, ["-e", ""], { cwd: path.join(tmp(), "nothere") })).code, "ENOCWD");
  let called = false;
  assert.equal((await exec("gitpro-no-such-program", [], { onSpawn: () => (called = true) })).code, "ENOENT");
  assert.equal(called, false);
});

test("timeout kills the child", async () => {
  const r = await exec(node, ["-e", "setTimeout(() => {}, 30000)"], { timeout: 200 });
  assert.equal(r.code, "TIMEOUT");
});

test("abort resolves at once; Child.exited waits for the child's own exit", async () => {
  const dir = tmp();
  const marker = path.join(dir, "cleaned");
  const ready = path.join(dir, "ready");
  // Like gitpro clone: on SIGTERM it cleans up for a while, then exits.
  const js = `process.on("SIGTERM", () => setTimeout(() => { require("fs").writeFileSync(${JSON.stringify(marker)}, "");`
    + ` process.exit(5); }, 400)); require("fs").writeFileSync(${JSON.stringify(ready)}, ""); setInterval(() => {}, 1000);`;
  const abort = new AbortController();
  let child: Child | undefined;
  const running = exec(node, ["-e", js], { timeout: 0, signal: abort.signal, onSpawn: (c) => (child = c) });
  await until(() => fs.existsSync(ready));
  abort.abort();
  const r = await running;
  assert.equal(r.code, "ABORT_ERR");
  assert.ok(!fs.existsSync(marker), "run() should not wait for the cleanup");
  assert.equal(await within(child!.exited, 5000), 5);
  assert.ok(fs.existsSync(marker), "exited resolved before the child finished");
});

test("abort signals the whole process group on POSIX (no orphaned grandchild)", { skip: !posix }, async () => {
  const dir = tmp();
  const pidfile = path.join(dir, "pid");
  const abort = new AbortController();
  let child: Child | undefined;
  // sh does not pass SIGTERM on to its background job: only a group kill reaches the sleep.
  const running = exec("sh", ["-c", `sleep 60 & echo $! > '${pidfile}'; wait`], { timeout: 0, signal: abort.signal,
    onSpawn: (c) => (child = c) });
  await until(() => fs.existsSync(pidfile) && fs.readFileSync(pidfile, "utf8").trim() !== "");
  const grandchild = Number(fs.readFileSync(pidfile, "utf8"));
  abort.abort();
  assert.equal((await running).code, "ABORT_ERR");
  assert.equal(await within(child!.exited, 5000), "SIGTERM");
  try {
    await until(() => !alive(grandchild));
  } finally {
    if (alive(grandchild)) {
      process.kill(grandchild, "SIGKILL");
    }
  }
});

test("abort escalates after its grace period without delaying ABORT_ERR", { skip: !posix }, async () => {
  const dir = tmp();
  const ready = path.join(dir, "ready");
  const abort = new AbortController();
  let child: Child | undefined;
  const running = exec(node, ["-e", `process.on("SIGTERM", () => {});`
    + ` require("fs").writeFileSync(${JSON.stringify(ready)}, ""); setInterval(() => {}, 1000);`],
  { timeout: 1000, signal: abort.signal, onSpawn: (c) => (child = c) });
  try {
    await until(() => fs.existsSync(ready));
    abort.abort();
    assert.equal((await within(running, 1000))?.code, "ABORT_ERR");
    assert.ok(alive(child!.pid!), "abort must leave time for cleanup");
    assert.equal(await within(child!.exited, KILL_GRACE + 5000), "SIGKILL");
    await until(() => liveCount() === 0);
  } finally {
    if (child?.pid && alive(child.pid)) {
      process.kill(child.pid, "SIGKILL");
      await child.exited;
    }
  }
});

test("abort kills an ignoring grandchild even after the leader exits", { skip: !posix }, async () => {
  const dir = tmp();
  const pidfile = path.join(dir, "pid");
  const worker = `process.on("SIGTERM", () => {}); require("fs").writeFileSync(${JSON.stringify(pidfile)},`
    + ` String(process.pid)); setInterval(() => {}, 1000);`;
  const leader = `require("child_process").spawn(process.execPath, ["-e", ${JSON.stringify(worker)}],`
    + ` {stdio: "inherit"}); setInterval(() => {}, 1000);`;
  const abort = new AbortController();
  let child: Child | undefined;
  let grandchild: number | undefined;
  const running = exec(node, ["-e", leader], { timeout: 0, signal: abort.signal, onSpawn: (c) => (child = c) });
  try {
    await until(() => fs.existsSync(pidfile) && fs.readFileSync(pidfile, "utf8").trim() !== "");
    grandchild = Number(fs.readFileSync(pidfile, "utf8"));
    abort.abort();
    assert.equal((await within(running, 1000))?.code, "ABORT_ERR");
    assert.equal(await within(child!.exited, 5000), "SIGTERM");
    await until(() => !alive(grandchild!), KILL_GRACE + 5000);
    await until(() => liveCount() === 0);
  } finally {
    if (child?.pid) {
      try { process.kill(-child.pid, "SIGKILL"); } catch { /* already gone */ }
      await child.exited;
    }
    if (grandchild && alive(grandchild)) {
      process.kill(grandchild, "SIGKILL");
    }
  }
});

test("within gives up after ms", async () => {
  assert.equal(await within(new Promise<number>(() => {}), 50), undefined);
  assert.equal(await within(Promise.resolve(1), 50), 1);
});

test("an already aborted signal starts nothing", async () => {
  const abort = new AbortController();
  abort.abort();
  let called = false;
  const r = await exec(node, ["-e", ""], { signal: abort.signal, onSpawn: () => (called = true) });
  assert.equal(r.code, "ABORT_ERR");
  assert.equal(called, false);
});

test("redactUrl hides URL passwords and http tokens, like resolve.redact_url", () => {
  assert.equal(redactUrl("https://user:ghp_xxx@github.com/o/r.git"), "https://user:***@github.com/o/r.git");
  assert.equal(redactUrl("https://ghp_xxx@github.com/o/r.git"), "https://***@github.com/o/r.git");
  assert.equal(redactUrl("ssh://git:pw@host/o/r"), "ssh://git:***@host/o/r");
  for (const same of ["ssh://git@host/o/r", "git@github.com:o/r.git", "--json", "/a/b@c", "https://github.com/o/r"]) {
    assert.equal(redactUrl(same), same);
  }
});

test("redactUrl masks secret query values; the same cases as tests/test_mcp.py QUERY_CASES", () => {
  const cases: [string, string][] = [
    ["https://h/o/r.git?access_token=abc&x=1#frag", "https://h/o/r.git?access_token=***&x=1#frag"],
    ["https://gitlab.com/o/r.git?private_token=glpat-1&ref=main", "https://gitlab.com/o/r.git?private_token=***&ref=main"],
    ["https://h/a?x=1&Token=t1;sig=s2", "https://h/a?x=1&Token=***;sig=***"],
    ["https://h/a?X-Amz-Signature=ab&X-Amz-Credential=cd&a=b", "https://h/a?X-Amz-Signature=***&X-Amz-Credential=***&a=b"],
    ["https://u:p@h/a?auth=z", "https://u:***@h/a?auth=***"],
    ["https://h/a?password=&key=v", "https://h/a?password=***&key=***"],
    ["https://h/a?tokenish=1&mykey=2&ref=token", "https://h/a?tokenish=1&mykey=2&ref=token"],
  ];
  for (const [url, shown] of cases) {
    assert.equal(redactUrl(url), shown, url);
    assert.equal(redactUrl(url), shown, `${url} (again: the global regex keeps no state)`);
  }
});

test("timeout ends a run whose background job holds the output open", { skip: !posix }, async () => {
  // sh exits at once, but `sleep 30` keeps stdout open: without a group kill 'close' would wait 30 s.
  const t = Date.now();
  let child: Child | undefined;
  const r = await exec("sh", ["-c", "sleep 30 & echo hi"], { timeout: 300, onSpawn: (c) => (child = c) });
  assert.equal(r.code, "TIMEOUT");
  assert.equal(r.stdout, "hi\n");
  assert.equal(await child!.exited, 0);
  assert.ok(Date.now() - t < KILL_GRACE, `took ${Date.now() - t} ms`);
  assert.equal(liveCount(), 0);
});

test("timeout SIGKILLs a child that ignores SIGTERM after the grace period", { skip: !posix }, async () => {
  const dir = tmp();
  const ready = path.join(dir, "ready");
  const t = Date.now();
  let child: Child | undefined;
  // The trap is inherited by sleep: the whole group ignores SIGTERM.
  const r = await exec("sh", ["-c", `trap '' TERM; touch '${ready}'; sleep 30; echo late`],
    { timeout: 300, onSpawn: (c) => (child = c) });
  const took = Date.now() - t;
  assert.equal(r.code, "TIMEOUT");
  assert.equal(r.stdout, "");
  assert.ok(fs.existsSync(ready));
  assert.ok(took >= KILL_GRACE && took < KILL_GRACE + 10_000, `took ${took} ms`);
  assert.equal(await within(child!.exited, 5000), "SIGKILL");
});

test("maxBuffer counts stdout and stderr separately, like execFile", async () => {
  const mb = 1024 * 1024;
  // 40 MB on each stream: 80 MB together is over the 64 MB limit, but neither stream is.
  const both = `const b = Buffer.alloc(${mb}, 97); for (let n = 0; n < 40; n++) { process.stdout.write(b); process.stderr.write(b); }`;
  const r = await exec(node, ["-e", both], { timeout: 60_000 });
  assert.equal(r.code, 0);
  assert.equal(r.stdout.length, 40 * mb);
  assert.equal(r.stderr.length, 40 * mb);

  const over = `const b = Buffer.alloc(${mb}, 97); for (let n = 0; n < 70; n++) { process.stdout.write(b); }`
    + " setInterval(() => {}, 1000);";
  const o = await exec(node, ["-e", over], { timeout: 60_000 });
  assert.equal(o.code, "ERR_CHILD_PROCESS_STDIO_MAXBUFFER");
  assert.equal(o.stdout.length, 64 * mb);
});

test("onSpawn throwing kills the child and resolves with an error", async () => {
  let pid: number | undefined;
  const r = await exec(node, ["-e", "setTimeout(() => {}, 30000)"], { timeout: 0,
    onSpawn: (c) => {
      pid = c.pid;
      throw new Error("boom");
    } });
  assert.equal(r.code, "EONSPAWN");
  assert.match(r.stderr, /onSpawn failed: boom/);
  await until(() => !alive(pid!));
});

test("killAll ends every live child's process group", { skip: !posix }, async () => {
  const dir = tmp();
  const pidfile = path.join(dir, "pid");
  let child: Child | undefined;
  const running = exec("sh", ["-c", `sleep 60 & echo $! > '${pidfile}'; wait`], { timeout: 0,
    onSpawn: (c) => (child = c) });
  await until(() => fs.existsSync(pidfile) && fs.readFileSync(pidfile, "utf8").trim() !== "");
  const grandchild = Number(fs.readFileSync(pidfile, "utf8"));
  assert.equal(liveCount(), 1);
  await killAll(2000);
  try {
    assert.equal(await within(child!.exited, 5000), "SIGTERM");
    await running;
    await until(() => !alive(grandchild));
    assert.equal(liveCount(), 0);
  } finally {
    if (alive(grandchild)) {
      process.kill(grandchild, "SIGKILL");
    }
  }
});
