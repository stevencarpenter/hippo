//! Skeletons for the capture-reliability invariants (I-1, I-2, I-7, I-8,
//! I-10) that cannot be exercised against `main` today because they depend
//! on infrastructure documented in `docs/capture/architecture.md` (the live
//! reference) and the historical specs in
//! `docs/archive/capture-reliability-overhaul/{01-source-health.md,
//! 04-watchdog.md, 05-synthetic-probes.md}` plus the FS watcher (design
//! archived at `docs/archive/capture-reliability-overhaul/06-claude-session-watcher.md`;
//! shipped in PR #86) and not yet wired in here.
//!
//! Most tests in this file are `#[ignore]` skeletons with an explanation
//! pointing at the blocking roadmap task (see
//! `docs/archive/capture-reliability-overhaul/07-roadmap.md`); they are
//! committed so that when each P0/P1/P2 phase lands, the test is a one-line
//! enable, not a "remember to write this later" TODO. The exception is the
//! I-14 (embedding orphan backlog) tests at the end of the file, which are
//! live, runnable tests.
//!
//! Tracking: docs/capture/test-matrix.md rows F-10..F-14.

// ============================================================================
// F-11 / I-1 — shell liveness
// ============================================================================

#[test]
#[ignore = "blocked on P0.1 (source_health table) — the test cannot run until \
            the daemon writes `source_health WHERE source='shell'` on every \
            flush. Once the schema + write path land (01-source-health.md, \
            P0.1 in 07-roadmap.md), remove this attribute and fill in the \
            body using the daemon test harness."]
fn i1_shell_liveness() {
    // Given: daemon running, fresh source_health table.
    // When: 3 shell events flushed through send_event_fire_and_forget.
    // Then: SELECT FROM source_health WHERE source='shell' shows
    //         last_event_ts within 1 s of now_ms
    //         consecutive_failures = 0
    //         events_last_1h increased by >=3
    //         probe_ok = 1
    unimplemented!("implement once P0.1 lands");
}

#[test]
#[ignore = "blocked on P0.1 — source_health.probe_ok must be 0 when zsh not \
            running, per I-1 suppression rules in 02-invariants.md"]
fn i1_shell_liveness_suppressed_when_no_zsh_process() {
    // Given: no zsh process (watchdog probe sets probe_ok=0).
    // When: 60+ seconds pass with no events.
    // Then: source_health row has probe_ok=0 and the invariant does not fire.
    unimplemented!();
}

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

// ============================================================================
// F-10 / I-2 — Claude-session end-to-end
// ============================================================================

#[test]
#[ignore = "blocked on P0.1 (source_health). The FS-watched session ingester \
            shipped (see crates/hippo-daemon/src/watch_claude_sessions.rs); once \
            source_health is wired in, drive the watcher with a JSONL that grows \
            over time and assert the claude_sessions row appears within 5 min + \
            source_health updates."]
fn i2_claude_session_end_to_end() {
    // Given: a JSONL under ~/.claude/projects/<p>/<id>.jsonl with mtime < 5 min.
    // When: the FS-watched session ingester processes it.
    // Then: claude_sessions has a row with matching session_id
    //       source_health WHERE source='claude-session' is fresh.
    unimplemented!();
}

// ============================================================================
// F-13 / I-7 — watchdog heartbeat
// ============================================================================

#[test]
#[ignore = "blocked on P1.1 (watchdog process, 04-watchdog.md). When the \
            watchdog lands, verify its heartbeat row in source_health \
            updates within 60 s and goes stale (> 180 s) if the watchdog \
            is killed."]
fn i7_watchdog_heartbeat() {
    // Given: watchdog process running.
    // When: 60 seconds pass.
    // Then: source_health WHERE source='watchdog' has last_event_ts within 60 s of now_ms.
    // Then (negative): kill watchdog; after 180 s, doctor flags [!!] watchdog stale.
    unimplemented!();
}

// ============================================================================
// F-12 / I-8 — synthetic probe round-trip
// ============================================================================

#[test]
#[ignore = "blocked on P2.2 (synthetic probes, 05-synthetic-probes.md). When \
            probes land, inject a synthetic shell event with probe_tag set \
            and assert it round-trips into events + updates source_health \
            probe_latency_ms within 15 min threshold."]
fn i8_probe_round_trip() {
    // Given: probe scheduler running, synthetic event injected with probe_tag.
    // When: daemon processes it.
    // Then: events row exists with probe_tag matching; source_health.probe_latency_ms < 15 min;
    //       probe event is NOT exposed in user-facing queries (RAG, hippo ask).
    unimplemented!();
}

// ============================================================================
// F-14 / I-10 — capture decoupled from enrichment
// ============================================================================

#[test]
#[ignore = "blocked on P0.2 (source_health writes on every capture path). The \
            test kills the brain process, then sends shell events; \
            source_health must still update (capture is independent of \
            enrichment). When P0.2 lands, enable this and assert the \
            decoupling contract."]
fn i10_decoupled_from_brain() {
    // Given: daemon running, brain DOWN.
    // When: 3 shell events flushed.
    // Then: source_health WHERE source='shell' shows fresh last_event_ts
    //       (enrichment-queue depth may grow, but capture health is green).
    unimplemented!();
}
