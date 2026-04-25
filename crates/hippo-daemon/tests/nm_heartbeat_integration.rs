//! Integration test for the browser extension heartbeat path (T-3).
//!
//! Verifies the end-to-end contract:
//!   extension → NM host → `DaemonRequest::UpdateSourceHealthHeartbeat` → daemon socket
//!   → `source_health.browser.last_heartbeat_ts` UPSERT in SQLite.
//!
//! The NM host subprocess is not spawned here (that would require a real Firefox
//! process).  Instead, the test sends `UpdateSourceHealthHeartbeat` directly via
//! `send_request`, which is exactly what `native_messaging::run()` does.  This
//! isolates the daemon handler from the stdio framing of the NM host.

mod common;

use common::{test_config, wait_for_daemon};
use hippo_core::protocol::{DaemonRequest, DaemonResponse};

/// Sending `UpdateSourceHealthHeartbeat` updates `source_health.browser.last_heartbeat_ts`.
///
/// Named to match the success criterion filter:
///   `cargo test -p hippo-daemon -- source_health_heartbeat`
#[tokio::test]
async fn source_health_heartbeat_updated_on_nm_request() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    let ts = chrono::Utc::now().timestamp_millis();

    let resp = hippo_daemon::commands::send_request(
        &socket_path,
        &DaemonRequest::UpdateSourceHealthHeartbeat {
            source: "browser".to_string(),
            ts,
        },
    )
    .await
    .expect("send_request failed");

    assert!(
        matches!(resp, DaemonResponse::Ack),
        "expected Ack, got {resp:?}"
    );

    // Wait briefly to allow the write to commit (it's synchronous in the handler
    // but give the async runtime a tick to settle).
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();
    let saved_ts: Option<i64> = conn
        .query_row(
            "SELECT last_heartbeat_ts FROM source_health WHERE source = 'browser'",
            [],
            |row| row.get(0),
        )
        .expect("source_health.browser row not found");

    assert_eq!(
        saved_ts,
        Some(ts),
        "last_heartbeat_ts should equal the ts we sent"
    );

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}

/// A second heartbeat overwrites the first (monotonic update).
#[tokio::test]
async fn source_health_heartbeat_second_call_overwrites_first() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    let ts1 = chrono::Utc::now().timestamp_millis() - 60_000; // 1 minute ago
    let ts2 = chrono::Utc::now().timestamp_millis();

    for ts in [ts1, ts2] {
        let resp = hippo_daemon::commands::send_request(
            &socket_path,
            &DaemonRequest::UpdateSourceHealthHeartbeat {
                source: "browser".to_string(),
                ts,
            },
        )
        .await
        .unwrap();
        assert!(matches!(resp, DaemonResponse::Ack));
    }

    tokio::time::sleep(std::time::Duration::from_millis(50)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();
    let saved_ts: Option<i64> = conn
        .query_row(
            "SELECT last_heartbeat_ts FROM source_health WHERE source = 'browser'",
            [],
            |row| row.get(0),
        )
        .unwrap();

    assert_eq!(
        saved_ts,
        Some(ts2),
        "last_heartbeat_ts should hold the most recent ts"
    );

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}

/// Heartbeat response arrives within 500 ms (DoD timing requirement).
#[tokio::test]
async fn source_health_heartbeat_responds_within_500ms() {
    let config = test_config();
    let socket_path = config.socket_path();

    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    let ts = chrono::Utc::now().timestamp_millis();
    let start = std::time::Instant::now();

    let resp = hippo_daemon::commands::send_request(
        &socket_path,
        &DaemonRequest::UpdateSourceHealthHeartbeat {
            source: "browser".to_string(),
            ts,
        },
    )
    .await
    .unwrap();

    let elapsed = start.elapsed();
    assert!(matches!(resp, DaemonResponse::Ack));
    assert!(
        elapsed < std::time::Duration::from_millis(500),
        "heartbeat round-trip took {elapsed:?} (> 500ms)"
    );

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}
