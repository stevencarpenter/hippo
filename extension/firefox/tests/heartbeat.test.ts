/**
 * Unit tests for the Firefox extension heartbeat (T-3).
 *
 * Tests the payload shape and interval constant without requiring a real
 * browser runtime.  `buildHeartbeatPayload()` and `HEARTBEAT_INTERVAL_MS`
 * are pure helpers exported from background.ts precisely so they can be
 * tested here without mocking the full browser API.
 */

import { describe, expect, test } from "bun:test";
import { buildHeartbeatPayload, HEARTBEAT_INTERVAL_MS } from "../src/heartbeat";

describe("HEARTBEAT_INTERVAL_MS", () => {
  test("is exactly 5 minutes in milliseconds", () => {
    expect(HEARTBEAT_INTERVAL_MS).toBe(5 * 60 * 1000);
  });
});

describe("buildHeartbeatPayload", () => {
  test("type field is 'heartbeat'", () => {
    const p = buildHeartbeatPayload("0.2.0", true);
    expect(p.type).toBe("heartbeat");
  });

  test("extension_version matches the supplied version string", () => {
    const p = buildHeartbeatPayload("1.3.7", true);
    expect(p.extension_version).toBe("1.3.7");
  });

  test("enabled_state is true", () => {
    const p = buildHeartbeatPayload("0.2.0", true);
    expect(p.enabled_state).toBe(true);
  });

  test("sent_at_ms is a recent epoch-ms timestamp", () => {
    const before = Date.now();
    const p = buildHeartbeatPayload("0.2.0", true);
    const after = Date.now();
    expect(typeof p.sent_at_ms).toBe("number");
    expect(p.sent_at_ms).toBeGreaterThanOrEqual(before);
    expect(p.sent_at_ms).toBeLessThanOrEqual(after);
  });

  test("payload has exactly the four required fields", () => {
    const p = buildHeartbeatPayload("0.2.0", true);
    const keys = Object.keys(p).sort();
    expect(keys).toEqual(["enabled_state", "extension_version", "sent_at_ms", "type"].sort());
  });

  test("each call produces a fresh timestamp", async () => {
    const p1 = buildHeartbeatPayload("0.2.0", true);
    // Small sleep to guarantee the clock advances at least 1ms.
    await new Promise((r) => setTimeout(r, 2));
    const p2 = buildHeartbeatPayload("0.2.0", true);
    expect(p2.sent_at_ms).toBeGreaterThanOrEqual(p1.sent_at_ms);
  });
});
