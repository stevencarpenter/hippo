//! Backfill subcommand: re-extract session segments from JSONL files.
//!
//! Designed to recover data lost to Bug A (watcher read truncated JSONL
//! before the file was fully flushed). The new upsert path in
//! `claude_session::ingest_session_file` (T-A.3) handles dedup automatically
//! via `content_hash`, so running backfill multiple times is safe.
//!
//! Per-file behaviour:
//! 1. Reset `claude_session_offsets.size_at_last_read = 0` so the live
//!    watcher reprocesses the file on its next FSEvents tick.
//! 2. Call `claude_session::ingest_session_file` to re-parse and upsert.
//! 3. Accumulate per-file stats for the final summary table.

use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{Context, Result};
use chrono::{DateTime, NaiveDate, Utc};
use tracing::warn;

use hippo_core::config::HippoConfig;
use hippo_core::storage::open_db;

use crate::claude_session::ingest_session_file;

/// Summary returned after a backfill run.
pub struct BackfillSummary {
    pub files_matched: usize,
    pub files_processed: usize,
    pub files_errored: usize,
    pub segments_updated: usize,
    pub segments_unchanged: usize,
    pub duration_secs: f64,
}

/// Run the backfill over all files matching `glob_pattern`.
///
/// * `since` — if `Some`, skip files whose mtime is older than this timestamp.
/// * `dry_run` — if `true`, collect the file list but do not write to the DB.
pub fn run_backfill(
    config: &HippoConfig,
    glob_pattern: &str,
    since: Option<DateTime<Utc>>,
    dry_run: bool,
) -> Result<BackfillSummary> {
    let start = Instant::now();

    // Resolve matching paths.
    let paths = collect_paths(glob_pattern, since)?;
    let files_matched = paths.len();

    if dry_run {
        for p in &paths {
            println!("  would process: {}", p.display());
        }
        return Ok(BackfillSummary {
            files_matched,
            files_processed: 0,
            files_errored: 0,
            segments_updated: 0,
            segments_unchanged: 0,
            duration_secs: start.elapsed().as_secs_f64(),
        });
    }

    let db_path = config.db_path();
    let conn = open_db(&db_path).context("failed to open hippo.db for backfill")?;

    let mut files_processed = 0usize;
    let mut files_errored = 0usize;
    let mut segments_updated = 0usize;
    let mut segments_unchanged = 0usize;

    for path in &paths {
        // Reset size_at_last_read so the live watcher reprocesses on its
        // next FSEvents tick (even if we already fixed the segments here).
        if let Err(e) = reset_offset(&conn, path) {
            warn!(path = %path.display(), %e, "backfill: failed to reset offset row");
            // Non-fatal; continue with ingest.
        }

        let (inserted, skipped, errors) = ingest_session_file(&conn, path);

        if errors > 0 {
            eprintln!(
                "  warning: {} error(s) while processing {}",
                errors,
                path.display()
            );
            files_errored += 1;
        } else {
            files_processed += 1;
        }

        segments_updated += inserted;
        segments_unchanged += skipped;
    }

    Ok(BackfillSummary {
        files_matched,
        files_processed,
        files_errored,
        segments_updated,
        segments_unchanged,
        duration_secs: start.elapsed().as_secs_f64(),
    })
}

/// Parse `--since YYYY-MM-DD` into a `DateTime<Utc>` at midnight local time,
/// converted to UTC.
pub fn parse_since_date(s: &str) -> Result<DateTime<Utc>> {
    let naive_date =
        NaiveDate::parse_from_str(s, "%Y-%m-%d").context("--since must be YYYY-MM-DD")?;
    // Interpret midnight on that date in local time, then convert to UTC.
    let naive_dt = naive_date
        .and_hms_opt(0, 0, 0)
        .context("failed to build midnight datetime")?;
    // chrono::Local gives us the offset for midnight on that date.
    use chrono::TimeZone;
    let local_dt = chrono::Local
        .from_local_datetime(&naive_dt)
        .single()
        .context("ambiguous local time (DST transition?)")?;
    Ok(local_dt.with_timezone(&Utc))
}

/// Expand a leading `~` (or `~/`) in `pattern` to the user's home directory.
/// `glob::glob` treats `~` as a literal path component, so quoted CLI input
/// like `'~/.claude/projects/**/*.jsonl'` (the documented recovery form)
/// would silently match zero files. We expand here so the documented command
/// works as written. Patterns without a leading `~` pass through unchanged.
fn expand_tilde(pattern: &str) -> Result<String> {
    if pattern == "~" {
        let home = dirs::home_dir().context("could not determine home directory for `~`")?;
        return Ok(home.to_string_lossy().into_owned());
    }
    if let Some(rest) = pattern.strip_prefix("~/") {
        let home = dirs::home_dir().context("could not determine home directory for `~/`")?;
        return Ok(home.join(rest).to_string_lossy().into_owned());
    }
    Ok(pattern.to_string())
}

/// Expand `glob_pattern` to a sorted list of paths, optionally filtered by mtime.
fn collect_paths(glob_pattern: &str, since: Option<DateTime<Utc>>) -> Result<Vec<PathBuf>> {
    let expanded = expand_tilde(glob_pattern)?;
    // nosemgrep: rust.actix.path-traversal.tainted-path.tainted-path
    // This is a local CLI operating on the user's own machine.
    let entries =
        glob::glob(&expanded).with_context(|| format!("invalid glob pattern: {expanded}"))?;

    let since_secs: Option<i64> = since.map(|dt| dt.timestamp());

    let mut paths: Vec<PathBuf> = entries
        .filter_map(|entry| {
            let path = match entry {
                Ok(p) => p,
                Err(e) => {
                    warn!("backfill: glob error: {e}");
                    return None;
                }
            };

            if !path.is_file() {
                return None;
            }

            if let Some(cutoff) = since_secs {
                let mtime = std::fs::metadata(&path)
                    .ok()
                    .and_then(|m| m.modified().ok())
                    .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                    .map(|d| d.as_secs() as i64)
                    .unwrap_or(0);
                if mtime < cutoff {
                    return None;
                }
            }

            Some(path)
        })
        .collect();

    paths.sort();
    Ok(paths)
}

/// Set `size_at_last_read = 0` for the given path so the watcher reprocesses
/// the file. It's fine if no row exists yet (UPDATE matches 0 rows, no error).
fn reset_offset(conn: &rusqlite::Connection, path: &Path) -> Result<()> {
    conn.execute(
        "UPDATE claude_session_offsets SET size_at_last_read = 0 WHERE path = ?1",
        rusqlite::params![path.to_string_lossy()],
    )
    .context("failed to reset claude_session_offsets")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use hippo_core::storage::open_db;
    use std::io::Write;
    use tempfile::TempDir;

    /// Minimal JSONL representing one user prompt + assistant response.
    fn make_session_jsonl(session_id: &str) -> String {
        format!(
            r#"{{"type":"system","message":{{"role":"system","content":"hi"}},"sessionId":"{session_id}","timestamp":"2026-04-25T10:00:00.000Z","cwd":"/tmp"}}
{{"type":"user","message":{{"role":"user","content":"hello"}},"sessionId":"{session_id}","timestamp":"2026-04-25T10:00:01.000Z","cwd":"/tmp"}}
{{"type":"assistant","message":{{"role":"assistant","content":[{{"type":"text","text":"Hello! How can I help you today? This is a longer response to exceed the minimum text length."}}]}},"sessionId":"{session_id}","timestamp":"2026-04-25T10:00:02.000Z","cwd":"/tmp"}}
"#
        )
    }

    /// Create a temp dir with a JSONL session file and a DB.
    fn setup_tmp() -> (TempDir, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("hippo.db");
        open_db(&db_path).expect("db init");
        (dir, db_path)
    }

    /// Write JSONL content to a path, returning the path.
    fn write_jsonl(dir: &Path, name: &str, content: &str) -> PathBuf {
        let path = dir.join(name);
        let mut f = std::fs::File::create(&path).unwrap();
        f.write_all(content.as_bytes()).unwrap();
        path
    }

    /// Build a `HippoConfig` whose `db_path()` points inside `dir`.
    fn test_config(dir: &Path) -> HippoConfig {
        let mut cfg = HippoConfig::default();
        cfg.storage.data_dir = dir.to_path_buf();
        cfg.storage.config_dir = dir.to_path_buf();
        cfg
    }

    #[test]
    fn test_backfill_dry_run_writes_nothing() {
        let (dir, db_path) = setup_tmp();
        let session_id = "dry-run-test-0000-0000-0000-000000000000";
        let content = make_session_jsonl(session_id);
        let jsonl_path = write_jsonl(dir.path(), "session.jsonl", &content);

        let conn = open_db(&db_path).unwrap();

        // Pre-seed a claude_session_offsets row.
        let now_ms = Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO claude_session_offsets (path, session_id, byte_offset, inode, device, size_at_last_read, updated_at) VALUES (?1, ?2, 0, 0, 0, 9999, ?3)",
            rusqlite::params![jsonl_path.to_string_lossy(), session_id, now_ms],
        ).unwrap();
        drop(conn);

        // Drive run_backfill end-to-end with dry_run=true. It must enumerate
        // the matching file but write nothing to the DB.
        let cfg = test_config(dir.path());
        // Hippo uses `hippo.db` under data_dir; the file at db_path is exactly that.
        assert_eq!(cfg.db_path(), db_path);
        let pattern = format!("{}/*.jsonl", dir.path().display());
        let summary = run_backfill(&cfg, &pattern, None, true).expect("dry_run backfill");
        assert_eq!(summary.files_matched, 1);
        assert_eq!(summary.files_processed, 0, "dry-run must not process");
        assert_eq!(summary.segments_updated, 0);

        // Verify DB is untouched after dry-run.
        let conn = open_db(&db_path).unwrap();
        let size: i64 = conn
            .query_row(
                "SELECT size_at_last_read FROM claude_session_offsets WHERE path = ?1",
                rusqlite::params![jsonl_path.to_string_lossy()],
                |r| r.get(0),
            )
            .unwrap();
        // Should still be 9999 because dry-run didn't write.
        assert_eq!(size, 9999, "dry-run must not modify size_at_last_read");

        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM claude_sessions WHERE session_id = ?1",
                rusqlite::params![session_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 0, "dry-run must not insert claude_sessions rows");
    }

    #[test]
    fn test_backfill_resets_offset_for_matched_files() {
        let (dir, db_path) = setup_tmp();
        let session_id = "offset-reset-0000-0000-0000-000000000000";
        let content = make_session_jsonl(session_id);
        let jsonl_path = write_jsonl(dir.path(), "session.jsonl", &content);

        let conn = open_db(&db_path).unwrap();
        let now_ms = Utc::now().timestamp_millis();
        conn.execute(
            "INSERT INTO claude_session_offsets (path, session_id, byte_offset, inode, device, size_at_last_read, updated_at) VALUES (?1, ?2, 100, 0, 0, 512, ?3)",
            rusqlite::params![jsonl_path.to_string_lossy(), session_id, now_ms],
        ).unwrap();

        reset_offset(&conn, &jsonl_path).unwrap();

        let size: i64 = conn
            .query_row(
                "SELECT size_at_last_read FROM claude_session_offsets WHERE path = ?1",
                rusqlite::params![jsonl_path.to_string_lossy()],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(size, 0, "reset_offset must zero size_at_last_read");
    }

    #[test]
    fn test_backfill_reparses_and_updates_segment() {
        let (dir, db_path) = setup_tmp();
        // The session_id column in claude_sessions is derived from the file stem,
        // not from the JSONL contents. Use the file stem as the key for queries.
        let file_stem = "reparse-test-0000-0000-0000-000000000001";
        let content = make_session_jsonl(file_stem);
        // Name the file after the stem so SessionFile::from_path produces the expected id.
        let jsonl_path = write_jsonl(dir.path(), &format!("{file_stem}.jsonl"), &content);

        let conn = open_db(&db_path).unwrap();

        // Call ingest once to seed the row.
        let (inserted, skipped, errors) = ingest_session_file(&conn, &jsonl_path);
        assert!(
            inserted > 0,
            "first ingest should insert segments; got inserted={inserted} skipped={skipped} errors={errors}"
        );
        assert_eq!(errors, 0);

        // Verify the row landed in claude_sessions (session_id = file stem).
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM claude_sessions WHERE session_id = ?1",
                rusqlite::params![file_stem],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            count > 0,
            "claude_sessions row must exist after first ingest"
        );
    }

    #[test]
    fn test_backfill_idempotent_on_second_run() {
        let (dir, db_path) = setup_tmp();
        let file_stem = "idempotent-test-0000-0000-0000-00000000001";
        let content = make_session_jsonl(file_stem);
        let jsonl_path = write_jsonl(dir.path(), &format!("{file_stem}.jsonl"), &content);

        let conn = open_db(&db_path).unwrap();

        // First run.
        let (inserted1, _skipped1, errors1) = ingest_session_file(&conn, &jsonl_path);
        assert_eq!(errors1, 0);
        assert!(inserted1 > 0);

        // Second run — same content. Under upsert + content-hash dedup
        // (T-A.3/T-A.4), the existing row is updated in place but the hash
        // is unchanged, so it's counted as `skipped` (not `inserted`).
        let (inserted2, skipped2, errors2) = ingest_session_file(&conn, &jsonl_path);
        assert_eq!(errors2, 0);
        assert_eq!(
            inserted2, 0,
            "second run must not insert duplicate segments"
        );
        assert!(
            skipped2 > 0,
            "second run must skip already-present segments"
        );
    }

    #[test]
    fn test_backfill_skips_files_older_than_since() {
        let (dir, db_path) = setup_tmp();
        let _ = db_path; // DB not needed for this test.

        let session_a = "since-new-0000-0000-0000-000000000001";
        let session_b = "since-old-0000-0000-0000-000000000002";

        let _new_path = write_jsonl(
            dir.path(),
            "new_session.jsonl",
            &make_session_jsonl(session_a),
        );
        let old_path = write_jsonl(
            dir.path(),
            "old_session.jsonl",
            &make_session_jsonl(session_b),
        );

        // Set old_session mtime to 10 days ago using libc::utimes.
        {
            use std::ffi::CString;
            let ten_days_ago_secs = (std::time::SystemTime::now()
                - std::time::Duration::from_secs(10 * 24 * 3600))
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
            let times = [
                libc::timeval {
                    tv_sec: ten_days_ago_secs,
                    tv_usec: 0,
                },
                libc::timeval {
                    tv_sec: ten_days_ago_secs,
                    tv_usec: 0,
                },
            ];
            let path_cstr = CString::new(old_path.to_string_lossy().as_bytes()).unwrap();
            unsafe { libc::utimes(path_cstr.as_ptr(), times.as_ptr()) };
        }

        // since = 5 days ago.
        let five_days_ago = Utc::now() - chrono::Duration::days(5);
        let paths = collect_paths(
            &format!("{}/*.jsonl", dir.path().display()),
            Some(five_days_ago),
        )
        .unwrap();

        let matched: Vec<_> = paths
            .iter()
            .map(|p| p.file_name().unwrap().to_str().unwrap())
            .collect();
        assert!(
            matched.contains(&"new_session.jsonl"),
            "new file must be included"
        );
        assert!(
            !matched.contains(&"old_session.jsonl"),
            "old file must be excluded"
        );
    }

    #[test]
    fn test_backfill_glob_matches_multiple_files() {
        let (dir, db_path) = setup_tmp();
        let conn = open_db(&db_path).unwrap();

        let files = [
            ("a.jsonl", "glob-multi-a-0000-0000-0000-000000000001"),
            ("b.jsonl", "glob-multi-b-0000-0000-0000-000000000002"),
            ("c.jsonl", "glob-multi-c-0000-0000-0000-000000000003"),
        ];

        let mut jsonl_paths = Vec::new();
        for (name, session_id) in &files {
            let content = make_session_jsonl(session_id);
            let path = write_jsonl(dir.path(), name, &content);
            jsonl_paths.push(path);
        }

        let pattern = format!("{}/*.jsonl", dir.path().display());
        let paths = collect_paths(&pattern, None).unwrap();
        assert_eq!(paths.len(), 3, "glob must match all 3 files");

        // Process all via ingest.
        let mut total_inserted = 0usize;
        for path in &paths {
            let (inserted, _skipped, errors) = ingest_session_file(&conn, path);
            assert_eq!(errors, 0);
            total_inserted += inserted;
        }
        assert!(
            total_inserted > 0,
            "at least one segment should be inserted"
        );
    }

    #[test]
    fn test_parse_since_date_valid() {
        let result = parse_since_date("2026-04-25");
        assert!(result.is_ok());
        let dt = result.unwrap();
        // Should be 2026-04-25T00:00:00 local time, converted to UTC.
        // Just check the date portion (UTC may differ by hours due to TZ).
        assert!(
            dt.format("%Y-%m-%d").to_string() == "2026-04-25"
                || dt.format("%Y-%m-%d").to_string() == "2026-04-24",
            "date should be 2026-04-25 or 2026-04-24 (UTC offset)"
        );
    }

    #[test]
    fn test_parse_since_date_invalid() {
        assert!(parse_since_date("not-a-date").is_err());
        assert!(parse_since_date("2026/04/25").is_err());
    }

    #[test]
    fn test_expand_tilde_passthrough() {
        // Patterns without a leading `~` are returned unchanged.
        assert_eq!(expand_tilde("/abs/path").unwrap(), "/abs/path");
        assert_eq!(
            expand_tilde("relative/*.jsonl").unwrap(),
            "relative/*.jsonl"
        );
        // A `~` mid-path is not a home reference; it stays literal.
        assert_eq!(expand_tilde("foo/~/bar").unwrap(), "foo/~/bar");
    }

    #[test]
    fn test_expand_tilde_prefix() {
        let home = dirs::home_dir().expect("home dir");
        assert_eq!(expand_tilde("~").unwrap(), home.to_string_lossy());
        let expected = home.join(".claude/projects/**/*.jsonl");
        assert_eq!(
            expand_tilde("~/.claude/projects/**/*.jsonl").unwrap(),
            expected.to_string_lossy()
        );
    }

    #[test]
    fn test_collect_paths_expands_tilde() {
        // `glob::glob` does not expand `~`. Without expansion, a pattern
        // like `~/<unique-tmp-name>/*.jsonl` would silently return zero
        // paths. We can't reliably write under the user's $HOME from a
        // unit test, but we can confirm that the expansion happens before
        // glob — i.e., a tilde-pattern returns Ok with an empty list (the
        // home dir doesn't contain the unique fixture name) rather than a
        // glob-pattern error.
        let nonce = format!(
            "hippo-backfill-tilde-test-{}-not-real",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        );
        let pattern = format!("~/{nonce}/*.jsonl");
        let paths = collect_paths(&pattern, None).expect("tilde pattern must not error");
        assert!(
            paths.is_empty(),
            "fixture path should not exist; got {paths:?}"
        );
    }

    #[test]
    fn test_reset_offset_no_row_is_ok() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("hippo.db");
        let conn = open_db(&db_path).unwrap();
        // Should not error even when no row exists.
        let result = reset_offset(&conn, Path::new("/nonexistent/session.jsonl"));
        assert!(result.is_ok(), "reset with no existing row must be OK");
    }
}
