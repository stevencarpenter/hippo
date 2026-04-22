use std::path::PathBuf;

use hippo_core::protocol::DaemonRequest;

mod common;

use common::{test_config, wait_for_daemon};

const FIXTURE_SESSION_ID: &str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

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
        &db_path,
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
        &db_path,
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
        &db_path,
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

/// Regression test for #58: `hippo ingest claude-session --batch` must
/// populate the `claude_sessions` table — not just `events`. Prior to the
/// fix, the batch importer only fired tool-call events through the daemon
/// socket and never wrote the session-segment rows that enrichment depends
/// on, so the 272-session sev1 backfill reported success while landing
/// zero rows.
///
/// This test writes a fixture session JSONL (with user prompts, assistant
/// text, and 2 completed tool_use/tool_result pairs), ingests it with
/// `ingest_batch`, and asserts both that:
///
/// 1. ≥1 row exists in `claude_sessions` with the fixture session_id.
/// 2. The existing `source_kind='claude-tool'` event flow still fires.
///
/// If the segment-write half regresses, this test fails with 0 rows in
/// `claude_sessions`.
#[tokio::test]
async fn test_ingest_batch_writes_claude_sessions_row() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    // Lay out the JSONL under a simulated `projects/<project>/<session>.jsonl`
    // path so `SessionFile::from_path` can derive the project_dir from the
    // parent directory name — same shape Claude Code uses in `~/.claude`.
    let project_root = config
        .storage
        .data_dir
        .parent()
        .unwrap()
        .join("projects")
        .join("-home-user-projects-hippo");
    std::fs::create_dir_all(&project_root).unwrap();
    let jsonl_path = project_root.join(format!("{}.jsonl", FIXTURE_SESSION_ID));

    // Complete session: a user prompt, an assistant message with reasoning
    // plus a tool_use, a tool_result, a second assistant+tool_use, and a
    // second tool_result. Matches the entry shapes parsed by
    // `extract_segments` and `process_line`.
    let content = format!(
        r#"{{"type":"user","timestamp":"2026-03-28T10:00:00.000Z","sessionId":"{sid}","cwd":"/projects/hippo","message":{{"role":"user","content":[{{"type":"text","text":"please run the test suite and show me any failures"}}]}}}}
{{"type":"assistant","timestamp":"2026-03-28T10:00:01.000Z","sessionId":"{sid}","cwd":"/projects/hippo","gitBranch":"main","message":{{"role":"assistant","content":[{{"type":"text","text":"I'll run the tests now to see the current state of the suite."}},{{"type":"tool_use","id":"toolu_r1","name":"Bash","input":{{"command":"cargo test -p hippo-core"}}}}]}}}}
{{"type":"user","timestamp":"2026-03-28T10:00:03.000Z","sessionId":"{sid}","cwd":"/projects/hippo","message":{{"role":"user","content":[{{"type":"tool_result","tool_use_id":"toolu_r1","content":"running 12 tests\ntest result: ok. 12 passed"}}]}}}}
{{"type":"assistant","timestamp":"2026-03-28T10:00:04.000Z","sessionId":"{sid}","cwd":"/projects/hippo","gitBranch":"main","message":{{"role":"assistant","content":[{{"type":"text","text":"All 12 tests passed. I'll also verify the daemon crate compiles cleanly."}},{{"type":"tool_use","id":"toolu_r2","name":"Bash","input":{{"command":"cargo build -p hippo-daemon"}}}}]}}}}
{{"type":"user","timestamp":"2026-03-28T10:00:07.000Z","sessionId":"{sid}","cwd":"/projects/hippo","message":{{"role":"user","content":[{{"type":"tool_result","tool_use_id":"toolu_r2","content":"Compiling hippo-daemon\n    Finished"}}]}}}}
"#,
        sid = FIXTURE_SESSION_ID,
    );
    std::fs::write(&jsonl_path, content).unwrap();

    // Start the daemon.
    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    // Run the batch ingest.
    let (sent, errors) = hippo_daemon::claude_session::ingest_batch(
        &jsonl_path,
        &socket_path,
        config.daemon.socket_timeout_ms,
        &db_path,
    )
    .await
    .unwrap();
    assert_eq!(errors, 0, "batch should report zero errors");
    assert_eq!(
        sent, 2,
        "should fire two shell-shaped tool-call events (one per tool_use/result pair)"
    );

    // Wait for the flush interval so the daemon can drain the event buffer
    // into the `events` table (flush_interval_ms is set to 100 in the test
    // harness).
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();

    // Primary assertion: the fix must write at least one row into
    // `claude_sessions` for the fixture session_id.
    let segment_rows: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_sessions WHERE session_id = ?1",
            [FIXTURE_SESSION_ID],
            |row| row.get(0),
        )
        .unwrap();
    assert!(
        segment_rows >= 1,
        "expected ≥1 claude_sessions row for fixture session, got {}",
        segment_rows
    );

    // Confirm the row has meaningful payload (we're writing real data, not
    // a stub), so the enrichment queue actually has something to process.
    let (message_count, tool_calls_json, project_dir): (i64, String, String) = conn
        .query_row(
            "SELECT message_count, tool_calls_json, project_dir FROM claude_sessions
             WHERE session_id = ?1 ORDER BY segment_index ASC LIMIT 1",
            [FIXTURE_SESSION_ID],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert!(
        message_count > 0,
        "segment should have a positive message_count, got {}",
        message_count
    );
    assert!(
        tool_calls_json.contains("cargo test") || tool_calls_json.contains("cargo build"),
        "tool_calls_json should capture at least one tool invocation, got: {}",
        tool_calls_json
    );
    assert_eq!(
        project_dir, "-home-user-projects-hippo",
        "project_dir should be derived from the parent directory name"
    );

    // Enrichment queue should have gotten an entry per segment, or the
    // brain will never pick it up.
    let queue_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_enrichment_queue ceq
             JOIN claude_sessions cs ON ceq.claude_session_id = cs.id
             WHERE cs.session_id = ?1",
            [FIXTURE_SESSION_ID],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(
        queue_count, segment_rows,
        "every claude_sessions row must have a matching enrichment queue entry"
    );

    // Secondary assertion: we didn't break the original tool-call event
    // flow. Events should still land in the `events` table as
    // `source_kind='claude-tool'`.
    let tool_event_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE source_kind = 'claude-tool'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert!(
        tool_event_count > 0,
        "existing source_kind='claude-tool' event flow should still fire, got {}",
        tool_event_count
    );

    // Shut down.
    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}

/// Re-ingesting the same session file must be idempotent — the
/// `UNIQUE (session_id, segment_index)` constraint plus `INSERT OR IGNORE`
/// in the segment writer should leave the table unchanged on the second
/// pass. Guards against a future refactor that swaps `INSERT OR IGNORE`
/// for a plain `INSERT` and starts erroring (or, worse, double-writing).
#[tokio::test]
async fn test_ingest_batch_claude_sessions_is_idempotent() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    let project_root = config
        .storage
        .data_dir
        .parent()
        .unwrap()
        .join("projects")
        .join("-home-user-projects-hippo");
    std::fs::create_dir_all(&project_root).unwrap();
    let jsonl_path = project_root.join(format!("{}.jsonl", FIXTURE_SESSION_ID));

    let content = format!(
        r#"{{"type":"user","timestamp":"2026-03-28T10:00:00.000Z","sessionId":"{sid}","cwd":"/p","message":{{"role":"user","content":[{{"type":"text","text":"run the build"}}]}}}}
{{"type":"assistant","timestamp":"2026-03-28T10:00:01.000Z","sessionId":"{sid}","cwd":"/p","gitBranch":"main","message":{{"role":"assistant","content":[{{"type":"text","text":"I'll start the build now and watch for any errors."}},{{"type":"tool_use","id":"toolu_x1","name":"Bash","input":{{"command":"cargo build"}}}}]}}}}
{{"type":"user","timestamp":"2026-03-28T10:00:03.000Z","sessionId":"{sid}","cwd":"/p","message":{{"role":"user","content":[{{"type":"tool_result","tool_use_id":"toolu_x1","content":"ok"}}]}}}}
"#,
        sid = FIXTURE_SESSION_ID,
    );
    std::fs::write(&jsonl_path, content).unwrap();

    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    for _ in 0..2 {
        hippo_daemon::claude_session::ingest_batch(
            &jsonl_path,
            &socket_path,
            config.daemon.socket_timeout_ms,
            &db_path,
        )
        .await
        .unwrap();
    }

    tokio::time::sleep(std::time::Duration::from_millis(300)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();
    let segment_rows: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_sessions WHERE session_id = ?1",
            [FIXTURE_SESSION_ID],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(
        segment_rows, 1,
        "re-ingesting should be idempotent — expected exactly 1 row, got {}",
        segment_rows
    );

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}
