//! One-step SQLite snapshot used by every backup path.

use anyhow::{Context, Result, ensure};
use rusqlite::backup::Backup;
use rusqlite::{Connection, OpenFlags};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::io::Read;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::time::Duration;

/// Facts about a finished snapshot, recorded in the backup `meta.json`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotInfo {
    pub page_size: u32,
    pub page_count: u64,
    pub sha256: [u8; 32],
    pub schema_version: i64,
    /// Row counts for `events`, `knowledge_nodes`, `agentic_sessions`, `browser_events`.
    /// Tables that do not exist are omitted.
    pub row_counts: BTreeMap<String, i64>,
}

const COUNTED_TABLES: [&str; 4] = [
    "events",
    "knowledge_nodes",
    "agentic_sessions",
    "browser_events",
];

/// Copy `src` into a standalone, WAL-free file at `dst` with the SQLite online
/// backup API (all pages in one step). Does not use `open_db`, so no migration runs.
pub fn take_snapshot(src: &Path, dst: &Path) -> Result<SnapshotInfo> {
    // NOFOLLOW rejects any symlink in the path (macOS /var), so resolve the parent
    // directory and keep the check for the database file itself.
    let name = src.file_name().context("source path has no file name")?;
    let src = src
        .parent()
        .unwrap_or(Path::new("."))
        .canonicalize()?
        .join(name);
    let source = Connection::open_with_flags(
        &src,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )
    .with_context(|| format!("open source {}", src.display()))?;
    source.busy_timeout(Duration::from_millis(5000))?;

    // Create dst with mode 0600 before SQLite opens it; SQLite keeps existing permissions.
    std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(dst)
        .with_context(|| format!("create {}", dst.display()))?;
    let mut target = Connection::open(dst)?;
    // shortcut: step(-1) holds a read lock for the whole copy; fine at current DB size.
    Backup::new(&source, &mut target)?.step(-1)?;
    drop(source);

    target.pragma_update(None, "journal_mode", "DELETE")?;
    let check: String = target.pragma_query_value(None, "quick_check", |r| r.get(0))?;
    ensure!(check == "ok", "snapshot quick_check failed: {check}");

    let page_size: u32 = target.pragma_query_value(None, "page_size", |r| r.get(0))?;
    let page_count =
        u64::try_from(target.pragma_query_value::<i64, _>(None, "page_count", |r| r.get(0))?)?;
    let schema_version: i64 = target.pragma_query_value(None, "user_version", |r| r.get(0))?;
    let mut row_counts = BTreeMap::new();
    for table in COUNTED_TABLES {
        let exists: bool = target.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
            [table],
            |r| r.get(0),
        )?;
        if exists {
            let n = target.query_row(&format!("SELECT COUNT(*) FROM \"{table}\""), [], |r| {
                r.get(0)
            })?;
            row_counts.insert(table.to_string(), n);
        }
    }
    target.close().map_err(|(_, e)| e)?;

    let mut hasher = Sha256::new();
    let mut file = std::fs::File::open(dst)?;
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(SnapshotInfo {
        page_size,
        page_count,
        sha256: hasher.finalize().into(),
        schema_version,
        row_counts,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;
    use std::time::{Duration, Instant};

    fn fixture(path: &std::path::Path) {
        let c = Connection::open(path).unwrap();
        c.pragma_update(None, "journal_mode", "WAL").unwrap();
        c.execute_batch(
            "CREATE TABLE events(id INTEGER PRIMARY KEY, body TEXT);
             CREATE VIRTUAL TABLE docs USING fts5(text);
             INSERT INTO docs(text) VALUES ('hello world');
             CREATE TABLE kv_vector_chunks00(rowid INTEGER PRIMARY KEY, vectors BLOB NOT NULL);
             PRAGMA user_version = 29;",
        )
        .unwrap();
        let tx = c.unchecked_transaction().unwrap();
        for i in 0..1000 {
            tx.execute("INSERT INTO events(body) VALUES (?1)", [format!("e{i}")])
                .unwrap();
        }
        for i in 0..500 {
            tx.execute(
                "INSERT INTO kv_vector_chunks00(vectors) VALUES (?1)",
                [vec![i as u8; 64]],
            )
            .unwrap();
        }
        tx.commit().unwrap();
    }

    fn rowids(c: &Connection) -> Vec<(i64, i64)> {
        c.prepare("SELECT _rowid_, rowid FROM kv_vector_chunks00 ORDER BY 1")
            .unwrap()
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap()
    }

    #[test]
    fn snapshot_matches_source_rows() {
        let dir = tempfile::tempdir().unwrap();
        let (src, dst) = (dir.path().join("src.db"), dir.path().join("snap.db"));
        fixture(&src);
        let info = take_snapshot(&src, &dst).unwrap();
        assert_eq!(info.row_counts["events"], 1000);
        assert_eq!(info.schema_version, 29);
        assert!(!info.row_counts.contains_key("knowledge_nodes"));
        let s = Connection::open(&dst).unwrap();
        let mode: String = s
            .pragma_query_value(None, "journal_mode", |r| r.get(0))
            .unwrap();
        assert_eq!(mode, "delete");
        let qc: String = s
            .pragma_query_value(None, "quick_check", |r| r.get(0))
            .unwrap();
        assert_eq!(qc, "ok");
        assert!(!dir.path().join("snap.db-wal").exists());
        let source = Connection::open(&src).unwrap();
        assert_eq!(rowids(&source), rowids(&s));
    }

    #[test]
    fn snapshot_completes_under_concurrent_writes() {
        let dir = tempfile::tempdir().unwrap();
        let (src, dst) = (dir.path().join("src.db"), dir.path().join("snap.db"));
        fixture(&src);
        let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let (s2, p2) = (stop.clone(), src.clone());
        let writer = std::thread::spawn(move || {
            let c = Connection::open(p2).unwrap();
            c.busy_timeout(Duration::from_millis(5000)).unwrap();
            while !s2.load(std::sync::atomic::Ordering::Relaxed) {
                let _ = c.execute("INSERT INTO events(body) VALUES ('w')", []);
            }
        });
        let start = Instant::now();
        let res = take_snapshot(&src, &dst);
        stop.store(true, std::sync::atomic::Ordering::Relaxed);
        writer.join().unwrap();
        res.unwrap();
        assert!(start.elapsed() < Duration::from_secs(10));
        let qc: String = Connection::open(&dst)
            .unwrap()
            .pragma_query_value(None, "quick_check", |r| r.get(0))
            .unwrap();
        assert_eq!(qc, "ok");
    }
}
