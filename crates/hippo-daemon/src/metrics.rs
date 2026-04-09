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
