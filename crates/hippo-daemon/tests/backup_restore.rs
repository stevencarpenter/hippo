//! Integration tests for `hippo backup verify` and `hippo backup restore`.

use age::secrecy::ExposeSecret;
use hippo_core::config::HippoConfig;
use hippo_core::storage::EXPECTED_VERSION;
use hippo_daemon::backup::artifact::ArtifactKind;
use hippo_daemon::backup::manifest::Manifest;
use hippo_daemon::backup::restore::{
    RestoreOpts, RestoreTarget, ServiceControl, loss_report, restore_with, verify,
};
use hippo_daemon::backup::run::{RunMode, RunReport, run_backup};
use hippo_daemon::backup::status;
use rusqlite::Connection;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

const H: i64 = 3_600_000;
/// Monday 2026-11-02 01:00:00 UTC (ISO week 45).
const T0: i64 = 1_793_581_200_000;

struct Fx {
    t: tempfile::TempDir,
    config: HippoConfig,
    dest: PathBuf,
    work: PathBuf,
    key_ref: String,
}

/// Configured backup over a live WAL database. `schema` creates the tables.
fn fixture_with(schema: impl FnOnce(&Path)) -> Fx {
    let t = tempfile::tempdir().unwrap();
    let (data, cfg, dest) = (
        t.path().join("data"),
        t.path().join("config"),
        t.path().join("dest"),
    );
    for d in [&data, &cfg, &dest] {
        fs::create_dir_all(d).unwrap();
    }
    fs::write(cfg.join("config.toml"), "[backup]\n").unwrap();
    schema(&data.join("hippo.db"));
    let identity = age::x25519::Identity::generate();
    let key = t.path().join("key.txt");
    fs::write(&key, identity.to_string().expose_secret()).unwrap();
    let mut config = HippoConfig::default();
    config.storage.data_dir = data;
    config.storage.config_dir = cfg;
    config.backup.destination = dest.to_string_lossy().into_owned();
    config.backup.recipient = identity.to_public().to_string();
    let work = config.backup_work_dir();
    Fx {
        t,
        config,
        dest,
        work,
        key_ref: format!("file:{}", key.display()),
    }
}

fn fixture() -> Fx {
    let fx = fixture_with(|db| {
        let c = Connection::open(db).unwrap();
        c.pragma_update(None, "journal_mode", "WAL").unwrap();
        c.execute_batch(
            "CREATE TABLE events(id INTEGER PRIMARY KEY, body TEXT,
                 created_at INTEGER NOT NULL DEFAULT 0, probe_tag TEXT);
             PRAGMA user_version = 29;",
        )
        .unwrap();
    });
    add_rows(&fx, 200);
    fx
}

fn add_rows(fx: &Fx, n: usize) {
    let c = Connection::open(fx.config.db_path()).unwrap();
    let tx = c.unchecked_transaction().unwrap();
    for i in 0..n {
        tx.execute(
            "INSERT INTO events(body, created_at) VALUES (?1, ?2)",
            rusqlite::params![format!("{i:0>200}"), T0 + 2 * H],
        )
        .unwrap();
    }
    tx.commit().unwrap();
}

fn count(db: &Path) -> i64 {
    Connection::open(db)
        .unwrap()
        .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
        .unwrap()
}

fn backup(fx: &Fx, now_ms: i64) -> (String, ArtifactKind) {
    match run_backup(&fx.config, RunMode::Scheduled, now_ms).unwrap() {
        RunReport::Done {
            id,
            kind,
            shipped: true,
        } => (id, kind),
        other => panic!("expected a shipped Done, got {other:?}"),
    }
}

fn opts(fx: &Fx) -> RestoreOpts {
    RestoreOpts {
        yes: true,
        identity: Some(fx.key_ref.clone()),
        ..RestoreOpts::default()
    }
}

#[derive(Default)]
struct StubServices {
    log: Vec<&'static str>,
    /// Runs inside `stop`, to inject a fault that only the swap sees.
    on_stop: Option<Box<dyn FnMut()>>,
}

impl ServiceControl for StubServices {
    fn stop(&mut self) -> anyhow::Result<()> {
        self.log.push("stop");
        if let Some(f) = self.on_stop.as_mut() {
            f();
        }
        Ok(())
    }
    fn start(&mut self) -> anyhow::Result<()> {
        self.log.push("start");
        Ok(())
    }
}

/// Names in `dir` that start with `prefix`.
fn entries_with(dir: &Path, prefix: &str) -> Vec<String> {
    fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with(prefix))
        .collect()
}

fn mode(p: &Path) -> u32 {
    fs::metadata(p).unwrap().permissions().mode() & 0o777
}

fn wal(live: &Path) -> PathBuf {
    let mut p = live.as_os_str().to_owned();
    p.push("-wal");
    PathBuf::from(p)
}

fn file_sha(p: &Path) -> Vec<u8> {
    use sha2::{Digest, Sha256};
    Sha256::digest(fs::read(p).unwrap()).to_vec()
}

#[test]
fn verify_full_and_delta() {
    let mut fx = fixture();
    let (full, kind) = backup(&fx, T0);
    assert_eq!(kind, ArtifactKind::Full);
    add_rows(&fx, 300);
    let (delta, kind) = backup(&fx, T0 + 21 * H);
    assert_eq!(kind, ArtifactKind::Delta);

    let err = verify(&fx.config, None).unwrap_err();
    assert!(format!("{err:#}").contains("identity_ref"), "{err:#}");

    fx.config.backup.identity_ref = fx.key_ref.clone();
    verify(&fx.config, Some(&full)).unwrap();
    verify(&fx.config, Some(&delta)).unwrap();
    verify(&fx.config, None).unwrap();
    assert!(status::load(&fx.work).last_verify_ms.is_some());
    assert!(entries_with(&fx.work, "verify-tmp").is_empty());

    // A corrupt delta fails and does not record a verify.
    status::save(&fx.work, &status::BackupStatus::default()).unwrap();
    let file = fx.dest.join(format!("{delta}.tar.zst.age"));
    let mut bytes = fs::read(&file).unwrap();
    let mid = bytes.len() / 2;
    bytes[mid] ^= 0xff;
    fs::write(&file, bytes).unwrap();
    let err = verify(&fx.config, Some(&delta)).unwrap_err();
    assert!(
        format!("{err:#}").contains("ciphertext SHA-256 does not match the manifest"),
        "{err:#}"
    );
    assert_eq!(status::load(&fx.work).last_verify_ms, None);
    assert!(entries_with(&fx.work, "verify-tmp").is_empty());

    assert!(verify(&fx.config, Some("hippo-full-nope")).is_err());
}

#[test]
fn restore_replaces_db_and_checkpoints_previous() {
    let fx = fixture();
    let (full, _) = backup(&fx, T0);
    add_rows(&fx, 300);
    let live = fx.config.db_path();
    assert_eq!(count(&live), 500);

    let mut svc = StubServices::default();
    restore_with(
        &fx.config,
        RestoreTarget::Id(full),
        &opts(&fx),
        &mut svc,
        &mut &b""[..],
    )
    .unwrap();

    assert_eq!(svc.log, ["stop", "start"]);
    assert_eq!(count(&live), 200);
    let data = &fx.config.storage.data_dir;
    for gone in ["hippo.db-wal", "hippo.db-shm", "hippo.db.restoring"] {
        assert!(!data.join(gone).exists(), "{gone} left behind");
    }
    assert!(entries_with(&fx.work, "restore-tmp").is_empty());

    // The pre-restore checkpoint holds the replaced database and can itself be restored.
    let m = Manifest::load(&fx.dest).unwrap();
    let cp = m
        .entries
        .iter()
        .find(|e| e.meta.kind == ArtifactKind::Checkpoint)
        .expect("pre-restore checkpoint in the manifest");
    assert_eq!(cp.meta.label.as_deref(), Some("pre-restore"));
    assert_eq!(cp.meta.row_counts["events"], 500);
    restore_with(
        &fx.config,
        RestoreTarget::Id(cp.meta.id.clone()),
        &opts(&fx),
        &mut StubServices::default(),
        &mut &b""[..],
    )
    .unwrap();
    assert_eq!(count(&live), 500);
}

#[test]
fn restore_without_backup_config_snapshots_previous_db() {
    let mut fx = fixture();
    let (full, _) = backup(&fx, T0);
    add_rows(&fx, 300);
    // A new machine: no [backup] section, destination and identity given on the command line.
    fx.config.backup = Default::default();
    let o = RestoreOpts {
        destination: Some(fx.dest.clone()),
        ..opts(&fx)
    };
    restore_with(
        &fx.config,
        RestoreTarget::Latest,
        &o,
        &mut StubServices::default(),
        &mut &b""[..],
    )
    .unwrap();
    assert_eq!(count(&fx.config.db_path()), 200);
    let saved: Vec<PathBuf> = fs::read_dir(&fx.work)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| {
            let n = p.file_name().unwrap().to_string_lossy();
            n.starts_with("pre-restore-") && n.ends_with(".db")
        })
        .collect();
    assert_eq!(saved.len(), 1, "{saved:?}");
    assert_eq!(count(&saved[0]), 500);
    let _ = full;
}

#[test]
fn restore_aborts_without_confirmation() {
    let fx = fixture();
    let (full, _) = backup(&fx, T0);
    add_rows(&fx, 300);
    let live = fx.config.db_path();
    let before = file_sha(&live);
    let mut svc = StubServices::default();
    let o = RestoreOpts {
        yes: false,
        ..opts(&fx)
    };
    let err = restore_with(
        &fx.config,
        RestoreTarget::Id(full),
        &o,
        &mut svc,
        &mut &b"no\n"[..],
    )
    .unwrap_err();
    assert!(format!("{err:#}").contains("aborted"), "{err:#}");
    assert!(svc.log.is_empty());
    assert_eq!(file_sha(&live), before);
}

#[test]
fn restore_refuses_newer_schema() {
    let fx = fixture();
    let live = fx.config.db_path();
    let set_version = |v: i64| {
        Connection::open(&live)
            .unwrap()
            .pragma_update(None, "user_version", v)
            .unwrap();
    };
    set_version(EXPECTED_VERSION + 1);
    let (id, _) = backup(&fx, T0);
    set_version(EXPECTED_VERSION);
    let before = file_sha(&live);

    let mut svc = StubServices::default();
    let err = restore_with(
        &fx.config,
        RestoreTarget::Id(id),
        &opts(&fx),
        &mut svc,
        &mut &b""[..],
    )
    .unwrap_err();
    assert!(format!("{err:#}").contains("newer"), "{err:#}");
    assert!(svc.log.is_empty());
    assert_eq!(file_sha(&live), before);
}

#[test]
fn restore_older_schema_then_open_db_migrates() {
    let fx = fixture_with(|db| {
        let c = hippo_core::storage::open_db(db).unwrap();
        // Roll the fresh database back to v28: v29 only added the eval_* tables.
        c.execute_batch(
            "DROP TABLE eval_bench_runs; DROP TABLE eval_escapes; DROP TABLE eval_verdicts;
             DROP TABLE eval_findings; DROP TABLE eval_review_runs;
             PRAGMA user_version = 28;",
        )
        .unwrap();
    });
    let (id, _) = backup(&fx, T0);
    let live = fx.config.db_path();
    drop(hippo_core::storage::open_db(&live).unwrap()); // live moves on to v29

    restore_with(
        &fx.config,
        RestoreTarget::Id(id),
        &opts(&fx),
        &mut StubServices::default(),
        &mut &b""[..],
    )
    .unwrap();
    let version = |c: &Connection| -> i64 {
        c.query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap()
    };
    assert_eq!(version(&Connection::open(&live).unwrap()), 28);

    let c = hippo_core::storage::open_db(&live).unwrap();
    assert_eq!(version(&c), EXPECTED_VERSION);
    let has_eval: bool = c
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name = 'eval_review_runs')",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(has_eval);
}

#[test]
fn loss_report_counts_rows_after_cutoff() {
    let c = Connection::open_in_memory().unwrap();
    c.execute_batch(
        "CREATE TABLE events(id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, probe_tag TEXT);
         CREATE TABLE browser_events(id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, probe_tag TEXT);
         CREATE TABLE knowledge_nodes(id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL);
         INSERT INTO knowledge_nodes(created_at) VALUES (5), (100), (101);
         CREATE TABLE agentic_sessions(id INTEGER PRIMARY KEY, harness TEXT NOT NULL,
             created_at INTEGER NOT NULL, end_time INTEGER NOT NULL, probe_tag TEXT);
         INSERT INTO events(created_at, probe_tag) VALUES (99, NULL), (100, NULL), (150, NULL), (200, 'probe');
         INSERT INTO browser_events(created_at) VALUES (50), (101);
         INSERT INTO agentic_sessions(harness, created_at, end_time) VALUES
             ('claude-code', 10, 20),   -- before the cutoff
             ('claude-code', 10, 120),  -- grew after the cutoff
             ('codex', 130, 90),        -- ingested after the cutoff
             ('codex', 140, 150);",
    )
    .unwrap();
    assert_eq!(
        loss_report(&c, 100).unwrap(),
        [
            ("shell".to_owned(), Some(2)),
            ("browser".to_owned(), Some(1)),
            ("knowledge".to_owned(), Some(2)),
            ("agentic:claude-code".to_owned(), Some(1)),
            ("agentic:codex".to_owned(), Some(2)),
        ]
    );

    let only_events = Connection::open_in_memory().unwrap();
    only_events
        .execute_batch(
            "CREATE TABLE events(id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL);
             CREATE TABLE browser_events(id INTEGER PRIMARY KEY);",
        )
        .unwrap();
    // A missing table is left out; a table without created_at is reported as skipped.
    assert_eq!(
        loss_report(&only_events, 0).unwrap(),
        [("shell".to_owned(), Some(0)), ("browser".to_owned(), None)]
    );
}

#[test]
fn extract_does_not_touch_live_db() {
    let fx = fixture();
    backup(&fx, T0);
    add_rows(&fx, 300);
    let (delta, kind) = backup(&fx, T0 + 21 * H);
    assert_eq!(kind, ArtifactKind::Delta);
    add_rows(&fx, 10);
    let live = fx.config.db_path();
    let before = file_sha(&live);

    let out = fx.t.path().join("extracted");
    let mut svc = StubServices::default();
    let o = RestoreOpts {
        yes: false,
        extract: Some(out.clone()),
        ..opts(&fx)
    };
    restore_with(
        &fx.config,
        RestoreTarget::Id(delta.clone()),
        &o,
        &mut svc,
        &mut &b""[..],
    )
    .unwrap();
    assert!(svc.log.is_empty());
    assert_eq!(file_sha(&live), before);
    assert_eq!(count(&out.join("hippo.db")), 500);
    assert!(out.join("config/config.toml").is_file());
    assert!(out.join("meta.json").is_file());
    assert_eq!(mode(&out), 0o700, "--extract creates DIR 0700");
    assert_eq!(mode(&out.join("hippo.db")), 0o600, "delta output is 0600");
    let mut names: Vec<String> = fs::read_dir(&out)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    assert_eq!(names, ["config", "hippo.db", "meta.json"]);

    // A non-empty directory is refused.
    let err = restore_with(
        &fx.config,
        RestoreTarget::Id(delta),
        &o,
        &mut svc,
        &mut &b""[..],
    )
    .unwrap_err();
    assert!(format!("{err:#}").contains("not empty"), "{err:#}");
}

/// Makes the live database carry a non-empty WAL holding `n` rows that the main file
/// lacks: the files are copied while a connection with autocheckpoint off is open.
fn leave_stale_wal(fx: &Fx, n: usize) {
    let live = fx.config.db_path();
    let side = fx.t.path().join("side");
    fs::create_dir_all(&side).unwrap();
    let names = ["hippo.db", "hippo.db-wal", "hippo.db-shm"];
    {
        let c = Connection::open(&live).unwrap();
        c.pragma_update(None, "wal_autocheckpoint", 0).unwrap();
        let tx = c.unchecked_transaction().unwrap();
        for _ in 0..n {
            tx.execute("INSERT INTO events(body) VALUES ('wal-only')", [])
                .unwrap();
        }
        tx.commit().unwrap();
        for name in names {
            fs::copy(fx.config.storage.data_dir.join(name), side.join(name)).unwrap();
        }
    }
    for name in names {
        fs::copy(side.join(name), fx.config.storage.data_dir.join(name)).unwrap();
    }
    assert!(fs::metadata(wal(&live)).unwrap().len() > 0);
}

#[test]
fn restore_folds_a_stale_live_wal_before_the_swap() {
    let fx = fixture();
    let (full, _) = backup(&fx, T0);
    leave_stale_wal(&fx, 300);
    let live = fx.config.db_path();

    let mut svc = StubServices::default();
    restore_with(
        &fx.config,
        RestoreTarget::Id(full),
        &opts(&fx),
        &mut svc,
        &mut &b""[..],
    )
    .unwrap();
    assert_eq!(svc.log, ["stop", "start"]);
    assert!(!wal(&live).exists());
    assert_eq!(
        count(&live),
        200,
        "restored rows plus none from the old WAL"
    );
    let m = Manifest::load(&fx.dest).unwrap();
    let cp = m
        .entries
        .iter()
        .find(|e| e.meta.kind == ArtifactKind::Checkpoint)
        .unwrap();
    assert_eq!(
        cp.meta.row_counts["events"], 500,
        "checkpoint saw the WAL rows"
    );
}

#[test]
fn restore_moves_a_stale_wal_aside_when_the_live_db_is_missing() {
    let fx = fixture();
    let (full, _) = backup(&fx, T0);
    leave_stale_wal(&fx, 300);
    // The operator moved a broken database away, leaving its -wal and -shm.
    let live = fx.config.db_path();
    fs::rename(&live, fx.t.path().join("hippo.db.broken")).unwrap();
    let stale = fs::read(wal(&live)).unwrap();

    restore_with(
        &fx.config,
        RestoreTarget::Id(full),
        &opts(&fx),
        &mut StubServices::default(),
        &mut &b""[..],
    )
    .unwrap();
    assert!(!wal(&live).exists());
    assert_eq!(count(&live), 200, "no frame of the stale WAL was applied");
    let moved = entries_with(&fx.work, "pre-restore-");
    let wal_copy = moved
        .iter()
        .find(|n| n.ends_with(".db-wal"))
        .unwrap_or_else(|| panic!("-wal not moved into the work dir: {moved:?}"));
    assert_eq!(fs::read(fx.work.join(wal_copy)).unwrap(), stale);
    assert!(moved.iter().any(|n| n.ends_with(".db-shm")), "{moved:?}");
}

#[test]
fn restore_refuses_while_a_connection_holds_the_live_db() {
    let fx = fixture();
    let (full, _) = backup(&fx, T0);
    add_rows(&fx, 300);
    let live = fx.config.db_path();
    let holder = Connection::open(&live).unwrap();
    let _: i64 = holder
        .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
        .unwrap();
    let before = file_sha(&live);

    let mut svc = StubServices::default();
    let err = restore_with(
        &fx.config,
        RestoreTarget::Id(full),
        &opts(&fx),
        &mut svc,
        &mut &b""[..],
    )
    .unwrap_err();
    assert!(format!("{err:#}").contains("still open"), "{err:#}");
    assert_eq!(svc.log, ["stop", "start"]);
    assert_eq!(file_sha(&live), before);
    drop(holder);
    assert_eq!(count(&live), 500);
}

#[test]
fn restore_refuses_while_another_process_holds_the_live_db() {
    if std::process::Command::new("lsof")
        .arg("-v")
        .output()
        .is_err()
    {
        eprintln!("lsof not installed; skipping");
        return;
    }
    let fx = fixture();
    let (full, _) = backup(&fx, T0);
    let live = fx.config.db_path();
    let mut child = std::process::Command::new("sleep")
        .arg("60")
        .stdin(fs::File::open(&live).unwrap())
        .spawn()
        .unwrap();
    let before = file_sha(&live);

    let mut svc = StubServices::default();
    let err = restore_with(
        &fx.config,
        RestoreTarget::Id(full),
        &opts(&fx),
        &mut svc,
        &mut &b""[..],
    );
    let _ = child.kill();
    let _ = child.wait();
    let msg = format!("{:#}", err.unwrap_err());
    assert!(msg.contains(&child.id().to_string()), "{msg}");
    assert!(msg.contains("hippo-mcp"), "{msg}");
    assert_eq!(svc.log, ["stop", "start"]);
    assert_eq!(file_sha(&live), before);
    // The refusal comes before the pre-restore checkpoint, so it costs no artifact.
    let m = Manifest::load(&fx.dest).unwrap();
    assert!(
        m.entries
            .iter()
            .all(|e| e.meta.kind != ArtifactKind::Checkpoint),
        "a refused restore wrote a checkpoint"
    );
    assert!(entries_with(&fx.work.join("outbox"), "hippo-").is_empty());
}

/// Folds the live WAL, then overwrites `len` bytes at `offset` of the main file.
fn corrupt_live(fx: &Fx, offset: u64, len: usize) {
    use std::io::{Seek, SeekFrom, Write};
    let live = fx.config.db_path();
    Connection::open(&live)
        .unwrap()
        .execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")
        .unwrap();
    let mut f = fs::OpenOptions::new().write(true).open(&live).unwrap();
    f.seek(SeekFrom::Start(offset)).unwrap();
    f.write_all(&vec![0xa5; len]).unwrap();
}

/// The single `pre-restore-<ts>` directory in the work dir.
fn raw_copy_dir(fx: &Fx) -> PathBuf {
    let dirs: Vec<PathBuf> = entries_with(&fx.work, "pre-restore-")
        .into_iter()
        .map(|n| fx.work.join(n))
        .filter(|p| p.is_dir())
        .collect();
    assert_eq!(dirs.len(), 1, "{dirs:?}");
    dirs[0].clone()
}

#[test]
fn restore_replaces_a_live_db_with_corrupt_pages() {
    let fx = fixture();
    let (full, _) = backup(&fx, T0);
    add_rows(&fx, 300);
    corrupt_live(&fx, 3 * 4096, 4 * 4096);
    let live = fx.config.db_path();
    let damaged = file_sha(&live);

    let o = RestoreOpts {
        yes: false,
        ..opts(&fx)
    };
    restore_with(
        &fx.config,
        RestoreTarget::Id(full),
        &o,
        &mut StubServices::default(),
        &mut &b"yes\n"[..],
    )
    .unwrap();
    assert_eq!(count(&live), 200);
    let dir = raw_copy_dir(&fx);
    assert_eq!(mode(&dir), 0o700);
    assert_eq!(file_sha(&dir.join("hippo.db")), damaged, "raw byte copy");
    assert_eq!(mode(&dir.join("hippo.db")), 0o600);
    let m = Manifest::load(&fx.dest).unwrap();
    assert!(
        m.entries
            .iter()
            .all(|e| e.meta.kind != ArtifactKind::Checkpoint),
        "a damaged database gets a raw copy, not a checkpoint"
    );
}

#[test]
fn restore_replaces_an_unreadable_live_db_after_a_raw_copy() {
    let fx = fixture();
    let (full, _) = backup(&fx, T0);
    corrupt_live(&fx, 0, 100); // the header: SQLite no longer opens it
    let live = fx.config.db_path();
    fs::write(wal(&live), vec![0x5a; 8192]).unwrap();

    restore_with(
        &fx.config,
        RestoreTarget::Id(full),
        &opts(&fx),
        &mut StubServices::default(),
        &mut &b""[..],
    )
    .unwrap();
    assert_eq!(count(&live), 200);
    let dir = raw_copy_dir(&fx);
    assert_eq!(
        fs::read(dir.join("hippo.db-wal")).unwrap(),
        vec![0x5a; 8192]
    );
    // The fold fails ("file is not a database"). SQLite itself drops a WAL whose
    // header is invalid, so the raw copy above is the only place it survives.
    assert!(!wal(&live).exists());
}

#[test]
fn restore_restarts_services_when_the_swap_fails() {
    let fx = fixture();
    let (full, _) = backup(&fx, T0);
    add_rows(&fx, 300);
    let data = fx.config.storage.data_dir.clone();
    let live = fx.config.db_path();
    let before = file_sha(&live);
    // After stop, the data directory turns read-only: SQLite cannot create the -shm
    // file, so the pre-restore snapshot and the swap cannot proceed.
    let locked = data.clone();
    let mut svc = StubServices {
        on_stop: Some(Box::new(move || {
            fs::set_permissions(&locked, fs::Permissions::from_mode(0o500)).unwrap();
        })),
        ..StubServices::default()
    };
    let res = restore_with(
        &fx.config,
        RestoreTarget::Id(full),
        &opts(&fx),
        &mut svc,
        &mut &b""[..],
    );
    fs::set_permissions(&data, fs::Permissions::from_mode(0o700)).unwrap();
    assert!(res.is_err());
    assert_eq!(svc.log, ["stop", "start"]);
    assert_eq!(file_sha(&live), before);
    assert_eq!(count(&live), 500);
}
