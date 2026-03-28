use anyhow::Result;
use chrono::Utc;
use hippo_core::config::{ENV_ALLOWLIST, HippoConfig};
use hippo_core::events::{EventEnvelope, EventPayload, GitState, ShellEvent, ShellKind};
use hippo_core::protocol::{DaemonRequest, DaemonResponse};
use hippo_core::redaction::RedactionEngine;
use hippo_core::storage;
use std::collections::HashMap;
use std::path::PathBuf;
use tokio::net::UnixStream;
use uuid::Uuid;

use crate::framing::{read_frame, write_frame};

pub async fn send_request(
    socket_path: &std::path::Path,
    request: &DaemonRequest,
) -> Result<DaemonResponse> {
    let mut stream = UnixStream::connect(socket_path).await?;
    let json = serde_json::to_vec(request)?;
    write_frame(&mut stream, &json).await?;
    let frame = read_frame(&mut stream)
        .await?
        .ok_or_else(|| anyhow::anyhow!("no response from daemon"))?;
    let response: DaemonResponse = serde_json::from_slice(&frame)?;
    Ok(response)
}

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

fn redacted_fallback_envelope(envelope: &EventEnvelope) -> EventEnvelope {
    let EventPayload::Shell(shell) = &envelope.payload else {
        return envelope.clone();
    };

    let redaction = RedactionEngine::builtin();
    let mut redacted_shell = shell.clone();
    let command = redaction.redact(&redacted_shell.command);
    redacted_shell.command = command.text;
    redacted_shell.redaction_count = command.count;
    redacted_shell.env_snapshot = redacted_shell
        .env_snapshot
        .iter()
        .filter(|(key, _)| ENV_ALLOWLIST.contains(&key.as_str()))
        .map(|(key, value)| {
            let redacted = redaction.redact(value);
            (key.clone(), redacted.text)
        })
        .collect();

    EventEnvelope {
        envelope_id: envelope.envelope_id,
        producer_version: envelope.producer_version,
        timestamp: envelope.timestamp,
        payload: EventPayload::Shell(redacted_shell),
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
        stdout: None,
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
            let fallback = redacted_fallback_envelope(&envelope);
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

pub fn handle_redact_test(input: &str) {
    let engine = RedactionEngine::builtin();
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
    println!("Hippo Doctor");
    println!("============");

    // Check daemon socket
    let socket = config.socket_path();
    if socket.exists() {
        match send_request(&socket, &DaemonRequest::GetStatus).await {
            Ok(DaemonResponse::Status(_)) => println!("[OK] Daemon is running"),
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
    let brain_url = format!("http://localhost:{}/health", config.brain.port);
    match reqwest::get(&brain_url).await {
        Ok(r) if r.status().is_success() => println!("[OK] Brain server reachable"),
        _ => println!(
            "[!!] Brain server not reachable on port {}",
            config.brain.port
        ),
    }

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

    Ok(())
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
}
