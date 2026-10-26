//! HIPO-59: subagent identity from the sibling `meta.json`, and the
//! `subagent_type: description` Agent tool-call summary.
//!
//! Fixture layout mirrors `~/.claude/projects`:
//! `fixtures/claude/<project>/<parent>.jsonl` and
//! `fixtures/claude/<project>/<parent>/subagents/agent-<id>.{jsonl,meta.json}`.

use std::path::{Path, PathBuf};

use hippo_core::storage::open_db;
use hippo_daemon::claude_session::ingest_session_file;

const PARENT_ID: &str = "11111111-2222-4333-8444-555555555555";
const SUBAGENT_ID: &str = "agent-afixture000000001";

fn project_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/claude/-projects-demo")
}

fn parent_path() -> PathBuf {
    project_dir().join(format!("{PARENT_ID}.jsonl"))
}

fn subagent_path() -> PathBuf {
    project_dir().join(format!("{PARENT_ID}/subagents/{SUBAGENT_ID}.jsonl"))
}

type SubagentFields = (i64, String, Option<i64>, Option<String>, Option<i64>);

fn subagent_fields(conn: &rusqlite::Connection, session_id: &str) -> SubagentFields {
    conn.query_row(
        "SELECT is_subagent, agent, spawn_depth, parent_tool_use_id, subagent_finished
         FROM agentic_sessions
         WHERE session_id = ?1 AND harness = 'claude-code'",
        [session_id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
    )
    .unwrap()
}

/// Every column of every queue row, so any re-enqueue side effect shows up.
fn queue_snapshot(conn: &rusqlite::Connection) -> Vec<(i64, i64, String, i64, i64, i64)> {
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
fn subagent_meta_and_agent_summary_land_and_reingest_is_idempotent() {
    let tmp = tempfile::tempdir().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();

    for path in [parent_path(), subagent_path()] {
        let (inserted, _, errors) = ingest_session_file(&conn, &path);
        assert_eq!((inserted, errors), (1, 0), "{}", path.display());
    }

    assert_eq!(
        subagent_fields(&conn, SUBAGENT_ID),
        (
            1,
            "Explore".to_string(),
            Some(1),
            Some("toolu_fixture_spawn".to_string()),
            Some(1)
        )
    );
    // Main sessions carry no meta.json: agent stays empty, new fields NULL.
    assert_eq!(
        subagent_fields(&conn, PARENT_ID),
        (0, String::new(), None, None, None)
    );

    // Columns that later Phase 1 tasks populate hold their defaults today.
    let defaults: (Option<String>, Option<String>, i64, i64, String, String) = conn
        .query_row(
            "SELECT requested_model, effort, input_tokens, hook_runs,
                    hook_names_json, permission_decisions_json
             FROM agentic_sessions WHERE session_id = ?1",
            [SUBAGENT_ID],
            |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                ))
            },
        )
        .unwrap();
    assert_eq!(
        defaults,
        (None, None, 0, 0, "[]".to_string(), "{}".to_string())
    );

    let tool_calls: String = conn
        .query_row(
            "SELECT tool_calls_json FROM agentic_sessions WHERE session_id = ?1",
            [PARENT_ID],
            |r| r.get(0),
        )
        .unwrap();
    let tool_calls: serde_json::Value = serde_json::from_str(&tool_calls).unwrap();
    assert_eq!(
        tool_calls[0],
        serde_json::json!({"name": "Agent", "summary": "Explore: survey the fixture module"})
    );

    // Re-ingesting the same transcripts leaves the enrichment queue unchanged.
    let before = queue_snapshot(&conn);
    assert_eq!(before.len(), 2);
    for path in [parent_path(), subagent_path()] {
        let (inserted, skipped, errors) = ingest_session_file(&conn, &path);
        assert_eq!((inserted, skipped, errors), (0, 1, 0));
    }
    assert_eq!(queue_snapshot(&conn), before);
}

#[test]
fn subagent_without_meta_json_still_lands_with_null_fields() {
    let tmp = tempfile::tempdir().unwrap();
    let conn = open_db(&tmp.path().join("hippo.db")).unwrap();
    let dir = tmp
        .path()
        .join(format!("-projects-demo/{PARENT_ID}/subagents"));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join(format!("{SUBAGENT_ID}.jsonl"));
    std::fs::copy(subagent_path(), &path).unwrap();

    let (inserted, _, errors) = ingest_session_file(&conn, &path);
    assert_eq!((inserted, errors), (1, 0));
    assert_eq!(
        subagent_fields(&conn, SUBAGENT_ID),
        (1, String::new(), None, None, None)
    );
}
