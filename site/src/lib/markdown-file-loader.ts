import type { Loader } from "astro/loaders";
import { glob as astroGlob } from "astro/loaders";
import path from "node:path";

/**
 * Wrap Astro's built-in `glob()` loader so each entry gets a `sourcePath` field
 * injected into its frontmatter. Remark/rehype plugins (link rewrite, image
 * resolve, edit-on-github) read `file.data.astro.frontmatter.sourcePath` to
 * resolve relative refs back to repo paths.
 *
 * `computeSourcePath(id)` is called with the loader-generated id (a posix path
 * relative to `base`, with no extension) and must return the repo-relative
 * posix path of the source file (e.g. "docs/capture/anti-patterns.md").
 */
export function withSourcePath(
  inner: Loader,
  computeSourcePath: (id: string) => string | null,
): Loader {
  return {
    name: `${inner.name}-with-sourcepath`,
    load: async (context) => {
      const wrapped = {
        ...context,
        parseData: async (args: { id: string; data: Record<string, unknown>; filePath?: string }) => {
          const sourcePath = computeSourcePath(args.id);
          // When sourcePath is null the entry is "excluded" — the file is still loaded
          // into the docs collection, but with no sourcePath in its frontmatter so the
          // remark/rehype plugins skip rewriting and `pages/docs/[...slug].astro`
          // filters it out of getStaticPaths so no route is generated. This keeps
          // archive / superpowers / initial-research files queryable internally
          // without exposing them as pages.
          if (sourcePath == null) {
            return context.parseData(args);
          }
          return context.parseData({
            ...args,
            data: { ...args.data, sourcePath },
          });
        },
      };
      // @ts-expect-error -- duck-typed loader context shape
      await inner.load(wrapped);
    },
  };
}

/** Doc-tree prefixes that are loaded into the collection but never rendered as pages.
 *  Mirror these in `pages/docs/[...slug].astro`'s getStaticPaths filter — that's where
 *  exclusion is actually enforced (the loader can only refuse to inject sourcePath). */
export const EXCLUDED_DOCS_PREFIXES = ["archive/", "superpowers/", "initial-research/"];

/** Glob loader for ../docs/**\/*.md with sourcePath injected per entry. Excluded
 *  prefixes are still loaded (Astro's glob loader doesn't accept array patterns), but
 *  receive no sourcePath and are filtered from routing. */
export function docsLoader(): Loader {
  const inner = astroGlob({
    pattern: "**/*.md",
    base: "../docs",
  });
  return withSourcePath(inner, (id) => {
    if (EXCLUDED_DOCS_PREFIXES.some((p) => id.startsWith(p))) return null;
    return `docs/${id}.md`;
  });
}

/** Single-file loader: wrap glob() to take a single repo-root file and rename its id. */
export function singleMarkdownLoader(opts: {
  /** path relative to Astro project root, e.g. "../README.md" */
  filePath: string;
  /** id to assign in the collection */
  id: string;
  /** repo-rel sourcePath for frontmatter */
  sourcePath: string;
}): Loader {
  const baseRel = path.posix.dirname(opts.filePath) || ".";
  const filename = path.posix.basename(opts.filePath);
  const inner = astroGlob({
    pattern: filename,
    base: baseRel,
    generateId: () => opts.id,
  });
  return withSourcePath(inner, () => opts.sourcePath);
}
