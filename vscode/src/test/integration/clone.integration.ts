// Spec 07 §5: gitpro: Clone… against the real gitpro, cloning only from a local bare repo (never the network).
import * as assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import type { ClonePlan } from "../../clone";
import type { Api } from "../../extension";
import { env, withEnv, withUi } from "./helpers";

function git(args: string[]): void {
  execFileSync("git", args, { stdio: ["ignore", "ignore", "pipe"] });
}

/** A bare repo with one commit (README.md). */
function seeded(root: string): string {
  const work = path.join(root, "seed");
  const bare = path.join(root, "app.git");
  git(["init", "-q", work]);
  fs.writeFileSync(path.join(work, "README.md"), "# app\n");
  git(["-C", work, "add", "README.md"]);
  git(["-C", work, "-c", "user.name=Seed", "-c", "user.email=seed@x.test", "commit", "-q", "-m", "seed"]);
  git(["clone", "-q", "--bare", work, bare]);
  return bare;
}

async function clone(url: string, dir: string): Promise<ClonePlan | undefined> {
  return vscode.commands.executeCommand<ClonePlan | undefined>("gitpro.clone", url, dir);
}

export async function run(api: Api): Promise<void> {
  assert.ok(api.caps.has("clone"), `gitpro ${api.caps.version} has no 'clone'`);
  const root = path.join(path.dirname(env("GI_HOME")), "clone-test");
  fs.mkdirSync(root, { recursive: true });
  try {
    const bare = seeded(root);
    const target = path.join(root, "out", "app");

    // Declined: nothing is cloned, nothing reported.
    await withUi(api, {}, async (seen) => {
      assert.equal(await clone(bare, target), undefined);
      assert.deepEqual(seen.modal, [`Clone ${bare} into ${target} as the default identity?`]);
      assert.deepEqual([seen.info, seen.error], [[], []]);
      assert.ok(!fs.existsSync(target));
    });

    // Confirmed: the dry run's plan in the modal, then the clone and Open Folder.
    await withUi(api, { modal: "Clone" }, async (seen) => {
      const done = await clone(bare, target);
      assert.equal(done?.git_exit, 0);
      assert.equal(done?.dir, target);
      assert.ok(fs.existsSync(path.join(target, "README.md")));
      assert.deepEqual(seen.info, [`Cloned into ${target}`]);
      assert.deepEqual(seen.error, []);
    });

    // The target exists now: the dry run fails, with gitpro's message and no modal.
    await withUi(api, { modal: "Clone" }, async (seen) => {
      assert.equal(await clone(bare, target), undefined);
      assert.deepEqual(seen.modal, []);
      assert.equal(seen.error.length, 1);
      assert.match(seen.error[0], /already exists/);
    });

    // ssh needs a passphrase (a fake ssh on PATH refuses): the error names the terminal command.
    const bin = path.join(root, "bin");
    fs.mkdirSync(bin);
    const sshLog = path.join(root, "ssh.log");
    fs.writeFileSync(path.join(bin, "ssh"), `#!/bin/sh\nprintf '%s\\n' "$*" >> '${sshLog}'\n`
      + `echo "\${GI_FAKE_SSH_SAYS:-git@github.com: Permission denied (publickey).}" >&2\nexit 255\n`,
    { mode: 0o755 });
    const url = "git@github.com:corp-org/nothere.git";
    const sshTarget = path.join(root, "out", "nothere");
    const PATH = `${bin}${path.delimiter}${process.env.PATH ?? ""}`;
    await withEnv({ PATH }, () =>
      withUi(api, { modal: "Clone" }, async (seen) => {
        assert.equal(await clone(url, sshTarget), undefined);
        assert.deepEqual(seen.modal, [`Clone ${url} into ${sshTarget} as work-github?`]);
        assert.deepEqual(seen.error, ["Clone needs a terminal (ssh passphrase or credentials). Run in a terminal: "
          + `gitpro clone ${url} ${sshTarget}`]);
        assert.deepEqual(seen.info, []);
        assert.ok(!fs.existsSync(sshTarget), "the failed clone was left behind");
        // The fake ssh ran (not a real one), with the identity's key from its core.sshCommand.
        const key = new RegExp(`-i ${env("GI_HOME")}/\\.ssh/id_work .*github\\.com`);
        assert.match(fs.readFileSync(sshLog, "utf8"), key);
      }));
    // An unknown host key needs a terminal too (matched case-insensitively).
    await withEnv({ PATH, GI_FAKE_SSH_SAYS: "HOST KEY VERIFICATION FAILED." }, () =>
      withUi(api, { modal: "Clone" }, async (seen) => {
        assert.equal(await clone(url, sshTarget), undefined);
        assert.equal(seen.error.length, 1);
        assert.match(seen.error[0], /^Clone needs a terminal/);
      }));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}
