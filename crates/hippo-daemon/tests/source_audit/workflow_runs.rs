//! Source #6 — GitHub workflow runs (Actions poller).
//!
//! Production path: `hippo gh-poll` → `gh_poll::run_once` →
//! `workflow_store::upsert_run`/`upsert_job`/`insert_annotation`/
//! `insert_log_excerpt`/`enqueue_enrichment` (all direct SQLite inserts
//! in `storage.rs::workflow_store`).
//!
//! The exhaustive contract is in `tests/gh_poll_integration.rs`. This
//! audit is the minimal "rows land in every expected table" assertion —
//! if the set of tables the poller writes to drifts, this test breaks
//! even if the behavioural one still passes for the subset it covers.

use hippo_core::storage::open_db;
use hippo_daemon::gh_api::GhApi;
use hippo_daemon::gh_poll::{PollConfig, run_once};
use serde_json::json;
use tempfile::TempDir;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[tokio::test]
async fn gh_poll_writes_workflow_tables() {
    let server = MockServer::start().await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "workflow_runs": [{
                "id": 5001,
                "head_sha": "cafebabe",
                "head_branch": "audit-branch",
                "status": "completed",
                "conclusion": "failure",
                "event": "push",
                "html_url": "https://github.com/me/repo/actions/runs/5001",
                "run_started_at": "2026-04-22T10:00:00Z",
                "updated_at": "2026-04-22T10:05:00Z",
                "actor": {"login": "audit-bot"}
            }]
        })))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs/5001/jobs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "jobs": [{
                "id": 6001, "name": "test",
                "status": "completed", "conclusion": "failure",
                "started_at": "2026-04-22T10:00:00Z",
                "completed_at": "2026-04-22T10:04:00Z",
                "runner_name": "ubuntu-latest",
                "check_run_url": format!("{}/repos/me/repo/check-runs/6001", server.uri()),
            }]
        })))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/check-runs/6001/annotations"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([{
            "annotation_level": "failure",
            "message": "test_audit_fails",
            "path": "tests/source_audit.rs",
            "start_line": 42
        }])))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/jobs/6001/logs"))
        .respond_with(ResponseTemplate::new(200).set_body_string("audit log"))
        .mount(&server)
        .await;

    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("hippo.db");
    open_db(&db_path).unwrap();

    let api = GhApi::new(server.uri(), "fake-token".into());
    let cfg = PollConfig {
        watched_repos: vec!["me/repo".into()],
        log_excerpt_max_bytes: 1024,
        redact_config_path: None,
    };

    run_once(&api, &db_path, &cfg).await.unwrap();

    let conn = rusqlite::Connection::open(&db_path).unwrap();

    // Every table in the workflow source's expected-tables column must
    // hold at least one row.
    for (table, expected_min) in [
        ("workflow_runs", 1),
        ("workflow_jobs", 1),
        ("workflow_annotations", 1),
        ("workflow_log_excerpts", 1),
        ("workflow_enrichment_queue", 1),
    ] {
        let count: i64 = conn
            .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0))
            .unwrap();
        assert!(
            count >= expected_min,
            "{table} should have ≥{expected_min} row(s), got {count}"
        );
    }
}
