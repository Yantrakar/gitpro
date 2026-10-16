// Spec 13 §10 (integration): gitpro.devcontainerSetup against the real gitpro on throwaway repos, and the nudge:
// once per folder per session, and "Don't ask again" remembered. Every file this test adds is removed again.
import * as assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";
import { LINE, NUDGE, NUDGE_COMMAND, NudgeResult } from "../../devcontainer";
import type { Api } from "../../extension";
import { env, withUi } from "./helpers";

const COMMENTED = '{\n  // the image\n  "image": "x"\n}\n';

/** A throwaway repo with a staged .devcontainer/devcontainer.json holding `text`. */
function tempRepo(text: string): { dir: string; file: string } {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gitpro-dc-it-"));
  execFileSync("git", ["init", "-q", dir]);
  const file = addFile(dir, text);
  execFileSync("git", ["-C", dir, "add", "."]);
  return { dir, file };
}

function addFile(dir: string, text: string): string {
  fs.mkdirSync(path.join(dir, ".devcontainer"), { recursive: true });
  const file = path.join(dir, ".devcontainer", "devcontainer.json");
  fs.writeFileSync(file, text);
  return file;
}

async function nudge(dir: string): Promise<NudgeResult> {
  const r = await vscode.commands.executeCommand<NudgeResult[]>(NUDGE_COMMAND, dir);
  return r[0];
}

/** Runs fn with api.ui.info answering `answer` and recording each message (withUi's info always dismisses). */
async function answeringInfo<T>(api: Api, answer: string | undefined, fn: (seen: string[]) => Promise<T>): Promise<T> {
  const saved = api.ui.info;
  const seen: string[] = [];
  api.ui.info = async (msg: string) => (seen.push(msg), answer);
  try {
    return await fn(seen);
  } finally {
    api.ui.info = saved;
  }
}

async function setup(api: Api): Promise<void> {
  const ids = await vscode.commands.getCommands(true);
  assert.ok(ids.includes("gitpro.devcontainerSetup"));
  const a = tempRepo('{\n  "image": "x"\n}\n');
  const b = tempRepo(COMMENTED);
  try {
    // Tracked file: the question, then the team question, then the CLI writes (backup and undo logged).
    await withUi(api, { modal: "Write" }, async (seen) => {
      assert.equal(await vscode.commands.executeCommand<string>("gitpro.devcontainerSetup", a.dir), "written");
      assert.equal(seen.modal.length, 2, JSON.stringify(seen.modal));
      assert.ok(seen.modal[0].startsWith("Add initializeCommand to "), seen.modal[0]);
      assert.ok(seen.modal[0].endsWith("which is harmless)."), seen.modal[0]);
      assert.ok(seen.modal[1].startsWith("This file is shared with your team. Write anyway?"));
      assert.match(seen.info[0], /Undo: /);
    });
    assert.equal(JSON.parse(fs.readFileSync(a.file, "utf8")).initializeCommand, LINE);
    await withUi(api, {}, async (seen) => {
      assert.equal(await vscode.commands.executeCommand<string>("gitpro.devcontainerSetup", a.dir), "already");
      assert.equal(seen.modal.length, 0);
    });
    // Comments: only Copy is offered; the file is never touched.
    await withUi(api, { modal: "Copy" }, async (seen) => {
      assert.equal(await vscode.commands.executeCommand<string>("gitpro.devcontainerSetup", b.dir), "copied");
      assert.ok(seen.modal[0].includes("has comments"));
    });
    assert.ok((await vscode.env.clipboard.readText()).startsWith('"initializeCommand": "command true || exit 0'));
    assert.equal(fs.readFileSync(b.file, "utf8"), COMMENTED);
  } finally {
    fs.rmSync(a.dir, { recursive: true, force: true });
    fs.rmSync(b.dir, { recursive: true, force: true });
  }
}

async function nudges(api: Api): Promise<void> {
  const repoA = env("GI_TEST_REPO"); // identity work-github
  const repoB = env("GI_REPO_B"); // identity personal
  const plain = fs.mkdtempSync(path.join(os.tmpdir(), "gitpro-dc-it-"));
  const added = [addFile(repoA, '{"image": "x"}\n'), addFile(repoB, '{"image": "x"}\n')];
  try {
    assert.equal(await nudge(plain), "no-file");
    addFile(plain, JSON.stringify({ image: "x", initializeCommand: LINE }));
    assert.equal(await nudge(plain), "has-pin");

    // Dismissed: shown once, then not again this session.
    await answeringInfo(api, undefined, async (seen) => {
      assert.equal(await nudge(repoA), "shown");
      assert.deepEqual(seen, [NUDGE]);
      assert.equal(await nudge(repoA), "seen");
      assert.deepEqual(seen, [NUDGE]);
    });
    // Don't ask again: remembered in workspaceState (checked before the session's own list).
    await answeringInfo(api, "Don't ask again", async (seen) => {
      assert.equal(await nudge(repoB), "shown");
      assert.equal(await nudge(repoB), "off");
      assert.deepEqual(seen, [NUDGE]);
    });
    // The nudge never writes.
    assert.equal(fs.readFileSync(added[0], "utf8"), '{"image": "x"}\n');
  } finally {
    for (const f of added) {
      fs.rmSync(path.dirname(f), { recursive: true, force: true });
    }
    fs.rmSync(plain, { recursive: true, force: true });
  }
}

export async function run(api: Api): Promise<void> {
  await setup(api);
  await nudges(api);
}
