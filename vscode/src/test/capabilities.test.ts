import * as assert from "node:assert/strict";
import { test } from "node:test";
import type { Result } from "../cli";
import { LiveCapabilities, loadCapabilities, missing, parseCapabilities, UPDATE } from "../capabilities";

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
