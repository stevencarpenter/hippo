pub mod claude_session;
pub mod commands;
pub mod daemon;
pub mod framing;

use hippo_core::config::ENV_ALLOWLIST;
use hippo_core::events::ShellEvent;
use hippo_core::redaction::{RedactionEngine, RedactionResult};

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
/// Returns the redacted event and the command redaction result (for counting).
pub fn redact_shell_event(event: &ShellEvent, redaction: &RedactionEngine) -> Box<ShellEvent> {
    let RedactionResult { text, count } = redaction.redact(&event.command);
    let filtered_env = event
        .env_snapshot
        .iter()
        .filter(|(k, _)| ENV_ALLOWLIST.contains(&k.as_str()))
        .map(|(k, v)| (k.clone(), redaction.redact(v).text))
        .collect();

    Box::new(ShellEvent {
        command: text,
        redaction_count: count,
        env_snapshot: filtered_env,
        ..event.clone()
    })
}
