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

fn test_config(tmp: &TempDir, opencode_db: &std::path::Path) -> HippoConfig {
    let mut config = HippoConfig::default();
    config.storage.data_dir = tmp.path().join("data");
    config.storage.config_dir = tmp.path().join("config");
    config.opencode.db_path = opencode_db.to_path_buf();
    config.opencode.enabled = true;
    config
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
    // Second tick on an unchanged opencode DB must not re-emit the same row.
    assert_eq!(
        hippo_daemon::opencode_session::poll_tick(&config).unwrap(),
        0,
        "cursor must skip already-seen rows"
    );

    let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM agentic_sessions", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 1, "no duplicate row should land on a second poll");
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
