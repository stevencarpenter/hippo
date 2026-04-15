use hippo_core::storage::open_db;
use rusqlite::Connection;
use tempfile::TempDir;

fn seed_v4(path: &std::path::Path) {
    let conn = Connection::open(path).unwrap();
    conn.execute_batch(include_str!("fixtures/schema_v4.sql")).unwrap();
    conn.pragma_update(None, "user_version", 4).unwrap();
}

#[test]
fn v4_db_migrates_to_v5_and_has_workflow_tables() {
    let tmp = TempDir::new().unwrap();
    let db = tmp.path().join("hippo.db");
    seed_v4(&db);

    let conn = open_db(&db).unwrap();

    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(version, 5);

    for table in [
        "workflow_runs",
        "workflow_jobs",
        "workflow_annotations",
        "workflow_log_excerpts",
        "sha_watchlist",
        "workflow_enrichment_queue",
        "lessons",
        "lesson_pending",
        "knowledge_node_workflow_runs",
        "knowledge_node_lessons",
    ] {
        let exists: i64 = conn
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?1",
                [table],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(exists, 1, "table {table} missing after v5 migration");
    }
}
