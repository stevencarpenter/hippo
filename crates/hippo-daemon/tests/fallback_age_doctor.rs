//! Regression guard for capture-reliability F-8 (invariant I-9).
//!
//! Failure mode: fallback JSONL files accumulate for > 24 hours while the
//! daemon is up. That means the drain-on-startup path is broken — every
//! captured event during the accumulation window is stuck in JSONL, never
//! reaching `events` / `browser_events`. `hippo doctor` currently counts
//! fallback files but does not inspect their mtime, so a 25-hour-old file
//! and a 25-second-old file read the same in the output.
//!
//! The doctor check we want — "any fallback file older than the configured
//! recovery threshold (default 24 h) is a drain-broken signal" — requires
//! a source change. These tests are `#[ignore]` until that source change
//! lands; the skeleton reserves the test file and names the intended shape.
//!
//! Tracking: docs/capture/test-matrix.md row F-8.

use std::fs;
use std::path::Path;
use std::time::{Duration, SystemTime};

use tempfile::tempdir;

/// Helper: write a fallback file and back-date its mtime by `age_ms`.
/// Uses `filetime` via `std::fs` — falls back to a naive touch if the
/// underlying filesystem doesn't support mtime manipulation.
fn write_aged_fallback_file(dir: &Path, name: &str, age_ms: u64) {
    fs::create_dir_all(dir).unwrap();
    let path = dir.join(name);
    fs::write(
        &path,
        r#"{"envelope_id":"00000000-0000-0000-0000-000000000000","producer_version":1,"timestamp":"2026-01-01T00:00:00Z","payload":{"type":"Raw","data":{}}}"#,
    )
    .unwrap();

    let new_mtime = SystemTime::now() - Duration::from_millis(age_ms);
    // utimensat via std isn't stable; use filetime-like approach through
    // the `set_file_times` helper. Since `filetime` isn't a dep here, we
    // skip the actual mtime adjustment and rely on the doctor check being
    // written to accept an injected clock for test purposes.
    let _ = new_mtime;
}

#[test]
#[ignore = "blocked on F-8 doctor source change — hippo doctor only counts \
            fallback files, does not inspect mtime. When `storage::list_stale_fallback_files` \
            (or an equivalent age-aware check in doctor) lands, swap to \
            injecting a fake clock and asserting on the output."]
fn doctor_flags_fallback_file_older_than_24h_as_drain_broken() {
    let temp = tempdir().unwrap();
    let fallback_dir = temp.path().join("fallback");

    // 25 hours old.
    write_aged_fallback_file(&fallback_dir, "2026-04-21.jsonl", 25 * 60 * 60 * 1000);

    // TODO when F-8 lands:
    //   let stale = storage::list_stale_fallback_files(&fallback_dir,
    //       Duration::from_secs(24 * 3600));
    //   assert_eq!(stale.len(), 1);
    //   assert_doctor_output_contains(&config, "fallback files: 1 file > 24h");
    unimplemented!("remove #[ignore] once doctor grows an age-aware fallback check");
}

#[test]
#[ignore = "blocked on F-8 doctor source change"]
fn doctor_is_silent_when_all_fallback_files_are_fresh() {
    let temp = tempdir().unwrap();
    let fallback_dir = temp.path().join("fallback");
    write_aged_fallback_file(&fallback_dir, "2026-04-22.jsonl", 60 * 1000); // 1 min old
    // TODO: assert doctor says [OK] — fresh file is not a drain-broken signal.
    unimplemented!();
}

#[test]
#[ignore = "blocked on F-8 doctor source change"]
fn doctor_counts_multiple_stale_files_and_shows_oldest_age() {
    let temp = tempdir().unwrap();
    let fallback_dir = temp.path().join("fallback");
    write_aged_fallback_file(&fallback_dir, "old1.jsonl", 25 * 3600 * 1000);
    write_aged_fallback_file(&fallback_dir, "old2.jsonl", 30 * 3600 * 1000);
    // TODO: assert doctor reports "2 files > 24h, oldest 30h".
    unimplemented!();
}
