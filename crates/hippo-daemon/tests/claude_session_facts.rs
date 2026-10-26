//! HIPO-61: `cost-state`, `pr-link`, and `ai-title` records land in
//! `agentic_session_facts` and `agentic_sessions.title`, without touching the
//! enrichment queue.

use std::path::{Path, PathBuf};

use hippo_core::storage::open_db;
use hippo_daemon::claude_session::ingest_session_file;
use rusqlite::Connection;

const SESSION_ID: &str = "cccccccc-2222-4333-8444-555555555555";

fn fixture(project: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/claude")
        .join(project)
        .join(format!("{SESSION_ID}.jsonl"))
}

type Facts = (
    Option<f64>,
    Option<i64>,
    Option<i64>,
    Option<i64>,
    Option<i64>,
    Option<String>,
    Option<String>,
    String,
);

fn facts(conn: &Connection) -> Vec<Facts> {
    let mut stmt = conn
        .prepare(
            "SELECT cost_usd, duration_ms, tool_duration_ms, lines_added, lines_removed,
                    title, cli_version, pr_links_json
             FROM agentic_session_facts
             WHERE session_id = ?1 AND harness = 'claude-code'",
        )
        .unwrap();
    stmt.query_map([SESSION_ID], |r| {
        Ok((
            r.get(0)?,
            r.get(1)?,
            r.get(2)?,
            r.get(3)?,
            r.get(4)?,
            r.get(5)?,
            r.get(6)?,
            r.get(7)?,
        ))
    })
    .unwrap()
    .collect::<rusqlite::Result<_>>()
    .unwrap()
}

fn titles(conn: &Connection) -> Vec<String> {
    let mut stmt = conn
        .prepare(
            "SELECT title FROM agentic_sessions
             WHERE session_id = ?1 AND harness = 'claude-code' ORDER BY segment_index",
        )
        .unwrap();
    stmt.query_map([SESSION_ID], |r| r.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
}

/// Every column of every queue row, so any re-enqueue side effect shows up.
fn queue_snapshot(conn: &Connection) -> Vec<(i64, i64, String, i64, i64, i64)> {
    let mut stmt = conn
        .prepare(
            "SELECT id, session_id, status, retry_count, enqueued_at, updated_at
             FROM agentic_enrichment_queue ORDER BY id",
        )
        .unwrap();
    stmt.query_map([], |r| {
        Ok((
            r.get(0)?,
            r.get(1)?,
            r.get(2)?,
            r.get(3)?,
            r.get(4)?,
            r.get(5)?,
        ))
    })
    .unwrap()
    .collect::<rusqlite::Result<_>>()
    .unwrap()
}

#[test]
fn last_records_win_and_reingest_keeps_one_row_and_the_queue() {
    let tmp = tempfile::tempdir().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();
    let path = fixture("-projects-demo");

    let (inserted, _, errors) = ingest_session_file(&conn, &path);
    assert_eq!((inserted, errors), (2, 0));
    let queue = queue_snapshot(&conn);
    assert_eq!(queue.len(), 2);

    let rows = facts(&conn);
    assert_eq!(rows.len(), 1);
    let (cost, dur, tool, added, removed, title, version, pr_json) = rows[0].clone();
    assert_eq!(cost, Some(1.5));
    assert_eq!((dur, tool), (Some(9000), Some(700)));
    assert_eq!((added, removed), (Some(42), Some(9)));
    assert_eq!(
        title.as_deref(),
        Some("Add and document fixture client retry")
    );
    assert_eq!(version.as_deref(), Some("0.0.2-fixture"));

    let links: serde_json::Value = serde_json::from_str(&pr_json).unwrap();
    let links = links.as_array().unwrap();
    assert_eq!(links.len(), 2, "duplicate pr-link must collapse: {links:?}");
    assert_eq!(links[0]["number"], 7);
    assert_eq!(links[0]["repo"], "example/demo");
    assert_eq!(links[0]["url"], "https://github.com/example/demo/pull/7");
    assert_eq!(links[1]["number"], 8);
    let url = links[1]["url"].as_str().unwrap();
    assert!(!url.contains("ghp_aaaa"), "prUrl must be redacted: {url}");

    assert_eq!(
        titles(&conn),
        vec!["Add and document fixture client retry"; 2],
        "title on every segment"
    );

    let (inserted, skipped, errors) = ingest_session_file(&conn, &path);
    assert_eq!((inserted, skipped, errors), (0, 2, 0));
    assert_eq!(facts(&conn), rows, "second ingest leaves the same facts");
    assert_eq!(queue_snapshot(&conn), queue, "queue unchanged by re-ingest");
}

#[test]
fn resumed_file_without_cost_state_keeps_stored_facts() {
    let tmp = tempfile::tempdir().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();
    ingest_session_file(&conn, &fixture("-projects-demo"));
    let before = facts(&conn);

    let (_, _, errors) = ingest_session_file(&conn, &fixture("-projects-demo-resume"));
    assert_eq!(errors, 0);

    let after = facts(&conn);
    assert_eq!(after.len(), 1);
    let (b, a) = (&before[0], &after[0]);
    assert_eq!(
        (a.0, a.1, a.2, a.3, a.4),
        (b.0, b.1, b.2, b.3, b.4),
        "cost kept"
    );
    assert_eq!(a.5, b.5, "title kept");
    assert_eq!(a.7, b.7, "pr links kept");
    assert_eq!(a.6.as_deref(), Some("0.0.3-fixture"), "later version wins");
    assert!(
        titles(&conn)
            .iter()
            .all(|t| t == "Add and document fixture client retry"),
        "the resumed file's segment must not blank the title"
    );
}
