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
  const inside = p.slice("docs/".length);
  if (!inside.endsWith(".md")) return null;
  const noExt = inside.slice(0, -".md".length);
  if (!noExt.includes("/")) return `/docs/reference/${noExt}`;
  if (noExt.endsWith("/README")) return `/docs/${noExt.slice(0, -"/README".length)}`;
  return `/docs/${noExt}`;
}

const GITHUB_BLOB_RE =
  /^https?:\/\/github\.com\/(?:stevencarpenter|sjcarpenter)\/hippo\/blob\/[^/]+\/(.+?)(#.*)?$/;

/**
 * Rehype plugin that rewrites repo-relative markdown links to site-relative URLs and
 * marks external links with target="_blank" + rel + arrow glyph.
 *
 * Source path of the current document is read from frontmatter.sourcePath, set by the
 * docs page loader (POSIX path of the .md within the repo).
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

      if (href.startsWith("#")) return;
      if (href.startsWith("/")) return;
      if (href.startsWith("mailto:") || href.startsWith("tel:")) return;

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

      if (/^https?:\/\//.test(href)) {
        props.target = "_blank";
        props.rel = "noopener";
        const last = node.children[node.children.length - 1];
        const wantsArrow = !(last && last.type === "text" && last.value.endsWith("↗"));
        if (wantsArrow) {
          node.children.push({ type: "text", value: " ↗" });
        }
        return;
      }

      if (!sourcePath) return;

      const hashIdx = href.indexOf("#");
      const linkPath = hashIdx >= 0 ? href.slice(0, hashIdx) : href;
      const fragment = hashIdx >= 0 ? href.slice(hashIdx) : "";

      // Accept .md targets and dir targets that point inside docs/.
      const isMdLink = linkPath.endsWith(".md");
      const isDirLink = linkPath.endsWith("/");
      if (!isMdLink && !isDirLink) return;

      const sourceDir = path.posix.dirname(sourcePath);
      const resolved = path.posix.normalize(path.posix.join(sourceDir, linkPath));
      const site = isMdLink ? repoPathToSitePath(resolved) : null;
      if (site) {
        props.href = `${site}${fragment}`;
        return;
      }
      // Resolved path lives in an excluded section (archive, superpowers, etc.)
      // or is a directory link. Redirect to GitHub so the link doesn't 404.
      if (resolved.startsWith("docs/")) {
        const slash = isDirLink ? "" : "";
        const treeOrBlob = isDirLink ? "tree" : "blob";
        props.href = `https://github.com/stevencarpenter/hippo/${treeOrBlob}/main/${resolved}${slash}${fragment}`;
        props.target = "_blank";
        props.rel = "noopener";
      }
    });
  };
};
