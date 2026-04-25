//! Integration tests for doctor checks 2, 5, 6, 9, 10.
//!
//! All tests live inside `doctor::checks_2_5_6_9_10` so the cargo test
//! filter works:
//!
//!   cargo test -p hippo-daemon -- doctor::checks_2_5_6_9_10
//!
//! Each check has at least one negative test seeded with a failing fixture.
//! The `doctor_perf_budget` test asserts the combined wall-clock of all five
//! checks is under 2 seconds.

mod doctor {
    mod checks_2_5_6_9_10 {
        use std::fs;
        use std::io::Write;
        use std::os::unix::fs::PermissionsExt;
        use std::time::{Duration, SystemTime};

        use hippo_core::config::HippoConfig;
        use hippo_daemon::commands::{
            check_claude_session_db, check_fallback_age, check_nm_manifest, check_schema_version,
            check_session_hook_log,
        };
        use tempfile::tempdir;

        // ── helpers ────────────────────────────────────────────────────────

        /// Open a fresh in-memory SQLite DB with the full hippo schema applied
        /// (migrations run via `open_db`).
        fn open_test_db(dir: &std::path::Path) -> rusqlite::Connection {
            let db_path = dir.join("hippo.db");
            hippo_core::storage::open_db(&db_path).expect("open_db")
        }

        /// Write a valid NM manifest + executable wrapper into `nm_dir` and
        /// return the manifest path.
        fn write_valid_manifest(nm_dir: &std::path::Path) -> std::path::PathBuf {
            fs::create_dir_all(nm_dir).unwrap();
            let wrapper = nm_dir.join("hippo-native-messaging");
            fs::write(&wrapper, "#!/bin/bash\nexec hippo native-messaging-host\n").unwrap();
            fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();

            let manifest = nm_dir.join("hippo_daemon.json");
            let json = serde_json::json!({
                "name": "hippo_daemon",
                "description": "Hippo knowledge capture daemon",
                "path": wrapper.to_string_lossy(),
                "type": "stdio",
                "allowed_extensions": ["hippo-browser@local"],
            });
            fs::write(&manifest, serde_json::to_string_pretty(&json).unwrap()).unwrap();
            manifest
        }

        /// Back-date a file's mtime using `std::fs::FileTimes`.
        fn back_date_file(path: &std::path::Path, age: Duration) {
            let past = SystemTime::now().checked_sub(age).unwrap();
            let times = fs::FileTimes::new().set_modified(past);
            let file = fs::OpenOptions::new()
                .write(true)
                .open(path)
                .expect("open for back-dating");
            file.set_times(times).expect("set_times");
        }

        /// Insert a `claude_sessions` row for the given session_id.
        fn insert_session_row(conn: &rusqlite::Connection, session_id: &str) {
            conn.execute(
                "INSERT OR IGNORE INTO claude_sessions \
                 (session_id, project_dir, cwd, segment_index, start_time, end_time, \
                  summary_text, message_count, source_file, is_subagent, created_at) \
                 VALUES (?, '', '/', 0, unixepoch('now')*1000, unixepoch('now')*1000, \
                         '', 0, '', 0, unixepoch('now')*1000)",
                rusqlite::params![session_id],
            )
            .unwrap();
        }

        // ── Check 2: NM manifest ───────────────────────────────────────────

        #[test]
        fn check_2_missing_manifest_fails() {
            let tmp = tempdir().unwrap();
            let manifest = tmp.path().join("hippo_daemon.json");
            // Intentionally do NOT write the manifest.
            let fail = check_nm_manifest(&manifest, false);
            assert_eq!(fail, 1, "missing manifest must fail");
        }

        #[test]
        fn check_2_valid_manifest_passes() {
            let tmp = tempdir().unwrap();
            let manifest = write_valid_manifest(tmp.path());
            let fail = check_nm_manifest(&manifest, false);
            assert_eq!(fail, 0, "valid manifest must pass");
        }

        #[test]
        fn check_2_invalid_json_fails() {
            let tmp = tempdir().unwrap();
            let manifest = tmp.path().join("hippo_daemon.json");
            fs::write(&manifest, "not valid json{{").unwrap();
            let fail = check_nm_manifest(&manifest, false);
            assert_eq!(fail, 1, "invalid JSON must fail");
        }

        #[test]
        fn check_2_non_executable_path_fails() {
            let tmp = tempdir().unwrap();
            let wrapper = tmp.path().join("hippo-nm");
            fs::write(&wrapper, "#!/bin/bash\n").unwrap();
            // Deliberately NOT setting the execute bit.
            fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o644)).unwrap();

            let manifest = tmp.path().join("hippo_daemon.json");
            let json = serde_json::json!({
                "name": "hippo_daemon",
                "path": wrapper.to_string_lossy(),
                "type": "stdio",
                "allowed_extensions": ["hippo-browser@local"],
            });
            fs::write(&manifest, serde_json::to_string_pretty(&json).unwrap()).unwrap();

            let fail = check_nm_manifest(&manifest, false);
            assert_eq!(fail, 1, "non-executable wrapper must fail");
        }

        #[test]
        fn check_2_missing_extension_id_fails() {
            let tmp = tempdir().unwrap();
            let wrapper = tmp.path().join("hippo-nm");
            fs::write(&wrapper, "#!/bin/bash\n").unwrap();
            fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();

            let manifest = tmp.path().join("hippo_daemon.json");
            // allowed_extensions is empty — hippo-browser@local absent.
            let json = serde_json::json!({
                "name": "hippo_daemon",
                "path": wrapper.to_string_lossy(),
                "type": "stdio",
                "allowed_extensions": [],
            });
            fs::write(&manifest, serde_json::to_string_pretty(&json).unwrap()).unwrap();

            let fail = check_nm_manifest(&manifest, false);
            assert_eq!(fail, 1, "empty allowed_extensions must fail");
        }

        #[test]
        fn check_2_explain_output_does_not_panic() {
            let tmp = tempdir().unwrap();
            let manifest = tmp.path().join("hippo_daemon.json");
            // explain=true on missing manifest must not panic.
            let _ = check_nm_manifest(&manifest, true);
        }

        // ── Check 5: live-session vs DB ────────────────────────────────────

        #[test]
        fn check_5_active_session_missing_from_db_fails() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            // Fake projects dir with one fresh JSONL file.
            let projects = tmp.path().join("projects/abc");
            fs::create_dir_all(&projects).unwrap();
            let jsonl = projects.join("session-uuid-1234.jsonl");
            fs::write(&jsonl, "{}\n").unwrap();
            // File is fresh (just written) — mtime < 5min by default.

            // Do NOT insert any row into claude_sessions — session is "missing".
            let fail = check_claude_session_db(&tmp.path().join("projects"), &conn, false);
            assert_eq!(fail, 1, "active session absent from DB must yield fail=1");
        }

        #[test]
        fn check_5_active_session_present_in_db_passes() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            let projects = tmp.path().join("projects/abc");
            fs::create_dir_all(&projects).unwrap();
            let session_id = "session-uuid-5678";
            let jsonl = projects.join(format!("{session_id}.jsonl"));
            fs::write(&jsonl, "{}\n").unwrap();

            insert_session_row(&conn, session_id);

            let fail = check_claude_session_db(&tmp.path().join("projects"), &conn, false);
            assert_eq!(fail, 0, "active session in DB must pass");
        }

        #[test]
        fn check_5_no_active_sessions_is_not_data() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            // Projects dir exists but the one JSONL is old (> 5 min).
            let projects = tmp.path().join("projects/abc");
            fs::create_dir_all(&projects).unwrap();
            let jsonl = projects.join("old-session.jsonl");
            fs::write(&jsonl, "{}\n").unwrap();
            back_date_file(&jsonl, Duration::from_secs(600)); // 10 minutes old

            let fail = check_claude_session_db(&tmp.path().join("projects"), &conn, false);
            assert_eq!(fail, 0, "no active sessions → [--], not a failure");
        }

        #[test]
        fn check_5_missing_sessions_capped_at_3() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            let projects = tmp.path().join("projects/abc");
            fs::create_dir_all(&projects).unwrap();

            // Create 5 fresh JSONL files, none in the DB.
            for i in 0..5u32 {
                let jsonl = projects.join(format!("session-{i}.jsonl"));
                fs::write(&jsonl, "{}\n").unwrap();
            }

            let fail = check_claude_session_db(&tmp.path().join("projects"), &conn, false);
            assert_eq!(
                fail, 3,
                "fail_count must be capped at 3 regardless of missing count"
            );
        }

        #[test]
        fn check_5_nonexistent_projects_dir_is_not_data() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            let fail = check_claude_session_db(&tmp.path().join("no-such-dir"), &conn, false);
            assert_eq!(fail, 0, "non-existent projects dir → [--], not a failure");
        }

        // ── Check 6: session-hook log vs DB ───────────────────────────────

        /// Write timestamped "hook invoked" lines into a log file.
        /// `age_secs_list`: each entry is how many seconds ago to back-date the line.
        fn write_hook_log(log_path: &std::path::Path, age_secs_list: &[u64]) {
            let mut f = fs::File::create(log_path).unwrap();
            for &age_secs in age_secs_list {
                let ts = chrono::Utc::now() - chrono::TimeDelta::seconds(age_secs as i64);
                writeln!(
                    f,
                    "{} hook invoked, input={{}}",
                    ts.to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
                )
                .unwrap();
            }
        }

        #[test]
        fn check_6_three_invocations_no_db_rows_fails() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            let log = tmp.path().join("session-hook-debug.log");
            // 3 recent hook invocations, none older than 1h.
            write_hook_log(&log, &[10, 60, 120]);

            // No claude_sessions rows → 0 DB rows in last 1h.
            let fail = check_session_hook_log(&log, &conn, false);
            assert_eq!(fail, 1, "≥3 invocations with 0 DB rows must fail");
        }

        #[test]
        fn check_6_two_invocations_no_db_rows_warns_not_fails() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            let log = tmp.path().join("session-hook-debug.log");
            write_hook_log(&log, &[10, 60]); // only 2 invocations

            let fail = check_session_hook_log(&log, &conn, false);
            assert_eq!(fail, 0, "< 3 invocations with 0 DB rows is [WW], not [!!]");
        }

        #[test]
        fn check_6_no_log_file_is_no_data() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            let log = tmp.path().join("session-hook-debug.log");
            // File does not exist.
            let fail = check_session_hook_log(&log, &conn, false);
            assert_eq!(
                fail, 0,
                "missing log file → [--] no activity, not a failure"
            );
        }

        #[test]
        fn check_6_invocations_with_db_rows_passes() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            let log = tmp.path().join("session-hook-debug.log");
            write_hook_log(&log, &[30, 90, 150]);

            insert_session_row(&conn, "recent-session-abc");

            let fail = check_session_hook_log(&log, &conn, false);
            assert_eq!(fail, 0, "invocations + DB rows → [OK]");
        }

        #[test]
        fn check_6_old_log_lines_outside_1h_not_counted() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            let log = tmp.path().join("session-hook-debug.log");
            // 5 lines but all older than 1h.
            write_hook_log(&log, &[3700, 4000, 5000, 6000, 7200]);

            let fail = check_session_hook_log(&log, &conn, false);
            assert_eq!(fail, 0, "invocations outside 1h window should not count");
        }

        // ── Check 9: fallback file age ─────────────────────────────────────

        #[test]
        fn check_9_no_fallback_files_passes() {
            let tmp = tempdir().unwrap();
            let fallback = tmp.path().join("fallback");
            fs::create_dir_all(&fallback).unwrap();

            let fail = check_fallback_age(&fallback, true, false);
            assert_eq!(fail, 0, "no fallback files → [OK]");
        }

        #[test]
        fn check_9_stale_file_daemon_up_fails() {
            let tmp = tempdir().unwrap();
            let fallback = tmp.path().join("fallback");
            fs::create_dir_all(&fallback).unwrap();

            let old_file = fallback.join("2026-04-01.jsonl");
            fs::write(&old_file, "{}\n").unwrap();
            back_date_file(&old_file, Duration::from_secs(25 * 3600)); // 25 hours

            let fail = check_fallback_age(&fallback, /*daemon_reachable=*/ true, false);
            assert_eq!(fail, 1, "stale file with daemon up must fail");
        }

        #[test]
        fn check_9_stale_file_daemon_down_warns_not_fails() {
            let tmp = tempdir().unwrap();
            let fallback = tmp.path().join("fallback");
            fs::create_dir_all(&fallback).unwrap();

            let old_file = fallback.join("2026-04-01.jsonl");
            fs::write(&old_file, "{}\n").unwrap();
            back_date_file(&old_file, Duration::from_secs(25 * 3600));

            // daemon is down — drain cannot run, so stale files are expected.
            let fail = check_fallback_age(&fallback, /*daemon_reachable=*/ false, false);
            assert_eq!(fail, 0, "stale file with daemon down → [WW], not [!!]");
        }

        #[test]
        fn check_9_fresh_file_warns_not_fails() {
            let tmp = tempdir().unwrap();
            let fallback = tmp.path().join("fallback");
            fs::create_dir_all(&fallback).unwrap();

            let fresh = fallback.join("today.jsonl");
            fs::write(&fresh, "{}\n").unwrap();
            // File is < 24h old (just written), daemon is up.

            let fail = check_fallback_age(&fallback, true, false);
            assert_eq!(fail, 0, "fresh file → [WW] not [!!]");
        }

        #[test]
        fn check_9_explain_on_stale_does_not_panic() {
            let tmp = tempdir().unwrap();
            let fallback = tmp.path().join("fallback");
            fs::create_dir_all(&fallback).unwrap();

            let old = fallback.join("old.jsonl");
            fs::write(&old, "{}\n").unwrap();
            back_date_file(&old, Duration::from_secs(25 * 3600));

            let _ = check_fallback_age(&fallback, true, /*explain=*/ true);
        }

        // ── Check 10: schema version ───────────────────────────────────────

        #[test]
        fn check_10_version_match_passes() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            let current_version: i64 = conn
                .query_row("PRAGMA user_version", [], |r| r.get(0))
                .unwrap();

            let brain_json = serde_json::json!({
                "expected_schema_version": current_version,
                "accepted_read_versions": [],
            });

            let fail = check_schema_version(&conn, Some(&brain_json), false);
            assert_eq!(fail, 0, "matching versions must pass");
        }

        #[test]
        fn check_10_version_mismatch_fails() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            // Brain expects a future version the daemon DB doesn't have yet.
            let brain_json = serde_json::json!({
                "expected_schema_version": 9999,
                "accepted_read_versions": [],
            });

            let fail = check_schema_version(&conn, Some(&brain_json), false);
            assert_eq!(fail, 1, "version mismatch must fail");
        }

        #[test]
        fn check_10_db_version_in_accepted_read_versions_passes() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            let current_version: i64 = conn
                .query_row("PRAGMA user_version", [], |r| r.get(0))
                .unwrap();

            // Brain has moved forward but still accepts the daemon's version.
            let brain_json = serde_json::json!({
                "expected_schema_version": current_version + 1,
                "accepted_read_versions": [current_version],
            });

            let fail = check_schema_version(&conn, Some(&brain_json), false);
            assert_eq!(fail, 0, "version in accepted_read_versions must pass");
        }

        #[test]
        fn check_10_brain_unreachable_is_not_data() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            let fail = check_schema_version(&conn, None, false);
            assert_eq!(fail, 0, "brain unreachable → [--], not a failure");
        }

        #[test]
        fn check_10_brain_json_missing_field_is_not_data() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            // Brain JSON present but old (no expected_schema_version field).
            let brain_json = serde_json::json!({ "status": "ok" });
            let fail = check_schema_version(&conn, Some(&brain_json), false);
            assert_eq!(fail, 0, "missing field → [--], not a failure");
        }

        // ── Performance budget ─────────────────────────────────────────────

        /// Assert the combined wall-clock of all five checks is < 2 seconds.
        ///
        /// Uses a tempdir DB with representative data so I/O is included.
        #[test]
        fn doctor_perf_budget() {
            let tmp = tempdir().unwrap();
            let conn = open_test_db(tmp.path());

            // Seed some data to make the checks non-trivial.
            for i in 0..10u32 {
                insert_session_row(&conn, &format!("perf-session-{i}"));
            }

            // Manifest fixture.
            let nm_dir = tmp.path().join("nm_hosts");
            let manifest = write_valid_manifest(&nm_dir);

            // Projects dir with one fresh session.
            let projects = tmp.path().join("projects/proj1");
            fs::create_dir_all(&projects).unwrap();
            let session_id = "perf-session-0";
            fs::write(projects.join(format!("{session_id}.jsonl")), "{}\n").unwrap();

            // Hook log with a few recent lines.
            let log = tmp.path().join("session-hook-debug.log");
            {
                let mut f = fs::File::create(&log).unwrap();
                let ts = chrono::Utc::now();
                writeln!(
                    f,
                    "{} hook invoked, input={{}}",
                    ts.to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
                )
                .unwrap();
            }

            // Fallback dir with one fresh file.
            let fallback = tmp.path().join("fallback");
            fs::create_dir_all(&fallback).unwrap();
            fs::write(fallback.join("today.jsonl"), "{}\n").unwrap();

            // Brain JSON for Check 10.
            let db_ver: i64 = conn
                .query_row("PRAGMA user_version", [], |r| r.get(0))
                .unwrap();
            let brain_json = serde_json::json!({
                "expected_schema_version": db_ver,
                "accepted_read_versions": [],
            });

            let start = std::time::Instant::now();

            let _ = check_nm_manifest(&manifest, false);
            let _ = check_claude_session_db(&tmp.path().join("projects"), &conn, false);
            let _ = check_session_hook_log(&log, &conn, false);
            let _ = check_fallback_age(&fallback, true, false);
            let _ = check_schema_version(&conn, Some(&brain_json), false);

            let elapsed = start.elapsed();
            assert!(
                elapsed < Duration::from_secs(2),
                "all five checks combined took {:?}, must be < 2s",
                elapsed
            );
        }

        // ── Config struct used by perf test ───────────────────────────────

        #[allow(dead_code)]
        fn _make_config(dir: &std::path::Path) -> HippoConfig {
            let mut cfg = HippoConfig::default();
            cfg.storage.data_dir = dir.join("data");
            cfg.storage.config_dir = dir.join("config");
            cfg
        }
    }
}
