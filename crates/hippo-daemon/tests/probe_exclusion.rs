//! Integration tests for probe_tag exclusion (AP-6: probe contamination).
//!
//! Verifies the two-layer defence:
//!
//! 1. **Upstream filter** — `flush_events` / `insert_event_at` /
//!    `insert_browser_event` skip the enrichment queue when `probe_tag IS NOT
//!    NULL`.  If this layer is bypassed, probe rows pile up in the queue
//!    indefinitely and cost real LLM calls.
//!
//! 2. **Downstream belt-and-braces** — `get_events` / `get_status` in
//!    storage.rs filter `probe_tag IS NULL` so even if a probe row somehow
//!    slipped through, user-facing queries would not surface it.
//!
//! Reference: docs/capture/anti-patterns.md AP-6.

#[path = "common/mod.rs"]
mod common;

use chrono::Utc;
use hippo_core::events::{BrowserEvent, EventEnvelope, EventPayload};
use hippo_core::protocol::{DaemonRequest, DaemonResponse};
use uuid::Uuid;

use common::{test_config, wait_for_daemon};

/// Layer 1: probe shell event skips enrichment_queue; real event is queued.
#[tokio::test]
async fn probe_shell_event_not_queued_for_enrichment() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    // --- probe event (probe_tag = Some) ---
    hippo_daemon::commands::handle_send_event_shell(
        &config,
        "__hippo_probe__".to_string(),
        0,
        "/tmp/probe-cwd".to_string(),
        42,
        None,
        None,
        None,
        false,
        Some("ok".to_string()),
        Some("probe-v1".to_string()), // probe_tag
        Some("shell".to_string()),    // source_kind
        None,                         // tool_name
    )
    .await
    .expect("probe shell send should succeed");

    // --- real event (probe_tag = None) ---
    hippo_daemon::commands::handle_send_event_shell(
        &config,
        "cargo build".to_string(),
        0,
        "/tmp/real-cwd".to_string(),
        800,
        None,
        None,
        None,
        false,
        Some("Compiling hippo-core".to_string()),
        None, // probe_tag = None
        None, // source_kind
        None, // tool_name
    )
    .await
    .expect("real shell send should succeed");

    tokio::time::sleep(std::time::Duration::from_millis(400)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();

    // Both rows land in `events`.
    let total: i64 = conn
        .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(total, 2, "expected 2 events total in events table");

    // Only the probe row has probe_tag set.
    let probe_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE probe_tag IS NOT NULL",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(probe_count, 1, "expected exactly 1 probe event");

    let probe_cmd: String = conn
        .query_row(
            "SELECT command FROM events WHERE probe_tag IS NOT NULL",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(probe_cmd, "__hippo_probe__");

    // Layer 1: only the real event is in enrichment_queue.
    let queued: i64 = conn
        .query_row("SELECT COUNT(*) FROM enrichment_queue", [], |r| r.get(0))
        .unwrap();
    assert_eq!(queued, 1, "probe events must not be queued for enrichment");

    let queued_cmd: String = conn
        .query_row(
            r#"SELECT e.command FROM enrichment_queue eq
               JOIN events e ON e.id = eq.event_id"#,
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(queued_cmd, "cargo build", "real event should be queued");

    // Layer 2: get_status only counts non-probe events.
    if let Ok(DaemonResponse::Status(status)) =
        hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::GetStatus).await
    {
        assert_eq!(
            status.events_today, 1,
            "get_status must exclude probe events; got {}",
            status.events_today
        );
    }

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}

/// Layer 1: probe browser event skips browser_enrichment_queue.
#[tokio::test]
async fn probe_browser_event_not_queued_for_enrichment() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    let probe_domain = "probe.hippo.local";

    // --- probe browser event ---
    let probe_envelope = EventEnvelope {
        envelope_id: Uuid::new_v4(),
        producer_version: 1,
        timestamp: Utc::now(),
        payload: EventPayload::Browser(Box::new(BrowserEvent {
            url: format!("https://{probe_domain}/probe"),
            title: "Hippo Probe".to_string(),
            domain: probe_domain.to_string(),
            dwell_ms: 50,
            scroll_depth: 0.0,
            extracted_text: None,
            search_query: None,
            referrer: None,
            content_hash: None,
        })),
        probe_tag: Some("probe-browser-v1".to_string()),
    };

    hippo_daemon::commands::send_event_fire_and_forget(
        &socket_path,
        &probe_envelope,
        config.daemon.socket_timeout_ms,
    )
    .await
    .expect("probe browser send should succeed");

    // --- real browser event ---
    let real_envelope = EventEnvelope {
        envelope_id: Uuid::new_v4(),
        producer_version: 1,
        timestamp: Utc::now(),
        payload: EventPayload::Browser(Box::new(BrowserEvent {
            url: "https://docs.rs/tokio".to_string(),
            title: "tokio - Rust".to_string(),
            domain: "docs.rs".to_string(),
            dwell_ms: 18_000,
            scroll_depth: 0.55,
            extracted_text: Some("Tokio runtime docs".to_string()),
            search_query: None,
            referrer: None,
            content_hash: None,
        })),
        probe_tag: None,
    };

    hippo_daemon::commands::send_event_fire_and_forget(
        &socket_path,
        &real_envelope,
        config.daemon.socket_timeout_ms,
    )
    .await
    .expect("real browser send should succeed");

    tokio::time::sleep(std::time::Duration::from_millis(400)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();

    // Both rows land in browser_events.
    let total: i64 = conn
        .query_row("SELECT COUNT(*) FROM browser_events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(total, 2, "expected 2 browser_events rows total");

    // Probe row carries probe_tag; real row does not.
    let probe_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM browser_events WHERE probe_tag IS NOT NULL",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(probe_count, 1, "exactly one browser probe row");

    let probe_url: String = conn
        .query_row(
            "SELECT url FROM browser_events WHERE probe_tag IS NOT NULL",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(probe_url, format!("https://{probe_domain}/probe"));

    // Layer 1: only the real browser event has a queue row.
    let queued: i64 = conn
        .query_row("SELECT COUNT(*) FROM browser_enrichment_queue", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(
        queued, 1,
        "probe browser events must not be queued for enrichment"
    );

    let queued_domain: String = conn
        .query_row(
            r#"SELECT be.domain FROM browser_enrichment_queue beq
               JOIN browser_events be ON be.id = beq.browser_event_id"#,
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        queued_domain, "docs.rs",
        "real browser event should be queued"
    );

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}
