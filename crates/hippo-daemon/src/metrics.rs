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
        .f64_histogram("hippo.daemon.flush.duration")
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
        .f64_histogram("hippo.daemon.request.duration")
        .with_description("Per-request-type latency")
        .with_unit("ms")
        .build()
});

// --- Redaction ---

/// Counter of secret replacements. Callers should pass a `rule` attribute
/// identifying which redaction pattern fired (e.g. `"aws_access_key"`,
/// `"github_pat"`). Aggregate counts are recoverable by summing across `rule`
/// in PromQL; per-rule breakdown is recoverable from the label dimension.
pub static REDACTIONS: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.daemon.redactions")
        .with_description("Secret replacements applied, labelled by redaction rule name")
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

// --- Watcher ---

pub static WATCHER_SEGMENTS_INGESTED: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.watcher.segments.ingested")
        .with_description("Segments inserted by the FS watcher")
        .build()
});

pub static WATCHER_PROCESS_DURATION_MS: LazyLock<Histogram<f64>> = LazyLock::new(|| {
    METER
        .f64_histogram("hippo.watcher.process.duration")
        .with_description("Per-file processing time in the FS watcher")
        .with_unit("ms")
        .build()
});

pub static WATCHER_EVENTS_DROPPED: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.watcher.events.dropped")
        .with_description("FSEvents notifications dropped due to full channel")
        .build()
});

// --- Probe ---

/// Increment once per probe run; use `source` and `ok` attributes to slice.
pub static PROBE_RUN: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.probe.run")
        .with_description("Synthetic probe executions, labelled by source and ok")
        .build()
});

pub static PROBE_LAG_MS: LazyLock<Histogram<f64>> = LazyLock::new(|| {
    METER
        .f64_histogram("hippo.probe.lag")
        .with_description("Probe round-trip lag from submission to DB row")
        .with_unit("ms")
        .build()
});

// --- Watchdog ---

pub static WATCHDOG_RUN: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.watchdog.run")
        .with_description("Watchdog evaluation cycles completed")
        .build()
});

pub static WATCHDOG_ALARMS_FIRED: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.watchdog.alarms.fired")
        .with_description("New capture alarms inserted, labelled by invariant_id")
        .build()
});

/// Per-source violation counter as specified in docs/capture/architecture.md.
/// Complements WATCHDOG_ALARMS_FIRED (which slices by invariant_id) with the source dimension
/// required by the spec for dashboards and alerts.
pub static WATCHDOG_INVARIANT_VIOLATION: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.watchdog.invariant_violation")
        .with_description("Invariant violations by capture source, per spec 02-invariants.md")
        .build()
});

/// Alarms transitioned from active to resolved by the auto-resolve loop
/// after the underlying invariant stayed clean for 2 consecutive ticks.
pub static WATCHDOG_ALARMS_AUTO_RESOLVED: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.watchdog.alarms.auto_resolved")
        .with_description("Alarms cleared automatically after invariant stayed clean")
        .build()
});

/// Active alarms whose `clean_ticks` counter was reset to 0 by a re-violation
/// during the same tick. Leading indicator of a flapping source: a non-zero
/// rate here means an invariant is healing and re-violating without ever
/// reaching the 2-tick auto-resolve threshold.
pub static WATCHDOG_ALARMS_RESET: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.watchdog.alarms.reset")
        .with_description("Active alarms whose clean_ticks was reset by a re-violation")
        .build()
});

/// BT-15 + post-review I-3: Counter incremented every time a sqlite operation
/// hits SQLITE_BUSY. `busy_timeout=5000` handles the common case before this
/// fires; a non-zero rate here under bench load means write contention on the
/// same DB — useful for distinguishing "this model is slow" from "this model
/// causes SQLite write contention that backs up the queue."
///
/// Original BT-15 only instrumented the watchdog alarm-insert retry (a cold
/// path); post-review I-3 adds instrumentation across the daemon flush hot
/// path (event inserts and source_health updates) so contention from real
/// bench traffic is actually observable.
pub static DB_BUSY_COUNT: LazyLock<Counter<u64>> = LazyLock::new(|| {
    METER
        .u64_counter("hippo.daemon.db_busy_count")
        .with_description("SQLITE_BUSY events seen by the daemon (after busy_timeout)")
        .build()
});

/// Increment `DB_BUSY_COUNT` iff `err` is SQLITE_BUSY, tagging the originating
/// call site via `op`. Returns whether the increment fired so callers can
/// emit a contention-specific log alongside the generic warn.
///
/// Always cfg-gated by `feature = "otel"` at the call site — non-otel builds
/// see the same error-path semantics minus the metric.
pub fn record_db_busy(err: &rusqlite::Error, op: &'static str) -> bool {
    if crate::is_sqlite_busy(err) {
        DB_BUSY_COUNT.add(1, &[opentelemetry::KeyValue::new("op", op)]);
        true
    } else {
        false
    }
}
