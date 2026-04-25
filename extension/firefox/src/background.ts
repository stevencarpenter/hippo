/**
 * Hippo Browser Capture — background script.
 *
 * Dynamically registers content scripts on allowlisted domains via
 * browser.contentScripts.register(). Receives page_visit messages from
 * content scripts, filters by allowlist (defense-in-depth), extracts
 * search queries from referrers, and relays to the hippo_daemon native
 * messaging host.
 */

import { DEFAULT_ALLOWLIST, MIN_DWELL_MS, NATIVE_HOST, SEARCH_ENGINES } from "./config";
import { HEARTBEAT_INTERVAL_MS, buildHeartbeatPayload } from "./heartbeat";
import type { BrowserVisit, PageVisitMessage, Settings } from "./types";

// Re-export for consumers that import from background.ts directly.
export { HEARTBEAT_INTERVAL_MS, buildHeartbeatPayload };

// --- Heartbeat ---

/**
 * Send a heartbeat to the hippo_daemon Native Messaging host.
 *
 * Called on startup and every `HEARTBEAT_INTERVAL_MS`.  On success the
 * timestamp is persisted to `browser.storage.local` so the popup badge
 * can display freshness without querying the daemon.
 */
async function sendHeartbeat(): Promise<void> {
  const manifest = browser.runtime.getManifest();
  const msg = buildHeartbeatPayload(manifest.version, settings.enabled);
  try {
    // sendNativeMessage resolves with the response object from the NM host.
    // Inspect status to distinguish daemon-side errors from transport errors.
    const resp = (await browser.runtime.sendNativeMessage(NATIVE_HOST, msg)) as {
      status?: string;
    };
    if (resp?.status === "ok") {
      browser.storage.local.set({ lastHeartbeatTs: msg.sent_at_ms, lastHeartbeatOk: true });
    } else {
      console.warn("[hippo] heartbeat daemon error:", resp);
      browser.storage.local.set({ lastHeartbeatOk: false });
    }
  } catch (e) {
    console.warn("[hippo] heartbeat failed:", e);
    browser.storage.local.set({ lastHeartbeatOk: false });
  }
}

// --- Runtime settings (loaded from storage) ---
const settings: Settings = {
  enabled: true,
  allowlist: DEFAULT_ALLOWLIST.slice(),
  captureCount: 0,
};

// Serializes registration calls to prevent races on rapid allowlist changes
let registrationChain: Promise<void> = Promise.resolve();

// --- Load settings from storage ---
function loadSettings(): Promise<void> {
  return browser.storage.local.get(["enabled", "allowlist", "captureCount"]).then((result) => {
    if (typeof result.enabled === "boolean") {
      settings.enabled = result.enabled;
    }
    if (Array.isArray(result.allowlist) && result.allowlist.length > 0) {
      settings.allowlist = result.allowlist as string[];
    }
    if (typeof result.captureCount === "number") {
      settings.captureCount = result.captureCount;
    }
  });
}

// --- Persist capture count ---
function persistCaptureCount(): void {
  browser.storage.local.set({ captureCount: settings.captureCount });
}

// --- Check if a domain is in the allowlist ---
function isDomainAllowed(domain: string): boolean {
  const domainLower = domain.toLowerCase();
  return settings.allowlist.some((entry) => {
    const entryLower = entry.toLowerCase();
    return domainLower === entryLower || domainLower.endsWith("." + entryLower);
  });
}

// --- Dynamic content script registration ---
let registeredScript: browser.contentScripts.RegisteredContentScript | null = null;

async function updateContentScripts(): Promise<void> {
  // Unregister any existing content script registration
  if (registeredScript) {
    await registeredScript.unregister();
    registeredScript = null;
  }

  if (!settings.enabled || settings.allowlist.length === 0) return;

  // Build match patterns from allowlist domains
  const patterns = settings.allowlist.flatMap((domain) => [
    `*://${domain}/*`,
    `*://*.${domain}/*`,
  ]);

  try {
    registeredScript = await browser.contentScripts.register({
      matches: patterns,
      js: [{ file: "lib/Readability.js" }, { file: "dist/content.js" }],
      runAt: "document_idle",
    });
  } catch (error) {
    console.error("[hippo] content script registration failed:", error);
  }
}

// --- Extract search query from a referrer URL ---
function extractSearchQuery(referrer: string | null): string | null {
  if (!referrer) return null;

  let url: URL;
  try {
    url = new URL(referrer);
  } catch (_e) {
    return null;
  }

  const hostname = url.hostname.toLowerCase();
  const pathname = url.pathname;

  for (const engine of SEARCH_ENGINES) {
    const domainMatch =
      hostname === engine.domain || hostname.endsWith("." + engine.domain);
    if (!domainMatch) continue;

    if (engine.pathPrefix && !pathname.startsWith(engine.pathPrefix)) continue;

    const query = url.searchParams.get(engine.param);
    if (query && query.trim().length > 0) {
      return query.trim();
    }
  }

  return null;
}

// --- Validate message sender is our own extension ---
function isOwnExtension(sender: browser.runtime.MessageSender): boolean {
  return sender != null && sender.id === browser.runtime.id;
}

// --- Validate page_visit message structure ---
function isValidPageVisit(msg: unknown): msg is PageVisitMessage {
  if (typeof msg !== "object" || msg === null) return false;
  const m = msg as Record<string, unknown>;
  return (
    typeof m.url === "string" &&
    typeof m.domain === "string" &&
    typeof m.dwell_ms === "number" &&
    typeof m.scroll_depth === "number" &&
    typeof m.timestamp === "number" &&
    (m.url as string).length > 0 &&
    (m.domain as string).length > 0 &&
    (m.dwell_ms as number) >= 0 &&
    (m.scroll_depth as number) >= 0 &&
    (m.scroll_depth as number) <= 1.0
  );
}

// --- Listen for messages from content scripts ---
browser.runtime.onMessage.addListener(
  (message: unknown, sender: browser.runtime.MessageSender): void | Promise<unknown> => {
    // Reject messages from other extensions or web pages
    if (!isOwnExtension(sender)) return;

    if (typeof message !== "object" || message === null) return;
    const msg = message as Record<string, unknown>;
    if (msg.type !== "page_visit") return;

    // Ensure settings are loaded before processing
    return settingsReady.then(() => {
      if (!settings.enabled) return;

      // Validate message structure before processing
      if (!isValidPageVisit(message)) return;

      // Defense-in-depth: content scripts only run on allowlisted domains,
      // but we re-check here in case of bugs or race conditions.
      if (!isDomainAllowed(message.domain)) return;

      if (message.dwell_ms < MIN_DWELL_MS) return;

      const searchQuery = extractSearchQuery(message.referrer);

      const visit: BrowserVisit = {
        url: String(message.url),
        title: String(message.title || ""),
        domain: String(message.domain),
        dwell_ms: Math.round(message.dwell_ms),
        scroll_depth: parseFloat(message.scroll_depth.toFixed(3)),
        extracted_text: typeof message.extracted_text === "string" ? message.extracted_text : null,
        search_query: searchQuery,
        referrer: typeof message.referrer === "string" ? message.referrer : null,
        timestamp: Math.round(message.timestamp),
      };

      browser.runtime.sendNativeMessage(NATIVE_HOST, visit).then(
        () => {
          settings.captureCount++;
          persistCaptureCount();
          browser.storage.local.set({ lastSendOk: true, lastSendAt: Date.now() });
        },
        (error) => {
          console.error("[hippo] native messaging error:", error);
          browser.storage.local.set({
            lastSendOk: false,
            lastSendAt: Date.now(),
            lastSendError: String(error),
          });
        },
      );
    });
  },
);

// --- Listen for storage changes (settings updated from popup) ---
browser.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  let needsReregister = false;
  if (changes.enabled) {
    settings.enabled = changes.enabled.newValue as boolean;
    needsReregister = true;
  }
  if (changes.allowlist) {
    settings.allowlist = changes.allowlist.newValue as string[];
    needsReregister = true;
  }
  if (changes.captureCount) {
    settings.captureCount = changes.captureCount.newValue as number;
  }
  if (needsReregister) {
    registrationChain = registrationChain.then(() => updateContentScripts());
  }
});

// --- Initialize ---
const settingsReady: Promise<void> = loadSettings();
settingsReady.then(() => updateContentScripts());

// Fire heartbeat on startup (after settings loaded) and then every 5 minutes.
// Startup heartbeat is deferred until settings are ready so `enabled_state`
// reflects persisted state rather than the constructor default.
//
// We use `browser.alarms` (not setInterval) because the background page is
// non-persistent (`manifest.json: background.persistent = false`).  Firefox
// unloads idle event pages, which would silently kill a JS interval.  Alarms
// are wake-capable: they fire even after the background page is unloaded,
// causing Firefox to reload it and dispatch the alarm event.
settingsReady.then(() => {
  sendHeartbeat();
  // Derive periodInMinutes from the canonical constant so they never drift.
  browser.alarms.create("hippo-heartbeat", {
    periodInMinutes: HEARTBEAT_INTERVAL_MS / 60_000,
  });
});

// Gate the alarm handler on settingsReady: when Firefox wakes the event page
// to fire an alarm, module code re-runs and loadSettings() is called again.
// The alarm can be dispatched before the storage read resolves, which would
// cause sendHeartbeat() to see the default settings values.  Awaiting
// settingsReady here is free when the page is already live (resolved promise).
browser.alarms.onAlarm.addListener(async (alarm) => {
  await settingsReady;
  if (alarm.name === "hippo-heartbeat") {
    sendHeartbeat();
  }
});
