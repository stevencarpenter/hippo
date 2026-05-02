# Archived documentation

Historical planning documents, design records, and post-mortems whose work has shipped and is no longer in active development. **Documents here describe past intent, not current behavior.** For reference docs of live system behavior, see the [project README](../../README.md), [`docs/capture/`](../capture/), and [`docs/release.md`](../release.md).

## Archive policy

A doc is moved here when *both* are true:

- The work it describes has shipped, is dormant, or has been superseded by a different approach.
- No open PR or recently-touched branch references it.

Active in-flight work stays in `docs/superpowers/`. Live operator and architecture references stay at the top of `docs/`.

If something here contradicts the live codebase, the live codebase is right; please open an issue or PR against the live docs in `docs/capture/` or the README rather than editing the archive.

## Index

### `feature-waves/`

Plans and designs grouped by the feature wave they belong to. All shipped or dormant.

#### Initial v1 release (March 2026, v0.1 → v0.10)

| Doc | Topic |
|---|---|
| [`2026-03-27-hippo-v1.md`](feature-waves/2026-03-27-hippo-v1.md) | Original v1 plan |
| [`2026-03-27-hippo-design.md`](feature-waves/2026-03-27-hippo-design.md) | v1 design — daemon + brain + storage architecture |
| [`2026-03-28-service-lifecycle-fixes.md`](feature-waves/2026-03-28-service-lifecycle-fixes.md) | LaunchAgent + CLI lifecycle fixes |
| [`2026-03-29-versioning.md`](feature-waves/2026-03-29-versioning.md) | Versioning plan (build.rs + importlib.metadata) |
| [`2026-03-29-versioning-design.md`](feature-waves/2026-03-29-versioning-design.md) | Versioning design |

#### Enrichment pipeline (early v0.x → v0.10+)

| Doc | Topic |
|---|---|
| [`2026-03-29-enrichment-pipeline.md`](feature-waves/2026-03-29-enrichment-pipeline.md) | Plan — concurrent source processing via `asyncio.gather`, dynamic model fallback |
| [`2026-03-29-enrichment-pipeline-redesign.md`](feature-waves/2026-03-29-enrichment-pipeline-redesign.md) | Design |

#### Firefox browser source (v0.4+ initial; v0.17+ TypeScript rewrite)

| Doc | Topic |
|---|---|
| [`2026-03-31-firefox-browser-source.md`](feature-waves/2026-03-31-firefox-browser-source.md) | Plan — WebExtension + Native Messaging |
| [`2026-03-31-firefox-browser-source-design.md`](feature-waves/2026-03-31-firefox-browser-source-design.md) | Design |
| [`2026-04-02-firefox-extension-typescript.md`](feature-waves/2026-04-02-firefox-extension-typescript.md) | TS rewrite plan |
| [`2026-04-02-firefox-extension-typescript-rewrite.md`](feature-waves/2026-04-02-firefox-extension-typescript-rewrite.md) | TS rewrite design |

#### MCP server (v0.18)

| Doc | Topic |
|---|---|
| [`2026-04-01-hippo-mcp-server.md`](feature-waves/2026-04-01-hippo-mcp-server.md) | Plan |
| [`2026-04-01-hippo-mcp-server-design.md`](feature-waves/2026-04-01-hippo-mcp-server-design.md) | Design |

#### RAG query (v0.19)

| Doc | Topic |
|---|---|
| [`2026-04-04-rag-query.md`](feature-waves/2026-04-04-rag-query.md) | `hippo ask` plan — synthesis + scored sources |
| [`2026-04-04-rag-query-design.md`](feature-waves/2026-04-04-rag-query-design.md) | Design |

#### OpenTelemetry observability

| Doc | Topic |
|---|---|
| [`2026-04-02-otel-observability-stack.md`](feature-waves/2026-04-02-otel-observability-stack.md) | Local Grafana stack via Docker Compose |
| [`2026-04-08-otel-metrics-implementation.md`](feature-waves/2026-04-08-otel-metrics-implementation.md) | OTel metrics implementation plan |
| [`2026-04-08-otel-metrics-design.md`](feature-waves/2026-04-08-otel-metrics-design.md) | Design |

#### CI/CD (v0.18+)

| Doc | Topic |
|---|---|
| [`2026-04-08-cicd-implementation.md`](feature-waves/2026-04-08-cicd-implementation.md) | Plan — Blacksmith runners + workflow structure |
| [`2026-04-08-cicd-design.md`](feature-waves/2026-04-08-cicd-design.md) | Design |

#### GitHub Actions source

| Doc | Topic |
|---|---|
| [`2026-04-15-github-actions-source-and-hippo-skill.md`](feature-waves/2026-04-15-github-actions-source-and-hippo-skill.md) | Plan |
| [`2026-04-15-github-actions-source-and-hippo-skill-design.md`](feature-waves/2026-04-15-github-actions-source-and-hippo-skill-design.md) | Design |

#### Agentic ingestion / OpenCode / eval harness

| Doc | Topic |
|---|---|
| [`2026-04-17-agentic-session-ingestion.md`](feature-waves/2026-04-17-agentic-session-ingestion.md) | Plan |
| [`2026-04-17-opencode-ingestion-and-agentic-labeling-design.md`](feature-waves/2026-04-17-opencode-ingestion-and-agentic-labeling-design.md) | OpenCode + agentic labeling design |
| [`2026-04-17-eval-harness-design.md`](feature-waves/2026-04-17-eval-harness-design.md) | Eval-harness design — superseded by the live [`docs/eval-harness-design.md`](../eval-harness-design.md) |
| [`2026-04-17-retrieval-benchmark.md`](feature-waves/2026-04-17-retrieval-benchmark.md) | Retrieval benchmark spec |
| [`2026-04-17-corpus-health-report.md`](feature-waves/2026-04-17-corpus-health-report.md) | Corpus health report design |

#### sqlite-vec consolidation (wave-b → v0.20)

| Doc | Topic |
|---|---|
| [`2026-04-17-sqlite-vec-consolidation-design.md`](feature-waves/2026-04-17-sqlite-vec-consolidation-design.md) | LanceDB → sqlite-vec migration design |
| [`2026-04-17-sqlite-vec-consolidation-scorecard.md`](feature-waves/2026-04-17-sqlite-vec-consolidation-scorecard.md) | Scorecard / decision matrix |
| [`2026-04-17-risk-register.md`](feature-waves/2026-04-17-risk-register.md) | Risk register for the migration (R-1..R-22) |
| [`2026-04-17-session-handoff.md`](feature-waves/2026-04-17-session-handoff.md) | Session-handoff spec for the migration |
| [`2026-04-18-eval-baseline-pre-cutover.md`](feature-waves/2026-04-18-eval-baseline-pre-cutover.md) | Pre-cutover eval baseline |
| [`2026-04-18-eval-baseline-post-cutover.md`](feature-waves/2026-04-18-eval-baseline-post-cutover.md) | Post-cutover eval baseline |
| [`2026-04-18-migration-review.md`](feature-waves/2026-04-18-migration-review.md) | Migration review checklist |
| [`2026-04-18-phase1-code-review.md`](feature-waves/2026-04-18-phase1-code-review.md) | Phase 1 code-review checklist |
| [`2026-04-18-r22-r23-audit.md`](feature-waves/2026-04-18-r22-r23-audit.md) | R-22 / R-23 audit — enrichment-queue wedge risk |
| [`2026-04-18-session-handoff.md`](feature-waves/2026-04-18-session-handoff.md) | Session-handoff record |
| [`2026-04-18-wave-b-sqlite-vec-rollout.md`](feature-waves/2026-04-18-wave-b-sqlite-vec-rollout.md) | Wave-b rollout plan |
| [`2026-04-18-wave-b-sqlite-vec-rollout-design.md`](feature-waves/2026-04-18-wave-b-sqlite-vec-rollout-design.md) | Wave-b design |

#### Hippo GUI (v0.11+, ongoing maintenance)

| Doc | Topic |
|---|---|
| [`2026-04-18-hippo-gui-implementation.md`](feature-waves/2026-04-18-hippo-gui-implementation.md) | Initial SwiftUI GUI implementation plan |
| [`2026-04-18-hippo-gui-design.md`](feature-waves/2026-04-18-hippo-gui-design.md) | Design — `HippoGUIKit` Swift package + brain API expansion |

#### Browser yield improvements

| Doc | Topic |
|---|---|
| [`2026-04-22-browser-yield-improvements.md`](feature-waves/2026-04-22-browser-yield-improvements.md) | Post-wave-b browser-extension yield optimizations |

#### RAG entity surfacing (v0.20)

| Doc | Topic |
|---|---|
| [`2026-04-27-rag-entity-surfacing-ralph.md`](feature-waves/2026-04-27-rag-entity-surfacing-ralph.md) | Ralph-loop plan — Issue #108 fix |
| [`2026-04-27-rag-entity-surfacing-design.md`](feature-waves/2026-04-27-rag-entity-surfacing-design.md) | Design — `env_var` entity type, `Entities:` synthesis line |

### `capture-reliability-overhaul/`

Design records, decision logs, and reference material for the capture-reliability overhaul (P0–P3, v0.16 era). The overhaul shipped across PRs #67–#101; the live reference docs are now in [`docs/capture/`](../capture/).

| Doc | Topic |
|---|---|
| [`00-overview.md`](capture-reliability-overhaul/00-overview.md) | Cold-open narrative — the sev1 that motivated the overhaul; document map |
| [`01-source-health.md`](capture-reliability-overhaul/01-source-health.md) | `source_health` table design |
| [`02-invariants.md`](capture-reliability-overhaul/02-invariants.md) | I-1..I-10 specification — succeeded by [`docs/capture/architecture.md`](../capture/architecture.md) |
| [`03-doctor-upgrades.md`](capture-reliability-overhaul/03-doctor-upgrades.md) | Doctor improvements |
| [`04-watchdog.md`](capture-reliability-overhaul/04-watchdog.md) | Watchdog process design |
| [`05-synthetic-probes.md`](capture-reliability-overhaul/05-synthetic-probes.md) | Probe architecture |
| [`06-claude-session-watcher.md`](capture-reliability-overhaul/06-claude-session-watcher.md) | FS watcher proposal — shipped (T-5/PR #86, default in T-7/PR #88) |
| [`07-roadmap.md`](capture-reliability-overhaul/07-roadmap.md) | Original roadmap |
| [`07-roadmap-review.csv`](capture-reliability-overhaul/07-roadmap-review.csv) | Roadmap review tracker |
| [`08-anti-patterns.md`](capture-reliability-overhaul/08-anti-patterns.md) | Anti-patterns — succeeded by [`docs/capture/anti-patterns.md`](../capture/anti-patterns.md) |
| [`09-test-matrix.md`](capture-reliability-overhaul/09-test-matrix.md) | Test matrix — succeeded by [`docs/capture/test-matrix.md`](../capture/test-matrix.md) |
| [`10-source-audit.md`](capture-reliability-overhaul/10-source-audit.md) | Per-source audit — succeeded by [`docs/capture/sources.md`](../capture/sources.md) |
| [`11-watcher-data-loss-fix.md`](capture-reliability-overhaul/11-watcher-data-loss-fix.md) | 2026-04-27 watcher data-loss incident; AP-12 origin |
| [`m3-decision.md`](capture-reliability-overhaul/m3-decision.md) | M3 phase-gate decision record |

### `incident-2026-04-22/`

Closed sev1 postmortem: 21-day silent browser-capture outage and 8-day silent Claude-session outage that motivated the capture-reliability overhaul. Kept as the forever-record of what happened and why per-source health tracking became a hard requirement.

### `v0.9-to-v0.16-history/`

Pre-v0.16 planning, retrospectives, and one-time install/audit work.

| Doc | Topic |
|---|---|
| [`FEATURE_TIMELINE.md`](v0.9-to-v0.16-history/FEATURE_TIMELINE.md) | Release-by-release feature progression v0.9 → v0.12 |
| [`agent-execution-plan.md`](v0.9-to-v0.16-history/agent-execution-plan.md) | Multi-agent execution plan that completed |
| [`architecture-review-tracker.md`](v0.9-to-v0.16-history/architecture-review-tracker.md) | Architecture-review working doc; superseded by `docs/capture/` |
| [`schema-migration-strategy.md`](v0.9-to-v0.16-history/schema-migration-strategy.md) | Schema migrations as of v4. Live schema is v13+; the migration *mechanism* is unchanged but version numbers are out of date. See `crates/hippo-core/src/storage.rs` for current behavior. |
| [`smoke-test-and-risk-assessment.md`](v0.9-to-v0.16-history/smoke-test-and-risk-assessment.md) | 2026-03-27 install-impact assessment for a fresh-machine install |
