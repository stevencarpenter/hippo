# OTel Metrics Instrumentation & Dashboards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire up OpenTelemetry metrics for the brain (Python) and daemon (Rust), then replace existing Grafana dashboards with metric-driven versions.

**Architecture:** Brain adds a MeterProvider to `telemetry.py` and creates instruments in each module. Daemon adds a `metrics.rs` module with LazyLock instruments behind `#[cfg(feature = "otel")]`. Three Grafana dashboards (Overview, Enrichment, Daemon) are rebuilt around Prometheus queries with Loki/Tempo drill-down panels.

**Tech Stack:** OpenTelemetry SDK (Python 1.33+, Rust opentelemetry 0.x), Prometheus, Grafana 12.x

**Spec:** `docs/superpowers/specs/2026-04-08-otel-metrics-design.md`

---

## File Structure

### Brain (Python) — Modified Files
- `brain/src/hippo_brain/telemetry.py` — Add MeterProvider, metric export, instrument factory
- `brain/src/hippo_brain/mcp_logging.py` — Remove MetricsCollector class
- `brain/src/hippo_brain/mcp.py` — Replace MetricsCollector with OTel instruments
- `brain/src/hippo_brain/server.py` — Add enrichment loop metrics + queue depth gauge
- `brain/src/hippo_brain/client.py` — Add LM Studio call metrics
- `brain/src/hippo_brain/embeddings.py` — Add embedding metrics
- `brain/src/hippo_brain/rag.py` — Add RAG pipeline metrics
- `brain/tests/test_telemetry.py` — Add meter provider tests
- `brain/tests/test_mcp_logging.py` — Remove MetricsCollector tests
- `brain/tests/test_mcp_server.py` — Update metrics assertions

### Daemon (Rust) — New + Modified Files
- `crates/hippo-daemon/src/metrics.rs` — **New:** instrument definitions
- `crates/hippo-daemon/src/lib.rs` — Add metrics module declaration
- `crates/hippo-daemon/src/daemon.rs` — Add metric calls at instrumentation points

### Dashboards — Replaced + New Files
- `otel/grafana/dashboards/hippo-overview.json` — Replace with metric-driven version
- `otel/grafana/dashboards/hippo-enrichment.json` — Replace with metric-driven version
- `otel/grafana/dashboards/hippo-daemon.json` — **New**

---

## Task 1: Brain — Add MeterProvider to telemetry.py

**Files:**
- Modify: `brain/src/hippo_brain/telemetry.py`
- Modify: `brain/tests/test_telemetry.py`

- [ ] **Step 1: Write the failing test for meter provider initialization**

Add to `brain/tests/test_telemetry.py`:

```python
def test_telemetry_enabled_creates_meter_provider():
    """When OTel is enabled, init_telemetry should set up a meter provider."""
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
        shutdown = init_telemetry("test-service", endpoint="http://localhost:4318")
        if shutdown is not None:
            # Verify we can get a meter (doesn't raise)
            from opentelemetry import metrics as otel_metrics

            meter = otel_metrics.get_meter("test")
            counter = meter.create_counter("test.counter")
            counter.add(1)  # Should not raise
            shutdown()


def test_get_meter_returns_none_when_disabled():
    """get_meter should return None when OTel is disabled."""
    from hippo_brain.telemetry import get_meter

    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("HIPPO_OTEL_ENABLED", None)
        result = get_meter()
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project brain --extra dev pytest brain/tests/test_telemetry.py -v`
Expected: FAIL — `get_meter` does not exist yet

- [ ] **Step 3: Add MeterProvider to telemetry.py**

In `brain/src/hippo_brain/telemetry.py`, add metrics provider setup inside `init_telemetry()`. After the existing log exporter imports (line 36), add the metrics import:

```python
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
```

After the logger_provider block (after line 64), add:

```python
    # Metrics
    metric_exporter = OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")
    metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=15000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    otel_metrics.set_meter_provider(meter_provider)
```

Update the shutdown function to include meter_provider:

```python
    def shutdown() -> None:
        tracer_provider.shutdown()
        logger_provider.shutdown()
        meter_provider.shutdown()
```

Add a `get_meter` function at the end of the file:

```python
def get_meter(name: str = "hippo-brain"):
    """Get OTel meter if available, else return None."""
    if not _is_otel_enabled():
        return None
    try:
        from opentelemetry import metrics as otel_metrics

        return otel_metrics.get_meter(name)
    except ImportError:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project brain --extra dev pytest brain/tests/test_telemetry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/telemetry.py brain/tests/test_telemetry.py
git commit -m "feat(brain): add MeterProvider to OTel telemetry init"
```

---

## Task 2: Brain — Remove MetricsCollector, wire OTel instruments into MCP server

**Files:**
- Modify: `brain/src/hippo_brain/mcp_logging.py`
- Modify: `brain/src/hippo_brain/mcp.py`
- Modify: `brain/tests/test_mcp_logging.py`
- Modify: `brain/tests/test_mcp_server.py`

- [ ] **Step 1: Update test_mcp_logging.py — remove MetricsCollector tests, keep setup_logging**

Replace the contents of the MetricsCollector test section. The file currently has `TestSetupLogging` (keep) and `TestMetricsCollector` (remove). Replace `test_mcp_logging.py` with:

```python
import logging

from hippo_brain.mcp_logging import setup_logging


def test_setup_logging_creates_logger():
    logger = setup_logging("test-mcp")
    assert logger.name == "test-mcp"
    assert logger.level == logging.INFO


def test_setup_logging_writes_to_stderr(capsys):
    logger = setup_logging("test-mcp-stderr")
    logger.info("test message")
    captured = capsys.readouterr()
    assert "test message" in captured.err
```

- [ ] **Step 2: Update mcp_logging.py — remove MetricsCollector**

Remove the `MetricsCollector` class and `dataclass` import from `brain/src/hippo_brain/mcp_logging.py`. The file should contain only `setup_logging`:

```python
"""Structured logging for the Hippo MCP server.

Logging goes to stderr (stdout is reserved for MCP stdio transport).
"""

import logging
import sys


def setup_logging(server_name: str) -> logging.Logger:
    """Configure structured logging to stderr for the MCP server."""
    logger = logging.getLogger(server_name)
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(handler)

    logger.propagate = False
    return logger
```

- [ ] **Step 3: Update mcp.py — replace MetricsCollector with OTel instruments**

In `brain/src/hippo_brain/mcp.py`:

Replace the import line:
```python
from hippo_brain.mcp_logging import MetricsCollector, setup_logging
```
with:
```python
from hippo_brain.mcp_logging import setup_logging
from hippo_brain.telemetry import get_meter
```

Replace the global `metrics = MetricsCollector()` (line 32) with OTel instruments:

```python
_meter = get_meter()

_tool_calls = _meter.create_counter("hippo.brain.mcp.tool_calls", description="MCP tool invocations") if _meter else None
_tool_errors = _meter.create_counter("hippo.brain.mcp.tool_errors", description="MCP tool failures") if _meter else None
_tool_duration = _meter.create_histogram("hippo.brain.mcp.tool_duration_ms", description="MCP tool latency in ms", unit="ms") if _meter else None
```

Add helpers to record metrics safely (same helpers used in task 3's server.py):

```python
def _add(counter, value=1, **attrs):
    if counter:
        counter.add(value, attrs)


def _hist(histogram, value, **attrs):
    if histogram:
        histogram.record(value, attrs)
```

Then replace all `metrics.tool_calls += 1` etc. throughout the tool handlers. For example, in `search_knowledge()`:

Replace `metrics.tool_calls += 1` with `_add(_tool_calls, tool="search_knowledge")`.

Replace `metrics.semantic_searches += 1` with `_add(_tool_calls, tool="search_knowledge")` (already counted above — semantic/lexical distinction goes into the existing timing log, not a separate counter).

Replace `metrics.lmstudio_errors += 1` with `_add(_tool_errors, tool="search_knowledge")`.

Replace `metrics.lexical_fallbacks += 1` with `_add(_tool_calls, tool="search_knowledge")` (fallbacks are already visible as mode switch in logs).

At each `elapsed = time.monotonic() - t0` line, add:
```python
_hist(_tool_duration, elapsed * 1000, tool="search_knowledge")
```

Repeat the pattern for `ask()`, `search_events()`, `get_entities()` — each records `_tool_calls` with its own `tool=` label, `_tool_errors` on exception, and `_tool_duration` at the elapsed calculation.

- [ ] **Step 4: Update test_mcp_server.py — replace metric assertions**

In the `_reset_state` fixture, remove the `snapshot = metrics.snapshot()` save/restore block (lines 230-238). Replace with just the state restoration (no metrics to save — OTel counters are cumulative and don't need reset between tests).

Remove the import of `metrics` from the import block at line 23.

For test assertions that checked `metrics.tool_calls`, `metrics.tool_errors`, etc., remove those assertions. The OTel counters are global and cumulative — asserting exact values in unit tests is fragile. The behavior is verified by the tool returning correct results.

For `test_search_knowledge_increments_errors_on_db_failure` and similar error-path tests, keep the test but remove the metric assertion lines — the test still verifies the exception handling works.

- [ ] **Step 5: Run all MCP tests**

Run: `uv run --project brain --extra dev pytest brain/tests/test_mcp_logging.py brain/tests/test_mcp_server.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add brain/src/hippo_brain/mcp_logging.py brain/src/hippo_brain/mcp.py brain/tests/test_mcp_logging.py brain/tests/test_mcp_server.py
git commit -m "feat(brain): replace MetricsCollector with OTel instruments in MCP server"
```

---

## Task 3: Brain — Add enrichment loop metrics to server.py

**Files:**
- Modify: `brain/src/hippo_brain/server.py`

- [ ] **Step 1: Add instrument creation at module level**

Near the top of `server.py`, after the existing imports (after line 45), add:

```python
from hippo_brain.telemetry import get_meter

_meter = get_meter()
_events_claimed = _meter.create_counter("hippo.brain.enrichment.events_claimed", description="Events pulled from enrichment queue") if _meter else None
_nodes_created = _meter.create_counter("hippo.brain.enrichment.nodes_created", description="Knowledge nodes written") if _meter else None
_enrichment_failures = _meter.create_counter("hippo.brain.enrichment.failures", description="Enrichment batch failures") if _meter else None
_loop_duration = _meter.create_histogram("hippo.brain.enrichment.loop_duration_ms", description="Enrichment cycle wall clock", unit="ms") if _meter else None


def _add(counter, value=1, **attrs):
    if counter:
        counter.add(value, attrs)


def _hist(histogram, value, **attrs):
    if histogram:
        histogram.record(value, attrs)
```

- [ ] **Step 2: Add queue depth observable gauge**

After the instrument creation, add the queue depth gauge. This needs a reference to the DB path, so register it in `create_app()` after the `BrainService` is created. In the `BrainService.__init__` method (or `create_app`), after the service is instantiated, add:

```python
if _meter:
    def _observe_queue_depths(callback_options):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                for source, table in [
                    ("shell", "enrichment_queue"),
                    ("claude", "claude_enrichment_queue"),
                    ("browser", "browser_enrichment_queue"),
                ]:
                    for status in ("pending", "failed"):
                        try:
                            count = conn.execute(
                                f"SELECT COUNT(*) FROM {table} WHERE status = ?",
                                (status,),
                            ).fetchone()[0]
                            yield otel_metrics.Observation(count, {"source": source, "status": status})
                        except Exception:
                            pass
            finally:
                conn.close()
        except Exception:
            pass

    import opentelemetry.metrics as otel_metrics
    _meter.create_observable_gauge(
        "hippo.brain.enrichment.queue_depth",
        callbacks=[_observe_queue_depths],
        description="Enrichment queue sizes",
    )
```

Place this in `create_app()` after `service = BrainService(...)`, before `return app`.

- [ ] **Step 3: Instrument enrichment loop and batch methods**

In `_enrichment_loop()` (line 324), wrap the main work in timing:

```python
async def _enrichment_loop(self):
    while True:
        try:
            await asyncio.sleep(self.poll_interval_secs)
            if not self.enrichment_running:
                continue
            t0 = time.monotonic()
            # ... existing gather logic ...
            _hist(_loop_duration, (time.monotonic() - t0) * 1000)
        except Exception as exc:
            # ... existing error handling ...
```

In `_enrich_shell_batches()` (around line 455 where events are claimed), after the claim:

```python
_add(_events_claimed, len(event_ids), source="shell")
```

After `write_knowledge_node()` succeeds (around line 484):

```python
_add(_nodes_created, source="shell")
```

In the exception handler (around line 509):

```python
_add(_enrichment_failures, source="shell")
```

Repeat the same pattern in `_enrich_claude_batches()` (source="claude") and `_enrich_browser_batches()` (source="browser") at equivalent locations.

- [ ] **Step 4: Run enrichment tests**

Run: `uv run --project brain --extra dev pytest brain/tests/test_enrichment.py brain/tests/test_server.py -v`
Expected: PASS (metrics are fire-and-forget; no assertions on them)

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/server.py
git commit -m "feat(brain): add enrichment loop OTel metrics"
```

---

## Task 4: Brain — Add LM Studio, embedding, and RAG metrics

**Files:**
- Modify: `brain/src/hippo_brain/client.py`
- Modify: `brain/src/hippo_brain/embeddings.py`
- Modify: `brain/src/hippo_brain/rag.py`

- [ ] **Step 1: Add metrics to client.py**

At the top of `client.py`, after existing imports:

```python
import time

from hippo_brain.telemetry import get_meter

_meter = get_meter()
_request_duration = _meter.create_histogram("hippo.brain.lmstudio.request_duration_ms", description="LM Studio API latency", unit="ms") if _meter else None
_lm_errors = _meter.create_counter("hippo.brain.lmstudio.errors", description="Failed LM Studio calls") if _meter else None
_prompt_tokens = _meter.create_histogram("hippo.brain.lmstudio.prompt_tokens", description="Prompt size in chars") if _meter else None
```

In the `chat()` method, wrap the HTTP call:

```python
async def chat(self, messages, model="", temperature=0.0, max_tokens=16384):
    t0 = time.monotonic()
    try:
        # ... existing httpx.post logic ...
        elapsed = (time.monotonic() - t0) * 1000
        if _request_duration:
            _request_duration.record(elapsed, {"method": "chat"})
        if _prompt_tokens:
            total_chars = sum(len(m.get("content", "")) for m in messages)
            _prompt_tokens.record(total_chars)
        return resp.json()["choices"][0]["message"]["content"]
    except Exception:
        if _lm_errors:
            _lm_errors.add(1, {"method": "chat"})
        raise
```

In the `embed()` method, same pattern:

```python
async def embed(self, texts, model=""):
    t0 = time.monotonic()
    try:
        # ... existing httpx.post logic ...
        if _request_duration:
            _request_duration.record((time.monotonic() - t0) * 1000, {"method": "embed"})
        return [e["embedding"] for e in resp.json()["data"]]
    except Exception:
        if _lm_errors:
            _lm_errors.add(1, {"method": "embed"})
        raise
```

- [ ] **Step 2: Add metrics to embeddings.py**

At the top of `embeddings.py`, after existing imports:

```python
import time

from hippo_brain.telemetry import get_meter

_meter = get_meter()
_embed_duration = _meter.create_histogram("hippo.brain.embedding.duration_ms", description="Time to embed a knowledge node", unit="ms") if _meter else None
_embed_failures = _meter.create_counter("hippo.brain.embedding.failures", description="Failed embedding attempts") if _meter else None
```

In `embed_knowledge_node()`, wrap the function body:

```python
async def embed_knowledge_node(client, table, node_dict, embed_model="", command_model=""):
    t0 = time.monotonic()
    try:
        # ... existing embedding logic ...
        if _embed_duration:
            _embed_duration.record((time.monotonic() - t0) * 1000)
    except Exception:
        if _embed_failures:
            _embed_failures.add(1)
        raise
```

- [ ] **Step 3: Add metrics to rag.py**

At the top of `rag.py`, after existing imports:

```python
import time

from hippo_brain.telemetry import get_meter

_meter = get_meter()
_rag_duration = _meter.create_histogram("hippo.brain.rag.duration_ms", description="RAG stage latency", unit="ms") if _meter else None
_rag_hits = _meter.create_histogram("hippo.brain.rag.retrieval_hits", description="Vector search result count") if _meter else None
```

In the `ask()` function, instrument each stage:

```python
async def ask(question, lm_client, vector_table, query_model, embedding_model, limit=10):
    # Stage 1: Embed
    t0 = time.monotonic()
    query_vec = await lm_client.embed([question], model=embedding_model)
    if _rag_duration:
        _rag_duration.record((time.monotonic() - t0) * 1000, {"stage": "embed"})

    # Stage 2: Retrieve
    t0 = time.monotonic()
    hits = search_similar(vector_table, query_vec[0], limit=limit)
    if _rag_duration:
        _rag_duration.record((time.monotonic() - t0) * 1000, {"stage": "retrieve"})
    if _rag_hits:
        _rag_hits.record(len(hits))

    # ... build prompt ...

    # Stage 3: Synthesize
    t0 = time.monotonic()
    answer = await lm_client.chat(messages=messages, model=query_model)
    if _rag_duration:
        _rag_duration.record((time.monotonic() - t0) * 1000, {"stage": "synthesize"})

    # ... return result ...
```

- [ ] **Step 4: Run full test suite**

Run: `uv run --project brain --extra dev pytest brain/tests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/client.py brain/src/hippo_brain/embeddings.py brain/src/hippo_brain/rag.py
git commit -m "feat(brain): add LM Studio, embedding, and RAG OTel metrics"
```

---

## Task 5: Daemon — Create metrics.rs with instrument definitions

**Files:**
- Create: `crates/hippo-daemon/src/metrics.rs`
- Modify: `crates/hippo-daemon/src/lib.rs`

- [ ] **Step 1: Create metrics.rs with OTel instruments**

Create `crates/hippo-daemon/src/metrics.rs`:

```rust
//! OTel metric instruments for hippo-daemon.
//!
//! Only compiled with `--features otel`. Each instrument is a `LazyLock`
//! static that resolves against the global meter provider set in `telemetry::init`.

use opentelemetry::global;
use opentelemetry::metrics::{Counter, Histogram};
use std::sync::LazyLock;

static METER: LazyLock<opentelemetry::metrics::Meter> =
    LazyLock::new(|| global::meter("hippo-daemon"));

// --- Ingestion ---

pub static EVENTS_INGESTED: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.daemon.events.ingested")
        .with_description("Events accepted into buffer")
        .build()
});

pub static EVENTS_DROPPED: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.daemon.events.dropped")
        .with_description("Events rejected at buffer capacity")
        .build()
});

// --- Flush ---

pub static FLUSH_EVENTS: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.daemon.flush.events")
        .with_description("Events written to SQLite per flush")
        .build()
});

pub static FLUSH_DURATION_MS: LazyLock<Histogram<f64>> = LazyLock::new(|| {
    METER
        .f64_histogram("hippo.daemon.flush.duration_ms")
        .with_description("Time per flush batch")
        .with_unit("ms")
        .build()
});

pub static FLUSH_BATCH_SIZE: LazyLock<Histogram<u64>> = LazyLock::new(|| {
    METER
        .u64_histogram("hippo.daemon.flush.batch_size")
        .with_description("Events per flush batch")
        .build()
});

// --- Requests ---

pub static REQUESTS: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.daemon.requests")
        .with_description("Socket request count")
        .build()
});

pub static REQUEST_DURATION_MS: LazyLock<Histogram<f64>> = LazyLock::new(|| {
    METER
        .f64_histogram("hippo.daemon.request.duration_ms")
        .with_description("Per-request-type latency")
        .with_unit("ms")
        .build()
});

// --- Redaction ---

pub static REDACTIONS: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.daemon.redactions")
        .with_description("Total secret replacements applied")
        .build()
});

// --- Sessions ---

pub static SESSIONS_CREATED: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.daemon.sessions.created")
        .with_description("New shell sessions")
        .build()
});

// --- Fallback ---

pub static FALLBACK_WRITES: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.daemon.fallback.writes")
        .with_description("Events written to fallback JSONL")
        .build()
});

pub static FALLBACK_RECOVERED: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.daemon.fallback.recovered")
        .with_description("Events recovered from fallback")
        .build()
});
```

- [ ] **Step 2: Add module declaration to lib.rs**

In `crates/hippo-daemon/src/lib.rs`, add after the existing `telemetry` module:

```rust
#[cfg(feature = "otel")]
pub mod metrics;
```

- [ ] **Step 3: Build with otel feature to verify**

Run: `cargo build -p hippo-daemon --features otel`
Expected: Compiles successfully

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-daemon/src/metrics.rs crates/hippo-daemon/src/lib.rs
git commit -m "feat(daemon): add OTel metric instrument definitions"
```

---

## Task 6: Daemon — Wire metrics into daemon.rs

**Files:**
- Modify: `crates/hippo-daemon/src/daemon.rs`

- [ ] **Step 1: Add conditional metrics import**

At the top of `daemon.rs`, add:

```rust
#[cfg(feature = "otel")]
use crate::metrics;
#[cfg(feature = "otel")]
use opentelemetry::KeyValue;
#[cfg(feature = "otel")]
use std::time::Instant as OtelInstant;
```

- [ ] **Step 2: Instrument handle_request()**

In `handle_request()`, at the top of the function (after the match begins):

```rust
pub async fn handle_request(state: &Arc<DaemonState>, request: Request) -> Response {
    #[cfg(feature = "otel")]
    let req_start = OtelInstant::now();

    let request_type_str = match &request {
        Request::IngestEvent { .. } => "IngestEvent",
        Request::GetStatus => "GetStatus",
        Request::GetSessions { .. } => "GetSessions",
        Request::GetEvents { .. } => "GetEvents",
        Request::GetEntities { .. } => "GetEntities",
        Request::RawQuery { .. } => "RawQuery",
        Request::Shutdown => "Shutdown",
    };

    #[cfg(feature = "otel")]
    metrics::REQUESTS.add(1, &[KeyValue::new("type", request_type_str)]);
```

At each return point (or use a defer pattern), record duration:

Before the final response is returned, add a helper block. The cleanest approach is to wrap the body and record at the end. Add at the very end of `handle_request()`, before the final return:

```rust
    #[cfg(feature = "otel")]
    {
        let elapsed = req_start.elapsed().as_secs_f64() * 1000.0;
        metrics::REQUEST_DURATION_MS.record(elapsed, &[KeyValue::new("type", request_type_str)]);
    }
```

In the `IngestEvent` arm, after the event is pushed to buffer:

```rust
#[cfg(feature = "otel")]
metrics::EVENTS_INGESTED.add(1, &[KeyValue::new("type", "shell")]);
```

In the drop branch (buffer at capacity):

```rust
#[cfg(feature = "otel")]
metrics::EVENTS_DROPPED.add(1, &[KeyValue::new("type", "shell")]);
```

For browser events, use `KeyValue::new("type", "browser")` instead.

- [ ] **Step 3: Instrument flush_events()**

At the start of `flush_events()`:

```rust
#[cfg(feature = "otel")]
let flush_start = OtelInstant::now();
```

After the flush loop completes (before returning count):

```rust
#[cfg(feature = "otel")]
{
    let count_u64 = count as u64;
    metrics::FLUSH_EVENTS.add(count_u64, &[]);
    metrics::FLUSH_BATCH_SIZE.record(count_u64, &[]);
    metrics::FLUSH_DURATION_MS.record(flush_start.elapsed().as_secs_f64() * 1000.0, &[]);
}
```

Where redaction is applied (after `redaction.redact()` returns), add:

```rust
#[cfg(feature = "otel")]
if redaction_result.count > 0 {
    metrics::REDACTIONS.add(redaction_result.count as u64, &[]);
}
```

Where a new session is created, add:

```rust
#[cfg(feature = "otel")]
metrics::SESSIONS_CREATED.add(1, &[]);
```

Where fallback writes happen, add:

```rust
#[cfg(feature = "otel")]
metrics::FALLBACK_WRITES.add(1, &[]);
```

Where fallback recovery happens, add:

```rust
#[cfg(feature = "otel")]
metrics::FALLBACK_RECOVERED.add(1, &[]);
```

- [ ] **Step 4: Register observable gauges in daemon startup**

In `daemon.rs`, in the `run()` function after the OTel guard is created (or in `main.rs` after `telemetry::init`), register observable gauges for buffer size, DB size, and fallback pending. These need access to `DaemonState`, so register them after state is created:

```rust
#[cfg(feature = "otel")]
{
    use opentelemetry::global;
    let meter = global::meter("hippo-daemon");

    let state_ref = Arc::clone(&state);
    let _ = meter
        .u64_observable_gauge("hippo.daemon.buffer.size")
        .with_description("Current event buffer occupancy")
        .with_callback(move |gauge| {
            if let Ok(buf) = state_ref.event_buffer.try_lock() {
                gauge.observe(buf.len() as u64, &[]);
            }
        })
        .build();

    let db_path = state.config.data_dir().join("hippo.db");
    let _ = meter
        .u64_observable_gauge("hippo.daemon.db.size_bytes")
        .with_description("SQLite file size")
        .with_callback(move |gauge| {
            if let Ok(meta) = std::fs::metadata(&db_path) {
                gauge.observe(meta.len(), &[]);
            }
        })
        .build();

    let fallback_dir = state.config.data_dir().join("fallback");
    let _ = meter
        .u64_observable_gauge("hippo.daemon.fallback.pending")
        .with_description("Unrecovered fallback files")
        .with_callback(move |gauge| {
            let count = std::fs::read_dir(&fallback_dir)
                .map(|entries| entries.count() as u64)
                .unwrap_or(0);
            gauge.observe(count, &[]);
        })
        .build();
}
```

- [ ] **Step 5: Build and test**

Run: `cargo build -p hippo-daemon --features otel && cargo test -p hippo-daemon`
Expected: Compiles and tests pass

- [ ] **Step 6: Also verify non-otel build still works**

Run: `cargo build -p hippo-daemon && cargo test -p hippo-daemon`
Expected: Compiles and tests pass (no otel code included)

- [ ] **Step 7: Commit**

```bash
git add crates/hippo-daemon/src/daemon.rs
git commit -m "feat(daemon): wire OTel metrics into request handling and flush loop"
```

---

## Task 7: Dashboard — Hippo Overview

**Files:**
- Replace: `otel/grafana/dashboards/hippo-overview.json`

- [ ] **Step 1: Write the overview dashboard JSON**

Replace `otel/grafana/dashboards/hippo-overview.json` with a dashboard containing:

**Row 1 — Key indicators (4 stat panels across the top):**

Panel 1 (stat): "Events Ingested / min"
- Query: `sum(rate(hippo_daemon_events_ingested_total[5m])) * 60`
- gridPos: x=0, y=0, w=6, h=4

Panel 2 (stat): "Events Dropped"
- Query: `sum(hippo_daemon_events_dropped_total)`
- gridPos: x=6, y=0, w=6, h=4
- Thresholds: green=0, red=1

Panel 3 (stat): "Queue Depth"
- Query: `sum(hippo_brain_enrichment_queue_depth{status="pending"})`
- gridPos: x=12, y=0, w=6, h=4
- Thresholds: green=0, yellow=50, red=200

Panel 4 (stat): "Nodes Created / hr"
- Query: `sum(rate(hippo_brain_enrichment_nodes_created_total[1h])) * 3600`
- gridPos: x=18, y=0, w=6, h=4

**Row 2 — Trends (2 time series + 1 logs panel):**

Panel 5 (timeseries): "Ingestion vs Enrichment Rate"
- Target A: `sum(rate(hippo_daemon_events_ingested_total[5m])) * 60` legend "ingested/min"
- Target B: `sum(rate(hippo_brain_enrichment_events_claimed_total[5m])) * 60` legend "enriched/min"
- gridPos: x=0, y=4, w=8, h=8

Panel 6 (timeseries): "Error Rate"
- Target A: `sum(rate(hippo_brain_lmstudio_errors_total[5m])) * 60` legend "LM Studio errors"
- Target B: `sum(rate(hippo_brain_enrichment_failures_total[5m])) * 60` legend "enrichment failures"
- Target C: `sum(rate(hippo_daemon_events_dropped_total[5m])) * 60` legend "events dropped"
- gridPos: x=8, y=4, w=8, h=8

Panel 7 (logs): "Error Logs"
- Datasource: loki
- Query: `{service_name=~"hippo.*"} |~ "(?i)error|failed|panic"`
- gridPos: x=16, y=4, w=8, h=8

Use schemaVersion 39, uid "hippo-overview", tags ["hippo", "observability"], refresh "30s", time from "now-1h" to "now". All Prometheus panels use datasource `{"type": "prometheus", "uid": "prometheus"}`.

- [ ] **Step 2: Verify dashboard loads**

Restart Grafana to pick up the new JSON:
```bash
docker compose -f otel/docker-compose.yml restart grafana
```

Open `http://localhost:3000/d/hippo-overview` and verify panels load (they'll show "No data" until metrics flow, which is expected).

- [ ] **Step 3: Commit**

```bash
git add otel/grafana/dashboards/hippo-overview.json
git commit -m "feat(dashboards): replace overview dashboard with metric-driven version"
```

---

## Task 8: Dashboard — Enrichment Pipeline

**Files:**
- Replace: `otel/grafana/dashboards/hippo-enrichment.json`

- [ ] **Step 1: Write the enrichment dashboard JSON**

Replace `otel/grafana/dashboards/hippo-enrichment.json` with:

**Row 1 — Queue health (3 panels):**

Panel 1 (timeseries): "Queue Depth by Source"
- Target A: `hippo_brain_enrichment_queue_depth{status="pending",source="shell"}` legend "shell"
- Target B: `hippo_brain_enrichment_queue_depth{status="pending",source="claude"}` legend "claude"
- Target C: `hippo_brain_enrichment_queue_depth{status="pending",source="browser"}` legend "browser"
- gridPos: x=0, y=0, w=8, h=8

Panel 2 (stat): "Failed Queue Items"
- Query: `sum(hippo_brain_enrichment_queue_depth{status="failed"}) by (source)`
- gridPos: x=8, y=0, w=4, h=8
- Thresholds: green=0, red=1

Panel 3 (timeseries): "Events Claimed / min"
- Target A-C: `rate(hippo_brain_enrichment_events_claimed_total{source="shell"}[5m]) * 60` etc. for each source
- Stacked bar display
- gridPos: x=12, y=0, w=12, h=8

**Row 2 — LLM performance (3 panels):**

Panel 4 (timeseries): "LM Studio Latency"
- Target A: `histogram_quantile(0.5, rate(hippo_brain_lmstudio_request_duration_ms_bucket{method="chat"}[5m]))` legend "p50 chat"
- Target B: `histogram_quantile(0.95, rate(hippo_brain_lmstudio_request_duration_ms_bucket{method="chat"}[5m]))` legend "p95 chat"
- Target C: `histogram_quantile(0.99, rate(hippo_brain_lmstudio_request_duration_ms_bucket{method="chat"}[5m]))` legend "p99 chat"
- gridPos: x=0, y=8, w=8, h=8

Panel 5 (timeseries): "LM Studio Errors / min"
- Query: `sum(rate(hippo_brain_lmstudio_errors_total[5m])) by (method) * 60`
- gridPos: x=8, y=8, w=8, h=8

Panel 6 (timeseries): "Prompt Size Distribution"
- Query: `histogram_quantile(0.5, rate(hippo_brain_lmstudio_prompt_tokens_bucket[5m]))` legend "p50"
- Query: `histogram_quantile(0.95, rate(hippo_brain_lmstudio_prompt_tokens_bucket[5m]))` legend "p95"
- gridPos: x=16, y=8, w=8, h=8

**Row 3 — Embedding + RAG (4 panels):**

Panel 7 (timeseries): "Embedding Duration"
- p50 + p95 of `hippo_brain_embedding_duration_ms`
- gridPos: x=0, y=16, w=6, h=8

Panel 8 (stat): "Embedding Failures"
- Query: `sum(hippo_brain_embedding_failures_total)`
- gridPos: x=6, y=16, w=3, h=8
- Thresholds: green=0, red=1

Panel 9 (timeseries): "RAG Latency by Stage"
- p50 of `hippo_brain_rag_duration_ms` by stage label
- Stacked bar
- gridPos: x=9, y=16, w=8, h=8

Panel 10 (timeseries): "RAG Retrieval Hits"
- p50 + p95 of `hippo_brain_rag_retrieval_hits`
- gridPos: x=17, y=16, w=7, h=8

**Row 4 — MCP + drill-down (3 panels):**

Panel 11 (timeseries): "MCP Tool Latency"
- `histogram_quantile(0.95, rate(hippo_brain_mcp_tool_duration_ms_bucket[5m]))` by tool
- gridPos: x=0, y=24, w=8, h=8

Panel 12 (timeseries): "MCP Tool Calls / min"
- `sum(rate(hippo_brain_mcp_tool_calls_total[5m])) by (tool) * 60`
- Stacked bar
- gridPos: x=8, y=24, w=8, h=8

Panel 13 (table): "Enrichment Traces"
- Datasource: tempo
- Query: `{resource.service.name = "hippo-brain" && name =~ "enrichment.*"}`
- gridPos: x=16, y=24, w=8, h=8

Use uid "hippo-enrichment", tags ["hippo", "enrichment", "performance"], time "now-6h".

- [ ] **Step 2: Verify dashboard loads**

```bash
docker compose -f otel/docker-compose.yml restart grafana
```

Open `http://localhost:3000/d/hippo-enrichment`.

- [ ] **Step 3: Commit**

```bash
git add otel/grafana/dashboards/hippo-enrichment.json
git commit -m "feat(dashboards): replace enrichment dashboard with metric-driven version"
```

---

## Task 9: Dashboard — Daemon

**Files:**
- Create: `otel/grafana/dashboards/hippo-daemon.json`

- [ ] **Step 1: Write the daemon dashboard JSON**

Create `otel/grafana/dashboards/hippo-daemon.json` with:

**Row 1 — Ingestion (4 panels):**

Panel 1 (timeseries): "Events Ingested / min"
- `sum(rate(hippo_daemon_events_ingested_total[5m])) by (type) * 60` legend "{{type}}"
- gridPos: x=0, y=0, w=8, h=8

Panel 2 (gauge): "Buffer Utilization"
- Query: `hippo_daemon_buffer_size` (if available as observable gauge, otherwise omit)
- gridPos: x=8, y=0, w=4, h=8

Panel 3 (stat): "Events Dropped"
- Query: `sum(hippo_daemon_events_dropped_total)`
- gridPos: x=12, y=0, w=4, h=8
- Thresholds: green=0, red=1

Panel 4 (stat): "Sessions Created / hr"
- Query: `sum(rate(hippo_daemon_sessions_created_total[1h])) * 3600`
- gridPos: x=16, y=0, w=8, h=8

**Row 2 — Flush + Storage (4 panels):**

Panel 5 (timeseries): "Flush Duration"
- p50: `histogram_quantile(0.5, rate(hippo_daemon_flush_duration_ms_bucket[5m]))`
- p95: `histogram_quantile(0.95, rate(hippo_daemon_flush_duration_ms_bucket[5m]))`
- gridPos: x=0, y=8, w=6, h=8

Panel 6 (timeseries): "Flush Batch Size"
- p50 + p95 of `hippo_daemon_flush_batch_size`
- gridPos: x=6, y=8, w=6, h=8

Panel 7 (timeseries): "DB Size"
- Query: `hippo_daemon_db_size_bytes`
- Unit: bytes
- gridPos: x=12, y=8, w=6, h=8

Panel 8 (timeseries): "Redactions / min"
- Query: `sum(rate(hippo_daemon_redactions_total[5m])) * 60`
- gridPos: x=18, y=8, w=6, h=8

**Row 3 — Reliability (4 panels):**

Panel 9 (timeseries): "Request Latency by Type"
- `histogram_quantile(0.95, rate(hippo_daemon_request_duration_ms_bucket[5m]))` by type
- gridPos: x=0, y=16, w=8, h=8

Panel 10 (stat): "Fallback Writes"
- Query: `sum(hippo_daemon_fallback_writes_total)`
- gridPos: x=8, y=16, w=4, h=8
- Thresholds: green=0, red=1

Panel 11 (stat): "Fallback Pending"
- Query: `hippo_daemon_fallback_pending`
- gridPos: x=12, y=16, w=4, h=8
- Thresholds: green=0, red=1

Panel 12 (logs): "Daemon Error Logs"
- Datasource: loki
- Query: `{service_name="hippo-daemon"} |~ "(?i)error|failed|panic"`
- gridPos: x=16, y=16, w=8, h=8

Use uid "hippo-daemon", tags ["hippo", "daemon"], time "now-1h", refresh "30s".

- [ ] **Step 2: Verify dashboard loads**

```bash
docker compose -f otel/docker-compose.yml restart grafana
```

Open `http://localhost:3000/d/hippo-daemon`.

- [ ] **Step 3: Commit**

```bash
git add otel/grafana/dashboards/hippo-daemon.json
git commit -m "feat(dashboards): add daemon metrics dashboard"
```

---

## Task 10: Integration verification

**Files:** None (verification only)

- [ ] **Step 1: Rebuild daemon with OTel**

```bash
cargo build -p hippo-daemon --features otel --release
```

- [ ] **Step 2: Run full brain test suite**

```bash
uv run --project brain --extra dev pytest brain/tests -v --tb=short
```

Expected: All tests pass.

- [ ] **Step 3: Run full daemon test suite**

```bash
cargo test --workspace
```

Expected: All tests pass.

- [ ] **Step 4: Run linters**

```bash
uv run --project brain --extra dev ruff check brain/src brain/tests
uv run --project brain --extra dev ruff format --check brain/src brain/tests
cargo clippy --all-targets -p hippo-daemon --features otel -- -D warnings
cargo fmt --check
```

Expected: No lint errors.

- [ ] **Step 5: Verify metrics flow end-to-end**

Restart the brain service to pick up new code, then check Prometheus:

```bash
mise run restart
sleep 30
docker compose -f otel/docker-compose.yml exec prometheus wget -qO- 'http://localhost:9090/api/v1/label/__name__/values' | python3 -c "import sys,json; d=json.load(sys.stdin); [print(n) for n in d.get('data',[]) if 'hippo' in n]"
```

Expected: `hippo_brain_*` and/or `hippo_daemon_*` metric names appear.

- [ ] **Step 6: Verify dashboards render**

Open each dashboard in Grafana and confirm panels show data or "No data" (not errors):
- `http://localhost:3000/d/hippo-overview`
- `http://localhost:3000/d/hippo-enrichment`
- `http://localhost:3000/d/hippo-daemon`

- [ ] **Step 7: Commit any fixups**

```bash
git add -A
git commit -m "chore: integration verification fixups"
```

(Skip this step if no changes were needed.)
