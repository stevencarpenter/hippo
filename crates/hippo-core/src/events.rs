use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EventEnvelope {
    pub envelope_id: Uuid,
    pub producer_version: u32,
    pub timestamp: DateTime<Utc>,
    pub payload: EventPayload,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", content = "data")]
pub enum EventPayload {
    Shell(Box<ShellEvent>),
    FsChange(FsChangeEvent),
    IdeAction(IdeActionEvent),
    Browser(Box<BrowserEvent>),
    Raw(serde_json::Value),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShellEvent {
    pub session_id: Uuid,
    pub command: String,
    pub exit_code: i32,
    pub duration_ms: u64,
    pub cwd: PathBuf,
    pub hostname: String,
    pub shell: ShellKind,
    pub stdout: Option<CapturedOutput>,
    pub stderr: Option<CapturedOutput>,
    pub env_snapshot: HashMap<String, String>,
    pub git_state: Option<GitState>,
    pub redaction_count: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CapturedOutput {
    pub content: String,
    pub truncated: bool,
    pub original_bytes: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum ShellKind {
    Zsh,
    Bash,
    Fish,
    Unknown(String),
}

impl ShellKind {
    /// Stable string representation for database storage.
    pub fn as_db_str(&self) -> &str {
        match self {
            Self::Zsh => "zsh",
            Self::Bash => "bash",
            Self::Fish => "fish",
            Self::Unknown(s) => s,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitState {
    pub repo: Option<String>,
    pub branch: Option<String>,
    pub commit: Option<String>,
    pub is_dirty: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FsChangeEvent {
    pub path: PathBuf,
    pub change_type: String,
    pub timestamp: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserEvent {
    pub url: String,
    pub title: String,
    pub domain: String,
    pub dwell_ms: u64,
    pub scroll_depth: f32,
    pub extracted_text: Option<String>,
    pub search_query: Option<String>,
    pub referrer: Option<String>,
    pub content_hash: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IdeActionEvent {
    pub action: String,
    pub project: String,
    pub details: serde_json::Value,
    pub timestamp: DateTime<Utc>,
}

impl EventEnvelope {
    pub fn shell(event: ShellEvent) -> Self {
        Self {
            envelope_id: Uuid::new_v4(),
            producer_version: 1,
            timestamp: Utc::now(),
            payload: EventPayload::Shell(Box::new(event)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_shell_event() -> ShellEvent {
        ShellEvent {
            session_id: Uuid::new_v4(),
            command: "cargo build".to_string(),
            exit_code: 0,
            duration_ms: 1234,
            cwd: PathBuf::from("/home/user/project"),
            hostname: "laptop".to_string(),
            shell: ShellKind::Zsh,
            stdout: None,
            stderr: None,
            env_snapshot: HashMap::from([("HOME".to_string(), "/home/user".to_string())]),
            git_state: Some(GitState {
                repo: Some("myrepo".to_string()),
                branch: Some("main".to_string()),
                commit: Some("abc1234".to_string()),
                is_dirty: false,
            }),
            redaction_count: 0,
        }
    }

    #[test]
    fn test_shell_event_roundtrip() {
        let envelope = EventEnvelope::shell(sample_shell_event());
        let json = serde_json::to_string(&envelope).unwrap();
        let parsed: EventEnvelope = serde_json::from_str(&json).unwrap();
        match &parsed.payload {
            EventPayload::Shell(shell) => {
                assert_eq!(shell.command, "cargo build");
                assert_eq!(shell.exit_code, 0);
                assert_eq!(shell.duration_ms, 1234);
            }
            _ => panic!("expected Shell payload"),
        }
    }

    #[test]
    fn test_adjacently_tagged_json_shape() {
        let envelope = EventEnvelope::shell(sample_shell_event());
        let value: serde_json::Value = serde_json::to_value(&envelope).unwrap();
        let payload = &value["payload"];
        assert_eq!(payload["type"], "Shell");
        assert!(payload["data"].is_object());
        assert_eq!(payload["data"]["command"], "cargo build");
    }

    fn sample_browser_event() -> BrowserEvent {
        BrowserEvent {
            url: "https://docs.rs/serde/latest/serde/".to_string(),
            title: "serde - Rust".to_string(),
            domain: "docs.rs".to_string(),
            dwell_ms: 45000,
            scroll_depth: 0.75,
            extracted_text: Some("Serde is a framework for serializing...".to_string()),
            search_query: Some("rust serde tutorial".to_string()),
            referrer: Some("https://www.google.com/".to_string()),
            content_hash: Some("abc123def456".to_string()),
        }
    }

    #[test]
    fn test_browser_event_roundtrip() {
        let event = sample_browser_event();
        let envelope = EventEnvelope {
            envelope_id: Uuid::new_v4(),
            producer_version: 1,
            timestamp: Utc::now(),
            payload: EventPayload::Browser(Box::new(event)),
        };
        let json = serde_json::to_string(&envelope).unwrap();
        let parsed: EventEnvelope = serde_json::from_str(&json).unwrap();
        match &parsed.payload {
            EventPayload::Browser(browser) => {
                assert_eq!(browser.url, "https://docs.rs/serde/latest/serde/");
                assert_eq!(browser.title, "serde - Rust");
                assert_eq!(browser.domain, "docs.rs");
                assert_eq!(browser.dwell_ms, 45000);
                assert!((browser.scroll_depth - 0.75).abs() < f32::EPSILON);
                assert_eq!(
                    browser.extracted_text.as_deref(),
                    Some("Serde is a framework for serializing...")
                );
                assert_eq!(
                    browser.search_query.as_deref(),
                    Some("rust serde tutorial")
                );
                assert_eq!(
                    browser.referrer.as_deref(),
                    Some("https://www.google.com/")
                );
                assert_eq!(browser.content_hash.as_deref(), Some("abc123def456"));
            }
            _ => panic!("expected Browser payload"),
        }
    }

    #[test]
    fn test_browser_adjacently_tagged_json_shape() {
        let event = sample_browser_event();
        let envelope = EventEnvelope {
            envelope_id: Uuid::new_v4(),
            producer_version: 1,
            timestamp: Utc::now(),
            payload: EventPayload::Browser(Box::new(event)),
        };
        let value: serde_json::Value = serde_json::to_value(&envelope).unwrap();
        let payload = &value["payload"];
        assert_eq!(payload["type"], "Browser");
        assert!(payload["data"].is_object());
        assert_eq!(payload["data"]["domain"], "docs.rs");
    }

    #[test]
    fn test_raw_payload_roundtrip() {
        let raw = serde_json::json!({"custom": "data", "number": 42});
        let envelope = EventEnvelope {
            envelope_id: Uuid::new_v4(),
            producer_version: 1,
            timestamp: Utc::now(),
            payload: EventPayload::Raw(raw.clone()),
        };
        let json = serde_json::to_string(&envelope).unwrap();
        let parsed: EventEnvelope = serde_json::from_str(&json).unwrap();
        match parsed.payload {
            EventPayload::Raw(v) => {
                assert_eq!(v["custom"], "data");
                assert_eq!(v["number"], 42);
            }
            _ => panic!("expected Raw payload"),
        }
    }
}
