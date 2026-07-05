//! Browser capture health semantics shared by `hippo doctor` and the watchdog.

/// Extension heartbeats every 5 minutes (`extension/firefox/src/heartbeat.ts`).
/// Tolerate one missed tick plus launchd/SQLite jitter (same grace as I-4).
pub const BROWSER_HEARTBEAT_STALE_MS: i64 = 300_000 + 120_000;

/// Browser `source_health` WARN threshold — events younger than this are flowing.
pub const BROWSER_EVENT_WARN_SECS: i64 = 420;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BrowserExtensionConnectivity {
    FirefoxNotRunning,
    Connected,
    Disconnected,
    NeverConnected,
}

/// True when a Firefox main process is running (macOS + Linux binary names).
pub fn firefox_running() -> bool {
    ["firefox", "firefox-bin"].iter().any(|name| {
        std::process::Command::new("pgrep")
            .args(["-qx", name])
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    })
}

/// Returns whether `last_heartbeat_ts` is within the extension cadence window.
pub fn browser_heartbeat_fresh(heartbeat_ts_ms: i64, now_ms: i64) -> bool {
    now_ms - heartbeat_ts_ms <= BROWSER_HEARTBEAT_STALE_MS
}

pub fn browser_extension_connectivity(
    firefox_running: bool,
    heartbeat_age_secs: Option<i64>,
    event_age_secs: i64,
) -> BrowserExtensionConnectivity {
    if !firefox_running {
        return BrowserExtensionConnectivity::FirefoxNotRunning;
    }
    // Active event flow is proof of life — do not label "disconnected" while
    // events are landing within the normal WARN window.
    if event_age_secs <= BROWSER_EVENT_WARN_SECS {
        return BrowserExtensionConnectivity::Connected;
    }
    match heartbeat_age_secs {
        None => BrowserExtensionConnectivity::NeverConnected,
        Some(age) if age * 1000 <= BROWSER_HEARTBEAT_STALE_MS => {
            BrowserExtensionConnectivity::Connected
        }
        _ => BrowserExtensionConnectivity::Disconnected,
    }
}

pub fn browser_capture_state_label(
    connectivity: BrowserExtensionConnectivity,
    event_age_secs: i64,
    probe_ok: Option<i64>,
) -> &'static str {
    match connectivity {
        BrowserExtensionConnectivity::FirefoxNotRunning => "Firefox not running",
        BrowserExtensionConnectivity::NeverConnected => "extension never heartbeated",
        BrowserExtensionConnectivity::Disconnected => "extension disconnected from daemon",
        BrowserExtensionConnectivity::Connected => {
            if event_age_secs <= BROWSER_EVENT_WARN_SECS {
                "events flowing normally"
            } else if probe_ok == Some(0) {
                "extension connected; synthetic probe failing"
            } else if probe_ok == Some(1) {
                "extension connected; probe OK, user events idle"
            } else {
                "extension connected; event cadence stale"
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heartbeat_fresh_within_cadence_window() {
        let now = 1_000_000_i64;
        // 3 minutes ago — between 5-minute ticks, still fresh at 7-minute threshold.
        assert!(browser_heartbeat_fresh(now - 180_000, now));
    }

    #[test]
    fn heartbeat_stale_beyond_cadence_window() {
        let now = 1_000_000_i64;
        assert!(!browser_heartbeat_fresh(now - 500_000, now));
    }

    #[test]
    fn fresh_events_imply_connected_even_with_stale_heartbeat() {
        assert_eq!(
            browser_extension_connectivity(true, Some(300), 60),
            BrowserExtensionConnectivity::Connected,
            "event flow should not be labelled disconnected"
        );
    }

    #[test]
    fn stale_events_and_mid_cadence_heartbeat_are_connected() {
        assert_eq!(
            browser_extension_connectivity(true, Some(180), 24 * 60),
            BrowserExtensionConnectivity::Connected
        );
    }

    #[test]
    fn stale_events_and_stale_heartbeat_are_disconnected() {
        assert_eq!(
            browser_extension_connectivity(true, Some(600), 24 * 60),
            BrowserExtensionConnectivity::Disconnected
        );
    }
}
