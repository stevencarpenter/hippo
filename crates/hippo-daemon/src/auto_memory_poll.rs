//! Claude auto-memory ingest poll — reads configured sources and delegates
//! ingest to the Python brain package (`hippo-auto-memory-poll`).

use anyhow::{Context, Result};
use hippo_core::config::{HippoConfig, default_brain_dir};
use std::process::Command;
use tracing::{debug, info};

/// One poll cycle: ingest every configured auto-memory source via Python.
pub fn poll_tick(config: &HippoConfig) -> Result<usize> {
    if !config.auto_memory.enabled {
        debug!("auto-memory poll disabled by config");
        return Ok(0);
    }
    if config.auto_memory.sources.is_empty() {
        debug!("auto-memory enabled but no sources configured");
        return Ok(0);
    }

    // Resolve the brain via the shared canonical resolver so this matches the
    // install/serve location even under an XDG_DATA_HOME override.
    let brain_dir = default_brain_dir();
    let config_path = config.storage.config_dir.join("config.toml");
    let output = Command::new("uv")
        .args([
            "run",
            "--project",
            &brain_dir.to_string_lossy(),
            "hippo-auto-memory-poll",
            "--config",
            &config_path.to_string_lossy(),
        ])
        .output()
        .with_context(|| {
            format!(
                "failed to spawn hippo-auto-memory-poll in {}",
                brain_dir.display()
            )
        })?;

    if !output.status.success() {
        anyhow::bail!(
            "hippo-auto-memory-poll failed (exit {}): {}",
            output.status,
            String::from_utf8_lossy(&output.stderr)
        );
    }

    let parsed: serde_json::Value =
        serde_json::from_slice(&output.stdout).context("parse auto-memory poll JSON output")?;
    let changed = parsed
        .get("changed")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0) as usize;
    info!(changed, "auto-memory poll: completed");
    Ok(changed)
}
