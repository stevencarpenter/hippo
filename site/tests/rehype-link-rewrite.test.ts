import { describe, expect, it } from "vitest";
import { unified } from "unified";
import rehypeParse from "rehype-parse";
import rehypeStringify from "rehype-stringify";
import { rehypeLinkRewrite, repoPathToSitePath } from "../src/lib/rehype/link-rewrite.ts";

async function runWithSource(html: string, sourcePath: string): Promise<string> {
  const file = await unified()
    .use(rehypeParse, { fragment: true })
    .use(rehypeLinkRewrite)
    .use(rehypeStringify)
    .process({
      value: html,
      data: { astro: { frontmatter: { sourcePath } } },
    });
  return String(file);
}

describe("repoPathToSitePath", () => {
  it("README.md -> /docs/getting-started", () => {
    expect(repoPathToSitePath("README.md")).toBe("/docs/getting-started");
  });
  it("CONTRIBUTING.md -> /docs/contributing", () => {
    expect(repoPathToSitePath("CONTRIBUTING.md")).toBe("/docs/contributing");
  });
  it("docs/redaction.md -> /docs/reference/redaction", () => {
    expect(repoPathToSitePath("docs/redaction.md")).toBe("/docs/reference/redaction");
  });
  it("docs/capture/anti-patterns.md -> /docs/capture/anti-patterns", () => {
    expect(repoPathToSitePath("docs/capture/anti-patterns.md")).toBe(
      "/docs/capture/anti-patterns",
    );
  });
  it("docs/capture/README.md -> /docs/capture", () => {
    expect(repoPathToSitePath("docs/capture/README.md")).toBe("/docs/capture");
  });
  it("docs/archive/foo.md -> null", () => {
    expect(repoPathToSitePath("docs/archive/foo.md")).toBeNull();
  });
  it("docs/superpowers/foo.md -> null", () => {
    expect(repoPathToSitePath("docs/superpowers/foo.md")).toBeNull();
  });
  it("non-docs path -> null", () => {
    expect(repoPathToSitePath("Cargo.toml")).toBeNull();
  });
});

describe("rehypeLinkRewrite", () => {
  it("rewrites a sibling .md link", async () => {
    const out = await runWithSource(
      `<a href="adding-a-source.md">x</a>`,
      "docs/capture/architecture.md",
    );
    expect(out).toContain('href="/docs/capture/adding-a-source"');
  });

  it("rewrites parent-relative link", async () => {
    const out = await runWithSource(
      `<a href="../redaction.md">x</a>`,
      "docs/capture/architecture.md",
    );
    expect(out).toContain('href="/docs/reference/redaction"');
  });

  it("preserves anchor fragments", async () => {
    const out = await runWithSource(
      `<a href="../redaction.md#patterns">x</a>`,
      "docs/capture/architecture.md",
    );
    expect(out).toContain('href="/docs/reference/redaction#patterns"');
  });

  it("rewrites a GitHub blob URL to site-relative", async () => {
    const out = await runWithSource(
      `<a href="https://github.com/stevencarpenter/hippo/blob/main/docs/capture/anti-patterns.md">x</a>`,
      "README.md",
    );
    expect(out).toContain('href="/docs/capture/anti-patterns"');
  });

  it("marks external URLs target=_blank rel=noopener and appends ↗", async () => {
    const out = await runWithSource(
      `<a href="https://example.com/foo">x</a>`,
      "README.md",
    );
    expect(out).toContain('target="_blank"');
    expect(out).toContain('rel="noopener"');
    expect(out).toContain("↗");
  });

  it("leaves anchor-only links unchanged", async () => {
    const out = await runWithSource(`<a href="#section">x</a>`, "docs/lifecycle.md");
    expect(out).toContain('href="#section"');
  });

  it("leaves absolute site links unchanged", async () => {
    const out = await runWithSource(`<a href="/install">x</a>`, "docs/lifecycle.md");
    expect(out).toContain('href="/install"');
  });

  it("does not touch non-md relative links", async () => {
    const out = await runWithSource(
      `<a href="../../scripts/install.sh">x</a>`,
      "docs/capture/architecture.md",
    );
    // The plugin only rewrites .md targets; .sh is left alone (caller will see a 404 if
    // the file isn't published — that's the right tradeoff for v1.0).
    expect(out).toContain('href="../../scripts/install.sh"');
  });
});
