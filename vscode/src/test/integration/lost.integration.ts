// Spec 03 §5: the Lost Work view against the real gitpro. A repo under the temp HOME (the default scan root, the
// test config has no [scan]) with an unpushed branch; refresh shows it, Copy Fix puts the CLI's command on the
// clipboard. Everything it creates is removed again, so later tests see the HOME they expect.
import * as assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import type { Api } from "../../extension";
import type { KindNode } from "../../lost";
import { env, withUi } from "./helpers";

function git(args: string[]): string {
  return execFileSync("git", ["-c", "user.name=Lost Test", "-c", "user.email=lost@test.invalid", "-c",
    "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", ...args],
    { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}

export async function run(api: Api): Promise<void> {
  const home = env("GI_HOME");
  assert.ok(api.caps.has("lost"), `gitpro ${api.caps.version} has no 'lost'`);
  const work = path.join(home, "lostwork");
  const bare = path.join(home, ".lost-remote.git"); // hidden: never found as a repo itself
  const repo = path.join(work, "app");
  try {
    git(["init", "-q", "--bare", bare]);
    git(["init", "-q", repo]);
    const top = fs.realpathSync(git(["-C", repo, "rev-parse", "--show-toplevel"]));
    assert.equal(top, fs.realpathSync(repo), "git resolved the test repo somewhere else");
    git(["-C", repo, "remote", "add", "origin", bare]);
    git(["-C", repo, "commit", "-q", "--allow-empty", "-m", "first"]);
    git(["-C", repo, "push", "-q", "-u", "origin", "HEAD"]);
    git(["-C", repo, "checkout", "-q", "-b", "feature"]);
    git(["-C", repo, "commit", "-q", "--allow-empty", "-m", "unpushed"]);

    await withUi(api, {}, async (seen) => {
      const nodes = await vscode.commands.executeCommand<KindNode[] | undefined>("gitpro.lostRefresh");
      assert.ok(nodes, `refresh failed: ${seen.error.join("; ")}`);
      assert.deepEqual(seen.error, []);
      const unpushed = nodes.find((n) => n.kind === "unpushed");
      assert.ok(unpushed, JSON.stringify(nodes));
      assert.deepEqual(unpushed.repos.map((r) => r.label), ["~/lostwork/app"]);
      const [item] = unpushed.repos[0].items;
      assert.equal(item.label, "feature · 1 commit, no upstream");
      assert.equal(item.fix, `git -C ${unpushed.repos[0].repo} push -u origin feature`);

      assert.equal(await vscode.commands.executeCommand("gitpro.lostCopyFix", unpushed.repos[0]), undefined);
      const copied = await vscode.commands.executeCommand<string>("gitpro.lostCopyFix", item);
      assert.equal(copied, item.fix);
      assert.equal(await vscode.env.clipboard.readText(), item.fix);
      assert.deepEqual(seen.info, ["Copied: git -C ~/lostwork/app push -u origin feature"]);
      assert.equal(await vscode.commands.executeCommand("gitpro.lostOpenRepo"), undefined);
    });

    // Pushed: nothing lost, the tree is empty (the welcome text says so).
    git(["-C", repo, "push", "-q", "-u", "origin", "feature"]);
    const after = await vscode.commands.executeCommand<KindNode[] | undefined>("gitpro.lostRefresh");
    assert.deepEqual(after, []);
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
    fs.rmSync(bare, { recursive: true, force: true });
  }
}
