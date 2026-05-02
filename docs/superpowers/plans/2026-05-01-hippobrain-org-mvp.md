# hippobrain.org Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build hippobrain.org — a hybrid marketing-and-docs site for hippo, deployed to GitHub Pages with Cloudflare DNS, sourcing docs from `/docs` in the repo.

**Architecture:** Astro static-output site at `/site`. Content collections source markdown from `/docs`, `README.md`, `CONTRIBUTING.md`. Custom rehype pipeline rewrites repo-relative links to site URLs, injects "Edit on GitHub", highlights code with sepia Shiki theme. Pagefind for search. Build-time GitHub API fetches for `/changelog` and `/status`. GitHub Actions deploys to Pages on push to `main` and on a daily cron.

**Tech Stack:** Astro 4.x · @astrojs/mdx · @astrojs/sitemap · @astrojs/rss · astro-pagefind · Shiki · Satori (one-shot OG only) · pnpm · TypeScript · Vitest (rehype plugin tests) · GitHub Actions · Cloudflare DNS

**Source of truth for design decisions:** `docs/superpowers/specs/2026-05-01-hippobrain-org-design.md`. Every D1–D10 decision and every aesthetic token referenced in this plan lives there with full alternatives.

---

## File Structure

```
hippo/
├── site/
│   ├── astro.config.mjs                Astro + integrations + view transitions
│   ├── package.json                    pnpm + scripts (dev, build, preview, check)
│   ├── tsconfig.json                   strict TS
│   ├── pnpm-lock.yaml
│   ├── .gitignore                      dist, .astro, node_modules
│   ├── README.md                       site dev quickstart
│   ├── CONTRIBUTING.md                 site-specific contribution rules (D10)
│   ├── public/
│   │   ├── CNAME                       hippobrain.org
│   │   ├── robots.txt
│   │   ├── favicon.svg                 simplified cornu Ammonis spiral
│   │   ├── favicon.ico                 fallback
│   │   ├── apple-touch-icon.png
│   │   ├── og-default.png              fallback OG card
│   │   └── fonts/                      self-hosted woff2 (Latin subset)
│   │       ├── fraunces-variable.woff2
│   │       ├── fraunces-variable-italic.woff2
│   │       ├── source-serif-4-variable.woff2
│   │       ├── source-serif-4-variable-italic.woff2
│   │       ├── junicode-variable.woff2
│   │       ├── junicode-variable-italic.woff2
│   │       └── jetbrains-mono-variable.woff2
│   ├── src/
│   │   ├── content/
│   │   │   ├── config.ts               collection schemas
│   │   │   └── blog/                   field notes (.md)
│   │   ├── pages/
│   │   │   ├── index.astro             /
│   │   │   ├── install.astro
│   │   │   ├── why.astro
│   │   │   ├── privacy.astro
│   │   │   ├── faq.astro
│   │   │   ├── changelog.astro
│   │   │   ├── status.astro
│   │   │   ├── search.astro
│   │   │   ├── 404.astro
│   │   │   ├── blog/
│   │   │   │   ├── index.astro
│   │   │   │   ├── [...slug].astro
│   │   │   │   └── rss.xml.ts
│   │   │   ├── changelog/
│   │   │   │   └── rss.xml.ts
│   │   │   └── docs/
│   │   │       ├── index.astro
│   │   │       └── [...slug].astro
│   │   ├── layouts/
│   │   │   ├── Tier1.astro
│   │   │   ├── Tier2.astro
│   │   │   └── BlogPost.astro
│   │   ├── components/
│   │   │   ├── Head.astro
│   │   │   ├── Header.astro
│   │   │   ├── Footer.astro
│   │   │   ├── Hero.astro
│   │   │   ├── PlateBadge.astro
│   │   │   ├── Marginalia.astro
│   │   │   ├── Admonition.astro
│   │   │   ├── ChapterMark.astro
│   │   │   ├── CodeBlock.astro
│   │   │   ├── DocsRail.astro
│   │   │   ├── DocsOutline.astro
│   │   │   ├── DocsBreadcrumb.astro
│   │   │   ├── EditOnGitHub.astro
│   │   │   ├── ChangelogEntry.astro
│   │   │   ├── StatusBadge.astro
│   │   │   ├── Search.astro
│   │   │   ├── Wordmark.astro
│   │   │   └── motifs/
│   │   │       ├── CornuAmmonis.astro
│   │   │       ├── SectioCoronalis.astro
│   │   │       ├── TrisynapticCircuit.astro
│   │   │       ├── MarginaliaMotif.astro
│   │   │       ├── PlateFrame.astro
│   │   │       └── Fasciculus.astro
│   │   ├── lib/
│   │   │   ├── docs.ts
│   │   │   ├── github.ts
│   │   │   ├── rehype/
│   │   │   │   ├── link-rewrite.ts
│   │   │   │   ├── edit-on-github.ts
│   │   │   │   ├── image-resolve.ts
│   │   │   │   └── git-timestamp.ts
│   │   │   └── motifs.ts
│   │   ├── styles/
│   │   │   ├── reset.css
│   │   │   ├── fonts.css
│   │   │   ├── tokens.css
│   │   │   ├── tier-1.css
│   │   │   ├── tier-2.css
│   │   │   ├── prose.css
│   │   │   ├── code.css
│   │   │   └── print.css
│   │   └── env.d.ts
│   └── tests/
│       └── rehype-link-rewrite.test.ts
└── .github/
    └── workflows/
        ├── site-ci.yml
        └── site-deploy.yml
```

**Decomposition rationale:** Each component is one file with one responsibility. Rehype plugins are split per concern. CSS is split by concern, not per component, so cascading is predictable. The `lib/` directory has one file per integration (GitHub API, docs collection helpers, motif registry, rehype plugins).

---

## Conventions for every task

- **Tests:** This is a static site. "Tests" for most tasks means `pnpm build` succeeds + `pnpm exec astro check` passes + visual smoke via `pnpm preview`. Rehype plugins get real Vitest unit tests.
- **Commit cadence:** One commit per task. Format: `feat(site): <subject>`.
- **Verify command:** `cd site && pnpm build` after every implementation step that touches code; if it fails, fix before commit.
- **No `--no-verify`** on commits.
- **Security:** Use `execFileSync` (args array, no shell interpolation) for any shell-out, never `execSync` with template strings.

---

## Phase 1 — Astro scaffold (T1)

### T1: Initialize Astro project at /site

**Files:**
- Create: `site/package.json`
- Create: `site/astro.config.mjs`
- Create: `site/tsconfig.json`
- Create: `site/.gitignore`
- Create: `site/src/env.d.ts`
- Create: `site/README.md`

- [ ] **Step 1: `site/package.json`** (full content as specified, including `marked`, `@astrojs/rss`, `tsx` for one-shot OG script)

- [ ] **Step 2: `site/astro.config.mjs`** — static output, integrations (mdx, sitemap, pagefind), markdown rehype plugins (rehypeSlug, rehypeAutolinkHeadings, rehypeLinkRewrite, rehypeImageResolve, rehypeEditOnGitHub), Shiki `theme: "css-variables"`, view transitions enabled at the layout level.

- [ ] **Step 3: `site/tsconfig.json`** — extends `astro/tsconfigs/strict`, paths `@/*` → `src/*`.

- [ ] **Step 4: `site/.gitignore`** — `node_modules`, `dist`, `.astro`, `.env`, `.DS_Store`, `*.log`.

- [ ] **Step 5: `site/src/env.d.ts`** — `/// <reference types="astro/client" />`.

- [ ] **Step 6: `site/README.md`** — site dev quickstart, links to spec.

- [ ] **Step 7: `pnpm install && pnpm dev`** to verify Astro starts.

- [ ] **Step 8: Commit** `feat(site): scaffold Astro project at /site`.

---

## Phase 2 — Design system foundation (T2-T7)

### T2: Self-host fonts (Latin subset, woff2)

**Files:**
- Create: `site/public/fonts/*.woff2` (7 files)
- Create: `site/src/styles/fonts.css`

- [ ] **Step 1:** Download woff2 files (variable axes, Latin subset) for Fraunces, Source Serif 4, Junicode, JetBrains Mono via `gwfh.mranftl.com` / fontsource / SIL.
- [ ] **Step 2:** Verify total payload <500KB (`du -ch site/public/fonts/*.woff2 | tail -1`).
- [ ] **Step 3:** Write `fonts.css` with `@font-face` for each, `font-display: swap`, variable `font-weight` ranges.
- [ ] **Step 4:** Commit.

### T3: CSS tokens (`tokens.css`)

**Spec ref:** "Aesthetic System → Color tokens" + D8 lights-low.

- [ ] **Step 1:** Write `site/src/styles/tokens.css` containing:
  - Light tokens (`--paper #efe4ce`, `--paper-2 #f5efe2`, `--ink #2a1d10`, `--sepia #6b3c20`, `--oxblood #8a3a1f`, `--rust #b58a4b` decorative-only, `--quiet #7a6649`, `--code-bg #e9ddc5`, `--rule rgba(42,29,16,0.32)`, `--rule-soft rgba(42,29,16,0.15)`)
  - Type stack (`--font-display`, `--font-body-1`, `--font-body-2`, `--font-label`, `--font-mono`)
  - Tier-1 type scale (`--t1-display 52px`, `--t1-h1 32px`, ...)
  - Tier-2 type scale
  - Spacing (`--gap-xs/sm/md/lg/xl/xxl`)
  - Layout (`--max-prose 60ch`, `--max-page 1200px`, `--rail-left 220px`, `--rail-right 200px`)
  - `@media (max-width: 480px)` overrides for tier-1 display/h1
  - `[data-theme="dark"]` lights-low tokens (paper `#1f1812`, paper-2 `#261d15`, ink `#efe4ce`, sepia `#c39a76`, oxblood `#d97a5e`, rust `#c89968`, quiet `#8b7860`, code-bg `#2a1f17`, rule `rgba(239,228,206,0.32)`, rule-soft `rgba(239,228,206,0.15)`)
- [ ] **Step 2:** Commit.

### T4: Reset + prose CSS

- [ ] **Step 1:** Write `site/src/styles/reset.css` — modern minimal reset, body defaults to `--paper`/`--ink`/`var(--font-body-1)`, `:focus-visible` ring in oxblood, `prefers-reduced-motion` short-circuit, skip-link styles.
- [ ] **Step 2:** Write `site/src/styles/prose.css` — `.prose` class with markdown rendering (h1/h2/h3 in Fraunces, h2 italic sepia with SOFT 100, blockquote as marginalia, link with sepia bottom-border that turns oxblood on hover, table with Junicode small-caps headers + zebra), `.prose-tier-2` overrides body face to `var(--font-body-2)` and tightens scale, `.prose a.anchor` for heading anchors.
- [ ] **Step 3:** Commit.

### T5: Code block CSS + Shiki theme

- [ ] **Step 1:** Write `site/src/styles/code.css` — `pre` with `--code-bg` background, 6px sepia left bar, 14px JetBrains Mono, copy-button positioned top-right with sepia-flash animation on `.copied`. Inline `code` (not in `pre`) with subtle background. Shiki `css-variables` token styles using palette (`.token.keyword` oxblood + 600 weight, `.token.string` `#5a3a1f` italic, `.token.comment` quiet italic, `.token.number/punctuation/operator` sepia).
- [ ] **Step 2:** Commit.

### T6: Tier-1 + Tier-2 base styles

- [ ] **Step 1:** Write `site/src/styles/tier-1.css` — `.tier-1` body class with paper background plus radial-dot grain, `.page` max-width container, `.plate-section` separators, `.display`/`.lede`/`.eyebrow`/`.dropcap::first-letter` typography helpers, mobile breakpoint at 480px.
- [ ] **Step 2:** Write `site/src/styles/tier-2.css` — `.tier-2` body class with paper-2 background, `.docs-shell` 3-column grid (`220px 1fr 200px`), `.docs-main` body face + 70ch max width, `.breadcrumb` Junicode label, responsive collapse: `<1024px` drops right rail, `<768px` collapses to single column with left rail as `<details>`.
- [ ] **Step 3:** Commit.

### T7: Motif library (six SVG components + registry)

**Files:**
- Create: `site/src/components/motifs/CornuAmmonis.astro`
- Create: `site/src/components/motifs/SectioCoronalis.astro`
- Create: `site/src/components/motifs/TrisynapticCircuit.astro`
- Create: `site/src/components/motifs/MarginaliaMotif.astro`
- Create: `site/src/components/motifs/PlateFrame.astro`
- Create: `site/src/components/motifs/Fasciculus.astro`
- Create: `site/src/lib/motifs.ts`

- [ ] **Step 1:** Each motif accepts `size: "full" | "reduced" | "inline"` (sizes 200/32/24), optional `caption` (rendered as italic Junicode below baseline), optional `breathe` boolean (8s scale 1.00→1.03 ease-in-out infinite, with `prefers-reduced-motion` short-circuit). Use `currentColor` for stroke so the parent's text color drives the illustration.
- [ ] **Step 2:** SVG path data for each:
  - **CornuAmmonis** — hippocampus side view, the seahorse curl (path data from `.superpowers/brainstorm/23226-1777627445/content/aesthetic-system.html` Plate IV first figure)
  - **SectioCoronalis** — coronal section with brain ellipse and detail (Plate IV second figure)
  - **TrisynapticCircuit** — CA1/CA2/CA3/DG nodes with labeled connections (Plate IV third figure)
  - **MarginaliaMotif** — quill ribbon with hatching guides (Plate IV fourth figure)
  - **PlateFrame** — decorative four-corner ornament frame (no body content; used as a wrapper)
  - **Fasciculus** — bundle-of-fibers parallel curving strokes (new SVG)
- [ ] **Step 3:** `lib/motifs.ts` exports a typed registry mapping `MotifId` → motif component.
- [ ] **Step 4:** Commit.

---

## Phase 3 — Layouts (T8-T10)

### T8: Tier1 layout + Wordmark + Head + Header + Footer

**Files:**
- Create: `site/src/components/Wordmark.astro`
- Create: `site/src/components/Head.astro`
- Create: `site/src/components/Header.astro`
- Create: `site/src/components/Footer.astro`
- Create: `site/src/layouts/Tier1.astro`

- [ ] **Step 1: Wordmark** — "Hippo·campus." set in Fraunces with sepia mid-dot. Three sizes: `header` 1.25rem, `footer` 1rem, `hero` `var(--t1-display)`.

- [ ] **Step 2: Head** — meta charset, viewport, title, description, canonical (built from `Astro.url.pathname` + `Astro.site`), OG tags (type=website, url, title, description, image), twitter:card=summary_large_image, favicon links (svg + ico + apple-touch-icon), RSS alternate link, `<meta name="generator" content={Astro.generator} />`. Imports all the global CSS files (reset, fonts, tokens, prose, code, tier-1, tier-2, print).

- [ ] **Step 3: Header** — Wordmark + nav (Why, Install, Docs, Field notes, GitHub external). Active link based on `Astro.url.pathname`. Mobile breakpoint at 720px stacks vertically.

- [ ] **Step 4: Footer** — three-column grid (hippobrain, Read, Code), colophon line ("memoriae custos. Set in Fraunces and Source Serif 4. Hand-drawn anatomy in pen and ink."), build SHA + date pulled from `process.env.GITHUB_SHA` falling back to "dev". MIT license link, RSS link.

- [ ] **Step 5: Tier1 layout** — `<!doctype html>` + lang="en", `<head><Head {...props} /></head>`, body with `class="tier-1"`, skip-link as first focusable element, `<Header />`, `<main id="main"><slot /></main>`, `<Footer />`.

- [ ] **Step 6:** Commit.

### T9: Tier2 layout (docs shell) + DocsRail + DocsOutline + DocsBreadcrumb + EditOnGitHub + lib/docs.ts

**Files:**
- Create: `site/src/lib/docs.ts`
- Create: `site/src/components/DocsRail.astro`
- Create: `site/src/components/DocsOutline.astro`
- Create: `site/src/components/DocsBreadcrumb.astro`
- Create: `site/src/components/EditOnGitHub.astro`
- Create: `site/src/layouts/Tier2.astro`

- [ ] **Step 1: `lib/docs.ts`** — exports `buildSidebar()` (groups doc entries from collections `docs`, `rootDocs`, `contributing` into sections: Getting Started, Capture, Reference, Contributing — pattern matching on slug prefix) and `gitHubSourcePath(slug)` (reverse mapping from URL slug back to repo path: `getting-started` → `README.md`, `contributing` → `CONTRIBUTING.md`, `capture/foo` → `docs/capture/foo.md`, `reference/bar` → `docs/bar.md`, otherwise `docs/<slug>.md`).

- [ ] **Step 2: DocsRail** — left sidebar as `<details open>` (so it collapses on mobile per T6 css). Each section a `<h5>` (Junicode label) with a `<ul>` of entries; active entry styled with oxblood left-border + bold.

- [ ] **Step 3: DocsOutline** — right "On this page" rail. Filters headings to depth 2 + 3. Sticky position. Inline `<script>` uses IntersectionObserver to highlight the active heading on scroll.

- [ ] **Step 4: DocsBreadcrumb** — flat trail of `<a>` + final `<span class="current">`. Junicode small-caps.

- [ ] **Step 5: EditOnGitHub** — composes `https://github.com/stevencarpenter/hippo/blob/main/<gitHubSourcePath(slug)>` + optional `lastUpdated` date string. Footer-style.

- [ ] **Step 6: Tier2 layout** — `<head><Head ... /></head>`, body class="tier-2", skip-link, Header, `.docs-shell` grid containing DocsRail (left), `<main id="main" class="docs-main prose prose-tier-2"><slot /></main>` (center), DocsOutline (right). Footer.

- [ ] **Step 7:** Commit.

### T10: BlogPost layout

**Files:**
- Create: `site/src/layouts/BlogPost.astro`

- [ ] **Step 1:** Wraps Tier1 chrome with: chosen motif at top centered, eyebrow "Field note · YYYY-MM-DD", h1 title, italic lede description, `.prose.dropcap` body where the first paragraph's first letter is the drop cap (`> p:first-child::first-letter`).
- [ ] **Step 2:** Commit.

---

## Phase 4 — Components (T11-T16)

### T11: PlateBadge

- [ ] **Step 1:** Component with `label: string` + optional `figure: string`. Renders Junicode-uppercase plate badge + italic figure label, separated, with margin-bottom 1.4rem.
- [ ] **Step 2:** Commit.

### T12: Marginalia + Admonition

- [ ] **Step 1: Marginalia** — `type: "default" | "danger"`, sepia/oxblood left border, `¶`/`†` prefix in oxblood.
- [ ] **Step 2: Admonition** — `type: "note" | "warn" | "danger"` with title, sepia/rust/oxblood border colors, Junicode head, italic body. Used for docs admonitions.
- [ ] **Step 3:** Commit.

### T13: ChapterMark

- [ ] **Step 1:** Centered motif + optional plate strap (PlateBadge + figure). Used at top of each top-level docs section in tier-2 (rendered by `pages/docs/[...slug].astro` for the first heading of a top-level section).
- [ ] **Step 2:** Commit.

### T14: CodeBlock wrapper + Hero

- [ ] **Step 1: CodeBlock** — slot-based wrapper around `<pre>`. Renders a copy button (`<button class="code-copy" data-copy>`). Inline `<script>` attaches click handler that writes `navigator.clipboard`, briefly toggles `.copied` class for the sepia flash, and creates an off-screen `aria-live` announcement.
- [ ] **Step 2: Hero** — homepage hero composition: 2-column grid (text 1.1fr, illustration 0.9fr), text column has plate badge "Plate I", display "Memoriæ / custos.", italic lede "— the keeper of memory.", body paragraph with the agentic-coding pitch, install one-liner CodeBlock, CTAs ("Install" primary, "Read the docs" ghost). Illustration is `<CornuAmmonis size="full" caption="cornu Ammonis, sectio sagittalis" breathe={true} />`. Mobile: single column, illustration first.
- [ ] **Step 3:** Commit.

### T15: Search component

- [ ] **Step 1:** A mount point `<div id="search" class="search-mount"></div>` plus `<link rel="stylesheet" href="/pagefind/pagefind-ui.css" />` and `<script src="/pagefind/pagefind-ui.js" is:inline></script>` plus inline init `new PagefindUI({ element: "#search", showImages: false, showSubResults: true, placeholder: "Search the docs and field notes…" })`. Global CSS variable overrides for Pagefind to match the sepia palette (`--pagefind-ui-primary: var(--oxblood)`, etc.).
- [ ] **Step 2:** Commit.

### T16: ChangelogEntry + StatusBadge

- [ ] **Step 1: ChangelogEntry** — accepts `tag, date, name, url, body` props. Renders `<h3>{name}</h3>`, meta line (`<code>{tag}</code> · YYYY-MM-DD · GitHub link`), then `set:html={body}` for pre-rendered markdown HTML.
- [ ] **Step 2: StatusBadge** — accepts `state: "active" | "quiet" | "hibernating"` + `label`. Uppercase Junicode badge with green/yellow/grey background and ink border.
- [ ] **Step 3:** Commit.

---

## Phase 5 — Marketing pages (T17-T22)

### T17: `/` (homepage)

**Files:**
- Create: `site/src/pages/index.astro`

Six plate-sections per spec "Page-by-page composition → /". Plate VI (Field notes) is conditional on `getCollection("blog", ({ data }) => !data.draft).length > 0`.

- [ ] **Step 1: Plate I — Hero** (uses `<Hero />`)
- [ ] **Step 2: Plate II — What hippo captures, exactly** (3-column grid: Shell / Claude Code / Firefox). Each column has small motif + h3 + paragraph naming exactly what's captured (cite extension/path) + small "→ technical detail" link to `/privacy#shell` / etc.
- [ ] **Step 3: Plate III — How it works** — SVG redraw of the README ASCII architecture diagram with caption "fig. 3 — daemon, brain, MCP". One-paragraph explainer below. Link to `/docs/lifecycle`.
- [ ] **Step 4: Plate IV — Privacy** — three claims with one-paragraph proofs each, link to `/privacy`.
- [ ] **Step 5: Plate V — See it work** — `<pre>` of `hippo doctor` output (real, redacted) followed by `hippo ask "..."` with a synthesized answer + source citations.
- [ ] **Step 6: Plate VI — Field notes (CONDITIONAL)** — only if at least one non-draft blog post exists; render latest 3 with date, title link, excerpt.
- [ ] **Step 7:** All copy honors voice rules (no "AI-powered", no "transform", concrete > grand, Latin only at hero, narrow on agentic coding).
- [ ] **Step 8:** `pnpm build` and visual smoke. Commit.

### T18: `/install`

- [ ] **Step 1:** Tier1 layout with three sections: Quick install (Apple Silicon curl-bash one-liner), Manual installation (Cargo build + brain setup + LaunchAgent install + doctor verification), Verify it's working (sample `hippo doctor` output, link to `/docs/capture/operator-runbook`), Uninstall (explicit, never hidden). Adapt copy from current `README.md` "Quick Install" through "Verify it's working".
- [ ] **Step 2:** `pnpm build`. Commit.

### T19: `/why`

- [ ] **Step 1:** Tier1 layout with four sections: "The problem hippo actually solves" (drop cap on first paragraph; concrete example of agentic-coding context loss), "What hippo is not" (one-sentence each: not Atuin, not Rewind, not Apple Continuity, not Roam, not Notion), "Who hippo is for" (developers using agentic coding tools on macOS arm64), "What hippo will never do" (phone home, sync to cloud, share data with third parties, run a server).
- [ ] **Step 2:** `pnpm build`. Commit.

### T20: `/privacy`

- [ ] **Step 1:** Tier1 layout. Long-form data-flow story adapted from existing `docs/redaction.md`. Sections: "What gets captured" (per source — anchor IDs `#shell`, `#claude`, `#firefox` for deep-linking from homepage Plate II), "What gets redacted (and how)", "What's stored locally", "What never leaves your machine", "What you can turn off". Static SVG of data flow with "stays on machine / leaves machine" overlay.
- [ ] **Step 2:** `pnpm build`. Commit.

### T21: `/faq`

- [ ] **Step 1:** Tier1 layout. 12 entries from spec FAQ list, flat anchor-linked. Each `<section id="...">` with `<h2><a href="#..." class="anchor-h">{question}</a></h2><p>{answer}</p>`. Concrete answers with real numbers (disk usage `~50MB daemon, 200-500MB DB after a month`, memory `daemon ~30MB, brain idle most of the time`, etc.).
- [ ] **Step 2:** `pnpm build`. Commit.

### T22: `/404`

- [ ] **Step 1:** Tier1 layout, centered. Plate badge "Plate not found" with figure "fig. 404", display "The page / is missing.", italic lede "— or it never existed.", three links: home, docs, search. `noIndex={true}` in Head.
- [ ] **Step 2:** Commit.

---

## Phase 6 — Content collections + docs pipeline (T23-T28)

### T23: `content/config.ts`

- [ ] **Step 1:** Define `docs` (glob loader, base `../docs`, exclude `archive/**` and `superpowers/**`), `rootDocs` (file loader on `../README.md` with custom parser yielding one entry with `id: "getting-started"`), `contributing` (file loader on `../CONTRIBUTING.md` yielding one entry with `id: "contributing"`), `blog` (glob loader on `./src/content/blog/**/*.md` with schema: title, date coerced, description, motif enum default `cornu-ammonis`, draft default false). Export `collections` object.
- [ ] **Step 2:** Commit.

### T24: `rehype-link-rewrite.ts` (TDD)

**Files:**
- Create: `site/src/lib/rehype/link-rewrite.ts`
- Create: `site/tests/rehype-link-rewrite.test.ts`

- [ ] **Step 1: Failing tests** — Vitest tests for: sibling `.md` link rewrite (`adding-a-source.md` from `docs/capture/architecture.md` → `/docs/capture/adding-a-source`), parent-relative (`../redaction.md` from same source → `/docs/redaction`), repo-rooted (`docs/lifecycle.md` → `/docs/lifecycle`), `README.md` → `/docs/getting-started`, `CONTRIBUTING.md` → `/docs/contributing`, GitHub blob URL → site-relative, external URL gets `target="_blank"` + `rel="noopener"` + `↗`, anchor fragments preserved.

- [ ] **Step 2: Run tests, all FAIL.**

- [ ] **Step 3: Implement plugin** — exported `rehypeLinkRewrite` that walks `<a>` elements via `unist-util-visit`. Skip if `href` starts with `#` or `/`. Match GitHub blob regex first. For external URLs, mark and append `↗`. For relative `.md`, resolve via `path.posix` + `dirname(sourcePath)` + `repoPathToSitePath()`. Special cases: `README.md` → `/docs/getting-started`, `CONTRIBUTING.md` → `/docs/contributing`, `docs/X.md` → `/docs/X`, otherwise `/docs/<repoPath>`.

- [ ] **Step 4: All tests pass.**

- [ ] **Step 5: Commit.**

### T25: `rehype-edit-on-github.ts` + `git-timestamp.ts` (using `execFileSync`)

**Files:**
- Create: `site/src/lib/rehype/git-timestamp.ts`
- Create: `site/src/lib/rehype/edit-on-github.ts`

- [ ] **Step 1: `git-timestamp.ts`** — implements `gitLastUpdated(repoRelPath)`. **Use `execFileSync` with args array, never `execSync` with template strings** (defense in depth even though paths come from controlled metadata):

```ts
import { execFileSync } from "node:child_process";
import path from "node:path";

const cache = new Map<string, string>();

export function gitLastUpdated(repoRelPath: string): string {
  if (cache.has(repoRelPath)) return cache.get(repoRelPath)!;
  const repoRoot = path.resolve(process.cwd(), "..");
  try {
    const out = execFileSync(
      "git",
      ["log", "-1", "--format=%ci", "--", repoRelPath],
      { cwd: repoRoot, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
    ).trim();
    const date = out ? out.slice(0, 10) : "";
    cache.set(repoRelPath, date);
    return date;
  } catch {
    return "";
  }
}
```

- [ ] **Step 2: `edit-on-github.ts`** — rehype plugin that reads `(file.data.astro as any).frontmatter.sourcePath`, calls `gitLastUpdated`, and writes back to `frontmatter.lastUpdated` and `frontmatter.editPath` so the docs layout can read them.

- [ ] **Step 3: Commit.**

### T26: `rehype-image-resolve.ts`

- [ ] **Step 1: Plugin** — walks `<img>` elements. Skip if `src` is absolute (`http(s)://` or `/`). For relative paths, resolve via `path.posix` against the source dir, then rewrite to `https://raw.githubusercontent.com/stevencarpenter/hippo/main/<resolvedPath>`. Add `loading="lazy"`. (Astro:assets across cross-package boundaries is fragile; raw GitHub fallback is fine for v1.0.)
- [ ] **Step 2:** Commit.

### T27: `pages/docs/index.astro` + `pages/docs/[...slug].astro`

- [ ] **Step 1: `docs/index.astro`** — Tier2 (or simpler tier-2 wrapper). Lists all sections from `buildSidebar()` as `<h2>` + `<ul>` of entries. Title "Docs.", italic lede.
- [ ] **Step 2: `docs/[...slug].astro`** — `getStaticPaths()` enumerates all entries from `docs`, `rootDocs`, `contributing` collections; computes slug + sourcePath. `await render(entry)` to get `Content`, `headings`, `remarkPluginFrontmatter`. Renders `<DocsBreadcrumb />`, h1, `<Content />`, `<EditOnGitHub slug={slug} lastUpdated={remarkPluginFrontmatter.lastUpdated} />`. Wraps in Tier2 layout with headings passed through.
- [ ] **Step 3: `pnpm build`.** Verify all docs pages render. Open `dist/docs/capture/anti-patterns/index.html` and confirm internal links rewrite, code blocks have sepia syntax, "Edit on GitHub" present.
- [ ] **Step 4:** Commit.

### T28: End-to-end docs verify

- [ ] **Step 1:** `pnpm build`, then `pnpm exec linkinator dist --recurse --skip "https?://github.com/.*"`. All internal links resolve. External GitHub links allowed.
- [ ] **Step 2:** Fix any broken links by adjusting `rehype-link-rewrite` mapping.
- [ ] **Step 3:** Commit fixes.

---

## Phase 7 — Build-time integrations (T29-T31)

### T29: `lib/github.ts`

- [ ] **Step 1:** Functions: `listReleases()`, `latestCommit()`, `latestCIRun()`, `openIssueCount()`. Use native `fetch`. Add `Authorization: Bearer ${process.env.GITHUB_TOKEN}` if env var present. Filter draft releases. Issues count uses the search API endpoint.
- [ ] **Step 2:** Commit.

### T30: `/changelog`

- [ ] **Step 1:** Tier1 layout. `await listReleases()`. For each, render `<ChangelogEntry tag, date, name, url, body=marked.parse(r.body) />`. Plate badge "Plate VIII", header, RSS link, GitHub link.
- [ ] **Step 2:** `pnpm build`. Commit.

### T31: `/status`

- [ ] **Step 1:** Tier1 layout. Compute `daysSinceRelease`, `projectState`. Render `<StatusBadge>` then `<dl>` of latest release, latest CI run, open issues, last commit. "Build-time data, refreshed daily" footer.
- [ ] **Step 2:** `pnpm build`. Commit.

---

## Phase 8 — Blog + RSS (T32-T34)

### T32: `/blog/index.astro`

- [ ] **Step 1:** Tier1 layout. Plate badge "Plate X". `await getCollection("blog", ({ data }) => !data.draft)` sorted desc by date. If empty: italic message "No field notes yet." Otherwise list with date / title link / excerpt.
- [ ] **Step 2:** Commit.

### T33: `/blog/[...slug].astro`

- [ ] **Step 1:** `getStaticPaths()` over non-draft posts. Renders BlogPost layout with motif, dropcap, content.
- [ ] **Step 2:** Commit.

### T34: RSS feeds

- [ ] **Step 1:** Add `@astrojs/rss` to `package.json`. `pnpm install`.
- [ ] **Step 2:** `site/src/pages/blog/rss.xml.ts` — uses `rss()` helper, items from blog collection.
- [ ] **Step 3:** `site/src/pages/changelog/rss.xml.ts` — uses `rss()` helper, items from `listReleases()`.
- [ ] **Step 4:** Commit.

---

## Phase 9 — Search (T35)

### T35: `/search`

- [ ] **Step 1:** Tier1 layout, `<Search />` mount. `noIndex={true}`. Pagefind index built into `dist/pagefind/` automatically by the `pagefind` build step in `package.json`.
- [ ] **Step 2:** `pnpm preview` and verify search works against the built index.
- [ ] **Step 3:** Commit.

---

## Phase 10 — Assets + polish (T36-T39)

### T36: CNAME, robots.txt, favicons, OG default

- [ ] **Step 1: `site/public/CNAME`** — single line `hippobrain.org`.
- [ ] **Step 2: `site/public/robots.txt`** — `User-agent: *` `Allow: /` + sitemap reference.
- [ ] **Step 3: `site/public/favicon.svg`** — drastically simplified cornu Ammonis spiral within a 32-unit viewBox, `--ink` stroke.
- [ ] **Step 4: `site/public/favicon.ico`** + `apple-touch-icon.png` — generated from the SVG using a one-shot script (`pnpm dlx pwa-asset-generator` or manual export).
- [ ] **Step 5: One-shot OG generator** — `site/scripts/generate-og.ts` (run via `pnpm tsx`), uses `satori` + `@resvg/resvg-js`. Renders 1200×630 with paper background, "Plate I" badge, display "Hippo·campus.", motif on right, fig caption strap. Run once locally; commit `site/public/og-default.png`.
- [ ] **Step 6:** Commit.

### T37: Print stylesheet

- [ ] **Step 1: `site/src/styles/print.css`** — `@media print` block hiding header/footer/rails/skip-link, white background black text, `pre` with white bg + dark left border, links underlined with `[href]` appended for external, `page-break-after: avoid` on h1/h2, `page-break-inside: avoid` on `pre/blockquote/table/.marginalia/.admonition`, motifs at 0.6 opacity.
- [ ] **Step 2:** Browser-print one tier-1 page and one tier-2 page to PDF. Verify it looks like a textbook page.
- [ ] **Step 3:** Commit.

### T38: `site/CONTRIBUTING.md`

- [ ] **Step 1:** Write file with: local dev quickstart, "Things we don't do" list (D10 — no curlicue dividers, no quill cursors, no faux-distressed paper textures, no "ye olde" diction; no drop caps outside chapter first paragraph; no Latin twice in same viewport; no announce-y motion; no purple gradients; no "AI-powered"/"transform"/"supercharge"; no decorative emoji; no scope-promises), motif library reference, maintainer veto disclaimer, how-to-test commands.
- [ ] **Step 2:** Commit.

### T39: site README polish

- [ ] **Step 1:** Update `site/README.md` with full dev guide, build commands, link to spec, link to CONTRIBUTING, deployment status.
- [ ] **Step 2:** Commit.

---

## Phase 11 — CI + Deploy (T40-T41)

### T40: `.github/workflows/site-ci.yml`

- [ ] **Step 1: Workflow** — triggers on PRs touching `site/**`, `docs/**`, `README.md`, `CONTRIBUTING.md`, or the workflow itself. Steps: checkout (fetch-depth 0 for git timestamps), setup Node 20, setup pnpm 9, `pnpm install --frozen-lockfile` (working-dir `site`), `pnpm exec astro check`, `pnpm test`, `pnpm build`, `pnpm exec linkinator dist --recurse --skip "https?://github.com/.*/blob/.*"`, axe smoke (start `http-server dist`, run `pnpm exec axe http://localhost:4321/` `/install/` `/docs/capture/anti-patterns/` with `--exit`).
- [ ] **Step 2:** Commit.

### T41: `.github/workflows/site-deploy.yml`

- [ ] **Step 1: Workflow** — triggers: `push` to `main` matching same paths as CI, `schedule: cron: "0 6 * * *"`, `workflow_dispatch`. Permissions: contents: read, pages: write, id-token: write. Concurrency `pages` group with `cancel-in-progress: false`. Two jobs:
  - `build`: checkout (fetch-depth 0), setup Node 20 + pnpm 9, install, `pnpm build` with `GITHUB_TOKEN` env, `actions/configure-pages@v5`, `actions/upload-pages-artifact@v3` from `site/dist`.
  - `deploy`: needs `build`, environment `github-pages`, `actions/deploy-pages@v4`.
- [ ] **Step 2:** Commit.

---

## Phase 12 — Final verification (T42)

### T42: Build all, smoke, push branch, open PR

- [ ] **Step 1:** Final `pnpm install && pnpm build && pnpm preview` walkthrough on `/`, `/install`, `/why`, `/privacy`, `/faq`, `/docs`, `/docs/getting-started`, `/docs/capture/anti-patterns`, `/blog`, `/changelog`, `/status`, `/search`, `/404`. Resize to 375px and 768px to verify responsive.
- [ ] **Step 2:** `pnpm check && pnpm test && pnpm exec linkinator dist --recurse --skip "https?://github.com/.*/blob/.*"`.
- [ ] **Step 3:** Push branch, open PR with the hand-off notes below.

---

## Self-Review

**Spec coverage:**
- D1 hybrid posture — Tier1/Tier2 layouts + page composition.
- D2 same-repo /site — file structure (T1).
- D3 anatomical naturalism — aesthetic system (T3-T7) and pages.
- D4 Astro custom — T1.
- D5 Source Serif 4 tier-2 body — T2 fonts, T3 tokens, T6 tier-2.css.
- D6 wordmark + cornu Ammonis — T8 Wordmark, T36 favicon.
- D7 two-tier — Tier1 (T8) + Tier2 (T9).
- D8 lights-low tokens — T3 tokens.css `[data-theme="dark"]`.
- D9 Latin frequency — T38 CONTRIBUTING.
- D10 twee policing — T38 CONTRIBUTING.

**IA / pages:** `/` T17, `/install` T18, `/why` T19, `/privacy` T20, `/faq` T21, `/changelog` T30, `/blog` T32, `/blog/<slug>` T33, `/status` T31, `/docs` T27, `/docs/<slug>` T27, `/search` T35, `/404` T22. Sitemap auto. Robots T36. CNAME T36. RSS T34.

**Tech:** Content collections T23. Rehype pipeline T24-T26. Edit on GitHub T25+T9. Pagefind T1+T35. OG static T36. GitHub API T29. Build/deploy workflows T40-T41. CSS T3-T6. Fonts T2. Print T37. Site CONTRIBUTING T38.

**Placeholder scan:** none — every step has concrete code or commands.

**Type consistency:** `MotifId`, `SidebarEntry`, `Release`, `CommitInfo`, `CIRun`, `DocSection` — names stable across tasks.

**Gaps from spec (deferred, documented):**
- Per-page Satori OG cards — single static `og-default.png` for v1.0 (T36); per-page deferred to follow-up.
- Lights-low dark-mode toggle UI — tokens in T3, no toggle in v1.0.
- Custom Pagefind result UI — using Pagefind defaults with sepia palette overrides via CSS variables; sufficient for v1.0.

**Security note:** All shell-outs use `execFileSync` with args arrays. No `execSync` with template strings anywhere in the codebase.

---

## PR Hand-off Notes

When the implementation lands, the PR description should include:

**What's done in this PR:**
- Site at `/site` deploys to GitHub Pages
- Tier-1 marketing pages (`/`, `/install`, `/why`, `/privacy`, `/faq`, `/changelog`, `/status`, `/blog`, `/404`)
- Tier-2 docs pages (auto-generated from `/docs/**` markdown plus root README and CONTRIBUTING)
- Search via Pagefind
- RSS feeds for blog and changelog
- CI (`site-ci.yml`) and deploy (`site-deploy.yml`) workflows

**What you (steven) need to do before this works on hippobrain.org:**
1. **Repo settings → Pages** — set source to "GitHub Actions" (one-time).
2. **Cloudflare DNS** — add a CNAME record `hippobrain.org → stevencarpenter.github.io` with CNAME flattening enabled at apex; CNAME `www → stevencarpenter.github.io`.
3. **Cloudflare Redirect Rules** (free tier): add four rules:
   - `/git` → `https://github.com/stevencarpenter/hippo` (302)
   - `/issues` → `https://github.com/stevencarpenter/hippo/issues` (302)
   - `/releases` → `https://github.com/stevencarpenter/hippo/releases` (302)
   - `/install.sh` → `https://github.com/stevencarpenter/hippo/releases/latest/download/install.sh` (302)
4. **Cloudflare proxy**: orange-cloud both records for caching + Universal SSL.
5. **(Optional)** Cloudflare Web Analytics token — paste into a `<script>` in Head if/when desired.

**Follow-up issues to file** (deferred items):
- Per-page OG image generation via Satori
- Lights-low dark-mode toggle UI
- Custom Pagefind result UI to better match anatomical aesthetic
