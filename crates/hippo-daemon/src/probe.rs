//! Synthetic capture probes — end-to-end liveness verification.
//!
//! Each probe sends a tagged synthetic event through the real pipeline and
//! polls the database to confirm the row appeared. Results are written to
//! `source_health` so the watchdog can evaluate invariant I-8 (probe freshness).
//!
//! Reference: docs/capture/architecture.md

use anyhow::{Context, Result};
use hippo_core::config::HippoConfig;
use hippo_core::storage;
use rusqlite::OptionalExtension;
use std::time::Instant;
use tracing::{debug, info, warn};
use uuid::Uuid;

/// Maximum time to wait for a probe row to appear in SQLite.
const POLL_DEADLINE_MS: u64 = 10_000;
const POLL_INTERVAL_MS: u64 = 200;

const VALID_PROBE_SOURCES: &[&str] = &[
    "shell",
    "claude-tool",
    "agentic-session-claude",
    "agentic-session-cursor",
    "agentic-session-opencode",
    "agentic-session-codex",
    "browser",
    "claude-auto-memory",
];

type SyncProbeFn = fn(&HippoConfig) -> Result<(bool, Option<i64>)>;

fn should_run_auto_memory_probe(config: &HippoConfig, run_all: bool, source: Option<&str>) -> bool {
    source == Some("claude-auto-memory") || (run_all && config.auto_memory.enabled)
}

fn run_sync_probe(
    config: &HippoConfig,
    run_all: bool,
    source: Option<&str>,
    name: &str,
    probe_fn: SyncProbeFn,
) -> Result<()> {
    if !(run_all || source == Some(name)) {
        return Ok(());
    }
    match probe_fn(config) {
        Ok((ok, lag)) => {
            println!(
                "[probe] {name}: {} (lag={}ms)",
                if ok { "OK" } else { "FAIL" },
                lag.map(|l| l.to_string()).as_deref().unwrap_or("N/A")
            );
            write_probe_result(config, name, ok, lag)?;
        }
        Err(e) => {
            warn!("{name} probe error: {e:#}");
            println!("[probe] {name}: ERROR — {e:#}");
            write_probe_result(config, name, false, None)?;
        }
    }
    Ok(())
}

/// Run one or all probes, then write results to `source_health`.
///
/// `source` is one of `"shell"`, `"claude-tool"`, `"agentic-session-claude"`,
/// `"agentic-session-opencode"`, `"agentic-session-codex"`,
/// `"agentic-session-cursor"`, `"browser"`, or `None` to run all in sequence.
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

    run_sync_probe(
        config,
        run_all,
        source,
        "agentic-session-claude",
        probe_claude_session,
    )?;
    run_sync_probe(
        config,
        run_all,
        source,
        "agentic-session-cursor",
        crate::probe_agentic::probe_cursor_session,
    )?;
    run_sync_probe(
        config,
        run_all,
        source,
        "agentic-session-opencode",
        crate::probe_agentic::probe_opencode_session,
    )?;
    run_sync_probe(
        config,
        run_all,
        source,
        "agentic-session-codex",
        crate::probe_agentic::probe_codex_session,
    )?;

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

    // The scheduled all-source probe must not alarm on an intentionally
    // disabled optional source. An explicit source request remains available
    // as an operator diagnostic even while ingestion is disabled.
    if should_run_auto_memory_probe(config, run_all, source) {
        run_sync_probe(
            config,
            run_all,
            source,
            "claude-auto-memory",
            crate::probe_auto_memory::probe_auto_memory,
        )?;
    } else if run_all {
        debug!("claude-auto-memory probe skipped: source disabled");
    }

    if let Some(s) = source
        && !VALID_PROBE_SOURCES.contains(&s)
    {
        anyhow::bail!(
            "unknown probe source '{s}'; valid: {}",
            VALID_PROBE_SOURCES.join(", ")
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
/// assert that the transcript's current segment fingerprints are represented
/// exactly in `agentic_sessions`. If no JSONL was recently active: trivially
/// pass (no Claude session running). If a recent JSONL has semantic changes the
/// watcher has not captured: fail.
///
/// Recursive walk covers main sessions and subagent sessions at any depth:
/// ~/.claude/projects/<project>/<session>.jsonl
/// ~/.claude/projects/<project>/<parent>/subagents/<id>.jsonl
fn probe_claude_session(config: &HippoConfig) -> Result<(bool, Option<i64>)> {
    let projects_dir = dirs::home_dir()
        .context("cannot determine home dir")?
        .join(".claude/projects");
    probe_claude_session_in_dir(config, &projects_dir, chrono::Utc::now().timestamp_millis())
}

fn probe_claude_session_in_dir(
    config: &HippoConfig,
    projects_dir: &std::path::Path,
    now_ms: i64,
) -> Result<(bool, Option<i64>)> {
    let window_ms: i64 = 5 * 60 * 1000;

    if !projects_dir.exists() {
        info!("claude-session probe: ~/.claude/projects not found — trivial pass");
        return Ok((true, None));
    }

    let mut recent_jsonl: Vec<(std::path::PathBuf, i64)> = Vec::new();
    let mut dirs_to_scan = vec![projects_dir.to_path_buf()];

    // Recursive walk to catch main sessions and subagent sessions at any depth.
    while let Some(dir) = dirs_to_scan.pop() {
        let entries = std::fs::read_dir(&dir).with_context(|| {
            format!("failed to read Claude projects directory {}", dir.display())
        })?;
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
        let (matches, latest_end) = crate::claude_session::session_file_matches_db(&db, jsonl_path)
            .with_context(|| {
                format!(
                    "failed to compare Claude transcript coverage for {}",
                    jsonl_path.display()
                )
            })?;

        if !matches {
            warn!(
                "claude-session probe: transcript contents not represented for {}",
                jsonl_path.display()
            );
            all_ok = false;
            continue;
        }

        // Only report a latency when the transcript contains a semantic event
        // near its mtime. Metadata-only touches can be hours newer than the
        // final event and do not define a meaningful capture lag.
        if let Some(end) = latest_end
            && mtime_ms.saturating_sub(end).abs() <= window_ms
        {
            let lag = now_ms.saturating_sub(end).max(0);
            latest_lag = Some(latest_lag.map_or(lag, |prev| prev.max(lag)));
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

    #[cfg(feature = "otel")]
    {
        use opentelemetry::KeyValue;
        let source_owned = source.to_owned();
        crate::metrics::PROBE_RUN.add(
            1,
            &[
                KeyValue::new("source", source_owned.clone()),
                KeyValue::new("ok", ok),
            ],
        );
        if let Some(lag) = lag_ms {
            crate::metrics::PROBE_LAG_MS
                .record(lag as f64, &[KeyValue::new("source", source_owned)]);
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::TempDir;

    #[test]
    fn scheduled_probe_skips_disabled_auto_memory() {
        let config = HippoConfig::default();
        assert!(!config.auto_memory.enabled);
        assert!(!should_run_auto_memory_probe(&config, true, None));
    }

    #[test]
    fn explicit_auto_memory_probe_remains_available_when_disabled() {
        let config = HippoConfig::default();
        assert!(should_run_auto_memory_probe(
            &config,
            false,
            Some("claude-auto-memory")
        ));
    }

    fn claude_probe_fixture() -> (TempDir, HippoConfig, std::path::PathBuf) {
        let temp = tempfile::tempdir().unwrap();
        let projects_dir = temp.path().join("claude-projects");
        let project_dir = projects_dir.join("-tmp-test");
        std::fs::create_dir_all(&project_dir).unwrap();

        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("hippo-data");
        std::fs::create_dir_all(&config.storage.data_dir).unwrap();

        (temp, config, project_dir)
    }

    #[test]
    fn claude_probe_ignores_metadata_only_mtime_changes() {
        let (_temp, config, project_dir) = claude_probe_fixture();
        let path = project_dir.join("session-touch.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();
        writeln!(
            file,
            "{}",
            crate::watch_claude_sessions::make_test_jsonl_line(
                "session-touch",
                1,
                "user",
                "old prompt"
            )
        )
        .unwrap();
        writeln!(
            file,
            "{}",
            crate::watch_claude_sessions::make_test_jsonl_line(
                "session-touch",
                2,
                "assistant",
                "old reply"
            )
        )
        .unwrap();
        drop(file);

        let db = storage::open_db(&config.db_path()).unwrap();
        assert_eq!(
            crate::claude_session::ingest_session_file(&db, &path),
            (1, 0, 0)
        );
        drop(db);

        let now_ms = chrono::Utc::now().timestamp_millis();
        let (ok, lag) =
            probe_claude_session_in_dir(&config, project_dir.parent().unwrap(), now_ms).unwrap();
        assert!(ok, "an mtime-only touch must not fail transcript coverage");
        assert_eq!(lag, None, "an old semantic event has no useful probe lag");
    }

    #[test]
    fn claude_probe_detects_uningested_semantic_changes() {
        let (_temp, config, project_dir) = claude_probe_fixture();
        let path = project_dir.join("session-drift.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();
        writeln!(
            file,
            "{}",
            crate::watch_claude_sessions::make_test_jsonl_line(
                "session-drift",
                1,
                "user",
                "first prompt"
            )
        )
        .unwrap();
        writeln!(
            file,
            "{}",
            crate::watch_claude_sessions::make_test_jsonl_line(
                "session-drift",
                2,
                "assistant",
                "first reply"
            )
        )
        .unwrap();
        drop(file);

        let db = storage::open_db(&config.db_path()).unwrap();
        assert_eq!(
            crate::claude_session::ingest_session_file(&db, &path),
            (1, 0, 0)
        );

        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        writeln!(
            file,
            "{}",
            crate::watch_claude_sessions::make_test_jsonl_line(
                "session-drift",
                3,
                "user",
                "missed append"
            )
        )
        .unwrap();
        drop(file);
        drop(db);

        let now_ms = chrono::Utc::now().timestamp_millis();
        let (ok, _) =
            probe_claude_session_in_dir(&config, project_dir.parent().unwrap(), now_ms).unwrap();
        assert!(!ok, "an uningested semantic append must fail coverage");

        let db = storage::open_db(&config.db_path()).unwrap();
        assert_eq!(
            crate::claude_session::ingest_session_file(&db, &path),
            (1, 0, 0)
        );
        drop(db);
        let (ok, _) = probe_claude_session_in_dir(
            &config,
            project_dir.parent().unwrap(),
            chrono::Utc::now().timestamp_millis(),
        )
        .unwrap();
        assert!(ok, "ingesting the append must restore coverage");
    }
}
