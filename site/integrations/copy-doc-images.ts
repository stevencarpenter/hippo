import type { AstroIntegration } from "astro";
import { mkdirSync, copyFileSync, existsSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

/**
 * Astro integration that recursively mirrors ../docs/diagrams/** into
 * public/docs-images/ at config:setup. Pairs with rehype-image-resolve, which
 * rewrites the <img> src.
 *
 * Excalidraw .excalidraw source files are skipped — they're not browser-renderable;
 * exported PNG/SVG companions get copied if present.
 *
 * Recursive walk preserves subdirectory structure so future
 * docs/diagrams/<topic>/foo.png references survive.
 */
export function copyDocImages(): AstroIntegration {
  return {
    name: "copy-doc-images",
    hooks: {
      "astro:config:setup": ({ logger }) => {
        const src = path.resolve(process.cwd(), "..", "docs", "diagrams");
        const dest = path.resolve(process.cwd(), "public", "docs-images");
        if (!existsSync(src)) {
          logger.warn(`docs/diagrams not found at ${src}; skipping image copy`);
          return;
        }
        mkdirSync(dest, { recursive: true });
        const count = walkAndCopy(src, dest, "");
        logger.info(`copy-doc-images: mirrored ${count} files into public/docs-images`);
      },
    },
  };
}

/**
 * Walk `srcDir`, copying every file to `destDir`/<relative-subpath>. Returns count.
 * Skips .excalidraw source files. Creates intermediate dirs as needed.
 */
function walkAndCopy(srcDir: string, destDir: string, rel: string): number {
  let count = 0;
  for (const entry of readdirSync(srcDir)) {
    const sf = path.join(srcDir, entry);
    const stat = statSync(sf);
    if (stat.isDirectory()) {
      const nestedSrc = sf;
      const nestedDest = path.join(destDir, entry);
      mkdirSync(nestedDest, { recursive: true });
      count += walkAndCopy(nestedSrc, nestedDest, path.posix.join(rel, entry));
    } else if (stat.isFile()) {
      if (entry.endsWith(".excalidraw")) continue;
      copyFileSync(sf, path.join(destDir, entry));
      count++;
    }
  }
  return count;
}
