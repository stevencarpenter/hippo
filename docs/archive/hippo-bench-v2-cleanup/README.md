# hippo-bench v1/v2 transition — archived plans

These four plan documents were the working drafts from the v1 → v2 transition
(April 2026). They captured the MVP design, the v2 redesign, and the Ralph-loop
execution plan. After PR #131 collapsed the v1/v2 split (see commit log on the
`cleanup/bench-drop-v2-suffix` branch), these docs no longer match the live
code shape.

They are kept here as a frozen reference, **not** updated in place — per the
project convention that historical plans are archived rather than retconned.

| File | What it captured |
|---|---|
| `2026-04-21-hippo-bench-mvp.md` | Original Tier-0 MVP design and gate definitions |
| `2026-04-27-hippo-bench-v2-implementation.md` | v2 shadow-stack + downstream-proxy implementation plan |
| `2026-04-27-hippo-bench-v2-ralph-plan.md` | Wave-by-wave Ralph-loop work breakdown |
| `2026-04-27-hippo-bench-v2-ralph-runbook.md` | Operator runbook for the Ralph execution |

For the current operator-facing reference, see
`brain/src/hippo_brain/bench/README.md`. For active trust-initiative work, see
`docs/superpowers/plans/2026-05-03-hippo-bench-trust-tracking.md`.
