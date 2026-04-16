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
