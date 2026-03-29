use std::path::PathBuf;

use tempfile::tempdir;

use hippo_core::config::HippoConfig;
use hippo_core::protocol::{DaemonRequest, DaemonResponse};

fn test_config() -> HippoConfig {
    let temp = tempdir().unwrap();
    let mut config = HippoConfig::default();
    config.storage.data_dir = temp.path().join("data");
    config.storage.config_dir = temp.path().join("config");
    // Short flush interval so events land in the DB quickly
    config.daemon.flush_interval_ms = 100;
    std::mem::forget(temp);
    config
}

async fn wait_for_daemon(socket_path: &std::path::Path) {
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(2);
    loop {
        if let Ok(DaemonResponse::Status(_)) =
            hippo_daemon::commands::send_request(socket_path, &DaemonRequest::GetStatus).await
        {
            return;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "daemon never became ready"
        );
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    }
}

fn write_test_session_jsonl(dir: &std::path::Path) -> PathBuf {
    let path = dir.join("test-session.jsonl");
    let content = r#"{"type":"assistant","timestamp":"2026-03-28T10:00:00.000Z","sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","cwd":"/projects/hippo","gitBranch":"main","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_test001","name":"Bash","input":{"command":"cargo test"}}]}}
{"type":"user","timestamp":"2026-03-28T10:00:02.500Z","sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","cwd":"/projects/hippo","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_test001","content":"test result: ok. 10 passed"}]}}
{"type":"assistant","timestamp":"2026-03-28T10:00:03.000Z","sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","cwd":"/projects/hippo","gitBranch":"main","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_test002","name":"Read","input":{"file_path":"/projects/hippo/src/main.rs"}},{"type":"tool_use","id":"toolu_test003","name":"Edit","input":{"file_path":"/projects/hippo/src/lib.rs"}}]}}
{"type":"user","timestamp":"2026-03-28T10:00:03.500Z","sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","cwd":"/projects/hippo","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_test002","content":"fn main() {}"},{"type":"tool_result","tool_use_id":"toolu_test003","content":"edited"}]}}
{"type":"assistant","timestamp":"2026-03-28T10:00:04.000Z","sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","cwd":"/projects/hippo","gitBranch":"main","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_test004","name":"Bash","input":{"command":"cargo clippy"}}]}}
{"type":"user","timestamp":"2026-03-28T10:00:06.000Z","sessionId":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","cwd":"/projects/hippo","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_test004","is_error":true,"content":"error: unused variable"}]}}
"#;
    std::fs::write(&path, content).unwrap();
    path
}

#[tokio::test]
async fn test_ingest_batch_full_pipeline() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();
    let jsonl_dir = config.storage.data_dir.parent().unwrap().join("jsonl");
    std::fs::create_dir_all(&jsonl_dir).unwrap();
    let jsonl_path = write_test_session_jsonl(&jsonl_dir);

    // Start the daemon
    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    // Run batch import
    let (sent, errors) = hippo_daemon::claude_session::ingest_batch(
        &jsonl_path,
        &socket_path,
        config.daemon.socket_timeout_ms,
    )
    .await
    .unwrap();

    assert_eq!(
        sent, 4,
        "should send 4 events (1 bash + 2 parallel + 1 error bash)"
    );
    assert_eq!(errors, 0);

    // Wait for flush (100ms interval + margin)
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;

    // Query the DB directly to verify events landed
    let conn = hippo_core::storage::open_db(&db_path).unwrap();

    let event_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(event_count, 4, "all 4 events should be in the DB");

    // Verify shell is set to claude-code
    let shells: Vec<String> = {
        let mut stmt = conn.prepare("SELECT DISTINCT shell FROM events").unwrap();
        stmt.query_map([], |row| row.get(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert_eq!(shells.len(), 1);
    assert!(
        shells[0].contains("claude-code"),
        "shell should be claude-code variant, got: {}",
        shells[0]
    );

    // Verify commands are human-readable
    let commands: Vec<String> = {
        let mut stmt = conn
            .prepare("SELECT command FROM events ORDER BY timestamp ASC, id ASC")
            .unwrap();
        stmt.query_map([], |row| row.get(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert_eq!(commands[0], "cargo test");
    // The two parallel tool calls have the same timestamp — order may vary
    assert!(
        commands[1..3].contains(&"read /projects/hippo/src/main.rs".to_string()),
        "expected read command in parallel pair, got: {:?}",
        &commands[1..3]
    );
    assert!(
        commands[1..3].contains(&"edit /projects/hippo/src/lib.rs".to_string()),
        "expected edit command in parallel pair, got: {:?}",
        &commands[1..3]
    );
    assert_eq!(commands[3], "cargo clippy");

    // Verify the error event has exit_code 1
    let error_exit: i32 = conn
        .query_row(
            "SELECT exit_code FROM events WHERE command = 'cargo clippy'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(error_exit, 1);

    // Verify the successful event has exit_code 0
    let ok_exit: i32 = conn
        .query_row(
            "SELECT exit_code FROM events WHERE command = 'cargo test'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(ok_exit, 0);

    // Verify duration was computed from timestamps
    let duration: i64 = conn
        .query_row(
            "SELECT duration_ms FROM events WHERE command = 'cargo test'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        duration, 2500,
        "duration should be 2500ms (10:00:02.5 - 10:00:00)"
    );

    // Verify output was captured
    let stdout: String = conn
        .query_row(
            "SELECT stdout FROM events WHERE command = 'cargo test'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(stdout.contains("10 passed"));

    // Verify envelope_id was stored
    let envelope_ids: Vec<Option<String>> = {
        let mut stmt = conn
            .prepare("SELECT envelope_id FROM events ORDER BY timestamp ASC")
            .unwrap();
        stmt.query_map([], |row| row.get(0))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert!(
        envelope_ids.iter().all(|e| e.is_some()),
        "all events should have envelope_id set"
    );

    // Verify all events were queued for enrichment
    let queue_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM enrichment_queue", [], |r| r.get(0))
        .unwrap();
    assert_eq!(queue_count, 4, "all events should be in enrichment queue");

    // Verify session was created with claude-code session ID
    let session_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM sessions", [], |r| r.get(0))
        .unwrap();
    assert_eq!(session_count, 1, "all events share one session");

    // Shut down the daemon
    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}

#[tokio::test]
async fn test_ingest_batch_dedup_on_reimport() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();
    let jsonl_dir = config.storage.data_dir.parent().unwrap().join("jsonl");
    std::fs::create_dir_all(&jsonl_dir).unwrap();
    let jsonl_path = write_test_session_jsonl(&jsonl_dir);

    // Start the daemon
    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    // Import once
    let (sent1, _) = hippo_daemon::claude_session::ingest_batch(
        &jsonl_path,
        &socket_path,
        config.daemon.socket_timeout_ms,
    )
    .await
    .unwrap();
    assert_eq!(sent1, 4);

    // Wait for flush
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;

    // Import the same file again
    let (sent2, _) = hippo_daemon::claude_session::ingest_batch(
        &jsonl_path,
        &socket_path,
        config.daemon.socket_timeout_ms,
    )
    .await
    .unwrap();
    assert_eq!(
        sent2, 4,
        "sender doesn't know about dedup, sends all 4 again"
    );

    // Wait for flush
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;

    // Verify only 4 events in DB (not 8) due to envelope_id UNIQUE constraint
    let conn = hippo_core::storage::open_db(&db_path).unwrap();
    let event_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(
        event_count, 4,
        "dedup should prevent duplicate events on reimport"
    );

    // Shut down
    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}
