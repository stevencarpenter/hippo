//! Synthetic capture probes — end-to-end liveness verification.
//!
//! Each probe sends a tagged synthetic event through the real pipeline and
//! polls the database to confirm the row appeared. Results are written to
//! `source_health` so the watchdog can evaluate invariant I-8 (probe freshness).
//!
//! Reference: docs/capture-reliability/05-synthetic-probes.md

use anyhow::{Context, Result};
use hippo_core::config::HippoConfig;
use hippo_core::storage;
use rusqlite::OptionalExtension;
use std::time::Instant;
use tracing::{info, warn};
use uuid::Uuid;

/// Maximum time to wait for a probe row to appear in SQLite.
const POLL_DEADLINE_MS: u64 = 10_000;
const POLL_INTERVAL_MS: u64 = 200;

/// Run one or all probes, then write results to `source_health`.
///
/// `source` is one of `"shell"`, `"claude-tool"`, `"claude-session"`,
/// `"browser"`, or `None` to run all four in sequence.
pub async fn run(config: &HippoConfig, source: Option<&str>) -> Result<()> {
    let run_all = source.is_none();

    if run_all || source == Some("shell") {
        match probe_shell(config).await {
            Ok((ok, lag)) => {
                println!(
                    "[probe] shell: {} (lag={}ms)",
                    if ok { "OK" } else { "FAIL" },
                    lag.unwrap_or(0)
                );
                write_probe_result(config, "shell", ok, lag)?;
            }
            Err(e) => {
                warn!("shell probe error: {e:#}");
                println!("[probe] shell: ERROR — {e:#}");
                write_probe_result(config, "shell", false, None)?;
            }
        }
    }

    if run_all || source == Some("claude-tool") {
        match probe_claude_tool(config).await {
            Ok((ok, lag)) => {
                println!(
                    "[probe] claude-tool: {} (lag={}ms)",
                    if ok { "OK" } else { "FAIL" },
                    lag.unwrap_or(0)
                );
                write_probe_result(config, "claude-tool", ok, lag)?;
            }
            Err(e) => {
                warn!("claude-tool probe error: {e:#}");
                println!("[probe] claude-tool: ERROR — {e:#}");
                write_probe_result(config, "claude-tool", false, None)?;
            }
        }
    }

    if run_all || source == Some("claude-session") {
        match probe_claude_session(config) {
            Ok((ok, lag)) => {
                println!(
                    "[probe] claude-session: {} (lag={}ms)",
                    if ok { "OK" } else { "FAIL" },
                    lag.map(|l| l.to_string()).as_deref().unwrap_or("N/A")
                );
                write_probe_result(config, "claude-session", ok, lag)?;
            }
            Err(e) => {
                warn!("claude-session probe error: {e:#}");
                println!("[probe] claude-session: ERROR — {e:#}");
                write_probe_result(config, "claude-session", false, None)?;
            }
        }
    }

    if run_all || source == Some("browser") {
        match probe_browser(config).await {
            Ok((ok, lag)) => {
                println!(
                    "[probe] browser: {} (lag={}ms)",
                    if ok { "OK" } else { "FAIL" },
                    lag.unwrap_or(0)
                );
                write_probe_result(config, "browser", ok, lag)?;
            }
            Err(e) => {
                warn!("browser probe error: {e:#}");
                println!("[probe] browser: ERROR — {e:#}");
                write_probe_result(config, "browser", false, None)?;
            }
        }
    }

    if let Some(s) = source
        && !matches!(s, "shell" | "claude-tool" | "claude-session" | "browser")
    {
        anyhow::bail!(
            "unknown probe source '{}'; valid: shell, claude-tool, claude-session, browser",
            s
        );
    }

    Ok(())
}

/// Shell probe: send a synthetic shell event and wait for it to appear in `events`.
async fn probe_shell(config: &HippoConfig) -> Result<(bool, Option<i64>)> {
    let probe_uuid = Uuid::new_v4();
    let probe_start_ms = chrono::Utc::now().timestamp_millis();

    let uuid_str = probe_uuid.to_string();

    crate::commands::handle_send_event_shell(
        config,
        "__hippo_probe__".to_string(),
        0,
        std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string()),
        0,
        None,
        None,
        None,
        false,
        None,
        Some(uuid_str.clone()),
        None,
        None,
    )
    .await
    .context("shell probe send failed")?;

    poll_event_row(config, &uuid_str, probe_start_ms).await
}

/// Claude-tool probe: same pipeline as shell but with `source_kind = 'claude-tool'`.
async fn probe_claude_tool(config: &HippoConfig) -> Result<(bool, Option<i64>)> {
    let probe_uuid = Uuid::new_v4();
    let probe_start_ms = chrono::Utc::now().timestamp_millis();

    crate::commands::handle_send_event_shell(
        config,
        "__hippo_probe_claude_tool__".to_string(),
        0,
        std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string()),
        0,
        None,
        None,
        None,
        false,
        None,
        Some(probe_uuid.to_string()),
        Some("claude-tool".to_string()),
        Some("Bash".to_string()),
    )
    .await
    .context("claude-tool probe send failed")?;

    let uuid_str = probe_uuid.to_string();
    poll_event_row(config, &uuid_str, probe_start_ms).await
}

/// Claude-session probe: assertion-based, not injection.
///
/// For every `~/.claude/projects/**/*.jsonl` modified in the last 5 minutes,
/// assert that a `claude_sessions` row exists with `source_file = <path>` and
/// `end_time >= mtime_ms - 300_000`. If no JSONL was recently active: trivially
/// pass (no Claude session running). If JSONL exists but no matching row: fail.
///
/// Recursive walk covers main sessions and subagent sessions at any depth:
/// ~/.claude/projects/<project>/<session>.jsonl
/// ~/.claude/projects/<project>/<parent>/subagents/<id>.jsonl
fn probe_claude_session(config: &HippoConfig) -> Result<(bool, Option<i64>)> {
    let now_ms = chrono::Utc::now().timestamp_millis();
    let window_ms: i64 = 5 * 60 * 1000; // 5 minutes
    let stale_threshold_ms: i64 = 5 * 60 * 1000; // 5-minute tolerance

    // Find JSONL files modified within the last 5 minutes.
    let projects_dir = dirs::home_dir()
        .context("cannot determine home dir")?
        .join(".claude/projects");

    if !projects_dir.exists() {
        info!("claude-session probe: ~/.claude/projects not found — trivial pass");
        return Ok((true, None));
    }

    let mut recent_jsonl: Vec<(std::path::PathBuf, i64)> = Vec::new();
    let mut dirs_to_scan = vec![projects_dir];

    // Recursive walk to catch main sessions and subagent sessions at any depth.
    while let Some(dir) = dirs_to_scan.pop() {
        let entries = std::fs::read_dir(&dir)
            .with_context(|| format!("failed to read Claude projects directory {}", dir.display()))?;
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                dirs_to_scan.push(path);
            } else if path.extension().and_then(|e| e.to_str()) == Some("jsonl") {
                let mtime_ms = match entry.metadata().ok().and_then(|m| {
                    m.modified().ok().and_then(|t| {
                        t.duration_since(std::time::UNIX_EPOCH)
                            .ok()
                            .map(|d| d.as_millis() as i64)
                    })
                }) {
                    Some(ts) => ts,
                    None => continue,
                };
                if now_ms - mtime_ms <= window_ms {
                    recent_jsonl.push((path, mtime_ms));
                }
            }
        }
    }

    if recent_jsonl.is_empty() {
        // No active Claude session — trivially pass.
        info!("claude-session probe: no recently-modified JSONL files — trivial pass");
        return Ok((true, None));
    }

    let db =
        storage::open_db(&config.db_path()).context("cannot open DB for claude-session probe")?;

    let mut all_ok = true;
    let mut latest_lag: Option<i64> = None;

    for (jsonl_path, mtime_ms) in &recent_jsonl {
        let path_str = jsonl_path.to_string_lossy();
        let threshold = mtime_ms - stale_threshold_ms;

        let count: i64 = db
            .query_row(
                "SELECT COUNT(*) FROM claude_sessions
                 WHERE source_file = ?1
                   AND probe_tag IS NULL
                   AND end_time >= ?2",
                rusqlite::params![path_str.as_ref(), threshold],
                |row| row.get(0),
            )
            .with_context(|| {
                format!("failed to query claude_sessions for {}", path_str)
            })?;

        if count == 0 {
            warn!("claude-session probe: no row for {}", path_str);
            all_ok = false;
        } else {
            // Compute lag as now_ms - MAX(end_time).
            let max_end: Option<i64> = db
                .query_row(
                    "SELECT MAX(end_time) FROM claude_sessions
                     WHERE source_file = ?1 AND probe_tag IS NULL",
                    rusqlite::params![path_str.as_ref()],
                    |row| row.get(0),
                )
                .with_context(|| {
                    format!(
                        "claude-session probe: failed to query MAX(end_time) for {}",
                        path_str
                    )
                })?;

            if let Some(end) = max_end {
                let lag = now_ms - end;
                latest_lag = Some(match latest_lag {
                    Some(prev) => prev.max(lag),
                    None => lag,
                });
            }
        }
    }

    Ok((all_ok, latest_lag))
}

/// Browser probe: invoke the NM host binary via stdin/stdout, writing a
/// synthetic visit for `probe_domain`, then poll `browser_events` for the row.
///
/// Uses a fresh UUID per run (not make_envelope_id) and checks
/// `created_at > probe_start_ms` to avoid false positives from the dedup window
/// matching stale rows from prior probe runs.
async fn probe_browser(config: &HippoConfig) -> Result<(bool, Option<i64>)> {
    let probe_start_ms = chrono::Utc::now().timestamp_millis();
    let probe_domain = &config.browser.probe_domain;

    let probe_url = format!("https://{}/synthetic", probe_domain);
    // Use a fresh UUID per probe run (not make_envelope_id) to avoid dedup
    // window stale-row false positives. The NM host will use this as probe_tag.
    let probe_uuid = Uuid::new_v4();
    let probe_uuid_str = probe_uuid.to_string();

    // Build the BrowserVisit JSON message with the explicit probe_tag.
    let visit = serde_json::json!({
        "url": probe_url,
        "title": "Hippo Probe",
        "domain": probe_domain,
        "dwell_ms": 1,
        "scroll_depth": 1.0,
        "timestamp": probe_start_ms,
        "probe_tag": probe_uuid_str
    });
    let payload = serde_json::to_vec(&visit)?;
    // Encode with 4-byte native-endian length prefix (NM framing).
    let len = payload.len() as u32;
    let len_bytes = len.to_ne_bytes();
    let mut nm_message = Vec::with_capacity(4 + payload.len());
    nm_message.extend_from_slice(&len_bytes);
    nm_message.extend_from_slice(&payload);

    // Find the hippo binary — use current executable.
    let hippo_bin = std::env::current_exe().unwrap_or_else(|_| std::path::PathBuf::from("hippo"));

    // Spawn the NM host subprocess using tokio for non-blocking I/O.
    use tokio::io::AsyncWriteExt;
    let mut child = tokio::process::Command::new(&hippo_bin)
        .arg("native-messaging-host")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .context("failed to spawn native-messaging-host")?;

    {
        let stdin = child.stdin.as_mut().context("no stdin")?;
        stdin
            .write_all(&nm_message)
            .await
            .context("failed to write NM message")?;
        stdin.shutdown().await.context("failed to close NM stdin")?;
    }

    // Give the NM host time to forward the event before polling.
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;

    // Wait for child (don't care about its exit code here).
    let _ = child.wait().await;

    // Poll browser_events for the probe row.
    // Check created_at > probe_start_ms to ensure we get a fresh row,
    // not a stale one from a prior probe run within the dedup window.
    let db = storage::open_db(&config.db_path()).context("cannot open DB for browser probe")?;
    let deadline = Instant::now() + std::time::Duration::from_millis(POLL_DEADLINE_MS);

    loop {
        let row: Option<i64> = db
            .query_row(
                "SELECT created_at FROM browser_events
                WHERE probe_tag = ?1 AND created_at > ?2
                LIMIT 1",
                rusqlite::params![probe_uuid_str, probe_start_ms],
                |row| row.get(0),
            )
            .optional()?;

        if let Some(created_at) = row {
            let lag = created_at - probe_start_ms;
            return Ok((true, Some(lag.max(0))));
        }

        if Instant::now() >= deadline {
            return Ok((false, None));
        }

        tokio::time::sleep(std::time::Duration::from_millis(POLL_INTERVAL_MS)).await;
    }
}

/// Poll `events` for a row matching `probe_tag = uuid_str`. Returns `(ok, lag_ms)`.
///
/// We query by probe_tag alone because EventEnvelope::shell() generates a random
/// envelope_id that we cannot control from outside the constructor. probe_tag IS
/// set to the probe UUID, so it's the reliable identifier.
async fn poll_event_row(
    config: &HippoConfig,
    uuid_str: &str,
    probe_start_ms: i64,
) -> Result<(bool, Option<i64>)> {
    let db = storage::open_db(&config.db_path()).context("cannot open DB for probe poll")?;
    let deadline = Instant::now() + std::time::Duration::from_millis(POLL_DEADLINE_MS);

    loop {
        let row: Option<i64> = db
            .query_row(
                "SELECT created_at FROM events
                WHERE probe_tag = ?1
                LIMIT 1",
                rusqlite::params![uuid_str],
                |row| row.get(0),
            )
            .optional()?;

        if let Some(created_at) = row {
            let lag = created_at - probe_start_ms;
            return Ok((true, Some(lag.max(0))));
        }

        if Instant::now() >= deadline {
            return Ok((false, None));
        }

        tokio::time::sleep(std::time::Duration::from_millis(POLL_INTERVAL_MS)).await;
    }
}

/// Write probe result to `source_health`.
///
/// Silently skips if `source_health` row for this source doesn't exist
/// (the row is created by the P0 migration; if it's missing we're on a
/// pre-P0 DB and the probe result just has nowhere to land).
/// Uses `storage::open_db` to ensure migrations run so the schema is current.
fn write_probe_result(
    config: &HippoConfig,
    source: &str,
    ok: bool,
    lag_ms: Option<i64>,
) -> Result<()> {
    let conn = storage::open_db(&config.db_path()).context("cannot open DB for probe result")?;
    let now_ms = chrono::Utc::now().timestamp_millis();

    let rows = conn.execute(
        "UPDATE source_health SET
        probe_ok = ?1,
        probe_lag_ms = ?2,
        probe_last_run_ts = ?3,
        updated_at = ?3
        WHERE source = ?4",
        rusqlite::params![ok as i32, lag_ms, now_ms, source],
    )?;

    if rows == 0 {
        // source_health row absent (pre-P0 DB or source not registered) — not fatal.
        info!(
            "probe: no source_health row for '{}' — result not persisted",
            source
        );
    }

    Ok(())
}
