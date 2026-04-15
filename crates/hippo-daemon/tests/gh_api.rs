use hippo_daemon::gh_api::{GhApi, ListRunsQuery};
use wiremock::matchers::{header, method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[tokio::test]
async fn list_runs_paginates_and_dedups() {
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
