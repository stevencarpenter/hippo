//! Schema-version handshake between daemon and brain.
//!
//! The daemon and brain both read/write the same SQLite file and each
//! hardcodes the schema version they were built against. When the two
//! drift (e.g. daemon rebuilt from source while the installed brain is
//! stale), `open_db` happily migrates the DB forward and the brain then
//! logs a flood of "expected N, found N+1" errors on every query.
//!
//! This module lets the daemon ask a running brain what schema version
//! it expects *before* running any migrations. If the two disagree, the
//! daemon refuses to proceed and prints a clear remediation message
//! instead of silently breaking the brain.
//!
//! If the brain isn't running (connection refused / timeout), we assume
//! the user is doing a fresh install or brain-will-start-later and
//! proceed without the guard.
use anyhow::Result;
use serde::Deserialize;
use std::time::Duration;

/// The shape of fields we read out of brain's `/health` response. Extra
/// fields are tolerated — serde drops them by default.
#[derive(Debug, Deserialize)]
struct BrainHealth {
    expected_schema_version: Option<i64>,
}

/// Outcome of a brain handshake attempt.
#[derive(Debug, PartialEq, Eq)]
pub enum HandshakeResult {
    /// Brain isn't reachable on the expected port. Caller should proceed
    /// as if brain just hasn't started yet.
    BrainAbsent,
    /// Brain responded but didn't advertise an expected schema version.
    /// Probably an old brain predating this handshake — proceed with a
    /// warning; the brain's own version guard will catch real mismatches.
    Unknown,
    /// Brain is on the same version as daemon. Safe to migrate.
    Compatible,
    /// Brain reported a different expected version. Refuse to migrate.
    Incompatible {
        daemon_expects: i64,
        brain_expects: i64,
    },
}

/// Probe brain's `/health` endpoint and compare its
/// `expected_schema_version` field against `daemon_expected`.
///
/// Uses short timeouts so a hung brain doesn't block daemon startup
/// longer than a couple of seconds.
pub async fn check_brain_schema_compat(
    daemon_expected: i64,
    brain_port: u16,
) -> Result<HandshakeResult> {
    let url = format!("http://127.0.0.1:{}/health", brain_port);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_millis(1500))
        .connect_timeout(Duration::from_millis(500))
        .build()?;

    let response = match client.get(&url).send().await {
        Ok(r) => r,
        // Connection refused, DNS failure, timeout — treat as "brain not
        // running". The brain LaunchAgent will start it later and do its
        // own version check against the (by then) already-migrated DB.
        Err(_) => return Ok(HandshakeResult::BrainAbsent),
    };

    if !response.status().is_success() {
        return Ok(HandshakeResult::BrainAbsent);
    }

    let body: BrainHealth = match response.json().await {
        Ok(b) => b,
        // Response didn't parse — don't block startup on an unexpected
        // shape. Fall back to the brain's own version guard.
        Err(_) => return Ok(HandshakeResult::Unknown),
    };

    let Some(brain_expects) = body.expected_schema_version else {
        return Ok(HandshakeResult::Unknown);
    };

    if brain_expects == daemon_expected {
        Ok(HandshakeResult::Compatible)
    } else {
        Ok(HandshakeResult::Incompatible {
            daemon_expects: daemon_expected,
            brain_expects,
        })
    }
}

/// Render the remediation hint shown when the handshake detects a
/// mismatch. Lives next to the check so a change to the fix path stays
/// close to the detection path.
pub fn mismatch_advice(daemon_expects: i64, brain_expects: i64) -> String {
    format!(
        "Schema version mismatch: daemon was built for v{daemon} but the \
         running brain reports v{brain}.\n\n\
         The daemon will refuse to migrate the database while brain is \
         out of sync, because doing so would break every brain query \
         until brain is upgraded too.\n\n\
         To fix:\n  \
           - Redeploy the brain alongside the daemon: `mise run install`\n  \
           - Or stop the brain LaunchAgent (`launchctl bootout \
             gui/$UID/com.hippo.brain`) and restart the daemon; brain's \
             own guard will refuse to attach until it's upgraded.\n",
        daemon = daemon_expects,
        brain = brain_expects,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mismatch_advice_names_both_versions() {
        let msg = mismatch_advice(7, 6);
        assert!(msg.contains("v7"));
        assert!(msg.contains("v6"));
        assert!(msg.contains("mise run install"));
    }

    #[tokio::test]
    async fn brain_absent_when_port_closed() {
        // Pick a high port that (almost certainly) isn't bound locally.
        // If it happens to be in use, the test is vacuous; fine.
        let result = check_brain_schema_compat(7, 59_321).await.unwrap();
        assert!(matches!(
            result,
            HandshakeResult::BrainAbsent | HandshakeResult::Unknown
        ));
    }
}
