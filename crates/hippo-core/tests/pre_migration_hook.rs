//! `open_db` runs the registered pre-migration hook once, before any migration statement.
//! The hook is process-global, so one binary registers one hook and the tests serialize.

use hippo_core::storage::{EXPECTED_VERSION, open_db, set_pre_migration_hook};
use rusqlite::Connection;
use std::path::Path;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use tempfile::TempDir;

static SERIAL: Mutex<()> = Mutex::new(());
static FAIL: AtomicBool = AtomicBool::new(false);
/// When set, the hook plays a concurrent migrator: it finishes the migration and then
/// removes a table that a stale re-run of the v28 to v29 step would recreate.
static CONCURRENT: AtomicBool = AtomicBool::new(false);
static CALLS: Mutex<Vec<(i64, i64)>> = Mutex::new(Vec::new());

fn hook(db: &Path, from: i64, to: i64) -> anyhow::Result<()> {
    CALLS.lock().unwrap().push((from, to));
    if CONCURRENT.load(Ordering::SeqCst) {
        Connection::open(db)?.execute_batch(&format!(
            "DROP TABLE eval_findings; PRAGMA user_version = {to};"
        ))?;
    }
    anyhow::ensure!(!FAIL.load(Ordering::SeqCst), "checkpoint refused");
    Ok(())
}

fn setup(fail: bool) -> std::sync::MutexGuard<'static, ()> {
    let guard = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    set_pre_migration_hook(hook);
    FAIL.store(fail, Ordering::SeqCst);
    CONCURRENT.store(false, Ordering::SeqCst);
    CALLS.lock().unwrap().clear();
    guard
}

fn version_of(path: &Path) -> i64 {
    Connection::open(path)
        .unwrap()
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap()
}

/// A current-schema DB marked v28; the v28 to v29 step is additive and re-runnable.
fn v28_db(dir: &TempDir) -> std::path::PathBuf {
    let db = dir.path().join("hippo.db");
    let conn = open_db(&db).unwrap();
    drop(conn);
    Connection::open(&db)
        .unwrap()
        .execute_batch("PRAGMA user_version = 28;")
        .unwrap();
    CALLS.lock().unwrap().clear();
    db
}

#[test]
fn hook_runs_before_migration_and_blocks_on_error() {
    let _g = setup(false);
    let dir = TempDir::new().unwrap();
    let db = v28_db(&dir);

    FAIL.store(true, Ordering::SeqCst);
    let err = open_db(&db).unwrap_err();
    assert!(
        format!("{err:#}").contains("pre-migration checkpoint failed; migration not run"),
        "{err:#}"
    );
    assert_eq!(version_of(&db), 28);

    FAIL.store(false, Ordering::SeqCst);
    CALLS.lock().unwrap().clear();
    open_db(&db).unwrap();
    assert_eq!(*CALLS.lock().unwrap(), vec![(28, 29)]);
    assert_eq!(version_of(&db), EXPECTED_VERSION);
}

#[test]
fn hook_not_called_for_fresh_or_current_db() {
    let _g = setup(true);
    let dir = TempDir::new().unwrap();
    let db = dir.path().join("hippo.db");
    open_db(&db).unwrap();
    open_db(&db).unwrap();
    assert!(CALLS.lock().unwrap().is_empty());
}

#[test]
fn migration_is_not_rerun_when_the_hook_waited_for_another_migrator() {
    let _g = setup(false);
    let dir = TempDir::new().unwrap();
    let db = v28_db(&dir);
    CONCURRENT.store(true, Ordering::SeqCst);

    open_db(&db).unwrap();

    let marker_back: bool = Connection::open(&db)
        .unwrap()
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name = 'eval_findings')",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(!marker_back, "a stale version re-ran the v28 to v29 step");
    assert_eq!(version_of(&db), EXPECTED_VERSION);
}
