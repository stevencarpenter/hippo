//! Source #2 — Claude-tool events (synthesized from Claude session tool_use
//! blocks).
//!
//! Production path: `claude_session::process_line` emits an `EventEnvelope`
//! with `ShellEvent.tool_name = Some(<tool>)`. The daemon's `flush_events`
//! sends it through `storage::insert_event_at`, which derives
//! `source_kind='claude-tool'` (see `storage.rs:494`) because `tool_name`
//! is populated.
//!
//! This test builds a minimal `ShellEvent` with `tool_name` set, sends it
//! over the socket, and asserts the row lands with `source_kind='claude-tool'`
//! and `tool_name` preserved.

use std::collections::HashMap;

use chrono::Utc;
use hippo_core::events::{CapturedOutput, EventEnvelope, EventPayload, ShellEvent, ShellKind};
use hippo_core::protocol::DaemonRequest;
use uuid::Uuid;

use crate::common::{test_config, wait_for_daemon};

#[tokio::test]
async fn claude_tool_event_lands_with_correct_source_kind() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    let event = ShellEvent {
        session_id: Uuid::new_v4(),
        command: "cargo test --workspace".to_string(),
        exit_code: 0,
        duration_ms: 2_500,
        cwd: "/projects/hippo".into(),
        hostname: "test-host".to_string(),
        shell: ShellKind::Unknown("claude-code".to_string()),
        stdout: Some(CapturedOutput {
            content: "test result: ok. 42 passed".to_string(),
            truncated: false,
            original_bytes: 27,
        }),
        stderr: None,
        env_snapshot: HashMap::new(),
        git_state: None,
        redaction_count: 0,
        tool_name: Some("Bash".to_string()),
    };

    let envelope = EventEnvelope {
        envelope_id: Uuid::new_v4(),
        producer_version: 1,
        timestamp: Utc::now(),
        payload: EventPayload::Shell(Box::new(event)),
        probe_tag: None,
    };

    hippo_daemon::commands::send_event_fire_and_forget(
        &socket_path,
        &envelope,
        config.daemon.socket_timeout_ms,
    )
    .await
    .expect("send_event_fire_and_forget should succeed");

    tokio::time::sleep(std::time::Duration::from_millis(400)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();

    let (count, tool_name, command): (i64, String, String) = conn
        .query_row(
            "SELECT COUNT(*), tool_name, command FROM events WHERE source_kind = 'claude-tool' GROUP BY tool_name, command",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(count, 1, "expected exactly one claude-tool event");
    assert_eq!(tool_name, "Bash");
    assert_eq!(command, "cargo test --workspace");

    // No shell events should have been produced.
    let shell_rows: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE source_kind = 'shell'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        shell_rows, 0,
        "a tool-call envelope must not land as source_kind='shell'"
    );

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}
