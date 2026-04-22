//! Source #1 — shell commands (zsh hook).
//!
//! Production path: `handle_send_event_shell` → `send_event_fire_and_forget`
//! over the Unix socket → daemon buffer → `flush_events` → `insert_event_at`
//! which derives `source_kind='shell'` because `tool_name` is `None`.
//!
//! The test bypasses the real zsh shim (covered by `shell_hook.rs`) and
//! drives the daemon-facing half of the chain directly.

use hippo_core::protocol::DaemonRequest;

use crate::common::{test_config, wait_for_daemon};

#[tokio::test]
async fn shell_event_lands_in_events_table_with_source_kind_shell() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    // Drive the production CLI handler directly. No real zsh involved —
    // but everything downstream of `handle_send_event_shell` is real.
    hippo_daemon::commands::handle_send_event_shell(
        &config,
        "cargo test -p hippo-core".to_string(),
        0,
        "/tmp/not-a-real-repo".to_string(),
        1_234,
        None,
        None,
        None,
        false,
        Some("test result: ok. 12 passed".to_string()),
    )
    .await
    .expect("handle_send_event_shell should succeed when daemon is up");

    // Flush interval is 100ms in test_config; 400ms gives plenty of margin.
    tokio::time::sleep(std::time::Duration::from_millis(400)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();

    let shell_rows: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE source_kind = 'shell'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        shell_rows, 1,
        "expected exactly one shell event in the `events` table"
    );

    // Sanity-check row shape: command preserved, tool_name NULL.
    let (command, tool_name): (String, Option<String>) = conn
        .query_row(
            "SELECT command, tool_name FROM events WHERE source_kind = 'shell' LIMIT 1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(command, "cargo test -p hippo-core");
    assert!(
        tool_name.is_none(),
        "shell events must have tool_name NULL (got {:?})",
        tool_name
    );

    // Enrichment queue should have picked up the row.
    let queued: i64 = conn
        .query_row("SELECT COUNT(*) FROM enrichment_queue", [], |r| r.get(0))
        .unwrap();
    assert_eq!(queued, 1, "shell events must be queued for enrichment");

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}
