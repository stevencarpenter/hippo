//! Operator enrichment pause state, shared by the daemon CLI and the brain.
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const STATE_FILE: &str = "enrichment-state.json";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct EnrichmentState {
    pub paused: bool,
    pub changed_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StateRead {
    Missing,
    Valid(EnrichmentState),
    Invalid(String),
}

impl StateRead {
    /// An unreadable or malformed state file fails closed: enrichment stays paused.
    pub fn is_paused(&self) -> bool {
        match self {
            StateRead::Missing => false,
            StateRead::Valid(s) => s.paused,
            StateRead::Invalid(_) => true,
        }
    }

    pub fn changed_at_ms(&self) -> Option<i64> {
        match self {
            StateRead::Valid(s) => Some(s.changed_at_ms),
            _ => None,
        }
    }
}

pub fn state_path(data_dir: &Path) -> PathBuf {
    data_dir.join(STATE_FILE)
}

pub fn read_state(data_dir: &Path) -> StateRead {
    match std::fs::read_to_string(state_path(data_dir)) {
        Ok(text) => match serde_json::from_str::<EnrichmentState>(&text) {
            Ok(s) => StateRead::Valid(s),
            Err(e) => StateRead::Invalid(e.to_string()),
        },
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => StateRead::Missing,
        Err(e) => StateRead::Invalid(e.to_string()),
    }
}

/// Idempotent: a valid state that already has `paused` is returned unchanged.
pub fn write_state(data_dir: &Path, paused: bool, now_ms: i64) -> anyhow::Result<EnrichmentState> {
    if let StateRead::Valid(s) = read_state(data_dir)
        && s.paused == paused
    {
        return Ok(s);
    }
    std::fs::create_dir_all(data_dir)?;
    let state = EnrichmentState {
        paused,
        changed_at_ms: now_ms,
    };
    // Same-directory temp file so the rename is atomic.
    let tmp = data_dir.join(format!(".{STATE_FILE}.{}.tmp", std::process::id()));
    std::fs::write(&tmp, serde_json::to_string(&state)?)?;
    std::fs::rename(&tmp, state_path(data_dir))?;
    Ok(state)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn put(dir: &Path, text: &str) {
        std::fs::write(state_path(dir), text).unwrap();
    }

    #[test]
    fn missing_file_reads_as_running() {
        let t = tempfile::tempdir().unwrap();
        let r = read_state(t.path());
        assert_eq!(r, StateRead::Missing);
        assert!(!r.is_paused());
    }

    #[test]
    fn write_then_read_round_trips() {
        let t = tempfile::tempdir().unwrap();
        write_state(t.path(), true, 1000).unwrap();
        assert_eq!(
            read_state(t.path()),
            StateRead::Valid(EnrichmentState {
                paused: true,
                changed_at_ms: 1000
            })
        );
    }

    #[test]
    fn repeat_pause_keeps_original_timestamp() {
        let t = tempfile::tempdir().unwrap();
        write_state(t.path(), true, 1000).unwrap();
        let s = write_state(t.path(), true, 2000).unwrap();
        assert_eq!(s.changed_at_ms, 1000);
        assert_eq!(read_state(t.path()).changed_at_ms(), Some(1000));
    }

    #[test]
    fn resume_changes_timestamp() {
        let t = tempfile::tempdir().unwrap();
        write_state(t.path(), true, 1000).unwrap();
        write_state(t.path(), false, 3000).unwrap();
        assert_eq!(
            read_state(t.path()),
            StateRead::Valid(EnrichmentState {
                paused: false,
                changed_at_ms: 3000
            })
        );
    }

    #[test]
    fn invalid_contents_read_as_paused() {
        for bad in ["", "{", r#"{"paused":"yes"}"#, r#"{"changed_at_ms":1}"#] {
            let t = tempfile::tempdir().unwrap();
            put(t.path(), bad);
            let r = read_state(t.path());
            assert!(matches!(r, StateRead::Invalid(_)), "{bad:?}");
            assert!(r.is_paused());
        }
    }

    #[test]
    fn writer_output_matches_shared_fixture() {
        let t = tempfile::tempdir().unwrap();
        write_state(t.path(), true, 1792500000000).unwrap();
        let written: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(state_path(t.path())).unwrap()).unwrap();
        let fixture_path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/fixtures/enrichment-state-paused.json"
        );
        let fixture: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(fixture_path).unwrap()).unwrap();
        assert_eq!(written, fixture);
    }

    #[test]
    fn resume_overwrites_invalid_file() {
        let t = tempfile::tempdir().unwrap();
        put(t.path(), "{");
        write_state(t.path(), false, 5).unwrap();
        assert_eq!(
            read_state(t.path()),
            StateRead::Valid(EnrichmentState {
                paused: false,
                changed_at_ms: 5
            })
        );
    }
}
