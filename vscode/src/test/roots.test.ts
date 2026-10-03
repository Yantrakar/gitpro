// Spec 02 §10 tests 15-16: scan roots as pure functions (no vscode).
import * as assert from "node:assert/strict";
import { test } from "node:test";
import type { Capabilities } from "../capabilities";
import { effectiveRoots, foldersOutside, isUnder, outsideMessage, rootsArgs, rootsLog, tomlRoots, tomlSnippet,
  viewScan } from "../rootsModel";

const HOME = "/home/me";
const caps = (has: boolean): Capabilities => ({ version: "0.2.0", has: () => has });

/** A Cli whose `scan --roots --json [DIR...]` answers like gitpro: DIRs win, else the TOML's roots. */
function fakeCli(has = true, fail = false) {
  const calls: string[][] = [];
  return {
    calls,
    caps: caps(has),
    async json<T>(args: string[]): Promise<T | undefined> {
      calls.push(args);
      if (fail) {
        return undefined;
      }
      const dirs = args.slice(3);
      return (dirs.length
        ? { roots: dirs, source: "args", exclude: ["**/vendor"], config: `${HOME}/.config/gitpro.toml`, missing: [] }
        : { roots: [HOME, "/mnt/d/work"], source: "config" }) as T;
    },
  };
}

test("effectiveRoots: the CLI's roots and source when no directories are set", async () => {
  const cli = fakeCli();
  assert.deepEqual(await effectiveRoots(cli, [], "gitpro.scanDirs", HOME),
    { roots: [HOME, "/mnt/d/work"], source: "config", exclude: [], config: null, missing: [] });
  assert.deepEqual(cli.calls, [["scan", "--roots", "--json"]]);
});

test("effectiveRoots: gitpro.scanDirs (passed in) wins over the TOML", async () => {
  const cli = fakeCli();
  const r = await effectiveRoots(cli, ["/srv/git"], "gitpro.scanDirs", HOME);
  assert.deepEqual(r?.roots, ["/srv/git"]);
  assert.equal(r?.source, "gitpro.scanDirs");
  assert.deepEqual(r?.exclude, ["**/vendor"]);
  assert.equal(r?.config, `${HOME}/.config/gitpro.toml`);
  assert.deepEqual(cli.calls, [rootsArgs(["/srv/git"])]);
  assert.equal((await effectiveRoots(cli, ["/x"], "session", HOME))?.source, "session");
});

test("effectiveRoots: an old gitpro scans the setting, else home; a failed run is undefined", async () => {
  const old = fakeCli(false);
  assert.deepEqual((await effectiveRoots(old, [], "gitpro.scanDirs", HOME))?.roots, [HOME]);
  assert.deepEqual((await effectiveRoots(old, ["~/src", "/x"], "gitpro.scanDirs", HOME))?.roots, [`${HOME}/src`, "/x"]);
  assert.deepEqual(old.calls, []);
  assert.equal(await effectiveRoots(fakeCli(true, true), [], "gitpro.scanDirs", HOME), undefined);
});

test("foldersOutside: prefix match on whole path segments", () => {
  assert.deepEqual(foldersOutside(["/home/me/s"], ["/home/me/src", "/home/me/s/x", "/home/me/s"]), ["/home/me/src"]);
  assert.deepEqual(foldersOutside([HOME, "/mnt/d/work"], ["/home/me/a", "/mnt/d/work/b", "/srv/c", "/mnt/d/worker"]),
    ["/srv/c", "/mnt/d/worker"]);
  assert.deepEqual(foldersOutside(["/"], ["/srv/c"]), []);
  assert.deepEqual(foldersOutside(["/home/me/"], ["/home/me/a/"]), []);
  assert.deepEqual(foldersOutside([], ["/a"]), ["/a"]);
  assert.equal(isUnder("/home/me", "/home/me"), true);
  assert.equal(isUnder("/home/meme", "/home/me"), false);
});

test("messages, log line and the TOML snippet", () => {
  assert.equal(outsideMessage(2), "2 workspace folders are outside the scan roots");
  assert.equal(outsideMessage(1), "1 workspace folder is outside the scan roots");
  assert.equal(rootsLog([HOME, "/mnt/d/work"], "config", HOME), "roots: ~, /mnt/d/work (config)");
  const snippet = tomlSnippet([HOME, `${HOME}/src`, "/mnt/d/\"q\""], HOME, `${HOME}/.config/gitpro.toml`);
  assert.match(snippet, /^# paste into ~\/\.config\/gitpro\.toml/);
  assert.ok(snippet.endsWith('[scan]\nroots = ["~", "~/src", "/mnt/d/\\"q\\""]\n'), snippet);
  assert.match(tomlSnippet(["/x"], HOME, null), /^# paste into ~\/\.config\/gitpro\.toml/);
});

test("session roots replace gitpro.scanDirs for the view scan", () => {
  assert.deepEqual(viewScan(["/a"], ["/b", "/c"]), { dirs: ["/b", "/c"], source: "session" });
  assert.deepEqual(viewScan(["/a"], undefined), { dirs: ["/a"], source: "gitpro.scanDirs" });
  assert.deepEqual(viewScan([], []), { dirs: [], source: undefined });
});

test("TOML roots escape what TOML forbids (U+007F) and keep the rest", () => {
  assert.equal(tomlRoots(["/del\x7fx", "/tab\tx", `${HOME}/Ünï code`], HOME), '["/del\\u007fx", "/tab\\tx", "~/Ünï code"]');
});
