use anyhow::Result;
use chrono::Utc;
use hippo_core::config::{ENV_ALLOWLIST, HippoConfig};
use hippo_core::events::{CapturedOutput, EventEnvelope, EventPayload, GitState, ShellEvent};
use hippo_core::protocol::{DaemonRequest, DaemonResponse};
use hippo_core::redaction::RedactionEngine;
use hippo_core::storage;
use rusqlite::OptionalExtension as _;
use std::collections::HashMap;
use std::path::PathBuf;
use tokio::net::UnixStream;
use uuid::Uuid;
use walkdir::WalkDir;

use crate::codex_session;
use crate::framing::{read_frame, write_frame};

const REQUEST_TIMEOUT_MS: u64 = 5_000;

/// 10-minute idle window shared by the opencode and Codex `hippo doctor`
/// idle probes: a poller-backed source whose backing files have not changed
/// within this window is "idle" (user not using it), not "broken".
const IDLE_WINDOW_SECS: u64 = 600;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SocketProbeResult {
    Missing,
    Responsive,
    Stale,
    Unresponsive,
}

pub async fn send_request(
    socket_path: &std::path::Path,
    request: &DaemonRequest,
) -> Result<DaemonResponse> {
    send_request_with_timeout(socket_path, request, REQUEST_TIMEOUT_MS).await
}

pub async fn send_request_with_timeout(
    socket_path: &std::path::Path,
    request: &DaemonRequest,
    timeout_ms: u64,
) -> Result<DaemonResponse> {
    let timeout = std::time::Duration::from_millis(timeout_ms);
    let exchange = async {
        let mut stream = UnixStream::connect(socket_path).await?;
        let json = serde_json::to_vec(request)?;
        write_frame(&mut stream, &json).await?;
        let frame = read_frame(&mut stream)
            .await?
            .ok_or_else(|| anyhow::anyhow!("no response from daemon"))?;
        let response: DaemonResponse = serde_json::from_slice(&frame)?;
        anyhow::Ok(response)
    };

    tokio::time::timeout(timeout, exchange)
        .await
        .map_err(|_| anyhow::anyhow!("timed out waiting for daemon response"))?
}

pub async fn probe_socket(socket_path: &std::path::Path, timeout_ms: u64) -> SocketProbeResult {
    if !socket_path.exists() {
        return SocketProbeResult::Missing;
    }

    let timeout = std::time::Duration::from_millis(timeout_ms);
    let connect_result = match tokio::time::timeout(timeout, UnixStream::connect(socket_path)).await
    {
        Ok(result) => result,
        Err(_) => return SocketProbeResult::Unresponsive,
    };

    let mut stream = match connect_result {
        Ok(stream) => stream,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return SocketProbeResult::Missing,
        Err(e) if e.kind() == std::io::ErrorKind::ConnectionRefused => {
            return SocketProbeResult::Stale;
        }
        Err(_) => return SocketProbeResult::Unresponsive,
    };

    let request = match serde_json::to_vec(&DaemonRequest::GetStatus) {
        Ok(request) => request,
        Err(_) => return SocketProbeResult::Unresponsive,
    };

    let exchange = async {
        write_frame(&mut stream, &request).await?;
        let frame = read_frame(&mut stream).await?;
        anyhow::Ok(frame)
    };

    match tokio::time::timeout(timeout, exchange).await {
        Ok(Ok(Some(frame))) if serde_json::from_slice::<DaemonResponse>(&frame).is_ok() => {
            SocketProbeResult::Responsive
        }
        Ok(Ok(_)) => SocketProbeResult::Unresponsive,
        Ok(Err(_)) => SocketProbeResult::Unresponsive,
        Err(_) => SocketProbeResult::Unresponsive,
    }
}

/// Fire-and-forget event send. Returns Ok(()) once the frame is written to the socket.
///
/// Durability contract: success means the event was accepted by the daemon socket.
/// It does NOT mean the event has been written to SQLite. If the daemon crashes
/// after accept but before the next periodic flush, the event may be lost.
///
/// The fallback JSONL path is triggered only when the socket is unreachable — not
/// when the daemon crashes after accepting the event.
pub async fn send_event_fire_and_forget(
    socket_path: &std::path::Path,
    envelope: &EventEnvelope,
    timeout_ms: u64,
) -> Result<()> {
    let mut stream = tokio::time::timeout(
        std::time::Duration::from_millis(timeout_ms),
        UnixStream::connect(socket_path),
    )
    .await
    .map_err(|_| anyhow::anyhow!("timed out connecting to daemon socket"))??;

    let request = DaemonRequest::IngestEvent(Box::new(envelope.clone()));
    let json = serde_json::to_vec(&request)?;
    write_frame(&mut stream, &json).await?;
    Ok(())
}

fn load_redaction_engine(config: &HippoConfig) -> RedactionEngine {
    crate::load_redaction_engine(config)
}

fn format_optional_brain_field(label: &str, value: Option<&str>) -> Option<String> {
    value
        .filter(|s| !s.is_empty())
        .map(|s| format!("[OK] Brain {}: {}", label, s))
}

/// Fetch and print brain /health details.
///
/// Returns the raw JSON on success so callers can reuse it (e.g. Check 10 for
/// schema-version comparison) without issuing a second HTTP request.
async fn print_brain_health_details(
    config: &HippoConfig,
    client: &reqwest::Client,
) -> Option<serde_json::Value> {
    let brain_url = format!("http://localhost:{}/health", config.brain.port);
    match client.get(&brain_url).send().await {
        Ok(resp) if resp.status().is_success() => {
            println!("[OK] Brain server reachable");

            match resp.json::<serde_json::Value>().await {
                Ok(json) => {
                    let queue_depth = json
                        .get("queue_depth")
                        .and_then(|v| v.as_u64())
                        .unwrap_or_default()
                        + json
                            .get("claude_queue_depth")
                            .and_then(|v| v.as_u64())
                            .unwrap_or_default()
                        + json
                            .get("browser_queue_depth")
                            .and_then(|v| v.as_u64())
                            .unwrap_or_default()
                        + json
                            .get("workflow_queue_depth")
                            .and_then(|v| v.as_u64())
                            .unwrap_or_default();
                    let queue_failed = json
                        .get("queue_failed")
                        .and_then(|v| v.as_u64())
                        .unwrap_or_default()
                        + json
                            .get("claude_queue_failed")
                            .and_then(|v| v.as_u64())
                            .unwrap_or_default()
                        + json
                            .get("browser_queue_failed")
                            .and_then(|v| v.as_u64())
                            .unwrap_or_default()
                        + json
                            .get("workflow_queue_failed")
                            .and_then(|v| v.as_u64())
                            .unwrap_or_default();
                    let enrichment_running = json
                        .get("enrichment_running")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false);
                    // Accept the new field name (`inference_reachable`) first;
                    // fall back to the legacy `lmstudio_reachable` so a brain
                    // running an older binary against a newer daemon doesn't
                    // make this check flap.
                    let inference_reachable = json
                        .get("inference_reachable")
                        .or_else(|| json.get("lmstudio_reachable"))
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false);
                    let db_reachable = json
                        .get("db_reachable")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false);
                    let last_success_at_ms = json
                        .get("last_success_at_ms")
                        .and_then(|v| v.as_i64())
                        .map(|v| v.to_string());
                    let last_error = json
                        .get("last_error")
                        .and_then(|v| v.as_str())
                        .filter(|s| !s.is_empty())
                        .map(|s| s.to_string());

                    let brain_version = json
                        .get("version")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown");
                    let daemon_version = env!("HIPPO_VERSION_FULL");
                    if brain_version == daemon_version {
                        println!("[OK] Brain version match");
                    } else {
                        println!(
                            "[!!] Brain version mismatch: brain={}, daemon={}",
                            brain_version, daemon_version
                        );
                    }

                    let queue_tag = if queue_failed > 0 { "[WW]" } else { "[OK]" };
                    println!(
                        "{} Brain queue depth: {} pending, {} failed",
                        queue_tag, queue_depth, queue_failed
                    );
                    if inference_reachable {
                        println!("[OK] Brain inference backend: reachable");
                    } else {
                        println!("[!!] Brain inference backend: unreachable");
                    }
                    if db_reachable {
                        println!("[OK] Brain DB: reachable");
                    } else {
                        println!("[!!] Brain DB: unreachable");
                    }
                    println!(
                        "[OK] Brain enrichment loop: {}",
                        if enrichment_running {
                            "running"
                        } else {
                            "not running"
                        }
                    );

                    if let Some(drift) = json
                        .get("embed_model_drift")
                        .and_then(|v| v.as_str())
                        .filter(|s| !s.is_empty())
                    {
                        println!("[!!] Brain embed model drift: {}", drift);
                    }

                    if let Some(line) = format_optional_brain_field(
                        "last success ms",
                        last_success_at_ms.as_deref(),
                    ) {
                        println!("{}", line);
                    }
                    if let Some(line) =
                        format_optional_brain_field("last error", last_error.as_deref())
                    {
                        println!("{}", line);
                    }
                    Some(json)
                }
                Err(err) => {
                    println!(
                        "[!!] Brain server reachable but returned unreadable health JSON: {}",
                        err
                    );
                    None
                }
            }
        }
        _ => {
            println!(
                "[!!] Brain server not reachable on port {}",
                config.brain.port
            );
            None
        }
    }
}

fn redacted_fallback_envelope(
    envelope: &EventEnvelope,
    redaction: &RedactionEngine,
) -> EventEnvelope {
    let EventPayload::Shell(shell) = &envelope.payload else {
        return envelope.clone();
    };

    let (redacted, _hits) = crate::redact_shell_event(shell, redaction);
    EventEnvelope {
        envelope_id: envelope.envelope_id,
        producer_version: envelope.producer_version,
        timestamp: envelope.timestamp,
        payload: EventPayload::Shell(redacted),
        probe_tag: envelope.probe_tag.clone(),
    }
}

#[allow(clippy::too_many_arguments)]
pub async fn handle_send_event_shell(
    config: &HippoConfig,
    cmd: String,
    exit: i32,
    cwd: String,
    duration_ms: u64,
    git_repo: Option<String>,
    git_branch: Option<String>,
    git_commit: Option<String>,
    git_dirty: bool,
    output: Option<String>,
    probe_tag: Option<String>,
    source_kind: Option<String>,
    tool_name: Option<String>,
) -> Result<()> {
    let session_id = std::env::var("HIPPO_SESSION_ID")
        .ok()
        .and_then(|s| Uuid::parse_str(&s).ok())
        .unwrap_or_else(Uuid::new_v4);

    let hostname = hostname::get()
        .map(|h| h.to_string_lossy().to_string())
        .unwrap_or_else(|_| "unknown".to_string());

    // Caller-supplied value wins (shell hook caches it). Otherwise derive
    // from cwd once per invocation — cheap, and the shell path only pays
    // this when its cache misses or the hook is an older build.
    // cwd comes from the local shell hook (this user's own $PWD), not a remote source.
    let git_repo = git_repo
        .filter(|s| !s.is_empty())
        // nosemgrep: rust.actix.path-traversal.tainted-path.tainted-path
        .or_else(|| crate::git_repo::derive_git_repo(std::path::Path::new(&cwd)));

    let git_state = if git_repo.is_some() || git_branch.is_some() || git_commit.is_some() {
        Some(GitState {
            repo: git_repo,
            branch: git_branch,
            commit: git_commit,
            is_dirty: git_dirty,
        })
    } else {
        None
    };

    let env_snapshot: HashMap<String, String> = std::env::vars()
        .filter(|(k, _)| ENV_ALLOWLIST.contains(&k.as_str()))
        .collect();

    // When --source-kind claude-tool is passed, use the provided tool_name.
    // When --tool-name is passed without explicit source-kind, treat as claude-tool too.
    let effective_tool_name =
        if source_kind.as_deref() == Some("claude-tool") || tool_name.is_some() {
            tool_name
        } else {
            None
        };

    let event = ShellEvent {
        session_id,
        command: cmd,
        exit_code: exit,
        duration_ms,
        // cwd is the user's own $PWD forwarded from the local shell hook.
        // nosemgrep: rust.actix.path-traversal.tainted-path.tainted-path
        cwd: PathBuf::from(cwd),
        hostname,
        shell: crate::detect_shell_kind(),
        stdout: output.as_ref().map(|o| CapturedOutput {
            content: o.clone(),
            truncated: false,
            original_bytes: o.len(),
        }),
        stderr: None,
        env_snapshot,
        git_state,
        redaction_count: 0,
        tool_name: effective_tool_name,
    };

    let envelope = EventEnvelope {
        probe_tag: probe_tag.clone(),
        ..EventEnvelope::shell(event)
    };
    match send_event_fire_and_forget(
        &config.socket_path(),
        &envelope,
        config.daemon.socket_timeout_ms,
    )
    .await
    {
        Ok(()) => Ok(()),
        Err(_) => {
            let redaction = load_redaction_engine(config);
            let fallback = redacted_fallback_envelope(&envelope, &redaction);
            storage::write_fallback_jsonl(&config.fallback_dir(), &fallback)?;
            Ok(())
        }
    }
}

pub async fn handle_status(config: &HippoConfig) -> Result<()> {
    let response = send_request(&config.socket_path(), &DaemonRequest::GetStatus).await?;
    match response {
        DaemonResponse::Status(status) => {
            println!("Hippo Daemon Status");
            println!("  Uptime:            {}s", status.uptime_secs);
            println!("  Events today:      {}", status.events_today);
            println!("  Sessions today:    {}", status.sessions_today);
            println!("  Queue depth:       {}", status.queue_depth);
            println!("  Queue failed:      {}", status.queue_failed);
            println!("  Drop count:        {}", status.drop_count);
            println!("  DB size:           {} bytes", status.db_size_bytes);
            println!("  Fallback pending:  {}", status.fallback_files_pending);
            println!(
                "  Inference:        {}",
                if status.inference_reachable {
                    "reachable"
                } else {
                    "unreachable"
                }
            );
            println!(
                "  Brain:            {}",
                if status.brain_reachable {
                    "reachable"
                } else {
                    "unreachable"
                }
            );
        }
        DaemonResponse::Error(e) => eprintln!("Error: {}", e),
        _ => eprintln!("Unexpected response"),
    }
    Ok(())
}

pub async fn handle_sessions(
    config: &HippoConfig,
    today: bool,
    since: Option<String>,
) -> Result<()> {
    let since_ms = if today {
        let now = Utc::now();
        Some(
            now.date_naive()
                .and_hms_opt(0, 0, 0)
                .unwrap()
                .and_utc()
                .timestamp_millis(),
        )
    } else {
        since.as_deref().and_then(parse_duration_to_since_ms)
    };

    let response = send_request(
        &config.socket_path(),
        &DaemonRequest::GetSessions {
            since_ms,
            limit: Some(50),
        },
    )
    .await?;

    match response {
        DaemonResponse::Sessions(sessions) => {
            if sessions.is_empty() {
                println!("No sessions found.");
                return Ok(());
            }
            for s in &sessions {
                let time = chrono::DateTime::from_timestamp_millis(s.start_time)
                    .map(|dt| dt.format("%Y-%m-%d %H:%M").to_string())
                    .unwrap_or_else(|| "unknown".to_string());
                println!(
                    "[{}] {} | {} | {} | {} events{}",
                    s.id,
                    time,
                    s.hostname,
                    s.shell,
                    s.event_count,
                    s.summary
                        .as_ref()
                        .map(|s| format!(" | {}", s))
                        .unwrap_or_default()
                );
            }
        }
        DaemonResponse::Error(e) => eprintln!("Error: {}", e),
        _ => eprintln!("Unexpected response"),
    }
    Ok(())
}

pub async fn handle_events(
    config: &HippoConfig,
    session: Option<i64>,
    since: Option<String>,
    project: Option<String>,
) -> Result<()> {
    let since_ms = since.as_deref().and_then(parse_duration_to_since_ms);

    let response = send_request(
        &config.socket_path(),
        &DaemonRequest::GetEvents {
            session_id: session,
            since_ms,
            project,
            limit: Some(50),
        },
    )
    .await?;

    match response {
        DaemonResponse::Events(events) => {
            if events.is_empty() {
                println!("No events found.");
                return Ok(());
            }
            for e in &events {
                let time = chrono::DateTime::from_timestamp_millis(e.timestamp)
                    .map(|dt| dt.format("%H:%M:%S").to_string())
                    .unwrap_or_else(|| "??:??:??".to_string());
                let exit = e.exit_code.map(|c| format!(" [{}]", c)).unwrap_or_default();
                let branch = e
                    .git_branch
                    .as_ref()
                    .map(|b| format!(" ({})", b))
                    .unwrap_or_default();
                println!(
                    "{} {:>6}ms{}{} {} | {}",
                    time, e.duration_ms, exit, branch, e.cwd, e.command
                );
            }
        }
        DaemonResponse::Error(e) => eprintln!("Error: {}", e),
        _ => eprintln!("Unexpected response"),
    }
    Ok(())
}

pub async fn handle_query_raw(config: &HippoConfig, text: &str) -> Result<()> {
    let response = send_request(
        &config.socket_path(),
        &DaemonRequest::RawQuery {
            text: text.to_string(),
        },
    )
    .await?;

    match response {
        DaemonResponse::QueryResult(hits) => {
            if hits.is_empty() {
                println!("No results found.");
                return Ok(());
            }
            for h in &hits {
                let time = chrono::DateTime::from_timestamp_millis(h.timestamp)
                    .map(|dt| dt.format("%Y-%m-%d %H:%M").to_string())
                    .unwrap_or_else(|| "unknown".to_string());
                println!("{} {} | {}", time, h.cwd, h.command);
            }
        }
        DaemonResponse::Error(e) => eprintln!("Error: {}", e),
        _ => eprintln!("Unexpected response"),
    }
    Ok(())
}

pub fn handle_redact_test(config: &HippoConfig, input: &str) {
    let engine = load_redaction_engine(config);
    let matches = engine.test_string(input);
    if matches.is_empty() {
        println!("No patterns matched.");
    } else {
        println!("Matched patterns: {}", matches.join(", "));
    }
    let result = engine.redact(input);
    println!("Redacted ({} replacements):", result.count);
    println!("  {}", result.text);
}

// ---------------------------------------------------------------------------
// Alarms commands
// ---------------------------------------------------------------------------

struct AlarmRow {
    id: i64,
    invariant_id: String,
    raised_at: i64,
    details_json: String,
    resolved_at: Option<i64>,
}

/// List un-acknowledged `capture_alarms` rows, grouped by status.
///
/// Two sections:
///   * **ACTIVE** — `resolved_at IS NULL`; the underlying invariant is
///     still violating. These contribute to the exit code.
///   * **AUTO-RESOLVED** — `resolved_at IS NOT NULL` and `acked_at IS
///     NULL`; the watchdog cleared the invariant but the user hasn't
///     ack'd. Informational only.
///
/// Returns `true` if any *active* rows exist (caller should `exit(1)`),
/// `false` otherwise. Auto-resolved rows never set the return to `true`.
pub fn handle_alarms_list(config: &HippoConfig) -> Result<bool> {
    let conn = hippo_core::storage::open_db(&config.db_path())?;

    let mut stmt = conn.prepare(
        "SELECT id, invariant_id, raised_at, details_json, resolved_at
         FROM capture_alarms
         WHERE acked_at IS NULL
         ORDER BY raised_at ASC",
    )?;

    let rows: Vec<AlarmRow> = stmt
        .query_map([], |row| {
            Ok(AlarmRow {
                id: row.get(0)?,
                invariant_id: row.get(1)?,
                raised_at: row.get(2)?,
                details_json: row.get(3)?,
                resolved_at: row.get(4)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    // `Iterator::partition` preserves source order, so active rows inherit
    // raised_at ASC from the SELECT above — oldest still-violating alarm at
    // the top. Resolved rows then re-sort to resolved_at DESC so the most-
    // recent recoveries appear first (what an operator wants when scanning
    // a long auto-resolved list).
    let (active, mut resolved): (Vec<&AlarmRow>, Vec<&AlarmRow>) =
        rows.iter().partition(|r| r.resolved_at.is_none());
    resolved.sort_by_key(|r| std::cmp::Reverse(r.resolved_at));

    if active.is_empty() && resolved.is_empty() {
        println!("No alarms.");
        return Ok(false);
    }

    if !active.is_empty() {
        println!("ACTIVE");
        print_alarms_table(&active, false);
    }

    if !resolved.is_empty() {
        if !active.is_empty() {
            println!();
        }
        println!(
            "AUTO-RESOLVED ({} row{}, run `hippo alarms prune` to clear)",
            resolved.len(),
            if resolved.len() == 1 { "" } else { "s" }
        );
        print_alarms_table(&resolved, true);
    }

    Ok(!active.is_empty())
}

// Column widths for `hippo alarms list`, single source of truth so the
// header and body never drift apart.
const COL_ID: usize = 6;
const COL_INVARIANT: usize = 12;
const COL_TS: usize = 24;

fn print_alarms_table(rows: &[&AlarmRow], show_resolved: bool) {
    let resolved_header = if show_resolved {
        format!("{:<ts$}  ", "RESOLVED", ts = COL_TS)
    } else {
        String::new()
    };
    // Render the header into a String and size the underline from it so the
    // two always match by construction. Row DETAILS may run longer than
    // "DETAILS" — that's fine; the underline sits below the header, not the
    // longest row.
    let header = format!(
        "{:<id$}  {:<inv$}  {:<ts$}  {}DETAILS",
        "ID",
        "INVARIANT",
        "RAISED",
        resolved_header,
        id = COL_ID,
        inv = COL_INVARIANT,
        ts = COL_TS,
    );
    println!("{}", header);
    println!("{}", "-".repeat(header.chars().count()));

    for row in rows {
        let raised = format_ts(row.raised_at);
        let details_summary = alarm_details_summary(&row.details_json);
        let resolved_col = if show_resolved {
            let r = row
                .resolved_at
                .map(format_ts)
                .unwrap_or_else(|| "-".to_string());
            format!("{:<ts$}  ", r, ts = COL_TS)
        } else {
            String::new()
        };
        println!(
            "{:<id$}  {:<inv$}  {:<ts$}  {}{}",
            row.id,
            row.invariant_id,
            raised,
            resolved_col,
            details_summary,
            id = COL_ID,
            inv = COL_INVARIANT,
            ts = COL_TS,
        );
    }
}

fn format_ts(ts_ms: i64) -> String {
    // Treat 0 as an uninitialized sentinel rather than rendering 1970-01-01.
    if ts_ms == 0 {
        return "-".to_string();
    }
    chrono::DateTime::from_timestamp_millis(ts_ms)
        .map(|dt| dt.format("%Y-%m-%d %H:%M UTC").to_string())
        .unwrap_or_else(|| ts_ms.to_string())
}

/// Acknowledge a `capture_alarms` row by ID.
///
/// Sets `acked_at = now_ms` and optionally `ack_note`.  The `WHERE acked_at IS
/// NULL` guard makes this idempotent: a second ack on an already-acked row
/// updates 0 rows and returns `Ok(())`.
pub fn handle_alarms_ack(config: &HippoConfig, id: i64, note: Option<&str>) -> Result<()> {
    let conn = hippo_core::storage::open_db(&config.db_path())?;
    let now_ms = chrono::Utc::now().timestamp_millis();

    let updated = conn.execute(
        "UPDATE capture_alarms
         SET acked_at = ?1, ack_note = ?2
         WHERE id = ?3 AND acked_at IS NULL",
        rusqlite::params![now_ms, note, id],
    )?;

    if updated > 0 {
        println!("Alarm {} acknowledged.", id);
    } else {
        // Either already acked (idempotent) or not found — both are OK.
        println!("Alarm {} not found or already acknowledged.", id);
    }
    Ok(())
}

/// Bulk-acknowledge every auto-resolved alarm (resolved_at IS NOT NULL,
/// acked_at IS NULL). Sets `acked_at = now_ms` and `ack_note = "auto-resolved"`.
/// Idempotent: a second run on a clean table updates 0 rows and returns Ok.
pub fn handle_alarms_prune(config: &HippoConfig) -> Result<()> {
    let conn = hippo_core::storage::open_db(&config.db_path())?;
    let now_ms = chrono::Utc::now().timestamp_millis();

    let updated = conn.execute(
        "UPDATE capture_alarms
         SET acked_at = ?1, ack_note = 'auto-resolved'
         WHERE acked_at IS NULL AND resolved_at IS NOT NULL",
        rusqlite::params![now_ms],
    )?;

    if updated > 0 {
        println!(
            "Pruned {} auto-resolved alarm{}.",
            updated,
            if updated == 1 { "" } else { "s" }
        );
    } else {
        println!("No auto-resolved alarms to prune.");
    }
    Ok(())
}

/// Build a short human-readable summary from a `details_json` blob.
/// Formats `"{source} silent {duration}"` when both `source` and `since_ms`
/// are present; otherwise falls back to a (possibly truncated) raw JSON string.
fn alarm_details_summary(details_json: &str) -> String {
    let truncated = || details_json.chars().take(60).collect::<String>();

    let Ok(v) = serde_json::from_str::<serde_json::Value>(details_json) else {
        return truncated();
    };

    let Some(source) = v.get("source").and_then(|s| s.as_str()) else {
        return truncated();
    };

    let Some(since_secs) = v
        .get("since_ms")
        .and_then(|s| s.as_i64())
        .map(|ms| ms / 1_000)
    else {
        return truncated();
    };

    let hours = since_secs / 3600;
    let mins = (since_secs % 3600) / 60;

    if hours > 0 {
        format!("{} silent {}h {}m", source, hours, mins)
    } else {
        format!("{} silent {}m", source, mins)
    }
}

// ---------------------------------------------------------------------------
// Alarms unit tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod alarms {
    use super::*;
    use tempfile::TempDir;

    fn config_for_dir(dir: &TempDir) -> HippoConfig {
        let mut config = HippoConfig::default();
        config.storage.data_dir = dir.path().to_path_buf();
        config
    }

    // Ensure handle_alarms_list returns false (exit 0) when no rows.
    #[test]
    fn alarms_list_empty_returns_false() {
        let dir = TempDir::new().unwrap();
        // open_db applies full schema migrations, creating capture_alarms
        let _conn = hippo_core::storage::open_db(&dir.path().join("hippo.db")).unwrap();
        let config = config_for_dir(&dir);
        let has_alarms = handle_alarms_list(&config).unwrap();
        assert!(!has_alarms, "empty table must return false");
    }

    // Ensure handle_alarms_list returns true (exit 1) when un-acked rows exist.
    #[test]
    fn alarms_list_active_rows_returns_true() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("hippo.db");
        let conn = hippo_core::storage::open_db(&db_path).unwrap();

        let now_ms = chrono::Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES ('I-1', ?1, '{\"source\":\"shell\",\"since_ms\":90000}')",
            rusqlite::params![now_ms],
        )
        .unwrap();

        let config = config_for_dir(&dir);
        let has_alarms = handle_alarms_list(&config).unwrap();
        assert!(has_alarms, "active row must return true");
    }

    // Acked rows must not appear in list.
    #[test]
    fn alarms_list_excludes_acked_rows() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("hippo.db");
        let conn = hippo_core::storage::open_db(&db_path).unwrap();

        let now_ms = chrono::Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json, acked_at)
             VALUES ('I-1', ?1, '{}', ?1)",
            rusqlite::params![now_ms],
        )
        .unwrap();

        let config = config_for_dir(&dir);
        let has_alarms = handle_alarms_list(&config).unwrap();
        assert!(!has_alarms, "acked row must not appear in list");
    }

    // handle_alarms_ack sets acked_at and returns Ok.
    #[test]
    fn alarms_ack_sets_acked_at() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("hippo.db");
        let conn = hippo_core::storage::open_db(&db_path).unwrap();

        let now_ms = chrono::Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES ('I-3', ?1, '{}')",
            rusqlite::params![now_ms],
        )
        .unwrap();
        let id: i64 = conn.last_insert_rowid();

        let config = config_for_dir(&dir);
        handle_alarms_ack(&config, id, Some("test note")).unwrap();

        let (acked_at, ack_note): (Option<i64>, Option<String>) = conn
            .query_row(
                "SELECT acked_at, ack_note FROM capture_alarms WHERE id = ?1",
                rusqlite::params![id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();

        assert!(acked_at.is_some(), "acked_at must be set");
        assert_eq!(ack_note.as_deref(), Some("test note"));
    }

    // Re-ack is idempotent (must not error).
    #[test]
    fn alarms_ack_is_idempotent() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("hippo.db");
        let conn = hippo_core::storage::open_db(&db_path).unwrap();

        let now_ms = chrono::Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES ('I-3', ?1, '{}')",
            rusqlite::params![now_ms],
        )
        .unwrap();
        let id: i64 = conn.last_insert_rowid();

        let config = config_for_dir(&dir);
        handle_alarms_ack(&config, id, None).unwrap();
        // Second ack — must not return Err
        let result = handle_alarms_ack(&config, id, Some("again"));
        assert!(result.is_ok(), "re-ack must be idempotent");
    }

    // Resolved-but-unacked rows must NOT contribute to the exit code.
    // (An auto-resolved alarm is informational — exit 1 is reserved for
    // currently-violating invariants.)
    #[test]
    fn alarms_list_resolved_only_returns_false() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("hippo.db");
        let conn = hippo_core::storage::open_db(&db_path).unwrap();

        let now_ms = chrono::Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json, resolved_at)
             VALUES ('I-1', ?1, '{\"source\":\"shell\"}', ?1)",
            rusqlite::params![now_ms],
        )
        .unwrap();

        let config = config_for_dir(&dir);
        let has_alarms = handle_alarms_list(&config).unwrap();
        assert!(!has_alarms, "resolved-only rows must not trigger exit 1");
    }

    // Mixed: an active row alongside a resolved row → exit 1 (active drives it).
    #[test]
    fn alarms_list_active_and_resolved_returns_true() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("hippo.db");
        let conn = hippo_core::storage::open_db(&db_path).unwrap();

        let now_ms = chrono::Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES ('I-1', ?1, '{\"source\":\"shell\"}')",
            rusqlite::params![now_ms],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json, resolved_at)
             VALUES ('I-4', ?1, '{\"source\":\"browser\"}', ?1)",
            rusqlite::params![now_ms],
        )
        .unwrap();

        let config = config_for_dir(&dir);
        let has_alarms = handle_alarms_list(&config).unwrap();
        assert!(
            has_alarms,
            "any active alarm must trigger exit 1 even when resolved rows present"
        );
    }

    // Prune acks every resolved-but-unacked row, leaves active rows untouched.
    #[test]
    fn alarms_prune_acks_resolved_only() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("hippo.db");
        let conn = hippo_core::storage::open_db(&db_path).unwrap();

        let now_ms = chrono::Utc::now().timestamp_millis();
        // Two resolved-but-unacked + one active.
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json, resolved_at)
             VALUES ('I-1', ?1, '{\"source\":\"shell\"}', ?1)",
            rusqlite::params![now_ms],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json, resolved_at)
             VALUES ('I-4', ?1, '{\"source\":\"browser\"}', ?1)",
            rusqlite::params![now_ms],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES ('I-8', ?1, '{\"source\":\"shell\"}')",
            rusqlite::params![now_ms],
        )
        .unwrap();

        let config = config_for_dir(&dir);
        handle_alarms_prune(&config).unwrap();

        let acked: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM capture_alarms WHERE acked_at IS NOT NULL",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(acked, 2, "both resolved rows must be acked");

        let active_unacked: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM capture_alarms
                 WHERE acked_at IS NULL AND resolved_at IS NULL",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(active_unacked, 1, "active row must remain un-acked");

        // Ack note is set to 'auto-resolved' so the historical reason is preserved.
        let note: String = conn
            .query_row(
                "SELECT ack_note FROM capture_alarms WHERE id = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(note, "auto-resolved");
    }

    // Prune on an empty / all-active table is a no-op.
    #[test]
    fn alarms_prune_no_op_when_nothing_resolved() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("hippo.db");
        let conn = hippo_core::storage::open_db(&db_path).unwrap();
        let now_ms = chrono::Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
             VALUES ('I-1', ?1, '{}')",
            rusqlite::params![now_ms],
        )
        .unwrap();

        let config = config_for_dir(&dir);
        let result = handle_alarms_prune(&config);
        assert!(result.is_ok());

        let acked: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM capture_alarms WHERE acked_at IS NOT NULL",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(acked, 0);
    }

    // Ack of non-existent ID must not error.
    #[test]
    fn alarms_ack_nonexistent_id_is_ok() {
        let dir = TempDir::new().unwrap();
        let db_path = dir.path().join("hippo.db");
        let _conn = hippo_core::storage::open_db(&db_path).unwrap();

        let config = config_for_dir(&dir);
        let result = handle_alarms_ack(&config, 9999, None);
        assert!(result.is_ok(), "ack of non-existent id must not error");
    }

    // alarm_details_summary produces a readable human string.
    #[test]
    fn alarms_details_summary_formats_source_and_duration() {
        let json = r#"{"source":"shell","since_ms":7200000}"#; // 2h
        let summary = alarm_details_summary(json);
        assert!(
            summary.contains("shell"),
            "summary must contain source name"
        );
        assert!(summary.contains('h'), "summary must contain hours");
    }

    // Malformed JSON falls back to (truncated) raw string.
    #[test]
    fn alarms_details_summary_handles_malformed_json() {
        let summary = alarm_details_summary("not json {{{");
        assert!(!summary.is_empty());
        assert!(
            summary.contains("not json"),
            "should return raw input fragment"
        );
    }

    // Valid JSON missing required fields falls back to raw JSON string.
    #[test]
    fn alarms_details_summary_falls_back_when_fields_missing() {
        let json = r#"{"foo":"bar"}"#;
        let summary = alarm_details_summary(json);
        assert!(
            summary.contains("foo"),
            "should return raw JSON when fields absent"
        );
    }
}

pub async fn handle_doctor(config: &HippoConfig, explain: bool) -> Result<()> {
    let mut fail_count: u32 = 0;
    let cli_version = env!("HIPPO_VERSION_FULL");
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
        .unwrap_or_default();
    println!("Hippo Doctor");
    println!("============");
    println!("[OK] CLI version: {}", cli_version);

    // Check daemon socket and version
    let socket = config.socket_path();
    // Track whether the daemon socket responded — used by Check 9 (fallback age).
    let mut daemon_socket_ok = false;
    if socket.exists() {
        match send_request(&socket, &DaemonRequest::GetStatus).await {
            Ok(DaemonResponse::Status(status)) => {
                daemon_socket_ok = true;
                println!("[OK] Daemon is running (uptime {}s)", status.uptime_secs);
                if status.version.is_empty() {
                    println!("[!!] Daemon too old to report version — restart recommended");
                    fail_count += 1;
                } else if status.version == cli_version {
                    println!("[OK] Daemon version matches CLI");
                } else {
                    println!(
                        "[!!] Daemon version mismatch: running={}, cli={}",
                        status.version, cli_version
                    );
                    println!("     Run: mise run restart");
                    fail_count += 1;
                }
            }
            _ => {
                println!("[!!] Socket exists but daemon not responding");
                fail_count += 1;
            }
        }
    } else {
        println!("[!!] Daemon socket not found at {:?}", socket);
        fail_count += 1;
    }

    // Check database
    let db_path = config.db_path();
    if db_path.exists() {
        let size = std::fs::metadata(&db_path).map(|m| m.len()).unwrap_or(0);
        println!("[OK] Database exists ({} bytes)", size);
    } else {
        println!("[--] Database not found (will be created on first run)");
    }

    // Check config
    let config_path = config.storage.config_dir.join("config.toml");
    if config_path.exists() {
        println!("[OK] Config file found");
        fail_count += check_legacy_capture_section(&config_path, explain);
    } else {
        println!("[--] No config file (using defaults)");
    }

    // Check the configured inference backend (LM Studio, oMLX, ollama, vLLM, …).
    let inference_url = format!("{}/models", config.inference.base_url);
    match client.get(&inference_url).send().await {
        Ok(r) if r.status().is_success() => println!("[OK] Inference backend reachable"),
        _ => {
            println!(
                "[!!] Inference backend not reachable at {}",
                config.inference.base_url
            );
            fail_count += 1;
        }
    }

    // Check brain — store JSON for reuse in Check 10 (schema version).
    let brain_json = print_brain_health_details(config, &client).await;

    // Check 9: Fallback file age (extends the old plain-count check).
    fail_count += check_fallback_age(&config.fallback_dir(), daemon_socket_ok, explain);

    // Check embedding model
    if config.models.embedding.is_empty() {
        println!("[!!] No embedding model configured");
        fail_count += 1;
    } else {
        println!("[OK] Embedding model: {}", config.models.embedding);
    }

    // Check Claude session hook
    check_claude_session_hook(config);

    // Check Firefox extension build + Native Messaging manifest
    check_firefox_extension();

    // Per-source capture-freshness audit (one line per raw data source
    // hippo is supposed to collect). Uses day-level thresholds — complements
    // the seconds-level `check_source_staleness` below.
    fail_count += check_source_freshness(config);

    // Check OpenTelemetry configuration (incl. brain self-reported status)
    fail_count += check_otel_status(config, &client, brain_json.as_ref()).await;

    // Check GitHub CI-ingest configuration
    fail_count += check_github_source(config);

    // Check 1: Per-source staleness via source_health table (P0.1)
    // Check 8: Watchdog heartbeat
    // Check 5: Live-session vs DB reconciliation
    // Check 6: Session-hook log vs DB
    // Check 10: Schema version
    if db_path.exists()
        && let Ok(conn) = hippo_core::storage::open_db(&db_path)
    {
        fail_count += check_source_staleness(&conn, explain);
        fail_count += check_codex_state_coverage(config, &conn, explain);
        // On-disk-vs-Hippo completeness for the file-based agentic sources.
        if let Some(home) = dirs::home_dir() {
            fail_count +=
                check_claude_session_coverage(&conn, &home.join(".claude/projects"), explain);
        }
        fail_count += check_cursor_session_coverage(config, &conn, explain);
        fail_count += check_watchdog_heartbeat(&conn, explain);
        // Auto-resolved alarm count is informational — never increments fail_count.
        check_resolved_alarm_count(&conn);

        // Check 5: active JSONL sessions vs claude_sessions table
        if let Some(home) = dirs::home_dir() {
            fail_count += check_claude_session_db(
                &home.join(".claude/projects"),
                &config.storage.data_dir,
                &conn,
                explain,
            );
        } else {
            println!("[--] {:<29}  no home dir", "claude-session DB");
        }

        // Check 6: session-hook debug log vs claude_sessions rows (last 1h)
        let hook_log = config.storage.data_dir.join("session-hook-debug.log");
        fail_count += check_session_hook_log(&hook_log, &config.storage.data_dir, &conn, explain);

        // Check 10: daemon PRAGMA user_version vs brain expected_schema_version
        fail_count += check_schema_version(&conn, brain_json.as_ref(), explain);
    }

    // Check 4: zsh hook sourced
    fail_count += check_zsh_hook_sourced(explain);

    // Check 7: Log file sizes
    fail_count += check_log_file_sizes(config, explain);

    // Check 2: Native Messaging manifest (detailed — existence + JSON + path executable + extension ID)
    if let Some(home) = dirs::home_dir() {
        let nm_manifest =
            home.join("Library/Application Support/Mozilla/NativeMessagingHosts/hippo_daemon.json");
        fail_count += check_nm_manifest(&nm_manifest, explain);
    } else {
        println!("[--] {:<29}  no home dir", "native-msg manifest");
    }

    if fail_count > 0 {
        std::process::exit(fail_count as i32);
    }

    Ok(())
}

/// Per-source capture-freshness doctor check.
///
/// Emits one line per source, color-coded by how long since the freshest
/// row (staleness thresholds defined in `source_freshness_probes()`
/// below; see also the I-1..I-12 freshness invariants in
/// `docs/capture/architecture.md`). Queries the underlying tables
/// directly so it works without the `source_health` table (which is
/// still a P0.1 roadmap item).
fn check_source_freshness(config: &HippoConfig) -> u32 {
    let db_path = config.db_path();
    if !db_path.exists() {
        println!("[--] Source freshness: database not created yet");
        return 0;
    }

    let conn = match hippo_core::storage::open_db(&db_path) {
        Ok(c) => c,
        Err(e) => {
            println!("[!!] Source freshness: failed to open DB: {e}");
            return 1;
        }
    };

    let now_ms = chrono::Utc::now().timestamp_millis();
    let mut fail_count = 0u32;
    for probe in source_freshness_probes() {
        let (count, max_ts): (i64, Option<i64>) = conn
            .query_row(probe.query, [], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap_or((0, None));

        let line = source_freshness_verdict(probe.name, count, max_ts, now_ms, probe.thresholds);
        if line.starts_with("[!!]") {
            fail_count += 1;
        }
        println!("{}", line);
    }
    fail_count
}

/// Soft/hard staleness thresholds in milliseconds.
///
/// - `soft` → `[WW]` warning (source is dozing; probably fine overnight).
/// - `hard` → `[!!]` red alert (capture chain almost certainly broken).
/// - Zero rows EVER → always `[--]` (distinct from "rows but stale").
#[derive(Clone, Copy)]
pub struct FreshnessThresholds {
    pub soft_ms: i64,
    pub hard_ms: i64,
}

pub struct SourceFreshnessProbe {
    pub name: &'static str,
    /// Must return two columns: `count(*)`, `max(<ts>)`.
    pub query: &'static str,
    pub thresholds: FreshnessThresholds,
}

const HOUR_MS: i64 = 60 * 60 * 1000;
const DAY_MS: i64 = 24 * HOUR_MS;

/// Every raw-data source hippo is supposed to collect, with the query
/// that answers "when did we last see a row?" and a soft/hard threshold
/// tuned to how long that source can legitimately sit idle.
///
/// Session-table probes (claude-session, agentic-session-*) use `MAX(end_time)`
/// rather than `MAX(start_time)` on purpose: the upsert pattern across
/// `claude_session.rs`, `codex_session.rs`, `cursor_session.rs`, and
/// `opencode_session.rs` advances `end_time` on every conflict-update but
/// deliberately preserves `start_time`, so `end_time` is the column that tracks
/// "most recent activity" — the question freshness is actually asking. Using
/// `start_time` would let a long-running session's freshness lag by the segment
/// duration (`TASK_GAP_MS` = 5 min for Codex/Claude, up to the char cap), which
/// is negligible at the 3-day/30-day thresholds but wrong in principle.
pub fn source_freshness_probes() -> Vec<SourceFreshnessProbe> {
    vec![
        SourceFreshnessProbe {
            name: "shell",
            query: "SELECT COUNT(*), MAX(timestamp) FROM events WHERE source_kind = 'shell'",
            thresholds: FreshnessThresholds {
                soft_ms: 24 * HOUR_MS,
                hard_ms: 7 * DAY_MS,
            },
        },
        SourceFreshnessProbe {
            name: "claude-tool",
            query: "SELECT COUNT(*), MAX(timestamp) FROM events WHERE source_kind = 'claude-tool'",
            thresholds: FreshnessThresholds {
                soft_ms: 24 * HOUR_MS,
                hard_ms: 7 * DAY_MS,
            },
        },
        // Claude Code sessions live in `agentic_sessions` keyed by
        // `harness = 'claude-code'` (post v17→v18 agentic unification). The
        // harness column separates Codex/Cursor/opencode cleanly, so no
        // source_file path exclusions are needed. Probe rows excluded per AP-6.
        SourceFreshnessProbe {
            name: "claude-session (main)",
            query: "SELECT COUNT(*), MAX(end_time) FROM agentic_sessions \
                    WHERE harness = 'claude-code' AND is_subagent = 0 \
                    AND probe_tag IS NULL",
            thresholds: FreshnessThresholds {
                soft_ms: 12 * HOUR_MS,
                hard_ms: 7 * DAY_MS,
            },
        },
        SourceFreshnessProbe {
            name: "claude-session (subagent)",
            query: "SELECT COUNT(*), MAX(end_time) FROM agentic_sessions \
                    WHERE harness = 'claude-code' AND is_subagent = 1 \
                    AND probe_tag IS NULL",
            thresholds: FreshnessThresholds {
                soft_ms: 7 * DAY_MS,
                hard_ms: 30 * DAY_MS,
            },
        },
        SourceFreshnessProbe {
            name: "browser",
            query: "SELECT COUNT(*), MAX(timestamp) FROM browser_events",
            thresholds: FreshnessThresholds {
                soft_ms: 48 * HOUR_MS,
                hard_ms: 14 * DAY_MS,
            },
        },
        SourceFreshnessProbe {
            name: "workflow",
            query: "SELECT COUNT(*), MAX(started_at) FROM workflow_runs",
            thresholds: FreshnessThresholds {
                soft_ms: 3 * DAY_MS,
                hard_ms: 30 * DAY_MS,
            },
        },
        // Opencode harness rows in `agentic_sessions`. Probe rows excluded
        // per AP-6. Long-horizon thresholds: opencode is bursty by nature
        // (sessions only land when the user actively codes with it).
        SourceFreshnessProbe {
            name: "agentic-session-opencode",
            query: "SELECT COUNT(*), MAX(end_time) FROM agentic_sessions \
                    WHERE harness = 'opencode' AND probe_tag IS NULL",
            thresholds: FreshnessThresholds {
                soft_ms: 3 * DAY_MS,
                hard_ms: 30 * DAY_MS,
            },
        },
        // Codex rows in `agentic_sessions` keyed by `harness = 'codex'`. Probe
        // rows excluded per AP-6. Thresholds mirror opencode: Codex is bursty —
        // sessions only land when the user actively codes with it.
        SourceFreshnessProbe {
            name: "agentic-session-codex",
            query: "SELECT COUNT(*), MAX(end_time) FROM agentic_sessions \
                    WHERE harness = 'codex' AND probe_tag IS NULL",
            thresholds: FreshnessThresholds {
                soft_ms: 3 * DAY_MS,
                hard_ms: 30 * DAY_MS,
            },
        },
        // Cursor rows in `agentic_sessions` keyed by `harness = 'cursor'`. Probe
        // rows excluded per AP-6. Thresholds mirror opencode.
        SourceFreshnessProbe {
            name: "agentic-session-cursor",
            query: "SELECT COUNT(*), MAX(end_time) FROM agentic_sessions \
                    WHERE harness = 'cursor' AND probe_tag IS NULL",
            thresholds: FreshnessThresholds {
                soft_ms: 3 * DAY_MS,
                hard_ms: 30 * DAY_MS,
            },
        },
    ]
}

/// Format a single source-freshness line.
///
/// Pulled out of `check_source_freshness` so the doctor tests can
/// exercise the verdict logic without spinning up a daemon.
pub fn source_freshness_verdict(
    name: &str,
    count: i64,
    max_ts: Option<i64>,
    now_ms: i64,
    thresholds: FreshnessThresholds,
) -> String {
    if count == 0 {
        return format!("[--] Source freshness {name}: zero rows ever");
    }

    let Some(ts) = max_ts else {
        // Row count > 0 but no timestamp column — shouldn't happen with
        // the probes above, but play it safe.
        return format!("[!!] Source freshness {name}: {count} rows, no max timestamp");
    };

    let age_ms = (now_ms - ts).max(0);
    let human = format_duration_ms(age_ms);
    if age_ms > thresholds.hard_ms {
        format!(
            "[!!] Source freshness {name}: freshest {human} ago (> {})",
            format_duration_ms(thresholds.hard_ms)
        )
    } else if age_ms > thresholds.soft_ms {
        format!(
            "[WW] Source freshness {name}: freshest {human} ago (> {})",
            format_duration_ms(thresholds.soft_ms)
        )
    } else {
        format!("[OK] Source freshness {name}: {count} rows, freshest {human} ago")
    }
}

fn format_duration_ms(ms: i64) -> String {
    if ms < 0 {
        return "future".to_string();
    }
    let secs = ms / 1000;
    if secs < 60 {
        return format!("{secs}s");
    }
    let mins = secs / 60;
    if mins < 60 {
        return format!("{mins}m");
    }
    let hours = mins / 60;
    if hours < 48 {
        return format!("{hours}h");
    }
    let days = hours / 24;
    format!("{days}d")
}

async fn check_otel_status(
    config: &HippoConfig,
    client: &reqwest::Client,
    brain_json: Option<&serde_json::Value>,
) -> u32 {
    // Check if OTel feature is compiled in
    #[cfg(feature = "otel")]
    let otel_compiled = true;
    #[cfg(not(feature = "otel"))]
    let otel_compiled = false;

    if !otel_compiled {
        println!("[--] OpenTelemetry: not compiled (daemon built without --features otel)");
        return 0;
    }

    // Check if telemetry is enabled in config
    let config_enabled = config.telemetry.enabled;

    // Check if OTel collector is reachable via its health-check extension
    let collector_health_url = "http://localhost:13133/";
    let collector_reachable = client
        .get(collector_health_url)
        .send()
        .await
        .map(|r| r.status().is_success())
        .unwrap_or(false);

    let mut fail_count = match (config_enabled, collector_reachable) {
        (true, true) => {
            println!("[OK] OpenTelemetry: enabled and collector reachable");
            0
        }
        (true, false) => {
            println!(
                "[!!] OpenTelemetry: enabled but collector unreachable at {}",
                collector_health_url
            );
            println!("     Start the stack: mise run otel:up");
            1
        }
        (false, true) => {
            println!("[!!] OpenTelemetry: collector available but disabled in config");
            println!(
                "     Enable it: Set [telemetry] enabled = true in ~/.config/hippo/config.toml"
            );
            println!("     Then restart: mise run restart");
            1
        }
        (false, false) => {
            println!("[--] OpenTelemetry: disabled (start with: mise run otel:up)");
            0
        }
    };

    fail_count += check_brain_telemetry_status(brain_json);

    fail_count
}

/// Surface the brain's self-reported telemetry state. Catches the failure
/// mode where the brain process is alive and reports `enrichment_running:
/// true` but ships zero metrics because its venv was never re-synced after a
/// pyproject change. This is precisely the silent regression that left the
/// hippo-enrichment dashboard dark on 2026-04-26.
///
/// Three outcomes (return 1 only on the configured-on-but-dead case):
/// - `telemetry_enabled = true,  telemetry_active = true`  → OK, no print
/// - `telemetry_enabled = true,  telemetry_active = false` → fail loud
/// - `telemetry_enabled = false`                           → no-op
/// - either field missing (older brain)                    → unknown, no-op
fn check_brain_telemetry_status(brain_json: Option<&serde_json::Value>) -> u32 {
    let Some(json) = brain_json else { return 0 };
    let enabled = json.get("telemetry_enabled").and_then(|v| v.as_bool());
    let active = json.get("telemetry_active").and_then(|v| v.as_bool());

    match (enabled, active) {
        (Some(true), Some(true)) => {
            println!("[OK] Brain telemetry: initialized and active");
            0
        }
        (Some(true), Some(false)) => {
            println!("[!!] Brain telemetry: HIPPO_OTEL_ENABLED=1 but providers not initialized");
            println!("     CAUSE:  Deployed brain venv is out of sync with pyproject.toml,");
            println!("             or the OTel package namespace was half-installed.");
            println!("     FIX:    uv sync --project ~/.local/share/hippo-brain --reinstall");
            println!("             then: launchctl kickstart -k gui/$(id -u)/com.hippo.brain");
            println!(
                "     DOC:    docs/archive/capture-reliability-overhaul/03-doctor-upgrades.md"
            );
            1
        }
        (Some(false), _) => 0,
        // Older brain without the new health fields — treat as unknown.
        // Don't fail; the existing collector-reachability check above is a
        // close-enough proxy for installs that haven't been upgraded yet.
        _ => 0,
    }
}

/// Whether a launchd label is currently loaded. Local duplicate of
/// `install::service_is_loaded` because `install` is a binary-only module
/// (not reachable from this lib-side doctor check).
fn launchctl_service_loaded(label: &str) -> bool {
    std::process::Command::new("launchctl")
        .args(["list", label])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Report on GitHub CI-ingest (`gh-poll`) configuration.
///
/// Three states:
///   - enabled + plist loaded + token set  → OK
///   - enabled but plist missing / token missing → warn (fixable config error)
///   - disabled                             → info only, no fail increment
///     (opt-in feature; most users don't want it — but make the opt-in
///     discoverable since silent-missing-data is the #1 failure mode)
fn check_github_source(config: &HippoConfig) -> u32 {
    check_github_source_with(config, || {
        crate::gh_poll::resolve_github_token(&config.github.token_env).is_some()
    })
}

/// Testable core of `check_github_source`. `token_present` is a closure so
/// tests can inject token-presence without mutating the process environment
/// (which is `unsafe` on Rust 1.82+ and races with any concurrent env reader).
fn check_github_source_with<F>(config: &HippoConfig, token_present: F) -> u32
where
    F: FnOnce() -> bool,
{
    if !config.github.enabled {
        println!(
            "[--] GitHub CI ingest: disabled (set [github] enabled = true in {} to enable)",
            config.storage.config_dir.join("config.toml").display()
        );
        return 0;
    }

    let mut fail = 0u32;

    // Token must be resolvable at doctor time — same gate as install.
    // Resolver tries env first, then `~/.config/zsh/.env`, then `gh auth token`.
    if !token_present() {
        println!(
            "[!!] GitHub CI ingest: enabled but no token available (env {} unset, ~/.config/zsh/.env lacks it, and `gh auth token` failed)",
            config.github.token_env
        );
        println!(
            "     Either: export {} with a token",
            config.github.token_env
        );
        println!(
            "             (classic PAT: `repo` + `workflow` scopes; fine-grained PAT: Actions + Metadata + Contents read)"
        );
        println!(
            "     Or:     run `gh auth login` so the wrapper can fall back to `gh auth token`"
        );
        fail += 1;
    }

    if config.github.watched_repos.is_empty() {
        println!("[!!] GitHub CI ingest: enabled but [github] watched_repos is empty");
        println!("     Add at least one repo, e.g.  watched_repos = [\"owner/name\"]");
        fail += 1;
    }

    let Some(home_dir) = dirs::home_dir() else {
        println!(
            "[!!] GitHub CI ingest: cannot locate home directory; skipping plist verification"
        );
        return fail + 1;
    };
    let plist_path = home_dir.join("Library/LaunchAgents/com.hippo.gh-poll.plist");
    if !plist_path.exists() {
        println!(
            "[!!] GitHub CI ingest: enabled but gh-poll plist not installed at {}",
            plist_path.display()
        );
        println!("     Run: hippo daemon install --force");
        fail += 1;
    } else if !launchctl_service_loaded("com.hippo.gh-poll") {
        println!("[!!] GitHub CI ingest: enabled and plist installed but agent not loaded");
        println!(
            "     Run: launchctl bootstrap gui/$(id -u) {}",
            plist_path.display()
        );
        fail += 1;
    }

    if fail == 0 {
        println!(
            "[OK] GitHub CI ingest: enabled ({} repo(s) watched)",
            config.github.watched_repos.len()
        );
    }
    fail
}

/// Check 1: Per-source staleness via the `source_health` table (requires P0.1 migration).
///
/// Returns the number of failing (hard-threshold) checks.
fn check_source_staleness(db: &rusqlite::Connection, explain: bool) -> u32 {
    // Query source_health — if the table doesn't exist yet, print a soft notice and bail.
    let rows_result = db.prepare(
        "SELECT source, last_event_ts, last_error_msg, consecutive_failures, events_last_1h, probe_ok \
         FROM source_health \
         WHERE source IN ('shell', 'browser', 'agentic-session-claude', 'claude-tool', 'agentic-session-opencode', 'agentic-session-codex', 'agentic-session-cursor') \
         ORDER BY source",
    );

    let mut stmt = match rows_result {
        Ok(s) => s,
        Err(e) => {
            let msg = e.to_string();
            if msg.contains("no such table") {
                println!(
                    "[--] source health             table not yet created (run hippo daemon install --force)"
                );
                return 0;
            }
            println!("[!!] source health             DB error: {}", e);
            return 1;
        }
    };

    struct SourceRow {
        source: String,
        last_event_ts: Option<i64>,
        probe_ok: Option<i64>,
    }

    let mapped = match stmt.query_map([], |row| {
        Ok(SourceRow {
            source: row.get(0)?,
            last_event_ts: row.get(1)?,
            // columns 2, 3, 4 are last_error_msg, consecutive_failures, events_last_1h — not used
            probe_ok: row.get(5)?,
        })
    }) {
        Ok(m) => m,
        Err(e) => {
            println!("[!!] source health             query error: {}", e);
            return 1;
        }
    };
    let rows: Vec<SourceRow> = match mapped.collect::<rusqlite::Result<Vec<_>>>() {
        Ok(r) => r,
        Err(e) => {
            println!("[!!] source health             row error: {}", e);
            return 1;
        }
    };

    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64;

    // Check Firefox running (for browser suppression).
    // macOS Firefox (incl. Developer Edition) exposes the main process as `firefox`;
    // `firefox-bin` is Linux-only. Match either to keep the check portable.
    // `-q` suppresses pgrep's PID output so it doesn't leak into doctor output.
    let firefox_running = || -> bool {
        ["firefox", "firefox-bin"].iter().any(|name| {
            std::process::Command::new("pgrep")
                .args(["-qx", name])
                .status()
                .map(|s| s.success())
                .unwrap_or(false)
        })
    };

    // Load the runtime config once for both poller-backed idle probes below
    // (codex + opencode). A single parse avoids two TOML reads per doctor run
    // and the tiny TOCTOU window if the file changed between them. On a load
    // error each closure fails *open for alerting*: it returns the values that
    // reach `None` in `source_staleness_suppression_reason` (codex/cursor →
    // `(true, true, false)`, opencode → `true`), so a broken config file never
    // silently hides a real ingestion failure behind a misleading "no sessions
    // found" / "idle" reason. This mirrors the `*_enabled` fallback below.
    let doctor_config = hippo_core::config::HippoConfig::load_default();

    // Inspect the Codex rollout files under the configured session roots.
    // Two facts drive doctor suppression, mirroring opencode's DB checks:
    //   - `any_exist`: at least one `rollout-*.jsonl` file is present.
    //     False ⇒ "user has never run Codex" — suppress rather than alarm.
    //   - `any_recent`: at least one `rollout-*.jsonl` was modified within the
    //     IDLE_WINDOW_SECS window (the same window opencode uses for its DB
    //     mtime). False (with files present) ⇒ "user just isn't using Codex
    //     right now" — suppress.  True with a stale `source_health` row ⇒ a
    //     genuine ingestion failure, so it is NOT suppressed.
    let codex_session_state = || -> (bool, bool, bool) {
        let Ok(cfg) = doctor_config.as_ref() else {
            // Config unreadable: fail open for alerting. `(exist=true,
            // recent=true, in_flight=false)` skips every suppression arm
            // (the `false` is load-bearing — `in_flight=true` would match the
            // "settling" arm and suppress), so a stale row still alarms rather
            // than being hidden behind "no Codex sessions found".
            return (true, true, false);
        };
        let now = std::time::SystemTime::now();
        let recent_cutoff = now
            .checked_sub(std::time::Duration::from_secs(IDLE_WINDOW_SECS))
            .unwrap_or(std::time::UNIX_EPOCH);
        // In-flight = modified within the poller's settle gate, so `poll_tick`
        // is still skipping it and `source_health` can't have advanced yet.
        let in_flight_cutoff = now
            .checked_sub(std::time::Duration::from_secs(cfg.codex.min_idle_secs))
            .unwrap_or(std::time::UNIX_EPOCH);
        let mut any_exist = false;
        let mut any_recent = false;
        let mut any_in_flight = false;
        for root in &cfg.codex.session_roots {
            // Skip roots that don't exist, mirroring `codex_session::poll_tick`.
            if !root.is_dir() {
                continue;
            }
            // rollout-*.jsonl files can live nested under the root (Codex
            // shards sessions into date-stamped subdirectories), so walk
            // recursively rather than reading one level.
            for entry in WalkDir::new(root).into_iter().filter_map(|e| e.ok()) {
                let path = entry.path();
                let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if !(name.starts_with("rollout-") && name.ends_with(".jsonl")) {
                    continue;
                }
                any_exist = true;
                if let Ok(meta) = entry.metadata()
                    && let Ok(modified) = meta.modified()
                {
                    any_recent |= modified > recent_cutoff;
                    any_in_flight |= modified > in_flight_cutoff;
                }
                // Both freshness facts settled — stop walking.
                if any_recent && any_in_flight {
                    return (true, true, true);
                }
            }
        }
        (any_exist, any_recent, any_in_flight)
    };

    // Inspect the Cursor agent-transcript files under the configured session
    // roots. Two facts drive doctor suppression, mirroring opencode's DB checks:
    //   - `any_exist`: at least one `agent-transcripts/*.jsonl` file is present.
    //     False ⇒ "user has never run Cursor" — suppress rather than alarm.
    //   - `any_recent`: at least one `agent-transcripts/*.jsonl` was modified
    //     within the IDLE_WINDOW_SECS window (the same window opencode uses for
    //     its DB mtime). False (with files present) ⇒ "user just isn't using
    //     Cursor right now" — suppress.  True with a stale `source_health` row ⇒
    //     a genuine ingestion failure, so it is NOT suppressed.
    let cursor_session_state = || -> (bool, bool, bool) {
        let Ok(cfg) = doctor_config.as_ref() else {
            // Config unreadable: fail open for alerting (mirrors codex above).
            return (true, true, false);
        };
        let now = std::time::SystemTime::now();
        let recent_cutoff = now
            .checked_sub(std::time::Duration::from_secs(IDLE_WINDOW_SECS))
            .unwrap_or(std::time::UNIX_EPOCH);
        // In-flight = modified within the poller's settle gate (mirrors codex).
        let in_flight_cutoff = now
            .checked_sub(std::time::Duration::from_secs(cfg.cursor.min_idle_secs))
            .unwrap_or(std::time::UNIX_EPOCH);
        let mut any_exist = false;
        let mut any_recent = false;
        let mut any_in_flight = false;
        for root in &cfg.cursor.session_roots {
            if !root.is_dir() {
                continue;
            }
            for entry in WalkDir::new(root).into_iter().filter_map(|e| e.ok()) {
                let path = entry.path();
                let is_jsonl = path.extension().map(|e| e == "jsonl").unwrap_or(false);
                let under = path
                    .components()
                    .any(|c| c.as_os_str() == "agent-transcripts");
                if !(is_jsonl && under) {
                    continue;
                }
                any_exist = true;
                if let Ok(meta) = entry.metadata()
                    && let Ok(modified) = meta.modified()
                {
                    any_recent |= modified > recent_cutoff;
                    any_in_flight |= modified > in_flight_cutoff;
                }
                // Both freshness facts settled — stop walking.
                if any_recent && any_in_flight {
                    return (true, true, true);
                }
            }
        }
        (any_exist, any_recent, any_in_flight)
    };

    // Check whether the opencode DB itself looks active — the file exists
    // and has been modified within IDLE_WINDOW_SECS. Stale opencode means
    // "user just isn't using opencode right now," not "the poller is broken."
    let opencode_db_recent = || -> bool {
        let Ok(cfg) = doctor_config.as_ref() else {
            // Config unreadable: fail open for alerting. `true` skips the
            // "opencode DB idle" suppression arm so a stale row still alarms
            // rather than being hidden behind a misleading "idle" reason.
            return true;
        };
        let Ok(meta) = std::fs::metadata(&cfg.opencode.db_path) else {
            return false;
        };
        let idle_cutoff = std::time::SystemTime::now()
            .checked_sub(std::time::Duration::from_secs(IDLE_WINDOW_SECS))
            .unwrap_or(std::time::UNIX_EPOCH);
        meta.modified().map(|m| m > idle_cutoff).unwrap_or(false)
    };

    // Check if there's a recent claude JSONL with mtime < 5 minutes (for claude-session suppression).
    let recent_claude_session = || -> bool {
        let projects_dir = match dirs::home_dir() {
            Some(h) => h.join(".claude/projects"),
            None => return false,
        };
        let five_min_ago = std::time::SystemTime::now()
            .checked_sub(std::time::Duration::from_secs(300))
            .unwrap_or(std::time::UNIX_EPOCH);
        let Ok(entries) = std::fs::read_dir(&projects_dir) else {
            return false;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) == Some("jsonl")
                && let Ok(meta) = std::fs::metadata(&path)
                && let Ok(modified) = meta.modified()
                && modified > five_min_ago
            {
                return true;
            }
            // Also recurse one level (projects/*/session.jsonl layout).
            if path.is_dir()
                && let Ok(sub) = std::fs::read_dir(&path)
            {
                for sub_entry in sub.flatten() {
                    let sub_path = sub_entry.path();
                    if sub_path.extension().and_then(|e| e.to_str()) == Some("jsonl")
                        && let Ok(meta) = std::fs::metadata(&sub_path)
                        && let Ok(modified) = meta.modified()
                        && modified > five_min_ago
                    {
                        return true;
                    }
                }
            }
        }
        false
    };

    let mut fail_count: u32 = 0;
    let (codex_sessions_do_exist, codex_sessions_are_recent, codex_session_is_in_flight) =
        codex_session_state();
    let (cursor_sessions_do_exist, cursor_sessions_are_recent, cursor_session_is_in_flight) =
        cursor_session_state();
    // Extract enabled flags from config; fall back to `true` (no suppression) on
    // a config-load error so a broken config file doesn't silently hide real failures.
    let (opencode_enabled, codex_enabled, cursor_enabled) = doctor_config
        .as_ref()
        .map(|cfg| (cfg.opencode.enabled, cfg.codex.enabled, cfg.cursor.enabled))
        .unwrap_or((true, true, true));
    let suppression_env = SuppressionSignals {
        probe_ok: None, // per-row; filled in inside the loop
        firefox_running: firefox_running(),
        recent_claude_session: recent_claude_session(),
        opencode_db_recent: opencode_db_recent(),
        opencode_enabled,
        codex_sessions_exist: codex_sessions_do_exist,
        codex_sessions_recent: codex_sessions_are_recent,
        codex_session_in_flight: codex_session_is_in_flight,
        codex_enabled,
        cursor_sessions_exist: cursor_sessions_do_exist,
        cursor_sessions_recent: cursor_sessions_are_recent,
        cursor_session_in_flight: cursor_session_is_in_flight,
        cursor_enabled,
    };

    // All expected sources — report missing ones too.
    let all_sources = [
        "agentic-session-codex",
        "agentic-session-cursor",
        "agentic-session-opencode",
        "browser",
        "agentic-session-claude",
        "claude-tool",
        "shell",
    ];
    for source in all_sources {
        let label = format!("{} events", source);
        let padded = format!("{:<29}", label);

        let row = rows.iter().find(|r| r.source == source);

        let Some(row) = row else {
            println!("[--] {}  no data", padded);
            continue;
        };

        let Some(last_ts) = row.last_event_ts else {
            println!("[--] {}  never seen", padded);
            continue;
        };

        let age_secs = (now_ms - last_ts) / 1000;
        let human = format_age_secs(age_secs);

        let signals = SuppressionSignals {
            probe_ok: row.probe_ok,
            ..suppression_env
        };
        match classify_source_staleness(source, age_secs, signals) {
            SourceStalenessStatus::Ok => {
                println!("[OK] {}  {}", padded, human);
            }
            SourceStalenessStatus::Warn => {
                println!("[WW] {}  {} (WARN)", padded, human);
            }
            SourceStalenessStatus::Suppressed(reason) => {
                println!(
                    "{}",
                    format_suppressed_source_staleness_line(&label, &human, reason)
                );
            }
            SourceStalenessStatus::Fail => {
                println!("[!!] {}  {} (FAIL)", padded, human);
                fail_count += 1;
                if explain {
                    println!("     CAUSE:  No events have landed in SQLite for this source");
                    println!(
                        "     FIX:    Check source is running: hippo doctor (re-run); tail -f ~/.local/share/hippo/daemon.stderr.log"
                    );
                    println!("     DOC:    docs/capture/architecture.md");
                }
            }
        }
    }

    fail_count
}

fn check_codex_state_coverage(
    config: &HippoConfig,
    db: &rusqlite::Connection,
    explain: bool,
) -> u32 {
    let label = "Codex state coverage";
    let padded = format!("{:<29}", label);
    if !config.codex.enabled {
        println!("[--] {}  disabled", padded);
        return 0;
    }

    let Some(home) = dirs::home_dir() else {
        println!("[--] {}  no home dir", padded);
        return 0;
    };
    let state_path = home.join(".codex/state_5.sqlite");
    if !state_path.exists() {
        println!("[--] {}  no Codex state DB", padded);
        return 0;
    }
    let logs_path = home.join(".codex/logs_2.sqlite");
    let logs_arg = logs_path.exists().then_some(logs_path.as_path());

    match codex_session::check_codex_coverage(db, &state_path, logs_arg, config.codex.min_idle_secs)
    {
        Ok(report) => {
            // Only threads that are on disk but missing from Hippo are an
            // actionable failure ([!!]) — re-running the poller can recover
            // them. A thread whose rollout file was deleted from disk
            // (`missing_rollout_threads`) is UNRECOVERABLE and is NOT a Hippo
            // bug: the source-of-truth transcript is gone, so counting it as a
            // failure would pin doctor at a permanent false `[!!]`. It is
            // reported below as an informational `[--]` line instead.
            let recoverable_missing = report.missing_hippo_threads.len();
            if recoverable_missing == 0 {
                println!(
                    "[OK] {}  {}/{} threads captured ({} in-flight, {} log-only diagnostics)",
                    padded,
                    report.covered_threads,
                    report.total_state_threads,
                    report.in_flight_threads.len(),
                    report.log_only_thread_count
                );
                // Surface deleted-rollout threads as informational, never as a
                // failure — they are unrecoverable by design.
                if !report.missing_rollout_threads.is_empty() {
                    println!(
                        "[--] {:<29}  {} thread(s) unrecoverable (rollout file deleted from disk)",
                        "Codex rollout (deleted)",
                        report.missing_rollout_threads.len()
                    );
                }
                return 0;
            }

            println!(
                "[!!] {}  {}/{} threads captured; {} missing from Hippo, {} unrecoverable (rollout deleted) ({} in-flight)",
                padded,
                report.covered_threads,
                report.total_state_threads,
                report.missing_hippo_threads.len(),
                report.missing_rollout_threads.len(),
                report.in_flight_threads.len()
            );
            if explain {
                if !report.missing_hippo_threads.is_empty() {
                    println!(
                        "     MISSING HIPPO: {}",
                        report
                            .missing_hippo_threads
                            .iter()
                            .take(10)
                            .cloned()
                            .collect::<Vec<_>>()
                            .join(", ")
                    );
                }
                if !report.missing_rollout_threads.is_empty() {
                    println!(
                        "     UNRECOVERABLE (rollout deleted, informational only): {}",
                        report
                            .missing_rollout_threads
                            .iter()
                            .take(10)
                            .cloned()
                            .collect::<Vec<_>>()
                            .join(", ")
                    );
                }
                println!("     FIX:    run `hippo codex-poll`, then re-run `hippo doctor`");
            }
            // Only the recoverable (on-disk-but-not-in-Hippo) gap fails; deleted
            // rollouts never contribute to the exit code.
            1
        }
        Err(e) => {
            println!("[!!] {}  coverage check failed: {e}", padded);
            1
        }
    }
}

/// True iff `s` is a canonical lowercase 8-4-4-4-12 hex UUID. hippo-daemon
/// carries no `regex` dependency (see `codex_session::extract_user_text`), so
/// this matches `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`
/// by hand. Claude Code names every real session file `<uuid>.jsonl`; non-UUID
/// stems (e.g. workflow journals) are not sessions and are excluded.
fn is_uuid_stem(s: &str) -> bool {
    let groups = [8usize, 4, 4, 4, 12];
    let parts: Vec<&str> = s.split('-').collect();
    if parts.len() != groups.len() {
        return false;
    }
    parts.iter().zip(groups.iter()).all(|(part, &len)| {
        part.len() == len
            && part
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    })
}

/// Cheap "does this Claude JSONL carry at least one conversational turn?" peek.
/// A session file is an "expected-absent" stub if it has no assistant turn.
/// `extract_segments` segments on assistant turns, so a file with only user
/// messages (e.g. a /command that never got a response) produces zero segments
/// and will never appear in `agentic_sessions`. Checking for an assistant turn
/// keeps doctor's definition consistent with the ingestor's.
///
/// Only the first `MAX_PEEK_BYTES` are read to avoid stalling doctor on a huge
/// transcript. This is safe in practice: the first assistant turn always appears
/// within a few KB of the start of any real session.
fn claude_jsonl_has_turn(path: &std::path::Path) -> bool {
    use std::io::Read;
    const MAX_PEEK_BYTES: usize = 256 * 1024;
    let Ok(mut file) = std::fs::File::open(path) else {
        return false;
    };
    let mut buf = vec![0u8; MAX_PEEK_BYTES];
    let n = match file.read(&mut buf) {
        Ok(n) => n,
        Err(_) => return false,
    };
    let text = String::from_utf8_lossy(&buf[..n]);
    text.contains("\"type\":\"assistant\"") || text.contains("\"type\": \"assistant\"")
}

/// Doctor completeness check: on-disk Claude sessions vs `agentic_sessions`
/// (harness = 'claude-code', probe_tag IS NULL). Mirrors
/// `check_codex_state_coverage`'s shape. Enumerates
/// `~/.claude/projects/**/*.jsonl`, EXCLUDING:
///
///   - paths under a `/subagents/` directory. These are either workflow
///     journals (not sessions) OR real subagent transcripts (`agent-*.jsonl`,
///     ingested as `agentic_sessions` rows with `is_subagent=1`). Subagent
///     transcripts have non-UUID `agent-<hex>` stems, so the UUID filter below
///     would drop them regardless; they ride with their parent session and are
///     intentionally NOT coverage-checked here (a dedicated subagent-coverage
///     check is a possible follow-up),
///   - files whose stem is not a canonical UUID,
///   - 0-turn empty stubs (correctly absent — `extract_segments` skips them).
///
/// Any remaining UUID-named (top-level) session not present in `agentic_sessions`
/// is a real gap and fails `[!!]`.
fn check_claude_session_coverage(
    db: &rusqlite::Connection,
    projects_dir: &std::path::Path,
    explain: bool,
) -> u32 {
    let label = "Claude session coverage";
    let padded = format!("{:<29}", label);

    if !projects_dir.is_dir() {
        println!("[--] {}  no Claude projects dir", padded);
        return 0;
    }

    let known = match read_agentic_session_ids(db, "claude-code") {
        Ok(set) => set,
        Err(e) => {
            println!("[!!] {}  coverage check failed: {e}", padded);
            return 1;
        }
    };

    let mut total_sessions = 0usize;
    let mut empty_stubs = 0usize;
    let mut missing: Vec<String> = Vec::new();

    for entry in WalkDir::new(projects_dir)
        .into_iter()
        .filter_map(|e| e.ok())
    {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("jsonl") {
            continue;
        }
        // Exclude everything under /subagents/: workflow journals (non-sessions)
        // and subagent transcripts (ingested with is_subagent=1, non-UUID stems,
        // covered via their parent — see the fn doc comment).
        if path.components().any(|c| c.as_os_str() == "subagents") {
            continue;
        }
        let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        if !is_uuid_stem(stem) {
            continue; // workflow journals and other non-session files
        }
        total_sessions += 1;
        if known.contains(stem) {
            continue;
        }
        // Not in Hippo: classify a 0-turn stub as expected-absent, not missing.
        if !claude_jsonl_has_turn(path) {
            empty_stubs += 1;
            continue;
        }
        if missing.len() < 10 {
            missing.push(stem.to_string());
        } else {
            missing.push(String::new()); // count-only past the display cap
        }
    }

    let missing_count = missing.len();
    if missing_count == 0 {
        println!(
            "[OK] {}  {} session(s) captured ({} empty stub(s) expected absent)",
            padded,
            total_sessions.saturating_sub(empty_stubs),
            empty_stubs
        );
        return 0;
    }

    println!(
        "[!!] {}  {} of {} session(s) missing from Hippo ({} empty stub(s) expected absent)",
        padded, missing_count, total_sessions, empty_stubs
    );
    if explain {
        let shown: Vec<&str> = missing
            .iter()
            .filter(|s| !s.is_empty())
            .map(|s| s.as_str())
            .collect();
        if !shown.is_empty() {
            println!("     MISSING HIPPO: {}", shown.join(", "));
        }
        println!(
            "     CAUSE:  The claude-session watcher hasn't created a row for these session files"
        );
        println!(
            "     FIX:    launchctl print gui/$(id -u)/com.hippo.claude-session-watcher; or `hippo ingest claude-session <path>`"
        );
        println!("     DOC:    docs/capture/operator-runbook.md");
    }
    // Cap the contribution to the exit code so a large backlog doesn't inflate
    // it unboundedly, matching `check_claude_session_db`.
    (missing_count.min(3)) as u32
}

/// Doctor completeness check: on-disk Cursor transcripts vs `agentic_sessions`
/// (harness = 'cursor', probe_tag IS NULL). Enumerates
/// `~/.cursor/projects/**/agent-transcripts/**/*.jsonl` and compares basenames
/// (session_id is the file stem, per `cursor_session::PathIdentity::from_path`).
/// NOTE: a parent session and its subagents are separate transcript files but
/// each is its own `agentic_sessions` row, so disk-file count naturally exceeds
/// or equals session-row count without being a gap — this check only flags a
/// transcript whose stem has NO matching row at all.
fn check_cursor_session_coverage(
    config: &HippoConfig,
    db: &rusqlite::Connection,
    explain: bool,
) -> u32 {
    let label = "Cursor session coverage";
    let padded = format!("{:<29}", label);

    if !config.cursor.enabled {
        println!("[--] {}  disabled", padded);
        return 0;
    }

    let known = match read_agentic_session_ids(db, "cursor") {
        Ok(set) => set,
        Err(e) => {
            println!("[!!] {}  coverage check failed: {e}", padded);
            return 1;
        }
    };

    // Skip files still inside the idle window — like the poller, an in-flight
    // transcript is not yet expected to have a row.
    let idle_cutoff = std::time::SystemTime::now()
        .checked_sub(std::time::Duration::from_secs(config.cursor.min_idle_secs))
        .unwrap_or(std::time::UNIX_EPOCH);

    let mut any_root = false;
    let mut total_transcripts = 0usize;
    let mut in_flight = 0usize;
    let mut missing: Vec<String> = Vec::new();

    for root in &config.cursor.session_roots {
        if !root.is_dir() {
            continue;
        }
        any_root = true;
        for entry in WalkDir::new(root).into_iter().filter_map(|e| e.ok()) {
            let path = entry.path();
            let is_jsonl = path.extension().map(|e| e == "jsonl").unwrap_or(false);
            let under = path
                .components()
                .any(|c| c.as_os_str() == "agent-transcripts");
            if !(is_jsonl && under) {
                continue;
            }
            // Skip in-flight files (modified within the idle window).
            if let Ok(meta) = entry.metadata()
                && let Ok(modified) = meta.modified()
                && modified > idle_cutoff
            {
                in_flight += 1;
                continue;
            }
            let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else {
                continue;
            };
            total_transcripts += 1;
            if known.contains(stem) {
                continue;
            }
            if missing.len() < 10 {
                missing.push(stem.to_string());
            } else {
                missing.push(String::new());
            }
        }
    }

    if !any_root {
        println!("[--] {}  no Cursor projects dir", padded);
        return 0;
    }

    let missing_count = missing.len();
    if missing_count == 0 {
        println!(
            "[OK] {}  {} transcript(s) captured ({} in-flight)",
            padded, total_transcripts, in_flight
        );
        return 0;
    }

    println!(
        "[!!] {}  {} of {} transcript(s) missing from Hippo ({} in-flight)",
        padded, missing_count, total_transcripts, in_flight
    );
    if explain {
        let shown: Vec<&str> = missing
            .iter()
            .filter(|s| !s.is_empty())
            .map(|s| s.as_str())
            .collect();
        if !shown.is_empty() {
            println!("     MISSING HIPPO: {}", shown.join(", "));
        }
        println!("     FIX:    run `hippo cursor-poll`, then re-run `hippo doctor`");
        println!("     DOC:    docs/capture/operator-runbook.md");
    }
    (missing_count.min(3)) as u32
}

/// Read the set of `agentic_sessions.session_id` for a given harness, excluding
/// synthetic probe rows. Shared by the Claude/Cursor coverage checks; mirrors
/// `codex_session::read_hippo_session_ids` (which is harness-fixed to 'codex').
fn read_agentic_session_ids(
    db: &rusqlite::Connection,
    harness: &str,
) -> anyhow::Result<std::collections::HashSet<String>> {
    let mut stmt = db.prepare(
        "SELECT DISTINCT session_id
         FROM agentic_sessions
         WHERE harness = ?1
           AND probe_tag IS NULL",
    )?;
    let set = stmt
        .query_map(rusqlite::params![harness], |row| row.get::<_, String>(0))?
        .collect::<std::result::Result<std::collections::HashSet<_>, _>>()?;
    Ok(set)
}

fn format_suppressed_source_staleness_line(label: &str, human: &str, reason: &str) -> String {
    format!("[--] {:<29}  {} (suppressed — {})", label, human, reason)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SourceStalenessStatus {
    Ok,
    Warn,
    Fail,
    Suppressed(&'static str),
}

/// Environment facts that let a stale `source_health` row be suppressed as
/// "the user just isn't using this source right now" rather than alarmed as a
/// capture failure. Bundled into one struct so the staleness helpers stay
/// under clippy's argument-count limit and the call sites are self-documenting.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct SuppressionSignals {
    /// `probe_ok` column from `source_health` (synthetic-probe state).
    probe_ok: Option<i64>,
    /// A Firefox process is currently running.
    firefox_running: bool,
    /// A Claude session JSONL was modified within the last 5 minutes.
    recent_claude_session: bool,
    /// The opencode DB file was modified within the last 10 minutes.
    opencode_db_recent: bool,
    /// `[opencode] enabled` from config — false means the source is intentionally
    /// disabled and staleness alarms must not fire regardless of file activity.
    opencode_enabled: bool,
    /// At least one Codex `rollout-*.jsonl` file exists under the roots.
    codex_sessions_exist: bool,
    /// At least one Codex `rollout-*.jsonl` file changed within 10 minutes.
    codex_sessions_recent: bool,
    /// The newest Codex `rollout-*.jsonl` was modified within `[codex]
    /// min_idle_secs` — i.e. it is still being written, so the poller is
    /// *correctly* skipping it (it waits for the file to settle before reading,
    /// to avoid a partial parse). A stale `source_health` row in this state is
    /// expected, not a capture failure.
    codex_session_in_flight: bool,
    /// `[codex] enabled` from config — false means the source is intentionally
    /// disabled and staleness alarms must not fire regardless of file activity.
    codex_enabled: bool,
    /// At least one Cursor agent-transcript `.jsonl` exists under the roots.
    cursor_sessions_exist: bool,
    /// At least one Cursor agent-transcript `.jsonl` changed within 10 minutes.
    cursor_sessions_recent: bool,
    /// The newest Cursor agent-transcript `.jsonl` was modified within `[cursor]
    /// min_idle_secs` — i.e. it is still being written, so the poller is
    /// *correctly* skipping it (mirrors `codex_session_in_flight`).
    cursor_session_in_flight: bool,
    /// `[cursor] enabled` from config — false means the source is intentionally
    /// disabled and staleness alarms must not fire regardless of file activity.
    cursor_enabled: bool,
}

fn classify_source_staleness(
    source: &str,
    age_secs: i64,
    signals: SuppressionSignals,
) -> SourceStalenessStatus {
    let thresh = source_staleness_thresholds_for(source);
    if age_secs < thresh.warn_secs {
        return SourceStalenessStatus::Ok;
    }

    if let Some(reason) = source_staleness_suppression_reason(source, signals) {
        return SourceStalenessStatus::Suppressed(reason);
    }

    if age_secs < thresh.fail_secs {
        SourceStalenessStatus::Warn
    } else {
        SourceStalenessStatus::Fail
    }
}

fn source_staleness_suppression_reason(
    source: &str,
    signals: SuppressionSignals,
) -> Option<&'static str> {
    match source {
        "shell" if signals.probe_ok == Some(0) => Some("probe disabled"),
        "agentic-session-claude" if !signals.recent_claude_session => Some("no active session"),
        "claude-tool" if signals.probe_ok == Some(0) => Some("probe disabled"),
        "browser" if !signals.firefox_running => Some("no active Firefox session"),
        // Disabled sources are suppressed before any idle / file-activity checks.
        // A user who sets `enabled = false` in config is opting out intentionally;
        // even if transcript files exist on disk, the poller is halted so a stale
        // `source_health` row is expected — not a capture failure.
        "agentic-session-opencode" if !signals.opencode_enabled => Some("source disabled"),
        "agentic-session-opencode" if !signals.opencode_db_recent => Some("opencode DB idle"),
        "agentic-session-codex" if !signals.codex_enabled => Some("source disabled"),
        // Codex has two distinct idle cases. Never-installed (no rollout files
        // at all) is checked first. Otherwise, files exist but none changed
        // recently ⇒ the user simply isn't running Codex right now. A recent
        // rollout file with a stale `source_health` row is intentionally NOT
        // matched here — that combination is a genuine ingestion failure and
        // must still alarm.
        "agentic-session-codex" if !signals.codex_sessions_exist => Some("no Codex sessions found"),
        // A rollout still being written is in-flight: the poller skips files
        // modified within `min_idle_secs` to avoid a partial read, so a stale
        // `source_health` row is expected until the file settles. Checked before
        // the idle arm so an active session is never mislabelled "idle" and
        // never reaches the Fail arm. Self-clears once the file stops growing.
        "agentic-session-codex" if signals.codex_session_in_flight => {
            Some("Codex session in-flight (settling)")
        }
        "agentic-session-codex" if !signals.codex_sessions_recent => Some("Codex sessions idle"),
        "agentic-session-cursor" if !signals.cursor_enabled => Some("source disabled"),
        "agentic-session-cursor" if !signals.cursor_sessions_exist => {
            Some("no Cursor sessions found")
        }
        "agentic-session-cursor" if signals.cursor_session_in_flight => {
            Some("Cursor session in-flight (settling)")
        }
        "agentic-session-cursor" if !signals.cursor_sessions_recent => Some("Cursor sessions idle"),
        _ => None,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct SourceStalenessThresholds {
    warn_secs: i64,
    fail_secs: i64,
}

fn source_staleness_thresholds_for(source: &str) -> SourceStalenessThresholds {
    match source {
        // These rows are advanced by 5-minute synthetic probes, so warnings
        // must not fire between normal probe ticks.
        "shell" | "browser" => SourceStalenessThresholds {
            warn_secs: 420,
            fail_secs: 900,
        },
        "agentic-session-claude" => SourceStalenessThresholds {
            warn_secs: 300,
            fail_secs: 1800,
        },
        "claude-tool" => SourceStalenessThresholds {
            warn_secs: 300,
            fail_secs: 600,
        },
        // Opencode, Codex, and Cursor are all interval pollers — tolerate a
        // missed tick before warning, an hour before failing. All three also
        // suppress idle-source warnings in `source_staleness_suppression_reason`.
        "agentic-session-opencode" | "agentic-session-codex" | "agentic-session-cursor" => {
            SourceStalenessThresholds {
                warn_secs: 300,
                fail_secs: 3600,
            }
        }
        _ => SourceStalenessThresholds {
            warn_secs: 300,
            fail_secs: 1800,
        },
    }
}

/// Format age in seconds to a human-readable string.
fn format_age_secs(secs: i64) -> String {
    if secs < 60 {
        format!("{}s ago", secs)
    } else if secs < 3600 {
        format!("{}m ago", secs / 60)
    } else if secs < 86400 {
        format!("{}h ago", secs / 3600)
    } else {
        format!("{}d ago", secs / 86400)
    }
}

/// Check 4: Verify that hippo.zsh is sourced in a zsh startup file.
///
/// Returns 1 if not found in any zshrc/zshenv, 0 otherwise.
fn check_zsh_hook_sourced(explain: bool) -> u32 {
    let home = match dirs::home_dir() {
        Some(h) => h,
        None => {
            println!("[--] zsh hook sourced           cannot determine home dir");
            return 0;
        }
    };

    // Direct startup files. Most users wire the hook here.
    let mut candidates: Vec<std::path::PathBuf> = vec![
        home.join(".zshrc"),
        home.join(".zshenv"),
        home.join(".config/zsh/.zshrc"),
        home.join(".config/zsh/.zshenv"),
    ];

    // Drop-in dirs that interactive zshrcs commonly loop over (chezmoi /
    // dotfiles users routinely split aliases + functions across `profile.d`
    // or `.zshrc.d`). Without these the check false-negatives on any setup
    // that sources hippo.zsh transitively, even when shell events are
    // landing fine.
    for drop_in in [home.join(".config/zsh/profile.d"), home.join(".zshrc.d")] {
        if let Ok(entries) = std::fs::read_dir(&drop_in) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().and_then(|e| e.to_str()) == Some("zsh") {
                    candidates.push(path);
                }
            }
        }
    }

    for candidate in &candidates {
        if !candidate.exists() {
            continue;
        }
        let content = match std::fs::read_to_string(candidate) {
            Ok(c) => c,
            Err(_) => continue,
        };
        for line in content.lines() {
            // Skip blank and commented lines — a commented-out `# source …hippo.zsh`
            // is not an active source directive.
            let trimmed = line.trim_start();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                continue;
            }

            // Require the line to actually start with `source` or `.` (the POSIX
            // sourcing commands). Substring-matching `hippo.zsh` anywhere in the
            // line gave false positives on comments, variable assignments, etc.
            let mut tokens = trimmed.split_whitespace();
            let Some(cmd) = tokens.next() else { continue };
            if cmd != "source" && cmd != "." {
                continue;
            }
            let sourced_path: Option<&str> = tokens
                .next()
                .map(|p| p.trim_matches(|c: char| c == '"' || c == '\'' || c == ';'));
            if !sourced_path
                .map(|p| p.contains("hippo.zsh"))
                .unwrap_or(false)
            {
                continue;
            }

            let script_exists = sourced_path
                .map(|p| {
                    // Expand `~/` and `$HOME/` (the two forms users actually
                    // write in zshrcs). Don't try to handle arbitrary env
                    // expansion — the path either exists at the literal
                    // resolved location or the warning is correct.
                    let expanded = if let Some(rest) = p.strip_prefix("~/") {
                        home.join(rest)
                    } else if let Some(rest) = p.strip_prefix("$HOME/") {
                        home.join(rest)
                    } else if let Some(rest) = p.strip_prefix("${HOME}/") {
                        home.join(rest)
                    } else {
                        std::path::PathBuf::from(p)
                    };
                    expanded.exists()
                })
                .unwrap_or(false);

            let short_candidate = candidate
                .strip_prefix(&home)
                .map(|p| format!("~/{}", p.display()))
                .unwrap_or_else(|_| candidate.display().to_string());

            if script_exists {
                println!(
                    "[OK] {:<29}  found in {}",
                    "zsh hook sourced", short_candidate
                );
            } else {
                let path_str = sourced_path.unwrap_or("<unknown>");
                println!(
                    "[WW] {:<29}  source line found but script missing at {}",
                    "zsh hook sourced", path_str
                );
            }
            return 0;
        }
    }

    println!(
        "[!!] {:<29}  not found in any zshrc/zshenv",
        "zsh hook sourced"
    );
    if explain {
        println!("     CAUSE:  Shell hook not loaded — shell events cannot be captured");
        println!("     FIX:    Add to ~/.zshrc: source ~/.local/share/hippo/hippo.zsh");
        println!("     DOC:    docs/capture/anti-patterns.md");
    }
    1
}

/// Check 7: Warn/fail on large log files in the hippo data directory.
///
/// Returns 1 if any file exceeds 200 MB, 0 otherwise.
fn check_log_file_sizes(config: &HippoConfig, explain: bool) -> u32 {
    let data_dir = &config.storage.data_dir;
    if !data_dir.exists() {
        println!("[--] {:<29}  data dir not found", "log file sizes");
        return 0;
    }

    let entries = match std::fs::read_dir(data_dir) {
        Ok(e) => e,
        Err(err) => {
            println!(
                "[--] {:<29}  cannot read data dir: {}",
                "log file sizes", err
            );
            return 0;
        }
    };

    let mut largest_warn: Option<(String, u64)> = None;
    let mut largest_fail: Option<(String, u64)> = None;

    const WARN_BYTES: u64 = 50 * 1024 * 1024; // 50 MB
    const FAIL_BYTES: u64 = 200 * 1024 * 1024; // 200 MB

    for entry in entries.flatten() {
        let path = entry.path();
        let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("");
        if ext != "log" && ext != "jsonl" {
            continue;
        }
        let Ok(meta) = std::fs::metadata(&path) else {
            continue;
        };
        let size = meta.len();
        let name = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("?")
            .to_string();

        if size >= FAIL_BYTES {
            match &largest_fail {
                None => largest_fail = Some((name, size)),
                Some((_, prev)) if size > *prev => largest_fail = Some((name, size)),
                _ => {}
            }
        } else if size >= WARN_BYTES {
            match &largest_warn {
                None => largest_warn = Some((name, size)),
                Some((_, prev)) if size > *prev => largest_warn = Some((name, size)),
                _ => {}
            }
        }
    }

    if let Some((name, size)) = largest_fail {
        let size_mb = size / (1024 * 1024);
        println!("[!!] {:<29}  {}: {}MB", "log file sizes", name, size_mb);
        if explain {
            let full_path = data_dir.join(&name);
            println!("     CAUSE:  Log file growing without rotation");
            println!(
                "     FIX:    Upgrade hippo to a version with log rotation, or: truncate -s 0 {}",
                full_path.display()
            );
            println!("     DOC:    docs/archive/capture-reliability-overhaul/07-roadmap.md");
        }
        return 1;
    }

    if let Some((name, size)) = largest_warn {
        let size_mb = size / (1024 * 1024);
        println!("[WW] {:<29}  {}: {}MB", "log file sizes", name, size_mb);
        return 0;
    }

    println!("[OK] {:<29}  all under 50MB", "log file sizes");
    0
}

/// Warns when the user's `config.toml` still carries a `[capture]` section.
///
/// `[capture]` (and its only key, `claude_session_mode`) was deleted in T-8 /
/// PR #89. `HippoConfig` does not use `deny_unknown_fields`, so a stale
/// section silently drops on load — the user's old `tmux-tailer` override
/// stops doing anything with no visible signal. This check closes the
/// upgrade-path gap by surfacing it once on the next `hippo doctor` run.
///
/// Returns 0 always — this is a `[WW]` warning, not a failure.
fn check_legacy_capture_section(config_path: &std::path::Path, explain: bool) -> u32 {
    let Ok(text) = std::fs::read_to_string(config_path) else {
        return 0;
    };
    let has_capture = text
        .lines()
        .any(|l| l.trim_start().starts_with("[capture]"));
    if !has_capture {
        return 0;
    }
    println!(
        "[WW] {:<29}  legacy [capture] section in {}",
        "config legacy section",
        config_path.display()
    );
    if explain {
        println!("     CAUSE:  [capture] / claude_session_mode was retired in T-8 (PR #89);");
        println!(
            "             the FS watcher (com.hippo.claude-session-watcher) is the sole ingester."
        );
        println!(
            "             The section is silently ignored on load — your tmux-tailer override is dead."
        );
        println!(
            "     FIX:    Remove the [capture] section from {}",
            config_path.display()
        );
        println!("     DOC:    docs/archive/capture-reliability-overhaul/07-roadmap.md (T-8)");
    }
    0
}

#[derive(Debug, PartialEq, Eq)]
enum WatchdogHeartbeatStatus {
    Ok,
    Warn,
    Fail,
}

fn watchdog_heartbeat_status(age_secs: i64) -> WatchdogHeartbeatStatus {
    if age_secs < 120 {
        WatchdogHeartbeatStatus::Ok
    } else if age_secs < 180 {
        WatchdogHeartbeatStatus::Warn
    } else {
        WatchdogHeartbeatStatus::Fail
    }
}

/// Check 8: Watchdog heartbeat — verify the watchdog row in source_health is fresh.
///
/// Returns 1 if the watchdog row is stale (>= 180s), 0 otherwise.
fn check_watchdog_heartbeat(db: &rusqlite::Connection, explain: bool) -> u32 {
    let result = db.query_row(
        "SELECT updated_at, (unixepoch('now')*1000 - updated_at)/1000 AS age_secs \
         FROM source_health WHERE source = 'watchdog' LIMIT 1",
        [],
        |row| {
            let _updated_at: i64 = row.get(0)?;
            let age_secs: i64 = row.get(1)?;
            Ok(age_secs)
        },
    );

    match result {
        Err(rusqlite::Error::QueryReturnedNoRows) => {
            println!("[--] {:<29}  not installed", "watchdog heartbeat");
            0
        }
        Err(e) if e.to_string().contains("no such table") => {
            // Pre-migration DB (v7 or older) — source_health does not exist yet.
            println!("[--] {:<29}  not installed", "watchdog heartbeat");
            0
        }
        Err(e) => {
            println!("[!!] {:<29}  DB error: {e}", "watchdog heartbeat");
            if explain {
                println!("     CAUSE:  source_health query returned an unexpected DB error");
                println!("     FIX:    Inspect ~/.local/share/hippo/hippo.db for corruption");
                println!("     DOC:    docs/capture/operator-runbook.md");
            }
            1
        }
        Ok(age_secs) => match watchdog_heartbeat_status(age_secs) {
            WatchdogHeartbeatStatus::Ok => {
                println!("[OK] {:<29}  {}s ago", "watchdog heartbeat", age_secs);
                0
            }
            WatchdogHeartbeatStatus::Warn => {
                println!(
                    "[WW] {:<29}  {}s ago (WARN, expected < 120s)",
                    "watchdog heartbeat", age_secs
                );
                0
            }
            WatchdogHeartbeatStatus::Fail => {
                println!(
                    "[!!] {:<29}  stale {}s ago (FAIL)",
                    "watchdog heartbeat", age_secs
                );
                if explain {
                    println!("     CAUSE:  Watchdog has stopped sending heartbeats");
                    println!("     FIX:    Restart the watchdog service: mise run restart");
                    println!(
                        "     DOC:    docs/archive/capture-reliability-overhaul/07-roadmap.md"
                    );
                }
                1
            }
        },
    }
}

/// Informational doctor line: how many alarms the watchdog has auto-resolved
/// but the user hasn't acked. Doesn't affect exit code — these are
/// historical records of recovered outages, not current problems.
///
/// Silently no-ops when capture_alarms is absent (pre-v9 DB) or empty.
fn check_resolved_alarm_count(db: &rusqlite::Connection) {
    const LABEL: &str = "auto-resolved alarms";

    let result = db.query_row(
        "SELECT COUNT(*) FROM capture_alarms
         WHERE acked_at IS NULL AND resolved_at IS NOT NULL",
        [],
        |row| row.get::<_, i64>(0),
    );

    match result {
        Ok(0) => {} // silent on the steady-state "nothing to clean up"
        Ok(n) => println!(
            "[--] {:<29}  {} pending (run `hippo alarms prune` to clear)",
            LABEL, n
        ),
        // Pre-migration DB or missing column — no signal to surface.
        Err(_) => {}
    }
}

// ─── Check 2: Native Messaging manifest health ──────────────────────────────

/// Check 2: Native Messaging manifest health.
///
/// Verifies the manifest file exists, is valid JSON, the `path` field points
/// to an executable binary, and `allowed_extensions` contains
/// `"hippo-browser@local"`.
///
/// `nm_manifest_path` is the full path to `hippo_daemon.json`; injectable for
/// tests (pass a tempdir path instead of `~/Library/…`).
pub fn check_nm_manifest(nm_manifest_path: &std::path::Path, explain: bool) -> u32 {
    const LABEL: &str = "native-msg manifest";

    if !nm_manifest_path.exists() {
        println!("[!!] {:<29}  not found", LABEL);
        if explain {
            println!(
                "     CAUSE:  Native Messaging manifest not installed — Firefox cannot launch the host"
            );
            println!("     FIX:    hippo daemon install --force");
            println!("     DOC:    docs/capture/operator-runbook.md");
        }
        return 1;
    }

    let content = match std::fs::read_to_string(nm_manifest_path) {
        Ok(c) => c,
        Err(e) => {
            println!("[!!] {:<29}  cannot read: {}", LABEL, e);
            if explain {
                println!("     CAUSE:  Manifest file exists but is not readable");
                println!("     FIX:    hippo daemon install --force");
                println!("     DOC:    docs/capture/operator-runbook.md");
            }
            return 1;
        }
    };

    let json: serde_json::Value = match serde_json::from_str(&content) {
        Ok(v) => v,
        Err(e) => {
            println!("[!!] {:<29}  invalid JSON: {}", LABEL, e);
            if explain {
                println!("     CAUSE:  Manifest file is not valid JSON");
                println!("     FIX:    hippo daemon install --force");
                println!("     DOC:    docs/capture/operator-runbook.md");
            }
            return 1;
        }
    };

    // Check `path` field exists and is an executable file.
    let Some(path_str) = json.get("path").and_then(|v| v.as_str()) else {
        println!("[!!] {:<29}  missing `path` field", LABEL);
        if explain {
            println!("     CAUSE:  Manifest is malformed — no `path` key");
            println!("     FIX:    hippo daemon install --force");
            println!("     DOC:    docs/capture/operator-runbook.md");
        }
        return 1;
    };

    let binary = std::path::Path::new(path_str);
    if !binary.exists() {
        println!("[!!] {:<29}  `path` not found: {}", LABEL, path_str);
        if explain {
            println!("     CAUSE:  Manifest `path` points to a non-existent binary");
            println!("     FIX:    hippo daemon install --force");
            println!("     DOC:    docs/capture/operator-runbook.md");
        }
        return 1;
    }

    use std::os::unix::fs::PermissionsExt;
    let meta = std::fs::metadata(binary).ok();
    if !meta.as_ref().map(|m| m.is_file()).unwrap_or(false) {
        println!(
            "[!!] {:<29}  `path` is not a regular file: {}",
            LABEL, path_str
        );
        if explain {
            println!(
                "     CAUSE:  Manifest `path` points to a directory or special file, not an executable binary"
            );
            println!("     FIX:    hippo daemon install --force");
            println!("     DOC:    docs/capture/operator-runbook.md");
        }
        return 1;
    }
    if !meta
        .map(|m| m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
    {
        println!("[!!] {:<29}  `path` not executable: {}", LABEL, path_str);
        if explain {
            println!("     CAUSE:  Manifest `path` binary lacks execute permission");
            println!("     FIX:    chmod +x {}", path_str);
            println!("     DOC:    docs/capture/operator-runbook.md");
        }
        return 1;
    }

    // Check allowed_extensions contains the hippo extension ID.
    let has_ext = json
        .get("allowed_extensions")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .any(|e| e.as_str() == Some("hippo-browser@local"))
        })
        .unwrap_or(false);
    if !has_ext {
        println!(
            "[!!] {:<29}  `allowed_extensions` missing `hippo-browser@local`",
            LABEL
        );
        if explain {
            println!(
                "     CAUSE:  Extension ID absent from manifest — Firefox refuses to launch the host"
            );
            println!("     FIX:    hippo daemon install --force");
            println!("     DOC:    docs/capture/operator-runbook.md");
        }
        return 1;
    }

    println!(
        "[OK] {:<29}  path={}, extension ID matches",
        LABEL, path_str
    );
    0
}

// ─── Check 5: Live-session vs DB reconciliation ──────────────────────────────

/// Recursively collect JSONL files under `dir` whose mtime is newer than
/// `cutoff`.  Handles the Claude Code layout where subagent transcripts live
/// at `<project>/<parent-uuid>/subagents/<id>.jsonl`.
fn collect_active_jsonls(
    dir: &std::path::Path,
    cutoff: std::time::SystemTime,
    result: &mut Vec<std::path::PathBuf>,
) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_active_jsonls(&path, cutoff, result);
        } else if path.extension().and_then(|e| e.to_str()) == Some("jsonl")
            && !is_workflow_journal(&path)
            && let Ok(meta) = std::fs::metadata(&path)
            && let Ok(modified) = meta.modified()
            && modified > cutoff
        {
            result.push(path);
        }
    }
}

/// Returns true for Claude workflow orchestration journals, not session files.
///
/// These are `journal.jsonl` metadata files under a `subagents/.../workflows`
/// path, so check 5 excludes them from active-session reconciliation.
fn is_workflow_journal(path: &std::path::Path) -> bool {
    if path.file_name() != Some(std::ffi::OsStr::new("journal.jsonl")) {
        return false;
    }

    let mut saw_subagents = false;
    for component in path.components() {
        let name = component.as_os_str();
        if saw_subagents && name == "workflows" {
            return true;
        }
        if name == "subagents" {
            saw_subagents = true;
        }
    }

    false
}

/// Check 5: Recursively find active Claude session JSONL files under
/// `projects_dir` and verify each has a matching real row in `agentic_sessions`.
///
/// `projects_dir` is `~/.claude/projects` (injectable for tests).
/// A session file is "active" if its mtime is < 5 minutes old.
/// Recursion handles subagent transcripts at `<proj>/<parent>/subagents/*.jsonl`.
/// Returns fail_count capped at 3.
pub fn check_claude_session_db(
    projects_dir: &std::path::Path,
    data_dir: &std::path::Path,
    db: &rusqlite::Connection,
    explain: bool,
) -> u32 {
    const LABEL: &str = "claude-session DB";

    if !projects_dir.is_dir() {
        println!("[--] {:<29}  projects dir not found", LABEL);
        return 0;
    }

    let five_min_ago = std::time::SystemTime::now()
        .checked_sub(std::time::Duration::from_secs(300))
        .unwrap_or(std::time::UNIX_EPOCH);

    // Collect active JSONL paths (mtime within last 5 minutes) recursively.
    let mut active: Vec<std::path::PathBuf> = Vec::new();
    collect_active_jsonls(projects_dir, five_min_ago, &mut active);

    if active.is_empty() {
        println!("[--] {:<29}  no active sessions", LABEL);
        return 0;
    }

    // session_id is the file stem (the JSONL filename without extension).
    // This matches how hippo_daemon::claude_session::SessionFile::from_path derives it.
    let mut missing: u32 = 0;
    for path in &active {
        let Some(session_id) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };

        let exists = db
            .query_row(
                "SELECT 1 FROM agentic_sessions \
                 WHERE session_id = ? AND harness = 'claude-code' \
                 AND probe_tag IS NULL LIMIT 1",
                rusqlite::params![session_id],
                |_| Ok(()),
            )
            .optional()
            .unwrap_or(None)
            .is_some();

        if !exists {
            let short = &session_id[..session_id.len().min(8)];
            let fname = path.file_name().and_then(|n| n.to_str()).unwrap_or("?");
            println!(
                "[!!] {:<29}  session {}… not in DB (FAIL, active JSONL {})",
                LABEL, short, fname
            );
            if explain && missing == 0 {
                let log_path = data_dir.join("claude-session-watcher.log");
                println!(
                    "     CAUSE:  Watcher hasn't created a row for this active JSONL (watcher down, FSEvents missed the growth event, or the file has no extractable segments yet)"
                );
                println!(
                    "     FIX:    launchctl print gui/$(id -u)/com.hippo.claude-session-watcher; tail -f {}",
                    log_path.display()
                );
                println!("     DOC:    docs/capture/operator-runbook.md");
            }
            missing += 1;
            if missing >= 3 {
                break;
            }
        }
    }

    if missing == 0 {
        println!(
            "[OK] {:<29}  {} active session(s) in DB",
            LABEL,
            active.len()
        );
    }

    missing.min(3)
}

// ─── Check 6: Session-hook log vs DB ────────────────────────────────────────

/// Count "hook invoked" log entries within the past hour.
///
/// Streams the log through a ring buffer capped at 10 000 lines so memory
/// usage is bounded even for large files. Each line's first
/// whitespace-delimited token must be a valid RFC 3339 timestamp.
fn count_hook_invocations_in_last_1h(log_path: &std::path::Path) -> i64 {
    use std::collections::VecDeque;
    use std::io::{BufRead, BufReader};

    const MAX_LINES: usize = 10_000;

    let Ok(file) = std::fs::File::open(log_path) else {
        return 0;
    };

    // Stream into a ring buffer — avoids loading the whole file into memory.
    let mut ring: VecDeque<String> = VecDeque::with_capacity(MAX_LINES + 1);
    for line in BufReader::new(file).lines().map_while(Result::ok) {
        if ring.len() == MAX_LINES {
            ring.pop_front();
        }
        ring.push_back(line);
    }

    let one_hour_ago = chrono::Utc::now() - chrono::TimeDelta::hours(1);
    let mut count: i64 = 0;

    for line in &ring {
        if !line.contains("hook invoked") {
            continue;
        }
        if let Some(ts_str) = line.split_whitespace().next()
            && let Ok(ts) = chrono::DateTime::parse_from_rfc3339(ts_str)
            && ts.with_timezone(&chrono::Utc) > one_hour_ago
        {
            count += 1;
        }
    }
    count
}

/// Check 6: Session-hook debug log vs DB reconciliation.
///
/// Counts `"hook invoked"` entries in the last hour (capped at 10 000 log
/// lines) and compares to `agentic_sessions` rows (harness = 'claude-code')
/// created in the same window, excluding synthetic probe rows.
///
/// `log_path` = `$DATA_DIR/session-hook-debug.log` (injectable for tests).
pub fn check_session_hook_log(
    log_path: &std::path::Path,
    data_dir: &std::path::Path,
    db: &rusqlite::Connection,
    explain: bool,
) -> u32 {
    const LABEL: &str = "session-hook log";

    let invocations = count_hook_invocations_in_last_1h(log_path);

    let one_hour_ago_ms = chrono::Utc::now().timestamp_millis() - 3_600_000i64;
    let db_rows: i64 = db
        .query_row(
            "SELECT COUNT(*) FROM agentic_sessions \
             WHERE harness = 'claude-code' AND probe_tag IS NULL AND created_at >= ?",
            rusqlite::params![one_hour_ago_ms],
            |row| row.get(0),
        )
        .unwrap_or(0);

    if invocations == 0 && db_rows == 0 {
        println!("[--] {:<29}  no hook activity", LABEL);
        return 0;
    }

    if invocations > 0 && db_rows > 0 {
        println!(
            "[OK] {:<29}  {} invocations, {} DB rows (last 1h)",
            LABEL, invocations, db_rows
        );
        return 0;
    }

    if invocations > 0 && db_rows == 0 && invocations < 3 {
        println!(
            "[WW] {:<29}  {} invocations, 0 DB rows — too fresh",
            LABEL, invocations
        );
        return 0;
    }

    if invocations >= 3 && db_rows == 0 {
        println!(
            "[!!] {:<29}  {} invocations, 0 DB rows (last 1h)",
            LABEL, invocations
        );
        if explain {
            let watcher_log = data_dir.join("claude-session-watcher.log");
            println!(
                "     CAUSE:  Hook is firing but the watcher is not producing agentic_sessions rows"
            );
            println!(
                "     FIX:    launchctl print gui/$(id -u)/com.hippo.claude-session-watcher; tail -f {}",
                watcher_log.display()
            );
            println!("     DOC:    docs/capture/operator-runbook.md");
        }
        return 1;
    }

    // invocations == 0 && db_rows > 0 — sessions in DB but hook not logging recently.
    // Not an error; could be sessions from earlier this hour.
    println!(
        "[OK] {:<29}  0 invocations, {} DB rows (last 1h)",
        LABEL, db_rows
    );
    0
}

// ─── Check 9: Fallback file age ──────────────────────────────────────────────

/// Check 9: Fallback JSONL file age.
///
/// Extends the old "count fallback files" check with an mtime predicate:
/// - No files → `[OK]`
/// - Files all < 24 h old → `[WW]` (daemon may still be down / recovering)
/// - Any file > 24 h old AND daemon is responding → `[!!]` (drain is broken)
///
/// `daemon_reachable`: true if the daemon socket returned a valid Status
/// response earlier in `handle_doctor`; injectable for tests.
pub fn check_fallback_age(
    fallback_dir: &std::path::Path,
    daemon_reachable: bool,
    explain: bool,
) -> u32 {
    const LABEL: &str = "fallback files";
    const FAIL_AGE: std::time::Duration = std::time::Duration::from_secs(24 * 3600);

    let files = match hippo_core::storage::list_fallback_files(fallback_dir) {
        Ok(f) => f,
        Err(_) => {
            println!("[--] {:<29}  cannot read fallback dir", LABEL);
            return 0;
        }
    };

    if files.is_empty() {
        println!("[OK] {:<29}  none pending", LABEL);
        return 0;
    }

    let now = std::time::SystemTime::now();
    let mut oldest_secs: u64 = 0;
    let mut has_stale = false;

    for path in &files {
        if let Ok(meta) = std::fs::metadata(path)
            && let Ok(modified) = meta.modified()
        {
            let age = now.duration_since(modified).unwrap_or_default();
            if age.as_secs() > oldest_secs {
                oldest_secs = age.as_secs();
            }
            if age > FAIL_AGE {
                has_stale = true;
            }
        }
    }

    if has_stale && daemon_reachable {
        println!(
            "[!!] {:<29}  {} pending, oldest {}h (daemon up — drain broken?)",
            LABEL,
            files.len(),
            oldest_secs / 3600
        );
        if explain {
            println!(
                "     CAUSE:  Fallback files > 24h old while daemon is running — drain path broken"
            );
            println!("     FIX:    Restart daemon: mise run restart");
            println!("     DOC:    docs/capture/operator-runbook.md");
        }
        return 1;
    }

    if has_stale {
        println!(
            "[WW] {:<29}  {} pending, oldest {}h (daemon down — drain pending)",
            LABEL,
            files.len(),
            oldest_secs / 3600
        );
    } else {
        println!(
            "[WW] {:<29}  {} pending (all < 24h old)",
            LABEL,
            files.len()
        );
    }
    0
}

// ─── Check 10: Schema version ────────────────────────────────────────────────

/// Check 10: Daemon DB schema version vs brain's `expected_schema_version`.
///
/// Reuses the `brain_json` already fetched by `print_brain_health_details` —
/// no extra HTTP round-trip.
pub fn check_schema_version(
    db: &rusqlite::Connection,
    brain_json: Option<&serde_json::Value>,
    explain: bool,
) -> u32 {
    const LABEL: &str = "schema version";

    let db_version: i64 = match db.query_row("PRAGMA user_version", [], |row| row.get(0)) {
        Ok(v) => v,
        Err(e) => {
            println!("[!!] {:<29}  DB error: {}", LABEL, e);
            return 1;
        }
    };

    let Some(json) = brain_json else {
        println!(
            "[--] {:<29}  v{} (brain unreachable — cannot compare)",
            LABEL, db_version
        );
        return 0;
    };

    let Some(expected) = json.get("expected_schema_version").and_then(|v| v.as_i64()) else {
        println!(
            "[--] {:<29}  v{} (brain /health missing `expected_schema_version`)",
            LABEL, db_version
        );
        return 0;
    };

    // A daemon version listed in accepted_read_versions is rollback-compatible.
    let accepted: Vec<i64> = json
        .get("accepted_read_versions")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|e| e.as_i64()).collect())
        .unwrap_or_default();

    if db_version == expected || accepted.contains(&db_version) {
        println!("[OK] {:<29}  v{}", LABEL, db_version);
        0
    } else {
        println!(
            "[!!] {:<29}  daemon v{}, brain expects v{}",
            LABEL, db_version, expected
        );
        if explain {
            println!(
                "     CAUSE:  Daemon and brain schema versions diverged — enrichment silently crashes"
            );
            println!("     FIX:    mise run restart  (or rebuild both components after updating)");
            println!("     DOC:    docs/capture/operator-runbook.md");
        }
        1
    }
}

// ─────────────────────────────────────────────────────────────────────────────

fn check_claude_session_hook(config: &HippoConfig) {
    let settings_path = dirs::home_dir()
        .map(|h| h.join(".claude/settings.json"))
        .unwrap_or_default();
    check_claude_session_hook_at(config, &settings_path);
}

fn check_claude_session_hook_at(config: &HippoConfig, settings_path: &std::path::Path) {
    let expected = match expected_claude_session_hook_path(&config.storage.data_dir) {
        Some(path) => path,
        None => {
            println!("[--] Claude session hook check skipped");
            println!(
                "     unable to derive expected hook path from data_dir: {}",
                config.storage.data_dir.display()
            );
            return;
        }
    };

    let content = match std::fs::read_to_string(settings_path) {
        Ok(c) => c,
        Err(_) => {
            println!("[--] Claude settings not found (session hook not configured)");
            println!("     expected: {}", expected.display());
            println!("     Fix: hippo daemon install --force");
            return;
        }
    };

    let json: serde_json::Value = match serde_json::from_str(&content) {
        Ok(v) => v,
        Err(_) => {
            println!("[!!] Claude settings.json is malformed");
            println!("     fix the JSON manually, then rerun: hippo daemon install --force");
            return;
        }
    };

    // Reject structural surprises with a dedicated message. `daemon install` would
    // bail on these too, so suggesting `--force` would be misleading — the user
    // must repair the file by hand.
    if !json.is_object() {
        println!("[!!] Claude settings.json root is not a JSON object");
        println!("     repair the file manually before running hippo daemon install");
        return;
    }
    if let Some(hooks) = json.get("hooks")
        && !hooks.is_object()
    {
        println!("[!!] Claude settings.json `hooks` is not an object");
        println!("     repair the file manually before running hippo daemon install");
        return;
    }
    if let Some(ss) = json.get("hooks").and_then(|h| h.get("SessionStart"))
        && !ss.is_array()
    {
        println!("[!!] Claude settings.json `hooks.SessionStart` is not an array");
        println!("     repair the file manually before running hippo daemon install");
        return;
    }

    // Collect all commands across all SessionStart matchers so a user with multiple
    // hooks configured doesn't get a false mismatch when the hippo hook is present
    // but not the first entry.
    let all_commands: Vec<String> = json
        .get("hooks")
        .and_then(|h| h.get("SessionStart"))
        .and_then(|ss| ss.as_array())
        .into_iter()
        .flatten()
        .filter_map(|entry| entry.get("hooks"))
        .filter_map(|hooks| hooks.as_array())
        .flatten()
        .filter_map(|hook| hook.get("command"))
        .filter_map(|cmd| cmd.as_str().map(String::from))
        .collect();

    // Narrow to hippo hook commands — a user may have multiple (stale + current).
    // If *any* exactly matches expected, the install is correct; only report a
    // mismatch when none match.
    let hippo_cmds: Vec<&String> = all_commands
        .iter()
        .filter(|cmd| cmd.contains("claude-session-hook.sh"))
        .collect();
    let exact_match = hippo_cmds
        .iter()
        .any(|cmd| std::path::Path::new(cmd.as_str()) == expected);

    if exact_match {
        if expected.exists() {
            println!("[OK] Claude session hook configured");
            if hippo_cmds.len() > 1 {
                println!(
                    "     note: {} stale hippo hook entries also present — clean up manually",
                    hippo_cmds.len() - 1
                );
            }
        } else {
            println!(
                "[!!] Claude session hook configured but script missing: {}",
                expected.display()
            );
        }
    } else if let Some(first) = hippo_cmds.first() {
        println!("[!!] Claude session hook path mismatch");
        println!("     configured: {}", first);
        println!("     expected:   {}", expected.display());
        println!("     Fix: hippo daemon install --force");
    } else {
        println!("[--] Claude session hook not configured");
        println!("     expected: {}", expected.display());
        println!("     Fix: hippo daemon install --force");
    }
}

fn expected_claude_session_hook_path(data_dir: &std::path::Path) -> Option<PathBuf> {
    // The brain is installed as a sibling of the hippo data dir, e.g.
    // data_dir = ~/.local/share/hippo  →  brain = ~/.local/share/hippo-brain
    data_dir
        .file_name()
        .map(|_| data_dir.with_file_name("hippo-brain"))
        .map(|brain_dir| brain_dir.join("shell/claude-session-hook.sh"))
}

/// Check that the Firefox extension's compiled dist/ bundle exists and the
/// Native Messaging manifest is installed.
///
/// The extension's `manifest.json` references `dist/background.js` and
/// `dist/content.js`, but `dist/` is gitignored — it must be produced by
/// `mise run build:ext:dist`. If dist/ is missing the extension loads cleanly
/// as a temporary add-on in Firefox but captures nothing (silent no-op).
fn check_firefox_extension() {
    // Native Messaging manifest — the bridge between Firefox and hippo-daemon.
    let nm_manifest = dirs::home_dir().map(|h| {
        h.join("Library/Application Support/Mozilla/NativeMessagingHosts/hippo_daemon.json")
    });
    match nm_manifest {
        Some(path) if path.exists() => println!("[OK] Firefox Native Messaging manifest installed"),
        Some(path) => {
            println!(
                "[!!] Firefox Native Messaging manifest missing: {}",
                path.display()
            );
            println!("     Fix: hippo daemon install --force");
        }
        None => println!("[--] Firefox Native Messaging check skipped (no home dir)"),
    }

    // Extension dist/ files. We locate the repo via the canonical path of the
    // currently running binary — typically `<repo>/target/release/hippo`, with
    // `~/.local/bin/hippo` being a symlink into it. If we can't find the repo
    // layout, skip rather than false-alarm.
    let Some(repo_root) = repo_root_from_current_exe() else {
        println!("[--] Firefox extension dist/ check skipped (could not locate repo root)");
        return;
    };
    check_firefox_extension_dist_at(&repo_root.join("extension/firefox"));
}

fn check_firefox_extension_dist_at(ext_dir: &std::path::Path) {
    if !ext_dir.exists() {
        // Not fatal: release installs won't have the repo extension dir.
        println!(
            "[--] Firefox extension dir not found at {}",
            ext_dir.display()
        );
        return;
    }
    let required = ["dist/background.js", "dist/content.js"];
    let missing: Vec<&str> = required
        .iter()
        .filter(|rel| !ext_dir.join(rel).exists())
        .copied()
        .collect();
    if missing.is_empty() {
        println!(
            "[OK] Firefox extension dist/ built ({})",
            ext_dir.join("dist").display()
        );
    } else {
        println!(
            "[!!] Firefox extension dist/ missing: {}",
            missing.join(", ")
        );
        println!("     The extension loads but captures nothing when dist/ is absent.");
        println!("     Fix: mise run build:ext:dist");
    }
}

/// Walk up from the running binary's canonical path looking for a hippo repo
/// checkout (identified by a top-level `Cargo.toml` and `extension/firefox/`
/// sibling). Returns `None` for release installs where the binary lives
/// outside the source tree.
fn repo_root_from_current_exe() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    // Follow symlinks — ~/.local/bin/hippo typically points into target/release.
    let real = std::fs::canonicalize(&exe).unwrap_or(exe);
    // Climb parents looking for Cargo.toml + extension/firefox/manifest.json.
    let mut cur = real.as_path();
    for _ in 0..6 {
        let parent = cur.parent()?;
        if parent.join("Cargo.toml").exists()
            && parent.join("extension/firefox/manifest.json").exists()
        {
            return Some(parent.to_path_buf());
        }
        cur = parent;
    }
    None
}

pub fn parse_duration_to_since_ms(s: &str) -> Option<i64> {
    let s = s.trim();
    if s.len() < 2 {
        return None;
    }
    let (num_str, unit) = s.split_at(s.len() - 1);
    let num: u64 = num_str.parse().ok()?;
    let ms = match unit {
        "m" => num * 60 * 1000,
        "h" => num * 3600 * 1000,
        "d" => num * 86400 * 1000,
        "w" => num * 7 * 86400 * 1000,
        _ => return None,
    };
    let now = Utc::now().timestamp_millis();
    Some(now - ms as i64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use hippo_core::events::EventPayload;
    use tempfile::tempdir;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::{TcpListener, UnixListener};

    #[test]
    fn is_uuid_stem_accepts_canonical_and_rejects_others() {
        assert!(is_uuid_stem("0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"));
        assert!(is_uuid_stem("ffffffff-ffff-ffff-ffff-ffffffffffff"));
        // Uppercase hex is rejected — Claude Code writes lowercase stems.
        assert!(!is_uuid_stem("0A1B2C3D-4E5F-6071-8293-A4B5C6D7E8F9"));
        // Non-UUID names (workflow journals etc.) are rejected.
        assert!(!is_uuid_stem("workflow-journal"));
        assert!(!is_uuid_stem("not-a-uuid"));
        assert!(!is_uuid_stem(""));
        // Wrong group lengths / non-hex digits.
        assert!(!is_uuid_stem("0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f")); // 11 in last
        assert!(!is_uuid_stem("ghijklmn-4e5f-6071-8293-a4b5c6d7e8f9"));
    }

    #[test]
    fn claude_jsonl_has_turn_requires_assistant_line() {
        let dir = tempdir().unwrap();

        // Only a user turn (no assistant response) — expected-absent, same as a stub.
        let user_only = dir.path().join("a.jsonl");
        std::fs::write(
            &user_only,
            "{\"type\":\"summary\",\"x\":1}\n{\"type\":\"user\",\"message\":{}}\n",
        )
        .unwrap();
        assert!(
            !claude_jsonl_has_turn(&user_only),
            "user-only file must be classified as expected-absent (no assistant turn)"
        );

        // Assistant turn present (compact key).
        let with_assistant = dir.path().join("b.jsonl");
        std::fs::write(
            &with_assistant,
            "{\"type\":\"user\"}\n{\"type\":\"assistant\"}\n",
        )
        .unwrap();
        assert!(claude_jsonl_has_turn(&with_assistant));

        // Assistant turn present (spaced key variant).
        let with_spaced_assistant = dir.path().join("c.jsonl");
        std::fs::write(&with_spaced_assistant, "{\"type\": \"assistant\"}\n").unwrap();
        assert!(claude_jsonl_has_turn(&with_spaced_assistant));

        // 0-turn metadata-only stub.
        let empty_stub = dir.path().join("d.jsonl");
        std::fs::write(
            &empty_stub,
            "{\"type\":\"summary\",\"summary\":\"x\"}\n{\"type\":\"file-history-snapshot\"}\n",
        )
        .unwrap();
        assert!(
            !claude_jsonl_has_turn(&empty_stub),
            "metadata-only stub must report no turn"
        );
    }

    /// Helper: open an in-memory-ish temp DB with the full schema for coverage tests.
    fn coverage_test_db(dir: &std::path::Path) -> rusqlite::Connection {
        hippo_core::storage::open_db(&dir.join("hippo.db")).unwrap()
    }

    fn insert_agentic_row(conn: &rusqlite::Connection, session_id: &str, harness: &str) {
        conn.execute(
            "INSERT INTO agentic_sessions
                 (session_id, harness, segment_index, project_dir, cwd, summary_text,
                  tool_calls_json, user_prompts_json, message_count, source_file,
                  is_subagent, start_time, end_time, created_at)
             VALUES (?1, ?2, 0, 'proj', '/work', 'summary', '[]', '[]', 1,
                     '/x.jsonl', 0, 1, 2, 3)",
            rusqlite::params![session_id, harness],
        )
        .unwrap();
    }

    #[test]
    fn check_claude_session_coverage_flags_missing_but_not_stubs_or_subagents() {
        let dir = tempdir().unwrap();
        let conn = coverage_test_db(dir.path());

        let projects = dir.path().join("projects");
        let proj_a = projects.join("proj-a");
        std::fs::create_dir_all(&proj_a).unwrap();

        // Captured session (UUID stem, has a turn, present in DB).
        let captured = "11111111-1111-1111-1111-111111111111";
        std::fs::write(
            proj_a.join(format!("{captured}.jsonl")),
            "{\"type\":\"user\",\"message\":{}}\n",
        )
        .unwrap();
        insert_agentic_row(&conn, captured, "claude-code");

        // Missing session (UUID stem, has a turn, NOT in DB) -> real gap.
        let missing = "22222222-2222-2222-2222-222222222222";
        std::fs::write(
            proj_a.join(format!("{missing}.jsonl")),
            "{\"type\":\"assistant\",\"message\":{}}\n",
        )
        .unwrap();

        // 0-turn empty stub (UUID stem, no turn, NOT in DB) -> expected absent.
        let stub = "33333333-3333-3333-3333-333333333333";
        std::fs::write(
            proj_a.join(format!("{stub}.jsonl")),
            "{\"type\":\"summary\"}\n",
        )
        .unwrap();

        // Non-UUID workflow journal -> excluded entirely.
        std::fs::write(
            proj_a.join("workflow-journal.jsonl"),
            "{\"type\":\"user\"}\n",
        )
        .unwrap();

        // Subagent journal under /subagents/ -> excluded.
        let subagents = proj_a.join("subagents");
        std::fs::create_dir_all(&subagents).unwrap();
        let sub = "44444444-4444-4444-4444-444444444444";
        std::fs::write(
            subagents.join(format!("{sub}.jsonl")),
            "{\"type\":\"user\"}\n",
        )
        .unwrap();

        // Exactly one real gap (the `missing` session) -> fail count 1.
        let fails = check_claude_session_coverage(&conn, &projects, false);
        assert_eq!(
            fails, 1,
            "only the UUID-named, turn-bearing, uncaptured session is a gap"
        );
    }

    #[test]
    fn check_claude_session_coverage_clean_when_all_captured() {
        let dir = tempdir().unwrap();
        let conn = coverage_test_db(dir.path());
        let projects = dir.path().join("projects");
        std::fs::create_dir_all(&projects).unwrap();
        let id = "55555555-5555-5555-5555-555555555555";
        std::fs::write(
            projects.join(format!("{id}.jsonl")),
            "{\"type\":\"user\"}\n",
        )
        .unwrap();
        insert_agentic_row(&conn, id, "claude-code");
        assert_eq!(check_claude_session_coverage(&conn, &projects, false), 0);
    }

    #[test]
    fn check_cursor_session_coverage_flags_missing_transcript() {
        let dir = tempdir().unwrap();
        let conn = coverage_test_db(dir.path());

        let root = dir.path().join("cursor-projects");
        let transcripts = root.join("slug/agent-transcripts/sess");
        std::fs::create_dir_all(&transcripts).unwrap();

        // Write a transcript whose mtime is old enough to be past the idle
        // window (set mtime far in the past via filetime-free trick: the file
        // is created now, so we instead rely on min_idle_secs = 0 in config).
        let captured = "cap-1";
        std::fs::write(transcripts.join(format!("{captured}.jsonl")), "{}\n").unwrap();
        insert_agentic_row(&conn, captured, "cursor");

        let missing = "miss-1";
        std::fs::write(transcripts.join(format!("{missing}.jsonl")), "{}\n").unwrap();

        let mut config = HippoConfig::default();
        config.cursor.enabled = true;
        config.cursor.session_roots = vec![root];
        config.cursor.min_idle_secs = 0; // treat all files as settled (not in-flight)

        let fails = check_cursor_session_coverage(&config, &conn, false);
        assert_eq!(fails, 1, "the uncaptured transcript is a gap");
    }

    #[test]
    fn check_cursor_session_coverage_disabled_is_skipped() {
        let dir = tempdir().unwrap();
        let conn = coverage_test_db(dir.path());
        let mut config = HippoConfig::default();
        config.cursor.enabled = false;
        assert_eq!(check_cursor_session_coverage(&config, &conn, false), 0);
    }

    #[test]
    fn test_expected_claude_session_hook_path_with_absolute_data_dir() {
        let data_dir = PathBuf::from("/tmp/hippo");
        let expected = PathBuf::from("/tmp/hippo-brain/shell/claude-session-hook.sh");
        assert_eq!(expected_claude_session_hook_path(&data_dir), Some(expected));
    }

    #[test]
    fn test_expected_claude_session_hook_path_with_relative_data_dir() {
        let data_dir = PathBuf::from("workspace/hippo");
        let expected = PathBuf::from("workspace/hippo-brain/shell/claude-session-hook.sh");
        assert_eq!(expected_claude_session_hook_path(&data_dir), Some(expected));
    }

    #[test]
    fn test_expected_claude_session_hook_path_without_data_dir_component_returns_none() {
        let data_dir = std::path::Path::new("/");
        assert_eq!(expected_claude_session_hook_path(data_dir), None);
    }

    #[test]
    fn test_check_firefox_extension_dist_at_reports_missing_files() {
        // Arrange: a fake extension dir with manifest.json but no dist/.
        let tmp = tempdir().unwrap();
        let ext = tmp.path().join("firefox");
        std::fs::create_dir_all(&ext).unwrap();
        std::fs::write(ext.join("manifest.json"), "{}").unwrap();

        // The function prints to stdout; we just assert it doesn't panic and
        // that the logic correctly identifies missing files.
        let missing: Vec<&str> = ["dist/background.js", "dist/content.js"]
            .iter()
            .filter(|rel| !ext.join(rel).exists())
            .copied()
            .collect();
        assert_eq!(missing, vec!["dist/background.js", "dist/content.js"]);

        check_firefox_extension_dist_at(&ext);
    }

    #[test]
    fn test_check_firefox_extension_dist_at_accepts_present_files() {
        let tmp = tempdir().unwrap();
        let ext = tmp.path().join("firefox");
        std::fs::create_dir_all(ext.join("dist")).unwrap();
        std::fs::write(ext.join("dist/background.js"), "// built").unwrap();
        std::fs::write(ext.join("dist/content.js"), "// built").unwrap();

        let missing: Vec<&str> = ["dist/background.js", "dist/content.js"]
            .iter()
            .filter(|rel| !ext.join(rel).exists())
            .copied()
            .collect();
        assert!(missing.is_empty());

        check_firefox_extension_dist_at(&ext);
    }

    #[test]
    fn test_check_firefox_extension_dist_at_handles_missing_ext_dir() {
        // Nonexistent path must not panic — release installs won't have it.
        let tmp = tempdir().unwrap();
        check_firefox_extension_dist_at(&tmp.path().join("does-not-exist"));
    }

    #[tokio::test]
    async fn test_handle_send_event_shell_writes_redacted_fallback_when_daemon_unavailable() {
        let temp = tempdir().unwrap();
        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("data");
        config.storage.config_dir = temp.path().join("config");

        handle_send_event_shell(
            &config,
            "export API_KEY=sk-1234567890abcdef".to_string(),
            0,
            "/tmp".to_string(),
            42,
            None,
            Some("main".to_string()),
            None,
            false,
            None,
            None,
            None,
            None,
        )
        .await
        .unwrap();

        let files = storage::list_fallback_files(&config.fallback_dir()).unwrap();
        assert_eq!(files.len(), 1);

        let content = std::fs::read_to_string(&files[0]).unwrap();
        assert!(content.contains("[REDACTED]"));
        assert!(!content.contains("sk-1234567890abcdef"));

        let envelope: EventEnvelope =
            serde_json::from_str(content.lines().next().unwrap()).unwrap();
        match envelope.payload {
            EventPayload::Shell(shell) => {
                assert!(shell.command.contains("[REDACTED]"));
                assert_eq!(shell.redaction_count, 1);
            }
            other => panic!("expected shell payload, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn test_handle_send_event_shell_uses_custom_redaction_config_for_fallback() {
        let temp = tempdir().unwrap();
        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("data");
        config.storage.config_dir = temp.path().join("config");
        std::fs::create_dir_all(&config.storage.config_dir).unwrap();
        std::fs::write(
            config.redact_path(),
            r#"
[[patterns]]
name = "internal_token"
regex = "internal_[A-Z0-9]{8}"
replacement = "***"
"#,
        )
        .unwrap();

        handle_send_event_shell(
            &config,
            "echo internal_ABCD1234".to_string(),
            0,
            "/tmp".to_string(),
            42,
            None,
            None,
            None,
            false,
            None,
            None,
            None,
            None,
        )
        .await
        .unwrap();

        let files = storage::list_fallback_files(&config.fallback_dir()).unwrap();
        assert_eq!(files.len(), 1);

        let content = std::fs::read_to_string(&files[0]).unwrap();
        assert!(content.contains("***"));
        assert!(!content.contains("internal_ABCD1234"));

        let envelope: EventEnvelope =
            serde_json::from_str(content.lines().next().unwrap()).unwrap();
        match envelope.payload {
            EventPayload::Shell(shell) => {
                assert!(shell.command.contains("***"));
                assert!(!shell.command.contains("internal_ABCD1234"));
            }
            other => panic!("expected shell payload, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn test_handle_send_event_shell_derives_git_repo_from_cwd() {
        let temp = tempdir().unwrap();
        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("data");
        config.storage.config_dir = temp.path().join("config");

        // Create a git repo with a remote whose origin matches the owner/repo shape.
        let repo_dir = temp.path().join("work");
        std::fs::create_dir(&repo_dir).unwrap();
        std::process::Command::new("git")
            .arg("-C")
            .arg(&repo_dir)
            .args(["init", "--quiet", "-b", "main"])
            .status()
            .unwrap();
        std::process::Command::new("git")
            .arg("-C")
            .arg(&repo_dir)
            .args([
                "remote",
                "add",
                "origin",
                "git@github.com:sjcarpenter/hippo.git",
            ])
            .status()
            .unwrap();

        handle_send_event_shell(
            &config,
            "echo hi".to_string(),
            0,
            repo_dir.to_string_lossy().into_owned(),
            10,
            None,
            Some("main".to_string()),
            None,
            false,
            None,
            None,
            None,
            None,
        )
        .await
        .unwrap();

        let files = storage::list_fallback_files(&config.fallback_dir()).unwrap();
        assert_eq!(files.len(), 1);
        let content = std::fs::read_to_string(&files[0]).unwrap();
        let envelope: EventEnvelope =
            serde_json::from_str(content.lines().next().unwrap()).unwrap();
        match envelope.payload {
            EventPayload::Shell(shell) => {
                let gs = shell.git_state.expect("git_state should be populated");
                assert_eq!(gs.repo.as_deref(), Some("sjcarpenter/hippo"));
                assert_eq!(gs.branch.as_deref(), Some("main"));
            }
            other => panic!("expected shell payload, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn test_handle_send_event_shell_prefers_caller_supplied_git_repo() {
        let temp = tempdir().unwrap();
        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("data");
        config.storage.config_dir = temp.path().join("config");

        // /tmp is not in a git repo, but the caller passes an explicit value.
        handle_send_event_shell(
            &config,
            "echo hi".to_string(),
            0,
            "/tmp".to_string(),
            10,
            Some("acme/widget".to_string()),
            None,
            None,
            false,
            None,
            None,
            None,
            None,
        )
        .await
        .unwrap();

        let files = storage::list_fallback_files(&config.fallback_dir()).unwrap();
        assert_eq!(files.len(), 1);
        let content = std::fs::read_to_string(&files[0]).unwrap();
        let envelope: EventEnvelope =
            serde_json::from_str(content.lines().next().unwrap()).unwrap();
        match envelope.payload {
            EventPayload::Shell(shell) => {
                let gs = shell.git_state.expect("git_state should be populated");
                assert_eq!(gs.repo.as_deref(), Some("acme/widget"));
            }
            other => panic!("expected shell payload, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn test_send_request_with_timeout_fails_fast_on_hung_server() {
        let temp = tempdir().unwrap();
        let socket_path = temp.path().join("hung.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let server = tokio::spawn(async move {
            let (_stream, _) = listener.accept().await.unwrap();
            tokio::time::sleep(std::time::Duration::from_secs(30)).await;
        });

        let result = send_request_with_timeout(&socket_path, &DaemonRequest::GetStatus, 50).await;

        server.abort();
        let _ = server.await;

        let err = result.expect_err("hung server should trigger a timeout");
        assert!(
            err.to_string().contains("timed out"),
            "unexpected error: {err:?}"
        );
    }

    #[tokio::test]
    async fn test_doctor_reports_brain_health_details_from_json() {
        let temp = tempdir().unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 1024];
            let _ = stream.read(&mut buf).await.unwrap();
            let body = format!(
                r#"{{"status":"ok","version":"{}","inference_reachable":true,"enrichment_running":true,"db_reachable":true,"queue_depth":3,"queue_failed":1,"last_success_at_ms":123456,"last_error":"model offline"}}"#,
                env!("HIPPO_VERSION_FULL")
            );
            let response = format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).await.unwrap();
        });

        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("data");
        config.storage.config_dir = temp.path().join("config");
        config.brain.port = addr.port();

        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(2))
            .build()
            .unwrap();
        print_brain_health_details(&config, &client).await;

        server.await.unwrap();
    }

    #[test]
    fn test_hook_expected_path_derived_from_data_dir() {
        let mut config = HippoConfig::default();
        config.storage.data_dir = std::path::PathBuf::from("/home/user/.local/share/hippo");
        // parent = /home/user/.local/share → brain dir = .../hippo-brain
        let expected = config
            .storage
            .data_dir
            .parent()
            .map(|p| p.join("hippo-brain/shell/claude-session-hook.sh"))
            .unwrap();
        assert_eq!(
            expected,
            std::path::Path::new(
                "/home/user/.local/share/hippo-brain/shell/claude-session-hook.sh"
            )
        );
    }

    #[test]
    fn test_hook_check_not_configured() {
        let temp = tempdir().unwrap();
        let settings = temp.path().join("settings.json");
        std::fs::write(&settings, r#"{"theme":"dark"}"#).unwrap();

        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("hippo");
        // Should not panic; prints [--] not configured
        check_claude_session_hook_at(&config, &settings);
    }

    #[test]
    fn test_hook_check_mismatch() {
        let temp = tempdir().unwrap();
        let settings = temp.path().join("settings.json");
        let wrong_path = "/wrong/path/claude-session-hook.sh";
        std::fs::write(
            &settings,
            format!(
                r#"{{"hooks":{{"SessionStart":[{{"hooks":[{{"command":"{wrong_path}"}}]}}]}}}}"#
            ),
        )
        .unwrap();

        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("hippo");
        // Should not panic; prints [!!] path mismatch
        check_claude_session_hook_at(&config, &settings);
    }

    #[test]
    fn test_hook_check_multiple_entries_one_exact_match() {
        // User has both a stale hippo hook and a correct one — doctor should
        // treat the install as OK rather than false-report a mismatch.
        let temp = tempdir().unwrap();
        let expected_path = temp.path().join("hippo-brain/shell/claude-session-hook.sh");
        std::fs::create_dir_all(expected_path.parent().unwrap()).unwrap();
        std::fs::write(&expected_path, "#!/bin/bash\n").unwrap();

        let settings = temp.path().join("settings.json");
        let stale = "/old/stale/claude-session-hook.sh";
        std::fs::write(
            &settings,
            format!(
                r#"{{"hooks":{{"SessionStart":[
                    {{"hooks":[{{"command":"{stale}"}}]}},
                    {{"hooks":[{{"command":"{}"}}]}}
                ]}}}}"#,
                expected_path.display()
            ),
        )
        .unwrap();

        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("hippo");
        check_claude_session_hook_at(&config, &settings);
    }

    #[test]
    fn test_hook_check_structural_type_mismatch() {
        // Root is not an object → dedicated manual-repair message, no Fix hint.
        let temp = tempdir().unwrap();
        let settings = temp.path().join("settings.json");
        std::fs::write(&settings, r#"[]"#).unwrap();

        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("hippo");
        check_claude_session_hook_at(&config, &settings);
    }

    #[test]
    fn test_hook_check_match_missing_script() {
        let temp = tempdir().unwrap();
        // Expected path: temp/hippo-brain/shell/claude-session-hook.sh
        // data_dir parent = temp, so expected = temp/hippo-brain/shell/claude-session-hook.sh
        let expected_path = temp.path().join("hippo-brain/shell/claude-session-hook.sh");
        let settings = temp.path().join("settings.json");
        std::fs::write(
            &settings,
            format!(
                r#"{{"hooks":{{"SessionStart":[{{"hooks":[{{"command":"{}"}}]}}]}}}}"#,
                expected_path.display()
            ),
        )
        .unwrap();

        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("hippo");
        // Script doesn't exist on disk → prints [!!] configured but script missing
        check_claude_session_hook_at(&config, &settings);
    }

    #[test]
    fn test_doctor_staleness_check() {
        // Use a real temp-file DB. When P0.1 is merged this will pick up source_health
        // from the full schema. Until then we create the table manually so the
        // staleness logic can be exercised independently.
        let tmp = tempdir().unwrap();
        let db_path = tmp.path().join("hippo.db");
        let conn = hippo_core::storage::open_db(&db_path).unwrap();

        // Create source_health if the migration hasn't run yet (pre-P0.1 schema).
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS source_health (
                source                 TEXT PRIMARY KEY,
                last_event_ts          INTEGER,
                last_success_ts        INTEGER,
                last_error_ts          INTEGER,
                last_error_msg         TEXT,
                consecutive_failures   INTEGER NOT NULL DEFAULT 0,
                events_last_1h         INTEGER NOT NULL DEFAULT 0,
                events_last_24h        INTEGER NOT NULL DEFAULT 0,
                expected_min_per_hour  INTEGER,
                probe_ok               INTEGER,
                probe_lag_ms           INTEGER,
                probe_last_run_ts      INTEGER,
                last_heartbeat_ts      INTEGER,
                updated_at             INTEGER NOT NULL
             );",
        )
        .unwrap();

        // Seed a stale shell row: 1 hour ago (3600s past the 300s FAIL threshold).
        conn.execute(
            "INSERT OR REPLACE INTO source_health \
             (source, last_event_ts, consecutive_failures, updated_at) \
             VALUES ('shell', (unixepoch('now')-3600)*1000, 0, unixepoch('now')*1000)",
            [],
        )
        .unwrap();

        let fail = check_source_staleness(&conn, false);
        assert_eq!(fail, 1, "stale shell row should return fail_count=1");

        // Now seed a fresh shell row (1 second ago) and assert 0 failures.
        conn.execute(
            "INSERT OR REPLACE INTO source_health \
             (source, last_event_ts, consecutive_failures, updated_at) \
             VALUES ('shell', (unixepoch('now')-1)*1000, 0, unixepoch('now')*1000)",
            [],
        )
        .unwrap();

        let fail2 = check_source_staleness(&conn, false);
        assert_eq!(fail2, 0, "fresh shell row should return fail_count=0");
    }

    #[test]
    fn test_source_staleness_thresholds_tolerate_probe_cadence() {
        let shell = source_staleness_thresholds_for("shell");
        assert!(
            shell.warn_secs > 300,
            "shell WARN threshold must be longer than the 5 minute probe interval"
        );
        assert!(
            shell.fail_secs >= 900,
            "shell FAIL threshold should allow multiple missed probe ticks"
        );

        let browser = source_staleness_thresholds_for("browser");
        assert!(
            browser.warn_secs > 300,
            "browser WARN threshold must be longer than the 5 minute probe interval"
        );
        assert!(
            browser.fail_secs >= 900,
            "browser FAIL threshold should allow multiple missed probe ticks"
        );
    }

    #[test]
    fn test_suppressed_source_staleness_is_neutral_notice() {
        let line = format_suppressed_source_staleness_line(
            "agentic-session-opencode events",
            "10h ago",
            "opencode DB idle",
        );

        assert!(
            line.starts_with("[--]"),
            "suppressed idle integrations should not look like actionable warnings: {line}"
        );
        assert!(
            !line.starts_with("[WW]"),
            "suppressed idle integrations should be neutral notices: {line}"
        );
    }

    /// Build `SuppressionSignals` for staleness tests. Defaults to "nothing
    /// suppressible" (no probe state, no recent activity, Codex/Cursor never seen,
    /// all sources enabled); each test overrides only the fields it exercises.
    fn signals(
        recent_claude_session: bool,
        codex_sessions_exist: bool,
        codex_sessions_recent: bool,
        cursor_sessions_exist: bool,
        cursor_sessions_recent: bool,
    ) -> SuppressionSignals {
        SuppressionSignals {
            probe_ok: None,
            firefox_running: false,
            recent_claude_session,
            opencode_db_recent: false,
            // Default all enabled flags to `true` so existing tests keep their
            // meaning — they test idle/not-installed suppression, not disabled-source
            // suppression.
            opencode_enabled: true,
            codex_sessions_exist,
            codex_sessions_recent,
            // Default `false`: existing tests cover idle / not-installed /
            // fresh-but-stale suppression, not the in-flight case. The dedicated
            // in-flight tests set this explicitly via struct update syntax.
            codex_session_in_flight: false,
            codex_enabled: true,
            cursor_sessions_exist,
            cursor_sessions_recent,
            cursor_session_in_flight: false,
            cursor_enabled: true,
        }
    }

    #[test]
    fn test_idle_claude_session_staleness_is_suppressed_before_warn() {
        assert_eq!(
            classify_source_staleness(
                "agentic-session-claude",
                12 * 60,
                signals(false, false, false, false, false),
            ),
            SourceStalenessStatus::Suppressed("no active session"),
            "inactive Claude sessions should not warn just because no new session rows landed"
        );
    }

    #[test]
    fn test_active_claude_session_staleness_still_warns() {
        assert_eq!(
            classify_source_staleness(
                "agentic-session-claude",
                12 * 60,
                signals(true, false, false, false, false),
            ),
            SourceStalenessStatus::Warn,
            "recent Claude JSONL activity should make stale source-health actionable"
        );
    }

    #[test]
    fn test_codex_staleness_suppressed_when_no_sessions_exist() {
        // No rollout files at all → never-installed → suppress.
        assert_eq!(
            classify_source_staleness(
                "agentic-session-codex",
                2 * 3600,
                signals(false, false, false, false, false),
            ),
            SourceStalenessStatus::Suppressed("no Codex sessions found"),
            "machines without any Codex session files should not alarm"
        );
    }

    #[test]
    fn test_codex_staleness_suppressed_when_sessions_exist_but_idle() {
        // Rollout files exist but none changed recently → user just isn't
        // running Codex right now → suppress (mirrors idle opencode).
        assert_eq!(
            classify_source_staleness(
                "agentic-session-codex",
                2 * 3600,
                signals(false, true, false, false, false),
            ),
            SourceStalenessStatus::Suppressed("Codex sessions idle"),
            "previously-used but idle Codex should be suppressed, not alarmed"
        );
    }

    #[test]
    fn test_codex_staleness_alarms_when_files_fresh_but_health_stale() {
        // A rollout file changed recently yet `source_health` is stale: the
        // poller is wedged. This is a genuine ingestion failure and must NOT
        // be suppressed — age past fail_secs (3600) → FAIL.
        assert_eq!(
            classify_source_staleness(
                "agentic-session-codex",
                2 * 3600,
                signals(false, true, true, false, false),
            ),
            SourceStalenessStatus::Fail,
            "fresh rollout files + stale source_health is a real ingestion bug — must alarm"
        );
    }

    #[test]
    fn test_codex_staleness_suppressed_when_session_in_flight() {
        // The fresh rollout file is still being written (modified within
        // `min_idle_secs`), so the poller is *correctly* skipping it to avoid a
        // partial read. `source_health` is therefore stale through no fault of
        // the poller — the file simply hasn't settled. This must NOT alarm even
        // though files are fresh and the row is past fail_secs.
        let sig = SuppressionSignals {
            codex_sessions_exist: true,
            codex_sessions_recent: true,
            codex_session_in_flight: true,
            ..signals(false, false, false, false, false)
        };
        assert_eq!(
            classify_source_staleness("agentic-session-codex", 2 * 3600, sig),
            SourceStalenessStatus::Suppressed("Codex session in-flight (settling)"),
            "an in-flight (still-being-written) Codex session must suppress, not alarm"
        );
    }

    #[test]
    fn test_codex_staleness_warns_when_files_fresh_but_health_briefly_stale() {
        // warn_secs = 300 for agentic-session-codex; 600s is past warn but
        // under fail (3600). Fresh files → not suppressed → WARN.
        assert_eq!(
            classify_source_staleness(
                "agentic-session-codex",
                600,
                signals(false, true, true, false, false),
            ),
            SourceStalenessStatus::Warn,
            "fresh codex files with briefly-stale source-health should warn"
        );
    }

    #[test]
    fn test_cursor_staleness_suppressed_when_no_sessions_exist() {
        assert_eq!(
            classify_source_staleness(
                "agentic-session-cursor",
                2 * 3600,
                signals(false, false, false, false, false),
            ),
            SourceStalenessStatus::Suppressed("no Cursor sessions found"),
        );
    }

    #[test]
    fn test_cursor_staleness_suppressed_when_sessions_exist_but_idle() {
        assert_eq!(
            classify_source_staleness(
                "agentic-session-cursor",
                2 * 3600,
                signals(false, false, false, true, false),
            ),
            SourceStalenessStatus::Suppressed("Cursor sessions idle"),
        );
    }

    #[test]
    fn test_cursor_staleness_alarms_when_files_fresh_but_health_stale() {
        assert_eq!(
            classify_source_staleness(
                "agentic-session-cursor",
                2 * 3600,
                signals(false, false, false, true, true),
            ),
            SourceStalenessStatus::Fail,
        );
    }

    /// Finding 1 regression test: a disabled source (cursor_enabled=false) must
    /// suppress the staleness alarm even when transcript files exist on disk.
    /// Without this fix `cursor_sessions_recent = true` would reach the Fail arm.
    #[test]
    fn test_cursor_disabled_suppresses_staleness() {
        let sig = SuppressionSignals {
            cursor_enabled: false,
            // files exist AND are recent — would alarm if the enabled check is absent
            cursor_sessions_exist: true,
            cursor_sessions_recent: true,
            ..signals(false, false, false, false, false)
        };
        assert_eq!(
            classify_source_staleness("agentic-session-cursor", 2 * 3600, sig),
            SourceStalenessStatus::Suppressed("source disabled"),
            "cursor with enabled=false must be suppressed regardless of file activity"
        );
    }

    #[test]
    fn test_cursor_staleness_suppressed_when_session_in_flight() {
        // Symmetric with codex: an actively-written Cursor transcript is skipped
        // by the poller's `min_idle_secs` settle gate, so a stale `source_health`
        // row is expected — suppress, don't alarm.
        let sig = SuppressionSignals {
            cursor_sessions_exist: true,
            cursor_sessions_recent: true,
            cursor_session_in_flight: true,
            ..signals(false, false, false, false, false)
        };
        assert_eq!(
            classify_source_staleness("agentic-session-cursor", 2 * 3600, sig),
            SourceStalenessStatus::Suppressed("Cursor session in-flight (settling)"),
            "an in-flight (still-being-written) Cursor session must suppress, not alarm"
        );
    }

    #[test]
    fn test_in_flight_takes_precedence_over_disabled_is_false() {
        // Ordering guard: a *disabled* source must still win over in-flight, so
        // that `enabled = false` is always honoured even mid-session.
        let sig = SuppressionSignals {
            codex_enabled: false,
            codex_sessions_exist: true,
            codex_sessions_recent: true,
            codex_session_in_flight: true,
            ..signals(false, false, false, false, false)
        };
        assert_eq!(
            classify_source_staleness("agentic-session-codex", 2 * 3600, sig),
            SourceStalenessStatus::Suppressed("source disabled"),
            "disabled must take precedence over in-flight"
        );
    }

    /// Same class fix — disabled Codex must suppress regardless of file activity.
    #[test]
    fn test_codex_disabled_suppresses_staleness() {
        let sig = SuppressionSignals {
            codex_enabled: false,
            codex_sessions_exist: true,
            codex_sessions_recent: true,
            ..signals(false, false, false, false, false)
        };
        assert_eq!(
            classify_source_staleness("agentic-session-codex", 2 * 3600, sig),
            SourceStalenessStatus::Suppressed("source disabled"),
            "codex with enabled=false must be suppressed regardless of file activity"
        );
    }

    /// Same class fix — disabled opencode must suppress regardless of DB mtime.
    #[test]
    fn test_opencode_disabled_suppresses_staleness() {
        let sig = SuppressionSignals {
            opencode_enabled: false,
            opencode_db_recent: true, // DB looks active — would alarm if check absent
            ..signals(false, false, false, false, false)
        };
        assert_eq!(
            classify_source_staleness("agentic-session-opencode", 2 * 3600, sig),
            SourceStalenessStatus::Suppressed("source disabled"),
            "opencode with enabled=false must be suppressed regardless of DB mtime"
        );
    }

    #[test]
    fn test_watchdog_heartbeat_status_tolerates_launchd_jitter() {
        assert_eq!(
            watchdog_heartbeat_status(74),
            WatchdogHeartbeatStatus::Ok,
            "launchd StartInterval=60 can drift past one minute without an outage"
        );
        assert_eq!(
            watchdog_heartbeat_status(121),
            WatchdogHeartbeatStatus::Warn
        );
        assert_eq!(
            watchdog_heartbeat_status(180),
            WatchdogHeartbeatStatus::Fail
        );
    }

    #[test]
    fn test_check_github_source_disabled_is_info_only() {
        let config = HippoConfig::default();
        assert!(!config.github.enabled, "default must be disabled");
        // Disabled → opt-in feature, should not fail doctor.
        // Resolver is never called when disabled — pass a trivial stub.
        assert_eq!(check_github_source_with(&config, || false), 0);
    }

    #[test]
    fn test_check_github_source_enabled_without_token_fails() {
        let mut config = HippoConfig::default();
        config.github.enabled = true;
        config.github.watched_repos = vec!["owner/repo".to_string()];
        // Inject "token missing" without touching process env.
        // Token missing AND (probably) plist missing in test env → at least 1.
        assert!(check_github_source_with(&config, || false) >= 1);
    }

    #[test]
    fn test_check_github_source_enabled_empty_repos_fails() {
        let mut config = HippoConfig::default();
        config.github.enabled = true;
        // Inject "token present" so we isolate the empty-repos check.
        // watched_repos stays empty.
        let fail = check_github_source_with(&config, || true);
        // At least the empty-repos fail (maybe also plist-not-installed in CI).
        assert!(fail >= 1);
    }

    #[test]
    fn test_check_brain_telemetry_status_no_brain() {
        // Brain unreachable → no info to report → no fail.
        assert_eq!(check_brain_telemetry_status(None), 0);
    }

    #[test]
    fn test_check_brain_telemetry_status_disabled() {
        // telemetry_enabled=false → no fail regardless of active state.
        let json = serde_json::json!({
            "telemetry_enabled": false,
            "telemetry_active": false,
        });
        assert_eq!(check_brain_telemetry_status(Some(&json)), 0);
    }

    #[test]
    fn test_check_brain_telemetry_status_active() {
        let json = serde_json::json!({
            "telemetry_enabled": true,
            "telemetry_active": true,
        });
        assert_eq!(check_brain_telemetry_status(Some(&json)), 0);
    }

    #[test]
    fn test_check_brain_telemetry_status_enabled_but_inactive_fails() {
        // The exact failure mode from the 2026-04-26 dashboard outage:
        // brain alive, env says telemetry on, but the venv was out of sync
        // and providers never initialized.
        let json = serde_json::json!({
            "telemetry_enabled": true,
            "telemetry_active": false,
        });
        assert_eq!(check_brain_telemetry_status(Some(&json)), 1);
    }

    #[test]
    fn test_check_brain_telemetry_status_older_brain_unknown() {
        // Older brain that doesn't yet expose telemetry_{enabled,active}.
        // Don't fail — the daemon-side collector check above is a close-enough
        // proxy until the brain is upgraded.
        let json = serde_json::json!({
            "status": "ok",
            "version": "0.16.0",
        });
        assert_eq!(check_brain_telemetry_status(Some(&json)), 0);
    }

    // ─── Agentic-unification repoint: doctor must read agentic_sessions, ────────
    //     not the frozen claude_sessions table (v17→v18 unification debt).

    #[test]
    fn check_claude_session_db_finds_session_in_agentic_sessions() {
        let dir = tempdir().unwrap();
        let conn = coverage_test_db(dir.path());

        // Active JSONL whose stem is the session_id.
        let projects = dir.path().join("projects");
        std::fs::create_dir_all(&projects).unwrap();
        let session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
        std::fs::write(
            projects.join(format!("{session_id}.jsonl")),
            "{\"type\":\"user\"}\n",
        )
        .unwrap();

        // Post-unification the session lives in agentic_sessions (harness =
        // 'claude-code'), NOT the frozen claude_sessions table.
        insert_agentic_row(&conn, session_id, "claude-code");

        assert_eq!(
            check_claude_session_db(&projects, dir.path(), &conn, false),
            0,
            "active session present in agentic_sessions must not be reported missing"
        );
    }

    #[test]
    fn check_session_hook_log_counts_agentic_sessions_rows() {
        let dir = tempdir().unwrap();
        let conn = coverage_test_db(dir.path());

        // Three "hook invoked" entries within the past hour.
        let now = chrono::Utc::now();
        let log = dir.path().join("session-hook-debug.log");
        std::fs::write(
            &log,
            format!("{} hook invoked\n", now.to_rfc3339()).repeat(3),
        )
        .unwrap();

        // A claude-code session recorded this hour lives in agentic_sessions.
        let now_ms = now.timestamp_millis();
        conn.execute(
            "INSERT INTO agentic_sessions
                 (session_id, harness, segment_index, project_dir, cwd, summary_text,
                  tool_calls_json, user_prompts_json, message_count, source_file,
                  is_subagent, start_time, end_time, created_at)
             VALUES ('s1','claude-code',0,'p','/w','sum','[]','[]',1,'/x.jsonl',0,?1,?1,?1)",
            rusqlite::params![now_ms],
        )
        .unwrap();

        // invocations > 0 AND db_rows > 0 → OK (0), not a false [!!] FAIL.
        assert_eq!(
            check_session_hook_log(&log, dir.path(), &conn, false),
            0,
            "hook invocations matched by agentic_sessions rows must report OK"
        );
    }

    #[test]
    fn source_freshness_probes_never_read_frozen_claude_sessions() {
        for probe in source_freshness_probes() {
            assert!(
                !probe.query.contains("claude_sessions"),
                "freshness probe {:?} still reads the frozen claude_sessions table; \
                 repoint it to agentic_sessions (harness-keyed)",
                probe.name
            );
        }
    }
}
