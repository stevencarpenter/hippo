pub mod auto_memory_poll;
pub mod backfill;
pub mod browser_health;
pub mod claude_session;
pub mod codex_session;
pub mod commands;
pub mod cursor_session;
pub mod daemon;
pub mod framing;
pub mod gh_api;
pub mod gh_poll;
pub mod git_repo;
#[cfg(feature = "otel")]
pub mod health_score;
#[cfg(feature = "otel")]
pub mod metrics;
pub mod native_messaging;
pub mod opencode_session;
pub mod probe;
mod probe_agentic;
mod probe_auto_memory;
#[cfg(feature = "otel")]
pub mod process_metrics;
pub mod schema_handshake;
#[cfg(feature = "otel")]
pub mod source_health_metric;
#[cfg(feature = "otel")]
pub mod telemetry;
pub mod watch_auto_memory;
pub mod watch_claude_sessions;
pub mod watchdog;

use hippo_core::config::ENV_ALLOWLIST;
use hippo_core::events::ShellEvent;
use hippo_core::redaction::{RedactionEngine, RedactionResult};
use tracing::warn;

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

pub(crate) fn is_missing_source_health_table_error(err: &rusqlite::Error) -> bool {
    err.to_string().contains("no such table: source_health")
}

/// Returns `true` when the rusqlite error is SQLITE_BUSY (error code 5).
/// Shared between watchdog (alarm-insert retry) and daemon flush_events
/// (per-op DB_BUSY_COUNT instrumentation, post-review I-3).
pub(crate) fn is_sqlite_busy(err: &rusqlite::Error) -> bool {
    matches!(
        err,
        rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error {
                code: rusqlite::ErrorCode::DatabaseBusy,
                ..
            },
            _,
        )
    )
}

/// Run `f` once; on SQLITE_BUSY record the metric, log a warning, sleep 100ms,
/// and retry exactly once. Models the watchdog alarm-insert path so every
/// writer shares one definition of single-retry-on-BUSY.
pub(crate) fn with_busy_retry<T, F>(op: &'static str, mut f: F) -> Result<T, rusqlite::Error>
where
    F: FnMut() -> Result<T, rusqlite::Error>,
{
    match f() {
        Ok(v) => Ok(v),
        Err(e) if is_sqlite_busy(&e) => {
            #[cfg(feature = "otel")]
            {
                crate::metrics::record_db_busy(&e, op);
            }
            warn!(op, "transient SQLite lock contention; retrying once");
            std::thread::sleep(std::time::Duration::from_millis(100));
            f()
        }
        Err(e) => Err(e),
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

#[cfg(test)]
mod busy_retry_tests {
    use super::*;
    use rusqlite::ffi;

    pub(crate) fn sqlite_busy_error() -> rusqlite::Error {
        rusqlite::Error::SqliteFailure(
            ffi::Error {
                code: rusqlite::ErrorCode::DatabaseBusy,
                extended_code: ffi::SQLITE_BUSY,
            },
            Some("database is locked".into()),
        )
    }

    #[test]
    fn with_busy_retry_succeeds_on_second_attempt() {
        let mut attempts = 0;
        let result = with_busy_retry("test_op", || {
            attempts += 1;
            if attempts == 1 {
                Err(sqlite_busy_error())
            } else {
                Ok(42)
            }
        });
        assert_eq!(result.unwrap(), 42);
        assert_eq!(attempts, 2);
    }

    #[test]
    fn with_busy_retry_returns_persistent_busy() {
        let mut attempts = 0;
        let result: rusqlite::Result<i32> = with_busy_retry("test_op", || {
            attempts += 1;
            Err(sqlite_busy_error())
        });
        assert!(is_sqlite_busy(&result.unwrap_err()));
        assert_eq!(attempts, 2);
    }

    fn non_busy_sqlite_error() -> rusqlite::Error {
        rusqlite::Error::SqliteFailure(
            ffi::Error {
                code: rusqlite::ErrorCode::ConstraintViolation,
                extended_code: ffi::SQLITE_CONSTRAINT,
            },
            Some("constraint".into()),
        )
    }

    #[test]
    fn with_busy_retry_does_not_retry_other_errors() {
        let mut attempts = 0;
        let result: rusqlite::Result<i32> = with_busy_retry("test_op", || {
            attempts += 1;
            Err(non_busy_sqlite_error())
        });
        assert!(result.is_err());
        assert_eq!(attempts, 1);
    }

    #[cfg(feature = "otel")]
    #[test]
    fn record_db_busy_claude_session_insert() {
        assert!(crate::metrics::record_db_busy(
            &sqlite_busy_error(),
            "claude_session_insert"
        ));
        assert!(!crate::metrics::record_db_busy(
            &non_busy_sqlite_error(),
            "claude_session_insert"
        ));
    }
}
