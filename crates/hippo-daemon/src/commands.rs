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
    if socket.exists() {
        match send_request(&socket, &DaemonRequest::GetStatus).await {
            Ok(DaemonResponse::Status(status)) => {
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
    } else {
        println!("[--] No config file (using defaults)");
    }

    // Check LM Studio
    let lm_url = format!("{}/models", config.lmstudio.base_url);
    match client.get(&lm_url).send().await {
        Ok(r) if r.status().is_success() => println!("[OK] LM Studio reachable"),
        _ => {
            println!(
                "[!!] LM Studio not reachable at {}",
                config.lmstudio.base_url
            );
            fail_count += 1;
        }
    }

    // Check brain
    print_brain_health_details(config, &client).await;

    // Check fallback files
    let fallback_files = storage::list_fallback_files(&config.fallback_dir())
        .map(|f| f.len())
        .unwrap_or(0);
    if fallback_files > 0 {
        println!("[!!] {} fallback files pending recovery", fallback_files);
        fail_count += 1;
    } else {
        println!("[OK] No fallback files pending");
    }

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

    // Check OpenTelemetry configuration
    fail_count += check_otel_status(config, &client).await;

    // Check GitHub CI-ingest configuration
    fail_count += check_github_source(config);

    // Check 1: Per-source staleness via source_health table (P0.1)
    // Check 8: Watchdog heartbeat
    if db_path.exists()
        && let Ok(conn) = hippo_core::storage::open_db(&db_path)
    {
        fail_count += check_source_staleness(&conn, explain);
        fail_count += check_watchdog_heartbeat(&conn, explain);
    }

    // Check 4: zsh hook sourced
    fail_count += check_zsh_hook_sourced(explain);

    // Check 7: Log file sizes
    fail_count += check_log_file_sizes(config, explain);

    if fail_count > 0 {
        std::process::exit(fail_count as i32);
    }

    Ok(())
}

/// Per-source capture-freshness doctor check.
///
/// Emits one line per source, color-coded by how long since the freshest
/// row (staleness threshold per source — see
/// `docs/capture-reliability/10-source-audit.md`). Queries the underlying
/// tables directly so it works without the `source_health` table (which
/// is still a P0.1 roadmap item).
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

async fn check_otel_status(config: &HippoConfig, client: &reqwest::Client) -> u32 {
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

    // Determine status and provide actionable feedback
    match (config_enabled, collector_reachable) {
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
    if !config.github.enabled {
        println!(
            "[--] GitHub CI ingest: disabled (set [github] enabled = true in {} to enable)",
            config.storage.config_dir.join("config.toml").display()
        );
        return 0;
    }

    let mut fail = 0u32;

    // Token must be readable at doctor time — same gate as install.
    if std::env::var(&config.github.token_env).is_err() {
        println!(
            "[!!] GitHub CI ingest: enabled but {} is not set",
            config.github.token_env
        );
        println!(
            "     Create a PAT with repo + actions:read scopes and export {}",
            config.github.token_env
        );
        fail += 1;
    }

    if config.github.watched_repos.is_empty() {
        println!("[!!] GitHub CI ingest: enabled but [github] watched_repos is empty");
        println!("     Add at least one repo, e.g.  watched_repos = [\"owner/name\"]");
        fail += 1;
    }

    let plist_path = dirs::home_dir()
        .map(|h| h.join("Library/LaunchAgents/com.hippo.gh-poll.plist"))
        .unwrap_or_default();
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
         WHERE source IN ('shell', 'browser', 'claude-session', 'claude-tool') \
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

    struct Thresholds {
        warn_secs: i64,
        fail_secs: i64,
    }

    let thresholds_for = |source: &str| -> Thresholds {
        match source {
            "shell" => Thresholds {
                warn_secs: 60,
                fail_secs: 300,
            },
            "claude-session" => Thresholds {
                warn_secs: 300,
                fail_secs: 1800,
            },
            "claude-tool" => Thresholds {
                warn_secs: 300,
                fail_secs: 600,
            },
            "browser" => Thresholds {
                warn_secs: 120,
                fail_secs: 600,
            },
            _ => Thresholds {
                warn_secs: 300,
                fail_secs: 1800,
            },
        }
    };

    // Check Firefox running (for browser suppression).
    // macOS Firefox (incl. Developer Edition) exposes the main process as `firefox`;
    // `firefox-bin` is Linux-only. Match either to keep the check portable.
    let firefox_running = || -> bool {
        ["firefox", "firefox-bin"].iter().any(|name| {
            std::process::Command::new("pgrep")
                .args(["-x", name])
                .status()
                .map(|s| s.success())
                .unwrap_or(false)
        })
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

    // All four expected sources — report missing ones too.
    let all_sources = ["browser", "claude-session", "claude-tool", "shell"];
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
        let thresh = thresholds_for(source);

        if age_secs < thresh.warn_secs {
            println!("[OK] {}  {}", padded, human);
        } else if age_secs < thresh.fail_secs {
            println!("[WW] {}  {} (WARN)", padded, human);
        } else {
            // Check suppression conditions.
            let suppressed = match source {
                "shell" => row.probe_ok == Some(0),
                "claude-session" => !recent_claude_session(),
                "claude-tool" => row.probe_ok == Some(0),
                "browser" => !firefox_running(),
                _ => false,
            };

            if suppressed {
                let reason = match source {
                    "browser" => "no active Firefox session",
                    "claude-session" => "no active session",
                    _ => "probe disabled",
                };
                println!("[WW] {}  {} (suppressed — {})", padded, human, reason);
            } else {
                println!("[!!] {}  {} (FAIL)", padded, human);
                fail_count += 1;
                if explain {
                    println!("     CAUSE:  No events have landed in SQLite for this source");
                    println!(
                        "     FIX:    Check source is running: hippo doctor (re-run); tail -f ~/.local/share/hippo/daemon.stderr.log"
                    );
                    println!("     DOC:    docs/capture-reliability/02-invariants.md");
                }
            }
        }
    }

    fail_count
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

    let candidates = [
        home.join(".zshrc"),
        home.join(".zshenv"),
        home.join(".config/zsh/.zshrc"),
        home.join(".config/zsh/.zshenv"),
    ];

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
                    let expanded = if let Some(rest) = p.strip_prefix("~/") {
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
        println!("     DOC:    docs/capture-reliability/08-anti-patterns.md");
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
            println!("     DOC:    docs/capture-reliability/07-roadmap.md");
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
                println!("     DOC:    docs/capture-reliability/03-doctor-upgrades.md");
            }
            1
        }
        Ok(age_secs) => {
            if age_secs < 60 {
                println!("[OK] {:<29}  {}s ago", "watchdog heartbeat", age_secs);
                0
            } else if age_secs < 180 {
                println!(
                    "[WW] {:<29}  {}s ago (WARN, expected < 60s)",
                    "watchdog heartbeat", age_secs
                );
                0
            } else {
                println!(
                    "[!!] {:<29}  stale {}s ago (FAIL)",
                    "watchdog heartbeat", age_secs
                );
                if explain {
                    println!("     CAUSE:  Watchdog has stopped sending heartbeats");
                    println!("     FIX:    Restart the watchdog service: mise run restart");
                    println!("     DOC:    docs/capture-reliability/07-roadmap.md");
                }
                1
            }
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
    fn test_check_github_source_disabled_is_info_only() {
        let config = HippoConfig::default();
        assert!(!config.github.enabled, "default must be disabled");
        // Disabled → opt-in feature, should not fail doctor.
        assert_eq!(check_github_source(&config), 0);
    }

    #[test]
    fn test_check_github_source_enabled_without_token_fails() {
        // SAFETY: this test mutates process env. Other tests in this file
        // don't touch HIPPO_GITHUB_TOKEN_DOES_NOT_EXIST_12345, so no race.
        let mut config = HippoConfig::default();
        config.github.enabled = true;
        config.github.token_env = "HIPPO_GITHUB_TOKEN_DOES_NOT_EXIST_12345".to_string();
        config.github.watched_repos = vec!["owner/repo".to_string()];
        // Empty watched_repos would add another fail; we want to isolate token.
        unsafe {
            std::env::remove_var("HIPPO_GITHUB_TOKEN_DOES_NOT_EXIST_12345");
        }
        // Token missing AND (probably) plist missing in test env → at least 1.
        assert!(check_github_source(&config) >= 1);
    }

    #[test]
    fn test_check_github_source_enabled_empty_repos_fails() {
        let mut config = HippoConfig::default();
        config.github.enabled = true;
        // Set a token so we isolate the empty-repos check.
        unsafe {
            std::env::set_var("HIPPO_GITHUB_TOKEN_TEST_REPOS", "dummy");
        }
        config.github.token_env = "HIPPO_GITHUB_TOKEN_TEST_REPOS".to_string();
        // watched_repos stays empty.
        let fail = check_github_source(&config);
        unsafe {
            std::env::remove_var("HIPPO_GITHUB_TOKEN_TEST_REPOS");
        }
        // At least the empty-repos fail (maybe also plist-not-installed in CI).
        assert!(fail >= 1);
    }
}
