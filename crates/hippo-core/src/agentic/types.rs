use std::path::PathBuf;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::events::{CapturedOutput, GitState};

/// Which coding harness produced this tool call.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "name", rename_all = "kebab-case")]
pub enum Harness {
    ClaudeCode,
    Opencode,
    Codex,
    Unknown(String),
}

impl Harness {
    /// Stable string form for DB storage and search filters.
    pub fn as_db_str(&self) -> &str {
        match self {
            Self::ClaudeCode => "claude-code",
            Self::Opencode => "opencode",
            Self::Codex => "codex",
            Self::Unknown(s) => s,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum AgenticStatus {
    Ok,
    Error,
    Orphaned,
}

impl AgenticStatus {
    pub fn as_db_str(&self) -> &str {
        match self {
            Self::Ok => "ok",
            Self::Error => "error",
            Self::Orphaned => "orphaned",
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct TokenUsage {
    pub input: u64,
    pub output: u64,
    pub reasoning: u64,
    pub cache_read: u64,
    pub cache_write: u64,
}

/// A single tool call emitted by an agentic coding harness.
///
/// Pairs an assistant-side tool invocation with its result. Orphaned calls
/// (no result observed) are emitted with `status = Orphaned` at source EOF.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgenticToolCall {
    pub session_id: Uuid,
    pub parent_session_id: Option<Uuid>,
    pub harness: Harness,
    pub harness_version: Option<String>,
    pub model: String,
    pub provider: Option<String>,
    pub agent: Option<String>,
    pub effort: Option<String>,
    pub tool_name: String,
    pub tool_input: serde_json::Value,
    pub command: String,
    pub tool_output: Option<CapturedOutput>,
    pub status: AgenticStatus,
    pub duration_ms: u64,
    pub started_at: DateTime<Utc>,
    pub cwd: PathBuf,
    pub hostname: String,
    pub git_state: Option<GitState>,
    pub tokens: Option<TokenUsage>,
    pub cost_usd: Option<f64>,
    pub redaction_count: u32,
}
