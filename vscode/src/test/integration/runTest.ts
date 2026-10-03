// Starts a real VS Code with the extension on throwaway repos and a throwaway HOME. Needs a display (CI: xvfb-run).
// Every *.integration.js next to this file runs, in name order (index.ts); env GI_* tells them where things are.
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { runTests } from "@vscode/test-electron";

const CONFIG = `[default]
name = "Default Me"
email = "me@default.test"

[[identity]]
id = "work-github"
name = "Work Me"
email = "me@corp.test"
host = "github.com"
owners = ["corp-org"]
ssh_key = "~/.ssh/id_work"

[[identity]]
id = "personal"
name = "Personal Me"
email = "me@personal.test"
host = "github.com"
owners = ["me"]
ssh_key = "~/.ssh/id_personal"
`;

function script(file: string, body: string): string {
  fs.writeFileSync(file, `#!/bin/sh\n${body}`, { mode: 0o755 });
  return file;
}

function sq(s: string): string {
  return `'${s.replace(/'/g, "'\\''")}'`;
}

/** Aborts unless git sees `repo` as its own repository inside the temp dir (not, say, a GIT_DIR from a hook). */
function guard(repo: string, tmp: string): void {
  const dir = execFileSync("git", ["-C", repo, "rev-parse", "--absolute-git-dir"], { encoding: "utf8" }).trim();
  const real = fs.realpathSync(dir);
  if (!real.startsWith(tmp + path.sep)) {
    throw new Error(`refusing to run: git resolves ${repo} to ${real}, outside ${tmp}`);
  }
}

async function main(): Promise<void> {
  delete process.env.ELECTRON_RUN_AS_NODE; // set in VS Code terminals; would start VS Code as plain node
  // Nothing from the real setup may leak in. A git hook in a linked worktree exports an absolute GIT_DIR (also
  // GIT_INDEX_FILE, GIT_CONFIG_PARAMETERS, ...): every git below, and in VS Code, would then act on that repo.
  // runTests starts VS Code with process.env plus extensionTestsEnv, so cleaning process.env cleans both.
  for (const k of Object.keys(process.env)) {
    if (k.startsWith("GIT_")) {
      delete process.env[k];
    }
  }
  for (const k of ["GITPRO_CONFIG", "XDG_CONFIG_HOME", "XDG_STATE_HOME"]) {
    delete process.env[k];
  }
  process.env.GIT_CONFIG_NOSYSTEM = "1";
  let tmp: string | undefined;
  try {
    tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "gi-ext-")));
    const gitpro = path.resolve(process.env.GITPRO ?? path.resolve(__dirname, "../../../../.venv/bin/gitpro"));
    const home = path.join(tmp, "home");
    const state = path.join(tmp, "state"); // where fixes put their undo scripts
    const config = path.join(home, ".config", "gitpro.toml");
    fs.mkdirSync(path.dirname(config), { recursive: true });
    fs.writeFileSync(config, CONFIG, { mode: 0o600 });
    const env: NodeJS.ProcessEnv = { ...process.env, HOME: home, XDG_STATE_HOME: state };
    const git = (args: string[]) => execFileSync("git", args, { env });

    // Two workspace folders: a work repo (corp-org → work-github) and a personal one (me → personal).
    const repoA = path.join(tmp, "repo-a");
    const repoB = path.join(tmp, "repo-b");
    for (const [repo, url] of [[repoA, "git@github.com:corp-org/app.git"], [repoB, "git@github.com:me/b.git"]]) {
      git(["init", "-q", repo]);
      guard(repo, tmp);
      git(["-C", repo, "remote", "add", "origin", url]);
      fs.writeFileSync(path.join(repo, "README.md"), `# ${path.basename(repo)}\n`);
    }
    const workspace = path.join(tmp, "test.code-workspace");
    fs.writeFileSync(workspace, JSON.stringify({ folders: [{ path: repoA }, { path: repoB }],
      settings: { "gitpro.path": gitpro } }, null, 2));

    // For the Repo Overrides view: a repo whose own user.email hides the global one (GI_GLOBAL, set by that test).
    const global = path.join(tmp, "gitconfig");
    fs.writeFileSync(global, "[user]\n\tname = Global Me\n\temail = me@global.test\n");
    const scanned = path.join(tmp, "scan", "app");
    git(["init", "-q", scanned]);
    guard(scanned, tmp);
    git(["-C", scanned, "remote", "add", "origin", "git@github.com:x/y.git"]);
    git(["-C", scanned, "config", "user.email", "old@x.test"]);

    execFileSync(gitpro, ["apply", "--yes"], { env, stdio: ["ignore", "ignore", "inherit"] });

    // Wrappers: an "old" gitpro whose --version --json lists no capabilities; a git that logs its argv.
    const bin = path.join(tmp, "bin");
    fs.mkdirSync(bin);
    const old = script(path.join(tmp, "gitpro-old"), `if [ "$1" = --version ]; then
  echo '{"ok": true, "command": "version", "version": "0.1.9", "capabilities": []}'
  exit 0
fi
exec ${sq(gitpro)} "$@"
`);
    const realGit = execFileSync("sh", ["-c", "command -v git"], { encoding: "utf8" }).trim();
    const gitLog = path.join(tmp, "git.log");
    script(path.join(bin, "git"), `printf '%s\\n' "$*" >> ${sq(gitLog)}\nexec ${sq(realGit)} "$@"\n`);

    await runTests({
      extensionDevelopmentPath: path.resolve(__dirname, "../../.."),
      extensionTestsPath: path.resolve(__dirname, "index"),
      launchArgs: [workspace, "--disable-extensions", "--disable-workspace-trust"],
      // Merged over the (cleaned) process.env for the whole VS Code process; profile dirs come from runTests' args.
      extensionTestsEnv: {
        HOME: home,
        GIT_CONFIG_NOSYSTEM: "1",
        XDG_STATE_HOME: state,
        GI_HOME: home,
        GI_CONFIG: config,
        GI_TEST_REPO: repoA,
        GI_REPO_B: repoB,
        GI_GITPRO: gitpro,
        GI_GITPRO_OLD: old,
        GI_GIT_BIN: bin,
        GI_GIT_LOG: gitLog,
        GI_GLOBAL: global,
        GI_SCAN_DIR: path.dirname(scanned),
        GI_SCAN_REPO: scanned,
      },
    });
  } finally {
    if (tmp) {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
