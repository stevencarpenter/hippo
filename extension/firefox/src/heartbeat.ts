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
 * Build the heartbeat payload for a given extension version.
 *
 * Does not send anything — use `browser.runtime.sendNativeMessage` with the
 * return value to deliver the heartbeat.
 */
export function buildHeartbeatPayload(version: string): HippoHeartbeat {
  return {
    type: "heartbeat",
    extension_version: version,
    enabled_state: true,
    sent_at_ms: Date.now(),
  };
}
