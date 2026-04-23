mod cli;
mod install;

use hippo_daemon::{claude_session, commands, daemon, gh_api, gh_poll};

use std::path::PathBuf;

use anyhow::{Context, Result};
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
    // Load config early — needed for telemetry init before CLI parsing
    let config = match HippoConfig::load_default() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Warning: failed to load config: {e:#}. Using defaults.");
            HippoConfig::default()
        }
    };

    // Set up rolling file appender for runtime logs (7-day retention).
    // The launchd StandardErrorPath still captures pre-main panics and OS-level
    // launch output; runtime application logs go here exclusively.
    let data_dir = config.storage.data_dir.clone();
    std::fs::create_dir_all(&data_dir).unwrap_or_else(|e| {
        eprintln!(
            "Warning: could not create data dir {}: {e}",
            data_dir.display()
        )
    });
    let file_appender = tracing_appender::rolling::RollingFileAppender::builder()
        .rotation(tracing_appender::rolling::Rotation::DAILY)
        .filename_prefix("daemon")
        .filename_suffix("log")
        .max_log_files(7)
        .build(&data_dir)
        .expect("failed to initialize log appender");
    let (non_blocking, _log_guard) = tracing_appender::non_blocking(file_appender);

    // Initialize telemetry — OTel if feature-enabled and config says so, else plain fmt
    #[cfg(feature = "otel")]
    let _otel_guard = if config.telemetry.enabled {
        // Clone so the fallback path below can reuse the same writer if OTel init fails.
        let writer = non_blocking.clone();
        match hippo_daemon::telemetry::init("hippo-daemon", &config.telemetry.endpoint, writer) {
            Ok(guard) => Some(guard),
            Err(e) => {
                tracing_subscriber::fmt()
                    .with_writer(non_blocking)
                    .with_env_filter(
                        EnvFilter::try_from_default_env()
                            .unwrap_or_else(|_| EnvFilter::new("info")),
                    )
                    .init();
                tracing::warn!("OTel init failed, using plain logging: {e}");
                None
            }
        }
    } else {
        tracing_subscriber::fmt()
            .with_writer(non_blocking)
            .with_env_filter(
                EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
            )
            .init();
        None
    };

    #[cfg(not(feature = "otel"))]
    tracing_subscriber::fmt()
        .with_writer(non_blocking)
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    let cli = Cli::parse();

    match cli.command {
        Commands::Daemon { action } => match action {
            DaemonAction::Run => {
                daemon::run(config).await?;
            }
            DaemonAction::Start => {
                let uid = unsafe { libc::getuid() };
                let domain = format!("gui/{uid}");
                let launch_agents = dirs::home_dir()
                    .context("cannot determine home directory")?
                    .join("Library/LaunchAgents");
                for label in &["com.hippo.daemon", "com.hippo.brain"] {
                    let plist = launch_agents.join(format!("{label}.plist"));
                    if !plist.exists() {
                        eprintln!(
                            "LaunchAgent not found: {}. Run: hippo daemon install",
                            plist.display()
                        );
                        continue;
                    }
                    if install::service_is_loaded(label) {
                        continue;
                    }
                    let status = std::process::Command::new("launchctl")
                        .args([
                            "bootstrap",
                            &domain,
                            plist.to_str().context("non-UTF-8 path")?,
                        ])
                        .status()
                        .context("launchctl failed")?;
                    if status.success() {
                        println!("Started {label}");
                    } else {
                        eprintln!("{label} failed to start");
                    }
                }
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
            DaemonAction::Install {
                force,
                brain_dir: brain_dir_arg,
            } => {
                let brain_dir = brain_dir_arg.unwrap_or_else(|| {
                    // Default to ~/.local/share/hippo-brain
                    dirs::home_dir()
                        .expect("cannot determine home directory")
                        .join(".local/share/hippo-brain")
                });

                let vars = install::detect_vars(&brain_dir)?;

                println!("Installing LaunchAgents...");
                println!("  hippo binary: {}", vars.hippo_bin.display());
                println!("  uv binary:    {}", vars.uv_bin.display());
                println!("  brain dir:    {}", vars.brain_dir.display());
                println!("  data dir:     {}", vars.data_dir.display());
                println!();

                // Detect which services are running so we can cycle them after writing
                // new plists. Do this before touching anything on disk.
                let uid = unsafe { libc::getuid() };
                let domain = format!("gui/{uid}");
                let launch_agents = dirs::home_dir()
                    .context("cannot determine home directory")?
                    .join("Library/LaunchAgents");
                let daemon_was_loaded = install::service_is_loaded("com.hippo.daemon");
                let brain_was_loaded = install::service_is_loaded("com.hippo.brain");

                if brain_was_loaded {
                    print!("  Draining brain (waiting for in-flight requests)");
                    let _ = std::io::Write::flush(&mut std::io::stdout());
                    let drained = install::drain_brain(std::time::Duration::from_secs(10));
                    println!(
                        "{}",
                        if drained {
                            " done"
                        } else {
                            " timed out, proceeding"
                        }
                    );
                    install::service_bootout(&domain, &launch_agents.join("com.hippo.brain.plist"));
                    println!("  Stopped brain");
                }
                if daemon_was_loaded {
                    install::service_bootout(
                        &domain,
                        &launch_agents.join("com.hippo.daemon.plist"),
                    );
                    println!("  Stopped daemon");
                }

                let daemon_template = include_str!("../../../launchd/com.hippo.daemon.plist");
                let brain_template = include_str!("../../../launchd/com.hippo.brain.plist");
                let gh_poll_template = include_str!("../../../launchd/com.hippo.gh-poll.plist");
                let xcode_claude_template =
                    include_str!("../../../launchd/com.hippo.xcode-claude-ingest.plist");
                let xcode_codex_template =
                    include_str!("../../../launchd/com.hippo.xcode-codex-ingest.plist");

                install::install_plist("com.hippo.daemon", daemon_template, &vars, force)?;
                install::install_plist("com.hippo.brain", brain_template, &vars, force)?;
                install::install_plist(
                    "com.hippo.xcode-claude-ingest",
                    xcode_claude_template,
                    &vars,
                    force,
                )?;
                install::install_plist(
                    "com.hippo.xcode-codex-ingest",
                    xcode_codex_template,
                    &vars,
                    force,
                )?;

                // GitHub Actions poller plist — only written when github source is enabled.
                let gh_poll_installed = if config.github.enabled {
                    // Verify the token env var is set at install time so the user
                    // gets an early error, but don't embed it in the plist.
                    if std::env::var(&config.github.token_env).is_err() {
                        anyhow::bail!(
                            "{} must be set to enable the github source",
                            config.github.token_env
                        );
                    }
                    install::install_gh_poll_wrapper(
                        &vars.hippo_bin,
                        &config.github.token_env,
                        &vars.data_dir,
                        force,
                    )?;
                    install::install_plist("com.hippo.gh-poll", gh_poll_template, &vars, force)?;
                    true
                } else {
                    println!("  (github source disabled; skipping gh-poll plist)");
                    false
                };

                println!();
                println!("Symlink binary...");
                install::symlink_binary(&vars.hippo_bin, force)?;

                println!();
                println!("Installing Native Messaging manifest for Firefox...");
                install::install_native_messaging_manifest(&vars.hippo_bin, force)?;

                println!();
                println!("Configuring Claude session hook...");
                // Derive the hook's brain dir the same way `hippo doctor` does
                // (sibling of data_dir) so install and doctor always agree on the
                // expected path, regardless of what --brain-dir was passed.
                let hook_brain_dir = vars
                    .data_dir
                    .parent()
                    .map(|p| p.join("hippo-brain"))
                    .context("data_dir has no parent — cannot derive brain dir for hook")?;
                match install::configure_claude_session_hook(&hook_brain_dir) {
                    Ok(()) => {}
                    Err(e) => println!(
                        "  Warning: {e} — configure manually: {}/shell/claude-session-hook.sh",
                        hook_brain_dir.display()
                    ),
                }

                // Reload services that were running before the upgrade.
                if daemon_was_loaded || brain_was_loaded {
                    println!();
                    println!("Restarting services...");
                    if daemon_was_loaded {
                        install::service_bootstrap(
                            &domain,
                            &launch_agents.join("com.hippo.daemon.plist"),
                        )?;
                        println!("  Started daemon");
                    }
                    if brain_was_loaded {
                        install::service_bootstrap(
                            &domain,
                            &launch_agents.join("com.hippo.brain.plist"),
                        )?;
                        println!("  Started brain");
                    }
                }

                // Only print "Load with:" for services that weren't already cycled.
                let needs_manual_start =
                    !daemon_was_loaded || !brain_was_loaded || gh_poll_installed;
                if needs_manual_start {
                    println!();
                    println!("Load with:");
                    if !daemon_was_loaded {
                        println!(
                            "  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hippo.daemon.plist"
                        );
                    }
                    if !brain_was_loaded {
                        println!(
                            "  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hippo.brain.plist"
                        );
                    }
                    if gh_poll_installed {
                        println!(
                            "  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hippo.gh-poll.plist"
                        );
                    }
                }
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
                git_repo,
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
                    git_repo,
                    git_branch,
                    git_commit,
                    git_dirty,
                    output,
                )
                .await?;
            }
            SendEventSource::Watchlist { sha, repo, ttl } => {
                let request = hippo_core::protocol::DaemonRequest::RegisterWatchSha {
                    sha,
                    repo,
                    ttl_secs: ttl,
                };
                commands::send_request(&config.socket_path(), &request).await?;
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
        Commands::Ask { question } => {
            let brain_url = format!("http://localhost:{}/ask", config.brain.port);
            let client = reqwest::Client::new();
            match client
                .post(&brain_url)
                .json(&serde_json::json!({"question": question}))
                .timeout(std::time::Duration::from_secs(120))
                .send()
                .await
            {
                Ok(resp) if resp.status().is_success() => {
                    let body: serde_json::Value = resp.json().await?;

                    // Build the full output as a string
                    let mut output = String::new();

                    if let Some(answer) = body.get("answer").and_then(|a| a.as_str()) {
                        output.push_str(answer);
                    }
                    if let Some(error) = body.get("error").and_then(|e| e.as_str()) {
                        eprintln!("Error: {error}");
                    }

                    if let Some(sources) = body.get("sources").and_then(|s| s.as_array())
                        && !sources.is_empty()
                    {
                        output.push_str("\n\n---\n\n**Sources:**\n");
                        for (i, src) in sources.iter().enumerate() {
                            let score = src.get("score").and_then(|s| s.as_f64()).unwrap_or(0.0);
                            let summary = src
                                .get("summary")
                                .and_then(|s| s.as_str())
                                .unwrap_or("(no summary)");
                            let cwd = src.get("cwd").and_then(|s| s.as_str()).unwrap_or("");
                            let branch =
                                src.get("git_branch").and_then(|s| s.as_str()).unwrap_or("");
                            let ts = src.get("timestamp").and_then(|t| t.as_i64()).unwrap_or(0);
                            let date = if ts > 0 {
                                chrono::DateTime::from_timestamp_millis(ts)
                                    .map(|dt| dt.format("%Y-%m-%d").to_string())
                                    .unwrap_or_default()
                            } else {
                                String::new()
                            };

                            let display_summary = if summary.len() > 120 {
                                format!("{}…", &summary[..119])
                            } else {
                                summary.to_string()
                            };

                            output.push_str(&format!(
                                "{}. **[{:.0}%]** {}\n",
                                i + 1,
                                score * 100.0,
                                display_summary
                            ));
                            let mut location = String::new();
                            if !cwd.is_empty() {
                                location.push_str(cwd);
                            }
                            if !branch.is_empty() {
                                location.push_str(&format!(" ({branch})"));
                            }
                            if !date.is_empty() {
                                if !location.is_empty() {
                                    location.push_str(" — ");
                                }
                                location.push_str(&date);
                            }
                            if !location.is_empty() {
                                output.push_str(&format!("   {location}\n"));
                            }
                        }
                    }

                    // Pipe through glow if available and stdout is a TTY
                    let use_glow = std::io::IsTerminal::is_terminal(&std::io::stdout())
                        && which::which("glow").is_ok();

                    if use_glow {
                        use std::io::Write;
                        match std::process::Command::new("glow")
                            .args(["-", "--width", "100"])
                            .stdin(std::process::Stdio::piped())
                            .spawn()
                        {
                            Ok(mut child) => {
                                if let Some(mut stdin) = child.stdin.take() {
                                    let _ = stdin.write_all(output.as_bytes());
                                }
                                let _ = child.wait();
                            }
                            Err(_) => {
                                print!("{output}");
                            }
                        }
                    } else {
                        print!("{output}");
                    }
                }
                Ok(resp) => {
                    let status = resp.status();
                    let body = resp.text().await.unwrap_or_default();
                    eprintln!("Brain server error ({status}): {body}");
                }
                Err(_) => {
                    eprintln!(
                        "Brain server not reachable at localhost:{}. Is hippo-brain running?",
                        config.brain.port
                    );
                    eprintln!("Run: hippo doctor");
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
            anyhow::bail!(
                "Training export is not yet implemented. Use: uv run --project brain hippo-brain export"
            );
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
                anyhow::bail!("config set is not yet implemented (key={key}, value={value})");
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
                wait_for_file,
            } => {
                let path = std::path::Path::new(&path);
                if !path.exists() {
                    if wait_for_file > 0 {
                        let deadline = std::time::Instant::now()
                            + std::time::Duration::from_secs(wait_for_file);
                        eprint!("Waiting for {}...", path.display());
                        while !path.exists() {
                            if std::time::Instant::now() >= deadline {
                                eprintln!(
                                    "\nFile not found after {}s: {}",
                                    wait_for_file,
                                    path.display()
                                );
                                std::process::exit(1);
                            }
                            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                        }
                        eprintln!(" found.");
                    } else {
                        eprintln!("File not found: {}", path.display());
                        std::process::exit(1);
                    }
                }
                let socket = config.socket_path();
                let timeout = config.daemon.socket_timeout_ms;
                if batch {
                    let db = config.db_path();
                    let (sent, errors) =
                        claude_session::ingest_batch(path, &socket, timeout, &db).await?;
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
                    let cmd = {
                        // Shell-quote paths to handle spaces/metacharacters.
                        // Wrap in single quotes, escaping embedded quotes.
                        let sq = |s: &str| format!("'{}'", s.replace('\'', "'\\''"));
                        let q_bin = sq(&hippo_bin.to_string_lossy());
                        let q_path = sq(&path.to_string_lossy());
                        if wait_for_file > 0 {
                            format!(
                                "{q_bin} ingest claude-session --inline --wait-for-file {wait_for_file} {q_path}"
                            )
                        } else {
                            format!("{q_bin} ingest claude-session --inline {q_path}")
                        }
                    };
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
        Commands::GhPoll { repo } => {
            let token = std::env::var(&config.github.token_env)
                .map_err(|_| anyhow::anyhow!("{} is not set", config.github.token_env))?;
            let api = gh_api::GhApi::new("https://api.github.com".into(), token);
            let watched_repos = match repo {
                Some(r) => vec![r],
                None => config.github.watched_repos.clone(),
            };
            let poll_cfg = gh_poll::PollConfig {
                watched_repos,
                log_excerpt_max_bytes: config.github.log_excerpt_max_bytes,
                redact_config_path: Some(config.redact_path()),
            };
            gh_poll::run_once(&api, &config.db_path(), &poll_cfg).await?;
        }
        Commands::GhPendingNotifications { repo, ack } => {
            let db = hippo_core::storage::open_db(&config.db_path())?;
            let now = chrono::Utc::now().timestamp_millis();
            let pending = hippo_core::storage::watchlist::pending_notifications(&db, now)?;
            let matching: Vec<_> = pending.iter().filter(|e| e.repo == repo).collect();

            for entry in &matching {
                println!(
                    "CI {} on SHA {} (repo: {})",
                    entry.terminal_status.as_deref().unwrap_or("unknown"),
                    entry.sha,
                    entry.repo
                );
            }

            if ack {
                for entry in &matching {
                    hippo_core::storage::watchlist::mark_notified(&db, &entry.sha, &entry.repo)?;
                }
            }
        }
        Commands::NativeMessagingHost => {
            hippo_daemon::native_messaging::run(&config).await?;
        }
        Commands::Doctor { explain } => {
            commands::handle_doctor(&config, explain).await?;
        }
    }

    // Shutdown OTel providers
    #[cfg(feature = "otel")]
    if let Some(guard) = _otel_guard {
        guard.shutdown();
    }

    Ok(())
}
