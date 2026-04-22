//! Source #8 — Xcode Coding Assistant (ClaudeAgentConfig) sessions.
//!
//! Production path in deployment: the LaunchAgent
//! `com.hippo.xcode-claude-ingest.plist` runs
//! `scripts/hippo-ingest-claude.py --claude-dir
//! ~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig/projects`
//! every 5 min — so Python is the canonical ingester. But the JSONL
//! schema is identical to `~/.claude/projects/` modulo a few envelope
//! types (`queue-operation`) that the Rust `process_line` silently
//! skips. If a user ever runs `hippo ingest claude-session --batch`
//! against an Xcode JSONL, the Rust path MUST produce the same
//! `claude_sessions` and `claude-tool` events as it would for a regular
//! Claude Code JSONL.
//!
//! This test feeds an Xcode-shaped JSONL (including the queue-operation
//! rows that real Xcode emits at the start of every file) through
//! `ingest_batch` and asserts the rows land. If Xcode introduces a
//! schema divergence that breaks the shared path, this test surfaces
//! it.

use hippo_core::protocol::DaemonRequest;

use crate::common::{test_config, wait_for_daemon};

const FIXTURE_SESSION_ID: &str = "84f9c094-d29b-48d9-9fa3-36e093a735e2";

fn write_xcode_jsonl(root: &std::path::Path) -> std::path::PathBuf {
    // Matches the real Xcode path shape: the project-dir encoding uses
    // --swiftpm-xcode as a suffix (not a double dash — Xcode encodes /./
    // as --).
    let project = root
        .join("projects")
        .join("-Users-carpenter-projects-hippo-hippo-gui--swiftpm-xcode");
    std::fs::create_dir_all(&project).unwrap();
    let path = project.join(format!("{FIXTURE_SESSION_ID}.jsonl"));

    // First two lines are Xcode-specific queue-operation envelopes that
    // `process_line` must silently skip (msg_type != "assistant"|"user").
    // The rest is a stock Claude Code conversation with one completed
    // tool_use/tool_result pair.
    let content = format!(
        r#"{{"type":"queue-operation","operation":"enqueue","timestamp":"2026-04-22T13:00:00.000Z","sessionId":"{sid}"}}
{{"type":"queue-operation","operation":"dequeue","timestamp":"2026-04-22T13:00:00.001Z","sessionId":"{sid}"}}
{{"type":"user","timestamp":"2026-04-22T13:00:01.000Z","sessionId":"{sid}","cwd":"/projects/hippo/hippo-gui","message":{{"role":"user","content":[{{"type":"text","text":"fix the swiftlint warning"}}]}}}}
{{"type":"assistant","timestamp":"2026-04-22T13:00:02.000Z","sessionId":"{sid}","cwd":"/projects/hippo/hippo-gui","gitBranch":"main","message":{{"role":"assistant","content":[{{"type":"text","text":"reading the file first"}},{{"type":"tool_use","id":"toolu_xc_1","name":"Read","input":{{"file_path":"/projects/hippo/hippo-gui/Sources/DaemonSocketClient.swift"}}}}]}}}}
{{"type":"user","timestamp":"2026-04-22T13:00:03.500Z","sessionId":"{sid}","cwd":"/projects/hippo/hippo-gui","message":{{"role":"user","content":[{{"type":"tool_result","tool_use_id":"toolu_xc_1","content":"class DaemonSocketClient {{ ... }}"}}]}}}}
"#,
        sid = FIXTURE_SESSION_ID,
    );
    std::fs::write(&path, content).unwrap();
    path
}

#[tokio::test]
async fn xcode_format_jsonl_flows_through_same_ingest_path() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();
    let jsonl_path = write_xcode_jsonl(config.storage.data_dir.parent().unwrap());

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
    .expect("ingest_batch must handle Xcode-format JSONL");
    // The queue-operation rows should be silently skipped — no errors.
    assert_eq!(errors, 0, "queue-operation rows must not produce errors");
    assert_eq!(sent, 1, "exactly one completed tool_use/result pair");

    tokio::time::sleep(std::time::Duration::from_millis(400)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();

    // claude_sessions row must land — same contract as a regular Claude
    // Code JSONL.
    let sessions: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_sessions WHERE session_id = ?1",
            [FIXTURE_SESSION_ID],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        sessions >= 1,
        "Xcode JSONL must produce ≥1 claude_sessions row, got {sessions}"
    );

    // tool events flow through too.
    let tool_events: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE source_kind = 'claude-tool' AND command LIKE 'read %'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        tool_events >= 1,
        "Xcode tool_use rows must produce claude-tool events"
    );

    // Enrichment queue wired up for the Xcode session row too.
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
        "Xcode segments must be queued for enrichment"
    );

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}
