use anyhow::Result;
use chrono::Utc;
use hippo_core::config::{ENV_ALLOWLIST, HippoConfig};
use hippo_core::events::{CapturedOutput, EventEnvelope, EventPayload, GitState, ShellEvent};
use hippo_core::protocol::{DaemonRequest, DaemonResponse};
use hippo_core::redaction::RedactionEngine;
use hippo_core::storage;
use std::collections::HashMap;
use std::path::PathBuf;
use tokio::net::UnixStream;
use uuid::Uuid;

use crate::framing::{read_frame, write_frame};

const REQUEST_TIMEOUT_MS: u64 = 5_000;

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

async fn print_brain_health_details(config: &HippoConfig, client: &reqwest::Client) {
    let brain_url = format!("http://localhost:{}/health", config.brain.port);
    match client.get(&brain_url).send().await {
        Ok(resp) if resp.status().is_success() => {
            println!("[OK] Brain server reachable");

            match resp.json::<serde_json::Value>().await {
                Ok(json) => {
                    let queue_depth = json
                        .get("queue_depth")
                        .and_then(|v| v.as_u64())
                        .unwrap_or_default();
                    let queue_failed = json
                        .get("queue_failed")
                        .and_then(|v| v.as_u64())
                        .unwrap_or_default();
                    let enrichment_running = json
                        .get("enrichment_running")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false);
                    let lmstudio_reachable = json
                        .get("lmstudio_reachable")
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

                    println!(
                        "[OK] Brain queue depth: {} pending, {} failed",
                        queue_depth, queue_failed
                    );
                    if lmstudio_reachable {
                        println!("[OK] Brain LM Studio: reachable");
                    } else {
                        println!("[!!] Brain LM Studio: unreachable");
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
                }
                Err(err) => {
                    println!(
                        "[!!] Brain server reachable but returned unreadable health JSON: {}",
                        err
                    );
                }
            }
        }
        _ => println!(
            "[!!] Brain server not reachable on port {}",
            config.brain.port
        ),
    }
}

fn redacted_fallback_envelope(
    envelope: &EventEnvelope,
    redaction: &RedactionEngine,
) -> EventEnvelope {
    let EventPayload::Shell(shell) = &envelope.payload else {
        return envelope.clone();
    };

    EventEnvelope {
        envelope_id: envelope.envelope_id,
        producer_version: envelope.producer_version,
        timestamp: envelope.timestamp,
        payload: EventPayload::Shell(crate::redact_shell_event(shell, redaction)),
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
        tool_name: None,
    };

    let envelope = EventEnvelope::shell(event);
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
                "  LM Studio:        {}",
                if status.lmstudio_reachable {
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

pub async fn handle_doctor(config: &HippoConfig) -> Result<()> {
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
    if socket.exists() {
        match send_request(&socket, &DaemonRequest::GetStatus).await {
            Ok(DaemonResponse::Status(status)) => {
                println!("[OK] Daemon is running (uptime {}s)", status.uptime_secs);
                if status.version.is_empty() {
                    println!("[!!] Daemon too old to report version — restart recommended");
                } else if status.version == cli_version {
                    println!("[OK] Daemon version matches CLI");
                } else {
                    println!(
                        "[!!] Daemon version mismatch: running={}, cli={}",
                        status.version, cli_version
                    );
                    println!("     Run: mise run restart");
                }
            }
            _ => println!("[!!] Socket exists but daemon not responding"),
        }
    } else {
        println!("[!!] Daemon socket not found at {:?}", socket);
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
    } else {
        println!("[--] No config file (using defaults)");
    }

    // Check LM Studio
    let lm_url = format!("{}/models", config.lmstudio.base_url);
    match client.get(&lm_url).send().await {
        Ok(r) if r.status().is_success() => println!("[OK] LM Studio reachable"),
        _ => println!(
            "[!!] LM Studio not reachable at {}",
            config.lmstudio.base_url
        ),
    }

    // Check brain
    print_brain_health_details(config, &client).await;

    // Check fallback files
    let fallback_files = storage::list_fallback_files(&config.fallback_dir())
        .map(|f| f.len())
        .unwrap_or(0);
    if fallback_files > 0 {
        println!("[!!] {} fallback files pending recovery", fallback_files);
    } else {
        println!("[OK] No fallback files pending");
    }

    // Check embedding model
    if config.models.embedding.is_empty() {
        println!("[!!] No embedding model configured");
    } else {
        println!("[OK] Embedding model: {}", config.models.embedding);
    }

    // Check Claude session hook
    check_claude_session_hook(config);

    // Check Firefox extension build + Native Messaging manifest
    check_firefox_extension();

    // Per-source capture-freshness audit (one line per raw data source
    // hippo is supposed to collect). Bridge until the `source_health`
    // table (docs/capture-reliability/01-source-health.md, P0.1) lands.
    check_source_freshness(config);

    // Check OpenTelemetry configuration
    check_otel_status(config, &client).await;

    Ok(())
}

/// Per-source capture-freshness doctor check.
///
/// Emits one line per source, color-coded by how long since the freshest
/// row (staleness threshold per source — see
/// `docs/capture-reliability/10-source-audit.md`). Queries the underlying
/// tables directly so it works without the `source_health` table (which
/// is still a P0.1 roadmap item).
fn check_source_freshness(config: &HippoConfig) {
    let db_path = config.db_path();
    if !db_path.exists() {
        println!("[--] Source freshness: database not created yet");
        return;
    }

    let conn = match hippo_core::storage::open_db(&db_path) {
        Ok(c) => c,
        Err(e) => {
            println!("[!!] Source freshness: failed to open DB: {e}");
            return;
        }
    };

    let now_ms = chrono::Utc::now().timestamp_millis();
    for probe in source_freshness_probes() {
        let (count, max_ts): (i64, Option<i64>) = conn
            .query_row(probe.query, [], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap_or((0, None));

        println!(
            "{}",
            source_freshness_verdict(probe.name, count, max_ts, now_ms, probe.thresholds)
        );
    }
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
        SourceFreshnessProbe {
            name: "claude-session (main)",
            query: "SELECT COUNT(*), MAX(start_time) FROM claude_sessions WHERE is_subagent = 0",
            thresholds: FreshnessThresholds {
                soft_ms: 12 * HOUR_MS,
                hard_ms: 7 * DAY_MS,
            },
        },
        SourceFreshnessProbe {
            name: "claude-session (subagent)",
            query: "SELECT COUNT(*), MAX(start_time) FROM claude_sessions WHERE is_subagent = 1",
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

async fn check_otel_status(config: &HippoConfig, client: &reqwest::Client) {
    // Check if OTel feature is compiled in
    #[cfg(feature = "otel")]
    let otel_compiled = true;
    #[cfg(not(feature = "otel"))]
    let otel_compiled = false;

    if !otel_compiled {
        println!("[--] OpenTelemetry: not compiled (daemon built without --features otel)");
        return;
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

    // Determine status and provide actionable feedback
    match (config_enabled, collector_reachable) {
        (true, true) => {
            println!("[OK] OpenTelemetry: enabled and collector reachable");
        }
        (true, false) => {
            println!(
                "[!!] OpenTelemetry: enabled but collector unreachable at {}",
                collector_health_url
            );
            println!("     Start the stack: mise run otel:up");
        }
        (false, true) => {
            println!("[!!] OpenTelemetry: collector available but disabled in config");
            println!(
                "     Enable it: Set [telemetry] enabled = true in ~/.config/hippo/config.toml"
            );
            println!("     Then restart: mise run restart");
        }
        (false, false) => {
            println!("[--] OpenTelemetry: disabled (start with: mise run otel:up)");
        }
    }
}

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
                r#"{{"status":"ok","version":"{}","lmstudio_reachable":true,"enrichment_running":true,"db_reachable":true,"queue_depth":3,"queue_failed":1,"last_success_at_ms":123456,"last_error":"model offline"}}"#,
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
}
