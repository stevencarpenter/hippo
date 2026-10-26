//! Agent-eval ingest contract (design section 4): validation reasons, the
//! `id_conflict` comparison rule, parents, caps, and batch failure handling.

use hippo_core::redaction::RedactionEngine;
use hippo_core::storage;
use hippo_daemon::agent_eval::{IngestSummary, ingest};
use rusqlite::Connection;
use serde_json::{Value, json};

const SHA: &str = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567";
const NOW: i64 = 1_800_000_000_000;

fn db() -> (tempfile::TempDir, Connection) {
    let dir = tempfile::tempdir().unwrap();
    let conn = storage::open_db(&dir.path().join("hippo.db")).unwrap();
    (dir, conn)
}

fn run(conn: &mut Connection, records: &[Value]) -> IngestSummary {
    let input: String = records.iter().map(|r| format!("{r}\n")).collect();
    ingest(conn, input.as_bytes(), &[RedactionEngine::builtin()], NOW)
}

fn review_run(id: &str) -> Value {
    json!({"schema_version":1,"type":"review_run","id":id,"agent":"blind-review",
        "mode":"mechanical","repo":"/home/dev/example-repo","base":"main",
        "base_sha":SHA,"head_sha":SHA,"input_sha256":"ab","started_at":1,"finished_at":2})
}

fn finding(id: &str, run_id: &str) -> Value {
    json!({"schema_version":1,"type":"finding","id":id,"run_id":run_id,
        "repo":"/home/dev/example-repo","path":"src/a.rs","side":"+","line":4,
        "line_text":"  let x = 1;","severity":"low","confidence":"medium",
        "claim":"x is unused.","logged_at":3})
}

fn verdict(id: &str, finding_id: &str) -> Value {
    json!({"schema_version":1,"type":"verdict","id":id,"finding_id":finding_id,
        "verdict":"accepted","auto":true,"decided_at":5})
}

fn bench_run(id: &str) -> Value {
    json!({"schema_version":1,"type":"bench_run","id":id,"bench":"b","recorded_at":1,
        "repo_commit":"686e67b","model":"haiku","reps":3,"cases":2,"cases_sha256":"c",
        "brief_sha256":"d","scores":{"recall":1,"jaccard":0.5},
        "per_case":[{"case":"c01","extra":0}]})
}

fn with(mut record: Value, key: &str, value: Value) -> Value {
    record[key] = value;
    record
}

fn without(mut record: Value, key: &str) -> Value {
    record.as_object_mut().unwrap().remove(key);
    record
}

fn reasons(summary: &IngestSummary) -> Vec<(usize, &str)> {
    summary
        .rejections
        .iter()
        .map(|(n, r)| (*n, r.as_str()))
        .collect()
}

#[test]
fn every_validation_reason_is_reported_per_line_and_processing_continues() {
    let (_dir, mut conn) = db();
    let records = [
        review_run("r1"),
        with(review_run("r2"), "schema_version", json!(2)),
        with(review_run("r3"), "schema_version", json!("1")),
        with(review_run("r4"), "type", json!("review")),
        without(finding("f1", "r1"), "claim"),
        with(review_run("r5"), "base_sha", json!("abc1234")),
        with(review_run("r6"), "head_sha", json!(SHA.to_uppercase())),
        with(finding("f2", "r1"), "side", json!("~")),
        with(finding("f3", "r1"), "severity", json!("critical")),
        with(verdict("v1", "f9"), "verdict", json!("fixed")),
        with(verdict("v2", "f9"), "auto", json!("false")),
        with(review_run("bad id"), "agent", json!("a")),
        with(review_run("r7"), "session_id", json!("s")),
        with(
            with(review_run("r8"), "session_id", json!("s")),
            "harness",
            json!("vim"),
        ),
        with(review_run("r9"), "started_at", json!(1.5)),
        with(bench_run("b1"), "scores", json!([1])),
        without(bench_run("b2"), "scores"),
        with(finding("f4", "r1"), "line", json!("4")),
    ];
    let summary = run(&mut conn, &records);
    assert_eq!(summary.written, 1);
    assert_eq!(
        reasons(&summary),
        vec![
            (2, "unknown_schema_version"),
            (3, "invalid_field:schema_version"),
            (4, "unknown_type"),
            (5, "missing_field:claim"),
            (6, "invalid_field:base_sha"),
            (7, "invalid_field:head_sha"),
            (8, "invalid_field:side"),
            (9, "invalid_field:severity"),
            (10, "invalid_field:verdict"),
            (11, "invalid_field:auto"),
            (12, "invalid_field:id"),
            (13, "missing_field:harness"),
            (14, "invalid_field:harness"),
            (15, "invalid_field:started_at"),
            (16, "invalid_field:scores"),
            (17, "missing_field:scores"),
            (18, "invalid_field:line"),
        ]
    );
}

#[test]
fn line_too_long_and_blank_lines_keep_file_line_numbers() {
    let (_dir, mut conn) = db();
    let long = format!(
        "{}\n\n{}\n",
        with(review_run("r1"), "agent", json!("x".repeat(1 << 20))),
        review_run("r2")
    );
    let summary = ingest(
        &mut conn,
        long.as_bytes(),
        &[RedactionEngine::builtin()],
        NOW,
    );
    assert_eq!(summary.written, 1);
    assert_eq!(reasons(&summary), vec![(1, "line_too_long")]);
}

#[test]
fn id_conflict_compares_stored_fields_but_not_redacted_or_derived_ones() {
    let (_dir, mut conn) = db();
    let base = [
        review_run("r1"),
        finding("f1", "r1"),
        verdict("v1", "f1"),
        bench_run("b1"),
    ];
    assert_eq!(run(&mut conn, &base).written, 4);

    let equal = [
        // Explicit column defaults equal omitted optional fields.
        with(
            with(
                with(review_run("r1"), "model_id", json!("")),
                "units_total",
                json!(0),
            ),
            "cli_version",
            json!(""),
        ),
        // Redacted text is never compared; a producer hash is ignored.
        with(
            with(finding("f1", "r1"), "claim", json!("Different words.")),
            "line_hash",
            json!("ff"),
        ),
        with(verdict("v1", "f1"), "note", json!("a note added later")),
        // JSON values compare parsed, numbers as f64, keys unordered.
        with(
            with(
                bench_run("b1"),
                "scores",
                json!({"jaccard":0.50,"recall":1.0}),
            ),
            "model_ids",
            json!([]),
        ),
    ];
    let summary = run(&mut conn, &equal);
    assert_eq!((summary.written, summary.skipped), (0, 4), "{summary:?}");
    assert!(summary.rejections.is_empty(), "{summary:?}");

    let differing = [
        with(review_run("r1"), "units_failed", json!(1)),
        with(finding("f1", "r1"), "severity", json!("high")),
        with(finding("f1", "r1"), "category", json!("unknown-other")),
        with(verdict("v1", "f1"), "auto", json!(false)),
        with(verdict("v1", "f1"), "commit", json!(SHA)),
        with(
            bench_run("b1"),
            "scores",
            json!({"recall":1,"jaccard":0.51}),
        ),
    ];
    let summary = run(&mut conn, &differing);
    assert_eq!(summary.written + summary.skipped, 0);
    assert!(
        summary.rejections.iter().all(|(_, r)| r == "id_conflict"),
        "{summary:?}"
    );
    assert_eq!(summary.rejections.len(), differing.len());
}

#[test]
fn unknown_parent_is_rejected_until_the_parent_arrives() {
    let (_dir, mut conn) = db();
    let orphans = [finding("f1", "r1"), verdict("v1", "f1")];
    let summary = run(&mut conn, &orphans);
    assert_eq!(
        reasons(&summary),
        vec![(1, "unknown_parent"), (2, "unknown_parent")]
    );
    let ledger = [review_run("r1"), finding("f1", "r1"), verdict("v1", "f1")];
    assert_eq!(run(&mut conn, &ledger).written, 3);
}

#[test]
fn free_text_is_redacted_before_it_is_capped() {
    let (_dir, mut conn) = db();
    let token = format!("ghp_{}", "Ab1".repeat(12));
    // The token straddles the 4,000-character claim cap.
    let claim = format!("{}{token}{}", "c".repeat(3990), "d".repeat(2000));
    let records = [
        review_run("r1"),
        with(
            with(
                with(finding("f1", "r1"), "claim", json!(claim)),
                "line_text",
                json!("l".repeat(1500)),
            ),
            "category",
            json!("k".repeat(100)),
        ),
    ];
    assert_eq!(run(&mut conn, &records).written, 2);
    let (claim, line_text, category): (String, String, String) = conn
        .query_row(
            "SELECT claim, line_text, category FROM eval_findings",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(claim.chars().count(), 4000);
    assert!(claim.ends_with("[REDACTED]"), "{}", &claim[3980..]);
    assert!(!claim.contains("ghp_"));
    assert_eq!(line_text.chars().count(), 1000);
    assert_eq!(category.chars().count(), 64);
}

#[test]
fn a_failed_batch_is_db_error_and_later_batches_still_commit() {
    let (_dir, mut conn) = db();
    conn.execute_batch("DROP TABLE eval_bench_runs;").unwrap();
    // Batch 1: lines 1..=100 hold a bench_run, so the whole batch rolls back.
    // Batch 2: lines 101..=102 commit.
    let mut records = vec![review_run("r0"), bench_run("b1")];
    records.extend((2..100).map(|i| review_run(&format!("r{i}"))));
    records.extend([review_run("late-1"), review_run("late-2")]);
    let summary = run(&mut conn, &records);
    assert_eq!(summary.written, 2);
    assert_eq!(summary.rejections.len(), 100);
    assert!(summary.rejections.iter().all(|(_, r)| r == "db_error"));
    let late: i64 = conn
        .query_row(
            "SELECT count(*) FROM eval_review_runs WHERE run_id LIKE 'late-%'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(late, 2);
}

#[test]
fn findings_in_a_later_batch_find_their_run_from_an_earlier_one() {
    let (_dir, mut conn) = db();
    let mut records = vec![review_run("r1")];
    records.extend((0..150).map(|i| finding(&format!("f{i}"), "r1")));
    let summary = run(&mut conn, &records);
    assert_eq!((summary.written, summary.rejections.len()), (151, 0));
    let summary = run(&mut conn, &records);
    assert_eq!((summary.written, summary.skipped), (0, 151));
}
