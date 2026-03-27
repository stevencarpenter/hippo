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
