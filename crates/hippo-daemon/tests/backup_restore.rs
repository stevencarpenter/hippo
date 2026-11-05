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
}

impl ServiceControl for StubServices {
    fn stop(&mut self) -> anyhow::Result<()> {
        self.log.push("stop");
        Ok(())
    }
    fn start(&mut self) -> anyhow::Result<()> {
        self.log.push("start");
        Ok(())
    }
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
    assert!(!fx.work.join("verify-tmp").exists());

    // A corrupt delta fails and does not record a verify.
    status::save(&fx.work, &status::BackupStatus::default()).unwrap();
    let file = fx.dest.join(format!("{delta}.tar.zst.age"));
    let mut bytes = fs::read(&file).unwrap();
    let mid = bytes.len() / 2;
    bytes[mid] ^= 0xff;
    fs::write(&file, bytes).unwrap();
    assert!(verify(&fx.config, Some(&delta)).is_err());
    assert_eq!(status::load(&fx.work).last_verify_ms, None);
    assert!(!fx.work.join("verify-tmp").exists());

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
    assert!(!fx.work.join("restore-tmp").exists());

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
            ("shell".to_owned(), 2),
            ("browser".to_owned(), 1),
            ("agentic:claude-code".to_owned(), 1),
            ("agentic:codex".to_owned(), 2),
        ]
    );

    let only_events = Connection::open_in_memory().unwrap();
    only_events
        .execute_batch("CREATE TABLE events(id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL);")
        .unwrap();
    assert_eq!(
        loss_report(&only_events, 0).unwrap(),
        [("shell".to_owned(), 0)]
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
