# Contributing to hippobrain.org

This is the site at `/site`, distinct from `hippo` itself. Contributions to the
site are welcome — but the aesthetic is opinionated, and the rulebook is
load-bearing.

## Local development

```bash
cd site
pnpm install
pnpm dev          # http://localhost:4321
pnpm build        # static output in dist/
pnpm preview      # serve dist/
pnpm check        # astro type / syntax checking
pnpm test         # rehype plugin unit tests
pnpm lint:links   # linkinator on the built dist
```

The site builds against the **same checkout** as the daemon. Edits to
`../docs/`, `../README.md`, or `../CONTRIBUTING.md` (root) are picked up
automatically by the dev server's HMR (we configure `vite.server.fs.allow:
[".."]` and watch the parent dir).

## Things we don't do

These are the rules — written, so reviewers can cite them, and so contributors
know in advance what won't ship. Maintainer veto applies on top of the list:
"this is twee" is a valid review reason.

- **No curlicue dividers, no quill cursors, no faux-distressed paper textures.**
  The aesthetic is anatomical naturalism — illustration plates from a 19th-century
  textbook — not Renaissance Faire.
- **No "ye olde" diction.** Plain English. Latin only where it's honest (`memoriae
  custos`, `cornu Ammonis`, `marginalia`, `fasciculus`, `concordantia`, `sub praelo`,
  `principia`, `sectio coronalis`) and never twice in the same viewport.
- **No drop caps outside of the first paragraph of a chapter.** A drop cap is a
  *chapter mark*, not decoration.
- **No motion that announces itself.** No "scroll to reveal", no carousel, no
  parallax marketing. The hero motif breathes on an 8-second cycle (transform
  scale 1.00 → 1.03, transform-only, SVG-container only — never the typography);
  `prefers-reduced-motion: reduce` short-circuits everything.
- **No purple gradients, no glowing buttons, no "AI-powered", no "transform",
  no "supercharge", no "unleash".** Concrete > grand. Specific > sweeping.
- **No emoji decorations in marketing copy.** Code blocks where they're meaningful
  (✓ shell prompt, ⚠ admonitions) only.
- **No promises of expansion.** No "future support for X", no "soon supports Y".
  We document what hippo does *today*. Today's sources are zsh, Claude Code
  sessions, and Firefox within an allowlist. That's it.

## Latin — `lang="la"` is required

WCAG 3.1.2 (AAA) — screen readers mispronounce Latin as English without `lang`
annotation. **Every Latin phrase in marketing or docs copy gets
`<span lang="la">…</span>`.**

Rules of thumb:
- If the phrase would be italicised in English prose, tag it.
  `<em lang="la">cornu Ammonis</em>`, `<span lang="la"><em>memoriae custos</em></span>`.
- *Don't* tag English words borrowed from Latin: "Plate I", "fig.", "marginalia"
  (used as English), "corpus" (used as English).
- The wordmark "Hippo·campus." is a project name; don't language-tag project
  names.
- The mid-dot in the wordmark is `<span aria-hidden="true">·</span>` so screen
  readers say "Hippocampus." not "Hippo middle-dot campus."

## Motif library — section-locked

Hand-drawn SVGs at three sizes (`full`, `reduced`, `inline`). Six motifs total:

| ID                    | Used for                                              |
|-----------------------|-------------------------------------------------------|
| `cornu-ammonis`       | Hero, capture-section docs                            |
| `sectio-coronalis`    | Reference docs (default for top-level docs)           |
| `trisynaptic-circuit` | Schema, lifecycle, "how it works"                     |
| `marginalia`          | Blog index, contributing                              |
| `plate-frame`         | Hero ornament, /404                                   |
| `fasciculus`          | Privacy, redaction docs                               |

Motif assignment lives in `src/lib/docs.ts → motifForSlug()`. **Don't sprinkle
motifs decoratively** — each is a chapter mark for one section. Blog posts pick
their own motif via required frontmatter (`motif:`); the schema rejects posts
that don't.

A11y default is `decorative={true}` (rendered with `aria-hidden`). Content
diagrams (Plate III architecture, /privacy data flow) opt in with
`decorative={false}` and ship a hidden `<desc>` long-description for screen
readers.

## Plate numbering

Each page is its own folio — plate numbering **restarts at I per page**. The
homepage uses Plate I (hero) through Plate VI (field notes) because it's a
multi-section page; every other tier-1 page just has Plate I. Don't try to
extend the homepage series globally — visitors don't navigate the site as one
continuous publication, and a fresh folio per page reads more like a book of
plates than a numbered sequence.

## Tier-2 outline auto-collapse (U2)

Docs pages with fewer than three h2/h3 headings collapse to a two-column grid
(left rail + main, no right rail). To bring the rail back, add a fourth h2 to
the page. The hint is in `Tier2.astro`'s frontmatter:

```ts
const outlineDataAttr = outlineCount < 3 ? "none" : "shown";
```

## Code blocks — fence info-string conventions

- ` ```bash ` — shell prompts and one-liners
- ` ```shellsession ` — `$ command` plus output
- ` ```ts ` / ` ```rust ` / ` ```python ` — language-tagged source
- ` ```sql ` — SQL (the redactor and brain queries use SQL frequently)

Per-block soft-wrap (`bash {wrap=true}`) is a deferred feature; if you need
soft-wrap on a long line today, break the line manually.

## Maintainer veto

The site's aesthetic erodes at PR boundaries unless governed. Two rules:

1. **Tier-1 visual additions** (homepage, /install, /why, /privacy, /faq, /404,
   /blog, /changelog, /status, /search) require maintainer sign-off — even when
   the addition matches the rulebook above. The maintainer's job is to keep the
   aesthetic from getting busy.
2. **Tier-2 docs additions** (anything under `/docs/**`) are governed by the
   docs themselves — they're rendered from `../docs/` at build time. Edit the
   markdown, not the layout.

If you hit "this is twee" feedback in review, the rulebook above is the
sharing-the-blame layer. Cite the rule, fix the PR.

## Where the spec lives

- Spec: `docs/superpowers/specs/2026-05-01-hippobrain-org-design.md`
- Plan: `docs/superpowers/plans/2026-05-01-hippobrain-org-mvp.md`
- Plan addendum (panel review): `docs/superpowers/plans/2026-05-01-hippobrain-org-mvp-addendum.md`

The plan + addendum are the source of truth for *what* the site does. The
"things we don't do" list above is the source of truth for *what* it doesn't.
