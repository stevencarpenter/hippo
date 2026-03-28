mod cli;
mod commands;
mod daemon;
mod framing;

use anyhow::Result;
use clap::Parser;
use cli::{BrainAction, Cli, Commands, ConfigAction, DaemonAction, RedactAction, SendEventSource};
use hippo_core::config::HippoConfig;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let config = HippoConfig::load_default().unwrap_or_default();

    match cli.command {
        Commands::Daemon { action } => match action {
            DaemonAction::Run => {
                tracing_subscriber::fmt()
                    .with_env_filter(
                        EnvFilter::try_from_default_env()
                            .unwrap_or_else(|_| EnvFilter::new("info")),
                    )
                    .init();
                daemon::run(config).await?;
            }
            DaemonAction::Start => {
                println!("Use: launchctl load ~/Library/LaunchAgents/com.hippo.daemon.plist");
            }
            DaemonAction::Stop => {
                let _ = commands::send_request(
                    &config.socket_path(),
                    &hippo_core::protocol::DaemonRequest::Shutdown,
                )
                    .await;
                println!("Shutdown signal sent.");
            }
            DaemonAction::Restart => {
                let _ = commands::send_request(
                    &config.socket_path(),
                    &hippo_core::protocol::DaemonRequest::Shutdown,
                )
                    .await;
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                tracing_subscriber::fmt()
                    .with_env_filter(
                        EnvFilter::try_from_default_env()
                            .unwrap_or_else(|_| EnvFilter::new("info")),
                    )
                    .init();
                daemon::run(config).await?;
            }
            DaemonAction::Install => {
                println!("Copy plist to ~/Library/LaunchAgents/ and load with launchctl.");
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
                    .json(&serde_json::json!({"text": text}))
                    .timeout(std::time::Duration::from_secs(5))
                    .send()
                    .await
                {
                    Ok(resp) if resp.status().is_success() => {
                        let body: serde_json::Value = resp.json().await?;
                        println!("{}", serde_json::to_string_pretty(&body)?);
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
        Commands::ExportTraining { out, since } => {
            let since_ms = since
                .as_deref()
                .and_then(commands::parse_duration_to_since_ms);
            println!("Export training data to {} (since_ms: {:?})", out, since_ms);
            println!("Training export requires the brain server. Use: hippo-brain export");
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
                println!("Setting {} = {} (not yet implemented)", key, value);
            }
        },
        Commands::Redact { action } => match action {
            RedactAction::Test { input } => {
                commands::handle_redact_test(&input);
            }
        },
        Commands::Doctor => {
            commands::handle_doctor(&config).await?;
        }
    }

    Ok(())
}
