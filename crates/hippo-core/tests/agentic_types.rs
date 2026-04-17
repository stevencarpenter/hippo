use std::path::PathBuf;

use chrono::Utc;
use hippo_core::agentic::{AgenticStatus, AgenticToolCall, Harness, TokenUsage};
use hippo_core::events::{CapturedOutput, GitState};
use uuid::Uuid;

fn sample() -> AgenticToolCall {
    AgenticToolCall {
        session_id: Uuid::new_v4(),
        parent_session_id: None,
        harness: Harness::ClaudeCode,
        harness_version: None,
        model: "claude-opus-4-7".into(),
        provider: Some("anthropic".into()),
        agent: None,
        effort: None,
        tool_name: "Bash".into(),
        tool_input: serde_json::json!({"command": "cargo test"}),
        command: "cargo test".into(),
        tool_output: Some(CapturedOutput {
            content: "ok".into(),
            truncated: false,
            original_bytes: 2,
        }),
        status: AgenticStatus::Ok,
        duration_ms: 1234,
        started_at: Utc::now(),
        cwd: PathBuf::from("/tmp"),
        hostname: "host".into(),
        git_state: Some(GitState {
            repo: None,
            branch: Some("main".into()),
            commit: None,
            is_dirty: false,
        }),
        tokens: Some(TokenUsage {
            input: 10,
            output: 5,
            reasoning: 0,
            cache_read: 0,
            cache_write: 0,
        }),
        cost_usd: None,
        redaction_count: 0,
    }
}

#[test]
fn roundtrip_json() {
    let call = sample();
    let json = serde_json::to_string(&call).unwrap();
    let parsed: AgenticToolCall = serde_json::from_str(&json).unwrap();
    assert_eq!(parsed.tool_name, "Bash");
    assert_eq!(parsed.command, "cargo test");
    assert_eq!(parsed.model, "claude-opus-4-7");
    assert_eq!(parsed.status, AgenticStatus::Ok);
}

#[test]
fn harness_unknown_roundtrip() {
    let call = AgenticToolCall {
        harness: Harness::Unknown("aider".into()),
        ..sample()
    };
    let json = serde_json::to_string(&call).unwrap();
    let parsed: AgenticToolCall = serde_json::from_str(&json).unwrap();
    assert_eq!(parsed.harness, Harness::Unknown("aider".into()));
}

#[test]
fn harness_as_db_str() {
    assert_eq!(Harness::ClaudeCode.as_db_str(), "claude-code");
    assert_eq!(Harness::Opencode.as_db_str(), "opencode");
    assert_eq!(Harness::Codex.as_db_str(), "codex");
    assert_eq!(Harness::Unknown("x".into()).as_db_str(), "x");
}

#[test]
fn status_as_db_str() {
    assert_eq!(AgenticStatus::Ok.as_db_str(), "ok");
    assert_eq!(AgenticStatus::Error.as_db_str(), "error");
    assert_eq!(AgenticStatus::Orphaned.as_db_str(), "orphaned");
}
