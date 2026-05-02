import type { AstroIntegration } from "astro";
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import path from "node:path";

/**
 * Mirrors small repo-root assets into public/ at build setup so they're
 * served at site root. Today: scripts/install.sh → /install.sh, the URL
 * advertised by the homepage hero `curl -fsSL ... | sh` snippet.
 *
 * Pairs with .gitignore entries that exclude the mirrored copies — source
 * of truth stays at scripts/install.sh, the public/ copy is a build artifact.
 */
export function copyPublicAssets(): AstroIntegration {
  return {
    name: "copy-public-assets",
    hooks: {
      "astro:config:setup": ({ logger }) => {
        const repoRoot = path.resolve(process.cwd(), "..");
        const publicDir = path.resolve(process.cwd(), "public");
        mkdirSync(publicDir, { recursive: true });

        const targets: Array<{ src: string; dest: string }> = [
          {
            src: path.join(repoRoot, "scripts", "install.sh"),
            dest: path.join(publicDir, "install.sh"),
          },
        ];

        for (const { src, dest } of targets) {
          if (!existsSync(src)) {
            logger.warn(`asset missing: ${src}; skipping`);
            continue;
          }
          copyFileSync(src, dest);
          logger.info(`copy-public-assets: ${path.relative(repoRoot, src)} → public/${path.basename(dest)}`);
        }
      },
    },
  };
}
