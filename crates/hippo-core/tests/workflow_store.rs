use hippo_core::storage::{open_db, workflow_store};
use tempfile::TempDir;

fn fresh_db() -> (TempDir, std::path::PathBuf) {
    let tmp = TempDir::new().unwrap();
    let path = tmp.path().join("hippo.db");
    open_db(&path).unwrap();
    (tmp, path)
}

#[test]
fn upsert_run_inserts_and_updates() {
    let (_tmp, path) = fresh_db();
    let conn = rusqlite::Connection::open(&path).unwrap();

    let run = workflow_store::RunRow {
        id: 1,
        repo: "me/r",
        head_sha: "abc",
        head_branch: Some("main"),
        event: "push",
        status: "in_progress",
        conclusion: None,
        started_at: Some(1000),
        completed_at: None,
        html_url: "https://x",
        actor: Some("me"),
        raw_json: "{}",
    };
    workflow_store::upsert_run(&conn, &run, 5000).unwrap();
    let count: i64 = conn
        .query_row("SELECT count(*) FROM workflow_runs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 1);

    // Update path: same id, status changed.
    let run2 = workflow_store::RunRow {
        status: "completed",
        conclusion: Some("success"),
        ..run
    };
    workflow_store::upsert_run(&conn, &run2, 6000).unwrap();
    let (status, conclusion, last_seen): (String, Option<String>, i64) = conn
        .query_row(
            "SELECT status, conclusion, last_seen_at FROM workflow_runs WHERE id=1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(status, "completed");
    assert_eq!(conclusion.as_deref(), Some("success"));
    assert_eq!(last_seen, 6000);
}

#[test]
fn insert_annotation_runs_parser() {
    let (_tmp, path) = fresh_db();
    let conn = rusqlite::Connection::open(&path).unwrap();

    // Need a parent run+job for FK cascade.
    workflow_store::upsert_run(
        &conn,
        &workflow_store::RunRow {
            id: 1,
            repo: "me/r",
            head_sha: "abc",
            head_branch: None,
            event: "push",
            status: "completed",
            conclusion: Some("failure"),
            started_at: None,
            completed_at: None,
            html_url: "x",
            actor: None,
            raw_json: "{}",
        },
        1000,
    )
    .unwrap();
    workflow_store::upsert_job(
        &conn,
        &workflow_store::JobRow {
            id: 10,
            run_id: 1,
            name: "ruff",
            status: "completed",
            conclusion: Some("failure"),
            started_at: None,
            completed_at: None,
            runner_name: None,
            raw_json: "{}",
        },
    )
    .unwrap();

    workflow_store::insert_annotation(
        &conn,
        10,
        "ruff",
        "failure",
        "F401 unused import",
        Some("brain/x.py"),
        Some(3),
    )
    .unwrap();

    let (tool, rule): (Option<String>, Option<String>) = conn
        .query_row(
            "SELECT tool, rule_id FROM workflow_annotations WHERE job_id=10",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(tool.as_deref(), Some("ruff"));
    assert_eq!(rule.as_deref(), Some("F401"));
}

#[test]
fn enqueue_enrichment_is_idempotent() {
    let (_tmp, path) = fresh_db();
    let conn = rusqlite::Connection::open(&path).unwrap();

    workflow_store::upsert_run(
        &conn,
        &workflow_store::RunRow {
            id: 1,
            repo: "me/r",
            head_sha: "abc",
            head_branch: None,
            event: "push",
            status: "completed",
            conclusion: Some("success"),
            started_at: None,
            completed_at: None,
            html_url: "x",
            actor: None,
            raw_json: "{}",
        },
        1000,
    )
    .unwrap();

    workflow_store::enqueue_enrichment(&conn, 1, 1000).unwrap();
    workflow_store::enqueue_enrichment(&conn, 1, 2000).unwrap();
    let count: i64 = conn
        .query_row(
            "SELECT count(*) FROM workflow_enrichment_queue WHERE run_id=1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
}
