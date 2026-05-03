import { execFileSync } from "node:child_process";
import path from "node:path";

/**
 * Batch git-log strategy: one fork loads "every file's most recent commit date" into
 * a Map, subsequent calls are O(1) lookups. For ~83 markdown files this cuts overhead
 * from ~83 forks to 1.
 *
 * Security: uses `execFileSync` with an args array — never `execSync` with template
 * strings, even though `repoRelPath` here is metadata-controlled.
 */
let batchCache: Map<string, string> | null = null;

function loadBatchCache(): Map<string, string> {
  if (batchCache) return batchCache;
  const repoRoot = path.resolve(process.cwd(), "..");
  batchCache = new Map();
  try {
    // Walk HEAD only (NOT --all). With --all, `first-seen-wins` could pick
    // up the latest edit on an unmerged feature branch as a file's "last
    // updated" date, even though that edit isn't shipped on the deploy
    // ref. HEAD-only ensures the timestamp reflects what's actually live.
    const out = execFileSync(
      "git",
      ["log", "--name-only", "--format=COMMIT %ci"],
      { cwd: repoRoot, encoding: "utf8", maxBuffer: 50 * 1024 * 1024 },
    );
    let currentDate = "";
    for (const line of out.split("\n")) {
      if (line.startsWith("COMMIT ")) {
        currentDate = line.slice(7, 17);
      } else if (line.trim() && !batchCache.has(line)) {
        batchCache.set(line, currentDate);
      }
    }
  } catch {
    // Empty cache — every lookup returns "". Builds outside a git checkout still work.
  }
  return batchCache;
}

export function gitLastUpdated(repoRelPath: string): string {
  return loadBatchCache().get(repoRelPath) ?? "";
}
