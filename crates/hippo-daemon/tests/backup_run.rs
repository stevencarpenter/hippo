//! Integration tests for one backup run: snapshot, kind choice, ship, manifest, status.

use hippo_core::config::HippoConfig;
use hippo_daemon::backup::artifact::{ArtifactKind, ArtifactMeta, extract_artifact};
use hippo_daemon::backup::manifest::{Manifest, ManifestEntry};
use hippo_daemon::backup::pagediff::apply_diff;
use hippo_daemon::backup::run::{
    RunMode, RunReport, checkpoint, checkpoint_once, checkpoint_waiting, run_backup,
};
use hippo_daemon::backup::status;
use rusqlite::Connection;
use sha2::{Digest, Sha256};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const H: i64 = 3_600_000;
const DAY: i64 = 24 * H;
/// Monday 2026-11-02 01:00:00 UTC (ISO week 45).
const T0: i64 = 1_793_581_200_000;

struct Fx {
    t: tempfile::TempDir,
    config: HippoConfig,
    identity: age::x25519::Identity,
    dest: PathBuf,
    work: PathBuf,
}

fn fixture() -> Fx {
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
    let c = Connection::open(data.join("hippo.db")).unwrap();
    c.pragma_update(None, "journal_mode", "WAL").unwrap();
    c.execute_batch(
        "CREATE TABLE events(id INTEGER PRIMARY KEY, body TEXT); PRAGMA user_version = 29;",
    )
    .unwrap();
    drop(c);
    let identity = age::x25519::Identity::generate();
    let mut config = HippoConfig::default();
    config.storage.data_dir = data;
    config.storage.config_dir = cfg;
    config.backup.destination = dest.to_string_lossy().into_owned();
    config.backup.recipient = identity.to_public().to_string();
    let work = config.backup_work_dir();
    let fx = Fx {
        t,
        config,
        identity,
        dest,
        work,
    };
    add_rows(&fx, 200);
    fx
}

fn add_rows(fx: &Fx, n: usize) {
    let c = Connection::open(fx.config.db_path()).unwrap();
    let tx = c.unchecked_transaction().unwrap();
    for i in 0..n {
        tx.execute(
            "INSERT INTO events(body) VALUES (?1)",
            [format!("{i:0>200}")],
        )
        .unwrap();
    }
    tx.commit().unwrap();
}

fn done(r: RunReport) -> (String, ArtifactKind) {
    match r {
        RunReport::Done { id, kind, shipped } => {
            assert!(shipped);
            (id, kind)
        }
        other => panic!("expected Done, got {other:?}"),
    }
}

fn outbox_artifacts(fx: &Fx) -> usize {
    fs::read_dir(fx.work.join("outbox"))
        .unwrap()
        .filter(|e| {
            e.as_ref()
                .unwrap()
                .file_name()
                .to_string_lossy()
                .ends_with(".tar.zst.age")
        })
        .count()
}

fn names(dir: &Path) -> Vec<String> {
    let mut v: Vec<String> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    v.sort();
    v
}

/// Adds a verified delta created 100 days before `T0` (expired) to the manifest in `dest`.
fn seed_expired(dest: &Path, id: &str, file: &str) -> ManifestEntry {
    let e = ManifestEntry {
        meta: ArtifactMeta {
            id: id.into(),
            kind: ArtifactKind::Delta,
            label: None,
            created_ms: T0 - 100 * DAY,
            base_id: Some("gone".into()),
            hippo_version: "0".into(),
            schema_version: 29,
            db_sha256: "x".into(),
            row_counts: Default::default(),
        },
        file: file.into(),
        ciphertext_sha256: "y".into(),
        verified: true,
        shipped_ms: Some(T0 - 100 * DAY),
        uploaded: true,
    };
    let mut m = Manifest::load(dest).unwrap();
    m.entries.push(e.clone());
    m.save(dest).unwrap();
    e
}

fn hex(b: &[u8]) -> String {
    b.iter().fold(String::new(), |mut s, b| {
        use std::fmt::Write;
        let _ = write!(s, "{b:02x}");
        s
    })
}

#[test]
fn first_run_writes_full_then_delta() {
    let fx = fixture();
    let (full_id, kind) = done(run_backup(&fx.config, RunMode::Scheduled, T0).unwrap());
    assert_eq!(kind, ArtifactKind::Full);
    add_rows(&fx, 300);
    let (delta_id, kind) = done(run_backup(&fx.config, RunMode::Scheduled, T0 + 21 * H).unwrap());
    assert_eq!(kind, ArtifactKind::Delta);

    let m = Manifest::load(&fx.dest).unwrap();
    assert_eq!(m.entries.len(), 2);
    assert_eq!(m.entries[0].meta.id, full_id);
    assert_eq!(m.entries[1].meta.id, delta_id);
    assert_eq!(m.entries[1].meta.base_id.as_deref(), Some(full_id.as_str()));
    assert!(m.entries.iter().all(|e| e.verified && e.uploaded));
    assert!(m.entries.iter().all(|e| e.shipped_ms.is_some()));
    assert_eq!(outbox_artifacts(&fx), 0);

    // The delta applied to the full reproduces the database the delta recorded.
    let x = fx.t.path().join("x");
    extract_artifact(
        &fx.dest.join(&m.entries[0].file),
        &fx.identity,
        &x.join("f"),
    )
    .unwrap();
    extract_artifact(
        &fx.dest.join(&m.entries[1].file),
        &fx.identity,
        &x.join("d"),
    )
    .unwrap();
    let out = x.join("restored.db");
    apply_diff(&x.join("f/hippo.db"), &x.join("d/hippo.db.pagediff"), &out).unwrap();
    assert_eq!(
        hex(&Sha256::digest(fs::read(&out).unwrap())),
        m.entries[1].meta.db_sha256
    );
    let n: i64 = Connection::open(&out)
        .unwrap()
        .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(n, 500);

    let s = status::load(&fx.work);
    assert_eq!(s.last_success_ms, Some(T0 + 21 * H));
    assert_eq!(s.last_kind, Some(ArtifactKind::Delta));
    assert_eq!(s.last_error, None);
    assert!(s.pending_upload.is_empty());
    assert_eq!(s.outbox_count, 0);
}

#[test]
fn scheduled_run_skips_within_20h() {
    let fx = fixture();
    done(run_backup(&fx.config, RunMode::Scheduled, T0).unwrap());
    let r = run_backup(&fx.config, RunMode::Scheduled, T0 + 19 * H).unwrap();
    assert!(matches!(r, RunReport::Skipped { .. }), "{r:?}");
    assert_eq!(Manifest::load(&fx.dest).unwrap().entries.len(), 1);
}

#[test]
fn new_iso_week_forces_full() {
    let fx = fixture();
    done(run_backup(&fx.config, RunMode::Scheduled, T0).unwrap());
    add_rows(&fx, 10);
    // Sunday 01:00 is still ISO week 45.
    let (_, kind) = done(run_backup(&fx.config, RunMode::Scheduled, T0 + 6 * DAY).unwrap());
    assert_eq!(kind, ArtifactKind::Delta);
    add_rows(&fx, 10);
    // Monday 01:00 starts ISO week 46.
    let (_, kind) = done(run_backup(&fx.config, RunMode::Scheduled, T0 + 7 * DAY).unwrap());
    assert_eq!(kind, ArtifactKind::Full);
}

#[test]
fn page_size_change_forces_full() {
    let fx = fixture();
    done(run_backup(&fx.config, RunMode::Scheduled, T0).unwrap());
    let c = Connection::open(fx.config.db_path()).unwrap();
    c.pragma_update(None, "journal_mode", "DELETE").unwrap();
    c.pragma_update(None, "page_size", 8192).unwrap();
    c.execute_batch("VACUUM").unwrap();
    let ps: i64 = c
        .pragma_query_value(None, "page_size", |r| r.get(0))
        .unwrap();
    assert_eq!(ps, 8192);
    drop(c);
    let (_, kind) = done(run_backup(&fx.config, RunMode::Scheduled, T0 + 21 * H).unwrap());
    assert_eq!(kind, ArtifactKind::Full);
}

#[test]
fn destination_missing_keeps_outbox_and_does_not_prune() {
    let fx = fixture();
    done(run_backup(&fx.config, RunMode::Scheduled, T0).unwrap());
    let old = seed_expired(&fx.dest, "hippo-delta-old", "hippo-delta-old.tar.zst.age");
    fs::write(fx.dest.join(&old.file), b"old").unwrap();
    let away = fx.t.path().join("dest.away");
    fs::rename(&fx.dest, &away).unwrap();
    let manifest_before = fs::read(away.join("manifest.json")).unwrap();
    add_rows(&fx, 50);

    assert!(run_backup(&fx.config, RunMode::Scheduled, T0 + 21 * H).is_err());
    assert_eq!(outbox_artifacts(&fx), 1);
    assert!(!fx.dest.exists(), "destination must never be created");
    assert!(away.join(&old.file).is_file(), "failed run must not prune");
    assert_eq!(
        fs::read(away.join("manifest.json")).unwrap(),
        manifest_before
    );
    let s = status::load(&fx.work);
    assert_eq!(s.last_success_ms, Some(T0));
    assert!(s.last_error.is_some());
    assert_eq!(s.outbox_count, 1);

    // A later scheduled run during the outage does not add another artifact.
    assert!(run_backup(&fx.config, RunMode::Scheduled, T0 + 22 * H).is_err());
    assert_eq!(outbox_artifacts(&fx), 1);

    fs::rename(&away, &fx.dest).unwrap();
    let stuck_id = fs::read_dir(fx.work.join("outbox"))
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .find_map(|n| n.strip_suffix(".tar.zst.age").map(str::to_owned))
        .unwrap();
    done(run_backup(&fx.config, RunMode::Scheduled, T0 + 23 * H).unwrap());
    assert_eq!(outbox_artifacts(&fx), 0);
    assert!(names(&fx.work.join("outbox")).is_empty());
    let m = Manifest::load(&fx.dest).unwrap();
    let stuck = m.entries.iter().find(|e| e.meta.id == stuck_id).unwrap();
    assert!(stuck.verified && stuck.shipped_ms == Some(T0 + 23 * H));
    assert!(fx.dest.join(&stuck.file).is_file());
    // Full, the stuck delta, the new delta; the expired entry is pruned on success.
    assert_eq!(m.entries.len(), 3);
    assert!(m.entries.iter().all(|e| e.meta.id != old.meta.id));
    assert!(!fx.dest.join(&old.file).exists());
    let s = status::load(&fx.work);
    assert_eq!(s.last_success_ms, Some(T0 + 23 * H));
    assert_eq!(s.last_error, None);
}

#[test]
fn failed_run_leaves_state_unchanged() {
    let fx = fixture();
    done(run_backup(&fx.config, RunMode::Scheduled, T0).unwrap());
    let hashes = fs::read(fx.work.join("pagehashes.bin")).unwrap();
    let manifest = fs::read(fx.dest.join("manifest.json")).unwrap();
    let outbox = fx.work.join("outbox");
    fs::set_permissions(&outbox, fs::Permissions::from_mode(0o500)).unwrap();
    add_rows(&fx, 50);

    let res = run_backup(&fx.config, RunMode::Scheduled, T0 + 21 * H);
    fs::set_permissions(&outbox, fs::Permissions::from_mode(0o700)).unwrap();
    assert!(res.is_err());
    assert_eq!(fs::read(fx.work.join("pagehashes.bin")).unwrap(), hashes);
    assert_eq!(fs::read(fx.dest.join("manifest.json")).unwrap(), manifest);
    assert_eq!(
        names(&fx.work),
        ["lock", "outbox", "pagehashes.bin", "status.json"]
    );
    assert!(names(&outbox).is_empty());
    assert_eq!(names(&fx.dest).len(), 2, "manifest and the full only");
    let s = status::load(&fx.work);
    assert_eq!(s.last_success_ms, Some(T0));
    assert!(s.last_error.is_some());
}

#[test]
fn scheduled_run_skips_when_locked() {
    let fx = fixture();
    fs::create_dir_all(&fx.work).unwrap();
    let lock = fs::File::create(fx.work.join("lock")).unwrap();
    lock.lock().unwrap();
    let r = run_backup(&fx.config, RunMode::Scheduled, T0).unwrap();
    assert!(matches!(r, RunReport::Skipped { .. }), "{r:?}");
    assert!(Manifest::load(&fx.dest).unwrap().entries.is_empty());
    drop(lock);
    done(run_backup(&fx.config, RunMode::Scheduled, T0).unwrap());
}

#[test]
fn checkpoint_waits_for_lock() {
    let fx = fixture();
    fs::create_dir_all(&fx.work).unwrap();
    let lock = fs::File::create(fx.work.join("lock")).unwrap();
    lock.lock().unwrap();
    let hold = Duration::from_millis(1500);
    let holder = std::thread::spawn(move || {
        std::thread::sleep(hold);
        drop(lock);
    });
    let start = Instant::now();
    let (id, kind) = done(checkpoint(&fx.config, "pre-migration v30").unwrap());
    assert!(start.elapsed() >= hold);
    holder.join().unwrap();
    assert_eq!(kind, ArtifactKind::Checkpoint);
    assert!(id.ends_with("-pre-migration-v30"), "{id}");
    // A checkpoint never becomes a delta base.
    assert!(!fx.work.join("pagehashes.bin").exists());
}

#[test]
fn checkpoint_lock_timeout_is_an_error() {
    let fx = fixture();
    fs::create_dir_all(&fx.work).unwrap();
    let lock = fs::File::create(fx.work.join("lock")).unwrap();
    lock.lock().unwrap();
    let r = checkpoint_waiting(&fx.config, "pre-gc", Duration::from_millis(200));
    let err = r.expect_err("a lock timeout must not count as success");
    assert!(format!("{err:#}").contains("timed out"), "{err:#}");
    drop(lock);
}

#[test]
fn checkpoint_once_skips_a_label_that_already_exists() {
    let fx = fixture();
    let label = "pre-migration-v28-v29";
    let first = checkpoint_once(&fx.config, label).unwrap();
    assert!(matches!(first, RunReport::Done { .. }), "{first:?}");
    let artifacts = outbox_artifacts(&fx);
    let manifest_len = Manifest::load(&fx.dest).unwrap().entries.len();
    let second = checkpoint_once(&fx.config, label).unwrap();
    assert!(matches!(second, RunReport::Skipped { .. }), "{second:?}");
    assert_eq!(outbox_artifacts(&fx), artifacts);
    assert_eq!(
        Manifest::load(&fx.dest).unwrap().entries.len(),
        manifest_len
    );
    let other = checkpoint_once(&fx.config, "pre-migration-v29-v30").unwrap();
    assert!(matches!(other, RunReport::Done { .. }), "{other:?}");
}

#[test]
fn now_unconfigured_exits_3() {
    let root = tempfile::tempdir().unwrap();
    let config_root = root.path().join("config");
    let data_root = root.path().join("data");
    fs::create_dir_all(config_root.join("hippo")).unwrap();
    fs::write(
        config_root.join("hippo/config.toml"),
        "[telemetry]\nenabled=false\n",
    )
    .unwrap();
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_hippo"))
        .env("HOME", root.path())
        .env("XDG_CONFIG_HOME", &config_root)
        .env("XDG_DATA_HOME", &data_root)
        .args(["backup", "now"])
        .output()
        .unwrap();
    assert_eq!(
        out.status.code(),
        Some(3),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}

#[test]
fn success_prunes_expired_artifacts_from_destination() {
    let fx = fixture();
    let old = seed_expired(&fx.dest, "hippo-delta-old", "hippo-delta-old.tar.zst.age");
    fs::write(fx.dest.join(&old.file), b"old").unwrap();

    let (id, _) = done(run_backup(&fx.config, RunMode::Scheduled, T0).unwrap());
    let m = Manifest::load(&fx.dest).unwrap();
    assert_eq!(
        m.entries
            .iter()
            .map(|e| e.meta.id.as_str())
            .collect::<Vec<_>>(),
        [id.as_str()]
    );
    assert!(!fx.dest.join(&old.file).exists());
}

#[test]
fn outbox_entry_shipped_before_a_crash_is_recorded() {
    let fx = fixture();
    let away = fx.t.path().join("dest.away");
    fs::rename(&fx.dest, &away).unwrap();
    assert!(run_backup(&fx.config, RunMode::Scheduled, T0).is_err());
    fs::rename(&away, &fx.dest).unwrap();
    // Simulate a crash after `ship` moved the artifact but before the manifest save.
    let outbox = fx.work.join("outbox");
    let file = names(&outbox)
        .into_iter()
        .find(|n| n.ends_with(".tar.zst.age"))
        .unwrap();
    fs::rename(outbox.join(&file), fx.dest.join(&file)).unwrap();

    let r = run_backup(
        &fx.config,
        RunMode::Checkpoint { label: "x".into() },
        T0 + H,
    )
    .unwrap();
    assert_eq!(done(r).1, ArtifactKind::Checkpoint);
    let m = Manifest::load(&fx.dest).unwrap();
    assert!(m.entries.iter().any(|e| e.file == file && e.verified));
    assert!(names(&outbox).is_empty());
}

#[test]
fn prune_saves_manifest_first_and_tolerates_undeletable_file() {
    let fx = fixture();
    let old = seed_expired(&fx.dest, "hippo-delta-old", "hippo-delta-old.tar.zst.age");
    // A non-empty directory under the artifact name makes remove_file fail.
    fs::create_dir_all(fx.dest.join(&old.file).join("x")).unwrap();

    done(run_backup(&fx.config, RunMode::Scheduled, T0).unwrap());
    let m = Manifest::load(&fx.dest).unwrap();
    assert!(m.entries.iter().all(|e| e.meta.id != old.meta.id));
    assert!(fx.dest.join(&old.file).exists());
    assert_eq!(status::load(&fx.work).last_success_ms, Some(T0));
    let r = run_backup(&fx.config, RunMode::Scheduled, T0 + H).unwrap();
    assert!(matches!(r, RunReport::Skipped { .. }), "{r:?}");
}

#[test]
fn manifest_entry_with_traversal_path_is_never_deleted() {
    let fx = fixture();
    let victim = fx.t.path().join("victim");
    fs::write(&victim, b"keep me").unwrap();
    seed_expired(&fx.dest, "hippo-delta-old", "../victim");
    seed_expired(&fx.dest, "../victim", "../victim.tar.zst.age");

    done(run_backup(&fx.config, RunMode::Scheduled, T0).unwrap());
    assert_eq!(fs::read(&victim).unwrap(), b"keep me");
    let m = Manifest::load(&fx.dest).unwrap();
    assert!(m.entries.iter().any(|e| e.file == "../victim"));
    assert!(m.entries.iter().any(|e| e.meta.id == "../victim"));
}

#[test]
fn missing_manifest_over_existing_artifacts_refuses_to_save() {
    let fx = fixture();
    let orphan = fx.dest.join("hippo-full-20261001T000000Z.tar.zst.age");
    fs::write(&orphan, b"earlier artifact").unwrap();

    let err = run_backup(&fx.config, RunMode::Scheduled, T0).unwrap_err();
    assert!(format!("{err:#}").contains("no manifest.json"), "{err:#}");
    assert!(!fx.dest.join("manifest.json").exists());
    assert_eq!(names(&fx.dest), ["hippo-full-20261001T000000Z.tar.zst.age"]);
    assert_eq!(fs::read(&orphan).unwrap(), b"earlier artifact");

    // A checkpoint still lands in the local outbox.
    let r = run_backup(
        &fx.config,
        RunMode::Checkpoint { label: "m".into() },
        T0 + H,
    )
    .unwrap();
    assert!(matches!(r, RunReport::Done { shipped: false, .. }), "{r:?}");
    assert!(!fx.dest.join("manifest.json").exists());
    assert_eq!(outbox_artifacts(&fx), 1);
}

#[test]
fn checkpoint_with_destination_missing_stays_in_outbox() {
    let fx = fixture();
    let away = fx.t.path().join("dest.away");
    fs::rename(&fx.dest, &away).unwrap();

    let r = run_backup(
        &fx.config,
        RunMode::Checkpoint {
            label: "pre".into(),
        },
        T0,
    )
    .unwrap();
    let RunReport::Done { id, kind, shipped } = r else {
        panic!("expected Done, got {r:?}");
    };
    assert_eq!(kind, ArtifactKind::Checkpoint);
    assert!(!shipped);
    assert_eq!(
        names(&fx.work.join("outbox")),
        [format!("{id}.entry.json"), format!("{id}.tar.zst.age")]
    );
    assert!(!fx.dest.exists());
    let s = status::load(&fx.work);
    assert_eq!(s.last_success_ms, None);
    assert!(s.last_error.is_some());
    assert_eq!(s.outbox_count, 1);

    // Once the destination is back, the next run ships it with its manifest entry.
    fs::rename(&away, &fx.dest).unwrap();
    done(run_backup(&fx.config, RunMode::Scheduled, T0 + H).unwrap());
    let m = Manifest::load(&fx.dest).unwrap();
    assert!(m.entries.iter().any(|e| e.meta.id == id && e.verified));
}

#[test]
fn checkpoint_succeeds_with_corrupt_sidecar_in_outbox() {
    let fx = fixture();
    let outbox = fx.work.join("outbox");
    fs::create_dir_all(&outbox).unwrap();
    let bad = outbox.join("hippo-full-20261001T000000Z.entry.json");
    fs::write(&bad, b"{not json").unwrap();

    let (_, kind) =
        done(run_backup(&fx.config, RunMode::Checkpoint { label: "c".into() }, T0).unwrap());
    assert_eq!(kind, ArtifactKind::Checkpoint);
    let s = status::load(&fx.work);
    assert_eq!(s.last_success_ms, Some(T0));
    assert!(
        s.last_error.as_deref().is_some_and(|e| e.contains("parse")),
        "{s:?}"
    );
    assert!(bad.exists());

    // A scheduled run records it and still writes its own artifact.
    let (_, kind) = done(run_backup(&fx.config, RunMode::Scheduled, T0 + 21 * H).unwrap());
    assert_eq!(kind, ArtifactKind::Full);
    let s = status::load(&fx.work);
    assert!(
        s.last_error.as_deref().is_some_and(|e| e.contains("parse")),
        "{s:?}"
    );
}

/// Leaves one Full in the outbox (the destination was away during its run) and returns
/// its artifact file name.
fn stuck_in_outbox(fx: &Fx) -> String {
    let away = fx.t.path().join("dest.away");
    fs::rename(&fx.dest, &away).unwrap();
    assert!(run_backup(&fx.config, RunMode::Scheduled, T0).is_err());
    fs::rename(&away, &fx.dest).unwrap();
    names(&fx.work.join("outbox"))
        .into_iter()
        .find(|n| n.ends_with(".tar.zst.age"))
        .unwrap()
}

#[test]
fn stale_partial_from_a_crashed_run_is_replaced() {
    let fx = fixture();
    let file = stuck_in_outbox(&fx);
    // A run killed during the copy leaves its partial file in the destination.
    fs::write(fx.dest.join(format!(".{file}.partial")), b"half").unwrap();

    done(run_backup(&fx.config, RunMode::Scheduled, T0 + H).unwrap());
    let m = Manifest::load(&fx.dest).unwrap();
    assert!(
        m.entries.iter().any(|e| e.file == file),
        "stuck artifact shipped"
    );
    assert!(
        names(&fx.dest).iter().all(|n| !n.ends_with(".partial")),
        "{:?}",
        names(&fx.dest)
    );
    assert_eq!(outbox_artifacts(&fx), 0);
    assert_eq!(status::load(&fx.work).last_error, None);
}

#[test]
fn permanent_outbox_error_does_not_stop_a_scheduled_run() {
    let fx = fixture();
    let file = stuck_in_outbox(&fx);
    // A different file under the same name: shipping it can never succeed.
    fs::write(fx.dest.join(&file), b"someone else's").unwrap();

    let (id, kind) = done(run_backup(&fx.config, RunMode::Scheduled, T0 + H).unwrap());
    assert_eq!(kind, ArtifactKind::Full);
    let m = Manifest::load(&fx.dest).unwrap();
    assert!(m.entries.iter().any(|e| e.meta.id == id));
    assert_eq!(fs::read(fx.dest.join(&file)).unwrap(), b"someone else's");
    assert_eq!(outbox_artifacts(&fx), 1, "the failed artifact stays");
    let s = status::load(&fx.work);
    assert_eq!(s.last_success_ms, Some(T0 + H));
    assert!(
        s.last_error
            .as_deref()
            .is_some_and(|e| e.contains("different contents")),
        "{s:?}"
    );
}

#[test]
fn destination_that_fails_every_copy_does_not_grow_the_outbox() {
    let fx = fixture();
    fs::set_permissions(&fx.dest, fs::Permissions::from_mode(0o500)).unwrap();
    let first = run_backup(&fx.config, RunMode::Scheduled, T0);
    let second = run_backup(&fx.config, RunMode::Scheduled, T0 + H);
    fs::set_permissions(&fx.dest, fs::Permissions::from_mode(0o700)).unwrap();
    assert!(first.is_err() && second.is_err());
    assert_eq!(outbox_artifacts(&fx), 0);
    assert!(names(&fx.work.join("outbox")).is_empty());
    assert!(status::load(&fx.work).last_error.is_some());
}

#[test]
fn shipped_checkpoint_does_not_delay_the_scheduled_run() {
    let fx = fixture();
    let (_, kind) = done(checkpoint_at(&fx, T0));
    assert_eq!(kind, ArtifactKind::Checkpoint);
    let (_, kind) = done(run_backup(&fx.config, RunMode::Scheduled, T0 + H).unwrap());
    assert_eq!(kind, ArtifactKind::Full);
    let s = status::load(&fx.work);
    assert_eq!(s.last_scheduled_ms, Some(T0 + H));
    assert_eq!(s.last_success_ms, Some(T0 + H));

    // A later checkpoint updates freshness but not the scheduled clock.
    done(checkpoint_at(&fx, T0 + 2 * H));
    let s = status::load(&fx.work);
    assert_eq!(s.last_success_ms, Some(T0 + 2 * H));
    assert_eq!(s.last_kind, Some(ArtifactKind::Checkpoint));
    assert_eq!(s.last_scheduled_ms, Some(T0 + H));
    let r = run_backup(&fx.config, RunMode::Scheduled, T0 + 3 * H).unwrap();
    assert!(matches!(r, RunReport::Skipped { .. }), "{r:?}");
}

#[test]
fn status_without_last_scheduled_does_not_skip() {
    let fx = fixture();
    fs::create_dir_all(&fx.work).unwrap();
    // status.json written before last_scheduled_ms existed.
    fs::write(
        fx.work.join("status.json"),
        format!(
            r#"{{"last_success_ms": {}, "last_kind": "checkpoint"}}"#,
            T0
        ),
    )
    .unwrap();
    assert_eq!(status::load(&fx.work).last_success_ms, Some(T0));
    assert_eq!(status::load(&fx.work).last_scheduled_ms, None);
    done(run_backup(&fx.config, RunMode::Scheduled, T0 + H).unwrap());
}

fn checkpoint_at(fx: &Fx, now_ms: i64) -> RunReport {
    run_backup(
        &fx.config,
        RunMode::Checkpoint {
            label: "pre-gc".into(),
        },
        now_ms,
    )
    .unwrap()
}

/// A pid that belonged to a process that has exited.
fn dead_pid() -> u32 {
    let mut c = std::process::Command::new("true").spawn().unwrap();
    let pid = c.id();
    c.wait().unwrap();
    pid
}

#[test]
fn run_removes_scratch_dirs_of_dead_processes() {
    let fx = fixture();
    let dead = dead_pid();
    let me = std::process::id();
    for n in [
        format!("verify-tmp-{dead}-1"),
        format!("restore-tmp-{dead}-2"),
        format!("restore-tmp-{me}-3"),
        "verify-tmp-notapid".to_owned(),
    ] {
        fs::create_dir_all(fx.work.join(n).join("x")).unwrap();
    }
    done(run_backup(&fx.config, RunMode::Scheduled, T0).unwrap());
    let left: Vec<String> = names(&fx.work)
        .into_iter()
        .filter(|n| n.contains("-tmp-"))
        .collect();
    assert_eq!(
        left,
        [
            format!("restore-tmp-{me}-3"),
            "verify-tmp-notapid".to_owned()
        ]
    );
}

fn cp(label: &str) -> RunMode {
    RunMode::Checkpoint {
        label: label.into(),
    }
}

#[test]
fn same_label_same_second_checkpoints_keep_both_in_outbox() {
    let fx = fixture();
    let away = fx.t.path().join("dest.away");
    fs::rename(&fx.dest, &away).unwrap();
    let ids: Vec<String> = (0..2)
        .map(|_| match run_backup(&fx.config, cp("same"), T0).unwrap() {
            RunReport::Done { id, shipped, .. } => {
                assert!(!shipped);
                id
            }
            other => panic!("expected Done, got {other:?}"),
        })
        .collect();
    assert_ne!(ids[0], ids[1]);
    for id in &ids {
        let ob = fx.work.join("outbox");
        assert!(ob.join(format!("{id}.tar.zst.age")).is_file(), "{id}");
        assert!(ob.join(format!("{id}.entry.json")).is_file(), "{id}");
    }
    assert_eq!(outbox_artifacts(&fx), 2);
}

#[test]
fn same_label_same_second_checkpoints_both_reach_manifest() {
    let fx = fixture();
    let ids: Vec<String> = (0..2)
        .map(|_| done(run_backup(&fx.config, cp("same"), T0).unwrap()).0)
        .collect();
    assert_ne!(ids[0], ids[1]);
    let m = Manifest::load(&fx.dest).unwrap();
    for id in &ids {
        let e = m.entries.iter().find(|e| &e.meta.id == id).unwrap();
        assert!(fx.dest.join(&e.file).is_file());
    }
    assert_eq!(m.entries.len(), 2);
}

fn fallback_with(fx: &Fx, name: &str, body: &str) -> PathBuf {
    let dir = fx.config.storage.data_dir.join("fallback");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join(name), body).unwrap();
    dir
}

#[test]
fn fallback_file_present_at_snapshot_is_in_the_artifact() {
    let fx = fixture();
    fallback_with(&fx, "2026-11-02.jsonl", "{\"a\":1}\n");
    let (id, _) = done(checkpoint(&fx.config, "pre-gc").unwrap());
    let out = fx.t.path().join("x");
    let e = &Manifest::load(&fx.dest).unwrap().entries[0];
    assert_eq!(e.meta.id, id);
    extract_artifact(&fx.dest.join(&e.file), &fx.identity, &out).unwrap();
    assert_eq!(
        fs::read(out.join("fallback/2026-11-02.jsonl")).unwrap(),
        b"{\"a\":1}\n"
    );
}

#[test]
fn backup_waits_for_fallback_recovery_lock() {
    let fx = fixture();
    let dir = fallback_with(&fx, "2026-11-02.jsonl", "{}\n");
    let lock = fs::File::create(dir.join(".recovery.lock")).unwrap();
    lock.lock().unwrap();
    let hold = Duration::from_millis(1500);
    let holder = std::thread::spawn(move || {
        std::thread::sleep(hold);
        drop(lock);
    });
    let start = Instant::now();
    done(checkpoint(&fx.config, "pre-gc").unwrap());
    assert!(start.elapsed() >= hold);
    holder.join().unwrap();
}

#[test]
fn stuck_recovery_lock_times_out_checkpoint_and_skips_scheduled() {
    let fx = fixture();
    let dir = fallback_with(&fx, "2026-11-02.jsonl", "{}\n");
    let lock = fs::File::create(dir.join(".recovery.lock")).unwrap();
    lock.lock().unwrap();
    let err = checkpoint_waiting(&fx.config, "pre-gc", Duration::from_millis(200))
        .expect_err("a recovery lock timeout must not count as success");
    assert!(format!("{err:#}").contains("timed out"), "{err:#}");
    let r = run_backup(&fx.config, RunMode::Scheduled, T0).unwrap();
    assert!(matches!(r, RunReport::Skipped { .. }), "{r:?}");
    assert!(Manifest::load(&fx.dest).unwrap().entries.is_empty());
    assert_eq!(outbox_artifacts(&fx), 0);
    drop(lock);
}
