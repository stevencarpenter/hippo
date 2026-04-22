//! Source #5 — browser visits from the Firefox extension.
//!
//! Production path: `extension/firefox` → Native Messaging stdio → daemon's
//! `native_messaging::run` → builds a `BrowserEvent` envelope → same
//! `send_event_fire_and_forget` socket path → `flush_events` dispatches
//! `EventPayload::Browser(...)` to `storage::insert_browser_event`, which
//! writes the row + atomically enqueues enrichment in a transaction.
//!
//! The Native Messaging stdio layer is covered by the unit tests in
//! `native_messaging.rs` and by `nm_restart_integration.rs`. This audit
//! exercises the **daemon-side** write path: given a `BrowserEvent`
//! envelope arrives on the socket, the row must land in `browser_events`
//! and a matching queue row must land in `browser_enrichment_queue`.

use chrono::Utc;
use hippo_core::events::{BrowserEvent, EventEnvelope, EventPayload};
use hippo_core::protocol::DaemonRequest;
use uuid::Uuid;

use crate::common::{test_config, wait_for_daemon};

#[tokio::test]
async fn browser_envelope_lands_in_browser_events_and_queue() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    let visit = BrowserEvent {
        url: "https://docs.rs/tokio/latest/tokio/".to_string(),
        title: "tokio - Rust".to_string(),
        domain: "docs.rs".to_string(),
        dwell_ms: 17_500,
        scroll_depth: 0.62,
        extracted_text: Some("Tokio provides a runtime…".to_string()),
        search_query: None,
        referrer: Some("https://www.google.com/".to_string()),
        content_hash: None,
    };

    let envelope = EventEnvelope {
        envelope_id: Uuid::new_v4(),
        producer_version: 1,
        timestamp: Utc::now(),
        payload: EventPayload::Browser(Box::new(visit)),
    };

    hippo_daemon::commands::send_event_fire_and_forget(
        &socket_path,
        &envelope,
        config.daemon.socket_timeout_ms,
    )
    .await
    .expect("send_event_fire_and_forget should succeed for browser envelope");

    tokio::time::sleep(std::time::Duration::from_millis(400)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();

    let (count, url, domain, dwell): (i64, String, String, i64) = conn
        .query_row(
            "SELECT COUNT(*), url, domain, dwell_ms FROM browser_events GROUP BY url, domain, dwell_ms",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .unwrap();
    assert_eq!(count, 1, "expected exactly one browser_events row");
    assert_eq!(url, "https://docs.rs/tokio/latest/tokio/");
    assert_eq!(domain, "docs.rs");
    assert_eq!(dwell, 17_500);

    // insert_browser_event enqueues atomically — the queue row must exist.
    let queued: i64 = conn
        .query_row("SELECT COUNT(*) FROM browser_enrichment_queue", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(
        queued, 1,
        "browser_enrichment_queue must have a row for every browser_events row"
    );

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}
