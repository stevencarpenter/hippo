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
//! If the brain isn't running (connection refused), we assume the user
//! is doing a fresh install or brain-will-start-later and proceed
//! without the guard. Timeouts are treated as Unknown — a slow brain is
//! not the same as an absent one.
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
        // Timeout means brain's port is open but it's not responding in
        // time — a slow or half-started brain, not an absent one. Return
        // Unknown so the caller treats this with caution rather than
        // silently proceeding as if brain were absent.
        Err(e) if e.is_timeout() => return Ok(HandshakeResult::Unknown),
        // Connection refused, DNS failure, etc. — brain is definitely not
        // running. The LaunchAgent will start it later and its own version
        // guard will catch any mismatch against the already-migrated DB.
        Err(_) => return Ok(HandshakeResult::BrainAbsent),
    };

    // Non-2xx from brain means brain is *running* but unhealthy (e.g. half-
    // started, DB locked). Treat as Unknown rather than Absent: a startup
    // race against a half-started brain isn't the same as brain being off,
    // and we'd rather the daemon log a warning and proceed than silently
    // migrate as though brain weren't there.
    if !response.status().is_success() {
        return Ok(HandshakeResult::Unknown);
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
        // Bind an ephemeral port, record the number, then drop the listener
        // so the OS releases it before we probe. This guarantees nothing is
        // listening without relying on ambient port availability.
        let port = {
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            listener.local_addr().unwrap().port()
        };
        let result = check_brain_schema_compat(7, port).await.unwrap();
        assert!(matches!(
            result,
            HandshakeResult::BrainAbsent | HandshakeResult::Unknown
        ));
    }

    // ------------------------------------------------------------------
    // Capture-reliability F-16: schema version drift between daemon and brain.
    //
    // The v0.13.0 handshake incident was a brain built against schema v7 but
    // a daemon that had been migrated to v8 (or the reverse). The load-bearing
    // assertion is that `check_brain_schema_compat` returns Incompatible with
    // both versions named, so the CLI can print the remediation message.
    //
    // Tracking: docs/capture-reliability/09-test-matrix.md row F-16.
    // ------------------------------------------------------------------

    #[tokio::test]
    async fn brain_reports_different_version_returns_incompatible() {
        // Spin up a minimal HTTP server that answers /health with an
        // `expected_schema_version` that does not match the daemon's.
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 2048];
            let _ = stream.read(&mut buf).await.unwrap();
            let body = r#"{"status":"ok","expected_schema_version":6}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\
                 content-length: {}\r\nconnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).await.unwrap();
            stream.flush().await.unwrap();
        });

        let result = check_brain_schema_compat(7, addr.port()).await.unwrap();
        server.abort();
        let _ = server.await;

        match result {
            HandshakeResult::Incompatible {
                daemon_expects,
                brain_expects,
            } => {
                assert_eq!(daemon_expects, 7);
                assert_eq!(brain_expects, 6);
            }
            other => panic!("expected Incompatible, got {other:?}"),
        }
    }
}
