# site

The hippobrain.org marketing-and-docs site. Astro static output → GitHub Pages → Cloudflare DNS.

## Local development

```bash
pnpm install
pnpm dev          # http://localhost:4321
pnpm build        # static output in dist/
pnpm preview      # serve dist/
pnpm check        # astro type/syntax checking
pnpm test         # rehype plugin unit tests
```

## Where things live

- `src/pages/` — routes (tier-1 marketing, tier-2 docs, blog, RSS)
- `src/layouts/` — Tier1, Tier2, BlogPost
- `src/components/` — Hero, Wordmark, Header, Footer, motifs, etc.
- `src/styles/` — tokens, fonts, tier-1, tier-2, prose, code, print
- `src/lib/` — docs helpers, github API, motif registry
- `src/lib/rehype/` and `src/lib/remark/` — markdown transforms
- `src/content/blog/` — field notes (.md)
- `public/fonts/` — self-hosted Latin-subset woff2

Docs source from `../docs/**`, `../README.md`, `../CONTRIBUTING.md`. The
`copy-doc-images` integration mirrors `../docs/diagrams/**` into
`public/docs-images/` at build time.

## Spec & plan

Locked in:
- `docs/superpowers/specs/2026-05-01-hippobrain-org-design.md`
- `docs/superpowers/plans/2026-05-01-hippobrain-org-mvp.md`
- `docs/superpowers/plans/2026-05-01-hippobrain-org-mvp-addendum.md`

## Manual one-time setup (steven)

Before the first deploy works on hippobrain.org:

1. **GitHub → Settings → Pages → Source = "GitHub Actions"** (one-time).
2. **Cloudflare DNS — Phase A (cert provisioning):**
   - Apex `hippobrain.org` → CNAME → `stevencarpenter.github.io` (grey cloud, DNS-only).
   - `www.hippobrain.org` → CNAME → `stevencarpenter.github.io` (grey cloud).
   - Wait until GH Pages reports "DNS check successful" and the SSL cert provisions (5–30 min).
3. **Cloudflare DNS — Phase B (proxy on, post-cert):**
   - Flip apex and `www` to orange cloud.
   - SSL/TLS = **Full (strict)** (Flexible causes redirect loops with GH Pages).
   - Page rule: "Always Use HTTPS".
   - Tick "Enforce HTTPS" in GH Pages settings.
4. **Cloudflare Redirect Rules** (free tier, four rules):
   - `/git` → `https://github.com/stevencarpenter/hippo` (302)
   - `/issues` → `https://github.com/stevencarpenter/hippo/issues` (302)
   - `/releases` → `https://github.com/stevencarpenter/hippo/releases` (302)
   - `/install.sh` → `https://github.com/stevencarpenter/hippo/releases/latest/download/install.sh` (302)

## Contributing

See [`site/CONTRIBUTING.md`](./CONTRIBUTING.md) for the site-specific rules
(motif library, "things we don't do", maintainer veto on tier-1 visuals).
