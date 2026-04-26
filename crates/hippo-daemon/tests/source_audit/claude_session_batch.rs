//! Source #3 — Claude session segments via batch import.
//!
//! Production path: `hippo ingest claude-session <path>` →
//! `claude_session::ingest_batch` → `write_session_segments` → direct
//! SQLite `INSERT OR IGNORE INTO claude_sessions` (+ `claude_enrichment_queue`).
//!
//! This is an **audit** of the #59 fix (pre-fix the batch importer only
//! fired tool events and left `claude_sessions` empty). `tests/claude_session.rs`
//! has the behavioural contract; this test re-asserts the minimum thing —
//! ≥1 `claude_sessions` row + matching queue row — so the audit file is
//! self-contained: if someone deletes `claude_session.rs` this still screams.

use hippo_core::protocol::DaemonRequest;

use crate::common::{test_config, wait_for_daemon};

const FIXTURE_SESSION_ID: &str = "11111111-2222-3333-4444-555555555555";

fn write_jsonl(dir: &std::path::Path) -> std::path::PathBuf {
    // Lay out as `projects/<project-encoded>/<uuid>.jsonl` so
    // `SessionFile::from_path` can derive `project_dir` from the parent.
    let project = dir.join("projects").join("-projects-hippo");
    std::fs::create_dir_all(&project).unwrap();
    let path = project.join(format!("{FIXTURE_SESSION_ID}.jsonl"));

    let content = format!(
        r#"{{"type":"user","timestamp":"2026-04-22T09:00:00.000Z","sessionId":"{sid}","cwd":"/projects/hippo","message":{{"role":"user","content":[{{"type":"text","text":"audit batch path"}}]}}}}
{{"type":"assistant","timestamp":"2026-04-22T09:00:01.000Z","sessionId":"{sid}","cwd":"/projects/hippo","gitBranch":"main","message":{{"role":"assistant","content":[{{"type":"text","text":"running test"}},{{"type":"tool_use","id":"toolu_audit_b1","name":"Bash","input":{{"command":"cargo test"}}}}]}}}}
{{"type":"user","timestamp":"2026-04-22T09:00:02.500Z","sessionId":"{sid}","cwd":"/projects/hippo","message":{{"role":"user","content":[{{"type":"tool_result","tool_use_id":"toolu_audit_b1","content":"test result: ok. 3 passed"}}]}}}}
"#,
        sid = FIXTURE_SESSION_ID,
    );
    std::fs::write(&path, content).unwrap();
    path
}

#[tokio::test]
async fn batch_ingest_writes_both_events_and_claude_sessions() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();
    let jsonl_path = write_jsonl(config.storage.data_dir.parent().unwrap());

    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    let (sent, errors) = hippo_daemon::claude_session::ingest_batch(
        &jsonl_path,
        &socket_path,
        config.daemon.socket_timeout_ms,
        &db_path,
    )
    .await
    .expect("ingest_batch should succeed");

    assert_eq!(errors, 0, "batch ingest should report zero errors");
    assert_eq!(sent, 1, "fixture has exactly one completed tool_use pair");

    tokio::time::sleep(std::time::Duration::from_millis(400)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();

    // Claude sessions segment must exist (the #59 regression).
    let sessions: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_sessions WHERE session_id = ?1",
            [FIXTURE_SESSION_ID],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        sessions >= 1,
        "batch ingest must write ≥1 claude_sessions row, got {sessions}"
    );

    // Every segment must have a matching enrichment-queue entry.
    let queued: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_enrichment_queue ceq
             JOIN claude_sessions cs ON ceq.claude_session_id = cs.id
             WHERE cs.session_id = ?1",
            [FIXTURE_SESSION_ID],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        queued, sessions,
        "every claude_sessions row must be queued for enrichment"
    );

    // Original tool-event flow still fires.
    let tool_events: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE source_kind = 'claude-tool'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(tool_events >= 1, "claude-tool events must still land");

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}
