pub mod claude_session;
pub mod commands;
pub mod daemon;
pub mod framing;
pub mod gh_api;
pub mod gh_poll;
pub mod git_repo;
#[cfg(feature = "otel")]
pub mod metrics;
pub mod native_messaging;
pub mod probe;
#[cfg(feature = "otel")]
pub mod process_metrics;
pub mod schema_handshake;
#[cfg(feature = "otel")]
pub mod telemetry;

use hippo_core::config::ENV_ALLOWLIST;
use hippo_core::events::ShellEvent;
use hippo_core::redaction::{RedactionEngine, RedactionResult};

pub fn detect_shell_kind() -> hippo_core::events::ShellKind {
    std::env::var("SHELL")
        .ok()
        .and_then(|s| {
            let base = std::path::Path::new(&s).file_name()?.to_str()?;
            Some(match base {
                "zsh" => hippo_core::events::ShellKind::Zsh,
                "bash" => hippo_core::events::ShellKind::Bash,
                "fish" => hippo_core::events::ShellKind::Fish,
                other => hippo_core::events::ShellKind::Unknown(other.to_string()),
            })
        })
        .unwrap_or(hippo_core::events::ShellKind::Zsh)
}

pub fn load_redaction_engine(config: &hippo_core::config::HippoConfig) -> RedactionEngine {
    let redact_path = config.redact_path();
    match RedactionEngine::from_config_path(&redact_path) {
        Ok(engine) => engine,
        Err(e) => {
            eprintln!(
                "Warning: failed to load redaction config from {}: {e}. Using builtin patterns.",
                redact_path.display()
            );
            RedactionEngine::builtin()
        }
    }
}

/// Redact a shell event: scrub the command, filter env to allowlist, redact env values.
/// Returns the redacted event plus the per-rule hit breakdown from the command
/// redaction pass, so callers can emit per-rule observability (see #52). The
/// breakdown is command-only — env values are redacted too, but their hits are
/// not currently surfaced (would require a separate metric dimension).
pub fn redact_shell_event(
    event: &ShellEvent,
    redaction: &RedactionEngine,
) -> (Box<ShellEvent>, Vec<(String, u32)>) {
    let RedactionResult { text, count, hits } = redaction.redact(&event.command);
    let filtered_env = event
        .env_snapshot
        .iter()
        .filter(|(k, _)| ENV_ALLOWLIST.contains(&k.as_str()))
        .map(|(k, v)| (k.clone(), redaction.redact(v).text))
        .collect();

    let redacted = Box::new(ShellEvent {
        command: text,
        redaction_count: count,
        env_snapshot: filtered_env,
        ..event.clone()
    });
    (redacted, hits)
}
