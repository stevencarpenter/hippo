# OTel Metrics Instrumentation & Dashboards

**Date:** 2026-04-08
**Status:** Approved
**Scope:** Wire up OpenTelemetry metrics for both brain (Python) and daemon (Rust), replace existing Grafana dashboards with metric-driven versions.

## Context

The OTel stack (Collector, Prometheus, Loki, Tempo, Grafana) is running. Traces and logs flow from the brain; the daemon has OTel plumbing behind a cargo feature flag but no instruments. No metrics are emitted by either component. Existing Grafana dashboards approximate metrics by parsing logs — fragile and expensive.

## Decisions

- **Full observability**: instrument all three signal types (health, throughput, latency) across both components.
- **Daemon OTel stays opt-in**: `--features otel` cargo flag, zero overhead without it.
- **Replace existing dashboards**: rebuild around Prometheus metrics, keep Loki/Tempo for drill-down only.
- **Three dashboards** instead of two: Overview, Enrichment Pipeline, Daemon.

## Brain Metrics Instrumentation (Python)

### Telemetry Changes

Add `MeterProvider` to `telemetry.py` alongside existing traces/logs. When `HIPPO_OTEL_ENABLED=1`, the meter provider exports via OTLP HTTP to the collector. When disabled, no instruments are created.

### MetricsCollector Removal

The `MetricsCollector` dataclass in `mcp_logging.py` (8 plain Python integer fields) is replaced by real OTel instruments. The `metrics.snapshot()` call in the health endpoint is replaced with OTel-native reads.

### Instruments

All metrics use the `hippo.brain` namespace.

#### server.py (enrichment loop)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.brain.enrichment.events_claimed` | Counter | `source` (shell/claude/browser) | Events pulled from queue per cycle |
| `hippo.brain.enrichment.nodes_created` | Counter | `source` | Knowledge nodes written |
| `hippo.brain.enrichment.failures` | Counter | `source` | Enrichment batch failures |
| `hippo.brain.enrichment.queue_depth` | ObservableGauge | `source`, `status` (pending/failed) | Queue sizes polled from SQLite |
| `hippo.brain.enrichment.loop_duration` | Histogram | -- | Wall clock per enrichment cycle |

#### client.py (LM Studio)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.brain.lmstudio.request_duration` | Histogram | `method` (chat/embed) | LLM and embedding API latency |
| `hippo.brain.lmstudio.errors` | Counter | `method` | Failed LM Studio calls |
| `hippo.brain.lmstudio.prompt_tokens` | Histogram | -- | Prompt size in chars |

#### embeddings.py

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.brain.embedding.duration` | Histogram | -- | Time to embed a knowledge node |
| `hippo.brain.embedding.failures` | Counter | -- | Failed embedding attempts |

#### rag.py

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.brain.rag.duration` | Histogram | `stage` (embed/retrieve/synthesize) | Per-stage RAG latency |
| `hippo.brain.rag.retrieval_hits` | Histogram | -- | Number of vector search results |

#### mcp.py

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.brain.mcp.tool_calls` | Counter | `tool` (ask/search_knowledge/search_events/get_entities) | MCP tool invocations |
| `hippo.brain.mcp.tool_errors` | Counter | `tool` | MCP tool failures |
| `hippo.brain.mcp.tool_duration` | Histogram | `tool` | Per-tool latency |

### Queue Depth Implementation

Use OTel `ObservableGauge` with a callback that queries SQLite for queue counts. Polled at the metric export interval. Simpler and always accurate vs increment/decrement tracking.

## Daemon Metrics Instrumentation (Rust)

### Implementation Pattern

New `metrics.rs` module in `hippo-daemon`, gated behind `#[cfg(feature = "otel")]`. Instruments are `LazyLock` statics using the global meter provider already initialized in `telemetry.rs`. Call sites use the instruments directly; when the `otel` feature is off, the module exposes no-op stubs (zero overhead).

```rust
// metrics.rs
use opentelemetry::global;
use opentelemetry::metrics::{Counter, Histogram};
use std::sync::LazyLock;

static METER: LazyLock<opentelemetry::metrics::Meter> =
    LazyLock::new(|| global::meter("hippo-daemon"));

pub static EVENTS_INGESTED: LazyLock<Counter<u64>> =
    LazyLock::new(|| METER.u64_counter("hippo.daemon.events.ingested").build());
```

### Instruments

All metrics use the `hippo.daemon` namespace.

#### Ingestion (daemon.rs)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.daemon.events.ingested` | Counter | `type` (shell/browser) | Events accepted into buffer |
| `hippo.daemon.events.dropped` | Counter | `type` | Events rejected at buffer capacity |
| `hippo.daemon.buffer.size` | ObservableGauge | -- | Current event buffer occupancy |

#### Flush (daemon.rs)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.daemon.flush.events` | Counter | -- | Events written to SQLite per flush |
| `hippo.daemon.flush.duration` | Histogram | -- | Time per flush batch |
| `hippo.daemon.flush.batch_size` | Histogram | -- | Events per flush batch |

#### Requests (daemon.rs)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.daemon.requests` | Counter | `type` (GetStatus/GetSessions/GetEvents/...) | Socket request count |
| `hippo.daemon.request.duration` | Histogram | `type` | Per-request-type latency |

#### Redaction (daemon.rs)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.daemon.redactions` | Counter | -- | Total secret replacements applied |

#### Sessions (daemon.rs)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.daemon.sessions.created` | Counter | -- | New shell sessions |

#### Fallback (daemon.rs)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.daemon.fallback.writes` | Counter | -- | Events written to fallback JSONL |
| `hippo.daemon.fallback.recovered` | Counter | -- | Events recovered from fallback |
| `hippo.daemon.fallback.pending` | ObservableGauge | -- | Unrecovered fallback files |

#### Storage (daemon.rs)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hippo.daemon.db.size_bytes` | ObservableGauge | -- | SQLite file size |

### Observable Gauges

Buffer size, DB size, and fallback pending use callbacks registered at init time. They query the actual value on each metrics export rather than tracking state changes.

## Dashboards

Replace existing 2 dashboards with 3 metric-driven dashboards. All use Prometheus (`uid: prometheus`) as primary datasource, with Loki (`uid: loki`) for error drill-down and Tempo (`uid: tempo`) for trace links.

Metric names in Prometheus use underscore convention with counter/histogram suffixes (OTel collector converts automatically): `hippo.brain.enrichment.nodes_created` becomes `hippo_brain_enrichment_nodes_created_total`.

### Dashboard 1: Hippo Overview (uid: `hippo-overview`)

Top-level health at a glance.

**Row 1 -- Key indicators (stat panels):**
- Events ingested/min — `rate(hippo_daemon_events_ingested_total[5m])`
- Events dropped — `hippo_daemon_events_dropped_total` (should be 0)
- Enrichment queue depth — `sum(hippo_brain_enrichment_queue_depth)`
- Knowledge nodes created/hr — `rate(hippo_brain_enrichment_nodes_created_total[1h])`

**Row 2 -- Trends + errors:**
- Ingestion rate vs enrichment rate (time series, two lines)
- Error rate by service (time series, stacked)
- Error logs (Loki: `{service_name=~"hippo.*"} |~ "(?i)error|failed"`)

### Dashboard 2: Enrichment Pipeline (uid: `hippo-enrichment`)

Deep dive into brain enrichment.

**Row 1 -- Queue health:**
- Queue depth per source (time series, 3 lines: shell/claude/browser)
- Failed queue items per source (stat panels, red threshold > 0)
- Events claimed/min by source (time series, stacked bar)

**Row 2 -- LLM performance:**
- LM Studio request latency p50/p95/p99 (time series, histogram quantiles)
- LM Studio error rate (time series)
- Prompt size distribution (heatmap)

**Row 3 -- Embedding + RAG:**
- Embedding duration p50/p95 (time series)
- Embedding failures (stat, red if > 0)
- RAG latency by stage (stacked bar)
- Vector search hit count distribution (histogram)

**Row 4 -- MCP + drill-down:**
- Enrichment traces (Tempo query)
- MCP tool latency by tool (time series)
- MCP tool calls/min by tool (stacked bar)

### Dashboard 3: Daemon (uid: `hippo-daemon`, new)

Daemon ingestion and storage.

**Row 1 -- Ingestion:**
- Events ingested/min by type (time series)
- Buffer utilization (gauge, % of capacity)
- Events dropped (stat, red threshold > 0)
- Sessions created/hr (stat)

**Row 2 -- Flush + Storage:**
- Flush duration p50/p95 (time series)
- Flush batch size distribution (histogram)
- DB size (time series, bytes)
- Redactions applied/min (time series)

**Row 3 -- Reliability:**
- Request latency by type (time series)
- Fallback writes (stat, should be 0)
- Fallback files pending (gauge)
- Daemon error logs (Loki panel)

## Infrastructure

No changes needed to the OTel collector or Prometheus configs. The existing pipeline already handles the flow:

```
Brain/Daemon → OTLP → OTel Collector → Prometheus exporter (:8889) → Prometheus scrapes (15s)
```

The collector's Prometheus exporter automatically converts OTel metric names to Prometheus convention.

## Files Modified

### Brain (Python)
- `brain/src/hippo_brain/telemetry.py` — Add MeterProvider + metric export
- `brain/src/hippo_brain/mcp_logging.py` — Remove MetricsCollector, update setup_logging
- `brain/src/hippo_brain/mcp.py` — Replace MetricsCollector usage with OTel instruments
- `brain/src/hippo_brain/server.py` — Add enrichment loop metrics
- `brain/src/hippo_brain/client.py` — Add LM Studio call metrics
- `brain/src/hippo_brain/embeddings.py` — Add embedding metrics
- `brain/src/hippo_brain/rag.py` — Add RAG pipeline metrics
- `brain/tests/test_mcp_logging.py` — Update for MetricsCollector removal
- `brain/tests/test_telemetry.py` — Add meter provider tests

### Daemon (Rust)
- `crates/hippo-daemon/src/metrics.rs` — New: instrument definitions
- `crates/hippo-daemon/src/daemon.rs` — Add metric calls at ingestion/flush/request sites
- `crates/hippo-daemon/src/main.rs` or `lib.rs` — Register metrics module

### Dashboards
- `otel/grafana/dashboards/hippo-overview.json` — Replace
- `otel/grafana/dashboards/hippo-enrichment.json` — Replace
- `otel/grafana/dashboards/hippo-daemon.json` — New

### Tests
- Brain: update existing tests that reference MetricsCollector
- Daemon: no new tests needed (metrics are fire-and-forget counters)
