import type { Plugin } from "unified";
import type { Root, Element } from "hast";
import { visit } from "unist-util-visit";
import path from "node:path";
import { isAstroVFile } from "../remark/types.ts";

/**
 * Maps a repo-relative POSIX path (e.g. "docs/capture/anti-patterns.md", "README.md",
 * "CONTRIBUTING.md", "docs/redaction.md") to its site URL.
 *
 * Conventions:
 *   README.md           -> /docs/getting-started
 *   CONTRIBUTING.md     -> /docs/contributing
 *   docs/<x>.md         -> /docs/reference/<x>     (top-level docs/*.md = reference)
 *   docs/<dir>/<x>.md   -> /docs/<dir>/<x>
 *   docs/<dir>/README.md-> /docs/<dir>             (section index)
 */
export function repoPathToSitePath(repoRelPath: string): string | null {
  const p = repoRelPath.replace(/\\/g, "/").replace(/^\.\//, "");
  if (p === "README.md") return "/docs/getting-started";
  if (p === "CONTRIBUTING.md") return "/docs/contributing";
  if (!p.startsWith("docs/")) return null;
  if (p.startsWith("docs/archive/") || p.startsWith("docs/superpowers/")) return null;
  if (p.startsWith("docs/initial-research/")) return null;
  const inside = p.slice("docs/".length);
  if (!inside.endsWith(".md")) return null;
  const noExt = inside.slice(0, -".md".length);
  if (!noExt.includes("/")) return `/docs/reference/${noExt}`;
  if (noExt.endsWith("/README")) return `/docs/${noExt.slice(0, -"/README".length)}`;
  return `/docs/${noExt}`;
}

const GITHUB_REPO = "stevencarpenter/hippo";
const GITHUB_BLOB_RE =
  /^https?:\/\/github\.com\/(?:stevencarpenter|sjcarpenter)\/hippo\/blob\/[^/]+\/(.+?)(#.*)?$/;

function ghBlob(repoPath: string, fragment = ""): string {
  return `https://github.com/${GITHUB_REPO}/blob/main/${repoPath}${fragment}`;
}
function ghTree(repoPath: string, fragment = ""): string {
  // Strip any trailing slash so the URL is canonical.
  const clean = repoPath.replace(/\/$/, "");
  return `https://github.com/${GITHUB_REPO}/tree/main/${clean}${fragment}`;
}

function appendArrow(node: Element): void {
  const last = node.children[node.children.length - 1];
  const wantsArrow = !(last && last.type === "text" && last.value.endsWith("↗"));
  if (wantsArrow) {
    node.children.push({ type: "text", value: " ↗" });
  }
}

/**
 * Rehype plugin that rewrites repo-relative markdown links to site-relative URLs and
 * marks external links with target="_blank" + rel + arrow glyph.
 *
 * Source path of the current document is read from frontmatter.sourcePath, set by the
 * docs page loader (POSIX path of the .md within the repo).
 *
 * Behavior matrix for relative links inside a doc with sourcePath set:
 *
 *   foo.md        — included docs       -> /docs/...
 *   foo.md        — excluded section    -> github.com/.../blob/...
 *   subdir/       — section w/ README   -> /docs/<subdir>      (section index)
 *   subdir/       — excluded section    -> github.com/.../tree/...
 *   any other     — repo file           -> github.com/.../blob/...    (with ↗)
 *   #anchor       — anchor only         -> unchanged
 *   /absolute     — site-absolute       -> unchanged
 *   mailto:/tel:  — protocol            -> unchanged
 */
export const rehypeLinkRewrite: Plugin<[], Root> = () => {
  return (tree, file) => {
    let sourcePath: string | undefined;
    if (isAstroVFile(file)) {
      sourcePath = file.data.astro.frontmatter.sourcePath as string | undefined;
    }

    visit(tree, "element", (node: Element) => {
      if (node.tagName !== "a") return;
      const props = node.properties ?? {};
      const href = typeof props.href === "string" ? props.href : "";
      if (!href) return;

      // Pass-through cases.
      if (href.startsWith("#")) return;
      if (href.startsWith("/")) return;
      if (href.startsWith("mailto:") || href.startsWith("tel:")) return;

      // Already-absolute GitHub blob URL pointing into our repo: site-rewrite if possible.
      const ghMatch = GITHUB_BLOB_RE.exec(href);
      if (ghMatch) {
        const repoPath = ghMatch[1];
        const fragment = ghMatch[2] ?? "";
        const site = repoPathToSitePath(repoPath);
        if (site) {
          props.href = `${site}${fragment}`;
          return;
        }
      }

      // External absolute URL (any non-blob http(s)): mark + arrow.
      if (/^https?:\/\//.test(href)) {
        props.target = "_blank";
        props.rel = "noopener";
        appendArrow(node);
        return;
      }

      // Below this line: relative URL. We need a sourcePath to resolve against.
      if (!sourcePath) return;

      // Don't try to rewrite query-only links.
      if (href.startsWith("?")) return;

      const hashIdx = href.indexOf("#");
      const linkPath = hashIdx >= 0 ? href.slice(0, hashIdx) : href;
      const fragment = hashIdx >= 0 ? href.slice(hashIdx) : "";
      if (!linkPath) return;

      const sourceDir = path.posix.dirname(sourcePath);
      const resolved = path.posix.normalize(path.posix.join(sourceDir, linkPath));

      const isMdLink = linkPath.endsWith(".md");
      const isDirLink = linkPath.endsWith("/");

      if (isMdLink) {
        const site = repoPathToSitePath(resolved);
        if (site) {
          props.href = `${site}${fragment}`;
          return;
        }
        // .md link that isn't site-mappable — could be an excluded-section
        // doc (docs/archive/...) or a non-docs README (extension/firefox/README.md,
        // otel/README.md, CLAUDE.md, etc.). Either way, send to GitHub blob so the
        // link lands somewhere instead of resolving to /docs/<broken>.
        props.href = ghBlob(resolved, fragment);
        props.target = "_blank";
        props.rel = "noopener";
        appendArrow(node);
        return;
      }

      if (isDirLink) {
        // Section index: try mapping <dir>/README.md to its section URL.
        const dirNoSlash = resolved.replace(/\/$/, "");
        const readmePath = `${dirNoSlash}/README.md`;
        const site = repoPathToSitePath(readmePath);
        if (site) {
          props.href = `${site}${fragment}`;
          return;
        }
        // Excluded section or non-docs directory: GitHub tree URL.
        if (dirNoSlash.startsWith("docs/")) {
          props.href = ghTree(dirNoSlash, fragment);
          props.target = "_blank";
          props.rel = "noopener";
        }
        return;
      }

      // Other relative path (LICENSE, scripts/install.sh, crates/.../schema.sql, etc.).
      // These are valid repo files; rewrite to GitHub blob so the link lands somewhere
      // useful instead of resolving to a non-existent /docs/... URL.
      props.href = ghBlob(resolved, fragment);
      props.target = "_blank";
      props.rel = "noopener";
      appendArrow(node);
    });
  };
};
