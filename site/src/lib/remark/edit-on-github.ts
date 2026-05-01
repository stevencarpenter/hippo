import type { Plugin } from "unified";
import type { Root } from "mdast";
import { isAstroVFile } from "./types.ts";
import { gitLastUpdated } from "./git-timestamp.ts";

/**
 * Remark plugin: reads `file.data.astro.frontmatter.sourcePath` (set by the docs page
 * loader) and writes back `lastUpdated` (YYYY-MM-DD) and `editPath`
 * (the GitHub blob URL) so layouts can render the "Edit on GitHub" footer.
 *
 * This is a remark plugin, not a rehype plugin (E2): rehype runs after Astro snapshots
 * frontmatter, so writes there don't propagate to the page component.
 */
export const remarkEditOnGithub: Plugin<[], Root> = () => {
  return (_tree, file) => {
    if (!isAstroVFile(file)) return;
    const fm = file.data.astro.frontmatter;
    const sourcePath = fm.sourcePath as string | undefined;
    if (!sourcePath) return;
    if (!fm.lastUpdated) {
      const updated = gitLastUpdated(sourcePath);
      if (updated) fm.lastUpdated = updated;
    }
    if (!fm.editPath) {
      fm.editPath = `https://github.com/stevencarpenter/hippo/blob/main/${sourcePath}`;
    }
  };
};
