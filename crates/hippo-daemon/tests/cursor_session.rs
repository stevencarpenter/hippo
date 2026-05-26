use hippo_core::storage::open_db;
use tempfile::TempDir;

fn write_transcript(
    root: &std::path::Path,
    slug: &str,
    uuid: &str,
    prompt: &str,
) -> std::path::PathBuf {
    let dir = root.join(slug).join("agent-transcripts").join(uuid);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join(format!("{uuid}.jsonl"));
    let line = format!(
        r#"{{"role":"user","message":{{"content":[{{"type":"text","text":"<user_query>\n{prompt}\n</user_query>"}}]}}}}"#
    );
    std::fs::write(&p, line).unwrap();
    p
}

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

#[test]
fn poll_tick_ingests_idle_files_and_advances_cursor() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join("projects");
    let f = write_transcript(&roots, "Users-x-projects-foo", "sess-1", "hello cursor");
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(3600);
    filetime::set_file_mtime(&f, filetime::FileTime::from_system_time(old)).unwrap();

    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::cursor_session::test_config(&data_dir, std::slice::from_ref(&roots));
    let _ = open_db(&config.db_path()).unwrap();

    assert_eq!(hippo_daemon::cursor_session::poll_tick(&config).unwrap(), 1);
    assert_eq!(
        hippo_daemon::cursor_session::poll_tick(&config).unwrap(),
        0,
        "unchanged file must be skipped via cursor"
    );

    let conn = open_db(&config.db_path()).unwrap();
    let (last_success_ts, last_event_ts): (i64, Option<i64>) = conn
        .query_row(
            "SELECT last_success_ts, last_event_ts FROM source_health WHERE source = 'agentic-session-cursor'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert!(
        last_success_ts > 0,
        "source_health.last_success_ts must be bumped"
    );
    assert!(
        last_event_ts.is_some() && last_event_ts.unwrap() > 0,
        "source_health.last_event_ts must be set (got {:?}); regression guard — \
         poll_tick must write last_event_ts, not only last_success_ts",
        last_event_ts,
    );
}

#[test]
fn poll_tick_skips_in_flight_files() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join("projects");
    write_transcript(&roots, "Users-x-projects-foo", "fresh", "in flight"); // mtime = now
    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::cursor_session::test_config(&data_dir, &[roots]);
    let _ = open_db(&config.db_path()).unwrap();
    assert_eq!(hippo_daemon::cursor_session::poll_tick(&config).unwrap(), 0);
}

#[test]
fn poll_tick_returns_zero_when_disabled() {
    let tmp = TempDir::new().unwrap();
    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let mut config = hippo_daemon::cursor_session::test_config(&data_dir, &[]);
    config.cursor.enabled = false;
    assert_eq!(hippo_daemon::cursor_session::poll_tick(&config).unwrap(), 0);
}

#[test]
fn poll_tick_zero_segment_file_advances_cursor_without_health_bump() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join("projects");
    // An assistant-only transcript: extract_segments never opens a segment
    // (a segment is only created on a user turn), so it yields zero segments.
    let dir = roots
        .join("Users-x-projects-foo")
        .join("agent-transcripts")
        .join("emptyseg");
    std::fs::create_dir_all(&dir).unwrap();
    let f = dir.join("emptyseg.jsonl");
    std::fs::write(
        &f,
        r#"{"role":"assistant","message":{"content":[{"type":"text","text":"no user turn here"}]}}"#,
    )
    .unwrap();
    // Backdate mtime so the file is past min_idle_secs ("idle").
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(3600);
    filetime::set_file_mtime(&f, filetime::FileTime::from_system_time(old)).unwrap();

    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::cursor_session::test_config(&data_dir, std::slice::from_ref(&roots));
    let _ = open_db(&config.db_path()).unwrap();

    let n = hippo_daemon::cursor_session::poll_tick(&config).unwrap();
    assert_eq!(n, 0, "a zero-segment file ingests nothing");

    let conn = open_db(&config.db_path()).unwrap();
    // last_event_ts must stay NULL: a zero-segment file captured no real data,
    // so the health bump must be withheld.
    let last_event_ts: Option<i64> = conn
        .query_row(
            "SELECT last_event_ts FROM source_health WHERE source = 'agentic-session-cursor'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        last_event_ts, None,
        "zero-segment file must NOT bump source_health.last_event_ts"
    );

    // The cursor still advanced: a second tick re-finds the same idle file but
    // skips it via the cursor instead of re-parsing it.
    let n2 = hippo_daemon::cursor_session::poll_tick(&config).unwrap();
    assert_eq!(
        n2, 0,
        "cursor must advance so the empty file is not re-parsed"
    );
}
