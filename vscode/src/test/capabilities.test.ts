import * as assert from "node:assert/strict";
import { test } from "node:test";
import type { Result } from "../cli";
import { LiveCapabilities, loadCapabilities, lostBody, lostCall, missing, parseCapabilities, settingsCall, unpinCall,
  UPDATE } from "../capabilities";

test("parseCapabilities reads --version --json; an old CLI's text is null", () => {
  const c = parseCapabilities('{"version":"0.2.0","capabilities":["why","lost"]}');
  assert.ok(c);
  assert.equal(c.version, "0.2.0");
  assert.equal(c.has("why"), true);
  assert.equal(c.has("lost"), true);
  assert.equal(c.has("clone"), false);
  assert.equal(parseCapabilities("gitpro 0.1.0"), null);
  assert.equal(parseCapabilities('{"version":"0.2.0"}'), null);
  assert.equal(parseCapabilities("null"), null);
});

function fake(r: Result | undefined) {
  return { run: async () => r };
}

test("loadCapabilities: JSON, old text, failure", async () => {
  const json = await loadCapabilities(fake({ code: 0, stdout: '{"ok":true,"version":"0.2.0.dev0","capabilities":["why"]}',
    stderr: "" }));
  assert.equal(json.version, "0.2.0.dev0");
  assert.equal(json.has("why"), true);

  const old = await loadCapabilities(fake({ code: 0, stdout: "gitpro 0.1.0\n", stderr: "" }));
  assert.equal(old.version, "0.1.0");
  assert.equal(old.has("why"), false);

  for (const r of [undefined, { code: "ENOENT", stdout: "", stderr: "" }, { code: 2, stdout: "", stderr: "✗ x" }]) {
    const none = await loadCapabilities(fake(r));
    assert.equal(none.version, "?");
    assert.equal(none.has("why"), false);
  }
});

test("missing names the version, the capability and the update line", () => {
  const c = parseCapabilities('{"version":"0.2.0","capabilities":[]}')!;
  assert.equal(missing(c, "clone"), `gitpro 0.2.0 has no 'clone'; update: ${UPDATE}`);
  assert.equal(UPDATE, "pipx install --force gitpro");
});

test("LiveCapabilities answers from whatever was set last", () => {
  const live = new LiveCapabilities();
  assert.equal(live.version, "?");
  assert.equal(live.has("why"), false);
  live.set(parseCapabilities('{"version":"1.0","capabilities":["why"]}')!);
  assert.equal(live.version, "1.0");
  assert.equal(live.has("why"), true);
});

const has = (...caps: string[]) => ({ has: (c: string) => caps.includes(c) });

test("settingsCall: why --json with why.commit, else which --json", () => {
  assert.deepEqual(settingsCall(has("why", "why.commit"), "/r"), { argv: ["why", "--json", "/r"], accept: [0, 1, 2] });
  assert.deepEqual(settingsCall(has("why"), "/r"), { argv: ["which", "--json", "/r"], accept: [0] });
});

test("lostCall and lostBody: scan --lost reads its lost member, else lost --offline reads the whole output", () => {
  assert.deepEqual(lostCall(has("scan.lost", "lost")), ["scan", "--lost", "--json"]);
  assert.deepEqual(lostCall(has("lost")), ["lost", "--json", "--offline"]);
  const body = { ok: true, unpushed: [] };
  assert.deepEqual(lostBody(has("scan.lost"), { ok: true, command: "scan", lost: body }), body);
  assert.equal(lostBody(has("scan.lost"), { ok: true, command: "scan" }), undefined);
  assert.equal(lostBody(has("scan.lost"), null), undefined);
  assert.deepEqual(lostBody(has("lost"), body), body);
});

test("unpinCall: pin --remove -y with pin.remove, else unpin (-y with dry-run-defaults.v1)", () => {
  assert.deepEqual(unpinCall(has("pin.remove", "dry-run-defaults.v1"), "/r"), ["pin", "--remove", "-y", "/r"]);
  assert.deepEqual(unpinCall(has("dry-run-defaults.v1"), "/r"), ["unpin", "-y", "/r"]);
  assert.deepEqual(unpinCall(has(), "/r"), ["unpin", "/r"]);
});
