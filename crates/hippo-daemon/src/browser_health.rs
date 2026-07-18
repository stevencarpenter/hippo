//! Browser capture health semantics shared by `hippo doctor` and the watchdog.

use std::path::{Path, PathBuf};

/// Gecko extension ID from `extension/firefox/manifest.json`.
pub const FIREFOX_EXTENSION_ID: &str = "hippo-browser@local";

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

#[derive(Default)]
struct FirefoxProfileEntry {
    name: Option<String>,
    path: Option<String>,
    is_relative: bool,
}

fn parse_firefox_profiles_ini(content: &str) -> Vec<FirefoxProfileEntry> {
    let mut profiles = Vec::new();
    let mut current: Option<FirefoxProfileEntry> = None;

    for line in content.lines() {
        let line = line.trim();
        if let Some(section) = line.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
            if let Some(entry) = current.take().filter(|e| e.path.is_some()) {
                profiles.push(entry);
            }
            current = section
                .starts_with("Profile")
                .then_some(FirefoxProfileEntry {
                    is_relative: true,
                    ..FirefoxProfileEntry::default()
                });
            continue;
        }
        let Some(entry) = current.as_mut() else {
            continue;
        };
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        match key.trim() {
            "Name" => entry.name = Some(value.trim().to_string()),
            "Path" => entry.path = Some(value.trim().to_string()),
            "IsRelative" => entry.is_relative = value.trim() != "0",
            _ => {}
        }
    }
    if let Some(entry) = current.filter(|e| e.path.is_some()) {
        profiles.push(entry);
    }
    profiles
}

fn resolve_firefox_profile_path(firefox_root: &Path, entry: &FirefoxProfileEntry) -> PathBuf {
    let path = entry.path.as_deref().unwrap_or_default();
    if entry.is_relative {
        firefox_root.join(path)
    } else {
        PathBuf::from(path)
    }
}

/// Resolve the Firefox Developer Edition profile directory.
///
/// Strategy mirrors `mise.toml` `[tasks."install:ext"]` (Python configparser
/// block): prefer `Name=dev-edition-default`, then any profile whose `Path`
/// contains `dev-edition`.
pub fn firefox_dev_edition_profile_dir(home: &Path) -> Option<PathBuf> {
    let firefox_root = home.join("Library/Application Support/Firefox");
    let profiles_ini = firefox_root.join("profiles.ini");
    let content = std::fs::read_to_string(&profiles_ini).ok()?;
    let profiles = parse_firefox_profiles_ini(&content);

    for entry in &profiles {
        if entry.name.as_deref() == Some("dev-edition-default") {
            return Some(resolve_firefox_profile_path(&firefox_root, entry));
        }
    }
    for entry in &profiles {
        if entry
            .path
            .as_deref()
            .is_some_and(|path| path.contains("dev-edition"))
        {
            return Some(resolve_firefox_profile_path(&firefox_root, entry));
        }
    }
    None
}

pub fn firefox_extension_xpi_path(profile_dir: &Path) -> PathBuf {
    profile_dir.join(format!("extensions/{FIREFOX_EXTENSION_ID}.xpi"))
}

pub fn firefox_extension_xpi_installed(profile_dir: &Path) -> bool {
    firefox_extension_xpi_path(profile_dir).is_file()
}

/// True when `prefs.js` allows unsigned extension side-loads (required for local .xpi).
///
/// Matches the grep in `mise.toml` `[tasks."install:ext"]`:
/// `^user_pref\("xpinstall\.signatures\.required",[[:space:]]*false\)`
pub fn firefox_unsigned_install_allowed(prefs_js: &Path) -> bool {
    let Ok(content) = std::fs::read_to_string(prefs_js) else {
        return false;
    };
    content.lines().any(|line| {
        let line = line.trim();
        let Some(rest) = line.strip_prefix("user_pref(\"xpinstall.signatures.required\",") else {
            return false;
        };
        rest.trim().starts_with("false)")
    })
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

    #[test]
    fn profiles_ini_prefers_dev_edition_default_name() {
        let ini = r#"
[Install4F96D1932A9F858E]
Default=Profiles/abc123.default-release
Locked=1

[Profile0]
Name=default-release
IsRelative=1
Path=Profiles/abc123.default-release

[Profile1]
Name=dev-edition-default
IsRelative=1
Path=Profiles/xyz789.dev-edition-default
"#;
        let profiles = parse_firefox_profiles_ini(ini);
        assert_eq!(profiles.len(), 2);
        assert_eq!(profiles[1].name.as_deref(), Some("dev-edition-default"));
    }

    #[test]
    fn unsigned_install_pref_detects_false_value() {
        let tmp = tempfile::tempdir().unwrap();
        let prefs = tmp.path().join("prefs.js");
        std::fs::write(
            &prefs,
            r#"user_pref("xpinstall.signatures.required", false);"#,
        )
        .unwrap();
        assert!(firefox_unsigned_install_allowed(&prefs));
        std::fs::write(
            &prefs,
            r#"user_pref("xpinstall.signatures.required", true);"#,
        )
        .unwrap();
        assert!(!firefox_unsigned_install_allowed(&prefs));
        std::fs::write(
            &prefs,
            r#"// user_pref("xpinstall.signatures.required", false);"#,
        )
        .unwrap();
        assert!(!firefox_unsigned_install_allowed(&prefs));
    }

    #[test]
    fn extension_xpi_path_uses_gecko_id() {
        let profile = Path::new("/tmp/profile");
        assert_eq!(
            firefox_extension_xpi_path(profile),
            PathBuf::from("/tmp/profile/extensions/hippo-browser@local.xpi")
        );
    }

    #[test]
    fn dev_edition_profile_dir_resolves_from_profiles_ini() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path();
        let firefox_root = home.join("Library/Application Support/Firefox");
        let profile_rel = "Profiles/xyz.dev-edition-default";
        std::fs::create_dir_all(firefox_root.join(profile_rel)).unwrap();
        std::fs::write(
            firefox_root.join("profiles.ini"),
            format!(
                r#"[Profile0]
Name=dev-edition-default
IsRelative=1
Path={profile_rel}
"#
            ),
        )
        .unwrap();
        let resolved = firefox_dev_edition_profile_dir(home).unwrap();
        assert_eq!(resolved, firefox_root.join(profile_rel));
    }

    #[test]
    fn dev_edition_profile_dir_returns_configured_path_when_dir_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path();
        let firefox_root = home.join("Library/Application Support/Firefox");
        let profile_rel = "Profiles/missing.dev-edition-default";
        std::fs::create_dir_all(&firefox_root).unwrap();
        std::fs::write(
            firefox_root.join("profiles.ini"),
            format!(
                r#"[Profile0]
Name=dev-edition-default
IsRelative=1
Path={profile_rel}
"#
            ),
        )
        .unwrap();
        let resolved = firefox_dev_edition_profile_dir(home).unwrap();
        assert_eq!(resolved, firefox_root.join(profile_rel));
        assert!(!resolved.is_dir());
    }
}
