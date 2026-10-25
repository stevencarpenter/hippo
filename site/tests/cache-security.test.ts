import { createRequire } from "node:module";
import { afterEach, expect, it, vi } from "vitest";

const CachePolicy = createRequire(import.meta.resolve("astro"))("http-cache-semantics");
const request = { url: "https://example.test/account", headers: { host: "example.test" } };

afterEach(() => vi.useRealTimers());

it.each([
  { "set-cookie": "session=private" },
  { "cache-control": "no-cache" },
  { "cache-control": "no-store" },
  { "cache-control": "private" },
  { "cache-control": "proxy-revalidate" },
])("does not reuse security-excluded responses with max-stale: %j", (headers) => {
  vi.useFakeTimers();
  const policy = new CachePolicy(request, { status: 200, headers });
  vi.advanceTimersByTime(1000);
  for (const directive of ["max-stale", "max-stale=86400"]) {
    const next = { ...request, headers: { ...request.headers, "cache-control": directive } };
    expect(policy.satisfiesWithoutRevalidation(next)).toBe(false);
    expect(policy.evaluateRequest(next).response).toBeUndefined();
  }
});

it("still honors max-stale for an ordinarily expired public response", () => {
  vi.useFakeTimers();
  const policy = new CachePolicy(request, {
    status: 200,
    headers: { "cache-control": "public, max-age=1" },
  });
  vi.advanceTimersByTime(2000);
  const next = {
    ...request,
    headers: { ...request.headers, "cache-control": "max-stale=86400" },
  };
  expect(policy.satisfiesWithoutRevalidation(next)).toBe(true);
  expect(policy.evaluateRequest(next).response).toBeDefined();
});
