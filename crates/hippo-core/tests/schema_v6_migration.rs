use hippo_core::storage::open_db;
use rusqlite::Connection;
use tempfile::TempDir;

fn seed_v5(path: &std::path::Path) {
    let conn = Connection::open(path).unwrap();
    conn.execute_batch(include_str!("fixtures/schema_v5.sql"))
        .unwrap();
}

#[test]
fn v5_db_migrates_to_latest_and_has_fts_and_triggers() {
    let tmp = TempDir::new().unwrap();
    let db = tmp.path().join("hippo.db");
    seed_v5(&db);

    let conn = open_db(&db).unwrap();

    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    // v5 → full chain (v6, v7, v8, v9); only the final version is exercised here.
    assert_eq!(version, 11);

    let fts_exists: i64 = conn
        .query_row(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='knowledge_fts'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(fts_exists, 1, "knowledge_fts missing after v6 migration");

    for trigger in [
        "knowledge_nodes_fts_ai",
        "knowledge_nodes_fts_ad",
        "knowledge_nodes_fts_au",
    ] {
        let exists: i64 = conn
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name=?1",
                [trigger],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(exists, 1, "trigger {trigger} missing");
    }
}

#[test]
fn inserting_knowledge_node_populates_fts() {
    let tmp = TempDir::new().unwrap();
    let db = tmp.path().join("hippo.db");
    seed_v5(&db);
    let conn = open_db(&db).unwrap();

    conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type, outcome, tags)
         VALUES (?1, ?2, ?3, 'observation', 'success', '[]')",
        rusqlite::params![
            "uuid-test-1",
            r#"{"summary":"rust migration design notes","other":"x"}"#,
            "rust migration design notes flush schema",
        ],
    )
    .unwrap();

    let hits: i64 = conn
        .query_row(
            "SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'migration'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(hits >= 1, "FTS did not index the inserted row");

    // Summary column should be searchable.
    let summary_hit: i64 = conn
        .query_row(
            "SELECT rowid FROM knowledge_fts WHERE summary MATCH 'design' LIMIT 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(summary_hit > 0);
}

#[test]
fn updating_knowledge_node_updates_fts() {
    let tmp = TempDir::new().unwrap();
    let db = tmp.path().join("hippo.db");
    seed_v5(&db);
    let conn = open_db(&db).unwrap();

    conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type)
         VALUES ('u', '{\"summary\":\"original\"}', 'original body', 'observation')",
        [],
    )
    .unwrap();
    let id: i64 = conn
        .query_row("SELECT id FROM knowledge_nodes WHERE uuid='u'", [], |r| {
            r.get(0)
        })
        .unwrap();

    conn.execute(
        "UPDATE knowledge_nodes SET content = ?1, embed_text = ?2 WHERE id = ?3",
        rusqlite::params![r#"{"summary":"replaced"}"#, "new body content", id],
    )
    .unwrap();

    let old_hits: i64 = conn
        .query_row(
            "SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'original'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(old_hits, 0, "old content still in FTS after update");

    let new_hits: i64 = conn
        .query_row(
            "SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'replaced'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(new_hits, 1);
}

#[test]
fn deleting_knowledge_node_removes_fts_row() {
    let tmp = TempDir::new().unwrap();
    let db = tmp.path().join("hippo.db");
    seed_v5(&db);
    let conn = open_db(&db).unwrap();

    conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type)
         VALUES ('u', '{\"summary\":\"s\"}', 'body', 'observation')",
        [],
    )
    .unwrap();
    let id: i64 = conn
        .query_row("SELECT id FROM knowledge_nodes WHERE uuid='u'", [], |r| {
            r.get(0)
        })
        .unwrap();

    // Clean up FK references first (none for this test but pattern matters).
    conn.execute("DELETE FROM knowledge_nodes WHERE id = ?1", [id])
        .unwrap();

    let hits: i64 = conn
        .query_row("SELECT count(*) FROM knowledge_fts", [], |r| r.get(0))
        .unwrap();
    assert_eq!(hits, 0, "FTS row not deleted when knowledge_node deleted");
}

#[test]
fn fresh_db_has_latest_schema_and_fts_ready() {
    let tmp = TempDir::new().unwrap();
    let db = tmp.path().join("hippo.db");
    let conn = open_db(&db).unwrap();

    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    // Fresh DB applies the full SCHEMA + any subsequent migrations, so it
    // lands at the latest version rather than v6.
    assert_eq!(version, 11);

    conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type)
         VALUES ('u', '{\"summary\":\"from fresh\"}', 'fresh body', 'observation')",
        [],
    )
    .unwrap();
    let hits: i64 = conn
        .query_row(
            "SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'fresh'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(hits, 1);
}
