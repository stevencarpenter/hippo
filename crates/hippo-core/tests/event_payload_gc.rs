use hippo_core::events::{CapturedOutput, ShellEvent, ShellKind};
use hippo_core::storage::{self, EVENT_PAYLOAD_RETENTION_MS, PayloadCheckpoint};
use rusqlite::Connection;
use sha2::{Digest, Sha256};
use std::collections::HashMap;

const CREATED: i64 = 1_700_000_000_000;
const EXPIRES: i64 = CREATED + EVENT_PAYLOAD_RETENTION_MS;

fn seed(conn: &Connection, id: i64, expiry: i64, env_id: Option<i64>) {
    conn.execute_batch(
        "INSERT OR IGNORE INTO sessions(id,start_time,shell,hostname,username)
         VALUES(1,0,'zsh','synthetic','test');",
    )
    .unwrap();
    conn.execute(
        "INSERT INTO events(id,session_id,timestamp,command,stdout,stderr,duration_ms,
         cwd,hostname,shell,git_repo,git_branch,created_at,payload_expiry,env_snapshot_id)
         VALUES(?1,1,100,'test command','synthetic output','synthetic error',200,
         '/tmp/test','synthetic','zsh','test/repo','main',?2,?3,?4)",
        rusqlite::params![id, CREATED, expiry, env_id],
    )
    .unwrap();
}

fn scalar(conn: &Connection, sql: &str) -> i64 {
    conn.query_row(sql, [], |row| row.get(0)).unwrap()
}

fn event() -> ShellEvent {
    ShellEvent {
        session_id: uuid::Uuid::new_v4(),
        command: "test command".into(),
        exit_code: 0,
        duration_ms: 100,
        cwd: "/tmp/test".into(),
        hostname: "synthetic".into(),
        shell: ShellKind::Zsh,
        git_state: None,
        stdout: Some(CapturedOutput {
            content: "synthetic".into(),
            truncated: false,
            original_bytes: 9,
        }),
        stderr: None,
        env_snapshot: HashMap::from([("TEST_ENV".into(), "synthetic".into())]),
        redaction_count: 0,
        tool_name: None,
    }
}

#[test]
fn expiry_boundary_keeps_ledger_metadata_joins_and_counts_but_removes_output() {
    let dir = tempfile::tempdir().unwrap();
    let conn = storage::open_db(&dir.path().join("test.db")).unwrap();
    seed(&conn, 1, EXPIRES, None);
    conn.execute_batch(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text) VALUES(1,'test','{}','derived');
         INSERT INTO knowledge_node_events VALUES(1,1);
         INSERT INTO enrichment_queue(event_id) VALUES(1);",
    )
    .unwrap();
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES - 1)
            .unwrap()
            .events_cleared,
        0
    );
    assert_eq!(
        scalar(&conn, "SELECT COUNT(*) FROM events WHERE stdout != ''"),
        1
    );
    let before: (String, i64, i64, String, String, String, i64) = conn
        .query_row(
            "SELECT command,timestamp,duration_ms,cwd,git_repo,git_branch,created_at FROM events",
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
    let report = storage::gc_event_payloads(&conn, EXPIRES).unwrap();
    assert_eq!(report.events_cleared, 1);
    assert_eq!(report.checkpoint, PayloadCheckpoint::Truncated);
    assert_eq!(scalar(&conn, "PRAGMA secure_delete"), 1);
    assert_eq!(scalar(&conn, "SELECT COUNT(*) FROM events"), 1);
    assert_eq!(
        scalar(&conn, "SELECT COUNT(*) FROM events WHERE stdout != ''"),
        0
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM events WHERE stdout IS NULL AND stderr IS NULL"
        ),
        1
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM knowledge_node_events JOIN events ON events.id=event_id"
        ),
        1
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM enrichment_queue JOIN events ON events.id=event_id"
        ),
        1
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'derived'"
        ),
        1
    );
    let after = conn
        .query_row(
            "SELECT command,timestamp,duration_ms,cwd,git_repo,git_branch,created_at FROM events",
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
    assert_eq!(before, after);
    let receipts: Vec<(String, i64, String, String)> = conn
        .prepare("SELECT content_hash,ts,reason,what FROM deletion_receipts ORDER BY id")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    assert_eq!(
        receipts,
        vec![
            (
                Sha256::digest(b"synthetic output")
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect(),
                EXPIRES,
                "payload_expired".into(),
                "events/1/stdout".into()
            ),
            (
                Sha256::digest(b"synthetic error")
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect(),
                EXPIRES,
                "payload_expired".into(),
                "events/1/stderr".into()
            ),
        ]
    );
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES)
            .unwrap()
            .events_cleared,
        0
    );
    assert_eq!(scalar(&conn, "SELECT COUNT(*) FROM deletion_receipts"), 2);
    for sql in [
        "UPDATE deletion_receipts SET reason='changed'",
        "DELETE FROM deletion_receipts",
        "INSERT OR REPLACE INTO deletion_receipts VALUES(1,'changed',0,'changed','changed')",
    ] {
        assert!(conn.execute(sql, []).is_err());
    }
}

#[test]
fn additive_migration_grandfathers_lazily_and_direct_and_capture_writes_set_expiry() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("old.db");
    let conn = storage::open_db(&path).unwrap();
    seed(&conn, 1, EXPIRES, None);
    conn.execute_batch(
        "DROP TRIGGER events_payload_expiry_on_insert;
         DROP INDEX idx_events_payload_expiry;
         ALTER TABLE events DROP COLUMN payload_expiry;
         DROP TABLE deletion_receipts;
         PRAGMA user_version=25;",
    )
    .unwrap();
    drop(conn);
    let conn = storage::open_db(&path).unwrap();
    assert_eq!(
        scalar(&conn, "PRAGMA user_version"),
        storage::EXPECTED_VERSION
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM events WHERE payload_expiry IS NULL"
        ),
        1
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COALESCE(payload_expiry,created_at+7776000000) FROM events WHERE id=1"
        ),
        EXPIRES
    );
    drop(conn);
    let conn = storage::open_db(&path).unwrap();
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES - 1)
            .unwrap()
            .events_cleared,
        0
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM events WHERE payload_expiry IS NULL"
        ),
        1
    );
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES)
            .unwrap()
            .events_cleared,
        1
    );
    assert_eq!(
        scalar(&conn, "SELECT payload_expiry FROM events WHERE id=1"),
        EXPIRES
    );
    conn.execute_batch("PRAGMA user_version=25;").unwrap(); // partial-success rerun
    drop(conn);
    let conn = storage::open_db(&path).unwrap();
    let id = storage::insert_event_at(&conn, 1, &event(), 1, 0, None, Some("new"), None).unwrap();
    assert_eq!(
        conn.query_row(
            "SELECT payload_expiry-created_at FROM events WHERE id=?1",
            [id],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        EVENT_PAYLOAD_RETENTION_MS
    );
    conn.execute_batch("INSERT INTO events(session_id,timestamp,command,duration_ms,cwd,hostname,shell,created_at) VALUES(1,0,'direct',0,'/tmp','test','zsh',100);").unwrap();
    assert_eq!(
        scalar(
            &conn,
            "SELECT payload_expiry FROM events WHERE command='direct'"
        ),
        100 + EVENT_PAYLOAD_RETENTION_MS
    );
}

#[test]
fn published_evidence_refs_pin_events_but_enrichment_links_do_not() {
    let dir = tempfile::tempdir().unwrap();
    let conn = storage::open_db(&dir.path().join("pins.db")).unwrap();
    for id in 1..=4 {
        seed(&conn, id, EXPIRES, None);
    }
    // Synthetic stand-ins only. Production migrations do not create either store.
    conn.execute_batch(
        "CREATE TABLE node_evidence(node_id INTEGER,ref_type TEXT,ref_id INTEGER);
         INSERT INTO node_evidence VALUES(1,'shell',1),(1,'browser',3);
         CREATE TABLE epitaphs(evidence_refs TEXT);
         INSERT INTO epitaphs VALUES('[\"shell-2\",\"claude-3\"]');
         INSERT INTO knowledge_nodes(id,uuid,content,embed_text) VALUES(1,'ordinary','{}','ordinary');
         INSERT INTO knowledge_node_events VALUES(1,4);",
    ).unwrap();
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES)
            .unwrap()
            .events_cleared,
        2
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM events WHERE id IN (1,2) AND stdout IS NOT NULL"
        ),
        2
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM events WHERE id IN (3,4) AND stdout IS NULL"
        ),
        2
    );
}

#[test]
fn incompatible_or_malformed_evidence_stops_destruction() {
    for ddl in [
        "CREATE TABLE node_evidence(ref_type TEXT,ref_id INTEGER); INSERT INTO node_evidence VALUES('unknown',1);",
        "CREATE TABLE node_evidence(other TEXT);",
        "CREATE TABLE epitaphs(evidence_refs TEXT); INSERT INTO epitaphs VALUES('broken');",
        "CREATE TABLE epitaphs(evidence_refs TEXT); INSERT INTO epitaphs VALUES('[\"shell-1x\"]');",
        "CREATE TABLE epitaphs(evidence_refs TEXT); INSERT INTO epitaphs VALUES('[{\"ref\":\"shell-1\"}]');",
        "CREATE TABLE epitaphs(project TEXT);",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let conn = storage::open_db(&dir.path().join("incompatible.db")).unwrap();
        seed(&conn, 1, EXPIRES, None);
        conn.execute_batch(ddl).unwrap();
        let error = storage::gc_event_payloads(&conn, EXPIRES).unwrap_err();
        assert!(format!("{error:#}").contains("incompatible"));
        assert_eq!(
            scalar(
                &conn,
                "SELECT COUNT(*) FROM events WHERE stdout IS NOT NULL"
            ),
            1
        );
        assert_eq!(scalar(&conn, "SELECT COUNT(*) FROM deletion_receipts"), 0);
    }
}

#[test]
fn shared_environment_survives_until_last_unpinned_reference_and_is_restored_atomically() {
    let dir = tempfile::tempdir().unwrap();
    let conn = storage::open_db(&dir.path().join("env.db")).unwrap();
    let ev = event();
    let env_id = storage::upsert_env_snapshot(&conn, &ev.env_snapshot)
        .unwrap()
        .unwrap();
    seed(&conn, 1, EXPIRES, Some(env_id));
    seed(&conn, 2, EXPIRES + 1, Some(env_id));
    seed(&conn, 3, EXPIRES, Some(env_id));
    conn.execute_batch("CREATE TABLE node_evidence(ref_type TEXT,ref_id INTEGER); INSERT INTO node_evidence VALUES('shell',3);").unwrap();
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES)
            .unwrap()
            .events_cleared,
        1
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM env_snapshots WHERE env_json != ''"
        ),
        1
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM deletion_receipts WHERE what LIKE 'env_snapshots/%'"
        ),
        0
    );
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES + 1)
            .unwrap()
            .events_cleared,
        1
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM env_snapshots WHERE env_json != ''"
        ),
        1
    );
    conn.execute_batch("DELETE FROM node_evidence;").unwrap();
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES + 1)
            .unwrap()
            .events_cleared,
        1
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM env_snapshots WHERE env_json = ''"
        ),
        1
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM deletion_receipts WHERE what LIKE 'env_snapshots/%/env_json'"
        ),
        1
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM deletion_receipts WHERE what LIKE 'events/%/env_snapshot_id'"
        ),
        3
    );
    let id = storage::insert_event_at(&conn, 1, &ev, 0, 0, None, Some("restore"), None).unwrap();
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM env_snapshots WHERE env_json != ''"
        ),
        1
    );
    assert_eq!(
        conn.query_row(
            "SELECT env_snapshot_id FROM events WHERE id=?1",
            [id],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        env_id
    );
    conn.execute(
        "UPDATE events SET payload_expiry=?1 WHERE id=?2",
        rusqlite::params![EXPIRES, id],
    )
    .unwrap();
    storage::gc_event_payloads(&conn, EXPIRES).unwrap();
    assert_eq!(
        storage::insert_event_at(&conn, 1, &ev, 0, 0, None, Some("restore"), None).unwrap(),
        -1
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM env_snapshots WHERE env_json = ''"
        ),
        1
    );
}

#[test]
fn receipt_and_payload_failures_roll_back_the_whole_batch() {
    for trigger in [
        "CREATE TRIGGER fail_receipt BEFORE INSERT ON deletion_receipts WHEN NEW.what LIKE '%stderr' BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END;",
        "CREATE TRIGGER fail_payload BEFORE UPDATE OF stdout ON events BEGIN SELECT RAISE(ABORT,'synthetic payload failure'); END;",
        "CREATE TRIGGER fail_env BEFORE UPDATE OF env_json ON env_snapshots BEGIN SELECT RAISE(ABORT,'synthetic env failure'); END;",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let conn = storage::open_db(&dir.path().join("failure.db")).unwrap();
        let env_id = storage::upsert_env_snapshot(&conn, &event().env_snapshot).unwrap();
        seed(&conn, 1, EXPIRES, env_id);
        conn.execute_batch(trigger).unwrap();
        assert!(storage::gc_event_payloads(&conn, EXPIRES).is_err());
        assert_eq!(scalar(&conn, "SELECT COUNT(*) FROM deletion_receipts"), 0);
        assert_eq!(
            scalar(
                &conn,
                "SELECT COUNT(*) FROM events WHERE stdout IS NOT NULL AND env_snapshot_id IS NOT NULL"
            ),
            1
        );
        assert_eq!(
            scalar(
                &conn,
                "SELECT COUNT(*) FROM env_snapshots WHERE env_json != ''"
            ),
            1
        );
    }
}

#[test]
fn gc_covers_all_signed_sqlite_row_ids_without_cursor_overflow() {
    let dir = tempfile::tempdir().unwrap();
    let conn = storage::open_db(&dir.path().join("row-ids.db")).unwrap();
    for id in [i64::MIN, 0, i64::MAX].into_iter().chain(1..=97) {
        seed(&conn, id, EXPIRES, None);
    }
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES)
            .unwrap()
            .events_cleared,
        100
    );
    assert_eq!(
        scalar(&conn, "SELECT COUNT(*) FROM events WHERE stdout IS NULL"),
        100
    );
    assert_eq!(scalar(&conn, "SELECT COUNT(*) FROM deletion_receipts"), 200);
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES)
            .unwrap()
            .events_cleared,
        0
    );
}

#[test]
fn later_batch_failure_preserves_receipts_for_only_committed_batches() {
    let dir = tempfile::tempdir().unwrap();
    let conn = storage::open_db(&dir.path().join("batches.db")).unwrap();
    for id in 1..=201 {
        seed(&conn, id, EXPIRES, None);
    }
    conn.execute_batch("CREATE TRIGGER fail_second_batch BEFORE UPDATE OF stdout ON events WHEN OLD.id=101 BEGIN SELECT RAISE(ABORT,'synthetic second batch failure'); END;").unwrap();
    let error = storage::gc_event_payloads(&conn, EXPIRES).unwrap_err();
    assert!(format!("{error:#}").contains("100 committed event clears"));
    assert_eq!(
        scalar(&conn, "SELECT COUNT(*) FROM events WHERE stdout IS NULL"),
        100
    );
    assert_eq!(scalar(&conn, "SELECT COUNT(*) FROM deletion_receipts"), 200);
    conn.execute_batch("DROP TRIGGER fail_second_batch;")
        .unwrap();
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES)
            .unwrap()
            .events_cleared,
        101
    );
    assert_eq!(scalar(&conn, "SELECT COUNT(*) FROM deletion_receipts"), 402);
}

#[test]
fn concurrent_capture_and_busy_checkpoint_do_not_claim_wal_erasure() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("concurrent.db");
    let conn = storage::open_db(&path).unwrap();
    let env_id = storage::upsert_env_snapshot(&conn, &event().env_snapshot).unwrap();
    for id in 1..=201 {
        seed(&conn, id, EXPIRES, env_id);
    }
    let writer_conn = storage::open_db(&path).unwrap();
    let reader = storage::open_db(&path).unwrap();
    reader.execute_batch("BEGIN;").unwrap();
    assert_eq!(scalar(&reader, "SELECT COUNT(*) FROM events"), 201);
    conn.busy_timeout(std::time::Duration::from_millis(100))
        .unwrap();
    let writer = std::thread::spawn(move || {
        let conn = writer_conn;
        for id in 0..20 {
            storage::insert_event_at(
                &conn,
                1,
                &event(),
                0,
                0,
                None,
                Some(&format!("writer-{id}")),
                None,
            )
            .unwrap();
        }
    });
    let report = storage::gc_event_payloads(&conn, EXPIRES).unwrap();
    writer.join().unwrap();
    assert_eq!(report.events_cleared, 201);
    assert_eq!(report.checkpoint, PayloadCheckpoint::Busy);
    assert_eq!(scalar(&conn, "SELECT COUNT(*) FROM events"), 221);
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM events WHERE stdout IS NOT NULL"
        ),
        20
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM deletion_receipts WHERE what LIKE 'events/%'"
        ),
        603
    );
    assert_eq!(
        scalar(
            &conn,
            "SELECT COUNT(*) FROM events JOIN env_snapshots ON env_snapshot_id=env_snapshots.id WHERE env_json != ''"
        ),
        20
    );
    assert!(
        std::fs::metadata(path.with_extension("db-wal"))
            .unwrap()
            .len()
            > 0
    );
    reader.execute_batch("ROLLBACK;").unwrap();
    assert_eq!(
        storage::gc_event_payloads(&conn, EXPIRES)
            .unwrap()
            .checkpoint,
        PayloadCheckpoint::Truncated
    );
    assert_eq!(
        std::fs::metadata(path.with_extension("db-wal"))
            .unwrap()
            .len(),
        0
    );
}
