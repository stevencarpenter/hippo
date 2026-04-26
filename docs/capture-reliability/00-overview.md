<!-- TL;DR: Two silent capture outages — 21 days for browser, 8+ days for Claude sessions — went undetected because hippo has no per-source health tracking. This document defines the problem, goals, scope, and architecture for fixing that. -->

# Capture Reliability Overhaul — Overview

## Problem Statement

On 2026-04-22 a post-mortem revealed that browser capture had been silently broken for at least 21 days, and Claude session capture had been silently broken for at least 8 days. Both regressions had discrete, findable root causes — a tmux `-t` index error in the Claude session hook and a missing `extension/dist/` directory in the browser extension — yet neither was detected by `hippo doctor`, by OTel dashboards, or by any automated alert. The machine appeared healthy: the daemon was running, LM Studio was reachable, the brain was enriching, and SQLite was growing. The only indication of failure was a user noticing stale knowledge in RAG answers.

The underlying systemic failure is that hippo has no concept of per-source capture health. `hippo doctor` checks whether processes are alive and whether services are reachable, but never asks "when did this source last deliver an event?" There is no `source_health` table, no heartbeat, no staleness invariant, and no watchdog process that could fire an alarm when a source goes quiet. OTel counters are untagged by source, so a counter stuck at zero for `claude-session` is indistinguishable from a quiet workday. Any capture path can fail completely — for weeks — and hippo will continue to appear operational.

## Goal

Make capture the most bulletproof subsystem in hippo.

**Success criteria:**

1. No silent capture outage can exceed 1 hour undetected on an actively-used machine.
2. `hippo doctor` shows a per-source last-event age and flags any source that has been silent longer than its configured threshold.
3. A separate watchdog process detects daemon-level failures independently — a wedged daemon cannot silence its own alarm.
4. Synthetic canary probes round-trip a real event through each capture path on a configurable schedule and record probe round-trip latency in `source_health`.
5. Probe events are excluded from all user-facing queries, knowledge enrichment, and RAG results.

## Scope

**Capture paths in scope (4):**

| Source | Primary table | Entry point |
|---|---|---|
| `shell` | `events` (source_kind='shell') | `flush_events` in `daemon.rs` |
| `claude-tool` | `events` (source_kind='claude-tool') | `flush_events` in `daemon.rs` |
| `claude-session` | `claude_sessions` | FS watcher (`com.hippo.claude-session-watcher`); manual recovery via `hippo ingest claude-session <path>` |
| `browser` | `browser_events` | `flush_events` in `daemon.rs` (via native messaging → Unix socket) |

**Out of scope for this design:**

- Enrichment pipeline health (brain LLM calls, LanceDB writes) — a separate concern
- Embedding pipeline health
- RAG retrieval quality
- GitHub Actions workflow capture (`workflow_runs`)
- Knowledge node quality or freshness

This design answers exactly one question per source: *did the event land in SQLite?* Enrichment correctness is a downstream concern tracked by separate queue metrics.

## Non-Goals

- Prevent all possible failures — failures will happen; the goal is rapid detection
- Replace LanceDB or the Python brain
- Add alerting fatigue — alarms are rate-limited per invariant; false-positives are a first-class cost
- Achieve sub-minute detection — 1-hour SLO is aggressive but realistic for a local tool
- Monitor enrichment latency or RAG answer quality (those are brain concerns, not capture concerns)

## Design Principles

1. **Ground truth lives in SQLite.** Health queries must be executable as one-line SELECTs on `source_health`. No in-memory state, no log scraping, no process inspection.
2. **Every capture path writes its own heartbeat.** No source is allowed to succeed silently without updating `source_health`. The write happens in the same transaction as the event insert.
3. **The watchdog is a separate process from the daemon.** A wedged or crashed daemon cannot silence its own alarm. The watchdog reads `source_health` directly from SQLite.
4. **Synthetic probes round-trip real events.** A probe for `shell` actually injects a shell-shaped event through the socket and verifies it lands in `events`. Not a ping — a full path exercise.
5. **Alarms are rate-limited per invariant.** `doctor` is the interactive manual check; the watchdog emits one alarm per source per threshold breach, not one per check cycle.
6. **Capture health is decoupled from enrichment health.** The brain being down, LM Studio being unreachable, or the enrichment queue backing up are not capture outages. `source_health` only tracks event landing.
7. **Probe events carry a `probe_tag` and are excluded from user queries.** They must not pollute RAG, enrichment queues, or `hippo ask` results.

## Architecture Diagram

```
  Capture Producers                    Daemon                     SQLite
  ─────────────────                    ──────                     ──────

  zsh hook  ─────────────────────────► flush_events() ──────────► events
                                           │                       (source_kind='shell')
  ~/.claude/projects/**/*.jsonl ────► FSEvents watcher ─────────► claude_sessions
                                           │
                                           │
  Firefox extension ──native msg──────► flush_events() ──────────► browser_events
                                           │
                                           │   (all paths write)
                                           └───────────────────────► source_health
                                                                       ▲
                                                        ┌──────────────┘
                                              Canary probes
                                           (inject synthetic events
                                            per source on schedule)

                      ┌─────────────────────────────────────────────┐
                      │  Watchdog (separate process)                │
                      │  Reads source_health directly from SQLite   │
                      │  Alarms when last_event_ts is stale         │
                      └─────────────────────────────────────────────┘
```

## Status

The overhaul shipped across PRs #67–#89 (v0.16.x → present). All design docs below are kept as live references for the schema, the watchdog, and the doctor checks that exist in the running code today. The two design artifacts that are now closed records (`06-claude-session-watcher.md` and `m3-decision.md`) live in [`../archive/capture-reliability-overhaul/`](../archive/capture-reliability-overhaul/).

## Investigations Closed

The 2026-04-22 sev1 forensics spawned five investigation issues (#49–#53). All five are now closed. Three were resolved by capture-reliability work; two were out of scope and were spun off so they remain tracked.

| Issue | Title | Resolution |
|---|---|---|
| [#49](https://github.com/stevencarpenter/hippo/issues/49) | Apr 10–17 Claude-session blackout (root cause unknown) | **Mitigated systemically** — root cause never isolated (logs already rotated by forensics time, which itself motivated T-0.5 / [#69](https://github.com/stevencarpenter/hippo/pull/69)). The 1-hour SLO from `source_health` ([#67](https://github.com/stevencarpenter/hippo/pull/67), [#68](https://github.com/stevencarpenter/hippo/pull/68)), watchdog ([#79](https://github.com/stevencarpenter/hippo/pull/79), [#83](https://github.com/stevencarpenter/hippo/pull/83)), probes ([#82](https://github.com/stevencarpenter/hippo/pull/82)), and doctor checks 1–10 ([#70](https://github.com/stevencarpenter/hippo/pull/70), [#81](https://github.com/stevencarpenter/hippo/pull/81)) ensure no future blackout of this category can stay silent. |
| [#50](https://github.com/stevencarpenter/hippo/issues/50) | claade wrapper may break session-hook PID chain | **Resolved by T-8** ([#89](https://github.com/stevencarpenter/hippo/pull/89)) — the hook is now a 14-line no-op; the FS watcher ([#86](https://github.com/stevencarpenter/hippo/pull/86), [#88](https://github.com/stevencarpenter/hippo/pull/88)) replaces the entire PID-walk path. |
| [#51](https://github.com/stevencarpenter/hippo/issues/51) | Daemon restart drops Firefox NM connection (recovery unknown) | **Resolved operationally by T-3** ([#80](https://github.com/stevencarpenter/hippo/pull/80)) — extension heartbeat (5 min) + popup badge + watchdog alarm. The deeper NM-retry questions are no longer load-bearing for capture reliability. |
| [#52](https://github.com/stevencarpenter/hippo/issues/52) | Redaction false-positives silently drop events | **Out of scope** for the capture-reliability epic (touches `crates/hippo-core/src/redaction.rs`, not capture paths). Spun off to [#90](https://github.com/stevencarpenter/hippo/issues/90). |
| [#53](https://github.com/stevencarpenter/hippo/issues/53) | Lessons pipeline doesn't graduate capture-reliability failures | **Out of scope** for the capture-reliability epic (downstream brain enrichment, not ingestion). Spun off to [#91](https://github.com/stevencarpenter/hippo/issues/91). |

## Document Map

| File | Contents |
|---|---|
| **[01-source-health.md](01-source-health.md)** | `source_health` table schema, migration (v7→v8), write paths per source, rolling-count recompute job, error path, read queries, back-fill behavior |
| **[02-invariants.md](02-invariants.md)** | Per-source staleness thresholds, expected-min-per-hour defaults, invariant definitions, alarm severity levels |
| **[03-doctor-upgrades.md](03-doctor-upgrades.md)** | `hippo doctor` checks: per-source health rows, staleness formatting, probe status display, exit code semantics |
| **[04-watchdog.md](04-watchdog.md)** | Watchdog process: launchd plist, poll interval, alarm output format, back-off |
| **[05-synthetic-probes.md](05-synthetic-probes.md)** | Synthetic canary probes: event schema, `probe_tag` filtering, scheduling, round-trip latency |
| **[07-roadmap.md](07-roadmap.md)** | Phased delivery tracker: P0 (source_health + write paths + doctor), P1 (watchdog + extension heartbeat), P2 (watcher + probes), P3 (cleanup). All tasks shipped; kept as a historical record of what landed and how it gated. |
| **[08-anti-patterns.md](08-anti-patterns.md)** | Patterns that caused the sev1 and patterns this design must avoid |
| **[09-test-matrix.md](09-test-matrix.md)** | One row per failure mode with the test that would catch it; status column tracks coverage |
| **[10-source-audit.md](10-source-audit.md)** | Source-by-source map of capture entry points, expected tables, and tests |

Start with **01-source-health.md** — it is the load-bearing schema document that all other sections depend on.
