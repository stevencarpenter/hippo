//! FSEvents watcher for configured Claude auto-memory Markdown sources.
//!
//! Debounces rapid writes, then delegates stable-read reconciliation to the
//! Python brain package (`hippo-auto-memory-reconcile`). A periodic fallback
//! runs the full configured-source reconcile (`hippo-auto-memory-poll`).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use hippo_core::config::{HippoConfig, default_brain_dir};
use notify::{
    Config as NotifyConfig, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher,
};
use tokio::signal::unix::{SignalKind, signal as unix_signal};
use tokio::sync::mpsc;
use tracing::{debug, info, warn};

const TICK_INTERVAL: Duration = Duration::from_secs(1);

fn expand_tilde(path: &Path) -> PathBuf {
    let raw = path.to_string_lossy();
    if let Some(rest) = raw.strip_prefix("~/") {
        dirs::home_dir()
            .map(|home| home.join(rest))
            .unwrap_or_else(|| path.to_path_buf())
    } else if raw == "~" {
        dirs::home_dir().unwrap_or_else(|| path.to_path_buf())
    } else {
        path.to_path_buf()
    }
}

fn configured_source_paths(config: &HippoConfig) -> Vec<PathBuf> {
    config
        .auto_memory
        .sources
        .iter()
        .map(|source| {
            expand_tilde(&source.path)
                .canonicalize()
                .unwrap_or_else(|_| expand_tilde(&source.path))
        })
        .collect()
}

fn spawn_reconcile_file(config: &HippoConfig, path: &Path) -> Result<()> {
    let brain_dir = default_brain_dir();
    let config_path = config.storage.config_dir.join("config.toml");
    let output = Command::new("uv")
        .args([
            "run",
            "--project",
            &brain_dir.to_string_lossy(),
            "hippo-auto-memory-reconcile",
            "--file",
            &path.to_string_lossy(),
            "--config",
            &config_path.to_string_lossy(),
        ])
        .output()
        .with_context(|| {
            format!(
                "failed to spawn hippo-auto-memory-reconcile for {}",
                path.display()
            )
        })?;
    if !output.status.success() {
        anyhow::bail!(
            "hippo-auto-memory-reconcile failed for {} (exit {}): {}",
            path.display(),
            output.status,
            String::from_utf8_lossy(&output.stderr)
        );
    }
    debug!(
        path = %path.display(),
        stdout = %String::from_utf8_lossy(&output.stdout).trim(),
        "auto-memory watcher: reconciled file"
    );
    Ok(())
}

fn spawn_reconcile_all(config: &HippoConfig) -> Result<usize> {
    let brain_dir = default_brain_dir();
    let config_path = config.storage.config_dir.join("config.toml");
    let output = Command::new("uv")
        .args([
            "run",
            "--project",
            &brain_dir.to_string_lossy(),
            "hippo-auto-memory-poll",
            "--config",
            &config_path.to_string_lossy(),
        ])
        .output()
        .with_context(|| "failed to spawn hippo-auto-memory-poll".to_string())?;
    if !output.status.success() {
        anyhow::bail!(
            "hippo-auto-memory-poll failed (exit {}): {}",
            output.status,
            String::from_utf8_lossy(&output.stderr)
        );
    }
    let parsed: serde_json::Value =
        serde_json::from_slice(&output.stdout).context("parse auto-memory poll JSON output")?;
    let changed = parsed
        .get("changed")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0) as usize;
    info!(changed, "auto-memory watcher: periodic reconcile completed");
    Ok(changed)
}

fn event_targets_known_source(event: &Event, sources: &[PathBuf]) -> Vec<PathBuf> {
    let mut hits = Vec::new();
    for path in event.paths.iter() {
        let resolved = path.canonicalize().unwrap_or_else(|_| path.clone());
        if sources.iter().any(|source| source == &resolved) {
            hits.push(resolved);
        }
    }
    hits
}

/// Entry point — runs until SIGTERM/ctrl-c.
pub async fn run(config: &HippoConfig) -> Result<()> {
    if !config.auto_memory.enabled {
        warn!("auto-memory watcher: disabled by config");
        return Ok(());
    }
    if config.auto_memory.sources.is_empty() {
        warn!("auto-memory watcher: enabled but no sources configured");
        return Ok(());
    }

    let sources = configured_source_paths(config);
    let debounce = Duration::from_millis(config.auto_memory.debounce_ms);
    let fallback = Duration::from_secs(config.auto_memory.reconcile_fallback_secs);

    let mut pending: HashMap<PathBuf, Instant> = HashMap::new();
    let mut last_fallback = Instant::now();

    for path in &sources {
        if path.is_file()
            && let Err(e) = spawn_reconcile_file(config, path)
        {
            warn!(path = %path.display(), error = %e, "auto-memory watcher: startup reconcile failed");
        }
    }

    let watch_dirs: Vec<PathBuf> = sources
        .iter()
        .filter_map(|path| path.parent().map(Path::to_path_buf))
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect();

    let (tx, mut rx) = mpsc::channel::<Event>(256);
    let mut watcher = RecommendedWatcher::new(
        move |event: notify::Result<Event>| {
            if let Ok(event) = event {
                let _ = tx.try_send(event);
            }
        },
        NotifyConfig::default(),
    )
    .context("auto-memory watcher: failed to create FSEvents watcher")?;

    for dir in &watch_dirs {
        if dir.exists() {
            watcher
                .watch(dir, RecursiveMode::NonRecursive)
                .with_context(|| {
                    format!("auto-memory watcher: failed to watch {}", dir.display())
                })?;
        } else {
            warn!(dir = %dir.display(), "auto-memory watcher: parent directory missing");
        }
    }

    info!(
        sources = sources.len(),
        "auto-memory watcher: listening for FSEvents"
    );

    let mut tick = tokio::time::interval(TICK_INTERVAL);
    let mut sigterm = unix_signal(SignalKind::terminate())
        .context("auto-memory watcher: failed to install SIGTERM handler")?;

    loop {
        tokio::select! {
            _ = sigterm.recv() => {
                info!("auto-memory watcher: received SIGTERM; exiting");
                break;
            }
            maybe_event = rx.recv() => {
                let Some(event) = maybe_event else { break };
                if matches!(event.kind, EventKind::Access(_)) {
                    continue;
                }
                for path in event_targets_known_source(&event, &sources) {
                    pending.insert(path, Instant::now());
                }
            }
            _ = tick.tick() => {
                let now = Instant::now();
                let ready: Vec<PathBuf> = pending
                    .iter()
                    .filter_map(|(path, seen)| {
                        if now.duration_since(*seen) >= debounce {
                            Some(path.clone())
                        } else {
                            None
                        }
                    })
                    .collect();
                for path in ready {
                    pending.remove(&path);
                    if let Err(e) = spawn_reconcile_file(config, &path) {
                        warn!(path = %path.display(), error = %e, "auto-memory watcher: reconcile failed");
                    }
                }
                if now.duration_since(last_fallback) >= fallback {
                    last_fallback = now;
                    if let Err(e) = spawn_reconcile_all(config) {
                        warn!(error = %e, "auto-memory watcher: periodic reconcile failed");
                    }
                }
            }
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_targets_known_source_matches_resolved_paths() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("MEMORY.md");
        std::fs::write(&file, "# test\n").unwrap();
        let resolved = file.canonicalize().unwrap();
        let event = Event {
            kind: EventKind::Modify(notify::event::ModifyKind::Data(
                notify::event::DataChange::Content,
            )),
            paths: vec![resolved.clone()],
            attrs: notify::event::EventAttributes::default(),
        };
        let hits = event_targets_known_source(&event, std::slice::from_ref(&resolved));
        assert_eq!(hits, vec![resolved]);
    }
}
