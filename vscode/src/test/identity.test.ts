import * as assert from "node:assert/strict";
import { test } from "node:test";
import { parseOrigin, status } from "../identity";

test("parseOrigin splits origin and value", () => {
  assert.deepEqual(parseOrigin("file:/home/me/.gitconfig-work\tme@corp.test\n"),
    { origin: "file:/home/me/.gitconfig-work", value: "me@corp.test" });
  assert.deepEqual(parseOrigin("file:/x\tvalue\twith tab\n").value, "value\twith tab");
  assert.deepEqual(parseOrigin(""), { origin: "", value: "" });
});

test("status shows the email and where it comes from", () => {
  const s = status({ value: "Me", origin: "file:/h/.gitconfig-work" },
    { value: "me@corp.test", origin: "file:/h/.gitconfig-work" }, ["origin git@github.com:corp/x.git"], false);
  assert.equal(s.text, "$(account) me@corp.test");
  assert.equal(s.warning, false);
  assert.match(s.tooltip, /\/h\/\.gitconfig-work/);
});

test("status warns when git would refuse to commit", () => {
  const none = { value: "", origin: "" };
  assert.equal(status(none, none, [], false).warning, true);
  assert.match(status(none, none, [], true).tooltip, /gitpro pin/);
  assert.doesNotMatch(status(none, none, [], false).tooltip, /pin/);
});
