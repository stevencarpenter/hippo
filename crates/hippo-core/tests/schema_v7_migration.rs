use hippo_core::storage::open_db;
use rusqlite::Connection;
use tempfile::TempDir;

fn seed_v6(path: &std::path::Path) {
    let conn = Connection::open(path).unwrap();
    conn.execute_batch(include_str!("fixtures/schema_v6.sql"))
        .unwrap();
}

#[test]
fn v6_db_migrates_to_v7_and_adds_source_kind_and_tool_name() {
    let tmp = TempDir::new().unwrap();
    let db = tmp.path().join("hippo.db");
    seed_v6(&db);

    // Confirm the fixture seeded at v6 before open_db migrates it forward.
    let seed_conn = Connection::open(&db).unwrap();
    let seed_version: i64 = seed_conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(seed_version, 6, "fixture should start at v6");
    drop(seed_conn);

    let conn = open_db(&db).unwrap();

    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(version, 9);

    // events table must gain source_kind (NOT NULL default 'shell') and tool_name (TEXT).
    let columns: Vec<(String, String, i64, Option<String>)> = conn
        .prepare("PRAGMA table_info(events)")
        .unwrap()
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(1)?,         // name
                row.get::<_, String>(2)?,         // type
                row.get::<_, i64>(3)?,            // notnull
                row.get::<_, Option<String>>(4)?, // dflt_value
            ))
        })
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();

    let source_kind = columns
        .iter()
        .find(|(n, _, _, _)| n == "source_kind")
        .expect("source_kind column missing after v7 migration");
    assert_eq!(source_kind.1, "TEXT");
    assert_eq!(source_kind.2, 1, "source_kind must be NOT NULL");
    assert_eq!(source_kind.3.as_deref(), Some("'shell'"));

    let tool_name = columns
        .iter()
        .find(|(n, _, _, _)| n == "tool_name")
        .expect("tool_name column missing after v7 migration");
    assert_eq!(tool_name.1, "TEXT");
    assert_eq!(tool_name.2, 0, "tool_name must be nullable");

    // Index on source_kind (partial) should exist.
    let idx_exists: i64 = conn
        .query_row(
            "SELECT count(*) FROM sqlite_master
             WHERE type='index' AND name='idx_events_source_kind'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        idx_exists, 1,
        "idx_events_source_kind missing after v7 migration"
    );

    // Partial index predicate is `WHERE source_kind != 'shell'`. Since every
    // pre-migration row defaults to 'shell', the index must be empty — a
    // cheap belt-and-suspenders assertion that the partial predicate was
    // applied correctly (not a full index that the planner might accidentally
    // populate with every row).
    let idx_rows: i64 = conn
        .query_row(
            "SELECT count(*) FROM events INDEXED BY idx_events_source_kind",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        idx_rows, 0,
        "partial index should contain no pre-migration 'shell' rows"
    );
}

#[test]
fn existing_v6_rows_default_to_source_kind_shell() {
    // Rows inserted into a v6 DB before the migration must default to
    // 'shell' for source_kind (and NULL for tool_name) after the upgrade.
    let tmp = TempDir::new().unwrap();
    let db = tmp.path().join("hippo.db");
    seed_v6(&db);

    let seed_conn = Connection::open(&db).unwrap();
    seed_conn
        .execute(
            "INSERT INTO sessions (start_time, shell, hostname, username) \
             VALUES (1, 'zsh', 'laptop', 'user')",
            [],
        )
        .unwrap();
    let sid: i64 = seed_conn.last_insert_rowid();
    seed_conn
        .execute(
            "INSERT INTO events (session_id, timestamp, command, exit_code, duration_ms, cwd, hostname, shell) \
             VALUES (?1, 1, 'pre-migration cmd', 0, 10, '/tmp', 'laptop', 'zsh')",
            rusqlite::params![sid],
        )
        .unwrap();
    drop(seed_conn);

    let conn = open_db(&db).unwrap();
    let (source_kind, tool_name): (String, Option<String>) = conn
        .query_row(
            "SELECT source_kind, tool_name FROM events WHERE command = 'pre-migration cmd'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(source_kind, "shell");
    assert!(tool_name.is_none());
}

#[test]
fn fresh_db_has_v9() {
    let tmp = TempDir::new().unwrap();
    let db = tmp.path().join("hippo.db");
    let conn = open_db(&db).unwrap();

    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(version, 9);

    // source_kind / tool_name must exist on a fresh install too
    // (i.e. schema.sql itself must carry them, not just the migration).
    let source_kind_exists: i64 = conn
        .query_row(
            "SELECT count(*) FROM pragma_table_info('events') WHERE name='source_kind'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let tool_name_exists: i64 = conn
        .query_row(
            "SELECT count(*) FROM pragma_table_info('events') WHERE name='tool_name'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(source_kind_exists, 1);
    assert_eq!(tool_name_exists, 1);
}
