//! Assertion-based probes for interval-polled agentic session sources.

use anyhow::{Context, Result};
use hippo_core::config::HippoConfig;
use hippo_core::redaction::RedactionEngine;
use hippo_core::storage;
use std::path::{Path, PathBuf};
use tracing::{info, warn};

/// Settle floor shared by cursor/codex/opencode poller-backed probes.
pub(crate) const POLLER_PROBE_SETTLE_MS: i64 = 90_000;

/// Outer eligibility window shared by cursor/codex/opencode poller-backed probes.
pub(crate) const POLLER_PROBE_WINDOW_MS: i64 = 600_000;

type SegmentCounter = fn(&Path, i64, &RedactionEngine) -> Result<usize>;

struct SourceFileProbeSpec {
    harness: &'static str,
    probe_label: &'static str,
    /// When true, require `end_time >= mtime_ms - window_ms` on the COUNT query.
    /// Cursor uses file mtime-aligned segment timestamps; codex uses rollout JSON
    /// timestamps that may predate the file mtime, so codex omits this floor.
    end_time_floor_from_mtime: bool,
}

fn walk_settled_files(
    roots: &[PathBuf],
    settle_ms: i64,
    window_ms: i64,
    is_candidate: impl Fn(&Path) -> bool,
) -> Result<Vec<(PathBuf, i64)>> {
    let now_ms = chrono::Utc::now().timestamp_millis();
    let mut recent = Vec::new();
    for root in roots {
        if !root.is_dir() {
            continue;
        }
        for entry in walkdir::WalkDir::new(root)
            .into_iter()
            .filter_map(|e| e.ok())
        {
            let path = entry.path();
            if !is_candidate(path) {
                continue;
            }
            let Some(mtime_ms) = entry.metadata().ok().and_then(|m| {
                m.modified().ok().and_then(|t| {
                    t.duration_since(std::time::UNIX_EPOCH)
                        .ok()
                        .map(|d| d.as_millis() as i64)
                })
            }) else {
                continue;
            };
            let age = now_ms - mtime_ms;
            if age >= settle_ms && age <= window_ms {
                recent.push((path.to_path_buf(), mtime_ms));
            }
        }
    }
    Ok(recent)
}

fn assert_source_file_rows(
    config: &HippoConfig,
    db_label: &str,
    recent: &[(PathBuf, i64)],
    spec: SourceFileProbeSpec,
    window_ms: i64,
    count_segments: SegmentCounter,
) -> Result<(bool, Option<i64>)> {
    if recent.is_empty() {
        info!(
            "{}: no settled recent files — trivial pass",
            spec.probe_label
        );
        return Ok((true, None));
    }

    let now_ms = chrono::Utc::now().timestamp_millis();
    let db = storage::open_db(&config.db_path())
        .with_context(|| format!("cannot open DB for {db_label}"))?;
    let mut redaction: Option<RedactionEngine> = None;
    let mut all_ok = true;
    let mut latest_lag: Option<i64> = None;

    for (path, mtime_ms) in recent {
        let path_str = path.to_string_lossy();
        let count: i64 = if spec.end_time_floor_from_mtime {
            db.query_row(
                "SELECT COUNT(*) FROM agentic_sessions
                 WHERE source_file = ?1
                   AND harness = ?2
                   AND probe_tag IS NULL
                   AND end_time >= ?3",
                rusqlite::params![path_str.as_ref(), spec.harness, mtime_ms - window_ms],
                |row| row.get(0),
            )
        } else {
            db.query_row(
                "SELECT COUNT(*) FROM agentic_sessions
                 WHERE source_file = ?1
                   AND harness = ?2
                   AND probe_tag IS NULL",
                rusqlite::params![path_str.as_ref(), spec.harness],
                |row| row.get(0),
            )
        }
        .with_context(|| format!("failed to query agentic_sessions for {}", path_str))?;

        if count > 0 {
            let max_end: Option<i64> = db
                .query_row(
                    "SELECT MAX(end_time) FROM agentic_sessions
                     WHERE source_file = ?1 AND harness = ?2 AND probe_tag IS NULL",
                    rusqlite::params![path_str.as_ref(), spec.harness],
                    |row| row.get(0),
                )
                .with_context(|| {
                    format!(
                        "{}: failed to query MAX(end_time) for {}",
                        spec.probe_label, path_str
                    )
                })?;
            if let Some(end) = max_end {
                let lag = now_ms - end;
                latest_lag = Some(latest_lag.map_or(lag, |p: i64| p.max(lag)));
            }
            continue;
        }

        let engine = match redaction.as_ref() {
            Some(r) => r,
            None => {
                redaction = Some(crate::load_redaction_engine(config));
                redaction.as_ref().expect("just set above")
            }
        };
        let segment_count = match count_segments(path, *mtime_ms, engine) {
            Ok(n) => n,
            Err(e) => {
                warn!(
                    "{}: cannot parse {} ({e:#}) — skipping",
                    spec.probe_label, path_str
                );
                continue;
            }
        };
        if segment_count == 0 {
            info!(
                "{}: {} yields no segments — no row expected, skipping",
                spec.probe_label, path_str
            );
            continue;
        }
        warn!("{}: no row for {}", spec.probe_label, path_str);
        all_ok = false;
    }
    Ok((all_ok, latest_lag))
}

fn is_cursor_transcript(path: &Path) -> bool {
    path.extension().map(|e| e == "jsonl").unwrap_or(false)
        && path
            .components()
            .any(|c| c.as_os_str() == "agent-transcripts")
}

fn is_codex_rollout(path: &Path) -> bool {
    path.extension().map(|e| e == "jsonl").unwrap_or(false)
        && path
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| n.starts_with("rollout-"))
}

fn count_cursor_segments(path: &Path, mtime_ms: i64, engine: &RedactionEngine) -> Result<usize> {
    Ok(crate::cursor_session::extract_segments(path, mtime_ms, engine)?.len())
}

fn count_codex_segments(path: &Path, _mtime_ms: i64, engine: &RedactionEngine) -> Result<usize> {
    Ok(crate::codex_session::extract_segments(path, engine)?.len())
}

pub(crate) fn probe_cursor_session(config: &HippoConfig) -> Result<(bool, Option<i64>)> {
    if !config.cursor.enabled {
        info!("cursor-session probe: cursor ingestion disabled — trivial pass");
        return Ok((true, None));
    }
    let window_ms = POLLER_PROBE_WINDOW_MS;
    let settle_ms = POLLER_PROBE_SETTLE_MS.min(window_ms / 2);
    let recent = walk_settled_files(
        &config.cursor.session_roots,
        settle_ms,
        window_ms,
        is_cursor_transcript,
    )?;
    assert_source_file_rows(
        config,
        "cursor-session probe",
        &recent,
        SourceFileProbeSpec {
            harness: "cursor",
            probe_label: "cursor-session probe",
            end_time_floor_from_mtime: true,
        },
        window_ms,
        count_cursor_segments,
    )
}

pub(crate) fn probe_codex_session(config: &HippoConfig) -> Result<(bool, Option<i64>)> {
    if !config.codex.enabled {
        info!("codex-session probe: codex ingestion disabled — trivial pass");
        return Ok((true, None));
    }
    let window_ms = POLLER_PROBE_WINDOW_MS;
    let settle_ms = POLLER_PROBE_SETTLE_MS.min(window_ms / 2);
    let recent = walk_settled_files(
        &config.codex.session_roots,
        settle_ms,
        window_ms,
        is_codex_rollout,
    )?;
    assert_source_file_rows(
        config,
        "codex-session probe",
        &recent,
        SourceFileProbeSpec {
            harness: "codex",
            probe_label: "codex-session probe",
            end_time_floor_from_mtime: false,
        },
        window_ms,
        count_codex_segments,
    )
}

pub(crate) fn probe_opencode_session(config: &HippoConfig) -> Result<(bool, Option<i64>)> {
    if !config.opencode.enabled {
        info!("opencode-session probe: opencode ingestion disabled — trivial pass");
        return Ok((true, None));
    }
    let db_path = &config.opencode.db_path;
    if !db_path.exists() {
        info!("opencode-session probe: opencode DB absent — trivial pass");
        return Ok((true, None));
    }

    let now_ms = chrono::Utc::now().timestamp_millis();
    let window_ms = POLLER_PROBE_WINDOW_MS;
    let settle_ms = POLLER_PROBE_SETTLE_MS.min(window_ms / 2);
    let recent = crate::opencode_session::sessions_in_probe_window(
        db_path,
        now_ms - window_ms,
        now_ms - settle_ms,
    )
    .with_context(|| format!("cannot read opencode sessions from {}", db_path.display()))?;

    if recent.is_empty() {
        info!("opencode-session probe: no settled recent sessions — trivial pass");
        return Ok((true, None));
    }

    let db =
        storage::open_db(&config.db_path()).context("cannot open DB for opencode-session probe")?;
    let mut all_ok = true;
    let mut latest_lag: Option<i64> = None;
    for (session_id, time_updated) in &recent {
        let count: i64 = db
            .query_row(
                "SELECT COUNT(*) FROM agentic_sessions
                 WHERE session_id = ?1
                   AND harness = 'opencode'
                   AND probe_tag IS NULL
                   AND end_time >= ?2",
                rusqlite::params![session_id, time_updated - window_ms],
                |row| row.get(0),
            )
            .with_context(|| {
                format!("failed to query agentic_sessions for opencode session {session_id}")
            })?;

        if count > 0 {
            let max_end: Option<i64> = db
                .query_row(
                    "SELECT MAX(end_time) FROM agentic_sessions
                     WHERE session_id = ?1 AND harness = 'opencode' AND probe_tag IS NULL",
                    rusqlite::params![session_id],
                    |row| row.get(0),
                )
                .with_context(|| {
                    format!(
                        "opencode-session probe: failed to query MAX(end_time) for {session_id}"
                    )
                })?;
            if let Some(end) = max_end {
                let lag = now_ms - end;
                latest_lag = Some(latest_lag.map_or(lag, |p: i64| p.max(lag)));
            }
            continue;
        }
        warn!("opencode-session probe: no row for session {session_id}");
        all_ok = false;
    }
    Ok((all_ok, latest_lag))
}

#[cfg(test)]
mod tests {
    use super::*;
    use hippo_core::config::HippoConfig;
    use std::path::{Path, PathBuf};
    use std::time::{Duration, SystemTime};

    fn test_config(tmp: &Path, root: &Path) -> HippoConfig {
        let data = tmp.join("data");
        std::fs::create_dir_all(&data).unwrap();
        let mut config = HippoConfig::default();
        config.storage.data_dir = data;
        config.cursor.session_roots = vec![root.to_path_buf()];
        config
    }

    fn write_transcript(root: &Path, id: &str, body: &str, age: Duration) -> PathBuf {
        let dir = root
            .join("Users-x-projects-foo")
            .join("agent-transcripts")
            .join(id);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(format!("{id}.jsonl"));
        std::fs::write(&path, body).unwrap();
        let mtime = SystemTime::now() - age;
        filetime::set_file_mtime(&path, filetime::FileTime::from_system_time(mtime)).unwrap();
        path
    }

    fn user_transcript() -> String {
        [
            r#"{"role":"user","message":{"content":[{"type":"text","text":"<user_query>\nfix the build\n</user_query>"}]}}"#,
            r#"{"role":"assistant","message":{"content":[{"type":"text","text":"On it."}]}}"#,
        ]
        .join("\n")
    }

    fn assistant_only_transcript() -> String {
        r#"{"role":"assistant","message":{"content":[{"type":"text","text":"orphaned reply"}]}}"#
            .to_string()
    }

    fn ingest_cursor(config: &HippoConfig, path: &Path) -> usize {
        crate::cursor_session::ingest_one(config, path).unwrap()
    }

    #[test]
    fn cursor_probe_trivial_pass_when_no_transcripts() {
        let tmp = tempfile::tempdir().unwrap();
        let config = test_config(tmp.path(), &tmp.path().join("nonexistent"));
        let (ok, lag) = probe_cursor_session(&config).unwrap();
        assert!(ok);
        assert_eq!(lag, None);
    }

    #[test]
    fn cursor_probe_happy_path_in_window_with_row() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let config = test_config(tmp.path(), &root);
        let path = write_transcript(
            &root,
            "happy-1",
            &user_transcript(),
            Duration::from_secs(180),
        );
        assert_eq!(ingest_cursor(&config, &path), 1);

        let (ok, lag) = probe_cursor_session(&config).unwrap();
        assert!(ok);
        assert!(lag.is_some());
    }

    #[test]
    fn cursor_probe_fails_when_expected_row_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let config = test_config(tmp.path(), &root);
        write_transcript(
            &root,
            "missing-1",
            &user_transcript(),
            Duration::from_secs(180),
        );
        let (ok, _) = probe_cursor_session(&config).unwrap();
        assert!(!ok);
    }

    #[test]
    fn cursor_probe_trivial_pass_when_disabled() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let mut config = test_config(tmp.path(), &root);
        config.cursor.enabled = false;
        write_transcript(
            &root,
            "disabled-1",
            &user_transcript(),
            Duration::from_secs(180),
        );
        let (ok, lag) = probe_cursor_session(&config).unwrap();
        assert!(ok);
        assert_eq!(lag, None);
    }

    #[test]
    fn cursor_probe_skips_zero_segment_transcript() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let config = test_config(tmp.path(), &root);
        let path = write_transcript(
            &root,
            "empty-1",
            &assistant_only_transcript(),
            Duration::from_secs(180),
        );
        assert_eq!(ingest_cursor(&config, &path), 0);
        let (ok, lag) = probe_cursor_session(&config).unwrap();
        assert!(ok);
        assert_eq!(lag, None);
    }

    fn codex_test_config(tmp: &Path, root: &Path) -> HippoConfig {
        let mut config = crate::codex_session::test_config(tmp, &[root.to_path_buf()]);
        config.codex.min_idle_secs = 0;
        config
    }

    fn write_rollout(root: &Path, name: &str, body: &str, age: Duration) -> PathBuf {
        std::fs::create_dir_all(root).unwrap();
        let path = root.join(format!("{name}.jsonl"));
        std::fs::write(&path, body).unwrap();
        let mtime = SystemTime::now() - age;
        filetime::set_file_mtime(&path, filetime::FileTime::from_system_time(mtime)).unwrap();
        path
    }

    fn codex_fixture_body() -> String {
        std::fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/codex/rollout-cli.jsonl"),
        )
        .unwrap()
    }

    fn session_meta_only_rollout() -> String {
        r#"{"timestamp":"2026-04-04T07:47:59.376Z","type":"session_meta","payload":{"id":"meta-only","timestamp":"2026-04-04T07:47:55.190Z","cwd":"/proj"}}"#
            .to_string()
    }

    #[test]
    fn codex_probe_trivial_pass_when_no_rollouts() {
        let tmp = tempfile::tempdir().unwrap();
        let config = codex_test_config(tmp.path(), &tmp.path().join("empty"));
        let (ok, lag) = probe_codex_session(&config).unwrap();
        assert!(ok);
        assert_eq!(lag, None);
    }

    #[test]
    fn codex_probe_happy_path_in_window_with_row() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let config = codex_test_config(tmp.path(), &root);
        write_rollout(
            &root,
            "rollout-happy",
            &codex_fixture_body(),
            Duration::from_secs(180),
        );
        assert!(crate::codex_session::poll_tick(&config).unwrap() >= 1);
        let (ok, lag) = probe_codex_session(&config).unwrap();
        assert!(ok);
        assert!(lag.is_some());
    }

    #[test]
    fn codex_probe_fails_when_expected_row_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let config = codex_test_config(tmp.path(), &root);
        write_rollout(
            &root,
            "rollout-missing",
            &codex_fixture_body(),
            Duration::from_secs(180),
        );
        let (ok, _) = probe_codex_session(&config).unwrap();
        assert!(!ok);
    }

    #[test]
    fn codex_probe_trivial_pass_when_disabled() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let mut config = codex_test_config(tmp.path(), &root);
        config.codex.enabled = false;
        write_rollout(
            &root,
            "rollout-off",
            &codex_fixture_body(),
            Duration::from_secs(180),
        );
        let (ok, lag) = probe_codex_session(&config).unwrap();
        assert!(ok);
        assert_eq!(lag, None);
    }

    #[test]
    fn codex_probe_skips_zero_segment_rollout() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("roots");
        let config = codex_test_config(tmp.path(), &root);
        write_rollout(
            &root,
            "rollout-empty",
            &session_meta_only_rollout(),
            Duration::from_secs(180),
        );
        let (ok, lag) = probe_codex_session(&config).unwrap();
        assert!(ok);
        assert_eq!(lag, None);
    }

    fn opencode_test_config(tmp: &Path, oc_db: &Path) -> HippoConfig {
        let data = tmp.join("data");
        std::fs::create_dir_all(&data).unwrap();
        let mut config = HippoConfig::default();
        config.storage.data_dir = data;
        config.opencode.db_path = oc_db.to_path_buf();
        config
    }

    fn init_opencode_db(path: &Path) -> rusqlite::Connection {
        let conn = rusqlite::Connection::open(path).unwrap();
        conn.execute_batch(
            "CREATE TABLE session (
                id TEXT PRIMARY KEY,
                slug TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                directory TEXT NOT NULL,
                parent_id TEXT,
                agent TEXT,
                model TEXT,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                summary_additions INTEGER,
                summary_deletions INTEGER,
                summary_files INTEGER,
                summary_diffs TEXT
            );",
        )
        .unwrap();
        conn
    }

    fn insert_opencode_session(conn: &rusqlite::Connection, id: &str, time_updated: i64) {
        conn.execute(
            "INSERT INTO session
               (id, slug, title, directory, time_created, time_updated)
             VALUES (?1, 'slug', 'title', '/work/proj', ?2, ?2)",
            rusqlite::params![id, time_updated],
        )
        .unwrap();
    }

    #[test]
    fn opencode_probe_trivial_pass_when_no_sessions() {
        let tmp = tempfile::tempdir().unwrap();
        let oc_db = tmp.path().join("opencode.db");
        init_opencode_db(&oc_db);
        let config = opencode_test_config(tmp.path(), &oc_db);
        let (ok, lag) = probe_opencode_session(&config).unwrap();
        assert!(ok);
        assert_eq!(lag, None);
    }

    #[test]
    fn opencode_probe_happy_path_in_window_with_row() {
        let tmp = tempfile::tempdir().unwrap();
        let oc_db = tmp.path().join("opencode.db");
        let oc_conn = init_opencode_db(&oc_db);
        let now_ms = chrono::Utc::now().timestamp_millis();
        insert_opencode_session(&oc_conn, "sess-happy", now_ms - 180_000);
        drop(oc_conn);

        let config = opencode_test_config(tmp.path(), &oc_db);
        assert_eq!(crate::opencode_session::poll_tick(&config).unwrap(), 1);
        let (ok, lag) = probe_opencode_session(&config).unwrap();
        assert!(ok);
        assert!(lag.is_some());
    }

    #[test]
    fn opencode_probe_fails_when_expected_row_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let oc_db = tmp.path().join("opencode.db");
        let oc_conn = init_opencode_db(&oc_db);
        insert_opencode_session(
            &oc_conn,
            "sess-missing",
            chrono::Utc::now().timestamp_millis() - 180_000,
        );
        drop(oc_conn);

        let config = opencode_test_config(tmp.path(), &oc_db);
        let (ok, _) = probe_opencode_session(&config).unwrap();
        assert!(!ok);
    }

    #[test]
    fn opencode_probe_trivial_pass_when_disabled() {
        let tmp = tempfile::tempdir().unwrap();
        let oc_db = tmp.path().join("opencode.db");
        let oc_conn = init_opencode_db(&oc_db);
        insert_opencode_session(
            &oc_conn,
            "sess-off",
            chrono::Utc::now().timestamp_millis() - 180_000,
        );
        drop(oc_conn);

        let mut config = opencode_test_config(tmp.path(), &oc_db);
        config.opencode.enabled = false;
        let (ok, lag) = probe_opencode_session(&config).unwrap();
        assert!(ok);
        assert_eq!(lag, None);
    }
}
