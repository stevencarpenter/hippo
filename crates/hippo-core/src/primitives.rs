//! Shared event primitive types used across `events::*` payload variants and
//! the `agentic::*` module.
//!
//! These types are pure data — no behavior, no harness-specific logic — so
//! they live outside `events.rs` and `agentic/` to keep the module dependency
//! graph one-directional: both `events` and `agentic` import from
//! `primitives`, neither imports from the other for primitive shapes.
//!
//! Re-exported from `events` for backwards-compat with existing call sites
//! that say `use hippo_core::events::{CapturedOutput, GitState}`.

use serde::{Deserialize, Serialize};

/// Captured stdout/stderr or tool-output content.
///
/// `content` may be truncated to a max byte budget; `truncated` flags that case
/// and `original_bytes` records the pre-truncation length so downstream
/// consumers can report "X KB truncated to Y" without re-fetching the source.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CapturedOutput {
    pub content: String,
    pub truncated: bool,
    pub original_bytes: usize,
}

/// Git working-state snapshot at the time an event was captured.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitState {
    pub repo: Option<String>,
    pub branch: Option<String>,
    pub commit: Option<String>,
    pub is_dirty: bool,
}
