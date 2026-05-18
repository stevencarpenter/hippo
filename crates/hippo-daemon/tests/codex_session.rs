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

    let queued2: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM claude_enrichment_queue q
             JOIN claude_sessions s ON s.id = q.claude_session_id
             WHERE s.session_id = 'codex-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(queued2, 1, "re-upsert must not create a second queue row");
}

/// Regression guard: when the same `(session_id, segment_index)` is upserted
/// twice with a changed `cwd`, the row's `cwd` and `project_dir` must reflect
/// the new values (not be frozen at the original insert).
#[test]
fn upsert_refreshes_cwd_and_project_dir_on_conflict() {
    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("hippo.db");
    let conn = open_db(&db_path).unwrap();

    let mut seg = hippo_daemon::codex_session::CodexSegment {
        session_id: "cwd-test".into(),
        project_dir: "old-proj".into(),
        cwd: "/old/path".into(),
        segment_index: 0,
        start_time: 1_775_634_000_000,
        end_time: 1_775_634_500_000,
        user_prompts: vec!["initial prompt".into()],
        assistant_texts: vec![],
        tool_calls: vec![],
        message_count: 1,
        source_file: "/Users/x/.codex/sessions/rollout-cwd-test.jsonl".into(),
    };
    hippo_daemon::codex_session::upsert_segment(&conn, &seg).unwrap();

    // Re-upsert with updated cwd (Codex emits turn_context with new cwd).
    seg.cwd = "/new/path".into();
    seg.project_dir = "new-proj".into();
    hippo_daemon::codex_session::upsert_segment(&conn, &seg).unwrap();

    let (cwd, project_dir): (String, String) = conn
        .query_row(
            "SELECT cwd, project_dir FROM claude_sessions WHERE session_id = 'cwd-test'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(cwd, "/new/path", "cwd must be updated on re-upsert");
    assert_eq!(
        project_dir, "new-proj",
        "project_dir must be updated on re-upsert"
    );
}

fn write_rollout(dir: &std::path::Path, id: &str, prompt: &str) -> std::path::PathBuf {
    let p = dir.join(format!("rollout-{id}.jsonl"));
    let lines = [
        format!(
            r#"{{"timestamp":"2026-04-04T00:00:00.000Z","type":"session_meta","payload":{{"id":"{id}","cwd":"/proj"}}}}"#
        ),
        format!(
            r#"{{"timestamp":"2026-04-04T00:00:01.000Z","type":"event_msg","payload":{{"type":"user_message","message":"{prompt}"}}}}"#
        ),
    ];
    std::fs::write(&p, lines.join("\n")).unwrap();
    p
}

#[test]
fn poll_tick_ingests_idle_files_and_advances_cursor() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join("sessions");
    std::fs::create_dir_all(&roots).unwrap();
    let f = write_rollout(&roots, "p1", "hello");
    // Backdate mtime so the file is "idle".
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(3600);
    filetime::set_file_mtime(&f, filetime::FileTime::from_system_time(old)).unwrap();

    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::codex_session::test_config(&data_dir, std::slice::from_ref(&roots));
    let _ = open_db(&config.db_path()).unwrap();

    let n = hippo_daemon::codex_session::poll_tick(&config).unwrap();
    assert_eq!(n, 1, "one new segment ingested");

    // Second tick: file unchanged -> cursor skip, zero new.
    let n2 = hippo_daemon::codex_session::poll_tick(&config).unwrap();
    assert_eq!(n2, 0, "unchanged file must be skipped");

    let conn = open_db(&config.db_path()).unwrap();
    let health: i64 = conn
        .query_row(
            "SELECT last_success_ts FROM source_health WHERE source = 'agentic-session-codex'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(health > 0, "source_health must be bumped");
}

#[test]
fn poll_tick_returns_zero_when_disabled() {
    let tmp = TempDir::new().unwrap();
    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let mut config = hippo_daemon::codex_session::test_config(&data_dir, &[]);
    config.codex.enabled = false;
    assert_eq!(hippo_daemon::codex_session::poll_tick(&config).unwrap(), 0);
}

/// Write a rollout file containing only a `session_meta` line — no
/// user-message line, so `extract_segments` opens no segment and yields zero.
fn write_empty_rollout(dir: &std::path::Path, id: &str) -> std::path::PathBuf {
    let p = dir.join(format!("rollout-{id}.jsonl"));
    let line = format!(
        r#"{{"timestamp":"2026-04-04T00:00:00.000Z","type":"session_meta","payload":{{"id":"{id}","cwd":"/proj"}}}}"#
    );
    std::fs::write(&p, line).unwrap();
    p
}

#[test]
fn poll_tick_zero_segment_file_advances_cursor_without_health_bump() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join("sessions");
    std::fs::create_dir_all(&roots).unwrap();
    // session_meta-only file -> extract_segments yields zero segments.
    let f = write_empty_rollout(&roots, "emptyseg");
    // Backdate mtime so the file is past min_idle_secs ("idle").
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(3600);
    filetime::set_file_mtime(&f, filetime::FileTime::from_system_time(old)).unwrap();

    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::codex_session::test_config(&data_dir, std::slice::from_ref(&roots));
    let _ = open_db(&config.db_path()).unwrap();

    let n = hippo_daemon::codex_session::poll_tick(&config).unwrap();
    assert_eq!(n, 0, "a zero-segment file ingests nothing");

    let conn = open_db(&config.db_path()).unwrap();
    // last_event_ts must stay NULL: a zero-segment file captured no real data,
    // so the health bump must be withheld (regression guard for Task 7).
    let last_event_ts: Option<i64> = conn
        .query_row(
            "SELECT last_event_ts FROM source_health WHERE source = 'agentic-session-codex'",
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
    let n2 = hippo_daemon::codex_session::poll_tick(&config).unwrap();
    assert_eq!(
        n2, 0,
        "cursor must advance so the empty file is not re-parsed"
    );
}

#[test]
fn poll_tick_skips_in_flight_files() {
    let tmp = TempDir::new().unwrap();
    let roots = tmp.path().join("sessions");
    std::fs::create_dir_all(&roots).unwrap();
    write_rollout(&roots, "fresh", "in flight"); // mtime = now
    let data_dir = tmp.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let config = hippo_daemon::codex_session::test_config(&data_dir, &[roots]);
    let _ = open_db(&config.db_path()).unwrap();
    let n = hippo_daemon::codex_session::poll_tick(&config).unwrap();
    assert_eq!(n, 0, "files within min_idle_secs are skipped");
}
