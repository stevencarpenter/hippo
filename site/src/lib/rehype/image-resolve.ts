import type { Plugin } from "unified";
import type { Element, Root } from "hast";
import { visit } from "unist-util-visit";
import path from "node:path";
import { isAstroVFile } from "../remark/types.ts";

/**
 * Rewrites relative <img src="../diagrams/foo.png"> references in docs markdown to
 * the site-local path /docs-images/foo.png. The copy-doc-images integration mirrors
 * docs/diagrams/** into public/docs-images/ at build time so the file actually exists.
 *
 * No raw.githubusercontent.com fallback (E3): the site is a self-contained artifact.
 */
export const rehypeImageResolve: Plugin<[], Root> = () => {
  return (tree, file) => {
    let sourcePath: string | undefined;
    if (isAstroVFile(file)) {
      sourcePath = file.data.astro.frontmatter.sourcePath as string | undefined;
    }
    if (!sourcePath) return;
    const sourceDir = path.posix.dirname(sourcePath);
    visit(tree, "element", (node: Element) => {
      if (node.tagName !== "img") return;
      const props = node.properties ?? {};
      const src = typeof props.src === "string" ? props.src : "";
      if (!src) return;
      if (/^https?:\/\//.test(src)) return;
      if (src.startsWith("/")) return;
      const resolved = path.posix.normalize(path.posix.join(sourceDir, src));
      const filename = path.posix.basename(resolved);
      props.src = `/docs-images/${filename}`;
      props.loading = "lazy";
      props.decoding = "async";
    });
  };
};
