//! `hippo eval export-bench-cases`: case format, anchors, redaction, skips,
//! and the inside-checkout guard.

use std::path::{Path, PathBuf};
use std::process::Command;

use hippo_core::redaction::RedactionEngine;
use hippo_core::storage;
use hippo_daemon::agent_eval::ingest;
use hippo_daemon::eval_export::export_bench_cases;
use rusqlite::Connection;
use serde_json::{Value, json};

/// Synthetic credential: `ghp_` plus 36 characters.
const TOKEN: &str = "ghp_abcdefghijklmnopqrstuvwxyz0123456789";
const DEFECTIVE: &str =
    "def check(user):\n    if user.is_admin or True:\n        return True\n    return False\n";
const FIXED: &str =
    "def check(user):\n    if user.is_admin:\n        return True\n    return False\n";

fn git(repo: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(["-c", "user.email=t@example.com", "-c", "user.name=t"])
        .args(["-c", "commit.gpgsign=false"])
        .args(args)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .output()
        .unwrap();
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8(out.stdout).unwrap().trim().to_owned()
}

fn commit(repo: &Path, file: &str, text: &str) -> String {
    let path = repo.join(file);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, text).unwrap();
    git(repo, &["add", "-A"]);
    git(repo, &["commit", "-qm", "c"]);
    git(repo, &["rev-parse", "HEAD"])
}

/// A repo whose `src/auth.py` goes defective (first SHA) then fixed (second).
fn repo_with_fix(root: &Path, defective: &str) -> (PathBuf, String, String) {
    let repo = root.join("repo");
    std::fs::create_dir_all(&repo).unwrap();
    git(&repo, &["init", "-q", "-b", "main"]);
    commit(&repo, "README", "x\n");
    let bad = commit(&repo, "src/auth.py", defective);
    let good = commit(&repo, "src/auth.py", FIXED);
    (repo.canonicalize().unwrap(), bad, good)
}

fn db(dir: &Path) -> Connection {
    storage::open_db(&dir.join("hippo.db")).unwrap()
}

fn load(conn: &mut Connection, records: &[Value]) {
    let input: String = records.iter().map(|r| format!("{r}\n")).collect();
    let summary = ingest(conn, input.as_bytes(), &[RedactionEngine::builtin()], 1_000);
    assert!(summary.rejections.is_empty(), "{:?}", summary.rejections);
}

fn run(repo: &Path, head_sha: &str) -> Value {
    json!({"schema_version":1,"type":"review_run","id":"r1","agent":"blind-review",
        "mode":"mechanical","repo":repo,"base":"main","base_sha":head_sha,
        "head_sha":head_sha,"input_sha256":"ab","started_at":1,"finished_at":2})
}

fn finding(id: &str, repo: &Path, side: &str, line: i64, line_text: &str) -> Value {
    json!({"schema_version":1,"type":"finding","id":id,"run_id":"r1","repo":repo,
        "path":"src/auth.py","side":side,"line":line,"line_text":line_text,
        "severity":"high","confidence":"high","category":"logic",
        "claim":format!("The or True bypasses the check; see {TOKEN}."),"logged_at":3})
}

fn verdict(
    id: &str,
    finding_id: &str,
    verdict: &str,
    auto: bool,
    commit: Option<&str>,
    at: i64,
) -> Value {
    let mut v = json!({"schema_version":1,"type":"verdict","id":id,"finding_id":finding_id,
        "verdict":verdict,"auto":auto,"decided_at":at});
    if let Some(c) = commit {
        v["commit"] = json!(c);
    }
    v
}

fn read_case(out: &Path, id: &str) -> Value {
    serde_json::from_str(&std::fs::read_to_string(out.join(format!("{id}.json"))).unwrap()).unwrap()
}

fn export(conn: &Connection, out: &Path) -> hippo_daemon::eval_export::ExportSummary {
    export_bench_cases(conn, out, &[RedactionEngine::builtin()], 0, None).unwrap()
}

#[test]
fn accepted_finding_becomes_a_redacted_case_with_an_added_row_anchor() {
    let tmp = tempfile::tempdir().unwrap();
    // A credential on a line other than the anchor must not reach the case.
    let defective = DEFECTIVE.replace("return False", &format!("return False  # {TOKEN}"));
    let (repo, bad, good) = repo_with_fix(tmp.path(), &defective);
    let mut conn = db(tmp.path());
    load(
        &mut conn,
        &[
            run(&repo, &bad),
            finding("f1", &repo, "+", 2, "    if user.is_admin or True:"),
            verdict("v1", "f1", "likely-addressed", true, Some(&good), 5),
            verdict("v2", "f1", "accepted", false, None, 6),
        ],
    );
    let out = tmp.path().join("cases");
    let summary = export(&conn, &out);
    assert_eq!(summary.written, 1, "{summary:?}");
    assert!(summary.skipped.is_empty(), "{summary:?}");

    let case = read_case(&out, "f-f1");
    assert_eq!(case["id"], "f-f1");
    assert_eq!(case["base"]["src/auth.py"], FIXED);
    let head = case["head"]["src/auth.py"].as_str().unwrap();
    assert!(
        head.contains("or True") && head.contains("[REDACTED]"),
        "{head}"
    );
    let trap = &case["traps"][0];
    assert_eq!(trap["kind"], "bug");
    assert_eq!(trap["anchors"], json!([["+", 2]]));
    assert_eq!(trap["severity"], "high");
    assert_eq!(trap["category"], "logic");
    let text = std::fs::read_to_string(out.join("f-f1.json")).unwrap();
    assert!(!text.contains(TOKEN), "{text}");

    // A rerun overwrites the same file.
    assert_eq!(export(&conn, &out).written, 1);
    assert_eq!(std::fs::read_dir(&out).unwrap().count(), 1);
}

#[test]
fn moved_flagged_line_is_found_by_its_hash() {
    let tmp = tempfile::tempdir().unwrap();
    let (repo, bad, good) = repo_with_fix(tmp.path(), DEFECTIVE);
    let mut conn = db(tmp.path());
    // The review saw an uncommitted line above, so its line number is 3.
    load(
        &mut conn,
        &[
            run(&repo, &bad),
            finding("f1", &repo, "+", 3, "    if user.is_admin or True:"),
            verdict("v1", "f1", "accepted", false, Some(&good), 5),
        ],
    );
    let out = tmp.path().join("cases");
    assert_eq!(export(&conn, &out).written, 1);
    assert_eq!(
        read_case(&out, "f-f1")["traps"][0]["anchors"],
        json!([["+", 2]])
    );
}

#[test]
fn escape_with_fix_commit_reverts_the_fix() {
    let tmp = tempfile::tempdir().unwrap();
    let (repo, _, good) = repo_with_fix(tmp.path(), DEFECTIVE);
    let mut conn = db(tmp.path());
    load(
        &mut conn,
        &[
            json!({"schema_version":1,"type":"escape","id":"e1","repo":repo,"path":"src/auth.py",
                "line":2,"fix_commit":good,"note":"Admin check always passed.","recorded_at":4}),
            json!({"schema_version":1,"type":"escape","id":"e2","repo":repo,"path":"src/auth.py",
                "line":2,"note":"No fix recorded.","recorded_at":4}),
        ],
    );
    let out = tmp.path().join("cases");
    let summary = export(&conn, &out);
    assert_eq!(summary.written, 1);
    assert_eq!(summary.skipped.get("no_linked_commit"), Some(&1));
    let case = read_case(&out, "e-e1");
    assert_eq!(case["base"]["src/auth.py"], FIXED);
    assert_eq!(case["head"]["src/auth.py"], DEFECTIVE);
    assert_eq!(case["traps"][0]["anchors"], json!([["+", 2]]));
    assert_eq!(case["traps"][0]["why"], "Admin check always passed.");
}

#[test]
fn skips_are_counted_by_reason() {
    let tmp = tempfile::tempdir().unwrap();
    let anchor_secret = DEFECTIVE.replace("or True:", &format!("or True:  # {TOKEN}"));
    let (repo, bad, good) = repo_with_fix(tmp.path(), &anchor_secret);
    let mut conn = db(tmp.path());
    let gone = tmp.path().join("gone");
    let missing_run = json!({"schema_version":1,"type":"review_run","id":"r2",
        "agent":"blind-review","mode":"mechanical","repo":gone,"base":"main",
        "base_sha":bad,"head_sha":bad,"input_sha256":"ab","started_at":1,"finished_at":2});
    let anchor_line = format!("    if user.is_admin or True:  # {TOKEN}");
    let accept =
        |id: &str, f: &str, commit: Option<&str>| verdict(id, f, "accepted", false, commit, 6);
    load(
        &mut conn,
        &[
            run(&repo, &bad),
            missing_run,
            // Redaction would change the anchor line.
            finding("f1", &repo, "+", 2, &anchor_line),
            accept("v1", "f1", Some(&good)),
            // No commit on the accepted verdict and no automatic verdict.
            finding("f2", &repo, "+", 2, &anchor_line),
            accept("v2", "f2", None),
            finding("f3", &repo, "-", 2, &anchor_line),
            accept("v3", "f3", Some(&good)),
            with_run(finding("f4", &gone, "+", 2, &anchor_line), "r2"),
            accept("v4", "f4", Some(&good)),
            // A line the fix did not touch.
            finding("f5", &repo, "+", 1, "def check(user):"),
            accept("v5", "f5", Some(&good)),
            // Not accepted: the human verdict wins over a later automatic one.
            finding("f6", &repo, "+", 2, &anchor_line),
            verdict("v6", "f6", "false-positive", false, None, 5),
            verdict("v7", "f6", "accepted", true, Some(&good), 9),
        ],
    );
    let out = tmp.path().join("cases");
    let summary = export(&conn, &out);
    assert_eq!(summary.written, 0, "{summary:?}");
    let expected = [
        ("anchor_not_changed_by_fix", 1),
        ("no_linked_commit", 1),
        ("redaction_changed_anchor", 1),
        ("removed_line_finding", 1),
        ("repo_missing", 1),
    ];
    assert_eq!(summary.skipped, expected.into_iter().collect());
}

#[test]
fn missing_file_at_revision_is_skipped() {
    let tmp = tempfile::tempdir().unwrap();
    let (repo, bad, _) = repo_with_fix(tmp.path(), DEFECTIVE);
    let first = git(&repo, &["rev-list", "--max-parents=0", "HEAD"]);
    let mut conn = db(tmp.path());
    load(
        &mut conn,
        &[
            run(&repo, &bad),
            finding("f1", &repo, "+", 2, "    if user.is_admin or True:"),
            // The cited commit predates the file.
            verdict("v1", "f1", "accepted", false, Some(&first), 6),
        ],
    );
    let summary = export(&conn, &tmp.path().join("cases"));
    assert_eq!(summary.skipped.get("file_missing_at_revision"), Some(&1));
}

#[test]
fn refuses_an_output_directory_inside_a_checkout() {
    let tmp = tempfile::tempdir().unwrap();
    let (repo, _, _) = repo_with_fix(tmp.path(), DEFECTIVE);
    let conn = db(tmp.path());
    let out = repo.join("private").join("cases");
    let err = export_bench_cases(&conn, &out, &[RedactionEngine::builtin()], 0, None).unwrap_err();
    assert!(err.to_string().contains("inside a git checkout"), "{err}");
    assert!(!repo.join("private").exists());
}

fn with_run(mut record: Value, run_id: &str) -> Value {
    record["run_id"] = json!(run_id);
    record
}

/// Synthetic PEM block; the body is not a real key.
const PEM: &str = "-----BEGIN RSA PRIVATE KEY-----\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2\nd3h5ejAxMjM0NTY3ODkrL0FCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaYWJjZGVm\n-----END RSA PRIVATE KEY-----\n";

#[test]
fn multi_line_secrets_are_redacted_across_lines() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path().join("repo");
    std::fs::create_dir_all(&repo).unwrap();
    git(&repo, &["init", "-q", "-b", "main"]);
    // f1: a PEM block in the fixed file (base) is redacted as a whole block.
    // f2: a PEM block in the defective file would change its line count.
    let bad = commit(&repo, "src/auth.py", DEFECTIVE);
    let good = commit(&repo, "src/auth.py", &format!("{FIXED}{PEM}"));
    git(&repo, &["checkout", "-q", "-b", "other", &bad]);
    let bad2 = commit(&repo, "src/auth.py", &format!("{DEFECTIVE}{PEM}"));
    let good2 = commit(&repo, "src/auth.py", FIXED);
    let repo = repo.canonicalize().unwrap();
    let mut conn = db(tmp.path());
    let line = "    if user.is_admin or True:";
    let mut run2 = run(&repo, &bad2);
    run2["id"] = json!("r2");
    load(
        &mut conn,
        &[
            run(&repo, &bad),
            run2,
            finding("f1", &repo, "+", 2, line),
            verdict("v1", "f1", "accepted", false, Some(&good), 6),
            with_run(finding("f2", &repo, "+", 2, line), "r2"),
            verdict("v2", "f2", "accepted", false, Some(&good2), 6),
        ],
    );
    let out = tmp.path().join("cases");
    let summary = export(&conn, &out);
    assert_eq!(summary.written, 1, "{summary:?}");
    assert_eq!(
        summary.skipped.get("redaction_changed_line_count"),
        Some(&1)
    );
    let text = std::fs::read_to_string(out.join("f-f1.json")).unwrap();
    assert!(text.contains("[REDACTED]"), "{text}");
    for body in PEM.lines().filter(|l| !l.starts_with("-----")) {
        assert!(!text.contains(body), "PEM body leaked: {text}");
    }
    assert!(!out.join("f-f2.json").exists());
}

#[test]
fn line_anchored_operator_patterns_still_apply() {
    use hippo_core::config::{RedactConfig, RedactPattern};
    let tmp = tempfile::tempdir().unwrap();
    // `^` without `(?m)` matches only at the start of the text it is given.
    let secret_line = "SECRET=hunter2-synthetic\n";
    let (repo, bad, good) = repo_with_fix(tmp.path(), &format!("{DEFECTIVE}{secret_line}"));
    let mut conn = db(tmp.path());
    load(
        &mut conn,
        &[
            run(&repo, &bad),
            finding("f1", &repo, "+", 2, "    if user.is_admin or True:"),
            verdict("v1", "f1", "accepted", false, Some(&good), 6),
        ],
    );
    let operator = RedactionEngine::new(&RedactConfig {
        patterns: vec![RedactPattern {
            name: "line_secret".into(),
            regex: "^SECRET=.*$".into(),
            replacement: "[REDACTED]".into(),
        }],
    })
    .unwrap();
    let out = tmp.path().join("cases");
    let engines = [RedactionEngine::builtin(), operator];
    let summary = export_bench_cases(&conn, &out, &engines, 0, None).unwrap();
    assert_eq!(summary.written, 1, "{summary:?}");
    let text = std::fs::read_to_string(out.join("f-f1.json")).unwrap();
    assert!(!text.contains("hunter2"), "{text}");
}
