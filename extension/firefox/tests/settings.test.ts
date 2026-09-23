import { expect, test } from "bun:test";
import { buildSync } from "esbuild";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import { DEFAULT_ALLOWLIST } from "../src/config";

function bundle(script: "background" | "popup"): string {
  const result = buildSync({
    entryPoints: [fileURLToPath(new URL(`../src/${script}.ts`, import.meta.url))],
    bundle: true,
    format: "iife",
    write: false,
  });
  return result.outputFiles[0].text;
}

const backgroundSource = bundle("background");
const popupSource = bundle("popup");
const settle = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

async function startBackground(stored: Record<string, unknown>) {
  const registrations: string[][] = [];
  const visits: unknown[] = [];
  let onMessage: (message: unknown, sender: { id: string }) => unknown = () => {
    throw new Error("Background did not install its message listener");
  };
  runInNewContext(backgroundSource, {
    URL,
    console,
    browser: {
      runtime: {
        id: "hippo-browser@local",
        getManifest: () => ({ version: "0.2.0" }),
        onMessage: { addListener: (listener: typeof onMessage) => { onMessage = listener; } },
        sendNativeMessage: async (_host: string, payload: { type?: string }) => {
          if (payload.type !== "heartbeat") visits.push(payload);
          return { status: "ok" };
        },
      },
      storage: {
        local: {
          get: async () => stored,
          set: async (changes: Record<string, unknown>) => { Object.assign(stored, changes); },
          remove: async (key: string) => { delete stored[key]; },
        },
        onChanged: { addListener: () => {} },
      },
      alarms: { create: () => {}, onAlarm: { addListener: () => {} } },
      contentScripts: {
        register: async (options: { matches: string[] }) => {
          registrations.push(options.matches);
          return { unregister: async () => {} };
        },
      },
    },
  });
  await settle();
  await onMessage({
    type: "page_visit",
    url: "https://github.com/project",
    domain: "github.com",
    dwell_ms: 4_000,
    scroll_depth: 0,
    timestamp: Date.now(),
    referrer: null,
  }, { id: "hippo-browser@local" });
  await settle();
  return { registrations, visits };
}

async function popupAllowlist(stored: Record<string, unknown>): Promise<string> {
  const textarea = { value: "" };
  runInNewContext(popupSource, {
    browser: { storage: { local: { get: async () => stored } } },
    document: {
      getElementById: (id: string) => id === "allowlist"
        ? textarea
        : { addEventListener: () => {}, style: {} },
    },
  });
  await settle();
  return textarea.value;
}

test("a saved empty allowlist prevents registration and capture after restart", async () => {
  const { registrations, visits } = await startBackground({ allowlist: [] });
  expect(registrations).toHaveLength(0);
  expect(visits).toHaveLength(0);
});

test("reopening the popup preserves a saved empty allowlist", async () => {
  expect(await popupAllowlist({ allowlist: [] })).toBe("");
});

test("an absent allowlist still uses defaults for capture and the popup", async () => {
  const { registrations, visits } = await startBackground({});
  expect(registrations).toHaveLength(1);
  expect(registrations[0]).toContain("*://github.com/*");
  expect(visits).toHaveLength(1);
  expect(await popupAllowlist({})).toBe(DEFAULT_ALLOWLIST.join("\n"));
});
