# Publish the Understand-Anything Dashboard to hippobrain.org/understand/ — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Run this in a Claude Code session opened **inside `~/projects/hippo`**. Also ingestible by `batallion brief --from <this-file>` (`### Task N:` convention).

**Goal:** Serve the interactive knowledge-graph dashboard that `/understand` produced for hippo as static docs, behind an **on-brand landing page at https://hippobrain.org/understand/** with the full-screen dashboard at **/understand/app/** — riding the existing `site/` Astro → GitHub Pages → Cloudflare pipeline, linked from the site header.

**Architecture:** The dashboard is a Vite + React SPA with a built-in *demo mode* that drops the dev-server token gate and reads the graph as a static asset. We build it **portably** (`--base=./` + relative `VITE_GRAPH_URL=./knowledge-graph.json`) so it works at the `/understand/app/` subpath, commit the bundle into `site/public/understand/app/`, drop hippo's `knowledge-graph.json` beside it, add an **on-brand Astro landing page** at `/understand/` that introduces the map and links into the full-screen app, link the landing from the header, and let the existing Site Deploy workflow publish it. Refreshing docs later = re-run `/understand` + recopy one JSON.

**Tech Stack:** understand-anything dashboard (Vite 6, React 19), Astro 6 (`site/`), GitHub Pages, Cloudflare (hippobrain.org), Node ≥ 22 + pnpm via mise, Bash.

> **Reconciliation note (2026-05-23, post-brainstorm).** This plan was reconciled with a design session. Three changes vs. the original draft:
> 1. **Graph is now the everything-scope snapshot — 1,285 nodes / 538 files** (was 845, core-scope). It already uses **relative** `filePath`s, so the publish-script sanitization (Task 2) is a no-op safety net here.
> 2. The dashboard moves to **`/understand/app/`** behind a new **on-brand landing page at `/understand/`** (Task 4). This avoids an output collision (an Astro `understand.astro` emits `dist/understand/index.html`, which would clash with the SPA's `index.html` at the same path).
> 3. **Source preview via GitHub raw (Task 6) is REQUIRED**, not optional — node "View source" renders inline from `raw.githubusercontent.com/stevencarpenter/hippo@<analyzed-commit>`.
>
> Also: hippo **intentionally commits** the root `.understand-anything/` graph (tracked as of `50aec2d`), so — unlike the generic recipe — we do **not** gitignore it. The published copy under `site/public/understand/app/` is a second, path-sanitized copy.

---

## Hippo specifics (this copy)
These are the verified values for this repo — already baked into the scripts/tasks below.

| Thing | Value |
|---|---|
| GitHub repo | `stevencarpenter/hippo` |
| Current branch | `understand-everything` (deploy triggers on **`main`** — see Task 5) |
| Astro site dir | `site/` (`site/astro.config.mjs`, `site: "https://hippobrain.org"`, no `base` ⇒ root `/`) |
| Served URLs | **`/understand/`** (on-brand landing) + **`/understand/app/`** (full-screen dashboard) |
| Bundle output dir | `site/public/understand/app/` (Astro copies `public/` → `dist/`) |
| Landing page | `site/src/pages/understand.astro` (new; uses `Tier1` layout, like every other top-level page) |
| Knowledge graph | **repo root** `/.understand-anything/knowledge-graph.json` — **READY: 1,285 nodes / 538 files (everything-scope), relative paths** (one level *above* `site/`) |
| Scripts go in | `site/scripts/` (alongside `generate-og.js`) |
| Nav/header | `site/src/components/Header.astro` (nav `<ul>` ~lines 22-37; GitHub link ~line 34) |
| Deploy workflow | `.github/workflows/site-deploy.yml` — push to `main`, paths include `site/**`; build = `pnpm build` = `astro build && pagefind --site dist` |
| This plan's path | `docs/superpowers/plans/…` — **excluded** from deploy triggers (`!docs/superpowers/**`), so committing it won't rebuild the site |

**The path twist:** unlike a single-root repo, hippo's graph lives at the **repo root** but the website is in **`site/`**. The scripts below resolve `SITE_ROOT` (= `site/`) and `REPO_ROOT` (= hippo root, where the graph is) separately. Don't "simplify" them to a single root.

**Hippo CI caveats (handled in Task 5):**
- `site-deploy.yml` triggers on **push to `main`**, not `understand-everything`. The bundle deploys only once it lands on `main` (PR/merge).
- `pnpm build` runs **pagefind** over `dist/` — it will index `dist/understand/app/index.html` (the SPA shell) and may add one junk search hit. Task 5 Step 1 adds a pagefind exclude. (The `/understand/` landing is a real content page and *should* be indexed.)
- If `site-ci.yml` runs **linkinator** over `dist`, it may crawl `/understand/app/`; add a `--skip "/understand/app/"` if it complains (Task 5 Step 4).

---

## Spec (condensed)

### Goals
1. Portable static build of the dashboard into `site/public/understand/app/`.
2. Sanitize + copy hippo's root `knowledge-graph.json` beside the bundle.
3. **On-brand landing page** at `/understand/` (layer legend, stats, "snapshot as of `<commit>`") with an "Explore the interactive graph →" button into `/understand/app/`.
4. Link the landing from `site/src/components/Header.astro`.
5. Deploy via the existing Site Deploy workflow; verify at `https://hippobrain.org/understand/`.
6. **Source preview from `raw.githubusercontent.com/stevencarpenter/hippo`** (required for public docs).
7. A documented refresh loop.

### Non-goals
No new Pages site / DNS (ride the existing one); no server component; no changes to the `/understand` pipeline or graph schema; no re-theming of the dashboard interior (the on-brand landing is the consistency layer).

### Verified facts (plugin v2.7.4 — don't re-investigate)
- Demo mode skips the token gate: `dashboard/src/App.tsx:37,71,96-98`.
- Demo data URL: `App.tsx:50-64` returns `VITE_GRAPH_URL` if set, else an **absolute** `/knowledge-graph.json` (breaks at a subpath) → we must set it **relative**.
- Demo build config `dashboard/vite.config.demo.ts` hardcodes `base:"/demo/"` (we override with `--base=./`) and has no dev middleware (pure static).
- Code viewer is server-only by default: `dashboard/src/components/CodeViewer.tsx:26-29,90-97` (demo branch shows a "run locally" message). Task 6 repoints it at GitHub raw.
- Path sanitization is dev-server-only (`dashboard/vite.config.ts:322-335`); replicate it for static (Task 2). Our graph is already relative, so this is a safety net.
- Plugin lives in a cache dir wiped on update (`~/.claude/plugins/cache/understand-anything/understand-anything/<version>/`) → the built bundle is a **committed artifact**; the build script re-locates the latest version each run.
- Trailing slash required (`--base=./` resolves assets/graph relative to the doc dir; serve at `/understand/app/`).

### Prerequisites
- In `~/projects/hippo`; understand-anything plugin installed; **graph already built** (it is — 1,285 nodes at root `.understand-anything/`); Node ≥ 22 + pnpm (direct or via mise).

### Acceptance criteria
- [ ] `bash site/scripts/build-understand-bundle.sh` → `site/public/understand/app/index.html` with **relative** asset URLs.
- [ ] `node site/scripts/publish-understand-graph.mjs` → `site/public/understand/app/knowledge-graph.json`, **no absolute paths**.
- [ ] Serving the bundle locally renders the layer overview + Tour (no token gate).
- [ ] `/understand/` landing renders (Tier1 + motif), shows live stats (1,285 nodes / 538 files / 10 layers) and an "Explore →" button to `/understand/app/`.
- [ ] Header shows a "Code Map" link → `/understand/`.
- [ ] Live at `https://hippobrain.org/understand/` after merge to `main`.
- [ ] (Task 6) "View source" loads from `raw.githubusercontent.com/stevencarpenter/hippo@<commit>`.

---

### Task 1: Portable build script (hippo-aware paths)

**Files:** Create `site/scripts/build-understand-bundle.sh`

- [ ] **Step 1: Create the script**

```bash
cat > site/scripts/build-understand-bundle.sh <<'SCRIPT'
#!/usr/bin/env bash
# Build the understand-anything dashboard as a PORTABLE static bundle into
# hippo's site/public/understand/app/. This script lives in site/scripts/.
#   SITE_ROOT = the Astro site (site/)            <- bundle goes to its public/understand/app/
#   REPO_ROOT = hippo repo root (one level up)    <- where the knowledge graph is
# Re-run after an understand-anything plugin update.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SITE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SITE_ROOT/.." && pwd)"
OUT_DIR="$SITE_ROOT/public/understand/app"

# Default source-preview base to the analyzed commit (Task 6) so source lines
# match the indexed graph. Override by exporting UNDERSTAND_SOURCE_BASE_URL.
if [ -z "${UNDERSTAND_SOURCE_BASE_URL:-}" ] && [ -f "$REPO_ROOT/.understand-anything/meta.json" ]; then
  REF="$(node -e "process.stdout.write(require('$REPO_ROOT/.understand-anything/meta.json').gitCommitHash||'')" 2>/dev/null || true)"
  [ -n "$REF" ] && UNDERSTAND_SOURCE_BASE_URL="https://raw.githubusercontent.com/stevencarpenter/hippo/$REF"
fi
[ -n "${UNDERSTAND_SOURCE_BASE_URL:-}" ] && echo "Source preview base: $UNDERSTAND_SOURCE_BASE_URL"

run_pnpm() {
  if pnpm --version >/dev/null 2>&1; then pnpm "$@";
  else mise exec pnpm@11.2.2 -- pnpm "$@"; fi
}

# Locate the installed dashboard (highest version; cache dir is volatile).
DASH="$(ls -d "$HOME/.claude/plugins/cache/understand-anything/understand-anything/"*/packages/dashboard 2>/dev/null | sort -V | tail -1)"
[ -n "${DASH:-}" ] || { echo "ERROR: understand-anything dashboard not found. Install the plugin and run /understand first." >&2; exit 1; }
PLUGIN_ROOT="$(cd "$DASH/../.." && pwd)"
echo "Using dashboard: $DASH"

# Ensure deps + the core package the dashboard imports are built.
(
  cd "$PLUGIN_ROOT"
  run_pnpm install >/dev/null 2>&1 || true       # tolerate ERR_PNPM_IGNORED_BUILDS
  run_pnpm approve-builds --all >/dev/null 2>&1 || true
  run_pnpm --filter @understand-anything/core build
)

# Portable build env: relative base + RELATIVE graph url (default is absolute).
TMP_ENV="$DASH/.env.production.local"
cat > "$TMP_ENV" <<EOF
VITE_GRAPH_URL=./knowledge-graph.json
VITE_META_URL=./meta.json
${UNDERSTAND_SOURCE_BASE_URL:+VITE_SOURCE_BASE_URL=$UNDERSTAND_SOURCE_BASE_URL}
EOF
trap 'rm -f "$TMP_ENV"' EXIT

# Task 6 (REQUIRED for hippo): repoint the code viewer at a raw source base URL.
if [ -n "${UNDERSTAND_SOURCE_BASE_URL:-}" ]; then
  node "$SCRIPT_DIR/patch-understand-source-viewer.mjs" "$DASH/src/components/CodeViewer.tsx"
fi

(
  cd "$DASH"
  run_pnpm exec tsc -b
  run_pnpm exec vite build --base=./ --config vite.config.demo.ts
)

# Publish bundle (minus the plugin's sample graph — Task 2 adds hippo's).
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
cp -R "$DASH/dist/." "$OUT_DIR/"
rm -f "$OUT_DIR/knowledge-graph.json" "$OUT_DIR/meta.json"
echo "OK: bundle -> $OUT_DIR (run publish-understand-graph.mjs next)"
SCRIPT
chmod +x site/scripts/build-understand-bundle.sh
```

- [ ] **Step 2: Run it**

Create the Task 6 patch script first (`site/scripts/patch-understand-source-viewer.mjs`), then:

```bash
bash site/scripts/build-understand-bundle.sh
```
The script auto-derives the source-preview commit from `.understand-anything/meta.json` (override via an exported `UNDERSTAND_SOURCE_BASE_URL`). Expected: `Source preview base: …/<commit>`, then `OK: patched .../CodeViewer.tsx`, then `OK: bundle -> .../site/public/understand/app ...`, exit 0. (Core is already built from your `/understand` run, so this is mostly the Vite build; ~30-90s.)

- [ ] **Step 3: Verify static + portable**

```bash
test -f site/public/understand/app/index.html && echo "index: ok"
grep -RqlE "knowledge-graph\.json" site/public/understand/app/assets && echo "graph-fetch inlined: ok"
grep -Eo 'src="[^"]+"' site/public/understand/app/index.html | head
```
Expected: `index: ok`, `graph-fetch inlined: ok`, and `src=` paths start with `./assets/` (relative).

- [ ] **Step 4: Commit**

```bash
git add site/scripts/build-understand-bundle.sh
git commit -m "build: add portable understand-anything dashboard build script"
```

---

### Task 2: Graph publish + sanitize (root graph → site bundle)

**Files:** Create `site/scripts/publish-understand-graph.mjs`

- [ ] **Step 1: Create the script** (derives both roots from its own path — no git/child_process)

```bash
cat > site/scripts/publish-understand-graph.mjs <<'SCRIPT'
#!/usr/bin/env node
// Copy hippo's root knowledge graph into site/public/understand/app/, relativizing
// any absolute filePath (the static build skips the dev server's sanitization).
// This script lives in site/scripts/:
//   siteRoot = site/        repoRoot = hippo root (graph lives here)
import { readFileSync, writeFileSync, existsSync, mkdirSync, copyFileSync } from "node:fs";
import { resolve, basename, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const siteRoot = resolve(scriptDir, "..");
const repoRoot = resolve(scriptDir, "..", "..");
const src = resolve(repoRoot, ".understand-anything/knowledge-graph.json");
const outDir = resolve(siteRoot, "public/understand/app");
const out = resolve(outDir, "knowledge-graph.json");

if (!existsSync(src)) {
  console.error(`ERROR: ${src} not found. Run /understand at the hippo repo root first.`);
  process.exit(1);
}
if (!existsSync(outDir)) mkdirSync(outDir, { recursive: true });

const graph = JSON.parse(readFileSync(src, "utf8"));
let fixed = 0;
for (const node of graph.nodes ?? []) {
  if (typeof node.filePath !== "string") continue;
  const p = node.filePath;
  if (p.startsWith(repoRoot)) { node.filePath = p.slice(repoRoot.length).replace(/^[\\/]/, ""); fixed++; }
  else if (p.startsWith("/")) { node.filePath = basename(p); fixed++; } // abs outside repo -> filename
}
const leaked = (graph.nodes ?? []).filter(n => typeof n.filePath === "string" && n.filePath.startsWith("/"));
if (leaked.length) {
  console.error(`ERROR: ${leaked.length} absolute filePath(s) remain, e.g. ${leaked[0].filePath}`);
  process.exit(1);
}
writeFileSync(out, JSON.stringify(graph));
console.log(`OK: wrote ${out} (${graph.nodes?.length ?? 0} nodes, relativized ${fixed} paths)`);

for (const extra of ["meta.json", "config.json", "domain-graph.json"]) {
  const ex = resolve(repoRoot, ".understand-anything", extra);
  if (existsSync(ex)) { copyFileSync(ex, resolve(outDir, extra)); console.log(`  + ${extra}`); }
}
SCRIPT
chmod +x site/scripts/publish-understand-graph.mjs
```

- [ ] **Step 2: Run it**

Run: `node site/scripts/publish-understand-graph.mjs`
Expected: `OK: wrote .../site/public/understand/app/knowledge-graph.json (1285 nodes, relativized 0 paths)` + `+ meta.json`. (0 relativized is expected — our graph is already relative.)

- [ ] **Step 3: Verify no leaks**

Run: `node -e "const g=require('./site/public/understand/app/knowledge-graph.json'); console.log('nodes:',g.nodes.length,'abs:',g.nodes.filter(n=>typeof n.filePath==='string'&&n.filePath.startsWith('/')).length)"`
Expected: `nodes: 1285 abs: 0`

- [ ] **Step 4: Commit**

```bash
git add site/scripts/publish-understand-graph.mjs
git commit -m "build: add knowledge-graph publish + sanitize script"
```

---

### Task 3: Local end-to-end verification

**Files:** none (verification)

- [ ] **Step 1: Serve the bundle at a root and check fetches**

```bash
( cd site/public/understand/app && python3 -m http.server 8099 ) &
SERVER_PID=$!
sleep 1
curl -s -o /dev/null -w "index:%{http_code} graph:" http://localhost:8099/
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8099/knowledge-graph.json
kill "$SERVER_PID" 2>/dev/null || true
```
Expected: `index:200 graph:200`.

- [ ] **Step 2: Visual check**

Open `http://localhost:8099/` (or use the `screenshot-bug-hunt` skill / Playwright). Expected: layer overview renders (no token gate), search works, Tour advances. "View source" loads from GitHub raw (Task 6).

- [ ] **Step 3: Commit the bundle**

```bash
git add site/public/understand/app
git commit -m "docs: publish understand-anything dashboard bundle + graph"
```
Note: the root `.understand-anything/` graph stays tracked (hippo's convention); `site/public/understand/app/` holds the path-sanitized published copy.

---

### Task 4: On-brand landing page + header link

**Files:** Create `site/src/pages/understand.astro`; modify `site/src/components/Header.astro`

- [ ] **Step 1: Create the landing page** at `/understand/`

It uses the standard `Tier1` layout (every top-level page does) and reads the *published* graph at build time to render live stats + the layer legend, then links into the full-screen app. The 1.1 MB JSON is read only at build (Node frontmatter) — only the computed counts ship to the client.

```astro
---
import Tier1 from "../layouts/Tier1.astro";
import TrisynapticCircuit from "../components/motifs/TrisynapticCircuit.astro";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const graphPath = fileURLToPath(
  new URL("../../public/understand/app/knowledge-graph.json", import.meta.url),
);
const graph = JSON.parse(readFileSync(graphPath, "utf8"));
const fileLevel = new Set([
  "file", "config", "document", "service", "pipeline",
  "table", "schema", "resource", "endpoint",
]);
const nodeCount = graph.nodes.length;
const edgeCount = graph.edges.length;
const fileCount = graph.nodes.filter((n) => fileLevel.has(n.type)).length;
const layers = (graph.layers ?? []).map((l) => ({ name: l.name, count: l.nodeIds.length }));
const commit = (graph.project?.gitCommitHash ?? "").slice(0, 7) || "unknown";
---

<Tier1
  title="Code Map"
  description="An interactive map of the hippo codebase — every file, symbol, and dependency, generated by /understand."
>
  <article class="prose">
    <TrisynapticCircuit />
    <h1>The hippo codebase, mapped</h1>
    <p>
      An interactive knowledge graph of hippo's source — the Rust capture daemon,
      the Python enrichment brain, this site, and the docs — built
      automatically by the <code>/understand</code> tool. Explore the architecture
      by layer, follow the guided tour, or jump to any file's source on GitHub.
    </p>
    <p class="stats">
      <strong>{fileCount}</strong> files ·
      <strong>{nodeCount}</strong> nodes ·
      <strong>{edgeCount}</strong> edges ·
      <strong>{layers.length}</strong> layers
    </p>
    <ul class="layer-legend">
      {layers.map((l) => <li>{l.name} <span>{l.count}</span></li>)}
    </ul>
    <p>
      <a class="cta" href="/understand/app/">Explore the interactive graph →</a>
    </p>
    <p class="snapshot">
      Snapshot as of commit <code>{commit}</code>. Source links open the file on
      GitHub at that commit.
    </p>
  </article>
</Tier1>
```
(Style `.cta`, `.stats`, `.layer-legend`, `.snapshot` with the site's existing tokens — match the look of `why.astro` / `install.astro`. Keep it light; this is a contributor on-ramp, not a marketing splash.)

- [ ] **Step 2: Link "Code Map" from the header**

Run: `sed -n '18,40p' site/src/components/Header.astro` to find the nav block.
Preferred (data-driven): if a nav-items array feeds the list, add `{ label: "Code Map", href: "/understand/" }`. Otherwise add a hardcoded `<li><a href="/understand/">Code Map</a></li>` next to the GitHub link. (Site base is `/`; keep the trailing slash.)

- [ ] **Step 3: Verify against a real Astro build**

```bash
cd site
run_astro() { if pnpm --version >/dev/null 2>&1; then pnpm "$@"; else mise exec pnpm@11.2.2 -- pnpm "$@"; fi; }
run_astro install --frozen-lockfile
run_astro exec astro build
run_astro exec astro preview --port 4321 &
PREVIEW_PID=$!
sleep 2
curl -s -o /dev/null -w "landing:%{http_code} app:" http://localhost:4321/understand/
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:4321/understand/app/
kill "$PREVIEW_PID" 2>/dev/null || true
cd ..
```
Expected: `landing:200 app:200`. Open `http://localhost:4321/`, click "Code Map", confirm the landing renders with correct stats, then "Explore →" loads the dashboard.

- [ ] **Step 4: Commit**

```bash
git add site/src/pages/understand.astro site/src/components/Header.astro
git commit -m "feat(site): add Code Map landing page + header link"
```

---

### Task 5: Deploy via Site Deploy + verify live

**Files:** optional `site/pagefind.yml`; `.github/workflows/site-ci.yml` (only if linkinator complains)

- [ ] **Step 1: Keep pagefind from indexing the SPA shell** (optional but tidy)

```bash
printf 'glob: "**/*.html"\nexclude_selectors:\n  - "#root"\n' > site/pagefind.yml
```
Rationale: stops pagefind from turning `/understand/app/`'s empty SPA shell (`<div id="root">`) into a junk search result. The `/understand/` landing is a real page and stays indexed. If hippo already has a pagefind config, merge instead of overwrite.

- [ ] **Step 2: Land on `main`** (deploy triggers on push to `main`, not `understand-everything`)

Open a PR from `understand-everything` → `main` (or merge per your flow):
```bash
git push -u origin understand-everything
gh pr create --base main --head understand-everything \
  --title "docs: interactive code map at /understand/" \
  --body "On-brand landing at /understand/ + static understand-anything dashboard at /understand/app/ (1,285-node graph), linked from the header."
```
Merge it. Expected: `.github/workflows/site-deploy.yml` runs (push to `main`, `site/**` changed). Watch: `gh run watch`.

- [ ] **Step 3: Verify live**

```bash
curl -s -o /dev/null -w "landing:%{http_code} app:" https://hippobrain.org/understand/
curl -s -o /dev/null -w "%{http_code}\n" https://hippobrain.org/understand/app/
```
Expected: `landing:200 app:200`. Open `https://hippobrain.org/understand/`; click "Explore →"; confirm the 1,285-node graph renders. Purge Cloudflare cache for `/understand/*` if stale.

- [ ] **Step 4: If `site-ci.yml` linkinator fails on /understand/app/**

Add a skip to the `lint:links` script in `site/package.json` (append): `--skip "/understand/app/"`. Commit. Re-run CI. (Only if it actually flags the SPA.)

---

### Task 6: Source preview from GitHub raw (REQUIRED)

Restores click-to-view-source on the static site by reading files from `raw.githubusercontent.com/stevencarpenter/hippo`. Applied at build time (plugin cache is volatile). The build script (Task 1) invokes this patch when `UNDERSTAND_SOURCE_BASE_URL` is set — and Task 1 Step 2 sets it. Pin to the **analyzed commit** so source lines match the indexed graph.

**Files:** Create `site/scripts/patch-understand-source-viewer.mjs`

- [ ] **Step 1: Create the patch script** (anchored; fails loudly if the plugin changed)

```bash
cat > site/scripts/patch-understand-source-viewer.mjs <<'SCRIPT'
#!/usr/bin/env node
// Patch the dashboard CodeViewer (in the plugin cache copy) to fetch source from a
// base URL (raw.githubusercontent.com) instead of the dev server. Re-run each
// build; the plugin cache is volatile. Fails loudly if the plugin changed.
import { readFileSync, writeFileSync } from "node:fs";
const file = process.argv[2];
if (!file) { console.error("usage: patch-understand-source-viewer.mjs <CodeViewer.tsx>"); process.exit(1); }
let s = readFileSync(file, "utf8");
const orig = s;

const A = 'return `/file-content.json?${params.toString()}`;';
if (!s.includes(A)) { console.error("anchor A not found - plugin changed; adjust patch."); process.exit(1); }
s = s.replace(A,
  'const __base = import.meta.env.VITE_SOURCE_BASE_URL;\n' +
  '  if (__base) return `${__base.replace(/\\/$/, "")}/${filePath}`;\n' +
  '  return `/file-content.json?${params.toString()}`;');

const B = 'if (accessToken === "__demo__") {';
if (!s.includes(B)) { console.error("anchor B not found - adjust patch."); process.exit(1); }
s = s.replace(B, 'if (accessToken === "__demo__" && !import.meta.env.VITE_SOURCE_BASE_URL) {');

const C = 'const data = (await res.json()) as SourceFile | { error?: string };';
if (!s.includes(C)) { console.error("anchor C not found - adjust patch."); process.exit(1); }
s = s.replace(C,
  'if (import.meta.env.VITE_SOURCE_BASE_URL) {\n' +
  '          const text = await res.text();\n' +
  '          if (!res.ok) throw new Error("Source unavailable");\n' +
  '          setState({ status: "loaded", source: { path: node.filePath, language: fallbackLanguage(node.filePath), content: text, sizeBytes: new Blob([text]).size, lineCount: text.split(/\\r\\n|\\n|\\r/).length }, error: null });\n' +
  '          return;\n' +
  '        }\n' +
  '        const data = (await res.json()) as SourceFile | { error?: string };');

if (s === orig) { console.error("no changes applied"); process.exit(1); }
writeFileSync(file, s);
console.log("OK: patched", file);
SCRIPT
chmod +x site/scripts/patch-understand-source-viewer.mjs
```

- [ ] **Step 2: Already wired into Task 1**

Task 1's build script auto-derives `UNDERSTAND_SOURCE_BASE_URL` from `.understand-anything/meta.json` (the analyzed commit) and applies this patch automatically. Re-run `bash site/scripts/build-understand-bundle.sh` if you created this script after the first build. Expected: build logs include `Source preview base: …` and `OK: patched .../CodeViewer.tsx`.

- [ ] **Step 3: Verify source preview**

Serve `site/public/understand/app/` (Task 3 Step 1), open a node, click "View source". Expected: file loads from `raw.githubusercontent.com/stevencarpenter/hippo/$REF/...` (check the Network tab).

- [ ] **Step 4: Commit**

```bash
git add site/scripts/patch-understand-source-viewer.mjs site/public/understand/app
git commit -m "feat(docs): source preview via GitHub raw in static code map"
```

---

### Task 7 (optional): one-command refresh

**Files:** Modify `site/package.json`

- [ ] **Step 1: Add scripts** to `site/package.json` `"scripts"`:

```json
"understand:build": "bash scripts/build-understand-bundle.sh && node scripts/publish-understand-graph.mjs",
"understand:graph": "node scripts/publish-understand-graph.mjs"
```

- [ ] **Step 2: Document the refresh loop**

1. At the hippo repo root, run `/understand` in a Claude session (incremental).
2. `cd site && pnpm understand:graph` (recopies just the JSON — no JS rebuild for content updates). The `/understand/` landing stats refresh on the next `astro build`.
3. `git commit -am "docs: refresh code map" && git push` → PR/merge to `main` to deploy.

Rebuild the JS (`pnpm understand:build`) only after a plugin update or to change build options.

- [ ] **Step 3: Commit**

```bash
git add site/package.json
git commit -m "chore(site): add understand refresh scripts"
```

---

## Self-review (executor)
- Spec coverage: Goal 1→T1, 2→T2, 3+4→T4, 5→T5, 6→T6, 7→T7; local proof in T3.
- If the app loads but the graph 404s: the relative `VITE_GRAPH_URL` was lost — confirm `.env.production.local` existed during the Vite build.
- If the landing 404s but `/understand/app/` works: the Astro page and the `public/understand/app/` bundle must not collide — the page owns `/understand/`, the SPA owns `/understand/app/`.
- Two distinct roots: `site/` (bundle) vs hippo root (graph). The scripts resolve both from their own location — don't collapse them.
- Deploy gate is **`main`** + `site/**`. Nothing publishes from `understand-everything` until merged.
- The temp `.env.production.local` is written into the plugin cache and removed via `trap`; it never enters this repo.
- hippo commits the root `.understand-anything/` graph (don't gitignore it); `site/public/understand/app/` holds the sanitized published copy.
