//! Shared types and helpers for agentic (coding-assistant) tool calls.
//!
//! Producers: Claude Code JSONL ingester, opencode SQLite poller, Codex batch
//! importer. Downstream consumers: the daemon event pipeline, the brain
//! enrichment module, and MCP query tools.

pub mod render;
pub mod types;

pub use render::render_command;
pub use types::{AgenticStatus, AgenticToolCall, Harness, TokenUsage};
