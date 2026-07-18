//! DB-backed capture invariants that are best expressed as standalone
//! integration tests. The obsolete P0/P1/P2 `#[ignore]` skeletons were retired
//! after those paths gained active coverage in the daemon, watcher, probe, and
//! consolidated doctor suites.
//!
//! Tracking: docs/capture/test-matrix.md rows F-10..F-14.

// ============================================================================
// I-14 — embedding orphan backlog
// ============================================================================

#[test]
fn i14_embedding_orphans_alarms_over_threshold() {
    use hippo_daemon::watchdog::check_i14_embedding_orphans;
    let conn = rusqlite::Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE knowledge_nodes (id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL);
         CREATE TABLE knowledge_vectors_rowids (rowid INTEGER PRIMARY KEY, id, chunk_id, chunk_offset);",
    )
    .unwrap();
    let now_ms: i64 = 10_000_000;
    let old = now_ms - 3_600_000; // 1h old — well past staleness
    // 3 orphan nodes, none embedded.
    for id in 1..=3 {
        conn.execute(
            "INSERT INTO knowledge_nodes (id, created_at) VALUES (?1, ?2)",
            rusqlite::params![id, old],
        )
        .unwrap();
    }
    // threshold 2 -> 3 orphans must alarm.
    let v = check_i14_embedding_orphans(&conn, now_ms, 900_000, 2).unwrap();
    assert!(v.is_some());
    assert_eq!(v.unwrap().invariant_id, "I-14");

    // threshold 5 -> 3 orphans must NOT alarm.
    assert!(
        check_i14_embedding_orphans(&conn, now_ms, 900_000, 5)
            .unwrap()
            .is_none()
    );
}

#[test]
fn i14_embedding_orphans_silent_when_shadow_table_absent() {
    use hippo_daemon::watchdog::check_i14_embedding_orphans;
    let conn = rusqlite::Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE knowledge_nodes (id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL);",
    )
    .unwrap();
    // No knowledge_vectors_rowids table -> fresh install -> must not alarm.
    assert!(
        check_i14_embedding_orphans(&conn, 10_000_000, 900_000, 0)
            .unwrap()
            .is_none()
    );
}

// ============================================================================
// I-16 — duplicate agentic knowledge nodes (re-enrichment dedup regression)
// ============================================================================

/// Minimal schema for the I-16 query: knowledge_nodes + the agentic shadow
/// table. Only the columns the invariant's GROUP BY touches are modeled.
fn create_i16_schema(conn: &rusqlite::Connection) {
    conn.execute_batch(
        "CREATE TABLE knowledge_nodes (
             id         INTEGER PRIMARY KEY,
             content    TEXT NOT NULL,
             embed_text TEXT NOT NULL,
             node_type  TEXT NOT NULL DEFAULT 'observation'
         );
         CREATE TABLE knowledge_node_agentic_sessions (
             knowledge_node_id  INTEGER NOT NULL,
             agentic_session_id INTEGER NOT NULL,
             PRIMARY KEY (knowledge_node_id, agentic_session_id)
         );",
    )
    .unwrap();
}

/// Insert a knowledge node and link it to an agentic session segment.
fn insert_i16_node(
    conn: &rusqlite::Connection,
    node_id: i64,
    agentic_session_id: i64,
    content: &str,
    embed_text: &str,
    node_type: &str,
) {
    conn.execute(
        "INSERT INTO knowledge_nodes (id, content, embed_text, node_type)
         VALUES (?1, ?2, ?3, ?4)",
        rusqlite::params![node_id, content, embed_text, node_type],
    )
    .unwrap();
    conn.execute(
        "INSERT INTO knowledge_node_agentic_sessions (knowledge_node_id, agentic_session_id)
         VALUES (?1, ?2)",
        rusqlite::params![node_id, agentic_session_id],
    )
    .unwrap();
}

#[test]
fn i16_duplicate_agentic_nodes_alarms_over_threshold() {
    use hippo_daemon::watchdog::check_i16_duplicate_agentic_nodes;
    let conn = rusqlite::Connection::open_in_memory().unwrap();
    create_i16_schema(&conn);

    // Session 1 carries TWO byte-identical observation nodes -> one duplicate
    // group. (I-16 is scoped to node_type='observation' — the class Fix B guards.)
    insert_i16_node(&conn, 1, 100, "fixed the bug", "embed-A", "observation");
    insert_i16_node(&conn, 2, 100, "fixed the bug", "embed-A", "observation");

    // threshold 0 -> any duplicate group must alarm.
    let v = check_i16_duplicate_agentic_nodes(&conn, 10_000_000, 0).unwrap();
    assert!(v.is_some(), "1 duplicate group must alarm at threshold 0");
    let v = v.unwrap();
    assert_eq!(v.invariant_id, "I-16");
    assert_eq!(v.source, "enrichment");

    // threshold 1 -> a single duplicate group is at-threshold, must NOT alarm.
    assert!(
        check_i16_duplicate_agentic_nodes(&conn, 10_000_000, 1)
            .unwrap()
            .is_none(),
        "1 duplicate group at threshold 1 must not alarm (strictly-greater gate)"
    );
}

#[test]
fn i16_covers_change_outcome_duplicates() {
    // Now that the workflow enricher is guarded by write-time content dedup,
    // I-16 covers ALL node types (no longer scoped to 'observation'). A duplicate
    // change_outcome group within one segment indicates the workflow guard
    // regressed and MUST alarm. See AP-13.
    use hippo_daemon::watchdog::check_i16_duplicate_agentic_nodes;
    let conn = rusqlite::Connection::open_in_memory().unwrap();
    create_i16_schema(&conn);

    insert_i16_node(&conn, 1, 100, "ci passed", "embed-A", "change_outcome");
    insert_i16_node(&conn, 2, 100, "ci passed", "embed-A", "change_outcome");

    assert!(
        check_i16_duplicate_agentic_nodes(&conn, 10_000_000, 0)
            .unwrap()
            .is_some(),
        "duplicate change_outcome groups must alarm now that workflow is guarded"
    );
}

#[test]
fn i16_clean_corpus_does_not_alarm() {
    use hippo_daemon::watchdog::check_i16_duplicate_agentic_nodes;
    let conn = rusqlite::Connection::open_in_memory().unwrap();
    create_i16_schema(&conn);

    // Distinct content within a session -> not a duplicate group.
    insert_i16_node(&conn, 1, 100, "fixed the bug", "embed-A", "lesson");
    insert_i16_node(&conn, 2, 100, "added a test", "embed-B", "lesson");

    // Identical content but in DIFFERENT sessions -> benign, NOT a duplicate
    // group (the GROUP BY is per-agentic_session_id, not global).
    insert_i16_node(&conn, 3, 200, "ran cargo test", "embed-C", "observation");
    insert_i16_node(&conn, 4, 300, "ran cargo test", "embed-C", "observation");

    assert!(
        check_i16_duplicate_agentic_nodes(&conn, 10_000_000, 0)
            .unwrap()
            .is_none(),
        "distinct intra-session content and cross-session identical content must not alarm"
    );
}

#[test]
fn i16_silent_when_shadow_table_absent() {
    use hippo_daemon::watchdog::check_i16_duplicate_agentic_nodes;
    let conn = rusqlite::Connection::open_in_memory().unwrap();
    // Only knowledge_nodes exists; no knowledge_node_agentic_sessions table.
    conn.execute_batch(
        "CREATE TABLE knowledge_nodes (
             id INTEGER PRIMARY KEY, content TEXT, embed_text TEXT, node_type TEXT);",
    )
    .unwrap();
    // Fresh install (no agentic enrichment yet) -> must not panic, must not alarm.
    assert!(
        check_i16_duplicate_agentic_nodes(&conn, 10_000_000, 0)
            .unwrap()
            .is_none(),
        "missing shadow table must stay silent on a fresh install"
    );
}
