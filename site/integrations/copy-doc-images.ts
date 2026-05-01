import type { AstroIntegration } from "astro";
import { mkdirSync, copyFileSync, existsSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

/**
 * Astro integration that mirrors ../docs/diagrams/** into public/docs-images/ at
 * config:setup. Pairs with rehype-image-resolve, which rewrites the <img> src.
 *
 * Excalidraw .excalidraw source files are skipped — they're not browser-renderable;
 * exported PNG/SVG companions get copied if present.
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
        let count = 0;
        for (const f of readdirSync(src)) {
          if (f.endsWith(".excalidraw")) continue;
          const sf = path.join(src, f);
          if (statSync(sf).isFile()) {
            copyFileSync(sf, path.join(dest, f));
            count++;
          }
        }
        logger.info(`copy-doc-images: mirrored ${count} files into public/docs-images`);
      },
    },
  };
}
