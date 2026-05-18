use hippo_core::storage::open_db;
use tempfile::TempDir;

#[test]
fn upsert_writes_claude_session_and_enqueues() {
    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("hippo.db");
    let conn = open_db(&db_path).unwrap();

    let seg = hippo_daemon::codex_session::CodexSegment {
        session_id: "codex-1".into(),
        project_dir: "proj".into(),
        cwd: "/work/proj".into(),
        segment_index: 0,
        start_time: 1_775_634_000_000,
        end_time: 1_775_634_500_000,
        user_prompts: vec!["do a thing".into()],
        assistant_texts: vec![],
        tool_calls: vec![],
        message_count: 1,
        source_file: "/Users/x/.codex/sessions/2026/04/04/rollout-codex-1.jsonl".into(),
    };
    hippo_daemon::codex_session::upsert_segment(&conn, &seg).unwrap();

    let (cnt, src): (i64, String) = conn
        .query_row(
            "SELECT COUNT(*), MAX(source_file) FROM claude_sessions WHERE session_id = 'codex-1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(cnt, 1);
    assert!(src.contains("/.codex/"));

    let queued: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_enrichment_queue q
             JOIN claude_sessions s ON s.id = q.claude_session_id
             WHERE s.session_id = 'codex-1' AND q.status = 'pending'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(queued, 1);

    // Idempotent re-upsert: no duplicate row.
    hippo_daemon::codex_session::upsert_segment(&conn, &seg).unwrap();
    let cnt2: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_sessions WHERE session_id = 'codex-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(cnt2, 1, "re-upsert must not duplicate");
}
