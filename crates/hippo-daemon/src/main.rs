mod cli;
mod install;

use hippo_daemon::{claude_session, commands, daemon};

use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;
use cli::{
    BrainAction, Cli, Commands, ConfigAction, DaemonAction, IngestSource, RedactAction,
    SendEventSource,
};
use hippo_core::config::HippoConfig;
use tracing_subscriber::EnvFilter;

async fn poll_socket_removal(socket: &std::path::Path, timeout: std::time::Duration) -> bool {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        if !socket.exists() {
            return true;
        }
        if tokio::time::Instant::now() >= deadline {
            return false;
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    let cli = Cli::parse();
    let config = match HippoConfig::load_default() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Warning: failed to load config: {e:#}. Using defaults.");
            HippoConfig::default()
        }
    };

    match cli.command {
        Commands::Daemon { action } => match action {
            DaemonAction::Run => {
                daemon::run(config).await?;
            }
            DaemonAction::Start => {
                println!(
                    "Use: launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hippo.daemon.plist"
                );
            }
            DaemonAction::Stop => {
                let socket = config.socket_path();
                match commands::send_request(
                    &socket,
                    &hippo_core::protocol::DaemonRequest::Shutdown,
                )
                .await
                {
                    Ok(_) => {
                        print!("Shutdown signal sent. Waiting for daemon to exit");
                        if poll_socket_removal(&socket, std::time::Duration::from_secs(5)).await {
                            println!(" done.");
                        } else {
                            println!(
                                " timed out.\nSocket still exists at {}. \
                                 The daemon may still be shutting down, or you may need: \
                                 pkill -9 -f 'hippo.*daemon'",
                                socket.display()
                            );
                        }
                    }
                    Err(e) => {
                        eprintln!("Shutdown request failed: {e:#}");
                        match commands::probe_socket(&socket, config.daemon.socket_timeout_ms).await
                        {
                            commands::SocketProbeResult::Missing => {
                                println!("Daemon is not running (no socket found).");
                            }
                            commands::SocketProbeResult::Stale => {
                                println!(
                                    "Could not connect to daemon, and socket {} is stale.\n\
                                     Cleaning it up.",
                                    socket.display()
                                );
                                std::fs::remove_file(&socket).ok();
                            }
                            commands::SocketProbeResult::Responsive => {
                                println!(
                                    "Daemon at {} responded to a probe, but shutdown request failed.\n\
                                     Leaving the socket in place.",
                                    socket.display()
                                );
                            }
                            commands::SocketProbeResult::Unresponsive => {
                                println!(
                                    "Socket exists at {}, but the daemon did not respond.\n\
                                     Refusing to remove it automatically; inspect or kill the process first.",
                                    socket.display()
                                );
                            }
                        }
                    }
                }
            }
            DaemonAction::Restart => {
                let socket = config.socket_path();
                if socket.exists() {
                    let _ = commands::send_request(
                        &socket,
                        &hippo_core::protocol::DaemonRequest::Shutdown,
                    )
                    .await;

                    if !poll_socket_removal(&socket, std::time::Duration::from_secs(3)).await {
                        eprintln!(
                            "Warning: daemon did not stop within 3s. \
                             You may need: pkill -9 -f 'hippo.*daemon'"
                        );
                        std::process::exit(1);
                    }
                }

                tracing::info!("starting daemon...");
                daemon::run(config).await?;
            }
            DaemonAction::Install { force } => {
                let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
                let project_root = manifest_dir
                    .parent()
                    .and_then(|p| p.parent())
                    .expect("cannot determine project root");
                let brain_dir = project_root.join("brain");

                let vars = install::detect_vars(&brain_dir)?;

                println!("Installing LaunchAgents...");
                println!("  hippo binary: {}", vars.hippo_bin.display());
                println!("  uv binary:    {}", vars.uv_bin.display());
                println!("  brain dir:    {}", vars.brain_dir.display());
                println!("  data dir:     {}", vars.data_dir.display());
                println!();

                let daemon_template = include_str!("../../../launchd/com.hippo.daemon.plist");
                let brain_template = include_str!("../../../launchd/com.hippo.brain.plist");

                install::install_plist("com.hippo.daemon", daemon_template, &vars, force)?;
                install::install_plist("com.hippo.brain", brain_template, &vars, force)?;

                println!();
                println!("Symlink binary...");
                install::symlink_binary(&vars.hippo_bin, force)?;

                println!();
                println!("Load with:");
                println!(
                    "  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hippo.daemon.plist"
                );
                println!(
                    "  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hippo.brain.plist"
                );
            }
        },
        Commands::Brain { action } => match action {
            BrainAction::Stop => {
                let output = std::process::Command::new("pkill")
                    .args(["-f", "hippo-brain serve"])
                    .output();
                match output {
                    Ok(o) if o.status.success() => {
                        println!("Sent SIGTERM to brain server.");
                    }
                    _ => {
                        println!("No brain server process found.");
                    }
                }
            }
        },
        Commands::SendEvent { source } => match source {
            SendEventSource::Shell {
                cmd,
                exit,
                cwd,
                duration_ms,
                git_branch,
                git_commit,
                git_dirty,
                output,
            } => {
                commands::handle_send_event_shell(
                    &config,
                    cmd,
                    exit,
                    cwd,
                    duration_ms,
                    git_branch,
                    git_commit,
                    git_dirty,
                    output,
                )
                .await?;
            }
        },
        Commands::Status => {
            commands::handle_status(&config).await?;
        }
        Commands::Sessions { today, since } => {
            commands::handle_sessions(&config, today, since).await?;
        }
        Commands::Events {
            session,
            since,
            project,
        } => {
            commands::handle_events(&config, session, since, project).await?;
        }
        Commands::Query { text, raw } => {
            if raw {
                commands::handle_query_raw(&config, &text).await?;
            } else {
                // Try brain server first, fall back to raw
                let brain_url = format!("http://localhost:{}/query", config.brain.port);
                let client = reqwest::Client::new();
                match client
                    .post(&brain_url)
                    .json(&serde_json::json!({"text": text, "mode": "semantic"}))
                    .timeout(std::time::Duration::from_secs(10))
                    .send()
                    .await
                {
                    Ok(resp) if resp.status().is_success() => {
                        let body: serde_json::Value = resp.json().await?;

                        if let Some(warning) = body.get("warning").and_then(|w| w.as_str()) {
                            eprintln!("Warning: {warning}");
                        }

                        match body.get("mode").and_then(|m| m.as_str()) {
                            Some("semantic") => {
                                if let Some(results) =
                                    body.get("results").and_then(|r| r.as_array())
                                {
                                    if results.is_empty() {
                                        println!("No results found.");
                                    } else {
                                        for result in results {
                                            let score = result
                                                .get("score")
                                                .and_then(|s| s.as_f64())
                                                .unwrap_or(0.0);
                                            let summary = result
                                                .get("summary")
                                                .and_then(|s| s.as_str())
                                                .unwrap_or("(no summary)");
                                            let cwd = result
                                                .get("cwd")
                                                .and_then(|s| s.as_str())
                                                .unwrap_or("");
                                            let branch = result
                                                .get("git_branch")
                                                .and_then(|s| s.as_str())
                                                .unwrap_or("");
                                            let tags = result
                                                .get("tags")
                                                .and_then(|t| t.as_array())
                                                .map(|arr| {
                                                    let items: Vec<&str> = arr
                                                        .iter()
                                                        .filter_map(|v| v.as_str())
                                                        .collect();
                                                    format!("[{}]", items.join(", "))
                                                });

                                            println!("[{score:.2}] {summary}");
                                            if !cwd.is_empty() || !branch.is_empty() {
                                                let location = if branch.is_empty() {
                                                    cwd.to_string()
                                                } else {
                                                    format!("{cwd} ({branch})")
                                                };
                                                println!("       {location}");
                                            }
                                            if let Some(ref tags_str) = tags {
                                                println!("       tags={tags_str}");
                                            }
                                        }
                                    }
                                }
                            }
                            _ => {
                                // Lexical or unknown mode: print raw JSON
                                println!("{}", serde_json::to_string_pretty(&body)?);
                            }
                        }
                    }
                    _ => {
                        eprintln!("Brain server unavailable, falling back to raw query...");
                        commands::handle_query_raw(&config, &text).await?;
                    }
                }
            }
        }
        Commands::Entities { entity_type } => {
            let response = commands::send_request(
                &config.socket_path(),
                &hippo_core::protocol::DaemonRequest::GetEntities { entity_type },
            )
            .await?;
            match response {
                hippo_core::protocol::DaemonResponse::Entities(entities) => {
                    if entities.is_empty() {
                        println!("No entities found.");
                    } else {
                        for e in &entities {
                            println!(
                                "[{}] {} ({}){}",
                                e.entity_type,
                                e.name,
                                e.id,
                                e.canonical
                                    .as_ref()
                                    .map(|c| format!(" -> {}", c))
                                    .unwrap_or_default()
                            );
                        }
                    }
                }
                hippo_core::protocol::DaemonResponse::Error(e) => eprintln!("Error: {}", e),
                _ => eprintln!("Unexpected response"),
            }
        }
        Commands::ExportTraining { out: _, since: _ } => {
            eprintln!(
                "Training export is not yet implemented. Use: uv run --project brain hippo-brain export"
            );
            std::process::exit(1);
        }
        Commands::Config { action } => match action {
            ConfigAction::Edit => {
                let editor = std::env::var("EDITOR").unwrap_or_else(|_| "vi".to_string());
                let config_path = config.storage.config_dir.join("config.toml");
                std::fs::create_dir_all(&config.storage.config_dir)?;
                if !config_path.exists() {
                    std::fs::write(
                        &config_path,
                        include_str!("../../../config/config.default.toml"),
                    )?;
                }
                let status = std::process::Command::new(editor)
                    .arg(&config_path)
                    .status()?;
                if !status.success() {
                    eprintln!("Editor exited with non-zero status");
                }
            }
            ConfigAction::Set { key, value } => {
                eprintln!("config set is not yet implemented (key={key}, value={value})");
                std::process::exit(1);
            }
        },
        Commands::Redact { action } => match action {
            RedactAction::Test { input } => {
                commands::handle_redact_test(&config, &input);
            }
        },
        Commands::Ingest { source } => match source {
            IngestSource::ClaudeSession {
                path,
                batch,
                inline,
            } => {
                let path = std::path::Path::new(&path);
                if !path.exists() {
                    eprintln!("File not found: {}", path.display());
                    std::process::exit(1);
                }
                let socket = config.socket_path();
                let timeout = config.daemon.socket_timeout_ms;
                if batch {
                    let (sent, errors) =
                        claude_session::ingest_batch(path, &socket, timeout).await?;
                    println!(
                        "Batch import complete: {} events sent, {} errors",
                        sent, errors
                    );
                } else if !inline && std::env::var("TMUX").is_ok() {
                    // Spawn tailer in a new tmux window
                    let hippo_bin =
                        std::env::current_exe().unwrap_or_else(|_| PathBuf::from("hippo"));
                    let session_name = path
                        .file_stem()
                        .and_then(|s| s.to_str())
                        .unwrap_or("session");
                    let short_id = &session_name[..8.min(session_name.len())];
                    let window_name = format!("hippo:{}", short_id);
                    let cmd = format!(
                        "{} ingest claude-session --inline {}",
                        hippo_bin.display(),
                        path.display()
                    );
                    let status = std::process::Command::new("tmux")
                        .args(["new-window", "-n", &window_name, &cmd])
                        .status();
                    match status {
                        Ok(s) if s.success() => {
                            println!(
                                "Tailing in tmux window '{}' (switch with: tmux select-window -t '{}')",
                                window_name, window_name
                            );
                        }
                        _ => {
                            eprintln!("Failed to create tmux window, falling back to inline");
                            println!("Tailing {} (Ctrl+C to stop)", path.display());
                            claude_session::ingest_tail(path, &socket, timeout).await?;
                        }
                    }
                } else {
                    println!("Tailing {} (Ctrl+C to stop)", path.display());
                    claude_session::ingest_tail(path, &socket, timeout).await?;
                }
            }
        },
        Commands::NativeMessagingHost => {
            hippo_daemon::native_messaging::run(&config).await?;
        }
        Commands::Doctor => {
            commands::handle_doctor(&config).await?;
        }
    }

    Ok(())
}
