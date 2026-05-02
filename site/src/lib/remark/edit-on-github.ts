import type { Plugin } from "unified";
import type { Root } from "mdast";
import { isAstroVFile } from "./types.ts";
import { gitLastUpdated } from "./git-timestamp.ts";
import { resolveSourcePath } from "../source-path.ts";

/**
 * Remark plugin: derives the source file's repo-relative path (from
 * `file.path` or frontmatter), then writes `lastUpdated` (YYYY-MM-DD) and
 * `editPath` (GitHub blob URL) into frontmatter so layouts can render the
 * "Edit on GitHub" footer.
 *
 * This is a remark plugin, not a rehype plugin (E2): rehype runs after Astro
 * snapshots frontmatter, so writes there don't propagate to the page
 * component.
 */
export const remarkEditOnGithub: Plugin<[], Root> = () => {
  return (_tree, file) => {
    if (!isAstroVFile(file)) return;
    const sourcePath = resolveSourcePath(file);
    if (!sourcePath) return;
    const fm = file.data.astro.frontmatter;
    if (!fm.lastUpdated) {
      const updated = gitLastUpdated(sourcePath);
      if (updated) fm.lastUpdated = updated;
    }
    if (!fm.editPath) {
      fm.editPath = `https://github.com/stevencarpenter/hippo/blob/main/${sourcePath}`;
    }
  };
};
