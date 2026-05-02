import path from "node:path";
import type { VFile } from "vfile";
import { isAstroVFile } from "./remark/types.ts";

/**
 * Resolve the repo-relative POSIX source path for a markdown file as seen by a
 * remark/rehype plugin. Tries two sources in order:
 *
 * 1. `file.data.astro.frontmatter.sourcePath` — set by the markdown-file-loader
 *    or directly in YAML frontmatter (kept for backwards compatibility / tests).
 * 2. `file.path` (absolute path) — Astro's markdown pipeline always sets this
 *    when running through a content collection. We strip the repo root to get
 *    a path like "docs/capture/anti-patterns.md" or "README.md".
 *
 * Returns undefined when no sourcePath could be determined (e.g. a synthetic
 * vfile in a test that doesn't construct either).
 *
 * The repo-root computation pins to the directory two levels above the Astro
 * project (`/site/..` → repo root). That works for both the build (cwd is
 * `/site`) and Astro's own internal calls (file.path is absolute).
 */
export function resolveSourcePath(file: VFile): string | undefined {
  if (isAstroVFile(file)) {
    const fromFrontmatter = file.data.astro.frontmatter.sourcePath;
    if (typeof fromFrontmatter === "string" && fromFrontmatter.length > 0) {
      return fromFrontmatter;
    }
  }
  if (file.path) {
    const abs = path.resolve(file.path);
    const repoRoot = path.resolve(process.cwd(), "..");
    if (abs.startsWith(repoRoot + path.sep)) {
      return abs.slice(repoRoot.length + 1).split(path.sep).join("/");
    }
  }
  return undefined;
}
