use hippo_core::storage::open_db;
use tempfile::TempDir;

fn seg(
    session_id: &str,
    is_subagent: bool,
    parent: Option<&str>,
) -> hippo_daemon::cursor_session::CursorSegment {
    hippo_daemon::cursor_session::CursorSegment {
        session_id: session_id.into(),
        project_dir: "foo".into(),
        cwd: "/work/foo".into(),
        segment_index: 0,
        start_time: 1_775_634_000_000,
        end_time: 1_775_634_000_000,
        user_prompts: vec!["do a thing".into()],
        assistant_texts: vec![],
        tool_calls: vec![],
        message_count: 1,
        source_file: format!(
            "/Users/x/.cursor/projects/Users-x-projects-foo/agent-transcripts/{session_id}/{session_id}.jsonl"
        ),
        is_subagent,
        parent_session_id: parent.map(|s| s.to_string()),
    }
}

#[test]
fn upsert_writes_claude_session_and_enqueues() {
    let tmp = TempDir::new().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();
    let s = seg("cur-1", false, None);
    hippo_daemon::cursor_session::upsert_segment(&conn, &s).unwrap();

    let (cnt, src): (i64, String) = conn
        .query_row(
            "SELECT COUNT(*), MAX(source_file) FROM claude_sessions WHERE session_id = 'cur-1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(cnt, 1);
    assert!(src.contains("/.cursor/"));

    let queued: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_enrichment_queue q
             JOIN claude_sessions s ON s.id = q.claude_session_id
             WHERE s.session_id = 'cur-1' AND q.status = 'pending'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(queued, 1);

    hippo_daemon::cursor_session::upsert_segment(&conn, &s).unwrap();
    let cnt2: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_sessions WHERE session_id = 'cur-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(cnt2, 1, "re-upsert must not duplicate");
}

#[test]
fn upsert_subagent_records_parent_link() {
    let tmp = TempDir::new().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();
    let s = seg("sub-1", true, Some("parent-1"));
    hippo_daemon::cursor_session::upsert_segment(&conn, &s).unwrap();

    let (is_sub, parent): (i64, Option<String>) = conn
        .query_row(
            "SELECT is_subagent, parent_session_id FROM claude_sessions WHERE session_id = 'sub-1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(is_sub, 1);
    assert_eq!(parent.as_deref(), Some("parent-1"));
}
