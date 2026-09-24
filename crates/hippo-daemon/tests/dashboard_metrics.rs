#![cfg(feature = "otel")]

use std::collections::BTreeMap;
use std::path::Path;
use std::time::Duration;

use hippo_core::config::HippoConfig;
use hippo_core::protocol::{DaemonRequest, DaemonResponse};
use hippo_daemon::{commands::send_request, metrics};
use opentelemetry::global;
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, Metric, MetricData};
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};

// One integration-test process owns the global provider and LazyLock instruments.
// No collector, production daemon, or live database is involved.
#[tokio::test]
async fn dashboards_reference_exported_rust_metrics() {
    let exporter = InMemoryMetricExporter::default();
    let provider = SdkMeterProvider::builder()
        .with_reader(PeriodicReader::builder(exporter.clone()).build())
        .build();
    global::set_meter_provider(provider.clone());

    let temp = tempfile::tempdir().unwrap();
    let mut config = HippoConfig::default();
    config.storage.data_dir = temp.path().join("data");
    config.storage.config_dir = temp.path().join("config");
    config.brain.port = 0; // No connection to a running brain during the handshake.
    config.inference.base_url = "http://127.0.0.1:0".into();
    hippo_core::storage::ensure_private_dir(&config.storage.data_dir).unwrap();
    let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
    conn.execute_batch(
        "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
         VALUES ('I-1', 1, '{}'), ('I-2', 1, '{}');
         UPDATE source_health SET last_event_ts = 1, consecutive_failures = 3, probe_ok = 0
         WHERE source = 'shell';",
    )
    .unwrap();
    let socket = config.socket_path();
    let daemon = tokio::spawn(hippo_daemon::daemon::run(config));
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
    loop {
        if matches!(
            send_request(&socket, &DaemonRequest::GetStatus).await,
            Ok(DaemonResponse::Status(_))
        ) {
            break;
        }
        assert!(tokio::time::Instant::now() < deadline, "daemon not ready");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }

    // Exercise the shipped instruments, including paths not hit by an idle daemon.
    for counter in [
        &metrics::EVENTS_INGESTED,
        &metrics::EVENTS_DROPPED,
        &metrics::FLUSH_EVENTS,
        &metrics::REDACTIONS,
        &metrics::SESSIONS_CREATED,
        &metrics::FALLBACK_WRITES,
        &metrics::FALLBACK_RECOVERED,
        &metrics::WATCHER_SEGMENTS_INGESTED,
        &metrics::WATCHER_EVENTS_DROPPED,
        &metrics::PROBE_RUN,
        &metrics::WATCHDOG_RUN,
        &metrics::WATCHDOG_ALARMS_FIRED,
        &metrics::WATCHDOG_INVARIANT_VIOLATION,
        &metrics::WATCHDOG_ALARMS_AUTO_RESOLVED,
        &metrics::WATCHDOG_ALARMS_RESET,
    ] {
        counter.add(2, &[]);
    }
    assert!(metrics::record_db_busy(
        &rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_BUSY),
            None
        ),
        "dashboard_contract",
    ));
    for histogram in [
        &metrics::FLUSH_DURATION_MS,
        &metrics::WATCHER_PROCESS_DURATION_MS,
        &metrics::PROBE_LAG_MS,
    ] {
        histogram.record(25.0, &[]);
    }
    metrics::FLUSH_BATCH_SIZE.record(2, &[]);

    // Health refresh runs asynchronously. Collect until the seeded alarms are visible.
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
    let snapshots = loop {
        exporter.reset();
        provider.force_flush().unwrap();
        let snapshots = exporter.get_finished_metrics().unwrap();
        if snapshots
            .iter()
            .flat_map(|resource| resource.scope_metrics())
            .flat_map(|scope| scope.metrics())
            .any(|metric| metric.name() == "hippo.daemon.health.grade" && gauge_value(metric) == 80)
        {
            break snapshots;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "alarm gauge not refreshed"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    };
    send_request(&socket, &DaemonRequest::Shutdown)
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(6), daemon)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    provider.shutdown().unwrap();

    let mut emitted = BTreeMap::new();
    for metric in snapshots
        .iter()
        .flat_map(|resource| resource.scope_metrics())
        .flat_map(|scope| scope.metrics())
    {
        let mut name = metric.name().replace('.', "_");
        name.push_str(match metric.unit() {
            "" => "",
            "ms" => "_milliseconds",
            "By" => "_bytes",
            "1" => "_ratio",
            unit => panic!("unhandled exported metric unit {unit:?}"),
        });
        let kind = match metric.data() {
            AggregatedMetrics::U64(data) => sample_kind(data),
            AggregatedMetrics::F64(data) => sample_kind(data),
            AggregatedMetrics::I64(data) => sample_kind(data),
        };
        if kind == "counter" {
            name.push_str("_total");
        }
        emitted.insert(name, kind);
        match metric.name() {
            "hippo.daemon.health.active_alarms" => assert_eq!(gauge_value(metric), 2),
            "hippo.daemon.source_health.consecutive_failures" => assert_eq!(gauge_value(metric), 3),
            "hippo.daemon.source_health.probe_ok" => assert_eq!(gauge_value(metric), 0),
            "hippo.daemon.source_health.lag" => assert!(gauge_value(metric) > 0),
            _ => {}
        }
    }

    // Python parses dashboard/alert queries against this shared contract.
    // Here every Rust contract entry must have real exported data of the right kind.
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let contract: BTreeMap<String, String> = serde_json::from_str(
        &std::fs::read_to_string(repo.join("tests/fixtures/otel-metric-names.json")).unwrap(),
    )
    .unwrap();
    for (name, kind) in contract {
        // Brain-only entries are validated by the Python collection test.
        if name.starts_with("hippo_brain_") || name == "process_threads" {
            continue;
        }
        assert_eq!(
            emitted.get(&name).copied(),
            Some(kind.as_str()),
            "exported contract for {name}"
        );
    }
}

fn gauge_value(metric: &Metric) -> u64 {
    let AggregatedMetrics::U64(MetricData::Gauge(gauge)) = metric.data() else {
        panic!("{} is not a u64 gauge", metric.name());
    };
    gauge
        .data_points()
        .find(|point| {
            point.attributes().next().is_none()
                || point.attributes().any(|attribute| {
                    attribute.key.as_str() == "source" && attribute.value.as_str() == "shell"
                })
        })
        .expect("gauge sample for the seeded source")
        .value()
}

fn sample_kind<T>(data: &MetricData<T>) -> &'static str {
    match data {
        MetricData::Gauge(gauge) => {
            assert!(gauge.data_points().next().is_some(), "gauge has no samples");
            "gauge"
        }
        MetricData::Sum(sum) => {
            assert!(sum.data_points().next().is_some(), "counter has no samples");
            assert!(sum.is_monotonic(), "dashboard expects a counter");
            "counter"
        }
        MetricData::Histogram(histogram) => {
            assert!(histogram.data_points().any(|point| point.count() > 0));
            "histogram"
        }
        MetricData::ExponentialHistogram(_) => {
            panic!("dashboard expects explicit histogram buckets")
        }
    }
}
