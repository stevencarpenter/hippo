//! Source #9 — Cursor Agent CLI transcripts.
//!
//! Drives a real transcript through the production `poll_tick` path and
//! asserts a `claude_sessions` row lands and `source_health` is updated.

use hippo_core::storage::open_db;
use tempfile::TempDir;

#[test]
fn cursor_agent_transcript_lands_row_and_bumps_health() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join(".cursor").join("projects");
    let dir = roots
        .join("Users-x-projects-foo")
        .join("agent-transcripts")
        .join("sess-audit");
    std::fs::create_dir_all(&dir).unwrap();
    let f = dir.join("sess-audit.jsonl");
    std::fs::write(
        &f,
        r#"{"role":"user","message":{"content":[{"type":"text","text":"<user_query>\naudit\n</user_query>"}]}}"#,
    )
    .unwrap();
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(3600);
    filetime::set_file_mtime(&f, filetime::FileTime::from_system_time(old)).unwrap();

    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::cursor_session::test_config(&data_dir, std::slice::from_ref(&roots));
    let _ = open_db(&config.db_path()).unwrap();

    let n = hippo_daemon::cursor_session::poll_tick(&config).unwrap();
    assert_eq!(
        n, 1,
        "Cursor transcript must produce one agentic_sessions row"
    );

    let conn = open_db(&config.db_path()).unwrap();
    let rows: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM agentic_sessions
             WHERE session_id = 'sess-audit'
               AND harness = 'cursor'
               AND source_file LIKE '%/.cursor/%'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(rows, 1);

    // source_health must be updated — fulfils the docstring contract.
    let (last_event_ts, last_success_ts): (Option<i64>, Option<i64>) = conn
        .query_row(
            "SELECT last_event_ts, last_success_ts FROM source_health WHERE source = 'agentic-session-cursor'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert!(
        last_event_ts.is_some() && last_event_ts.unwrap() > 0,
        "source_health.last_event_ts must be set after poll_tick, got {:?}",
        last_event_ts,
    );
    assert!(
        last_success_ts.is_some() && last_success_ts.unwrap() > 0,
        "source_health.last_success_ts must be set after poll_tick, got {:?}",
        last_success_ts,
    );
}
