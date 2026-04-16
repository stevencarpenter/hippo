use hippo_core::protocol::{DaemonRequest, DaemonResponse};

mod common;

use common::{test_config, wait_for_daemon};

#[tokio::test]
async fn register_watch_sha_creates_row() {
    let config = test_config();
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    let run_config = config.clone();
    let daemon_handle = tokio::spawn(async move { hippo_daemon::daemon::run(run_config).await });
    wait_for_daemon(&socket_path).await;

    let resp = hippo_daemon::commands::send_request(
        &socket_path,
        &DaemonRequest::RegisterWatchSha {
            sha: "abc123".into(),
            repo: "me/repo".into(),
            ttl_secs: 1200,
        },
    )
    .await
    .unwrap();

    assert!(
        matches!(resp, DaemonResponse::Ack),
        "expected Ack, got {:?}",
        resp
    );

    let conn = hippo_core::storage::open_db(&db_path).unwrap();
    let count: i64 = conn
        .query_row(
            "SELECT count(*) FROM sha_watchlist WHERE sha = 'abc123'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1, "sha_watchlist should have one row for abc123");

    let _ = hippo_daemon::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
    let _ = daemon_handle.await;
}
