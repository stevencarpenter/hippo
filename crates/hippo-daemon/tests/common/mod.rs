use std::net::TcpListener as StdTcpListener;

use tempfile::tempdir;

use hippo_core::config::HippoConfig;
use hippo_core::protocol::{DaemonRequest, DaemonResponse};

pub fn test_config() -> HippoConfig {
    let temp = tempdir().unwrap();
    let mut config = HippoConfig::default();
    config.storage.data_dir = temp.path().join("data");
    config.storage.config_dir = temp.path().join("config");
    // Short flush interval so events land in the DB quickly
    config.daemon.flush_interval_ms = 100;
    // Bind an ephemeral TCP port then drop the listener so no real service
    // will answer on it.  This prevents `check_brain_schema_compat` from
    // connecting to a live hippo-brain at the production default (9175) and
    // bailing with a schema-version mismatch.
    let ephemeral = StdTcpListener::bind("127.0.0.1:0").unwrap();
    let ephemeral_port = ephemeral.local_addr().unwrap().port();
    drop(ephemeral);
    config.brain.port = ephemeral_port;
    std::mem::forget(temp);
    config
}

pub async fn wait_for_daemon(socket_path: &std::path::Path) {
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(2);
    loop {
        if let Ok(DaemonResponse::Status(_)) =
            hippo_daemon::commands::send_request(socket_path, &DaemonRequest::GetStatus).await
        {
            return;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "daemon never became ready"
        );
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    }
}
