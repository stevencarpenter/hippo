//! Source #11 — Opencode sessions polled out of opencode's SQLite DB.
//!
//! Production path: `com.hippo.opencode-poll` LaunchAgent fires
//! `hippo opencode-poll` every `[opencode] poll_interval_secs` →
//! `opencode_session::poll_tick` reads the opencode DB read-only → upserts
//! `agentic_sessions`, enqueues `agentic_enrichment_queue`, bumps
//! `source_health`, advances `agentic_cursor` — all in one transaction.
//!
//! These tests drive the poller end-to-end against a fabricated opencode
//! DB and assert every destination is updated correctly.

use rusqlite::{Connection, params};
use tempfile::TempDir;

use hippo_core::config::HippoConfig;

/// Build a minimal opencode `session` table matching what `read_new_sessions`
/// queries — same column list, no constraints we don't need.
fn init_opencode_db(path: &std::path::Path) -> Connection {
    let conn = Connection::open(path).unwrap();
    conn.execute_batch(
        "CREATE TABLE session (
            id              TEXT PRIMARY KEY,
            slug            TEXT NOT NULL DEFAULT '',
            title           TEXT NOT NULL DEFAULT '',
            directory       TEXT NOT NULL,
            parent_id       TEXT,
            agent           TEXT,
            model           TEXT,
            time_created    INTEGER NOT NULL,
            time_updated    INTEGER NOT NULL,
            summary_additions INTEGER,
            summary_deletions INTEGER,
            summary_files     INTEGER,
            summary_diffs     TEXT
        );
        CREATE TABLE message (
            id           TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            data         TEXT NOT NULL,
            time_created INTEGER NOT NULL
        );
        CREATE TABLE part (
            id           TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            message_id   TEXT NOT NULL,
            data         TEXT NOT NULL,
            time_created INTEGER NOT NULL
        );",
    )
    .unwrap();
    conn
}

#[allow(clippy::too_many_arguments)]
fn insert_session(
    conn: &Connection,
    id: &str,
    slug: &str,
    title: &str,
    directory: &str,
    agent: Option<&str>,
    model: Option<&str>,
    time_created: i64,
    time_updated: i64,
    diff_stats: Option<(i64, i64, i64)>,
) {
    let (adds, dels, files) =
        diff_stats.map_or((None, None, None), |(a, d, f)| (Some(a), Some(d), Some(f)));
    conn.execute(
        "INSERT INTO session
           (id, slug, title, directory, agent, model,
            time_created, time_updated,
            summary_additions, summary_deletions, summary_files, summary_diffs)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, NULL)",
        params![
            id,
            slug,
            title,
            directory,
            agent,
            model,
            time_created,
            time_updated,
            adds,
            dels,
            files,
        ],
    )
    .unwrap();
}

fn insert_message(
    conn: &Connection,
    id: &str,
    session_id: &str,
    role: &str,
    time_created: i64,
    tokens_total: Option<i64>,
) {
    let data = match tokens_total {
        Some(total) => format!(r#"{{"role":"{role}","tokens":{{"total":{total}}}}}"#),
        None => format!(r#"{{"role":"{role}"}}"#),
    };
    conn.execute(
        "INSERT INTO message (id, session_id, data, time_created) VALUES (?1, ?2, ?3, ?4)",
        params![id, session_id, data, time_created],
    )
    .unwrap();
}

fn insert_part(
    conn: &Connection,
    id: &str,
    session_id: &str,
    message_id: &str,
    data: &str,
    time_created: i64,
) {
    conn.execute(
        "INSERT INTO part (id, session_id, message_id, data, time_created)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![id, session_id, message_id, data, time_created],
    )
    .unwrap();
}

fn test_config(tmp: &TempDir, opencode_db: &std::path::Path) -> HippoConfig {
    let mut config = HippoConfig::default();
    config.storage.data_dir = tmp.path().join("data");
    config.storage.config_dir = tmp.path().join("config");
    config.opencode.db_path = opencode_db.to_path_buf();
    config.opencode.enabled = true;
    config
}

fn opencode_source_key(path: &std::path::Path) -> String {
    use std::os::unix::fs::MetadataExt;
    format!("opencode-{}", std::fs::metadata(path).unwrap().ino())
}

#[test]
fn poll_tick_writes_session_queue_health_cursor_in_one_call() {
    let tmp = TempDir::new().unwrap();
    let opencode_db_path = tmp.path().join("opencode.db");
    let oc = init_opencode_db(&opencode_db_path);
    insert_session(
        &oc,
        "sess-1",
        "fix-the-bug",
        "Fix the Bug",
        "/Users/me/proj",
        Some("plan"),
        Some("claude-3.5"),
        1_700_000_000_000,
        1_700_000_001_000,
        Some((12, 4, 3)),
    );
    drop(oc);

    let config = test_config(&tmp, &opencode_db_path);
    // Bootstrap Hippo DB so open_db inside poll_tick finds it migrated.
    let _ = hippo_core::storage::open_db(&config.db_path()).unwrap();

    let events = hippo_daemon::opencode_session::poll_tick(&config)
        .expect("poll_tick should succeed against a healthy opencode DB");
    assert_eq!(events, 1, "exactly one session should land");

    let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();

    // agentic_sessions row.
    let (sid, harness, title, model, summary, start, end): (
        String,
        String,
        String,
        String,
        String,
        i64,
        i64,
    ) = conn
        .query_row(
            "SELECT session_id, harness, title, model, summary_text, start_time, end_time
             FROM agentic_sessions WHERE harness = 'opencode'",
            [],
            |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get(6)?,
                ))
            },
        )
        .unwrap();
    assert_eq!(sid, "sess-1");
    assert_eq!(harness, "opencode");
    assert_eq!(title, "Fix the Bug");
    assert_eq!(model, "claude-3.5");
    assert_eq!(start, 1_700_000_000_000);
    assert_eq!(end, 1_700_000_001_000);
    // summary_text must be a real prompt, NOT the cwd. This guards F-26's
    // sibling bug — column-stuffing summary_text with s.directory.
    assert!(
        summary.contains("Fix the Bug") || summary.contains("fix-the-bug"),
        "summary_text should contain the session title or slug, got: {summary:?}",
    );
    assert!(
        summary.contains("Snapshot diffs"),
        "summary_text should include diff stats when present, got: {summary:?}",
    );

    // probe_tag must be NULL on real rows (AP-6).
    let probe_tag: Option<String> = conn
        .query_row(
            "SELECT probe_tag FROM agentic_sessions WHERE session_id = 'sess-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        probe_tag.is_none(),
        "real session must have probe_tag IS NULL"
    );

    // agentic_enrichment_queue must have a matching pending row.
    let (queued_status, queued_count): (String, i64) = conn
        .query_row(
            "SELECT q.status, COUNT(*) FROM agentic_enrichment_queue q
             JOIN agentic_sessions s ON q.session_id = s.id
             WHERE s.harness = 'opencode' GROUP BY q.status",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(queued_status, "pending");
    assert_eq!(queued_count, 1);

    // source_health must reflect the latest event.
    let (last_event_ts, _updated_at): (Option<i64>, i64) = conn
        .query_row(
            "SELECT last_event_ts, updated_at FROM source_health
             WHERE source = 'agentic-session-opencode'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(
        last_event_ts,
        Some(1_700_000_001_000),
        "source_health.last_event_ts should mirror the session's time_updated"
    );

    // agentic_cursor must have advanced.
    let (last_seen, last_id): (i64, String) = conn
        .query_row(
            "SELECT last_seen_updated_at, last_id FROM agentic_cursor LIMIT 1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(last_seen, 1_700_000_001_000);
    assert_eq!(last_id, "sess-1");
}

#[test]
fn poll_tick_is_idempotent_when_no_new_writes() {
    let tmp = TempDir::new().unwrap();
    let opencode_db_path = tmp.path().join("opencode.db");
    let oc = init_opencode_db(&opencode_db_path);
    insert_session(
        &oc,
        "sess-1",
        "slug",
        "Title",
        "/proj",
        None,
        None,
        1_700_000_000_000,
        1_700_000_001_000,
        None,
    );
    drop(oc);

    let config = test_config(&tmp, &opencode_db_path);
    let _ = hippo_core::storage::open_db(&config.db_path()).unwrap();

    assert_eq!(
        hippo_daemon::opencode_session::poll_tick(&config).unwrap(),
        1
    );
    // Second tick with nothing changed: the boundary session sits exactly on
    // the cursor watermark, and the strict `>` comparison must NOT re-read it.
    // A `>=` re-read re-pends the queue row and re-enriches one unchanged
    // session into unbounded duplicate nodes. New same-ms siblings are still
    // caught by the `!known_session_ids` clause, so no genuine session is
    // skipped (F-26).
    assert_eq!(
        hippo_daemon::opencode_session::poll_tick(&config).unwrap(),
        0,
        "an unchanged session must not be re-read on a second poll"
    );

    let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM agentic_sessions", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 1, "no duplicate row should land on a second poll");
}

#[test]
fn poll_tick_does_not_repend_done_session() {
    // Regression: the newest finished session sits exactly on the cursor
    // watermark. With a `>=` boundary comparison the poller re-read it every
    // tick and the queue's ON CONFLICT branch reset the already-`done` row to
    // `pending`, so the brain re-enriched one unchanged session into an
    // unbounded stream of duplicate knowledge nodes. With `>` the finished row
    // is left alone.
    let tmp = TempDir::new().unwrap();
    let opencode_db_path = tmp.path().join("opencode.db");
    let oc = init_opencode_db(&opencode_db_path);
    insert_session(
        &oc,
        "sess-1",
        "slug",
        "Title",
        "/proj",
        None,
        None,
        1_700_000_000_000,
        1_700_000_001_000,
        None,
    );
    drop(oc);

    let config = test_config(&tmp, &opencode_db_path);
    let _ = hippo_core::storage::open_db(&config.db_path()).unwrap();

    assert_eq!(
        hippo_daemon::opencode_session::poll_tick(&config).unwrap(),
        1
    );

    // Simulate the brain finishing enrichment: flip the queue row to 'done'.
    let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
    conn.execute(
        "UPDATE agentic_enrichment_queue SET status = 'done'
         WHERE session_id IN (SELECT id FROM agentic_sessions WHERE session_id = 'sess-1')",
        [],
    )
    .unwrap();
    drop(conn);

    // Poll again with NOTHING changed in the opencode source DB.
    assert_eq!(
        hippo_daemon::opencode_session::poll_tick(&config).unwrap(),
        0,
        "an unchanged finished session must not be re-polled"
    );

    // The finished queue row must remain 'done', never resurrected to 'pending'.
    let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
    let status: String = conn
        .query_row(
            "SELECT q.status FROM agentic_enrichment_queue q
             JOIN agentic_sessions s ON q.session_id = s.id
             WHERE s.session_id = 'sess-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        status, "done",
        "re-pending a done session re-enriches it into a duplicate node"
    );
}

#[test]
fn poll_tick_re_polls_session_on_time_updated_advance() {
    let tmp = TempDir::new().unwrap();
    let opencode_db_path = tmp.path().join("opencode.db");
    let oc = init_opencode_db(&opencode_db_path);
    insert_session(
        &oc,
        "sess-1",
        "slug",
        "Original title",
        "/proj",
        None,
        None,
        1_700_000_000_000,
        1_700_000_001_000,
        None,
    );
    drop(oc);

    let config = test_config(&tmp, &opencode_db_path);
    let _ = hippo_core::storage::open_db(&config.db_path()).unwrap();

    assert_eq!(
        hippo_daemon::opencode_session::poll_tick(&config).unwrap(),
        1
    );

    // Simulate opencode updating the session: bump time_updated + change title.
    let oc = Connection::open(&opencode_db_path).unwrap();
    oc.execute(
        "UPDATE session SET title = 'Updated title', time_updated = ?1 WHERE id = 'sess-1'",
        params![1_700_000_005_000_i64],
    )
    .unwrap();
    drop(oc);

    assert_eq!(
        hippo_daemon::opencode_session::poll_tick(&config).unwrap(),
        1,
        "update with advancing time_updated must re-poll"
    );

    let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
    let (title, end): (String, i64) = conn
        .query_row(
            "SELECT title, end_time FROM agentic_sessions WHERE session_id = 'sess-1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(
        title, "Updated title",
        "ON CONFLICT DO UPDATE must refresh title"
    );
    assert_eq!(end, 1_700_000_005_000);

    // Queue must be re-pended (status='pending', retry_count=0).
    let (status, retries): (String, i64) = conn
        .query_row(
            "SELECT q.status, q.retry_count FROM agentic_enrichment_queue q
             JOIN agentic_sessions s ON q.session_id = s.id
             WHERE s.session_id = 'sess-1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(status, "pending");
    assert_eq!(retries, 0);
}

#[test]
fn poll_tick_backfills_hippo_missing_sessions_older_than_cursor() {
    let tmp = TempDir::new().unwrap();
    let opencode_db_path = tmp.path().join("opencode.db");
    let oc = init_opencode_db(&opencode_db_path);
    insert_session(
        &oc,
        "newer",
        "newer",
        "Newer",
        "/proj",
        None,
        None,
        1_700_000_000_000,
        1_700_000_002_000,
        None,
    );
    drop(oc);

    let config = test_config(&tmp, &opencode_db_path);
    let _ = hippo_core::storage::open_db(&config.db_path()).unwrap();
    assert_eq!(
        hippo_daemon::opencode_session::poll_tick(&config).unwrap(),
        1
    );

    let oc = Connection::open(&opencode_db_path).unwrap();
    insert_session(
        &oc,
        "older-missing",
        "older-missing",
        "Older Missing",
        "/proj",
        None,
        None,
        1_700_000_000_000,
        1_700_000_001_000,
        None,
    );
    drop(oc);

    let hippo_conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
    let (cursor_ts, cursor_id): (i64, String) = hippo_conn
        .query_row(
            "SELECT last_seen_updated_at, last_id
             FROM agentic_cursor WHERE source_key = ?1",
            params![opencode_source_key(&opencode_db_path)],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(cursor_ts, 1_700_000_002_000);
    assert_eq!(cursor_id, "newer");
    drop(hippo_conn);

    hippo_daemon::opencode_session::poll_tick(&config).unwrap();

    let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
    let landed: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM agentic_sessions WHERE harness = 'opencode'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        landed, 2,
        "opencode poll must backfill sessions missing from Hippo even when their time_updated is older than the cursor",
    );
    let queued_missing: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM agentic_enrichment_queue q
             JOIN agentic_sessions s ON q.session_id = s.id
             WHERE s.session_id = 'older-missing' AND q.status = 'pending'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(queued_missing, 1);
}

#[test]
fn poll_tick_extracts_opencode_message_parts_into_enrichment_summary() {
    let tmp = TempDir::new().unwrap();
    let opencode_db_path = tmp.path().join("opencode.db");
    let oc = init_opencode_db(&opencode_db_path);
    insert_session(
        &oc,
        "sess-context",
        "capture-context",
        "Capture useful opencode context",
        "/Users/me/hippo",
        Some("build"),
        Some("gpt-5"),
        1_700_000_000_000,
        1_700_000_010_000,
        None,
    );
    insert_message(
        &oc,
        "msg-user",
        "sess-context",
        "user",
        1_700_000_000_100,
        None,
    );
    insert_part(
        &oc,
        "part-user",
        "sess-context",
        "msg-user",
        r#"{"type":"text","text":"Make Hippo capture useful opencode context from message parts."}"#,
        1_700_000_000_100,
    );
    insert_message(
        &oc,
        "msg-assistant",
        "sess-context",
        "assistant",
        1_700_000_000_200,
        Some(42),
    );
    insert_part(
        &oc,
        "part-tool",
        "sess-context",
        "msg-assistant",
        r#"{"type":"tool","tool":"bash","state":{"status":"completed","input":{"command":"rg opencode brain/src/hippo_brain"},"output":"brain/src/hippo_brain/server.py\n"}}"#,
        1_700_000_000_210,
    );
    insert_part(
        &oc,
        "part-secret-tool",
        "sess-context",
        "msg-assistant",
        r#"{"type":"tool","tool":"bash","state":{"status":"completed","input":{"command":"export API_KEY=sk-1234567890abcdef"},"output":"ok"}}"#,
        1_700_000_000_220,
    );
    insert_part(
        &oc,
        "part-assistant",
        "sess-context",
        "msg-assistant",
        r#"{"type":"text","text":"Updated brain/src/hippo_brain/server.py and the opencode poller."}"#,
        1_700_000_000_230,
    );
    insert_part(
        &oc,
        "part-patch",
        "sess-context",
        "msg-assistant",
        r#"{"type":"patch","files":[{"path":"brain/src/hippo_brain/server.py"},{"path":"crates/hippo-daemon/src/opencode_session.rs"}]}"#,
        1_700_000_000_240,
    );
    drop(oc);

    let config = test_config(&tmp, &opencode_db_path);
    let _ = hippo_core::storage::open_db(&config.db_path()).unwrap();

    let events = hippo_daemon::opencode_session::poll_tick(&config).unwrap();
    assert_eq!(events, 1);

    let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
    let (summary, message_count, token_count): (String, i64, i64) = conn
        .query_row(
            "SELECT summary_text, message_count, token_count
             FROM agentic_sessions WHERE session_id = 'sess-context'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();

    assert!(
        summary.contains("Make Hippo capture useful opencode context"),
        "user text parts should land in summary_text, got: {summary}",
    );
    assert!(
        summary.contains("bash: rg opencode brain/src/hippo_brain"),
        "tool calls should land in summary_text, got: {summary}",
    );
    assert!(
        summary.contains("brain/src/hippo_brain/server.py"),
        "patch/file context should land in summary_text, got: {summary}",
    );
    assert!(
        summary.contains("[REDACTED]"),
        "tool inputs must be redacted before storage, got: {summary}",
    );
    assert!(
        !summary.contains("sk-1234567890abcdef"),
        "raw secrets must not be stored in opencode summaries",
    );
    assert_eq!(message_count, 2);
    assert_eq!(token_count, 42);
}

#[test]
fn poll_tick_no_op_when_disabled() {
    let tmp = TempDir::new().unwrap();
    let opencode_db_path = tmp.path().join("opencode.db");
    let oc = init_opencode_db(&opencode_db_path);
    insert_session(
        &oc,
        "sess-1",
        "slug",
        "Title",
        "/proj",
        None,
        None,
        1_700_000_000_000,
        1_700_000_001_000,
        None,
    );
    drop(oc);

    let mut config = test_config(&tmp, &opencode_db_path);
    config.opencode.enabled = false;
    let _ = hippo_core::storage::open_db(&config.db_path()).unwrap();

    assert_eq!(
        hippo_daemon::opencode_session::poll_tick(&config).unwrap(),
        0
    );

    let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM agentic_sessions", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 0, "disabled config must not write anything");
}

#[test]
fn poll_tick_no_op_when_opencode_db_missing() {
    let tmp = TempDir::new().unwrap();
    let missing = tmp.path().join("does-not-exist.db");

    let config = test_config(&tmp, &missing);
    let _ = hippo_core::storage::open_db(&config.db_path()).unwrap();

    // No DB → debug log + return Ok(0). Must not error and must not write.
    assert_eq!(
        hippo_daemon::opencode_session::poll_tick(&config).unwrap(),
        0
    );
}
