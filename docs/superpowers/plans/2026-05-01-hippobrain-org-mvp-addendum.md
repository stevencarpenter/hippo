# hippobrain.org Plan Addendum — Panel Review Synthesis

**Date:** 2026-05-01
**Status:** Locked. All four panelists' findings consolidated; the user's autonomous-execution mandate authorizes these calls.
**Source plan:** `docs/superpowers/plans/2026-05-01-hippobrain-org-mvp.md`
**Source spec:** `docs/superpowers/specs/2026-05-01-hippobrain-org-design.md`

The implementation plan stands; this addendum modifies specific tasks and adds a few. Read this alongside the plan, not in place of it.

---

## A1 — `--rule` token contrast is wrong (a11y BLOCKER)

The spec asserts `--rule rgba(42,29,16,0.32)` is 3.5:1 / 1.4.11 compliant. **The math is wrong: 0.32α computes to 1.94:1**, which fails 1.4.11's 3:1 requirement for UI components and graphical objects.

**Fix (T3 tokens.css):**
```css
--rule:      rgba(42, 29, 16, 0.50);   /* 3.05:1 on --paper, 3.18:1 on --paper-2 — passes 1.4.11 */
--rule-soft: rgba(42, 29, 16, 0.15);   /* decorative paper rules; no contrast requirement */
```

Lights-low equivalent:
```css
[data-theme="dark"] {
  --rule:      rgba(239, 228, 206, 0.50);
  --rule-soft: rgba(239, 228, 206, 0.15);
}
```

This is a one-line fix to T3. Also update spec line that cites the (wrong) 3.5:1 figure when revising docs.

---

## A2 — Code-comment color drops below AA on `--code-bg`

`--quiet #7a6649` is 4.55:1 on `--paper` — but on `--code-bg #e9ddc5` it drops materially. Code comments are body text that must hit 4.5:1 on the surface they appear on.

**Add to T3 tokens.css:**
```css
--quiet-on-code: #6f5a3e;   /* ~5.1:1 on --code-bg #e9ddc5 — code comments only */
```

Add a CSS comment in tokens.css explaining the contrast pair so future contributors don't reuse `--quiet` on `--code-bg`.

**Update T5 / `code.css`:** the Shiki `.token.comment` rule uses `var(--quiet-on-code)`, not `var(--quiet)`.

---

## A3 — Hero hierarchy (a11y BLOCKER)

The spec hero composition says "Italic h2: '— *memoriae custos.*'" — that's wrong. The Latin tagline is supporting text, not a section heading. Without an `<h1>`, the homepage fails 1.3.1 / 2.4.6.

**Fix (T14 Hero.astro and T17 homepage):**
- The wordmark "Memoriæ custos." (display style) is `<h1 class="display">`. Same look (52px Fraunces with axes), real semantic h1.
- The supporting "— the keeper of memory." line is `<p class="lede">`.
- Plate II–VI use `<h2>`; their inner sub-titles use `<h3>`.
- Apply this hierarchy template to every page: each page has exactly one h1 (the page title), plates/major sections are h2, sub-sections h3.
- `/404`: `<h1>The page is missing.</h1>` (display-styled), italic lede line is `<p class="lede">`.

---

## A4 — Latin phrases need `lang="la"`

WCAG 3.1.2 — screen readers mispronounce Latin as English without `lang` annotation.

**Rules** (codify in T38 site/CONTRIBUTING.md and apply during page implementation T17–T22, T30, T32):
- Wrap every Latin phrase in `<span lang="la">…</span>`. Examples: "Memoriæ custos.", "memoriae custos", "cornu Ammonis", "sectio sagittalis", "sectio coronalis", "fasciculus", "concordantia", "Sub praelo", per-motif Latin captions.
- Don't tag English words borrowed from Latin: "Plate I", "fig.", "fig. 404", "marginalia" (English usage), "corpus" (English usage). The rule of thumb: if it would be italicized in English prose, tag it.
- Wordmark "Hippo·campus." is the project name; do NOT add `lang="la"`. Convention: project names aren't language-tagged.
- Add `dir="ltr"` to `<html>` defensively.

---

## A5 — SVG motifs: a11y triage by role

The spec said "all SVG illustrations have `<title>` + `aria-labelledby`". Wrong by default — most motifs are decorative; treating them as content forces screen readers to read meaningless prose.

**Three categories:**

| Category | Examples | Markup |
|---|---|---|
| **Decorative motifs** | hero cornu Ammonis, blog-post chapter marks, plate-frame, fasciculus, wordmark mark | `<svg aria-hidden="true" focusable="false">`. The Latin caption (`fig. 1 — cornu Ammonis…`) is also `aria-hidden="true"` (typographic flourish, not prose). |
| **Content diagrams** | Plate III architecture diagram, `/privacy` data-flow diagram | Full treatment: `<svg role="img" aria-labelledby="title-id" aria-describedby="desc-id">` containing `<title id="title-id">` (short) and `<desc id="desc-id">` (long, or reference a visually-hidden `<div class="sr-only">` with prose description). WCAG 1.1.1 — diagram information must exist in text form. |
| **Wordmark mid-dot** | "Hippo·campus." | `<span aria-hidden="true">·</span>` so screen readers say "Hippocampus." not "Hippo middle-dot campus." |

**Update T7 (motif components):** every motif accepts `decorative?: boolean` (default `true`). When decorative, render `aria-hidden="true" focusable="false"`. When `decorative={false}`, render `role="img"` with title + desc.

**Update T17 Plate III and T20 `/privacy`:** SVG diagrams use `decorative={false}` and include a visually-hidden long-description. Add `.sr-only` utility class to `reset.css`.

---

## A6 — Active-state semantics: `aria-current` everywhere

WCAG 4.1.2 — color/weight alone doesn't communicate "you are here" to screen readers.

- **Header (T8):** active link gets `aria-current="page"`.
- **DocsRail (T9):** active entry gets `aria-current="page"`.
- **DocsOutline (T9):** the IntersectionObserver handler that toggles `.active` on the active section also toggles `aria-current="location"` on the matching anchor. Strip on mismatch.

---

## A7 — Skip link: focus target needs `tabindex="-1"`

Skip links to non-focusable elements work in Chrome/Firefox but fail in Safari (focus stays on link). 

**Update T8 Tier1 + T9 Tier2:** `<main id="main" tabindex="-1">`. Add `main:focus { outline: none }` in `reset.css` to suppress the focus-ring on programmatic focus.

Also: wrap the docs rails in landmark `<aside>` so screen-reader users can navigate around them.
- `DocsRail` outermost element: `<aside aria-label="Section navigation">`.
- `DocsOutline` outermost element: `<aside aria-label="On this page">` (already `<aside>` per plan).

---

## A8 — Code-copy button: a11y patches

**Pre-create one global live-region** in both Tier1 and Tier2 layouts, near `</body>`:
```html
<div id="sr-announce" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></div>
```

**Update T14 CodeBlock:**
- Button always visible, NOT hover-only (2.5.5 / 2.4.11). Position absolute top-right, 0.4rem inset, no opacity transitions on visibility.
- `<button aria-label="Copy code to clipboard" data-copy>Copy</button>` — never mutate the `aria-label`.
- Click handler writes "Copied to clipboard." into `#sr-announce`'s textContent, clears after 1.5s.
- Reduced-motion: skip the 600ms scale transition. State change still happens (color flip) but no easing.

---

## A9 — Pagefind UI a11y patches

Pagefind defaults have known issues. Until upstream fixes them, post-init patches:

**Update T15 / T35:**
- After `new PagefindUI({...})`, use a `MutationObserver` (or just `setTimeout(init, 100)`) to:
  - Find Pagefind's input and call `setAttribute("aria-label", "Search the docs and field notes")` if no label exists.
  - Wrap the result mount in `<div role="region" aria-live="polite" aria-label="Search results">`.
- Pagefind translations override:
  ```js
  new PagefindUI({
    element: "#search",
    showImages: false,
    showSubResults: true,
    placeholder: "Search the docs…",
    translations: {
      placeholder: "Search the docs…",
      clear_search: "Clear search",
      load_more: "Load more results",
      search_label: "Search the docs and field notes",
      filters_label: "Filter results",
      zero_results: "No entries match. Try a broader term, or browse the docs index.",
      many_results: "[COUNT] results for [SEARCH_TERM]",
      one_result: "[COUNT] result for [SEARCH_TERM]",
      alt_search: "Did you mean [DIFFERENT_TERM]?",
      search_suggestion: "No results for [SEARCH_TERM]. Try one of the suggested terms below.",
      searching: "Searching for [SEARCH_TERM]…",
    },
  });
  ```
- Add axe smoke test for `dist/search/index.html` to T40 CI.

---

## A10 — Search empty-state design

**Update T35 `/search`** — beyond the search component, show a small intro until the user types:
- Plate badge "Plate IX · *concordantia*"
- h1 "Search."
- Italic lede: "— *concordantia* — search the docs and field notes."

The Pagefind UI is below. The "Search the docs…" placeholder is set via translations (A9).

---

## U1 — Hero breathe behavior is SVG-only

**Update T14 Hero / T7 motifs:**
- Breathe animation is on the *SVG container only*, transform-only (no Fraunces axis modulation on type).
- `transform-origin: center; will-change: transform;` for compositor-only animation.
- Wordmark and lede do NOT breathe (logotype/typography stays still).
- Amplitude 1.00 → 1.03 on 8s ease-in-out loop. If first paint reads as "pulsing rather than breathing", drop to 1.00 → 1.02 (smaller change before changing duration).

---

## U2 — Empty right-rail collapses to 2-col

**Update T9 Tier2:**
- Compute `headings.filter(h => h.depth >= 2 && h.depth <= 3).length` in the layout's frontmatter.
- If `< 3`, set `data-outline="none"` on `<body>`.
- CSS rule: `.tier-2 [data-outline="none"] .docs-shell { grid-template-columns: var(--rail-left) minmax(0, 1fr); } .tier-2 [data-outline="none"] .docs-rail-right { display: none; } .tier-2 [data-outline="none"] .docs-main { max-width: 75ch; }`.

Document this in `site/CONTRIBUTING.md` so authors know that adding a 4th h2 brings the rail back.

---

## U3 — Mobile breadcrumb truncation

**Update T9 DocsBreadcrumb / `tier-2.css`:**
- On `<480px`, render only the last two segments with a leading `← ` glyph in oxblood that links to `/docs`.
- Letter-spacing drops from `0.34em` to `0.18em` on mobile.
- Implement via CSS-only: hide all `.breadcrumb-segment:not(:nth-last-child(-n+3))` on `<480px` (the `+3` accounts for the separator). Add a `.breadcrumb-back` pseudo-element with `← ` content visible on mobile.
- Page h1 titles wrap with `text-wrap: balance`, never truncate with ellipsis.

---

## U4 — Code-block overflow + per-block soft-wrap opt-in

**Update T5 code.css and T6 tier-2.css:**
- `pre { overflow-x: auto; }` always.
- Add a right-edge fade mask on `pre` to hint scrollability: `mask-image: linear-gradient(to right, black calc(100% - 24px), transparent)` applied conditionally (when scrollable — use a small JS islander or accept a faint always-on fade).
- The 6px sepia left bar uses `position: sticky` inside `pre` so it doesn't scroll-clip horizontally.
- On `<768px` (right-rail already collapsed), allow code blocks in `.docs-main` to break out: `pre { margin-inline: calc(50% - 50vw + 1rem); }` so shell snippets fit at viewport width minus margin.
- **Per-block soft-wrap:** support fence info-string `bash {wrap=true}` via Shiki transformer that adds `data-wrap="true"` to the `<pre>`; CSS rule `pre[data-wrap="true"] code { white-space: pre-wrap; word-break: break-word; }`.

---

## U5 — `/blog` zero-state needs full chrome

**Update T32 `/blog/index.astro`:** when `posts.length === 0`, still render plate badge + h1 + lede, then a Marginalia callout instead of the post list:

```html
<Marginalia>
  <span lang="la"><em>Sub praelo.</em></span> The first field notes are still being written. In the meantime: read <a href="/why">why hippo exists</a>, or follow <a href="/changelog">the changelog</a>.
</Marginalia>
```

Same pattern for `/changelog` if zero releases (vanishingly unlikely given hippo is on v0.20+, but cheap safety net).

---

## U6 — Motifs: section-locked, frontmatter required, lint warning

**Update T23 content/config.ts:** in the blog schema, **drop the default** for `motif`. Make it required. zod will fail-build any post without one.

```ts
motif: z.enum([
  "cornu-ammonis", "sectio-coronalis", "trisynaptic-circuit",
  "marginalia", "plate-frame", "fasciculus",
]),  // no .default()
```

**Add docs section-to-motif mapping** (locked, used by `pages/docs/[...slug].astro` and chapter marks):

| Section | Motif |
|---|---|
| capture/* | cornu-ammonis |
| reference/* (anything in docs/ root not under capture/) | sectio-coronalis |
| schema, lifecycle | trisynaptic-circuit |
| contributing | marginalia |
| privacy | fasciculus |
| hero, /404 | plate-frame |

Implement as a function in `lib/docs.ts`: `motifForDocSection(slug: string): MotifId`. The `pages/docs/[...slug].astro` calls this when rendering the chapter mark for the first heading of a top-level section.

**Add T44** (new) — build-time motif-distribution lint warning: a small Node script run after `pnpm build` that counts blog post motif usage; if any single motif > 40% of posts in last 90 days, print a warning. Non-fatal.

---

## E1 — Astro content collections: dev HMR fix

**Update T1 `astro.config.mjs`** — add Vite config to allow loading from parent and watch parent dir for HMR:

```js
import { defineConfig } from "astro/config";
// ...
export default defineConfig({
  // ...existing config...
  vite: {
    server: {
      fs: { allow: [".."] },
      watch: {
        ignored: ["!../docs/**", "!../README.md", "!../CONTRIBUTING.md"],
      },
    },
    ssr: { noExternal: ["satori"] },
  },
});
```

Without this, prod builds work but `pnpm dev` won't reload when you edit `../docs/foo.md`.

---

## E2 — `edit-on-github` MUST be a remark plugin

The plan registers this as rehype. Astro's official "modified time" recipe uses remark; rehype risks frontmatter being snapshotted before the plugin runs.

**Update T25:**
- File becomes `site/src/lib/remark/edit-on-github.ts` (not `lib/rehype/`).
- Register under `markdown.remarkPlugins`, not `rehypePlugins`, in `astro.config.mjs`.
- Same for `mdx({ remarkPlugins })` IF MDX is used (we're dropping it — see E10).
- The plugin reads `(file.data.astro as any).frontmatter.sourcePath` (set by the loader) and writes `frontmatter.lastUpdated` + `frontmatter.editPath`.

Add `lib/remark/types.ts`:
```ts
import type { VFile } from "vfile";
export interface AstroVFileData { astro: { frontmatter: Record<string, unknown> } }
export type AstroVFile = VFile & { data: AstroVFileData };
```
Use this throughout remark/rehype plugins to avoid `as any`.

---

## E3 — Reject `raw.githubusercontent.com` image fallback

The plan punts to GitHub raw URLs for `<img src="../diagrams/foo.png">`. Reject — defeats `astro:assets`, makes site dependent on GH being up, contradicts the "no third-party logging" rationale that drove font self-hosting.

**Update T26 + T1 (new integration):**

Replace `rehype-image-resolve.ts` with a different two-part approach:

**Part 1:** A small Astro integration `site/integrations/copy-doc-images.ts` registered in `astro.config.mjs` that runs in `astro:build:setup`:

```ts
import type { AstroIntegration } from "astro";
import { mkdirSync, copyFileSync, existsSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

export function copyDocImages(): AstroIntegration {
  return {
    name: "copy-doc-images",
    hooks: {
      "astro:config:setup": ({ logger }) => {
        // Source: ../docs/diagrams/** (and any other relative image refs)
        const src = path.resolve(process.cwd(), "..", "docs", "diagrams");
        const dest = path.resolve(process.cwd(), "public", "docs-images");
        if (!existsSync(src)) {
          logger.warn("docs/diagrams not found; skipping image copy");
          return;
        }
        mkdirSync(dest, { recursive: true });
        for (const f of readdirSync(src)) {
          const sf = path.join(src, f);
          if (statSync(sf).isFile()) copyFileSync(sf, path.join(dest, f));
        }
        logger.info(`Copied ${readdirSync(dest).length} images from docs/diagrams to public/docs-images`);
      },
    },
  };
}
```

**Part 2:** `rehype-image-resolve.ts` rewrites `<img src="../diagrams/foo.png">` → `<img src="/docs-images/foo.png">`:

```ts
import { visit } from "unist-util-visit";
import path from "node:path";
import type { Plugin } from "unified";
import type { Element, Root } from "hast";

export const rehypeImageResolve: Plugin<[], Root> = () => {
  return (tree, file) => {
    const sourcePath = (file.data?.astro as any)?.frontmatter?.sourcePath as string | undefined;
    if (!sourcePath) return;
    const sourceDir = path.dirname(sourcePath);
    visit(tree, "element", (node: Element) => {
      if (node.tagName !== "img") return;
      const src = node.properties?.src as string | undefined;
      if (!src) return;
      if (/^https?:\/\//.test(src)) return;
      if (src.startsWith("/")) return;
      const resolved = path.posix.normalize(path.posix.join(sourceDir, src));
      // diagrams → /docs-images
      const filename = path.basename(resolved);
      node.properties.src = `/docs-images/${filename}`;
      node.properties.loading = "lazy";
      node.properties.decoding = "async";
    });
  };
};
```

If non-`diagrams/` images are ever referenced from docs, the integration grows to support them. For v1.0, only `docs/diagrams/` is in scope.

---

## E4 — `<ViewTransitions />` lives in Head.astro

Single source of truth. All three layouts share Head.astro.

**Update T8 `Head.astro`:** add `import { ViewTransitions } from "astro:transitions"` at the top of the frontmatter, render `<ViewTransitions />` once near the end of the head content.

---

## E5 — Add Fraunces preload + tier-2 font gating

**Update T8 Head.astro:** add font preload for the most-used face:

```html
<link rel="preload" href="/fonts/fraunces-variable.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="/fonts/jetbrains-mono-variable.woff2" as="font" type="font/woff2" crossorigin>
```

Don't preload Source Serif 4 — it's tier-2 only.

**Update T2 fonts.css:** keep `@font-face` definitions for all four families, but add a CSS rule that delays Source Serif 4 fetch unless tier-2 is in use:

```css
@font-face {
  font-family: "Source Serif 4";
  src: url("/fonts/source-serif-4-variable.woff2") format("woff2-variations");
  font-weight: 200 900;
  font-style: normal;
  font-display: swap;
  unicode-range: U+0000-007F, U+00A0-00FF;  /* Latin only */
}
```

The browser fetches Source Serif 4 lazily — only when something on the page actually requires it (which is only `.tier-2` body). On tier-1, the font is in CSS but not requested.

---

## E6 — Drop `@astrojs/mdx` for v1.0

Blog posts are `.md` only per the plan; nothing else needs MDX.

**Update T1:** remove `@astrojs/mdx` from dependencies. Remove `mdx()` from `integrations:`. If MDX is needed later for a specific blog post, add it back and mirror the rehype/remark config explicitly.

---

## E7 — `process.env.GITHUB_TOKEN` with CI warning

**Update T29 lib/github.ts:**
```ts
const token = process.env.GITHUB_TOKEN;
if (!token && process.env.CI) {
  console.warn("[github] no GITHUB_TOKEN; rate-limited fetches (60/h per IP)");
}
```

**Update T41 deploy workflow build step:** explicit `env:` block:
```yaml
- run: pnpm build
  working-directory: site
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

---

## E8 — Batch git-log calls (perf, optional)

**Optional optimization to T25 git-timestamp.ts:** instead of one git-log per file, batch on first call:

```ts
import { execFileSync } from "node:child_process";
import path from "node:path";

let batchCache: Map<string, string> | null = null;

function loadBatchCache(): Map<string, string> {
  if (batchCache) return batchCache;
  const repoRoot = path.resolve(process.cwd(), "..");
  batchCache = new Map();
  try {
    // Get the most recent commit date for every tracked file in one call
    const out = execFileSync(
      "git",
      ["log", "--name-only", "--format=COMMIT %ci", "--all"],
      { cwd: repoRoot, encoding: "utf8", maxBuffer: 50 * 1024 * 1024 },
    );
    let currentDate = "";
    for (const line of out.split("\n")) {
      if (line.startsWith("COMMIT ")) {
        currentDate = line.slice(7, 17); // YYYY-MM-DD from %ci
      } else if (line.trim() && !batchCache.has(line)) {
        batchCache.set(line, currentDate);
      }
    }
  } catch {
    // fall through with empty cache
  }
  return batchCache;
}

export function gitLastUpdated(repoRelPath: string): string {
  return loadBatchCache().get(repoRelPath) ?? "";
}
```

For ~83 markdown files this cuts the git-log overhead from ~83 forks to 1.

---

## D1 — Pre-flight task: GitHub Pages source + DNS sequencing (NEW T0)

Add a new T0 BEFORE T1 documenting manual one-time setup that the user must do:

```markdown
### T0: Pre-flight (manual user actions, before first deploy)

These are not implementer steps — they're documented for the user. The implementer creates a checklist for the PR description.

1. **Repo Settings → Pages → Build and deployment → Source = "GitHub Actions"** (one-time; without this, the first deploy fails with a confusing 404).
2. **Repo Settings → Pages → Custom domain = `hippobrain.org`** (after DNS propagates).
3. **Cloudflare DNS — Phase A (cert provisioning):**
   - Apex `hippobrain.org` → CNAME → `stevencarpenter.github.io`. **Grey cloud (DNS-only).**
   - `www.hippobrain.org` → CNAME → `stevencarpenter.github.io`. **Grey cloud.**
   - Wait for GH Pages settings to show "DNS check successful" and the green "Your site is published" with a valid SSL cert (5–30 minutes).
4. **Cloudflare DNS — Phase B (proxy on, post-cert):**
   - Flip apex and `www` to **orange cloud** (proxied).
   - SSL/TLS mode: **Full (strict)** (NOT Flexible — Flexible causes redirect loops with GH Pages' force-HTTPS).
   - Page rule: "Always Use HTTPS" enabled.
   - Tick "Enforce HTTPS" in the GH Pages settings.
5. **Cloudflare Redirect Rules** (free tier covers four):
   - `hippobrain.org/git` → `https://github.com/stevencarpenter/hippo` (302)
   - `hippobrain.org/issues` → `https://github.com/stevencarpenter/hippo/issues` (302)
   - `hippobrain.org/releases` → `https://github.com/stevencarpenter/hippo/releases` (302)
   - `hippobrain.org/install.sh` → `https://github.com/stevencarpenter/hippo/releases/latest/download/install.sh` (302)
```

The implementer adds these instructions to `site/README.md` and the PR description.

**Also update T41 deploy workflow** — add `enablement: true` to `actions/configure-pages@v5` so the workflow attempts to enable Pages on first run as a fallback (cheap safety net).

---

## D2 — GitHub API resilience: retry + cached fallback

**Update T29 lib/github.ts:**
- Wrap each fetch in retry-with-backoff: 3 attempts, exponential 1s/4s/16s.
- On final failure, return data from `site/src/data/github-fallback.json` (committed file). Successful fetches refresh that file at build time.

**Update T41 site-deploy.yml:** **don't deploy if `pnpm build` exits non-zero.** Failed builds = previous good site stays up. The deploy job already runs after build via `needs: build`, so a build failure already prevents deploy. Add an explicit `if: success()` guard on the deploy job for safety.

---

## D3 — Workflow permissions, branch protection, no `workflow_run` chaining

**Update T40 `site-ci.yml`:** add explicit `permissions: contents: read` at workflow level (least privilege).

**Update T41 `site-deploy.yml`:** inline the build-checks (`pnpm exec astro check`, `pnpm test`) BEFORE `pnpm build` in the build job. Don't gate via `workflow_run` chaining — fragile and adds latency. Branch protection on `main` (manual setup, document in T0):
- Require `Site CI / build` check to pass.
- Require PR review before merge.
- Disallow force-pushes to `main`.

---

## D4 — Caching: pnpm + `.astro` + `.pagefind`

**Update T40 + T41 workflows:** add caching steps:

```yaml
- uses: pnpm/action-setup@v4
  with: { version: 9, run_install: false }
- uses: actions/setup-node@v4
  with:
    node-version: "20"
    cache: 'pnpm'
    cache-dependency-path: site/pnpm-lock.yaml
- uses: actions/cache@v4
  with:
    path: |
      site/node_modules/.astro
      site/node_modules/.pagefind
    key: astro-cache-${{ hashFiles('site/pnpm-lock.yaml', 'site/astro.config.mjs', 'docs/**', 'site/src/**', 'README.md', 'CONTRIBUTING.md') }}
- run: pnpm install --frozen-lockfile
  working-directory: site
```

Don't cache `site/dist`.

---

## D5 — Linkinator: tighter skip + retry + concurrency + non-blocking

**Update T40 + T28 link checks:**

```bash
pnpm exec linkinator dist \
  --recurse \
  --skip "^https?://github\\.com/.+/(blob|tree|releases|releases/tag|issues|pull|pulls|commit)/.*$" \
  --skip "^https?://github\\.com/[^/]+/[^/]+/?$" \
  --retry --retry-errors-count 3 --retry-errors-jitter 2000 \
  --concurrency 4
```

In CI workflow, run with `continue-on-error: true` for v1.0 (we'll watch a week of runs to see false-positive rate before making it blocking).

---

## D6 — Axe: serve dist, skip /search, save artifact

**Update T40 site-ci.yml axe step:**

```yaml
- name: Axe a11y smoke
  run: |
    npx -y serve@latest dist -l 4321 &
    SERVER_PID=$!
    sleep 2
    pnpm exec axe \
      http://localhost:4321/ \
      http://localhost:4321/install/ \
      http://localhost:4321/why/ \
      http://localhost:4321/docs/capture/anti-patterns/ \
      --exit \
      --tags wcag2a,wcag2aa \
      --save axe-results.json || (kill $SERVER_PID; exit 1)
    kill $SERVER_PID
  working-directory: site
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: axe-results
    path: site/axe-results.json
```

Don't include `/search` in v1.0 axe smoke (Pagefind's shadow-DOM is noisy in axe results); we cover it via the manual a11y patches in A9.

---

## D7 — Workflow path filters: self-trigger + ignore archive/superpowers

**Update T40 + T41:** add the workflow file itself to `paths:` (self-trigger on workflow edits), add `paths-ignore` for internal docs:

```yaml
on:
  pull_request:
    paths:
      - "site/**"
      - "docs/**"
      - "README.md"
      - "CONTRIBUTING.md"
      - ".github/workflows/site-ci.yml"
    paths-ignore:
      - "docs/archive/**"
      - "docs/superpowers/**"
```

Same shape for `site-deploy.yml`.

---

## Summary of changes by task

| Task | Changes |
|---|---|
| T1 (scaffold) | E1 vite config, E4 ViewTransitions in Head, E6 drop @astrojs/mdx, T0 pre-flight notes |
| T2 (fonts) | E5 unicode-range gate Source Serif 4 |
| T3 (tokens) | A1 fix `--rule`, A2 add `--quiet-on-code` |
| T5 (code css) | A2 use `--quiet-on-code` for comments, U4 overflow + per-block soft-wrap |
| T6 (tier-2 css) | U2 outline-none rule, U3 mobile breadcrumb truncation, U4 mobile code break-out |
| T7 (motifs) | A5 decorative prop default true, U1 transform-origin/will-change, breathe SVG-only |
| T8 (Tier1 + Head) | E4 ViewTransitions, E5 font preload, A7 `<main tabindex="-1">`, sr-only utility |
| T9 (Tier2 + rails) | A6 aria-current, A7 `<aside aria-label>`, U2 conditional outline collapse, U3 mobile breadcrumb |
| T14 (CodeBlock + Hero) | A3 hero h1, A8 button always-visible + sr-announce, U1 breathe semantics |
| T15 (Search) | A9 Pagefind translations + post-init label injection + result region wrap |
| T17 (homepage) | A3 h1 hierarchy, A4 lang="la", A5 Plate III content-diagram a11y, U6 motif mapping |
| T20 (privacy) | A5 data-flow content-diagram a11y, A4 lang="la" |
| T22 (404) | A3 real h1 |
| T23 (content config) | U6 drop motif default; required field |
| T25 (edit-on-github) | E2 rename to remark plugin, E2 typed AstroVFile, E8 batch git-log |
| T26 (image-resolve) | E3 reject GH-raw fallback; copy to /docs-images |
| T29 (github lib) | D2 retry + cached fallback, E7 CI warning |
| T32 (blog index) | U5 zero-state Marginalia callout |
| T33 (blog post) | A4 lang="la" rules in author-facing CONTRIBUTING |
| T35 (search page) | A9 a11y patches, A10 empty-state composition |
| T38 (CONTRIBUTING) | A4 lang="la" rule, U6 motif mapping, "things we don't do" |
| T40 (site-ci.yml) | D3 permissions, D4 caching, D5 linkinator, D6 axe + serve, D7 path filter |
| T41 (site-deploy.yml) | D1 enablement: true, D2 if: success guard + GITHUB_TOKEN env, D3 inline checks, D4 caching, D7 path filter |
| T0 (NEW) | D1 pre-flight manual user actions, documented for PR description |
| T44 (NEW) | U6 motif distribution lint warning script |

---

## Open questions resolved

Per the user's directive ("trust their outcome"), every panelist recommendation has been adopted unless it conflicted with another locked decision (none did). All four panelists ran independently; their findings overlapped on a few items (notably: motif a11y, tighter linkinator, font preload — all incorporated once).

The only finding **not adopted**: DevOps Q11 (the `install.sh` redirect concern). Verified in repo — `scripts/install.sh` exists and is published as a release asset by the existing `release.yml` workflow. No change needed.
