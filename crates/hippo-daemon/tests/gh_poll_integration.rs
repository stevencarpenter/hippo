use hippo_core::storage::open_db;
use hippo_daemon::gh_api::GhApi;
use hippo_daemon::gh_poll::{PollConfig, run_once};
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
        redact_config_path: None,
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
        .query_row("SELECT count(*) FROM workflow_annotations", [], |r| {
            r.get(0)
        })
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
        .query_row("SELECT count(*) FROM workflow_log_excerpts", [], |r| {
            r.get(0)
        })
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

#[tokio::test]
async fn in_progress_run_does_not_fetch_jobs() {
    let server = MockServer::start().await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "workflow_runs": [{
                "id": 5001,
                "head_sha": "aabbccdd",
                "head_branch": "feature",
                "status": "in_progress",
                "conclusion": null,
                "event": "push",
                "html_url": "https://github.com/me/repo/actions/runs/5001",
                "run_started_at": "2026-04-15T12:00:00Z",
                "updated_at": "2026-04-15T12:00:00Z",
                "actor": {"login": "me"}
            }]
        })))
        .mount(&server)
        .await;

    // No jobs mock — any attempt to hit the jobs endpoint would return a 501 fallback
    // which would cause run_once to error, surfacing the bug.

    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("hippo.db");
    open_db(&db_path).unwrap();

    let api = GhApi::new(server.uri(), "test-token".into());
    let cfg = PollConfig {
        watched_repos: vec!["me/repo".into()],
        log_excerpt_max_bytes: 1024,
        redact_config_path: None,
    };

    run_once(&api, &db_path, &cfg).await.unwrap();

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let runs: i64 = conn
        .query_row("SELECT count(*) FROM workflow_runs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(runs, 1, "in-progress run should be saved");

    let jobs: i64 = conn
        .query_row("SELECT count(*) FROM workflow_jobs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(jobs, 0, "jobs must not be fetched for an in-progress run");
}

#[tokio::test]
async fn list_jobs_failure_is_swallowed_and_run_continues() {
    let server = MockServer::start().await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "workflow_runs": [{
                "id": 6001,
                "head_sha": "sha_fail_jobs",
                "head_branch": "main",
                "status": "completed",
                "conclusion": "failure",
                "event": "push",
                "html_url": "https://github.com/me/repo/actions/runs/6001",
                "run_started_at": "2026-04-15T12:00:00Z",
                "updated_at": "2026-04-15T12:05:00Z",
                "actor": {"login": "me"}
            }]
        })))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs/6001/jobs"))
        .respond_with(ResponseTemplate::new(500).set_body_string("server error"))
        .mount(&server)
        .await;

    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("hippo.db");
    open_db(&db_path).unwrap();

    let api = GhApi::new(server.uri(), "test-token".into());
    let cfg = PollConfig {
        watched_repos: vec!["me/repo".into()],
        log_excerpt_max_bytes: 1024,
        redact_config_path: None,
    };

    // list_jobs failure must not propagate as an error from run_once
    run_once(&api, &db_path, &cfg).await.unwrap();

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let runs: i64 = conn
        .query_row("SELECT count(*) FROM workflow_runs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(runs, 1, "run should still be stored despite jobs failure");

    let jobs: i64 = conn
        .query_row("SELECT count(*) FROM workflow_jobs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(jobs, 0, "no jobs stored when list_jobs failed");
}

#[tokio::test]
async fn get_annotations_failure_still_saves_job_and_logs() {
    let server = MockServer::start().await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "workflow_runs": [{
                "id": 7001, "head_sha": "sha7", "head_branch": "main",
                "status": "completed", "conclusion": "failure", "event": "push",
                "html_url": "https://github.com/me/repo/actions/runs/7001",
                "run_started_at": "2026-04-15T12:00:00Z",
                "updated_at": "2026-04-15T12:05:00Z",
                "actor": {"login": "me"}
            }]
        })))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs/7001/jobs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "jobs": [{
                "id": 8001, "name": "lint",
                "status": "completed", "conclusion": "failure",
                "started_at": "2026-04-15T12:00:00Z",
                "completed_at": "2026-04-15T12:01:00Z",
                "runner_name": "ubuntu-latest",
                "check_run_url": format!("{}/repos/me/repo/check-runs/8001", server.uri())
            }]
        })))
        .mount(&server)
        .await;

    // Annotations endpoint fails.
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/check-runs/8001/annotations"))
        .respond_with(ResponseTemplate::new(500).set_body_string("internal error"))
        .mount(&server)
        .await;

    // Log tail succeeds normally.
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/jobs/8001/logs"))
        .respond_with(ResponseTemplate::new(200).set_body_string("log output"))
        .mount(&server)
        .await;

    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("hippo.db");
    open_db(&db_path).unwrap();

    let api = GhApi::new(server.uri(), "test-token".into());
    let cfg = PollConfig {
        watched_repos: vec!["me/repo".into()],
        log_excerpt_max_bytes: 1024,
        redact_config_path: None,
    };

    run_once(&api, &db_path, &cfg).await.unwrap();

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let jobs: i64 = conn
        .query_row("SELECT count(*) FROM workflow_jobs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(
        jobs, 1,
        "job should be saved even when annotations fetch fails"
    );

    let annotations: i64 = conn
        .query_row("SELECT count(*) FROM workflow_annotations", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(annotations, 0, "no annotations when fetch fails");

    let logs: i64 = conn
        .query_row("SELECT count(*) FROM workflow_log_excerpts", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(logs, 1, "log excerpt should still be saved");
}

#[tokio::test]
async fn get_log_tail_failure_skips_excerpt_but_saves_job() {
    let server = MockServer::start().await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "workflow_runs": [{
                "id": 9001, "head_sha": "sha9", "head_branch": "main",
                "status": "completed", "conclusion": "failure", "event": "push",
                "html_url": "https://github.com/me/repo/actions/runs/9001",
                "run_started_at": "2026-04-15T12:00:00Z",
                "updated_at": "2026-04-15T12:05:00Z",
                "actor": {"login": "me"}
            }]
        })))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs/9001/jobs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "jobs": [{
                "id": 9101, "name": "build",
                "status": "completed", "conclusion": "failure",
                "started_at": "2026-04-15T12:00:00Z",
                "completed_at": "2026-04-15T12:01:00Z",
                "runner_name": "ubuntu-latest",
                "check_run_url": null
            }]
        })))
        .mount(&server)
        .await;

    // Log tail endpoint returns an error.
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/jobs/9101/logs"))
        .respond_with(ResponseTemplate::new(403).set_body_string("forbidden"))
        .mount(&server)
        .await;

    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("hippo.db");
    open_db(&db_path).unwrap();

    let api = GhApi::new(server.uri(), "test-token".into());
    let cfg = PollConfig {
        watched_repos: vec!["me/repo".into()],
        log_excerpt_max_bytes: 1024,
        redact_config_path: None,
    };

    run_once(&api, &db_path, &cfg).await.unwrap();

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let jobs: i64 = conn
        .query_row("SELECT count(*) FROM workflow_jobs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(jobs, 1, "job should still be stored");

    let logs: i64 = conn
        .query_row("SELECT count(*) FROM workflow_log_excerpts", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(logs, 0, "no log excerpt when fetch fails");
}

#[tokio::test]
async fn annotations_are_redacted_before_storage() {
    let server = MockServer::start().await;

    // Use a pattern that won't match any builtin rule, so we know
    // the custom redact_config_path is driving the redaction.
    let secret = "HIPPOTEST_SECRET_DEADBEEF123";
    let annotation_msg = format!("build failed: token={secret} is invalid");
    let log_content = format!("Error: credential {secret} rejected by server");

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "workflow_runs": [{
                "id": 3001,
                "head_sha": "cafebabe",
                "head_branch": "main",
                "status": "completed",
                "conclusion": "failure",
                "event": "push",
                "html_url": "https://github.com/me/repo/actions/runs/3001",
                "run_started_at": "2026-04-15T12:00:00Z",
                "updated_at": "2026-04-15T12:08:00Z",
                "actor": {"login": "me"}
            }]
        })))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs/3001/jobs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "total_count": 1,
            "jobs": [{
                "id": 4001, "name": "build",
                "status": "completed", "conclusion": "failure",
                "started_at": "2026-04-15T12:00:00Z",
                "completed_at": "2026-04-15T12:01:00Z",
                "runner_name": "ubuntu-latest",
                "check_run_url": format!("{}/repos/me/repo/check-runs/4001", server.uri())
            }]
        })))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/check-runs/4001/annotations"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([{
            "annotation_level": "failure",
            "message": annotation_msg,
            "path": "config.yml",
            "start_line": 10
        }])))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/jobs/4001/logs"))
        .respond_with(ResponseTemplate::new(200).set_body_string(log_content))
        .mount(&server)
        .await;

    let tmp = TempDir::new().unwrap();
    let db_path = tmp.path().join("hippo.db");
    open_db(&db_path).unwrap();

    // Write a custom redact config that matches our fake secret pattern.
    let redact_path = tmp.path().join("redact.toml");
    std::fs::write(
        &redact_path,
        r#"
[[patterns]]
name = "hippotest_secret"
regex = "HIPPOTEST_SECRET_[A-Z0-9]+"
replacement = "[REDACTED]"
"#,
    )
    .unwrap();

    let api = GhApi::new(server.uri(), "test-token".into());
    let cfg = PollConfig {
        watched_repos: vec!["me/repo".into()],
        log_excerpt_max_bytes: 1024,
        redact_config_path: Some(redact_path),
    };

    run_once(&api, &db_path, &cfg).await.unwrap();

    let conn = rusqlite::Connection::open(&db_path).unwrap();

    let stored_msg: String = conn
        .query_row(
            "SELECT message FROM workflow_annotations LIMIT 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        !stored_msg.contains("HIPPOTEST_SECRET_DEADBEEF123"),
        "annotation message must not contain the raw secret; got: {stored_msg}"
    );
    assert!(
        stored_msg.contains("[REDACTED]"),
        "annotation message must contain the redaction placeholder; got: {stored_msg}"
    );

    let stored_excerpt: String = conn
        .query_row(
            "SELECT excerpt FROM workflow_log_excerpts LIMIT 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        !stored_excerpt.contains("HIPPOTEST_SECRET_DEADBEEF123"),
        "log excerpt must not contain the raw secret; got: {stored_excerpt}"
    );
    assert!(
        stored_excerpt.contains("[REDACTED]"),
        "log excerpt must contain the redaction placeholder; got: {stored_excerpt}"
    );
}
