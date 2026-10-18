// Running a program: argv array, never a shell. No vscode import, so the unit tests can load it; cli.ts re-exports it.
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";

export interface Result {
  code: number | string; // exit code, or "ENOENT" (no such program), "ENOCWD" (no such cwd), "TIMEOUT", "ABORT_ERR",
  // "ERR_CHILD_PROCESS_STDIO_MAXBUFFER", "EONSPAWN"
  stdout: string;
  stderr: string;
}

/** A started child process. */
export interface Child {
  /** Its pid (on POSIX also its process group id). */
  readonly pid: number | undefined;
  /** Resolves when the process has really exited: its exit code, or the signal that ended it ("SIGTERM").
   * An aborted run() resolves at once; this one waits for the child itself. Never rejects. */
  readonly exited: Promise<number | string>;
}

export interface RunOptions {
  /** Working directory; default the home directory. */
  cwd?: string;
  /** Milliseconds before the child is killed (code "TIMEOUT"); default 60 s; 0 for none. */
  timeout?: number;
  /** Merged over process.env; an undefined value removes the variable. */
  env?: Record<string, string | undefined>;
  /** Aborting kills the child (code "ABORT_ERR"), e.g. a cancelled withProgress. The result comes at once; use
   * onSpawn's Child.exited to wait for the child to finish (e.g. its cleanup). */
  signal?: AbortSignal;
  /** Exit codes that count as success for show()/json() (default [0]; `why` uses [0, 1]). */
  accept?: number[];
  /** Log at debug level and show no message on failure (status bar refreshes, probes). */
  quiet?: boolean;
  /** Called once the child has started. If it throws, the child is killed and the result's code is "EONSPAWN". */
  onSpawn?: (child: Child) => void;
}

/** Per stream (stdout, stderr), as execFile counts it. */
const MAX_BUFFER = 64 * 1024 * 1024;
/** After an abort, timeout or maxBuffer SIGTERM, how long the child (and its group) gets before SIGKILL. */
export const KILL_GRACE = 3_000;
/** POSIX: the child leads its own process group, and a kill (abort, timeout) goes to the whole group, so a
 * process it started (git's ssh) is not orphaned. Windows has no process groups; the child alone is killed. */
const GROUP = process.platform !== "win32";

interface Live {
  kill(sig: NodeJS.Signals): void;
  exited: Promise<number | string>;
}
/** Children whose output is still open (a process of their group may still run); killAll() ends them. */
const live = new Set<Live>();

/** For the extension's deactivate(): SIGTERM every live child's process group, give them `grace` ms to exit
 * (gitpro clone removes its partial directory), then SIGKILL whatever is left. */
export async function killAll(grace = 2_000): Promise<void> {
  const all = [...live];
  all.forEach((c) => c.kill("SIGTERM"));
  await within(Promise.all(all.map((c) => c.exited)), grace);
  all.filter((c) => live.has(c)).forEach((c) => c.kill("SIGKILL"));
}

/** How many children are live (tests). */
export function liveCount(): number {
  return live.size;
}

export function exec(cmd: string, args: string[], opts: RunOptions = {}): Promise<Result> {
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const [k, v] of Object.entries(opts.env ?? {})) {
    if (v === undefined) {
      delete env[k];
    } else {
      env[k] = v;
    }
  }
  const cwd = opts.cwd ?? os.homedir();
  // spawn reports a missing cwd as ENOENT, which would read as "gitpro is not installed".
  if (!isDir(cwd)) {
    return Promise.resolve({ code: "ENOCWD", stdout: "", stderr: `✗ ${cwd}: no such directory` });
  }
  if (opts.signal?.aborted) {
    return Promise.resolve({ code: "ABORT_ERR", stdout: "", stderr: "" });
  }
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { cwd, env, detached: GROUP, stdio: ["ignore", "pipe", "pipe"] });
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    const size = new Map<Buffer[], number>([[out, 0], [err, 0]]);
    let done = false;
    let closed = false; // 'close': the child has exited and nothing holds its output open any more
    let killed: string | undefined; // the code to report once the child is gone: "TIMEOUT", maxBuffer's
    let timer: NodeJS.Timeout | undefined;
    let grace: NodeJS.Timeout | undefined;

    const exited = new Promise<number | string>((res) => {
      child.once("exit", (code, sig) => res(code ?? sig ?? -1));
      child.once("close", (code, sig) => res(code ?? sig ?? -1)); // spawn failed: 'close' comes without 'exit'
    });
    /** Signal the child's process group (POSIX), even when the child itself has exited: a process it left
     * behind (`sleep 30 &`) may still hold its output open. Once 'close' came there is nothing left to signal. */
    function kill(sig: NodeJS.Signals): void {
      if (child.pid === undefined || closed) {
        return;
      }
      if (GROUP) {
        try {
          process.kill(-child.pid, sig);
          return;
        } catch {
          // the group is gone or not ours: signal the child alone
        }
      }
      if (child.exitCode === null && child.signalCode === null) {
        child.kill(sig);
      }
    }
    const entry: Live = { kill, exited };
    function finish(code: number | string, stderr = ""): void {
      if (done) {
        return;
      }
      done = true;
      clearTimeout(timer);
      opts.signal?.removeEventListener("abort", onAbort);
      resolve({ code, stdout: Buffer.concat(out).toString("utf8"), stderr: Buffer.concat(err).toString("utf8") + stderr });
    }
    /** Abort, timeout, maxBuffer: SIGTERM, and SIGKILL after KILL_GRACE if the output is still open (a child that
     * traps SIGTERM, a grandchild holding the pipe); then stop reading and resolve with `code` regardless. */
    function stop(code: string): void {
      killed ??= code;
      kill("SIGTERM");
      grace ??= setTimeout(() => {
        kill("SIGKILL");
        child.stdout?.destroy();
        child.stderr?.destroy();
        finish(killed ?? code);
      }, KILL_GRACE);
    }
    function onAbort(): void {
      stop("ABORT_ERR");
      finish("ABORT_ERR");
    }
    function collect(into: Buffer[]) {
      return (chunk: Buffer) => {
        const room = MAX_BUFFER - size.get(into)!;
        if (room <= 0) {
          return; // over the limit already: being stopped
        }
        if (chunk.length > room) {
          into.push(chunk.subarray(0, room)); // like execFile: keep output up to the limit
          size.set(into, MAX_BUFFER);
          stop("ERR_CHILD_PROCESS_STDIO_MAXBUFFER");
          return;
        }
        size.set(into, size.get(into)! + chunk.length);
        into.push(chunk);
      };
    }

    child.stdout?.on("data", collect(out));
    child.stderr?.on("data", collect(err));
    child.on("error", (e: NodeJS.ErrnoException) => {
      if (child.pid === undefined) {
        finish(e.code ?? -1); // could not start: "ENOENT", "EACCES", ...
      }
    });
    child.on("close", (code) => {
      closed = true;
      live.delete(entry);
      clearTimeout(grace);
      finish(killed ?? code ?? -1);
    });
    if (child.pid === undefined) {
      return; // 'error' follows
    }
    live.add(entry);
    opts.signal?.addEventListener("abort", onAbort, { once: true });
    const ms = opts.timeout ?? 60_000;
    if (ms > 0) {
      timer = setTimeout(() => stop("TIMEOUT"), ms);
    }
    try {
      opts.onSpawn?.({ pid: child.pid, exited });
    } catch (e) {
      kill("SIGKILL");
      finish("EONSPAWN", `✗ onSpawn failed: ${e instanceof Error ? e.message : String(e)}\n`);
    }
  });
}

/** Query parameters whose value is a secret (case-insensitive). Keep in sync with resolve.py SECRET_QUERY_KEYS. */
const SECRET_KEYS = ["access_token", "private_token", "oauth_token", "refresh_token", "id_token", "token", "auth",
  "authorization", "password", "passwd", "pwd", "secret", "client_secret", "api_key", "apikey",
  "key", "sig", "signature", "x-amz-signature", "x-amz-credential", "x-amz-security-token", "jwt"];
const SECRET_QUERY = new RegExp(`([?&;](?:${SECRET_KEYS.join("|")})=)[^&;#\\s]*`, "gi"); // the keys are [a-z_-]

/** An argument for the log, like resolve.redact_url: a password in a URL's userinfo becomes *** (user:***@), and
 * so does a bare token in an http(s) userinfo (***@) and the value of a secret query parameter
 * (?access_token=***); anything else is unchanged. Display only. */
export function redactUrl(s: string): string {
  const q = s.indexOf("?");
  if (q >= 0) {
    s = s.slice(0, q) + s.slice(q).replace(SECRET_QUERY, "$1***");
  }
  const m = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([^/]*)@/.exec(s);
  if (!m) {
    return s;
  }
  const colon = m[2].indexOf(":");
  let info: string;
  if (colon >= 0) {
    info = `${m[2].slice(0, colon)}:***`;
  } else if (/^https?$/i.test(m[1])) {
    info = "***";
  } else {
    return s;
  }
  return `${m[1]}://${info}@${s.slice(m[0].length)}`;
}

/** Text for the log: every URL in it goes through redactUrl, so a token in output a process printed is masked too. */
export function redactText(s: string): string {
  return s.replace(/[A-Za-z][A-Za-z0-9+.-]*:\/\/[^\s'"<>]+/g, redactUrl);
}

/** Resolves like p (e.g. Child.exited), or to undefined when p has not settled after `ms`. */
export function within<T>(p: Promise<T>, ms: number): Promise<T | undefined> {
  let timer: NodeJS.Timeout | undefined;
  const late = new Promise<undefined>((resolve) => {
    timer = setTimeout(() => resolve(undefined), ms);
  });
  return Promise.race([p, late]).finally(() => clearTimeout(timer));
}

export function isDir(p: string): boolean {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}
