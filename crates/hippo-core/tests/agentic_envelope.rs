use std::path::PathBuf;

use chrono::Utc;
use hippo_core::agentic::{AgenticStatus, AgenticToolCall, Harness, TokenUsage};
use hippo_core::events::{CapturedOutput, EventEnvelope, EventPayload, GitState};
use uuid::Uuid;

fn sample_tool_call() -> AgenticToolCall {
    AgenticToolCall {
        session_id: Uuid::new_v4(),
        parent_session_id: None,
        harness: Harness::ClaudeCode,
        harness_version: Some("1.2.3".into()),
        model: "claude-opus-4-7".into(),
        provider: Some("anthropic".into()),
        agent: None,
        effort: None,
        tool_name: "Bash".into(),
        tool_input: serde_json::json!({"command": "cargo test"}),
        command: "cargo test".into(),
        tool_output: Some(CapturedOutput {
            content: "test result: ok".into(),
            truncated: false,
            original_bytes: 15,
        }),
        status: AgenticStatus::Ok,
        duration_ms: 3210,
        started_at: Utc::now(),
        cwd: PathBuf::from("/projects/hippo"),
        hostname: "devbox".into(),
        git_state: Some(GitState {
            repo: Some("hippo".into()),
            branch: Some("agentic-ingestion-v2".into()),
            commit: Some("deadbeef".into()),
            is_dirty: false,
        }),
        tokens: Some(TokenUsage {
            input: 1024,
            output: 256,
            reasoning: 0,
            cache_read: 512,
            cache_write: 0,
        }),
        cost_usd: Some(0.0042),
        redaction_count: 0,
    }
}

#[test]
fn envelope_roundtrip_adjacently_tagged() {
    let envelope = EventEnvelope {
        envelope_id: Uuid::new_v4(),
        producer_version: 1,
        timestamp: Utc::now(),
        payload: EventPayload::AgenticToolCall(Box::new(sample_tool_call())),
        probe_tag: None,
    };

    let json = serde_json::to_string(&envelope).unwrap();
    let value: serde_json::Value = serde_json::from_str(&json).unwrap();

    // Adjacently-tagged: payload must have type = "AgenticToolCall"
    let payload = &value["payload"];
    assert_eq!(
        payload["type"], "AgenticToolCall",
        "payload.type must be AgenticToolCall, got: {payload}"
    );
    assert!(
        payload["data"].is_object(),
        "payload.data must be an object"
    );
    assert_eq!(
        payload["data"]["tool_name"], "Bash",
        "tool_name must round-trip"
    );
    assert_eq!(
        payload["data"]["command"], "cargo test",
        "command must round-trip"
    );
    assert_eq!(
        payload["data"]["duration_ms"], 3210,
        "duration_ms must round-trip"
    );

    // Full round-trip: deserialize back
    let parsed: EventEnvelope = serde_json::from_str(&json).unwrap();
    match parsed.payload {
        EventPayload::AgenticToolCall(call) => {
            assert_eq!(call.tool_name, "Bash");
            assert_eq!(call.command, "cargo test");
            assert_eq!(call.duration_ms, 3210);
            assert_eq!(call.status, AgenticStatus::Ok);
            assert_eq!(call.harness, Harness::ClaudeCode);
            let tokens = call.tokens.expect("tokens must be present");
            assert_eq!(tokens.input, 1024);
            assert_eq!(tokens.cache_read, 512);
        }
        other => panic!("expected AgenticToolCall payload, got {:?}", other),
    }
}
