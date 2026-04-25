use std::path::PathBuf;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::primitives::{CapturedOutput, GitState};

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
    /// Stable string form for the `agentic_sessions.harness` column and
    /// search filters. `Unknown(String)` returns the inner string verbatim.
    pub fn as_db_str(&self) -> &str {
        match self {
            Self::ClaudeCode => "claude-code",
            Self::Opencode => "opencode",
            Self::Codex => "codex",
            Self::Unknown(s) => s,
        }
    }

    /// Short basename for `source_health` row composition. Returns "claude" for
    /// `ClaudeCode` (NOT "claude-code") because v8 already seeded source_health
    /// with `claude-tool` and `claude-session` (see schema.sql v8 INSERT), and
    /// the Phase 2 v9→v10 migration preserves `claude-tool` while renaming
    /// `claude-session` → `agentic-session-claude` — both keying off the
    /// "claude" basename. Future ingesters compose `<basename>-tool` and
    /// `agentic-session-<basename>` via this method to avoid the
    /// "claude" vs "claude-code" footgun.
    pub fn source_basename(&self) -> &str {
        match self {
            Self::ClaudeCode => "claude",
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
    /// Wall time the tool actually started. **Distinct from**
    /// `EventEnvelope.timestamp`, which is producer ingest time.
    ///
    /// They diverge for batch backfills: a Codex JSONL ingester reading a
    /// session from three days ago emits an envelope with `timestamp = now()`
    /// (when the daemon read the rollout) but `started_at = <three days ago>`
    /// (when the tool ran). For the live pollers (Claude session tail,
    /// opencode SQLite poller), the two are very close but not asserted equal —
    /// queueing latency between source and daemon can introduce sub-second skew.
    ///
    /// Downstream consumers should use `started_at` for analytic time-bucketing
    /// (when did the user run this tool?) and `EventEnvelope.timestamp` for
    /// pipeline observability (when did hippo see it?).
    pub started_at: DateTime<Utc>,
    pub cwd: PathBuf,
    pub hostname: String,
    pub git_state: Option<GitState>,
    pub tokens: Option<TokenUsage>,
    pub cost_usd: Option<f64>,
    pub redaction_count: u32,
}
