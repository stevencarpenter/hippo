//! Synthetic auto-memory probe — dedicated fixture outside Claude's datastore.

use anyhow::{Context, Result};
use hippo_core::config::{HippoConfig, default_brain_dir};
use hippo_core::storage;
use std::path::{Path, PathBuf};
use std::process::Command;
use tracing::info;

const PROBE_REPOSITORY: &str = "hippo/__hippo_probe__";
const PROBE_LOGICAL_PATH: &str = "synthetic/probe-memory.md";

fn probe_program(brain_dir: &Path) -> PathBuf {
    brain_dir.join(".venv/bin/hippo-auto-memory-probe")
}

/// Run the auto-memory probe via the Python brain package and poll SQLite.
pub fn probe_auto_memory(config: &HippoConfig) -> Result<(bool, Option<i64>)> {
    let probe_start_ms = chrono::Utc::now().timestamp_millis();
    let brain_dir = default_brain_dir();
    let db_path = config.db_path();
    let data_dir = config.storage.data_dir.clone();
    let program = probe_program(&brain_dir);

    // Execute the installed console script directly. launchd intentionally has
    // a minimal PATH, so routing this through Homebrew's `uv` makes a healthy
    // installed brain fail solely because `/opt/homebrew/bin` is absent.
    let output = Command::new(&program)
        .args([
            "--db",
            &db_path.to_string_lossy(),
            "--data-dir",
            &data_dir.to_string_lossy(),
        ])
        .output()
        .with_context(|| format!("failed to spawn {}", program.display()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!("auto-memory probe failed: {stderr}");
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let parsed: serde_json::Value =
        serde_json::from_str(stdout.trim()).context("auto-memory probe returned invalid JSON")?;
    let ok = parsed.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    if !ok {
        let err = parsed
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown error");
        info!("auto-memory probe: FAIL — {err}");
        return Ok((false, None));
    }

    let lag = parsed.get("lag_ms").and_then(|v| v.as_i64());
    let db = storage::open_db(&db_path).context("cannot open DB for auto-memory probe verify")?;
    let count: i64 = db.query_row(
        "SELECT COUNT(*) FROM memory_documents WHERE repository = ?1 AND logical_path = ?2",
        rusqlite::params![PROBE_REPOSITORY, PROBE_LOGICAL_PATH],
        |row| row.get(0),
    )?;
    if count == 0 {
        info!("auto-memory probe: document row missing after probe run");
        return Ok((false, None));
    }

    let elapsed = chrono::Utc::now().timestamp_millis() - probe_start_ms;
    let lag_ms = lag.or(Some(elapsed.max(0)));
    Ok((true, lag_ms))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn probe_uses_installed_brain_entrypoint() {
        let root = Path::new("/tmp/hippo-brain");
        assert_eq!(
            probe_program(root),
            root.join(".venv/bin/hippo-auto-memory-probe")
        );
    }
}
