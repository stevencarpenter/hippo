//! Synthetic capture probes — end-to-end liveness verification.
//!
//! Each probe sends a tagged synthetic event through the real pipeline and
//! polls the database to confirm the row appeared. Results are written to
//! `source_health` so the watchdog can evaluate invariant I-8 (probe freshness).
//!
//! Reference: docs/capture/architecture.md

use anyhow::{Context, Result};
use hippo_core::config::HippoConfig;
use hippo_core::redaction::RedactionEngine;
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
/// `source` is one of `"shell"`, `"claude-tool"`, `"agentic-session-claude"`,
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

    if run_all || source == Some("agentic-session-claude") {
        match probe_claude_session(config) {
            Ok((ok, lag)) => {
                println!(
                    "[probe] agentic-session-claude: {} (lag={}ms)",
                    if ok { "OK" } else { "FAIL" },
                    lag.map(|l| l.to_string()).as_deref().unwrap_or("N/A")
                );
                write_probe_result(config, "agentic-session-claude", ok, lag)?;
            }
            Err(e) => {
                warn!("agentic-session-claude probe error: {e:#}");
                println!("[probe] agentic-session-claude: ERROR — {e:#}");
                write_probe_result(config, "agentic-session-claude", false, None)?;
            }
        }
    }

    if run_all || source == Some("agentic-session-cursor") {
        match probe_cursor_session(config) {
            Ok((ok, lag)) => {
                println!(
                    "[probe] agentic-session-cursor: {} (lag={}ms)",
                    if ok { "OK" } else { "FAIL" },
                    lag.map(|l| l.to_string()).as_deref().unwrap_or("N/A")
                );
                write_probe_result(config, "agentic-session-cursor", ok, lag)?;
            }
            Err(e) => {
                warn!("agentic-session-cursor probe error: {e:#}");
                println!("[probe] agentic-session-cursor: ERROR — {e:#}");
                write_probe_result(config, "agentic-session-cursor", false, None)?;
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
        && !matches!(
            s,
            "shell"
                | "claude-tool"
                | "agentic-session-claude"
                | "browser"
                | "agentic-session-cursor"
        )
    {
        anyhow::bail!(
            "unknown probe source '{}'; valid: shell, claude-tool, agentic-session-claude, browser, agentic-session-cursor",
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
            .with_context(|| format!("failed to query claude_sessions for {}", path_str))?;

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

/// Settle floor for the cursor probe eligibility window, in ms.
///
/// A transcript must be at least this old before we assert it was ingested.
/// Decoupled from `cursor.min_idle_secs` on purpose: deriving the floor from
/// config (the old `2 * min_idle` formula) let an operator with a large
/// `min_idle_secs` push the floor past `CURSOR_PROBE_WINDOW_MS`, collapsing the
/// eligibility window to empty so the probe silently trivial-passed and stopped
/// covering the source. 90 s ≈ one 60 s poll interval plus margin, which is
/// enough slack that the poller has had a chance to ingest a settled file.
const CURSOR_PROBE_SETTLE_MS: i64 = 90_000;

/// Outer edge of the cursor probe eligibility window, in ms.
///
/// Widened to 10 min (from the 5 min used by the Claude probe) so that a file
/// which becomes eligible at `CURSOR_PROBE_SETTLE_MS` (90 s) is still in-window
/// when the next ~5 min probe firing lands: the window span is
/// `600_000 - 90_000 = 510_000 ms`, comfortably wider than the 300 s probe
/// interval, so a settled transcript is asserted by at least one probe run
/// rather than slipping between firings (the coverage-gap concern).
const CURSOR_PROBE_WINDOW_MS: i64 = 600_000;

/// Cursor-session probe: assertion-based, mirrors `probe_claude_session`.
///
/// For every `~/.cursor/projects/**/agent-transcripts/**/*.jsonl` whose age
/// (`now - mtime`) falls in `[settle_ms, CURSOR_PROBE_WINDOW_MS]`, assert a
/// `claude_sessions` row exists with that `source_file` — *but only when the
/// transcript actually yields ≥1 segment*. A legitimately segment-less
/// transcript (assistant-only, no user turn) is correctly written as zero rows
/// by `cursor_session::poll_tick`, so asserting a row for it would be a false
/// FAIL; such files are skipped (treated as pass).
///
/// `settle_ms` is clamped strictly below `CURSOR_PROBE_WINDOW_MS` so the window
/// is always non-empty regardless of config — see `CURSOR_PROBE_SETTLE_MS`.
/// Lag is reported as `now - MAX(end_time)` of the matched rows (true ingestion
/// latency), mirroring `probe_claude_session`, not the file's age.
fn probe_cursor_session(config: &HippoConfig) -> Result<(bool, Option<i64>)> {
    // A disabled source intentionally ingests nothing (`poll_tick` early-returns),
    // so transcripts left on disk will never have matching `claude_sessions` rows.
    // Asserting against them would write `probe_ok = 0` and trip watchdog I-8 as a
    // false alarm. Trivially pass instead, mirroring `poll_tick`'s disabled guard.
    if !config.cursor.enabled {
        info!("cursor-session probe: cursor ingestion disabled — trivial pass");
        return Ok((true, None));
    }
    let now_ms = chrono::Utc::now().timestamp_millis();
    let window_ms: i64 = CURSOR_PROBE_WINDOW_MS;
    // Clamp the settle floor strictly below the window so `[settle_ms, window_ms]`
    // can never be empty (the empty-window bug that silently disabled coverage).
    let settle_ms: i64 = CURSOR_PROBE_SETTLE_MS.min(window_ms / 2);

    let roots = &config.cursor.session_roots;
    let mut recent: Vec<(std::path::PathBuf, i64)> = Vec::new();
    for root in roots {
        if !root.is_dir() {
            continue;
        }
        for entry in walkdir::WalkDir::new(root)
            .into_iter()
            .filter_map(|e| e.ok())
        {
            let path = entry.path();
            let is_jsonl = path.extension().map(|e| e == "jsonl").unwrap_or(false);
            let under = path
                .components()
                .any(|c| c.as_os_str() == "agent-transcripts");
            if !(is_jsonl && under) {
                continue;
            }
            let Some(mtime_ms) = entry.metadata().ok().and_then(|m| {
                m.modified().ok().and_then(|t| {
                    t.duration_since(std::time::UNIX_EPOCH)
                        .ok()
                        .map(|d| d.as_millis() as i64)
                })
            }) else {
                continue;
            };
            let age = now_ms - mtime_ms;
            if age >= settle_ms && age <= window_ms {
                recent.push((path.to_path_buf(), mtime_ms));
            }
        }
    }

    if recent.is_empty() {
        info!("cursor-session probe: no settled recent transcripts — trivial pass");
        return Ok((true, None));
    }

    let db =
        storage::open_db(&config.db_path()).context("cannot open DB for cursor-session probe")?;
    // Lazy-load the redaction engine: only the DB-miss branch below actually
    // needs it (to re-parse a file we suspect was never written). The happy
    // path — file present, row exists — skips disk I/O for `redact.toml` and
    // the regex recompile entirely.
    let mut redaction: Option<RedactionEngine> = None;
    let mut all_ok = true;
    let mut latest_lag: Option<i64> = None;
    for (path, mtime_ms) in &recent {
        let path_str = path.to_string_lossy();

        // Cheap path first: ask the DB whether this transcript already has a
        // row in the same window the existence check uses. The common case is
        // that `poll_tick` already ingested the file and wrote a row, so the
        // probe satisfies its assertion without touching the file or running
        // any redaction patterns — a meaningful saving for a directory with
        // many in-window transcripts.
        let count: i64 = db
            .query_row(
                "SELECT COUNT(*) FROM claude_sessions
                 WHERE source_file = ?1 AND probe_tag IS NULL AND end_time >= ?2",
                rusqlite::params![path_str.as_ref(), mtime_ms - window_ms],
                |row| row.get(0),
            )
            .with_context(|| format!("failed to query claude_sessions for {}", path_str))?;

        if count > 0 {
            // Assertion satisfied. Lag = now - MAX(end_time) of the matched
            // rows — true ingestion latency (mirrors probe_claude_session),
            // not the file's age.
            let max_end: Option<i64> = db
                .query_row(
                    "SELECT MAX(end_time) FROM claude_sessions
                     WHERE source_file = ?1 AND probe_tag IS NULL",
                    rusqlite::params![path_str.as_ref()],
                    |row| row.get(0),
                )
                .with_context(|| {
                    format!(
                        "cursor-session probe: failed to query MAX(end_time) for {}",
                        path_str
                    )
                })?;
            if let Some(end) = max_end {
                let lag = now_ms - end;
                latest_lag = Some(latest_lag.map_or(lag, |p: i64| p.max(lag)));
            }
            continue;
        }

        // DB miss: parse to decide whether a row was even expected.
        // `poll_tick` writes zero rows for a segment-less transcript (no user
        // turn), so we must not assert a row exists for one. Mirror the real
        // ingestion exactly by parsing with the same extractor it uses; a read
        // error is transient (the poller will retry), so we skip rather than
        // FAIL on it. This is the only branch that pays parse + redaction
        // cost, so we lazy-load the redaction engine on first use.
        let engine = match redaction.as_ref() {
            Some(r) => r,
            None => {
                redaction = Some(crate::load_redaction_engine(config));
                redaction.as_ref().expect("just set above")
            }
        };
        let segment_count = match crate::cursor_session::extract_segments(path, *mtime_ms, engine) {
            Ok(segs) => segs.len(),
            Err(e) => {
                warn!(
                    "cursor-session probe: cannot parse {} ({e:#}) — skipping",
                    path_str
                );
                continue;
            }
        };
        if segment_count == 0 {
            info!(
                "cursor-session probe: {} yields no segments — no row expected, skipping",
                path_str
            );
            continue;
        }
        // Segment-bearing transcript with no row: genuine FAIL.
        warn!("cursor-session probe: no row for {}", path_str);
        all_ok = false;
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
    use hippo_core::config::HippoConfig;
    use std::path::{Path, PathBuf};
    use std::time::{Duration, SystemTime};

    /// Build a `HippoConfig` whose data dir and cursor session root live under
    /// `tmp`. The DB is created lazily by the probe via `storage::open_db`.
    fn test_config(tmp: &Path, root: &Path) -> HippoConfig {
        let data = tmp.join("data");
        std::fs::create_dir_all(&data).unwrap();
        let mut config = HippoConfig::default();
        config.storage.data_dir = data;
        config.cursor.session_roots = vec![root.to_path_buf()];
        config
    }

    /// Write a transcript at `<root>/<slug>/agent-transcripts/<id>/<id>.jsonl`
    /// with the given body, then backdate its mtime to `age` ago so it lands in
    /// the probe's eligibility window. Returns the file path.
    fn write_transcript(root: &Path, id: &str, body: &str, age: Duration) -> PathBuf {
        let dir = root
            .join("Users-x-projects-foo")
            .join("agent-transcripts")
            .join(id);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(format!("{id}.jsonl"));
        std::fs::write(&path, body).unwrap();
        let mtime = SystemTime::now() - age;
        filetime::set_file_mtime(&path, filetime::FileTime::from_system_time(mtime)).unwrap();
        path
    }

    /// A one-turn user+assistant transcript that yields exactly one segment.
    fn user_transcript() -> String {
        [
            r#"{"role":"user","message":{"content":[{"type":"text","text":"<user_query>\nfix the build\n</user_query>"}]}}"#,
            r#"{"role":"assistant","message":{"content":[{"type":"text","text":"On it."}]}}"#,
        ]
        .join("\n")
    }

    /// An assistant-only transcript: no user turn, so `extract_segments` yields
    /// zero segments and `poll_tick` writes no row.
    fn assistant_only_transcript() -> String {
        r#"{"role":"assistant","message":{"content":[{"type":"text","text":"orphaned reply"}]}}"#
            .to_string()
    }

    /// Ingest `path` through the real cursor pipeline so a matching
    /// `claude_sessions` row exists exactly as production would write it
    /// (`source_file = path`, `end_time = file mtime`).
    fn ingest(config: &HippoConfig, path: &Path) -> usize {
        crate::cursor_session::ingest_one(config, path).unwrap()
    }

    #[test]
    fn cursor_probe_trivial_pass_when_no_transcripts() {
        let tmp = tempfile::tempdir().unwrap();
        let config = test_config(tmp.path(), &tmp.path().join("nonexistent"));
        let (ok, lag) = super::probe_cursor_session(&config).unwrap();
        assert!(ok);
        assert_eq!(lag, None);
    }

    /// Happy path: an in-window transcript that yields a segment and has a
    /// matching ingested row → (true, Some(lag)).
    #[test]
    fn cursor_probe_happy_path_in_window_with_row() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let config = test_config(tmp.path(), &root);
        // ~3 min old: past the 90 s settle, inside the 10 min window.
        let path = write_transcript(
            &root,
            "happy-1",
            &user_transcript(),
            Duration::from_secs(180),
        );
        assert_eq!(ingest(&config, &path), 1, "expected one ingested segment");

        let (ok, lag) = super::probe_cursor_session(&config).unwrap();
        assert!(ok, "probe should pass when the in-window row exists");
        let lag = lag.expect("happy path should report a lag");
        assert!(lag >= 0, "lag must be non-negative, got {lag}");
    }

    /// Genuine failure: an in-window transcript that SHOULD have a row (it
    /// yields a segment) but none was ingested → (false, _).
    #[test]
    fn cursor_probe_fails_when_expected_row_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let config = test_config(tmp.path(), &root);
        // Settled, in-window, yields a segment — but we never ingest it.
        write_transcript(
            &root,
            "missing-1",
            &user_transcript(),
            Duration::from_secs(180),
        );

        let (ok, _lag) = super::probe_cursor_session(&config).unwrap();
        assert!(
            !ok,
            "probe must FAIL when a segment-bearing in-window transcript has no row"
        );
    }

    /// Disabled-source guard (#3): with `[cursor] enabled = false` the poller
    /// ingests nothing, so a settled segment-bearing transcript has no row — but
    /// the probe must trivially PASS rather than write `probe_ok = 0` and trip
    /// watchdog I-8 on an intentionally disabled source. Mirror of
    /// `cursor_probe_fails_when_expected_row_missing` with the source disabled.
    #[test]
    fn cursor_probe_trivial_pass_when_disabled() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let mut config = test_config(tmp.path(), &root);
        config.cursor.enabled = false;
        // Settled, in-window, segment-bearing — would FAIL if enabled, but it
        // was never ingested because the source is off.
        write_transcript(
            &root,
            "disabled-1",
            &user_transcript(),
            Duration::from_secs(180),
        );

        let (ok, lag) = super::probe_cursor_session(&config).unwrap();
        assert!(ok, "disabled cursor probe must trivially pass, not FAIL");
        assert_eq!(lag, None);
    }

    /// Zero-segment skip (finding #2): an assistant-only in-window transcript
    /// correctly has no row, and the probe must treat that as a pass — not a
    /// false FAIL.
    #[test]
    fn cursor_probe_skips_zero_segment_transcript() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let config = test_config(tmp.path(), &root);
        let path = write_transcript(
            &root,
            "empty-1",
            &assistant_only_transcript(),
            Duration::from_secs(180),
        );
        // Confirm the contract: real ingestion writes zero rows for this file.
        assert_eq!(
            ingest(&config, &path),
            0,
            "assistant-only yields no segments"
        );

        let (ok, lag) = super::probe_cursor_session(&config).unwrap();
        assert!(
            ok,
            "probe must pass: a segment-less transcript correctly has no row"
        );
        assert_eq!(lag, None, "no asserted rows → no lag");
    }

    /// High `min_idle_secs` non-blindness (finding #1): with `min_idle_secs =
    /// 300`, the old `settle = 2 * min_idle = 600 s` collapsed the window to
    /// empty so the probe trivial-passed even with a missing row. The fixed,
    /// clamped settle keeps a ~200 s-old transcript in-window, so a missing
    /// row is still caught.
    #[test]
    fn cursor_probe_not_blind_with_high_min_idle() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let mut config = test_config(tmp.path(), &root);
        config.cursor.min_idle_secs = 300;
        // ~200 s old: would be excluded by the old 600 s settle floor, but is
        // in-window now. It yields a segment and has no row → must FAIL.
        write_transcript(
            &root,
            "blind-1",
            &user_transcript(),
            Duration::from_secs(200),
        );

        let (ok, _lag) = super::probe_cursor_session(&config).unwrap();
        assert!(
            !ok,
            "probe must still assert (not be blind) when min_idle_secs is large"
        );
    }

    /// Lag semantics (finding #3): lag is `now - MAX(end_time)` of the matched
    /// rows, NOT the file's age (`now - mtime`). We insert a row whose
    /// `end_time` is far in the past relative to the file's recent mtime, then
    /// assert the reported lag reflects `end_time`, not mtime.
    #[test]
    fn cursor_probe_lag_reflects_end_time_not_mtime() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let config = test_config(tmp.path(), &root);
        // File mtime is ~3 min old. The row we insert below carries an
        // end_time that is much more recent (~1 min ago) — distinct from the
        // mtime but still inside the staleness guard (end_time >= mtime - window).
        // If lag were `now - mtime` it would be ~180 s; because lag is
        // `now - end_time` it must be ~60 s instead.
        let file_age = Duration::from_secs(180);
        let path = write_transcript(&root, "lag-1", &user_transcript(), file_age);

        let now_ms = chrono::Utc::now().timestamp_millis();
        let end_time = now_ms - 60_000; // 1 min ago — recent, in-window.
        let conn = hippo_core::storage::open_db(&config.db_path()).unwrap();
        let seg = crate::cursor_session::CursorSegment {
            session_id: "lag-1".into(),
            project_dir: "foo".into(),
            cwd: "/work/foo".into(),
            segment_index: 0,
            start_time: end_time,
            end_time,
            user_prompts: vec!["do a thing".into()],
            assistant_texts: vec![],
            tool_calls: vec![],
            message_count: 1,
            source_file: path.to_string_lossy().into_owned(),
            is_subagent: false,
            parent_session_id: None,
        };
        crate::cursor_session::upsert_segment(&conn, &seg).unwrap();

        let (ok, lag) = super::probe_cursor_session(&config).unwrap();
        assert!(ok, "row exists → probe passes");
        let lag = lag.expect("should report lag");
        let file_age_ms = file_age.as_millis() as i64;
        // Lag tracks end_time (~60 s), which is well under the file's age
        // (~180 s). If the probe still used `now - mtime` this would be ~180 s.
        assert!(
            lag < file_age_ms - 30_000,
            "lag ({lag}ms) must reflect end_time (~60s), not file mtime (~{file_age_ms}ms)"
        );
        // And it must be in the right neighbourhood of end_time (~60 s),
        // allowing generous slack for test-execution time.
        assert!(
            (30_000..120_000).contains(&lag),
            "lag ({lag}ms) should be ~60s (now - end_time)"
        );
    }
}
