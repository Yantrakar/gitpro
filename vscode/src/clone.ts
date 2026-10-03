// gitpro: Clone… (spec 07 §5): asks for a URL, shows where it goes and as whom, clones with the identity's key.
import * as fs from "node:fs";
import * as os from "node:os";
import * as vscode from "vscode";
import { Child, Cli, failure, redactUrl, Result, shq, ui, within } from "./cli";
import { tilde } from "./scan";

/** `gitpro clone --json` (dry run or real). */
export interface ClonePlan {
  url: string;
  clone_url: string;
  dir: string;
  identity: string | null;
  auth: string;
  dry_run: boolean;
  problems: { code: string; message: string; fix: string | null }[];
  git_exit: number | null;
  warnings?: string[];
  error?: string;
}

/** git failed for want of a passphrase, credentials or a host key answer, which only a terminal can supply. */
export const NEEDS_TERMINAL = /permission denied|could not read|host key verification failed/i;

/** How long a cancel waits for gitpro to stop git and remove the partial clone (it SIGKILLs git after 2 s). */
export const STOP_WAIT = 10_000;

/** The modal question before cloning. */
export function question(p: ClonePlan, home: string): string {
  const who = p.identity ?? "the default identity";
  const q = `Clone ${p.clone_url} into ${tilde(p.dir, home)} as ${who}?`;
  return p.warnings?.length ? `${q}\n\n${p.warnings.join("\n")}` : q;
}

/** The command to run in a terminal instead. */
export function terminalCommand(url: string, dir?: string): string {
  return ["gitpro", "clone", url, ...(dir ? [dir] : [])].map(shq).join(" ");
}

function parse(r: Result): ClonePlan | undefined {
  try {
    return JSON.parse(r.stdout) as ClonePlan;
  } catch {
    return undefined;
  }
}

/** The command takes an optional URL and target directory (tests, other commands); else it asks for the URL.
 * Resolves to the clone's JSON after a successful clone, else undefined. */
export function registerClone(context: vscode.ExtensionContext, cli: Cli): void {
  context.subscriptions.push(vscode.commands.registerCommand("gitpro.clone", async (arg?: unknown, dirArg?: unknown) => {
    if (!cli.require("clone")) {
      return undefined;
    }
    let url = typeof arg === "string" ? arg.trim() : "";
    if (!url) {
      url = (await vscode.window.showInputBox({
        title: "gitpro: Clone",
        prompt: "Repository URL: git@host:owner/repo.git, https://host/owner/repo, or host/owner/repo",
        ignoreFocusOut: true,
        validateInput: (v) => (v.trim() ? undefined : "enter a git URL"),
      }))?.trim() ?? "";
      if (!url) {
        return undefined;
      }
    }
    const dir = typeof dirArg === "string" && dirArg ? dirArg : undefined;
    const target = [url, ...(dir ? [dir] : [])];
    const home = os.homedir();

    const p = await cli.json<ClonePlan>(["clone", "--json", "--dry-run", ...target]);
    if (!p) {
      return undefined; // cli.json showed the error (unparsable URL, target exists, ...)
    }
    if ((await ui.modal(question(p, home), "Clone")) !== "Clone") {
      return undefined;
    }

    const abort = new AbortController();
    let child: Child | undefined;
    const r = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Cloning ${p.clone_url}`, cancellable: true },
      async (progress, token) => {
        token.onCancellationRequested(() => abort.abort());
        // No timeout: a big clone takes as long as it takes. No prompts: a notification has no terminal, so git
        // fails instead of hanging. GIT_SSH_COMMAND unset so the identity's core.sshCommand picks the key.
        // Cancel sends gitpro's process group SIGTERM (POSIX; gitpro alone on Windows): gitpro stops git and its
        // ssh (SIGKILL after 2 s) and removes the partial clone.
        const res = await cli.run(["clone", "--json", "-y", ...target], {
          timeout: 0, signal: abort.signal, env: { GIT_TERMINAL_PROMPT: "0", GIT_SSH_COMMAND: undefined },
          onSpawn: (c) => (child = c),
        });
        if (res?.code === "ABORT_ERR" && child) {
          // The abort resolves run() as soon as the signal is sent; wait for gitpro itself to finish its cleanup.
          progress.report({ message: "stopping…" });
          const how = await within(child.exited, STOP_WAIT);
          if (how === undefined) {
            cli.log.warn(`  ✗ gitpro is still running ${STOP_WAIT / 1000} s after the cancel`);
          } else {
            cli.log.info(`  gitpro stopped (${how}) after the cancel`);
          }
        }
        return res;
      });
    if (!r) {
      return undefined; // gitpro not found (message shown)
    }
    if (r.code === "ABORT_ERR") {
      void (fs.existsSync(p.dir)
        ? ui.warn(`Clone cancelled, but ${tilde(p.dir, home)} is still there: remove it by hand`)
        : ui.warn(`Clone cancelled: ${p.clone_url}`));
      return undefined;
    }
    const done = parse(r);
    if (r.code !== 0 || !done || done.git_exit !== 0) {
      if (done?.git_exit && NEEDS_TERMINAL.test(r.stderr)) {
        const cmd = terminalCommand(url, dir);
        const shown = terminalCommand(redactUrl(url), dir); // display only: the copied command keeps the real url
        cli.log.warn(`  run in a terminal: ${shown}`);
        void ui.error(`Clone needs a terminal (ssh passphrase or credentials). Run in a terminal: ${shown}`,
          "Copy command").then((pick) => {
          if (pick) {
            void vscode.env.clipboard.writeText(cmd);
          }
        });
      } else {
        void ui.error(failure(r), "Show Log").then((pick) => pick && cli.log.show());
      }
      return undefined;
    }
    const shown = tilde(done.dir, home);
    cli.log.info(`  ✓ cloned into ${shown}; undo: rm -r ${shq(shown)}`);
    if (done.problems.length) {
      void ui.warn(`Cloned into ${shown}, but: ${done.problems[0].message}`);
    }
    void ui.info(`Cloned into ${shown}`, "Open Folder").then((pick) => {
      if (pick) {
        void vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(done.dir), { forceNewWindow: true });
      }
    });
    return done;
  }));
}
