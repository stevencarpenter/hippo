use hippo_daemon::gh_api::{GhApi, ListRunsQuery};
use wiremock::matchers::{header, method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[tokio::test]
async fn list_runs_returns_deserialized_runs() {
    let server = MockServer::start().await;

    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .and(header("authorization", "Bearer test-token"))
        .and(query_param("per_page", "20"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "total_count": 1,
            "workflow_runs": [
                {
                    "id": 999,
                    "head_sha": "abc",
                    "head_branch": "main",
                    "status": "completed",
                    "conclusion": "success",
                    "event": "push",
                    "html_url": "https://github.com/me/repo/actions/runs/999",
                    "run_started_at": "2026-04-15T12:00:00Z",
                    "updated_at":    "2026-04-15T12:05:00Z",
                    "actor": {"login": "me"}
                }
            ]
        })))
        .mount(&server)
        .await;

    let api = GhApi::new(server.uri(), "test-token".into());
    let runs = api.list_runs("me/repo", &ListRunsQuery::default()).await.unwrap();

    assert_eq!(runs.len(), 1);
    assert_eq!(runs[0].id, 999);
    assert_eq!(runs[0].head_sha, "abc");
    assert_eq!(runs[0].conclusion.as_deref(), Some("success"));
}

#[tokio::test]
async fn rate_limit_respects_reset_header() {
    let server = MockServer::start().await;

    // First response: 429 with retry-after.
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(ResponseTemplate::new(429).insert_header("retry-after", "1"))
        .up_to_n_times(1)
        .mount(&server)
        .await;

    // Second response: 200 with empty body.
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "total_count": 0, "workflow_runs": []
        })))
        .mount(&server)
        .await;

    let api = GhApi::new(server.uri(), "test-token".into());
    let start = std::time::Instant::now();
    let runs = api.list_runs("me/repo", &ListRunsQuery::default()).await.unwrap();
    assert!(runs.is_empty());
    assert!(start.elapsed().as_secs() >= 1, "should have waited at least 1s");
}

#[tokio::test]
async fn list_jobs_deserializes_response() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs/999/jobs"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "total_count": 1,
            "jobs": [{
                "id": 42, "name": "lint", "status": "completed",
                "conclusion": "failure",
                "started_at": "2026-04-15T12:00:00Z",
                "completed_at": "2026-04-15T12:05:00Z",
                "runner_name": "ubuntu-latest",
                "check_run_url": "https://api.github.com/repos/me/repo/check-runs/42"
            }]
        })))
        .mount(&server)
        .await;

    let api = GhApi::new(server.uri(), "test-token".into());
    let jobs = api.list_jobs("me/repo", 999).await.unwrap();
    assert_eq!(jobs.len(), 1);
    assert_eq!(jobs[0].id, 42);
    assert_eq!(jobs[0].conclusion.as_deref(), Some("failure"));
    assert_eq!(
        jobs[0].check_run_url.as_deref(),
        Some("https://api.github.com/repos/me/repo/check-runs/42")
    );
}

#[tokio::test]
async fn get_log_tail_truncates_when_oversize() {
    let server = MockServer::start().await;
    let body = "X".repeat(200);
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/jobs/42/logs"))
        .respond_with(ResponseTemplate::new(200).set_body_string(body))
        .mount(&server)
        .await;

    let api = GhApi::new(server.uri(), "test-token".into());
    let (excerpt, truncated) = api.get_log_tail("me/repo", 42, 50).await.unwrap();
    assert_eq!(excerpt.len(), 50);
    assert!(truncated);
    assert!(excerpt.chars().all(|c| c == 'X'));
}

#[tokio::test]
async fn get_log_tail_bails_on_http_error() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/jobs/42/logs"))
        .respond_with(
            ResponseTemplate::new(404).set_body_string(r#"{"message":"Not Found"}"#),
        )
        .mount(&server)
        .await;

    let api = GhApi::new(server.uri(), "test-token".into());
    let result = api.get_log_tail("me/repo", 42, 1024).await;
    assert!(result.is_err(), "404 must produce an error, not silently store the JSON body");
}

#[tokio::test]
async fn forbidden_without_retry_after_bails_immediately() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/repos/me/repo/actions/runs"))
        .respond_with(
            ResponseTemplate::new(403)
                .insert_header("x-ratelimit-remaining", "0")
                .set_body_string(r#"{"message":"Resource not accessible"}"#),
        )
        .mount(&server)
        .await;

    let api = GhApi::new(server.uri(), "test-token".into());
    let start = std::time::Instant::now();
    let result = api.list_runs("me/repo", &ListRunsQuery::default()).await;
    assert!(result.is_err());
    assert!(
        start.elapsed().as_secs() < 5,
        "permission 403 (no retry-after) must not retry"
    );
}
