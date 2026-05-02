---
title: "Placeholder"
date: 2026-05-01
description: "Silences Astro's empty-collection warning until the first real field note ships. Marked draft so it never appears."
motif: marginalia
draft: true
---

This file is a placeholder so Astro's `getCollection("blog")` doesn't emit
"collection does not exist or is empty" warnings during build. It is filtered
out by the `draft: true` predicate everywhere we list posts, so it never
appears on the site. Replace it (or just delete it) once a real post lands.
