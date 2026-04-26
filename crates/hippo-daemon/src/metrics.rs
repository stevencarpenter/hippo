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
