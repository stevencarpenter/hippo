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

/** Friendly default title from a slug, used when frontmatter doesn't supply one. */
export function defaultTitleForSlug(slug: string): string {
  if (slug === "getting-started") return "Getting started";
  if (slug === "contributing") return "Contributing";
  const last = slug.split("/").pop() ?? slug;
  return last.replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
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
    if (segments.length === 1) {
      routeSlug = `reference/${slug}`;
    } else if (last === "README" || last === "readme") {
      routeSlug = segments.slice(0, -1).join("/");
    } else {
      routeSlug = slug;
    }
    const title = entry.data.title ?? defaultTitleForSlug(routeSlug);
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
        title: e.data.title ?? "Getting started",
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
        title: e.data.title ?? "Contributing",
        url: `/docs/${e.id}`,
      })),
    });
  }

  return sections;
}
