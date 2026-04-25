use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct HippoConfig {
    #[serde(default)]
    pub lmstudio: LmStudioConfig,
    #[serde(default)]
    pub models: ModelsConfig,
    #[serde(default)]
    pub daemon: DaemonConfig,
    #[serde(default)]
    pub brain: BrainConfig,
    #[serde(default)]
    pub storage: StorageConfig,
    #[serde(default)]
    pub browser: BrowserConfig,
    #[serde(default)]
    pub telemetry: TelemetryConfig,
    #[serde(default)]
    pub github: GithubConfig,
    #[serde(default)]
    pub watchdog: WatchdogConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LmStudioConfig {
    #[serde(default = "default_lmstudio_base_url")]
    pub base_url: String,
}

fn default_lmstudio_base_url() -> String {
    "http://localhost:1234/v1".to_string()
}

impl Default for LmStudioConfig {
    fn default() -> Self {
        Self {
            base_url: default_lmstudio_base_url(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ModelsConfig {
    #[serde(default)]
    pub enrichment: String,
    #[serde(default)]
    pub enrichment_bulk: String,
    #[serde(default)]
    pub query: String,
    #[serde(default)]
    pub embedding: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DaemonConfig {
    #[serde(default = "default_flush_interval_ms")]
    pub flush_interval_ms: u64,
    #[serde(default = "default_flush_batch_size")]
    pub flush_batch_size: usize,
    #[serde(default = "default_socket_timeout_ms")]
    pub socket_timeout_ms: u64,
    #[serde(default = "default_output_head_lines")]
    pub output_head_lines: usize,
    #[serde(default = "default_output_tail_lines")]
    pub output_tail_lines: usize,
}

fn default_flush_interval_ms() -> u64 {
    100
}
fn default_flush_batch_size() -> usize {
    50
}
fn default_socket_timeout_ms() -> u64 {
    100
}
fn default_output_head_lines() -> usize {
    50
}
fn default_output_tail_lines() -> usize {
    100
}

impl Default for DaemonConfig {
    fn default() -> Self {
        Self {
            flush_interval_ms: default_flush_interval_ms(),
            flush_batch_size: default_flush_batch_size(),
            socket_timeout_ms: default_socket_timeout_ms(),
            output_head_lines: default_output_head_lines(),
            output_tail_lines: default_output_tail_lines(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrainConfig {
    #[serde(default = "default_brain_port")]
    pub port: u16,
    #[serde(default = "default_poll_interval_secs")]
    pub poll_interval_secs: u64,
    #[serde(default = "default_enrichment_batch_size")]
    pub enrichment_batch_size: usize,
    #[serde(default = "default_max_queue_depth")]
    pub max_queue_depth: usize,
    #[serde(default = "default_max_events_per_chunk")]
    pub max_events_per_chunk: usize,
    #[serde(default = "default_session_stale_secs")]
    pub session_stale_secs: u64,
}

fn default_brain_port() -> u16 {
    9175
}
fn default_poll_interval_secs() -> u64 {
    5
}
fn default_enrichment_batch_size() -> usize {
    30
}
fn default_max_queue_depth() -> usize {
    100
}
fn default_max_events_per_chunk() -> usize {
    30
}
fn default_session_stale_secs() -> u64 {
    120
}

impl Default for BrainConfig {
    fn default() -> Self {
        Self {
            port: default_brain_port(),
            poll_interval_secs: default_poll_interval_secs(),
            enrichment_batch_size: default_enrichment_batch_size(),
            max_queue_depth: default_max_queue_depth(),
            max_events_per_chunk: default_max_events_per_chunk(),
            session_stale_secs: default_session_stale_secs(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StorageConfig {
    #[serde(default = "default_data_dir")]
    pub data_dir: PathBuf,
    #[serde(default = "default_config_dir")]
    pub config_dir: PathBuf,
}

/// XDG-based data directory. We deliberately use ~/.local/share (not macOS's
/// ~/Library/Application Support) so all components agree on a single path.
fn default_data_dir() -> PathBuf {
    let base = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .or_else(|| dirs::home_dir().map(|h| h.join(".local/share")))
        .unwrap_or_else(|| PathBuf::from(".local/share"));
    base.join("hippo")
}

/// XDG-based config directory. Same rationale as default_data_dir.
fn default_config_dir() -> PathBuf {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| dirs::home_dir().map(|h| h.join(".config")))
        .unwrap_or_else(|| PathBuf::from(".config"));
    base.join("hippo")
}

impl Default for StorageConfig {
    fn default() -> Self {
        Self {
            data_dir: default_data_dir(),
            config_dir: default_config_dir(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserConfig {
    #[serde(default = "default_browser_enabled")]
    pub enabled: bool,
    #[serde(default = "default_min_dwell_ms")]
    pub min_dwell_ms: u64,
    #[serde(default = "default_scroll_depth_threshold")]
    pub scroll_depth_threshold: f32,
    #[serde(default = "default_dedup_window_minutes")]
    pub dedup_window_minutes: u64,
    #[serde(default = "default_correlation_window_ms")]
    pub correlation_window_ms: u64,
    #[serde(default = "default_browser_stale_session_secs")]
    pub stale_session_secs: u64,
    #[serde(default)]
    pub allowlist: BrowserAllowlist,
    #[serde(default)]
    pub url_redaction: BrowserUrlRedaction,
    /// Long-dwell bypass threshold (ms). Events with dwell_ms >= this value bypass
    /// the scroll-depth filter in the Python brain enrichment layer. Stored here so
    /// config.toml is the single source of truth — the daemon does not enforce this;
    /// the brain reads it at startup via `[browser] long_dwell_bypass_ms`.
    #[serde(default = "default_long_dwell_bypass_ms")]
    pub long_dwell_bypass_ms: u64,
    /// Domain used for synthetic browser probes. Always allowlisted by the NM host
    /// regardless of `allowlist.domains`. Must not be a real domain that Firefox
    /// would ever visit so probe rows cannot be confused with real visits.
    #[serde(default = "default_probe_domain")]
    pub probe_domain: String,
}

fn default_browser_enabled() -> bool {
    true
}
fn default_min_dwell_ms() -> u64 {
    3000
}
fn default_scroll_depth_threshold() -> f32 {
    0.15
}
fn default_dedup_window_minutes() -> u64 {
    30
}
fn default_correlation_window_ms() -> u64 {
    300_000
}
fn default_browser_stale_session_secs() -> u64 {
    60
}
fn default_long_dwell_bypass_ms() -> u64 {
    120_000
}
fn default_probe_domain() -> String {
    "probe.hippo.local".to_string()
}

impl Default for BrowserConfig {
    fn default() -> Self {
        Self {
            enabled: default_browser_enabled(),
            min_dwell_ms: default_min_dwell_ms(),
            scroll_depth_threshold: default_scroll_depth_threshold(),
            dedup_window_minutes: default_dedup_window_minutes(),
            correlation_window_ms: default_correlation_window_ms(),
            stale_session_secs: default_browser_stale_session_secs(),
            allowlist: BrowserAllowlist::default(),
            url_redaction: BrowserUrlRedaction::default(),
            long_dwell_bypass_ms: default_long_dwell_bypass_ms(),
            probe_domain: default_probe_domain(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserAllowlist {
    #[serde(default = "default_browser_allowlist_domains")]
    pub domains: Vec<String>,
}

fn default_browser_allowlist_domains() -> Vec<String> {
    vec![
        // Code forges & sharing
        "github.com".to_string(),
        "github.io".to_string(),
        "gitlab.com".to_string(),
        "bitbucket.org".to_string(),
        // Package registries
        "crates.io".to_string(),
        "npmjs.com".to_string(),
        "pypi.org".to_string(),
        "mvnrepository.com".to_string(),
        "pkg.go.dev".to_string(),
        "rubygems.org".to_string(),
        // Language & framework docs
        "docs.rs".to_string(),
        "doc.rust-lang.org".to_string(),
        "rust-lang.org".to_string(),
        "docs.python.org".to_string(),
        "python.org".to_string(),
        "swift.org".to_string(),
        "developer.mozilla.org".to_string(),
        "docs.astral.sh".to_string(),
        "typescriptlang.org".to_string(),
        "learn.microsoft.com".to_string(),
        "kubernetes.io".to_string(),
        "go.dev".to_string(),
        "nodejs.org".to_string(),
        "ziglang.org".to_string(),
        // AI & ML
        "anthropic.com".to_string(),
        "openai.com".to_string(),
        "huggingface.co".to_string(),
        "arxiv.org".to_string(),
        "lmstudio.ai".to_string(),
        // System & OS docs
        "man7.org".to_string(),
        "wiki.archlinux.org".to_string(),
        // Database & infra docs
        "sqlite.org".to_string(),
        "postgresql.org".to_string(),
        "redis.io".to_string(),
        "docker.com".to_string(),
        // Q&A & community
        "stackoverflow.com".to_string(),
        "stackexchange.com".to_string(),
        "reddit.com".to_string(),
        "news.ycombinator.com".to_string(),
        "lobste.rs".to_string(),
        // Developer content
        "medium.com".to_string(),
        "dev.to".to_string(),
        "hackernoon.com".to_string(),
        "substack.com".to_string(),
    ]
}

impl Default for BrowserAllowlist {
    fn default() -> Self {
        Self {
            domains: default_browser_allowlist_domains(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserUrlRedaction {
    #[serde(default = "default_browser_strip_params")]
    pub strip_params: Vec<String>,
}

fn default_browser_strip_params() -> Vec<String> {
    vec![
        "token".to_string(),
        "api_key".to_string(),
        "password".to_string(),
        "secret".to_string(),
        "auth".to_string(),
        "session".to_string(),
        "key".to_string(),
        "sig".to_string(),
    ]
}

impl Default for BrowserUrlRedaction {
    fn default() -> Self {
        Self {
            strip_params: default_browser_strip_params(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelemetryConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_telemetry_endpoint")]
    pub endpoint: String,
}

fn default_telemetry_endpoint() -> String {
    "http://localhost:4317".to_string()
}

impl Default for TelemetryConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            endpoint: default_telemetry_endpoint(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct GithubConfig {
    pub enabled: bool,
    pub poll_interval_secs: u64,
    pub tight_poll_interval_secs: u64,
    pub watchlist_ttl_secs: u64,
    pub log_excerpt_max_bytes: usize,
    pub watched_repos: Vec<String>,
    pub token_env: String,
    pub lessons: LessonsConfig,
}

fn default_github_token_env() -> String {
    "HIPPO_GITHUB_TOKEN".to_string()
}
fn default_poll_interval_secs_github() -> u64 {
    300
}
fn default_tight_poll_interval_secs() -> u64 {
    45
}
fn default_watchlist_ttl_secs() -> u64 {
    1200
}
fn default_log_excerpt_max_bytes() -> usize {
    51_200
}

impl Default for GithubConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            poll_interval_secs: default_poll_interval_secs_github(),
            tight_poll_interval_secs: default_tight_poll_interval_secs(),
            watchlist_ttl_secs: default_watchlist_ttl_secs(),
            log_excerpt_max_bytes: default_log_excerpt_max_bytes(),
            watched_repos: vec![],
            token_env: default_github_token_env(),
            lessons: LessonsConfig::default(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct LessonsConfig {
    pub cluster_window_days: u32,
    pub min_occurrences: u32,
    pub path_prefix_segments: u32,
}

impl Default for LessonsConfig {
    fn default() -> Self {
        Self {
            cluster_window_days: 30,
            min_occurrences: 2,
            path_prefix_segments: 2,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WatchdogConfig {
    #[serde(default = "default_watchdog_enabled")]
    pub enabled: bool,
    #[serde(default = "default_alarm_rate_limit_minutes")]
    pub alarm_rate_limit_minutes: u64,
    #[serde(default)]
    pub notify_macos: bool,
}

fn default_watchdog_enabled() -> bool {
    true
}
fn default_alarm_rate_limit_minutes() -> u64 {
    15
}

impl Default for WatchdogConfig {
    fn default() -> Self {
        Self {
            enabled: default_watchdog_enabled(),
            alarm_rate_limit_minutes: default_alarm_rate_limit_minutes(),
            notify_macos: false,
        }
    }
}

impl HippoConfig {
    pub fn load(path: &Path) -> Result<Self> {
        // nosemgrep
        let content = match std::fs::read_to_string(path) {
            Ok(c) => c,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Self::default());
            }
            Err(e) => {
                return Err(anyhow::anyhow!(
                    "failed to read config from {}: {}",
                    path.display(),
                    e
                ));
            }
        };
        let config: Self = toml::from_str(&content)
            .map_err(|e| anyhow::anyhow!("failed to parse config at {}: {}", path.display(), e))?;
        Ok(config)
    }

    pub fn load_default() -> Result<Self> {
        let config_path = default_config_dir().join("config.toml");
        Self::load(&config_path)
    }

    pub fn redact_path(&self) -> PathBuf {
        self.storage.config_dir.join("redact.toml")
    }

    pub fn db_path(&self) -> PathBuf {
        self.storage.data_dir.join("hippo.db")
    }

    pub fn socket_path(&self) -> PathBuf {
        socket_path(&self.storage.data_dir)
    }

    pub fn fallback_dir(&self) -> PathBuf {
        self.storage.data_dir.join("fallback")
    }

    pub fn log_path(&self) -> PathBuf {
        self.storage.data_dir.join("hippo.log")
    }
}

pub fn socket_path(data_dir: &Path) -> PathBuf {
    let candidate = data_dir.join("daemon.sock");
    if candidate.as_os_str().len() > 100 {
        std::env::temp_dir().join("hippo-daemon.sock")
    } else {
        candidate
    }
}

// --- Redaction config ---

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RedactConfig {
    pub patterns: Vec<RedactPattern>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RedactPattern {
    pub name: String,
    pub regex: String,
    #[serde(default = "default_replacement")]
    pub replacement: String,
}

fn default_replacement() -> String {
    "[REDACTED]".to_string()
}

impl RedactConfig {
    pub fn load(path: &Path) -> Result<Self> {
        // nosemgrep
        let content = match std::fs::read_to_string(path) {
            Ok(c) => c,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Self::builtin());
            }
            Err(e) => {
                return Err(anyhow::anyhow!(
                    "failed to read redact config from {}: {}",
                    path.display(),
                    e
                ));
            }
        };
        let config: Self = toml::from_str(&content).map_err(|e| {
            anyhow::anyhow!("failed to parse redact config at {}: {}", path.display(), e)
        })?;
        Ok(config)
    }

    pub fn load_default() -> Result<Self> {
        let redact_path = default_config_dir().join("redact.toml");
        Self::load(&redact_path)
    }

    pub fn builtin() -> Self {
        Self {
            patterns: vec![
                RedactPattern {
                    name: "aws_access_key".to_string(),
                    regex: r"AKIA[0-9A-Z]{16}".to_string(),
                    replacement: "[REDACTED]".to_string(),
                },
                RedactPattern {
                    name: "github_pat".to_string(),
                    regex: r"ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82}".to_string(),
                    replacement: "[REDACTED]".to_string(),
                },
                RedactPattern {
                    name: "generic_secret_assignment".to_string(),
                    regex: r"(?i)(api[_-]?key|api[_-]?token|access[_-]?token|auth[_-]?token|secret[_-]?key|private[_-]?key|password)\s*[=:]\s*\S{8,}".to_string(),
                    replacement: "[REDACTED]".to_string(),
                },
                RedactPattern {
                    name: "jwt".to_string(),
                    regex: r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+".to_string(),
                    replacement: "[REDACTED]".to_string(),
                },
                RedactPattern {
                    name: "bearer_header".to_string(),
                    regex: r"(?i)authorization:\s*bearer\s+\S+".to_string(),
                    replacement: "[REDACTED]".to_string(),
                },
                RedactPattern {
                    name: "private_key_pem".to_string(),
                    regex: r"-----BEGIN [A-Z ]*PRIVATE KEY-----".to_string(),
                    replacement: "[REDACTED]".to_string(),
                },
            ],
        }
    }
}

pub const ENV_ALLOWLIST: &[&str] = &[
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "COLORTERM",
    "TERM_PROGRAM",
    "LANG",
    "LC_ALL",
    "PATH",
    "PWD",
    "OLDPWD",
    "SHLVL",
    "HOSTNAME",
    "EDITOR",
    "VISUAL",
    "TMPDIR",
    "VIRTUAL_ENV",
    "CONDA_DEFAULT_ENV",
    "NODE_ENV",
    "RAILS_ENV",
    "APP_ENV",
    "AWS_PROFILE",
    "AWS_DEFAULT_REGION",
    "KUBECONFIG",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "HIPPO_SESSION_ID",
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        let config = HippoConfig::default();
        assert_eq!(config.lmstudio.base_url, "http://localhost:1234/v1");
        assert_eq!(config.daemon.flush_interval_ms, 100);
        assert_eq!(config.daemon.flush_batch_size, 50);
        assert_eq!(config.brain.port, 9175);
        assert_eq!(config.brain.poll_interval_secs, 5);
    }

    #[test]
    fn test_config_from_toml() {
        let toml_str = r#"
[lmstudio]
base_url = "http://custom:5678/v1"

[daemon]
flush_interval_ms = 200

[brain]
port = 8080
"#;
        let config: HippoConfig = toml::from_str(toml_str).unwrap();
        assert_eq!(config.lmstudio.base_url, "http://custom:5678/v1");
        assert_eq!(config.daemon.flush_interval_ms, 200);
        assert_eq!(config.brain.port, 8080);
        // Defaults for unspecified fields
        assert_eq!(config.daemon.flush_batch_size, 50);
    }

    #[test]
    fn test_missing_config_returns_default() {
        let config = HippoConfig::load(Path::new("/nonexistent/path/config.toml")).unwrap();
        assert_eq!(config.lmstudio.base_url, "http://localhost:1234/v1");
    }

    #[test]
    fn test_builtin_redact_patterns() {
        let config = RedactConfig::builtin();
        assert_eq!(config.patterns.len(), 6);
        let names: Vec<&str> = config.patterns.iter().map(|p| p.name.as_str()).collect();
        assert!(names.contains(&"aws_access_key"));
        assert!(names.contains(&"github_pat"));
        assert!(names.contains(&"jwt"));
        assert!(names.contains(&"bearer_header"));
        assert!(names.contains(&"private_key_pem"));
        assert!(names.contains(&"generic_secret_assignment"));
    }

    #[test]
    fn test_socket_path_length_fallback() {
        // Short path should use data_dir
        let short = PathBuf::from("/tmp/hippo");
        let result = socket_path(&short);
        assert_eq!(result, short.join("daemon.sock"));

        // Long path should fall back to TMPDIR
        let long_dir = PathBuf::from("/".to_string() + &"a".repeat(120));
        let result = socket_path(&long_dir);
        assert!(result.ends_with("hippo-daemon.sock"));
        assert!(result.as_os_str().len() <= 104);
    }

    #[test]
    fn test_env_allowlist_contains_essentials() {
        assert!(ENV_ALLOWLIST.contains(&"HOME"));
        assert!(ENV_ALLOWLIST.contains(&"PATH"));
        assert!(ENV_ALLOWLIST.contains(&"PWD"));
        assert!(ENV_ALLOWLIST.contains(&"SHELL"));
        assert!(ENV_ALLOWLIST.contains(&"HIPPO_SESSION_ID"));
    }

    #[test]
    fn test_load_valid_toml_file() {
        let dir = tempfile::tempdir().unwrap();
        let config_path = dir.path().join("config.toml");
        std::fs::write(
            &config_path,
            r#"
[lmstudio]
base_url = "http://custom:9999/v1"

[daemon]
flush_interval_ms = 500
flush_batch_size = 100

[brain]
port = 7777
poll_interval_secs = 10
"#,
        )
        .unwrap();
        let config = HippoConfig::load(&config_path).unwrap();
        assert_eq!(config.lmstudio.base_url, "http://custom:9999/v1");
        assert_eq!(config.daemon.flush_interval_ms, 500);
        assert_eq!(config.daemon.flush_batch_size, 100);
        assert_eq!(config.brain.port, 7777);
        assert_eq!(config.brain.poll_interval_secs, 10);
    }

    #[test]
    fn test_load_default_returns_ok() {
        // load_default points at ~/.config/hippo/config.toml which likely
        // does not exist in CI, so it should fall through to default.
        let config = HippoConfig::load_default().unwrap();
        assert_eq!(config.daemon.flush_interval_ms, 100);
    }

    #[test]
    fn test_db_path() {
        let config = HippoConfig::default();
        let db = config.db_path();
        assert!(db.ends_with("hippo.db"));
        assert!(db.starts_with(&config.storage.data_dir));
    }

    #[test]
    fn test_socket_path_method() {
        let config = HippoConfig::default();
        let sock = config.socket_path();
        assert!(
            sock.to_string_lossy().contains("daemon.sock")
                || sock.to_string_lossy().contains("hippo-daemon.sock")
        );
    }

    #[test]
    fn test_fallback_dir() {
        let config = HippoConfig::default();
        let fb = config.fallback_dir();
        assert!(fb.ends_with("fallback"));
        assert!(fb.starts_with(&config.storage.data_dir));
    }

    #[test]
    fn test_log_path() {
        let config = HippoConfig::default();
        let log = config.log_path();
        assert!(log.ends_with("hippo.log"));
        assert!(log.starts_with(&config.storage.data_dir));
    }

    #[test]
    fn test_redact_path() {
        let config = HippoConfig::default();
        let redact = config.redact_path();
        assert!(redact.ends_with("redact.toml"));
        assert!(redact.starts_with(&config.storage.config_dir));
    }

    #[test]
    fn test_redact_pattern_default_replacement() {
        // Exercises the default_replacement() serde default function
        let toml_str = r#"
[[patterns]]
name = "test_pat"
regex = "foo"
"#;
        let config: RedactConfig = toml::from_str(toml_str).unwrap();
        assert_eq!(config.patterns.len(), 1);
        assert_eq!(config.patterns[0].replacement, "[REDACTED]");
    }

    #[test]
    fn test_redact_config_toml_roundtrip() {
        let toml_str = r#"
[[patterns]]
name = "custom"
regex = "secret_\\w+"
replacement = "***"
"#;
        let config: RedactConfig = toml::from_str(toml_str).unwrap();
        assert_eq!(config.patterns.len(), 1);
        assert_eq!(config.patterns[0].name, "custom");
        assert_eq!(config.patterns[0].replacement, "***");
    }

    #[test]
    fn test_redact_load_missing_returns_builtin() {
        let config = RedactConfig::load(Path::new("/nonexistent/path/redact.toml")).unwrap();
        assert_eq!(config.patterns.len(), 6);
    }

    #[test]
    fn test_redact_load_reads_file() {
        let dir = tempfile::tempdir().unwrap();
        let redact_path = dir.path().join("redact.toml");
        std::fs::write(
            &redact_path,
            r#"
[[patterns]]
name = "internal_token"
regex = "internal_[A-Z0-9]{8}"
replacement = "***"
"#,
        )
        .unwrap();

        let config = RedactConfig::load(&redact_path).unwrap();
        assert_eq!(config.patterns.len(), 1);
        assert_eq!(config.patterns[0].name, "internal_token");
        assert_eq!(config.patterns[0].replacement, "***");
    }

    #[test]
    fn test_redact_load_default_returns_ok() {
        let config = RedactConfig::load_default().unwrap();
        assert!(!config.patterns.is_empty());
    }

    #[test]
    fn test_telemetry_defaults() {
        let config = HippoConfig::default();
        assert!(!config.telemetry.enabled);
        assert_eq!(config.telemetry.endpoint, "http://localhost:4317");
    }

    #[test]
    fn test_telemetry_from_toml() {
        let toml_str = r#"
[telemetry]
enabled = true
endpoint = "http://collector:4317"
"#;
        let config: HippoConfig = toml::from_str(toml_str).unwrap();
        assert!(config.telemetry.enabled);
        assert_eq!(config.telemetry.endpoint, "http://collector:4317");
    }

    #[test]
    fn test_browser_config_defaults() {
        let config = BrowserConfig::default();
        assert!(config.enabled);
        assert_eq!(config.min_dwell_ms, 3000);
        assert!((config.scroll_depth_threshold - 0.15).abs() < f32::EPSILON);
        assert_eq!(config.dedup_window_minutes, 30);
        assert_eq!(config.correlation_window_ms, 300_000);
        assert_eq!(config.stale_session_secs, 60);
        assert_eq!(config.long_dwell_bypass_ms, 120_000);
        assert!(config.allowlist.domains.contains(&"github.com".to_string()));
        assert!(
            config
                .allowlist
                .domains
                .contains(&"stackoverflow.com".to_string())
        );
        assert!(
            config
                .allowlist
                .domains
                .contains(&"rust-lang.org".to_string())
        );
        assert!(
            config
                .allowlist
                .domains
                .contains(&"anthropic.com".to_string())
        );
        assert!(config.allowlist.domains.contains(&"arxiv.org".to_string()));
        assert!(config.allowlist.domains.contains(&"sqlite.org".to_string()));
        assert!(config.allowlist.domains.contains(&"lobste.rs".to_string()));
        assert!(
            config
                .url_redaction
                .strip_params
                .contains(&"token".to_string())
        );
    }

    #[test]
    fn test_browser_config_from_toml() {
        let toml_str = r#"
[browser]
enabled = false
min_dwell_ms = 5000
long_dwell_bypass_ms = 300000

[browser.allowlist]
domains = ["example.com", "docs.rs"]

[browser.url_redaction]
strip_params = ["secret", "nonce"]
"#;
        let config: HippoConfig = toml::from_str(toml_str).unwrap();
        assert!(!config.browser.enabled);
        assert_eq!(config.browser.min_dwell_ms, 5000);
        // Unspecified fields keep defaults
        assert!((config.browser.scroll_depth_threshold - 0.15).abs() < f32::EPSILON);
        assert_eq!(config.browser.dedup_window_minutes, 30);
        assert_eq!(config.browser.correlation_window_ms, 300_000);
        assert_eq!(config.browser.stale_session_secs, 60);
        // Explicitly set to non-default to prove custom values round-trip
        assert_eq!(config.browser.long_dwell_bypass_ms, 300_000);
        // Overridden sub-sections
        assert_eq!(
            config.browser.allowlist.domains,
            vec!["example.com", "docs.rs"]
        );
        assert_eq!(
            config.browser.url_redaction.strip_params,
            vec!["secret", "nonce"]
        );
    }

    #[test]
    fn parses_github_section() {
        let toml = r#"
            [github]
            enabled = true
            watched_repos = ["sjcarpenter/hippo"]
            [github.lessons]
            min_occurrences = 3
        "#;
        let cfg: HippoConfig = toml::from_str(toml).unwrap();
        assert!(cfg.github.enabled);
        assert_eq!(cfg.github.watched_repos, vec!["sjcarpenter/hippo"]);
        assert_eq!(cfg.github.lessons.min_occurrences, 3);
        // defaults preserved for unset fields
        assert_eq!(cfg.github.poll_interval_secs, 300);
        assert_eq!(cfg.github.token_env, "HIPPO_GITHUB_TOKEN");
        assert_eq!(cfg.github.lessons.cluster_window_days, 30);
        assert_eq!(cfg.github.lessons.path_prefix_segments, 2);
    }
}
