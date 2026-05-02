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

  it("redirects archive-section .md links to GitHub blob", async () => {
    const out = await runWithSource(
      `<a href="../archive/capture-reliability-overhaul/11-watcher-data-loss-fix.md">x</a>`,
      "docs/capture/anti-patterns.md",
    );
    expect(out).toContain('href="https://github.com/stevencarpenter/hippo/blob/main/docs/archive/capture-reliability-overhaul/11-watcher-data-loss-fix.md"');
    expect(out).toContain('target="_blank"');
  });

  it("redirects cross-tree .md links (CLAUDE.md from contributing) to GitHub blob", async () => {
    const out = await runWithSource(
      `<a href="CLAUDE.md">x</a>`,
      "CONTRIBUTING.md",
    );
    expect(out).toContain('href="https://github.com/stevencarpenter/hippo/blob/main/CLAUDE.md"');
    expect(out).toContain('target="_blank"');
    expect(out).toContain("↗");
  });

  it("redirects sibling-package .md links (extension/firefox/README.md) to GitHub blob", async () => {
    const out = await runWithSource(
      `<a href="extension/firefox/README.md">x</a>`,
      "README.md",
    );
    expect(out).toContain('href="https://github.com/stevencarpenter/hippo/blob/main/extension/firefox/README.md"');
    expect(out).toContain('target="_blank"');
  });

  it("redirects parent-relative non-docs README links to GitHub blob", async () => {
    const out = await runWithSource(
      `<a href="../../shell/README.md">x</a>`,
      "docs/capture/adding-a-source.md",
    );
    expect(out).toContain('href="https://github.com/stevencarpenter/hippo/blob/main/shell/README.md"');
    expect(out).toContain('target="_blank"');
  });

  it("rewrites relative directory link to section index", async () => {
    const out = await runWithSource(
      `<a href="capture/">x</a>`,
      "docs/eval-harness-design.md",
    );
    expect(out).toContain('href="/docs/capture"');
    expect(out).not.toContain('target="_blank"');
  });

  it("rewrites parent-relative directory link to section index", async () => {
    const out = await runWithSource(
      `<a href="../capture/">x</a>`,
      "docs/capture/anti-patterns.md",
    );
    expect(out).toContain('href="/docs/capture"');
  });

  it("preserves fragment on directory link", async () => {
    const out = await runWithSource(
      `<a href="capture/#sources">x</a>`,
      "docs/eval-harness-design.md",
    );
    expect(out).toContain('href="/docs/capture#sources"');
  });

  it("redirects excluded-section directory link to GitHub tree", async () => {
    const out = await runWithSource(
      `<a href="../archive/capture-reliability-overhaul/">x</a>`,
      "docs/capture/anti-patterns.md",
    );
    expect(out).toContain(
      'href="https://github.com/stevencarpenter/hippo/tree/main/docs/archive/capture-reliability-overhaul"',
    );
    expect(out).toContain('target="_blank"');
  });

  it("rewrites non-md non-directory repo link to GitHub blob with arrow", async () => {
    const out = await runWithSource(
      `<a href="LICENSE">MIT License</a>`,
      "README.md",
    );
    expect(out).toContain(
      'href="https://github.com/stevencarpenter/hippo/blob/main/LICENSE"',
    );
    expect(out).toContain('target="_blank"');
    expect(out).toContain("↗");
  });

  it("rewrites deep repo file path to GitHub blob", async () => {
    const out = await runWithSource(
      `<a href="../../scripts/install.sh">x</a>`,
      "docs/capture/architecture.md",
    );
    expect(out).toContain(
      'href="https://github.com/stevencarpenter/hippo/blob/main/scripts/install.sh"',
    );
    expect(out).toContain('target="_blank"');
  });

  it("rewrites cross-directory repo link with subpath to GitHub blob", async () => {
    const out = await runWithSource(
      `<a href="crates/hippo-core/src/schema.sql">schema</a>`,
      "README.md",
    );
    expect(out).toContain(
      'href="https://github.com/stevencarpenter/hippo/blob/main/crates/hippo-core/src/schema.sql"',
    );
  });

  it("leaves query-only relative links unchanged", async () => {
    const out = await runWithSource(
      `<a href="?tab=changes">x</a>`,
      "README.md",
    );
    expect(out).toContain('href="?tab=changes"');
  });

  it("leaves mailto links unchanged", async () => {
    const out = await runWithSource(
      `<a href="mailto:foo@example.com">x</a>`,
      "docs/lifecycle.md",
    );
    expect(out).toContain('href="mailto:foo@example.com"');
  });
});
