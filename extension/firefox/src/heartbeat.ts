/**
 * Pure heartbeat helper functions — no browser API dependencies.
 *
 * Extracted so they can be unit-tested in Node/Bun without mocking the browser
 * runtime.  `background.ts` imports from here; the test suite imports from here
 * directly.
 */

import type { HippoHeartbeat } from "./types";

/** Interval between extension heartbeats sent to the hippo_daemon NM host. */
export const HEARTBEAT_INTERVAL_MS = 5 * 60 * 1000;

/**
 * Build the heartbeat payload for a given extension version and enabled state.
 *
 * Does not send anything — use `browser.runtime.sendNativeMessage` with the
 * return value to deliver the heartbeat.
 *
 * @param version - The extension version string (from `browser.runtime.getManifest().version`).
 * @param enabledState - Whether capture is currently enabled (`settings.enabled`).
 */
export function buildHeartbeatPayload(version: string, enabledState: boolean): HippoHeartbeat {
  return {
    type: "heartbeat",
    extension_version: version,
    enabled_state: enabledState,
    sent_at_ms: Date.now(),
  };
}
