//! End-to-end integration tests that assert every raw-data source hippo
//! claims to capture actually lands rows in the expected table(s).
//!
//! Motivation: on 2026-04-22 we discovered that BOTH the batch and tailer
//! Claude-session ingesters were silently not writing `claude_sessions`
//! rows (only tool-call `events` landed). This file is the audit contract —
//! every source below is exercised through its production write path so a
//! regression surfaces on CI instead of in a 272-session sev1 backfill.
//!
//! Source matrix: `docs/capture-reliability/10-source-audit.md`.
//!
//! One file per source, glued together as sub-modules (integration tests
//! only compile when declared from a file directly under `tests/`).

#[path = "common/mod.rs"]
mod common;

#[path = "source_audit/shell_events.rs"]
mod shell_events;

#[path = "source_audit/claude_tool_events.rs"]
mod claude_tool_events;

#[path = "source_audit/claude_session_batch.rs"]
mod claude_session_batch;

#[path = "source_audit/claude_session_tailer.rs"]
mod claude_session_tailer;

#[path = "source_audit/browser_events.rs"]
mod browser_events;

#[path = "source_audit/workflow_runs.rs"]
mod workflow_runs;

#[path = "source_audit/claude_subagent.rs"]
mod claude_subagent;

#[path = "source_audit/xcode_codingassistant.rs"]
mod xcode_codingassistant;

#[path = "source_audit/doctor_freshness.rs"]
mod doctor_freshness;

#[path = "source_audit/source_health_write_paths.rs"]
mod source_health_write_paths;
