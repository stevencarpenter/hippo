use anyhow::Result;
use chrono::Utc;
use hippo_core::config::{ENV_ALLOWLIST, HippoConfig};
use hippo_core::events::{
    CapturedOutput, EventEnvelope, EventPayload, GitState, ShellEvent, ShellKind,
};
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

async fn print_brain_health_details(config: &HippoConfig) {
    let brain_url = format!("http://localhost:{}/health", config.brain.port);
    match reqwest::get(&brain_url).await {
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

    let git_state = if git_branch.is_some() || git_commit.is_some() {
        Some(GitState {
            repo: None,
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
        cwd: PathBuf::from(cwd),
        hostname,
        shell: ShellKind::Zsh,
        stdout: output.as_ref().map(|o| CapturedOutput {
            content: o.clone(),
            truncated: false,
            original_bytes: o.len(),
        }),
        stderr: None,
        env_snapshot,
        git_state,
        redaction_count: 0,
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
    match reqwest::get(&lm_url).await {
        Ok(r) if r.status().is_success() => println!("[OK] LM Studio reachable"),
        _ => println!(
            "[!!] LM Studio not reachable at {}",
            config.lmstudio.base_url
        ),
    }

    // Check brain
    print_brain_health_details(config).await;

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
    check_claude_session_hook();

    Ok(())
}

fn check_claude_session_hook() {
    let expected = env!("HIPPO_SESSION_HOOK_PATH");
    let settings_path = dirs::home_dir()
        .map(|h| h.join(".claude/settings.json"))
        .unwrap_or_default();

    let content = match std::fs::read_to_string(&settings_path) {
        Ok(c) => c,
        Err(_) => {
            println!("[--] Claude settings not found (session hook not configured)");
            return;
        }
    };

    let json: serde_json::Value = match serde_json::from_str(&content) {
        Ok(v) => v,
        Err(_) => {
            println!("[!!] Claude settings.json is malformed");
            return;
        }
    };

    // Navigate: hooks -> SessionStart -> [].hooks -> [].command
    let configured_cmd = json
        .get("hooks")
        .and_then(|h| h.get("SessionStart"))
        .and_then(|ss| ss.as_array())
        .into_iter()
        .flatten()
        .filter_map(|entry| entry.get("hooks"))
        .filter_map(|hooks| hooks.as_array())
        .flatten()
        .filter_map(|hook| hook.get("command"))
        .find_map(|cmd| cmd.as_str().map(String::from));

    match configured_cmd {
        Some(cmd) if cmd == expected => {
            if std::path::Path::new(expected).exists() {
                println!("[OK] Claude session hook configured");
            } else {
                println!(
                    "[!!] Claude session hook configured but script missing: {}",
                    expected
                );
            }
        }
        Some(cmd) => {
            println!("[!!] Claude session hook path mismatch");
            println!("     configured: {}", cmd);
            println!("     expected:   {}", expected);
        }
        None => {
            println!("[--] Claude session hook not configured");
            println!("     expected: {}", expected);
        }
    }
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

        print_brain_health_details(&config).await;

        server.await.unwrap();
    }
}
