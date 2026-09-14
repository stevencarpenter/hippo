import { expect, it } from "vitest";
import type { CollectionEntry } from "astro:content";
import { buildSidebar } from "../src/lib/docs.ts";

it("keeps operating docs, research, and section indexes discoverable without duplicate links", () => {
  const ids = ["capture/README", "capture/sources", "mcp-reference", "research/retrieval", "baselines/latency", "archive/retired"];
  const entries = ids.map(id => ({ id, collection: "docs", data: {} })) as CollectionEntry<"docs">[];
  const sections = buildSidebar(entries, [], []);
  expect(sections.map(({ id, url, entries }) => ({ id, url, slugs: entries.map(entry => entry.slug) }))).toEqual([
    { id: "capture", url: "/docs/capture", slugs: ["capture/sources"] },
    { id: "reference", url: undefined, slugs: ["reference/mcp-reference"] },
    { id: "research", url: undefined, slugs: ["baselines/latency", "research/retrieval"] },
  ]);
});
