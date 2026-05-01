# hippobrain.org — Project Site Design

**Status:** Draft (brainstorm → spec)
**Author:** steven + Claude (brainstorm session 2026-05-01)
**Domain:** hippobrain.org (Cloudflare-managed)
**Target branch:** TBD (post-approval)

## Motivation

Hippo is a real, working, MIT-licensed open-source project — but it lives entirely on GitHub. There's no public face: nowhere to point a curious developer who's heard the elevator pitch but doesn't want to read a 20KB README to find the install command, no way to socialise the privacy story, no place to write field notes.

The user has acquired `hippobrain.org`. This spec defines the site to put behind it: a hybrid landing-page-and-docs surface, tightly coupled to the GitHub repo's existing markdown, with a distinctive aesthetic that's recognisable to anyone who later sees a screenshot in a tweet.

## Goals

1. **Convince a skeptic in 6 seconds.** Tier-1 marketing pages communicate, in plain English, what hippo *actually* is and who it's for. No hype.
2. **Respect a careful reader for 6 minutes.** Tier-2 docs render every existing markdown file in `docs/` (minus archive/superpowers) with a docs UX that doesn't apologise.
3. **Tightly coupled to repo state.** Site builds from the same checkout as the daemon. If a docs PR merges, the site rebuilds. There is one source of truth and it lives in `docs/`.
4. **Distinctive enough to remember.** Anatomical-naturalism aesthetic, executed with intent. The hippocampus motif is load-bearing, not decorative.
5. **Operationally simple.** Static output, GitHub Pages hosting, Cloudflare DNS, no server, no database, no analytics-with-cookies. Same release ecosystem as the daemon — one repo, one CI, one place to look.

## Non-Goals

- No CMS, no backend, no logged-in surface.
- No newsletter, no waitlist, no testimonial component, no avatar wall.
- No "Made with hippo" / "Adopters" page until there are real names to put on it (designer flagged: empty adopters pages signal the project is in trouble).
- No mobile-app pretensions. Hippo is macOS-only; the site is for reading, not for native-app feel.
- No live data from a running hippo daemon (privacy + ops complexity). `/status` is build-time only.
- No JS-heavy interactivity. Astro islands only where they earn their place (search, copy-button).

## Design Constraints

1. **Audience is narrow.** Developers who use Claude Code or another agentic-coding tool and want their machine to remember context across sessions. Site copy must not pretend hippo is for general consumers.
2. **Honest scope.** Today's sources are zsh shell activity, Claude Code session JSONLs, and Firefox browsing within an allowlist. The site copy describes what hippo does *today*. No "future support for X" language.
3. **No overhype.** No "AI-powered", no "transform your workflow", no "the future of [anything]". Concrete > grand. Specific > sweeping.
4. **macOS arm64 today.** Don't pretend Linux/Windows/Intel are coming. They aren't promised.
5. **Open-source posture.** "Edit this page on GitHub" link on every doc page. CONTRIBUTING.md and the existing capture-reliability rulebook are first-class content.

---

## Decisions & Rationale (with full gamut)

The following decisions are locked. Every alternative considered is documented here so we can revisit if a decision turns out to be wrong.

### D1 — Site posture: hybrid

| Option | Description | Selected? |
|---|---|---|
| A | Marketing-forward landing page; docs are a destination | — |
| **B** | **Hybrid 50/50 — landing page distinct from docs, both first-class** | **✅** |
| C | Docs-as-front-door; site IS the docs | — |

**Why hybrid:** A undersells the docs work that already exists; B undersells the project's pitch.

### D2 — Repo placement: same-repo `/site`

| Option | Description | Selected? |
|---|---|---|
| **A** | **Same repo, `/site` directory** | **✅** |
| B | Separate repo (`hippobrain-site`) with sync mechanism | — |
| C | `gh-pages` / docs branch | — |

**Why same-repo:** "Tightly coupled to repo state" is exactly what was asked for — same-repo is the literal interpretation. Single PR for code+docs change. GitHub Actions `paths:` filter on the workflow trigger keeps non-site changes from rebuilding the site.

### D3 — Aesthetic direction: Anatomical Naturalism

| Option | Description | Selected? |
|---|---|---|
| A | Terminal Brutalist — monospace, sharp borders, terminal-green | — |
| B | Editorial Literary — Fraunces serif on cream, magazine grid | — |
| **C** | **Anatomical Naturalism — old textbook plate, hippocampus illustrations, latin labels, sepia ink on aged paper** | **✅** |

**Why C:** The hippocampus motif is a gift built into the project's name. Most memorable, most differentiated, hardest to forget. Risk: hardest to keep consistent at docs scale → mitigated by the two-tier execution (D7).

### D4 — Tech stack: Astro, custom design (no Starlight)

| Option | Description | Selected? |
|---|---|---|
| **A** | **Astro, custom design** | **✅** |
| B | Astro + Starlight | — |
| C | VitePress | — |
| D | Next.js + Nextra | — |
| E | Hand-rolled (Pandoc + plain HTML) | — |

**Why custom Astro:** Anatomical naturalism fights pre-built docs themes. Astro's content collections are exactly the docs-coupling mechanism we need. Pagefind for static search. Static output deploys to GitHub Pages without any host-specific adapter. Going without Starlight means we don't bend a "modern docs" chrome — we get the aesthetic clean.

### D5 — Tier-2 docs body face: Source Serif 4

| Option | Description | Selected? |
|---|---|---|
| **A** | **Source Serif 4 for docs body only; Fraunces for h1/h2 and tier-1** | **✅** |
| B | All-Fraunces with opsz 14 for body | — |

**Why Source Serif 4:** Built for sustained reading. Calmer than Fraunces at body sizes. Tier-2 docs are 4,000-word reference pages where reading fatigue compounds. The 25KB bundle cost is worth it. Fraunces still does h1/h2 in tier-2 to maintain visual continuity with tier-1.

### D6 — Brand mark: wordmark + cornu Ammonis spiral

| Option | Description | Selected? |
|---|---|---|
| A | Wordmark only — "Hippo·campus." in Fraunces | — |
| **B** | **Wordmark + separable motif (cornu Ammonis spiral)** | **✅** |
| C | Defer to implementation | — |

**Why B:** Defer is the trap — every PR with a screenshot will use whatever placeholder ships first. The motif is a hand-drawn cornu Ammonis spiral that exists at three sizes: full (chapter mark, ~120×120), reduced (favicon+social, ~32×32, drastically simplified), and inline (header lockup mark, ~24×24).

### D7 — Two-tier execution

The aesthetic is dialed up on tier-1 marketing pages and dialed back on tier-2 docs pages. Same publication, different sections.

| Property | Tier-1 (marketing) | Tier-2 (docs) |
|---|---|---|
| Pages | `/`, `/install`, `/why`, `/privacy`, `/blog/index`, `/faq`, `/changelog`, `/status`, `/404` | `/docs/**` |
| Paper | `--paper #efe4ce` (deeper) | `--paper-2 #f5efe2` (lighter, easier on eyes for length) |
| Display face | Fraunces, opsz 144, SOFT 70 | Fraunces, opsz 36 |
| Body face | Fraunces opsz 16 | **Source Serif 4** opsz 16 |
| Decorative illustrations | Yes — anatomical SVG at section breaks, hero, plate dividers | No — chapter mark only at top of each top-level docs section |
| Drop caps | Yes — first paragraph of a chapter only | No |
| Plate badges | Yes | Only at chapter mark |
| Marginalia/callouts | Yes | Yes (admonitions: note/warn/danger styled as marginalia variants) |
| Typescale density | Generous | Tight |
| Whitespace | Generous | Disciplined |

### D8 — Dark mode: tokens specced, implementation post-launch

| Option | Description | Selected? |
|---|---|---|
| A | Skip dark mode entirely | — |
| **B** | **Spec "lights-low" tokens now; implementation in v1.0 if room, else v1.1** | **✅** |
| C | Defer entirely | — |

**Why B:** Audience is developers in dark-mode at 11pm. Skipping leaves real usability on the table. But it's not launch-blocking. We define the tokens (cream-on-dark, oxblood links survive, sepia warms preserved) so the half-day prototype isn't speculative — it's mechanical when it lands.

**Lights-low tokens** (defined now, implemented when):

```
--paper:    #1f1812
--paper-2:  #261d15
--ink:      #efe4ce
--sepia:    #c39a76
--oxblood:  #d97a5e   (oxblood lifted for dark contrast)
--rust:     #c89968   (decorative only; same use restriction)
--quiet:    #8b7860
--code-bg:  #2a1f17
--rule:     rgba(239,228,206,0.32)
--rule-soft:rgba(239,228,206,0.15)
```

### D9 — Latin frequency budget: once per top-level page, never twice per viewport

Locked. Bake into `CONTRIBUTING.md` and code-review guidance.

### D10 — Twee policing: written rules + maintainer veto

| Option | Description | Selected? |
|---|---|---|
| A | Written "things we don't do" list in CONTRIBUTING | — |
| B | Maintainer veto on tier-1 visual additions | — |
| **C** | **Both** | **✅** |

**Why C:** Aesthetic erodes at PR boundaries unless governed. A maintainer veto without a written list is unfair to contributors; a written list without a veto isn't enforceable.

**Things we don't do** (written rule, gets shipped in CONTRIBUTING.md):
- No curlicue dividers, no quill cursors, no "ye olde" diction, no faux-distressed paper textures.
- No drop caps outside of "first paragraph of a chapter".
- No Latin twice in the same viewport.
- No motion that announces itself (no "scroll to reveal", no carousel, no parallax marketing).
- No purple gradients, no glowing buttons, no "AI-powered", no startup-y verbs ("transform", "supercharge", "unleash").
- No emoji decorations in marketing copy. Code blocks where they're meaningful (✓ shell prompt, ⚠ admonitions) only.

---

## Information Architecture

### Sitemap

```
/                          home — hero, what hippo is, install one-liner, see-it-work demo
/install                   install instructions (curl-bash, manual build, doctor walkthrough, uninstall)
/why                       what hippo solves (agentic-coding context loss), what it doesn't
/privacy                   data-flow story; what's captured, redacted, stored, transmitted
/changelog                 auto-imported from GitHub Releases at build time
/blog                      "field notes" — markdown posts under /site/src/content/blog/
/blog/<slug>               individual post
/blog/rss.xml              RSS 2.0 feed
/status                    live (build-time) signal: latest release, latest CI run, open issues, last commit
/faq                       common-question doc; one question = one entry
/docs                      docs landing — links into the doc collections
/docs/getting-started      from README.md
/docs/contributing         from CONTRIBUTING.md
/docs/reference/<doc>      from docs/*.md
/docs/capture/<doc>        from docs/capture/*.md
/docs/diagrams/<asset>     image proxy from docs/diagrams/
/search                    search results (Pagefind UI on a static page)
/sitemap.xml               sitemap (Astro plugin)
/robots.txt                robots
/404                       themed 404 ("Plate not found — fig. 404")
```

**Search** runs across `/docs/**`, `/blog/**`, `/why`, `/privacy`, `/faq`. Index built at deploy via Pagefind.

**Excluded from publish** (intentionally): everything under `docs/archive/`, everything under `docs/superpowers/`, anything in the `private/` or `internal/` subdirectories of the repo (none currently exist; convention reserved).

### Page-by-page composition

#### `/` (homepage)

| Section | Goal | Composition |
|---|---|---|
| Plate I — Hero | Pitch in 6 seconds | Plate badge top-left ("Plate I"). Display: "Hippo·campus." with sepia mid-dot. Italic h2: "— *memoriae custos.*" Body line in plain English: "A local-first memory layer for your shell, your Claude Code sessions, and your browsing — within an allowlist you control." Install one-liner in a code block with copy button. Cornu Ammonis SVG illustration on the right, breathing animation. Two buttons: "Install" (primary), "Read the docs" (ghost). |
| Plate II — What hippo captures, exactly | Honest scope | Three columns: "Shell" (zsh hook), "Claude Code" (session JSONLs via FSEvents), "Firefox" (allowlisted domains via WebExtension Native Messaging). Each with a one-paragraph description and the actual file path or extension link. No vague "knowledge tool" language. |
| Plate III — How it works | Architecture in one diagram | The existing ASCII architecture diagram from README.md (the daemon ↔ brain ↔ MCP boxes), redrawn as a clean SVG with the same labels. One-paragraph explainer below. |
| Plate IV — Privacy | Build trust without preaching | Three short claims with the proof inline: "All inference local — LM Studio on your GPU." "No telemetry by default — when on, points at localhost." "Secrets redacted before storage — review the patterns at /privacy." Link to /privacy for the long version. |
| Plate V — See it work | Concrete demo | Code block showing `hippo doctor` output (real, redacted), then `hippo ask "..."` with a synthesized answer + cited sources. No video, no carousel — just text-as-screenshot. |
| Plate VI — Field notes | Recent activity, signals project is alive | Latest 3 blog posts as titled rows with date and excerpt. "All field notes →" link. **If no blog posts exist at launch, this entire plate is omitted from the homepage** (don't ship an empty state — better silent than thin). |
| Footer | Navigation + colophon | Links: GitHub, license, contributing, RSS. Colophon: "Set in Fraunces and Source Serif 4. Hand-drawn anatomy in pen and ink." Build commit hash + date. |

#### `/install`

Plate I hero with the install command repeated. Three sections:

1. **Quick install (Apple Silicon)** — the curl-bash one-liner with an "About this script" disclosure that links to its source on GitHub.
2. **Manual installation** — for Intel Macs and contributors. Cargo build, brain setup, LaunchAgent install, doctor verification.
3. **Verify it's working** — `hippo doctor` walkthrough with a sample of expected output, link to `docs/capture/operator-runbook.md` for failure modes.
4. **Uninstall** — explicit, never hidden.

#### `/why`

The pitch in long-form. Targets the developer who has 90 seconds.

- Section 1: "The problem hippo actually solves" — agentic coding context loss across sessions. Concrete example. No "imagine if" framing.
- Section 2: "What hippo is not" — not Atuin, not Rewind, not Apple Continuity, not Roam, not Notion. One-sentence each.
- Section 3: "Who hippo is for" — explicitly developers using agentic coding tools on macOS arm64.
- Section 4: "What hippo will never do" — phone home, sync to cloud, share data with third parties, run a server.

#### `/privacy`

Long-form data-flow story, technical and non-defensive. Sources are the existing `docs/redaction.md` plus a one-page summary of the architecture from a privacy lens. Diagrams: the data flow with a "stays on machine / leaves machine" overlay.

#### `/faq`

One question per entry, anchor-linked. Initial set:

1. Does hippo work on Linux/Windows? → No. macOS arm64 only today.
2. Does hippo send data anywhere? → No. All inference is local via LM Studio.
3. What if I have secrets in my shell history? → See `/privacy` for the redaction patterns.
4. Will hippo work with [Cursor/Aider/Continue]? → Today, only Claude Code session JSONLs. Other agentic tools may be supported in the future, but no promises.
5. How much disk space does hippo use? → Concrete numbers: ~50MB for the daemon binaries, varies by usage for the SQLite database (typical: 200-500MB after a month of heavy use).
6. How much memory? → Daemon ~30MB. Brain spawns LM Studio inference on demand; LM Studio's memory footprint depends on the model.
7. Does the brain run all the time? → Yes, as a launchd-managed service. It's idle most of the time, polling enrichment queues.
8. What models are supported? → Any LM Studio-compatible model. Currently tested with Qwen 3.5 35B-A3B; configurable in `~/.config/hippo/config.toml`.
9. Can I export my data? → Yes. SQLite at `~/.local/share/hippo/hippo.db`. Standard SQL.
10. Can I uninstall cleanly? → Yes. `hippo daemon uninstall` removes LaunchAgents and binaries. Database stays unless you `rm` it.
11. Is hippo open source? → Yes, MIT. GitHub link.
12. How do I contribute? → CONTRIBUTING.md link, plus blurb about the capture-reliability rulebook (`docs/capture/anti-patterns.md`).

#### `/changelog`

Auto-imported from GitHub Releases at build time. Each release becomes one entry with: tag, date, title, body (rendered markdown), link to the release on GitHub. RSS feed at `/changelog/rss.xml` for changelog-only subscribers.

#### `/status`

Build-time fetched, statically rendered. Sections:
- Latest release: tag, date, days-since-last-release. Green if <30 days, yellow <90, red otherwise.
- Latest CI run: status, commit, date.
- Open issues: count, link.
- Last commit: hash, message, date.
- "Hippo is" — a single one-liner: "Active" / "Quiet" / "Hibernating" computed from those signals.

The page itself is static HTML rebuilt nightly via the scheduled `site-deploy.yml` workflow (cron) that re-runs the build and re-publishes to GitHub Pages.

#### `/blog` and `/blog/<slug>`

Index page lists posts with title, date, excerpt. Individual post is full-width single-column with anatomical illustration at the top (per-post motif chosen from the library), drop cap on first paragraph, marginalia for asides. RSS feed at `/blog/rss.xml`.

Blog posts live in `site/src/content/blog/*.md`. Frontmatter:

```yaml
---
title: "Why I built hippo's redactor wrong twice"
date: 2026-05-14
description: "Two false starts and one principle that survived."
motif: cornu-ammonis  # one of: cornu-ammonis, sectio-coronalis, trisynaptic-circuit, marginalia
---
```

#### `/docs/**`

Tier-2 layout. See "Aesthetic system" → "Two-tier execution".

Three-column grid on ≥768px:
- **Left rail**: section nav (Capture, Reference, Contributing, Getting started). Active item bolded in oxblood.
- **Main**: breadcrumb, h1, italic lede, body. "Edit on GitHub" footer with last-updated timestamp.
- **Right rail**: "On this page" outline (h2/h3). Active section highlighted on scroll.

Below 768px: right rail collapses first. Below 480px: left rail collapses to a `<details>` toggle below the breadcrumb.

#### `/404`

Themed. Plate badge "Plate not found — fig. 404". Display heading: "The page is missing." Italic h2: "— or it never existed." Link list: home, docs, search. Do not fake-cute it.

---

## Marketing Voice & Tone

### Tagline ladder (locked)

| Surface | Copy |
|---|---|
| Hero (display, screenshotable) | **"Memoriæ custos."** |
| Hero (italic supporting line) | "— the keeper of memory." |
| Hero (plain English body) | "A local-first memory layer for your shell, your Claude Code sessions, and your browsing — within an allowlist you control." |
| `<title>` and OG description | "Hippo — a second brain that lives on your machine." |

### Voice rules (locked)

**Do:**
- Name what hippo captures, exactly: zsh activity, Claude Code session JSONLs, Firefox browsing within an allowlist.
- Name the audience: developers using agentic coding tools on macOS arm64.
- Name what hippo doesn't do: no iCloud, no mobile, no cloud sync.
- Use anatomical / library / cataloguing metaphors where they're honest (cornu Ammonis, fasciculus, marginalia, plate, fig.) and only there.
- Use Latin once per top-level page, never twice in the same viewport.
- Be specific: "200-500MB after a month of heavy use" beats "minimal disk footprint".

**Don't:**
- "AI-powered", "powered by AI", "transform your workflow", "supercharge", "the future of...", "join the waitlist".
- Promise expansion ("future support for X", "soon supports Y").
- Vague "knowledge tool" language that overstates capture.
- Pretend hippo is for non-developers.
- Use emoji decoratively in marketing copy.
- Use drop caps outside of the first paragraph of a chapter.

---

## Aesthetic System

### Color tokens

```
--paper:      #efe4ce      paper, tier-1
--paper-2:    #f5efe2      paper, tier-2 docs (lighter for length)
--ink:        #2a1d10      primary text (13.00:1 on paper, AAA)
--sepia:      #6b3c20      labels, eyebrows, fig captions (7.27:1, AA-AAA)
--oxblood:    #8a3a1f      links, emphasis (6.16:1 on paper, AA-body)
--rust:       #b58a4b      DECORATIVE ONLY — illustration strokes, hatch (fails AA at small text by design; never used for text)
--quiet:      #7a6649      muted body text, code comments (4.55:1, AA-body)
--code-bg:    #e9ddc5      code block background
--rule:       rgba(42,29,16,0.32)    functional dividers (3.5:1, 1.4.11 compliant)
--rule-soft:  rgba(42,29,16,0.15)    decorative paper rules
```

Lights-low (D8) tokens are listed in D8 above.

### Typography

```
--font-display: "Fraunces", "Source Serif 4", serif
--font-body-1:  "Fraunces", "Source Serif 4", serif      tier-1 body
--font-body-2:  "Source Serif 4", "Fraunces", serif      tier-2 docs body
--font-label:   "Junicode", "Fraunces", serif            plate badges, fig captions, latin labels
--font-mono:    "JetBrains Mono", ui-monospace, monospace
```

**Why Junicode (not Old Standard TT):** Designer review flagged Old Standard TT as a 16-year-old free clone with weak hinting at small sizes — exactly where it would be used (11–13px labels). Junicode is a Caslon-ish revival with proper hinting, complete italic, small caps, Latin extended, and a variable weight axis. Honest pairing with Fraunces.

**Type scale (tier-1):**

| Role | Family | Size | Weight | Notes |
|---|---|---|---|---|
| Display | Fraunces | 52px | 350 | opsz 144, SOFT 70, letter-spacing -0.02em |
| h1 | Fraunces | 32px | 400 | opsz 36 |
| h2 | Fraunces italic | 23px | 400 | sepia color, SOFT 100 |
| h3 | Fraunces | 18px | 600 | |
| Body | Fraunces | 16px | 400 | line-height 1.6, max-width 60ch |
| Eyebrow | Junicode | 11px | regular | letter-spacing 0.28em, uppercase, sepia |
| Mono | JetBrains Mono | 14px | 500 | line-height 1.55 |

**Type scale (tier-2):** display 32px, h1 28px, h2 20px italic, h3 16px, body 16px in Source Serif 4 (line-height 1.65), eyebrow 10px Junicode.

**Responsive (≤480px):** display 36px, h1 24px, h2 18px, body unchanged.

### Components

- **Code block.** Sepia-friendly highlight scheme. 14px JetBrains Mono. Left edge has a 6px sepia bar (visual anchor that survives in print). Copy button at top-right with 600ms sepia flash on success and a screen-reader announcement. Comments use `--quiet` (`#7a6649`, AA).
- **Link.** Underlined with `--rule` 1px. On hover: ink color shifts to `--oxblood`, underline darkens to `--oxblood`. No grow-from-left animation (designer flagged as SaaS tic).
- **Button.** No border-radius. `--ink` background, `--paper` text, Junicode label, letter-spacing 0.18em, uppercase. Hover: background to `--oxblood`. Ghost variant: transparent background, `--ink` text, `--ink` 1px border.
- **Drop cap.** First paragraph of a chapter only. Fraunces 600 weight, 3.4rem, oxblood, float left, 0.18rem 0.55rem 0 0 padding.
- **Plate badge.** Junicode 11px uppercase, letter-spacing 0.34em, sepia color, sepia 1px border, 2px 9px padding.
- **Fig caption.** Junicode 12px italic, sepia, centered.
- **Marginalia / callout.** 2px sepia left border, 0.4rem 0 0.4rem 0.9rem padding, italic sepia text. Prefix: ¶ in oxblood.
- **Admonitions** (tier-2 docs). Marginalia variants:
  - Note → sepia border (default).
  - Warn → rust border, sepia text.
  - Danger → oxblood border, oxblood text, prefix † instead of ¶.
- **Footnote.** 13.5px, line-height 1.55, ink color, sepia 1px top border, 0.5rem padding-top. Reference numbers in italic oxblood.
- **Table.** Junicode header in small caps, sepia. Sepia 1px top/bottom rule on `<thead>`. Body cells in body face. Zebra striping with `--rule-soft`.
- **Form input.** Sepia 1px bottom border (no full border). Ink text. No border-radius. Focus state: oxblood bottom border, no ring.
- **Focus ring.** Custom 2px oxblood ring with 1px paper offset (replaces default browser blue, which would jar against cream).
- **Heading anchor link.** Pilcrow (¶) in oxblood that appears on hover/focus, 0.5em to the left of the heading text.

### Motif library

Hand-drawn SVGs at three sizes: full (chapter mark, ~120×120), reduced (favicon + social card, ~32×32), inline (header lockup, ~24×24).

| ID | Description | First use |
|---|---|---|
| `cornu-ammonis` | Hippocampus side view, the seahorse-curl | Homepage hero, capture docs chapter mark |
| `sectio-coronalis` | Coronal section / cross-section | Reference docs chapter mark |
| `trisynaptic-circuit` | CA1/CA2/CA3/DG nodes with labeled connections | "How it works" section, schema docs chapter mark |
| `marginalia` | Hand-drawn quill ribbon / page-edge motif | Blog index, contributing docs chapter mark |
| `plate-frame` | Decorative plate frame with corner ornaments | Hero, /404 |
| `fasciculus` | Bundle-of-fibers illustration | Privacy chapter mark |

Every chapter mark is paired with a Latin caption in Junicode italic.

### Motion principles

- **Restrained, never decorative.** No slide-in carousels, no scroll-triggered marketing, no parallax.
- **Hero illustration breathes** on an 8s ease-in-out cycle (scale 1.00 → 1.03). `prefers-reduced-motion: reduce` → static.
- **Link hover**: 200ms color shift on text and underline. No translate.
- **Page transitions**: 180ms cross-fade via Astro `<ViewTransitions />`. Nothing else.
- **Code copy button**: 600ms sepia flash + sr-only announcement on copy.
- **Chapter mark fade-in** on first paint: 320ms opacity 0 → 1, no slide.
- **Search results**: 120ms fade-in on result rows as the user types.

### Accessibility

- All text/UI tokens above 4.5:1 (body) or 3:1 (large text or non-text component).
- `prefers-reduced-motion: reduce` short-circuits all motion.
- Focus visible on every interactive element with custom ring.
- Skip-to-content link as first focusable element on every page.
- All images have alt text; SVG illustrations have `<title>` + `aria-labelledby`.
- Heading hierarchy never skips levels.
- Color is not the only way information is conveyed (e.g., status badges have icons or text labels in addition to color).

### Print stylesheet

Anatomical-naturalism docs print beautifully. Treat as a real surface:
- Code blocks: white background, full ink text, no left-bar (it carries no print meaning).
- Links: ink color, underlined, with `[domain]` appended after external links.
- Page break controls: `page-break-after: avoid` on h1/h2, `page-break-inside: avoid` on code blocks, callouts, tables.
- No nav, no footer, no rails.

---

## Technical Architecture

### Repo layout

```
hippo/                       (root)
├── site/                    Astro project
│   ├── astro.config.mjs
│   ├── package.json
│   ├── tsconfig.json
│   ├── public/              static assets (favicon, social, fonts if self-hosted)
│   ├── src/
│   │   ├── content/
│   │   │   ├── blog/        *.md (hand-written field notes)
│   │   │   └── config.ts    content collection schema
│   │   ├── pages/
│   │   │   ├── index.astro
│   │   │   ├── install.astro
│   │   │   ├── why.astro
│   │   │   ├── privacy.astro
│   │   │   ├── faq.astro
│   │   │   ├── changelog.astro     (build-time fetch GitHub Releases)
│   │   │   ├── status.astro        (build-time fetch GitHub state)
│   │   │   ├── blog/
│   │   │   │   ├── index.astro
│   │   │   │   ├── [...slug].astro
│   │   │   │   └── rss.xml.ts
│   │   │   ├── docs/
│   │   │   │   ├── index.astro
│   │   │   │   └── [...slug].astro
│   │   │   ├── search.astro
│   │   │   └── 404.astro
│   │   ├── layouts/
│   │   │   ├── Tier1.astro
│   │   │   ├── Tier2.astro
│   │   │   └── BlogPost.astro
│   │   ├── components/
│   │   │   ├── Hero.astro
│   │   │   ├── PlateBadge.astro
│   │   │   ├── Marginalia.astro
│   │   │   ├── ChapterMark.astro
│   │   │   ├── CodeBlock.astro
│   │   │   ├── DocsRail.astro
│   │   │   ├── DocsOutline.astro
│   │   │   ├── ChangelogEntry.astro
│   │   │   ├── StatusBadge.astro
│   │   │   ├── Search.astro
│   │   │   └── motifs/
│   │   │       ├── CornuAmmonis.astro
│   │   │       ├── SectioCoronalis.astro
│   │   │       ├── TrisynapticCircuit.astro
│   │   │       ├── Marginalia.astro
│   │   │       ├── PlateFrame.astro
│   │   │       └── Fasciculus.astro
│   │   ├── lib/
│   │   │   ├── docs-loader.ts          read ../../docs/**/*.md
│   │   │   ├── rehype-link-rewrite.ts  repo-relative → site-relative
│   │   │   ├── rehype-edit-on-github.ts inject "Edit on GitHub" link
│   │   │   ├── github.ts               octokit client for changelog/status
│   │   │   └── og-image.ts             build-time OG card generation
│   │   └── styles/
│   │       ├── tokens.css              all CSS custom properties
│   │       ├── tier-1.css
│   │       └── tier-2.css
│   └── README.md
├── docs/                    (existing — source of truth for docs site)
└── README.md, CONTRIBUTING.md  (existing — also rendered by site)
```

### Astro setup

- `@astrojs/mdx` for MDX support in `site/src/content/blog/**.md` (Astro markdown is sufficient for `docs/**`).
- `@astrojs/sitemap` for `/sitemap.xml`.
- **Fonts self-hosted** (Latin subset, woff2). Consistent with hippo's local-first ethos — no Google Fonts CDN call from the user's browser, no third-party logging of visitors. Source files in `site/public/fonts/`. Total payload <450KB across all four families.
- **No host-specific adapter.** Astro's default static output (`output: "static"`) is sufficient for GitHub Pages. No `@astrojs/cloudflare` or `@astrojs/node` needed.
- `astro-pagefind` integration for static search index.
- `@shikijs/transformers` for code highlighting with a custom sepia theme.
- View transitions enabled at the layout level.

### Content collections

```ts
// site/src/content/config.ts
import { defineCollection, z } from "astro:content";
import { glob, file } from "astro/loaders";

const docs = defineCollection({
  loader: glob({
    pattern: ["**/*.md"],
    base: "../docs",
    // Exclude internal/in-progress directories
    exclude: ["archive/**", "superpowers/**", "**/.DS_Store"],
  }),
  schema: z.object({
    title: z.string().optional(),    // derived from h1 if absent
    description: z.string().optional(),
    order: z.number().optional(),    // sidebar ordering
  }),
});

const rootDocs = defineCollection({
  loader: file("../README.md"),  // README.md becomes /docs/getting-started
  // Custom transform to set slug, title, etc.
});

const contributing = defineCollection({
  loader: file("../CONTRIBUTING.md"),  // CONTRIBUTING.md → /docs/contributing
});

const blog = defineCollection({
  loader: glob({ pattern: "**/*.md", base: "./src/content/blog" }),
  schema: z.object({
    title: z.string(),
    date: z.coerce.date(),
    description: z.string(),
    motif: z.enum([
      "cornu-ammonis", "sectio-coronalis", "trisynaptic-circuit",
      "marginalia", "plate-frame", "fasciculus",
    ]).default("cornu-ammonis"),
    draft: z.boolean().default(false),
  }),
});

export const collections = { docs, rootDocs, contributing, blog };
```

### Markdown transform pipeline (rehype)

1. **`rehype-link-rewrite`** — rewrites repo-relative markdown links:
   - `[text](../capture/architecture.md)` → `[text](/docs/capture/architecture)`
   - `[text](docs/redaction.md)` → `[text](/docs/reference/redaction)`
   - `[text](README.md)` → `[text](/docs/getting-started)`
   - `[text](CONTRIBUTING.md)` → `[text](/docs/contributing)`
   - `[text](https://github.com/stevencarpenter/hippo/blob/main/docs/...)` → site-relative
   - External links unchanged; add `target="_blank" rel="noopener"` and an `↗` glyph.
2. **`rehype-image-resolve`** — `<img src="../diagrams/foo.png">` → import via `astro:assets`. Falls back to GitHub raw URL if the image lives outside the site bundle path.
3. **`rehype-shiki`** — code highlighting with a custom sepia theme; theme json in `site/src/styles/shiki-anatomical.json`.
4. **`rehype-autolink-headings`** — adds id and pilcrow anchor on each h2/h3.
5. **`rehype-edit-on-github`** — appends "Edit on GitHub" link footer using the source path captured during loading. Includes "last updated" derived from `git log -1 --format=%ci -- <path>` at build time.

### Search

Pagefind. Index built post-build via the `astro-pagefind` integration. Indexed surfaces: `/docs/**`, `/blog/**`, `/why`, `/privacy`, `/faq`. UI is custom (Pagefind's default doesn't fit the aesthetic): a single search input on `/search`, results rendered as plate-badged cards. Index ships as a static asset; total payload <100KB.

### Changelog & status: build-time GitHub fetches

`changelog.astro` and `status.astro` are top-level pages that fetch from the GitHub API at build time using the workflow's `GITHUB_TOKEN` (auto-provided by GitHub Actions, scoped to the repo). They render the data into static HTML — **no runtime JS, no client-side fetch.**

Scheduled deploys: the `site-deploy.yml` workflow has a `schedule: cron: "0 6 * * *"` trigger (daily 06:00 UTC) that re-runs the build and re-publishes to GitHub Pages so `/changelog` and `/status` stay current without code changes.

### OG image generation

Build-time OG card generation via `@vercel/og` (Satori-based). Template:
- 1200×630, `--paper` background, paper texture.
- Plate badge top-left ("Plate I" / blog post title acronym).
- Display title (Fraunces) on left.
- Motif (chosen per page; defaults to `cornu-ammonis`) on right.
- Junicode fig. strap below.
- Generated at build time per page; static `<meta property="og:image">` reference.

### Redirects (Cloudflare DNS Redirect Rules)

GitHub Pages doesn't support a `_redirects` file. Short-URL redirects are configured as **Cloudflare DNS Redirect Rules** (free tier covers a handful — easily enough for the four below). Configured at the apex `hippobrain.org`, these run before any GitHub Pages traffic so the request never hits the origin:

```
/git           → https://github.com/stevencarpenter/hippo                                       302
/issues        → https://github.com/stevencarpenter/hippo/issues                                302
/releases      → https://github.com/stevencarpenter/hippo/releases                              302
/install.sh    → https://github.com/stevencarpenter/hippo/releases/latest/download/install.sh   302
```

`/install.sh` is the live target of the curl-bash one-liner — it stays a redirect to the latest GitHub release asset; we never serve installer code from hippobrain.org.

---

## Deployment & Ops

### Hosting: GitHub Pages with Cloudflare DNS

Hosting and DNS are intentionally separated:

- **Hosting**: GitHub Pages, deployed from this repo via the `actions/deploy-pages` action. Same-repo (`/site` builds, `/dist` deploys) keeps everything in one place — same PR for code, docs, and site changes; same CI; no third-party hosting account.
- **DNS**: Cloudflare DNS continues to manage `hippobrain.org`. CNAME records point at GitHub Pages; Cloudflare's CNAME flattening handles the apex.
- **Adapter**: no `@astrojs/cloudflare` adapter needed — Astro's static output is sufficient. Drop the line in the Astro setup; output is just `dist/`.
- **Preview deploys**: **none** (decision locked). Reviewers run `pnpm preview` locally and attach screenshots to PR descriptions. If contributor volume ever justifies it, revisit by adding a third-party preview action.

### CI / Deploy workflow

**`.github/workflows/site-ci.yml`** — runs on PRs that touch `site/**`, `docs/**`, `README.md`, or `CONTRIBUTING.md`. Path filter via the workflow's `on.pull_request.paths` field. Steps:
- `pnpm install`
- `pnpm build` — proves the site builds with current docs
- `pnpm exec linkinator dist --recurse --skip "https?://github.com/.*/blob/.*"` — link checking
- `pnpm exec astro check` — type checking on `.astro` files
- `pnpm exec axe-core` smoke test on `dist/index.html`, `dist/install/index.html`, and `dist/docs/capture/anti-patterns/index.html` (canary tier-2 page) — accessibility regression catch

**`.github/workflows/site-deploy.yml`** — runs on push to `main` matching the same paths, plus a `workflow_dispatch` trigger and a daily `schedule: cron: "0 6 * * *"` (so `/changelog` and `/status` stay current via rebuild even when no source changes). Steps:
- `pnpm install`, `pnpm build`
- `actions/upload-pages-artifact` from `site/dist`
- `actions/deploy-pages` to publish

GITHUB_TOKEN comes from the workflow runner; no PAT needed for the public-repo Releases/Issues fetches.

### Domain & DNS (Cloudflare)

- **Apex** `hippobrain.org` → CNAME (flattened) to `stevencarpenter.github.io`. GitHub Pages custom-domain verification via the GitHub repo settings (`CNAME` file in `site/public/CNAME` containing `hippobrain.org`).
- **`www.hippobrain.org`** → 301 redirect to apex (Cloudflare Redirect Rule).
- **Cloudflare proxy** (orange cloud): on. Gives caching, free Universal SSL on top of GitHub Pages' SSL, and access to Cloudflare Web Analytics if we ever want it (cookieless, free, off by default).
- **Future-reserved subdomains** (no immediate use): `docs.` (alias to `/docs/`), `blog.` (alias to `/blog/`). Don't provision until needed.

---

## Open Questions Resolved (Full Gamut Recap)

For recoverability — every alternative is listed in **Decisions & Rationale** above. Quick index:

| ID | Decision |
|---|---|
| D1 | Hybrid posture (✅ B) |
| D2 | Same-repo `/site` (✅ A) |
| D3 | Anatomical Naturalism (✅ C) |
| D4 | Astro custom (✅ A) |
| D5 | Source Serif 4 for tier-2 body (✅ A) |
| D6 | Wordmark + cornu Ammonis motif (✅ B) |
| D7 | Two-tier execution (locked) |
| D8 | Lights-low tokens specced; impl post-launch (✅ B) |
| D9 | Latin once per top-level page (locked) |
| D10 | Twee policing: written rules + maintainer veto (✅ C) |

---

## Out of Scope (v1.0)

- Newsletter / email capture.
- Server-side anything (search, comments, analytics with cookies).
- Multi-language; site is English-only.
- Logged-in surface, user accounts.
- Live data from a running daemon.
- Native dark mode (tokens specced; implementation may land in v1.1).
- "Adopters" / "Made with hippo" page (deferred until real names exist).
- Video content (no demo reel; site-as-text-screenshot is enough).
- Search-as-you-type on the homepage. `/search` only.
- Custom 5xx pages (Cloudflare proxy default suffices when proxy is on).

## Implementation Sequence (high level)

The detailed plan is the writing-plans skill's job. At a high level, expect:

1. Astro scaffold + content collections + tokens + tier-1/tier-2 layouts.
2. Marketing pages with finalized copy: `/`, `/install`, `/why`, `/privacy`, `/faq`.
3. Docs pipeline: rehype transforms (link rewrite, edit-on-github, image resolve, shiki, autolink), tier-2 layout with rails, Pagefind integration.
4. Build-time integrations: GitHub fetches for `/changelog` and `/status`, OG image generation, RSS feeds.
5. Motif library: SVG illustrations at three sizes, breathing animation, `prefers-reduced-motion` short-circuit.
6. Polish: 404, search UI, focus rings, print stylesheet, admonition styles, table treatment.
7. CI: site-ci.yml, axe smoke, linkinator, Astro check.
8. Deploy: GitHub Pages publish workflow (`actions/deploy-pages`), daily cron rebuild, Cloudflare DNS records (apex CNAME + www redirect), Cloudflare Redirect Rules for `/git`, `/issues`, `/releases`, `/install.sh`.
9. **`site/CONTRIBUTING.md`** (separate from the repo-root CONTRIBUTING.md): site-specific contribution rules including the "things we don't do" list (D10), motif library reference, twee-policing rules, and how to test changes locally. Repo-root CONTRIBUTING.md is unchanged — it's about hippo the daemon, not the site.
10. Lights-low dark mode (v1.1 if launch ships first).
