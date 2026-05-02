import type { CollectionEntry } from "astro:content";

export type MotifId =
  | "cornu-ammonis"
  | "sectio-coronalis"
  | "trisynaptic-circuit"
  | "marginalia"
  | "plate-frame"
  | "fasciculus";

export interface SidebarEntry {
  slug: string; // URL slug (no leading slash)
  title: string;
  url: string; // /docs/...
}

export interface SidebarSection {
  id: string;
  label: string;
  caption?: string; // italic Latin caption under section heading
  entries: SidebarEntry[];
}

/** Map a docs entry slug to its repo-relative source path (used for "Edit on GitHub" + git timestamp). */
export function gitHubSourcePath(slug: string): string {
  if (slug === "getting-started") return "README.md";
  if (slug === "contributing") return "CONTRIBUTING.md";
  if (slug.startsWith("reference/")) {
    const inside = slug.slice("reference/".length);
    return `docs/${inside}.md`;
  }
  return `docs/${slug}.md`;
}

/** Reverse: repo path → site slug. Mirrors `repoPathToSitePath` in rehype-link-rewrite but local. */
export function sourcePathToSlug(repoRelPath: string): string | null {
  if (repoRelPath === "README.md") return "getting-started";
  if (repoRelPath === "CONTRIBUTING.md") return "contributing";
  if (!repoRelPath.startsWith("docs/")) return null;
  if (
    repoRelPath.startsWith("docs/archive/") ||
    repoRelPath.startsWith("docs/superpowers/")
  ) {
    return null;
  }
  const inside = repoRelPath.slice("docs/".length);
  if (!inside.endsWith(".md")) return null;
  const noExt = inside.slice(0, -".md".length);
  if (!noExt.includes("/")) return `reference/${noExt}`;
  if (noExt.endsWith("/README")) return noExt.slice(0, -"/README".length);
  return noExt;
}

/**
 * Per-spec U6 motif assignment by docs section. Used as the chapter mark for the
 * first heading of a top-level section, and on /404 / hero (plate-frame).
 */
export function motifForSlug(slug: string): MotifId {
  if (slug === "getting-started") return "cornu-ammonis";
  if (slug === "contributing") return "marginalia";
  if (slug.startsWith("capture/")) return "cornu-ammonis";
  if (slug === "reference/redaction") return "fasciculus";
  if (slug === "reference/lifecycle" || slug === "reference/schema") {
    return "trisynaptic-circuit";
  }
  if (slug.startsWith("reference/")) return "sectio-coronalis";
  return "sectio-coronalis";
}

/** Friendly default title from a slug, used as a last-resort fallback when neither
 *  frontmatter nor an h1 in the body supplies one. */
export function defaultTitleForSlug(slug: string): string {
  if (slug === "getting-started") return "Getting started";
  if (slug === "contributing") return "Contributing";
  const last = slug.split("/").pop() ?? slug;
  return last.replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Extract the first markdown h1 from raw entry body, before the rehype pipeline
 *  rewrites anything. Returns undefined if none found. Used by the sidebar so that
 *  entry titles are the actual page H1 (e.g. "MCP Tool Reference") rather than a
 *  Title Case-mangled slug ("Mcp Reference"). */
export function firstMarkdownH1(body: string | undefined): string | undefined {
  if (!body) return undefined;
  for (const line of body.split(/\r?\n/)) {
    const m = line.match(/^#\s+(.+?)\s*$/);
    if (m) return m[1];
  }
  return undefined;
}

/** Resolve an entry's display title via: frontmatter.title → first markdown h1 →
 *  defaultTitleForSlug. Same chain that pages/docs/[...slug].astro uses to render
 *  the page heading; sidebar/index must agree. */
function resolveTitle(
  data: Record<string, unknown> | undefined,
  body: string | undefined,
  slug: string,
): string {
  const frontmatterTitle = data?.title as string | undefined;
  if (frontmatterTitle) return frontmatterTitle;
  const h1 = firstMarkdownH1(body);
  if (h1) return h1;
  return defaultTitleForSlug(slug);
}

/**
 * Build the docs sidebar by sorting entries into Capture / Reference / Contributing /
 * Getting started sections. Each entry is the slug-derived URL.
 */
export function buildSidebar(
  docsEntries: CollectionEntry<"docs">[],
  rootEntries: CollectionEntry<"rootDocs">[],
  contribEntries: CollectionEntry<"contributing">[],
): SidebarSection[] {
  const capture: SidebarEntry[] = [];
  const reference: SidebarEntry[] = [];

  for (const entry of docsEntries) {
    const slug = entry.id.replace(/\.md$/, "");
    if (
      slug.startsWith("archive/") ||
      slug.startsWith("superpowers/") ||
      slug.startsWith("initial-research/")
    ) continue;
    // Top-level docs/<x>.md becomes reference/<x>.
    const segments = slug.split("/");
    const last = segments[segments.length - 1];
    let routeSlug: string;
    let isSectionIndex = false;
    if (segments.length === 1) {
      routeSlug = `reference/${slug}`;
    } else if (last === "README" || last === "readme") {
      routeSlug = segments.slice(0, -1).join("/");
      isSectionIndex = true;
    } else {
      routeSlug = slug;
    }
    // Drop section-index README entries from the sidebar list — the section
    // heading itself already serves as the link to the index page (I2).
    if (isSectionIndex) continue;
    const title = resolveTitle(
      entry.data as Record<string, unknown>,
      (entry as unknown as { body?: string }).body,
      routeSlug,
    );
    const sidebarEntry: SidebarEntry = {
      slug: routeSlug,
      title,
      url: `/docs/${routeSlug}`,
    };
    if (routeSlug.startsWith("capture/") || routeSlug === "capture") {
      capture.push(sidebarEntry);
    } else {
      reference.push(sidebarEntry);
    }
  }

  capture.sort((a, b) => a.title.localeCompare(b.title));
  reference.sort((a, b) => a.title.localeCompare(b.title));

  const sections: SidebarSection[] = [];

  if (rootEntries.length > 0) {
    sections.push({
      id: "getting-started",
      label: "Getting started",
      caption: "principia",
      entries: rootEntries.map((e) => ({
        slug: e.id,
        title: resolveTitle(
          e.data as Record<string, unknown>,
          (e as unknown as { body?: string }).body,
          e.id,
        ),
        url: `/docs/${e.id}`,
      })),
    });
  }

  if (capture.length > 0) {
    sections.push({
      id: "capture",
      label: "Capture",
      caption: "cornu Ammonis",
      entries: capture,
    });
  }

  if (reference.length > 0) {
    sections.push({
      id: "reference",
      label: "Reference",
      caption: "sectio coronalis",
      entries: reference,
    });
  }

  if (contribEntries.length > 0) {
    sections.push({
      id: "contributing",
      label: "Contributing",
      caption: "marginalia",
      entries: contribEntries.map((e) => ({
        slug: e.id,
        title: resolveTitle(
          e.data as Record<string, unknown>,
          (e as unknown as { body?: string }).body,
          e.id,
        ),
        url: `/docs/${e.id}`,
      })),
    });
  }

  return sections;
}
