# Capture

Reference documentation for hippo's capture-reliability stack — what every source captures, how it lands, what fires when something breaks, and the rules every contributor must follow when adding to it.

## Where to start

| Audience | Entry point |
|---|---|
| **New contributor** — "what does the system do?" | [`architecture.md`](architecture.md) |
| **Operator** — "something looks wrong" | [`operator-runbook.md`](operator-runbook.md) |
| **Source author** — "I want to add a new capture path" | [`anti-patterns.md`](anti-patterns.md) first, then [`sources.md`](sources.md) for the existing patterns |
| **Reviewer** — "is this PR safe?" | [`anti-patterns.md`](anti-patterns.md) (AP-1..AP-12 are review blockers) |
| **Test author** — "where does this test go?" | [`test-matrix.md`](test-matrix.md) |

## In this directory

| Doc | Topic |
|---|---|
| [`architecture.md`](architecture.md) | The four layers (capture path / `source_health` / watchdog / probe / alarms), I-1..I-10 invariants with thresholds, how the pieces interact. |
| [`sources.md`](sources.md) | Per-source coverage matrix: shell, claude-session, browser, workflow runs, and the rest. Entry point, tables, invariants, probe coverage. |
| [`anti-patterns.md`](anti-patterns.md) | AP-1..AP-12: forbidden patterns with rationale and the right alternative. Review blockers. |
| [`operator-runbook.md`](operator-runbook.md) | Doctor recipes, alarm responses, recovery flows. First-aid for "something is wrong with capture." |
| [`test-matrix.md`](test-matrix.md) | Failure-mode-to-test mapping. The "How to extend" section is the contract for adding new tests. |

## Historical context

The capture-reliability stack is the result of a P0–P3 overhaul (v0.16 era) motivated by two silent capture outages — 21 days for browser, 8 days for Claude sessions — that the daemon's existing health check completely missed. Design records, post-mortem notes, and the original incident report are archived under [`docs/archive/capture-reliability-overhaul/`](../archive/capture-reliability-overhaul/) and [`docs/archive/incident-2026-04-22/`](../archive/incident-2026-04-22/).

The docs in this directory describe the **current** system. If something here contradicts the live codebase, the live codebase is right; please open an issue or PR to fix the doc.
