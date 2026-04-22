//! Source #7 — Claude subagent sessions.
//!
//! Production path: Claude Code spawns subagent JSONLs at
//! `<projects-root>/<project-encoded>/<parent-uuid>/subagents/<id>.jsonl`.
//! `SessionFile::from_path` (crates/hippo-daemon/src/claude_session.rs:396)
//! detects the `subagents` path segment and sets `is_subagent=true` +
//! `parent_session_id=<parent uuid>`. Everything downstream of that is the
//! same write path as main sessions.
//!
//! This test lays out a subagent JSONL under the expected path, runs
//! `ingest_batch`, and asserts the `claude_sessions` row lands with
//! `is_subagent=1` and the parent UUID threaded through.

use hippo_core::protocol::DaemonRequest;

use crate::common::{test_config, wait_for_daemon};

const PARENT_SESSION_ID: &str = "aaaaaaaa-0000-0000-0000-000000000001";
const SUBAGENT_SESSION_ID: &str = "aaaaaaaa-0000-0000-0000-000000000002";

fn write_subagent_jsonl(root: &std::path::Path) -> std::path::PathBuf {
    // Full shape: projects/<project>/<parent-uuid>/subagents/<sub-uuid>.jsonl
    let subagent_dir = root
        .join("projects")
        .join("-projects-hippo")
        .join(PARENT_SESSION_ID)
        .join("subagents");
    std::fs::create_dir_all(&subagent_dir).unwrap();
    let path = subagent_dir.join(format!("{SUBAGENT_SESSION_ID}.jsonl"));

    let content = format!(
        r#"{{"type":"user","timestamp":"2026-04-22T12:00:00.000Z","sessionId":"{sid}","cwd":"/projects/hippo","message":{{"role":"user","content":[{{"type":"text","text":"subagent task"}}]}}}}
{{"type":"assistant","timestamp":"2026-04-22T12:00:01.000Z","sessionId":"{sid}","cwd":"/projects/hippo","gitBranch":"main","message":{{"role":"assistant","content":[{{"type":"text","text":"working"}},{{"type":"tool_use","id":"toolu_sub_1","name":"Bash","input":{{"command":"ls"}}}}]}}}}
{{"type":"user","timestamp":"2026-04-22T12:00:02.000Z","sessionId":"{sid}","cwd":"/projects/hippo","message":{{"role":"user","content":[{{"type":"tool_result","tool_use_id":"toolu_sub_1","content":"file1\nfile2"}}]}}}}
"#,
        sid = SUBAGENT_SESSION_ID,
    );
    std::fs::write(&path, content).unwrap();
    path
}

#[tokio::test]
async fn subagent_session_lands_with_is_subagent_flag() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();
    let jsonl_path = write_subagent_jsonl(config.storage.data_dir.parent().unwrap());

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
    .expect("ingest_batch should succeed on a subagent JSONL");
    assert_eq!(errors, 0);
    assert_eq!(sent, 1);

    tokio::time::sleep(std::time::Duration::from_millis(400)).await;

    let conn = hippo_core::storage::open_db(&db_path).unwrap();

    let (sessions, is_subagent, parent): (i64, i64, Option<String>) = conn
        .query_row(
            "SELECT COUNT(*), is_subagent, parent_session_id
             FROM claude_sessions
             WHERE session_id = ?1
             GROUP BY is_subagent, parent_session_id",
            [SUBAGENT_SESSION_ID],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert!(
        sessions >= 1,
        "subagent ingest must write ≥1 claude_sessions row, got {sessions}"
    );
    assert_eq!(is_subagent, 1, "is_subagent must be 1 for a subagent JSONL");
    assert_eq!(
        parent.as_deref(),
        Some(PARENT_SESSION_ID),
        "parent_session_id must come from the grandparent directory"
    );

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}
