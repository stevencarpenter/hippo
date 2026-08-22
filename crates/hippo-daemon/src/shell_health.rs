//! Shell capture health semantics shared by `hippo doctor` and the watchdog.
//!
//! I-1 (shell liveness) and I-8 (probe freshness, for the `shell` source)
//! both need to distinguish "the user simply isn't typing right now" from
//! "the capture pipe broke while the user was active". `source_health`'s
//! `shell.last_event_ts` cannot answer that alone: the synthetic probe
//! (`com.hippo.probe`, every 5 min) rides the exact same insert path as a
//! real shell command and advances `last_event_ts` on every successful run,
//! regardless of whether a human typed anything. System sleep produces the
//! same signature as a broken daemon on that column too: neither probes nor
//! real events land while the machine is asleep, and once it wakes the
//! watchdog sees the same "nothing landed recently" shape either way.
//!
//! The disambiguator already recorded on every event row is `probe_tag`:
//! real shell commands never carry one, probe injections always do.
//! Querying for the most recent *real* (`probe_tag IS NULL`) shell event
//! gives a "was a human here recently" signal the probe cannot pollute,
//! mirroring how the opencode/Codex/Cursor idle probes in `commands.rs`
//! check the recency of their own genuine session files rather than
//! anything hippo itself writes.

use rusqlite::Connection;

/// Idle window for the "was a human recently at the keyboard" signal used by
/// I-1 and the shell arm of I-8. Matches the 10-minute window the
/// opencode/Codex/Cursor idle probes use (`commands.rs::IDLE_WINDOW_SECS`)
/// for consistency, and is deliberately wider than `SHELL_LIVENESS_STALE_MS`
/// (7 min) so the two checks answer different questions: a source can go
/// quiet for the shorter I-1 window while still counting as "recently
/// active" here, so a genuine break (real commands stop landing and the
/// probe also stops landing) still alarms instead of being swallowed by the
/// idle carve-out.
pub const SHELL_IDLE_WINDOW_MS: i64 = 600_000;

/// Hard backstop on how long the idle carve-out may suppress I-1, the shell
/// arm of I-8, and doctor's shell staleness check. Ordinary idleness — sleep,
/// stepping away, a long weekend — does not plausibly last this long; without
/// this cap a sustained outage where `probe_ok` is frozen at a stale `1`
/// (the launchd job itself stopped running, e.g. issue #263's follow-up
/// incident class) would be suppressed as "idle" forever and could never
/// reach `Fail`. Mirrors the fixed 24 h backstop I-9 uses for fallback file
/// age (`docs/capture/architecture.md`).
pub const SHELL_IDLE_SUPPRESSION_BACKSTOP_MS: i64 = 86_400_000;

/// True when a real (non-probe) `shell` event has landed within `window_ms`
/// of `now_ms`. Returns `Ok(false)` (not an error) when no such event exists
/// yet, e.g. a fresh install that has only seen probe traffic so far: that
/// is correctly "idle", not "broken".
pub fn shell_real_activity_recent(
    conn: &Connection,
    now_ms: i64,
    window_ms: i64,
) -> rusqlite::Result<bool> {
    let last_real_ts: Option<i64> = conn.query_row(
        "SELECT MAX(timestamp) FROM events WHERE source_kind = 'shell' AND probe_tag IS NULL",
        [],
        |row| row.get(0),
    )?;
    Ok(last_real_ts.is_some_and(|ts| now_ms - ts <= window_ms))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn open_test_conn(dir: &TempDir) -> Connection {
        let path = dir.path().join("shell_health_test.db");
        hippo_core::storage::open_db(&path).unwrap()
    }

    /// Minimal fixture: one `sessions` row plus a raw `events` insert so the
    /// test can control `timestamp`, `source_kind`, and `probe_tag` directly
    /// without going through the full `ShellEvent` capture path.
    fn insert_event(conn: &Connection, timestamp: i64, source_kind: &str, probe_tag: Option<&str>) {
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, start_time, shell, hostname, username) \
             VALUES (1, 0, 'zsh', 'test-host', 'test-user')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO events (session_id, timestamp, command, duration_ms, cwd, hostname, shell, source_kind, probe_tag) \
             VALUES (1, ?1, 'echo hi', 0, '/tmp', 'test-host', 'zsh', ?2, ?3)",
            rusqlite::params![timestamp, source_kind, probe_tag],
        )
        .unwrap();
    }

    const NOW: i64 = 1_700_000_000_000i64;

    #[test]
    fn recent_real_shell_event_is_activity() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        insert_event(&conn, NOW - 60_000, "shell", None);
        assert!(shell_real_activity_recent(&conn, NOW, SHELL_IDLE_WINDOW_MS).unwrap());
    }

    #[test]
    fn stale_real_shell_event_is_not_activity() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        insert_event(&conn, NOW - SHELL_IDLE_WINDOW_MS - 1_000, "shell", None);
        assert!(!shell_real_activity_recent(&conn, NOW, SHELL_IDLE_WINDOW_MS).unwrap());
    }

    #[test]
    fn recent_probe_only_event_is_not_activity() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        // Only a synthetic probe landed recently, no real command, so this
        // must NOT count as "a human was here".
        insert_event(&conn, NOW - 60_000, "shell", Some("probe-uuid-1"));
        assert!(!shell_real_activity_recent(&conn, NOW, SHELL_IDLE_WINDOW_MS).unwrap());
    }

    #[test]
    fn recent_real_event_wins_over_older_probe_noise() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        insert_event(&conn, NOW - 500_000, "shell", Some("probe-uuid-1"));
        insert_event(&conn, NOW - 30_000, "shell", None);
        assert!(shell_real_activity_recent(&conn, NOW, SHELL_IDLE_WINDOW_MS).unwrap());
    }

    #[test]
    fn claude_tool_events_do_not_count_as_shell_activity() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        // A claude-tool event is real activity, but on a different
        // source_health row: it must not mask genuine shell silence.
        insert_event(&conn, NOW - 60_000, "claude-tool", None);
        assert!(!shell_real_activity_recent(&conn, NOW, SHELL_IDLE_WINDOW_MS).unwrap());
    }

    #[test]
    fn no_events_at_all_is_not_activity() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        assert!(!shell_real_activity_recent(&conn, NOW, SHELL_IDLE_WINDOW_MS).unwrap());
    }

    /// Boundary: an event exactly at the window edge (`<=`) still counts as
    /// recent activity.
    #[test]
    fn event_exactly_at_window_edge_is_activity() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        insert_event(&conn, NOW - SHELL_IDLE_WINDOW_MS, "shell", None);
        assert!(shell_real_activity_recent(&conn, NOW, SHELL_IDLE_WINDOW_MS).unwrap());
    }

    /// Boundary: one millisecond past the window edge is no longer recent.
    #[test]
    fn event_one_ms_past_window_edge_is_not_activity() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        insert_event(&conn, NOW - SHELL_IDLE_WINDOW_MS - 1, "shell", None);
        assert!(!shell_real_activity_recent(&conn, NOW, SHELL_IDLE_WINDOW_MS).unwrap());
    }

    /// A missing `events` table (e.g. schema not yet migrated, or a
    /// corrupted DB) makes the underlying query fail. Callers fail open via
    /// `.unwrap_or(true)` — this test just pins down that the function
    /// itself surfaces `Err` rather than silently returning `Ok(false)`, so
    /// callers actually reach their fail-open branch instead of a
    /// misleadingly confident `Ok(false)`.
    #[test]
    fn query_error_surfaces_as_err_not_a_false_positive_ok() {
        let dir = TempDir::new().unwrap();
        let conn = open_test_conn(&dir);
        conn.execute("DROP TABLE events", []).unwrap();
        assert!(shell_real_activity_recent(&conn, NOW, SHELL_IDLE_WINDOW_MS).is_err());
    }
}
