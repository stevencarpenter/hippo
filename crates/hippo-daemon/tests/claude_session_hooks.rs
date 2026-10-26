//! HIPO-62: `stop_hook_summary` outcomes and `permissionDecision` counts land
//! per segment in `agentic_sessions`, without changing segmentation or the
//! enrichment queue.

use std::path::{Path, PathBuf};

use hippo_core::storage::open_db;
use hippo_daemon::claude_session::ingest_session_file;
use rusqlite::Connection;

const SESSION_ID: &str = "dddddddd-2222-4333-8444-555555555555";

fn fixture() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/claude/-projects-demo-hooks")
        .join(format!("{SESSION_ID}.jsonl"))
}

/// (hook_runs, hook_errors, prevented_continuations, hook_names_json,
/// permission_decisions_json) per segment, in segment order.
type HookRow = (i64, i64, i64, String, String);

fn hook_rows(conn: &Connection) -> Vec<HookRow> {
    let mut stmt = conn
        .prepare(
            "SELECT hook_runs, hook_errors, prevented_continuations,
                    hook_names_json, permission_decisions_json
             FROM agentic_sessions
             WHERE session_id = ?1 AND harness = 'claude-code' ORDER BY segment_index",
        )
        .unwrap();
    stmt.query_map([SESSION_ID], |r| {
        Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
    })
    .unwrap()
    .collect::<rusqlite::Result<_>>()
    .unwrap()
}

fn segment_hashes(conn: &Connection) -> Vec<(i64, String)> {
    let mut stmt = conn
        .prepare(
            "SELECT segment_index, content_hash FROM agentic_sessions
             WHERE session_id = ?1 AND harness = 'claude-code' ORDER BY segment_index",
        )
        .unwrap();
    stmt.query_map([SESSION_ID], |r| Ok((r.get(0)?, r.get(1)?)))
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

fn json(s: &str) -> serde_json::Value {
    serde_json::from_str(s).unwrap()
}

#[test]
fn hooks_and_permission_decisions_are_recorded_per_segment() {
    let tmp = tempfile::tempdir().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();
    let path = fixture();

    let (inserted, _, errors) = ingest_session_file(&conn, &path);
    assert_eq!((inserted, errors), (2, 0));
    let queue = queue_snapshot(&conn);
    assert_eq!(queue.len(), 2);

    let rows = hook_rows(&conn);
    assert_eq!(rows.len(), 2);

    let (runs, errs, prevented, names, decisions) = &rows[0];
    assert_eq!((*runs, *errs, *prevented), (2, 2, 1));
    let names = json(names);
    let names = names.as_array().unwrap();
    assert_eq!(names.len(), 2, "{names:?}");
    assert!(
        names[0]
            .as_str()
            .unwrap()
            .starts_with("/opt/fixture/notify.sh"),
        "sorted: notify.sh before stop-check.sh: {names:?}"
    );
    assert!(
        !names[0].as_str().unwrap().contains("ghp_aaaa"),
        "hook command must be redacted: {names:?}"
    );
    assert_eq!(names[1], "/opt/fixture/stop-check.sh");
    assert_eq!(
        json(decisions),
        serde_json::json!({"accept": 2, "reject": 1})
    );

    let (runs, errs, prevented, names, decisions) = &rows[1];
    assert_eq!((*runs, *errs, *prevented), (3, 0, 0));
    assert_eq!(
        json(names),
        serde_json::json!(["/opt/fixture/stop-check.sh"])
    );
    assert_eq!(json(decisions), serde_json::json!({}));

    // Names only: no hook output or stop reason reaches any stored column.
    let leaked: i64 = conn
        .query_row(
            "SELECT count(*) FROM agentic_sessions
             WHERE session_id = ?1
               AND (summary_text || user_prompts_json || tool_calls_json
                    || hook_names_json || permission_decisions_json)
                   LIKE '%MARKER%'",
            [SESSION_ID],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(leaked, 0);

    let (inserted, skipped, errors) = ingest_session_file(&conn, &path);
    assert_eq!((inserted, skipped, errors), (0, 2, 0));
    assert_eq!(
        hook_rows(&conn),
        rows,
        "second ingest keeps the same values"
    );
    assert_eq!(queue_snapshot(&conn), queue, "queue unchanged by re-ingest");
}

#[test]
fn hook_fields_do_not_change_segmentation_or_content_hash() {
    let tmp = tempfile::tempdir().unwrap();
    let with_hooks = open_db(&tmp.path().join("a.db")).unwrap();
    ingest_session_file(&with_hooks, &fixture());

    // Same transcript with every hook and permission field removed. The
    // `system` records stay, so they still start and count as before.
    let stripped_dir = tmp.path().join("-projects-demo-hooks");
    std::fs::create_dir_all(&stripped_dir).unwrap();
    let stripped_path = stripped_dir.join(format!("{SESSION_ID}.jsonl"));
    let text = std::fs::read_to_string(fixture()).unwrap();
    let stripped: String = text
        .lines()
        .map(|l| {
            let mut v = json(l);
            let o = v.as_object_mut().unwrap();
            for k in [
                "permissionDecision",
                "hookCount",
                "preventedContinuation",
                "stopReason",
                "hookErrors",
                "hookInfos",
            ] {
                o.remove(k);
            }
            format!("{v}\n")
        })
        .collect();
    std::fs::write(&stripped_path, stripped).unwrap();
    let without = open_db(&tmp.path().join("b.db")).unwrap();
    ingest_session_file(&without, &stripped_path);

    let hashes = segment_hashes(&with_hooks);
    assert_eq!(hashes.len(), 2);
    assert_eq!(hashes, segment_hashes(&without));
    assert_eq!(
        hook_rows(&without),
        vec![
            (0, 0, 0, "[]".to_string(), "{}".to_string()),
            (0, 0, 0, "[]".to_string(), "{}".to_string())
        ]
    );
}

#[test]
fn malformed_hook_fields_contribute_nothing_and_drop_no_segment() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("-projects-demo-hooks");
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join(format!("{SESSION_ID}.jsonl"));
    let text = std::fs::read_to_string(fixture()).unwrap();
    let bad: String = text
        .lines()
        .map(|l| {
            let mut v = json(l);
            let o = v.as_object_mut().unwrap();
            if o.contains_key("hookCount") {
                o.insert("hookCount".into(), "many".into());
                o.insert("preventedContinuation".into(), "yes".into());
                o.insert("hookErrors".into(), "boom".into());
                o.insert("hookInfos".into(), serde_json::json!([{"command": 7}, 3]));
            }
            if o.contains_key("permissionDecision") {
                o.insert("permissionDecision".into(), serde_json::json!({"x": 1}));
            }
            format!("{v}\n")
        })
        .collect();
    std::fs::write(&path, bad).unwrap();

    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();
    let (inserted, _, errors) = ingest_session_file(&conn, &path);
    assert_eq!((inserted, errors), (2, 0));
    assert_eq!(
        hook_rows(&conn),
        vec![
            (0, 0, 0, "[]".to_string(), "{}".to_string()),
            (0, 0, 0, "[]".to_string(), "{}".to_string())
        ]
    );
}
