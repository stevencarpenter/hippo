use hippo_core::storage::open_db;
use hippo_daemon::gh_api::GhApi;
use hippo_daemon::gh_poll::{run_once, PollConfig};
use serde_json::json;
use tempfile::TempDir;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[tokio::test]
async fn single_pass_inserts_runs_jobs_annotations() {
    let server = MockServer::start().await;

    // 1 run, completed/failure
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "workflow_runs": [{
                "id": 1001,
                "head_sha": "deadbeef",
                "head_branch": "main",
                "status": "completed",
                "conclusion": "failure",
                "event": "push",
                "html_url": "https://github.com/me/repo/actions/runs/1001",
                "run_started_at": "2026-04-15T12:00:00Z",
                "updated_at": "2026-04-15T12:08:00Z",
                "actor": {"login": "me"}
            }]
        })))
        .mount(&server)
        .await;

    // 1 job under that run, conclusion=failure (so logs get fetched)
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs/1001/jobs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "jobs": [{
                "id": 2001, "name": "lint",
                "status": "completed", "conclusion": "failure",
                "started_at": "2026-04-15T12:00:00Z",
                "completed_at": "2026-04-15T12:01:00Z",
                "runner_name": "ubuntu-latest",
                "check_run_url": format!("{}/repos/me/repo/check-runs/2001", server.uri())
            }]
        })))
        .mount(&server)
        .await;

    // 1 annotation on that job
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/check-runs/2001/annotations"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([{
            "annotation_level": "failure",
            "message": "F401 unused import",
            "path": "brain/x.py",
            "start_line": 3
        }])))
        .mount(&server)
        .await;

    // Log tail for the failed job
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/jobs/2001/logs"))
        .respond_with(ResponseTemplate::new(200).set_body_string("the log content"))
        .mount(&server)
        .await;

    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("hippo.db");
    open_db(&db_path).unwrap();

    let api = GhApi::new(server.uri(), "test-token".into());
    let cfg = PollConfig {
        watched_repos: vec!["me/repo".into()],
        log_excerpt_max_bytes: 1024,
    };

    run_once(&api, &db_path, &cfg).await.unwrap();

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let runs: i64 = conn
        .query_row("SELECT count(*) FROM workflow_runs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(runs, 1);

    let jobs: i64 = conn
        .query_row("SELECT count(*) FROM workflow_jobs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(jobs, 1);

    let annotations: i64 = conn
        .query_row("SELECT count(*) FROM workflow_annotations", [], |r| r.get(0))
        .unwrap();
    assert_eq!(annotations, 1);

    // Annotation tool/rule attribution worked
    let (tool, rule): (Option<String>, Option<String>) = conn
        .query_row(
            "SELECT tool, rule_id FROM workflow_annotations LIMIT 1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(tool.as_deref(), Some("ruff"));
    assert_eq!(rule.as_deref(), Some("F401"));

    let logs: i64 = conn
        .query_row(
            "SELECT count(*) FROM workflow_log_excerpts",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(logs, 1);

    let queued: i64 = conn
        .query_row(
            "SELECT count(*) FROM workflow_enrichment_queue WHERE status='pending'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(queued, 1);
}
