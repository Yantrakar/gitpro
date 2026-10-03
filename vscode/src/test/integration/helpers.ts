// Shared by the *.integration.ts files.
import type { Api } from "../../extension";

export interface Seen {
  info: string[];
  warn: string[];
  error: string[];
  modal: string[];
  pick: string[][]; // the labels of each quick pick shown
}

/** Replaces api.ui for the duration of fn: messages are recorded and resolve at once; modal() answers `modal`,
 * pick() answers whatever `pick` chooses from the items (undefined: cancelled). */
export async function withUi<T>(api: Api, answers: { modal?: string; pick?: (labels: string[]) => string | undefined },
  fn: (seen: Seen) => Promise<T>): Promise<T> {
  const seen: Seen = { info: [], warn: [], error: [], modal: [], pick: [] };
  const saved = { ...api.ui };
  api.ui.info = async (msg: string) => (seen.info.push(msg), undefined);
  api.ui.warn = async (msg: string) => (seen.warn.push(msg), undefined);
  api.ui.error = async (msg: string) => (seen.error.push(msg), undefined);
  api.ui.modal = async (msg: string) => (seen.modal.push(msg), answers.modal);
  api.ui.pick = (async (items: { label: string }[]) => {
    const labels = items.map((i) => i.label);
    seen.pick.push(labels);
    const want = answers.pick?.(labels);
    return items.find((i) => i.label === want);
  }) as typeof api.ui.pick;
  try {
    return await fn(seen);
  } finally {
    Object.assign(api.ui, saved);
  }
}

/** Sets process.env[k] (undefined: removes it) for the duration of fn; the extension's children see it. */
export async function withEnv<T>(vars: Record<string, string | undefined>, fn: () => Promise<T>): Promise<T> {
  const saved: Record<string, string | undefined> = {};
  for (const [k, v] of Object.entries(vars)) {
    saved[k] = process.env[k];
    if (v === undefined) {
      delete process.env[k];
    } else {
      process.env[k] = v;
    }
  }
  try {
    return await fn();
  } finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) {
        delete process.env[k];
      } else {
        process.env[k] = v;
      }
    }
  }
}

export function env(k: string): string {
  const v = process.env[k];
  if (!v) {
    throw new Error(`${k} is not set: run through runTest.js`);
  }
  return v;
}
