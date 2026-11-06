//! One backup run: ship pending outbox artifacts, snapshot, choose the kind, build,
//! verify, ship, record in the manifest, prune, and update `status.json`.
//!
//! Local state in `<work_dir>` (mode 0700): `lock`, `status.json`, `pagehashes.bin`
//! (page hashes of the current week's verified Full), `outbox/` (artifacts not yet in
//! the destination, each with an `<id>.entry.json` holding its pending manifest entry).

use super::artifact::{
    ArtifactInputs, ArtifactKind, ArtifactMeta, DbEntry, artifact_id, write_artifact,
};
use super::manifest::{Manifest, ManifestEntry};
use super::pagediff::{PageHashes, hashes_after_diff, write_diff};
use super::retention::plan_prune;
use super::ship::{ShipOutcome, is_drive_fs, is_uploaded, sha256_of, ship};
use super::snapshot::take_snapshot;
use super::status::{self, BackupStatus, PendingUpload};
use anyhow::{Context, Result, anyhow, bail, ensure};
use chrono::Datelike;
use hippo_core::config::HippoConfig;
use std::fs::{self, DirBuilder, File, OpenOptions, TryLockError};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::time::{Duration, Instant};

const SKIP_WITHIN_MS: i64 = 20 * 3_600_000;
pub(crate) const CHECKPOINT_LOCK_WAIT: Duration = Duration::from_secs(600);
const ARTIFACT_EXT: &str = ".tar.zst.age";
const ENTRY_EXT: &str = ".entry.json";
const PAGEHASHES_FILE: &str = "pagehashes.bin";
const RUN_TMP_DIR: &str = "run.tmp";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunMode {
    /// Hourly launchd run: skips when another run holds the lock or a backup succeeded
    /// within 20 hours.
    Scheduled,
    /// Labeled checkpoint: waits for the lock, never becomes a delta base.
    Checkpoint { label: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunReport {
    NotConfigured,
    Skipped {
        reason: String,
    },
    /// `shipped: false` only for a checkpoint left in the outbox; a scheduled run that
    /// cannot ship returns an error instead.
    Done {
        id: String,
        kind: ArtifactKind,
        shipped: bool,
    },
}

/// Runs a checkpoint labeled `label` at the current time.
pub fn checkpoint(config: &HippoConfig, label: &str) -> Result<RunReport> {
    run_backup(
        config,
        RunMode::Checkpoint {
            label: label.to_owned(),
        },
        chrono::Utc::now().timestamp_millis(),
    )
}

/// Like `checkpoint`, but skips when a verified artifact with `label` already exists.
/// The check runs under the run lock: several processes that start together after an
/// upgrade all request the same label, and only the first should write one.
pub fn checkpoint_once(config: &HippoConfig, label: &str) -> Result<RunReport> {
    run_backup_impl(
        config,
        RunMode::Checkpoint {
            label: label.to_owned(),
        },
        chrono::Utc::now().timestamp_millis(),
        true,
        CHECKPOINT_LOCK_WAIT,
    )
}

/// `checkpoint` with an explicit lock wait. A timeout is an error, never a skip.
pub fn checkpoint_waiting(config: &HippoConfig, label: &str, wait: Duration) -> Result<RunReport> {
    run_backup_impl(
        config,
        RunMode::Checkpoint {
            label: label.to_owned(),
        },
        chrono::Utc::now().timestamp_millis(),
        false,
        wait,
    )
}

/// On error the temp directory is removed, `pagehashes.bin` and the manifest keep their
/// last good contents, and `status.json` records `last_error`.
pub fn run_backup(config: &HippoConfig, mode: RunMode, now_ms: i64) -> Result<RunReport> {
    run_backup_impl(config, mode, now_ms, false, CHECKPOINT_LOCK_WAIT)
}

fn run_backup_impl(
    config: &HippoConfig,
    mode: RunMode,
    now_ms: i64,
    once: bool,
    lock_wait: Duration,
) -> Result<RunReport> {
    if !config.backup.is_configured() {
        return Ok(RunReport::NotConfigured);
    }
    let work = create_work_dir(config)?;
    let lock = open_lock(&work)?;
    let wait = match mode {
        RunMode::Scheduled => None,
        RunMode::Checkpoint { .. } => Some(lock_wait),
    };
    if !acquire(&lock, wait)? {
        return Ok(RunReport::Skipped {
            reason: "another backup run holds the lock".into(),
        });
    }
    run_holding_lock(config, &mode, now_ms, once, &work)
}

/// `checkpoint` for a caller that already holds the run lock (`_lock`, from
/// `open_lock` and `acquire`). Taking the lock again would wait on itself.
pub(crate) fn checkpoint_holding_lock(
    config: &HippoConfig,
    label: &str,
    _lock: &File,
) -> Result<RunReport> {
    if !config.backup.is_configured() {
        return Ok(RunReport::NotConfigured);
    }
    let work = create_work_dir(config)?;
    let mode = RunMode::Checkpoint {
        label: label.to_owned(),
    };
    run_holding_lock(
        config,
        &mode,
        chrono::Utc::now().timestamp_millis(),
        false,
        &work,
    )
}

/// `<work_dir>` and its `outbox/`, mode 0700.
fn create_work_dir(config: &HippoConfig) -> Result<PathBuf> {
    let work = config.backup_work_dir();
    let outbox = work.join("outbox");
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&outbox)
        .with_context(|| format!("create {}", outbox.display()))?;
    Ok(work)
}

fn run_holding_lock(
    config: &HippoConfig,
    mode: &RunMode,
    now_ms: i64,
    once: bool,
    work: &Path,
) -> Result<RunReport> {
    let outbox = work.join("outbox");
    sweep_stale_tmp(work);
    let mut st = status::load(work);
    let mut notes = Vec::new();
    let result = run_locked(config, mode, now_ms, once, work, &mut st, &mut notes);
    st.outbox_count = pending_entries(&outbox).map_or(st.outbox_count, |v| {
        u32::try_from(v.len()).unwrap_or(u32::MAX)
    });
    match &result {
        Ok(RunReport::Done { kind, shipped, .. }) => {
            if *shipped {
                st.last_success_ms = Some(now_ms);
                st.last_kind = Some(*kind);
                if *kind != ArtifactKind::Checkpoint {
                    st.last_scheduled_ms = Some(now_ms);
                }
            }
            st.last_error = (!notes.is_empty()).then(|| notes.join("; "));
        }
        Ok(_) => {}
        Err(e) => st.last_error = Some(format!("{e:#}")),
    }
    let saved = status::save(work, &st);
    let report = result?;
    saved?;
    Ok(report)
}

/// `<work_dir>/lock`, which serializes runs and every other `status.json` writer.
pub(crate) fn open_lock(work: &Path) -> Result<File> {
    OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .mode(0o600)
        .open(work.join("lock"))
        .context("open backup lock")
}

/// Takes the lock; `None` tries once, `Some(d)` polls with backoff for up to `d`.
/// Returns `false` only when a single try finds the lock held.
pub(crate) fn acquire(lock: &File, wait: Option<Duration>) -> Result<bool> {
    let deadline = wait.map(|d| Instant::now() + d);
    let mut delay = Duration::from_millis(50);
    loop {
        match lock.try_lock() {
            Ok(()) => return Ok(true),
            Err(TryLockError::WouldBlock) => {}
            Err(TryLockError::Error(e)) => return Err(e).context("lock backup"),
        }
        let Some(deadline) = deadline else {
            return Ok(false);
        };
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            bail!(
                "timed out after {} s waiting for the backup lock",
                wait.unwrap_or_default().as_secs()
            );
        }
        std::thread::sleep(delay.min(left));
        delay = (delay * 2).min(Duration::from_secs(5));
    }
}

/// Removes `verify-tmp-<pid>-<ms>` and `restore-tmp-<pid>-<ms>` directories (plaintext
/// database copies) whose process is gone. Call it under the run lock. Best effort.
pub(crate) fn sweep_stale_tmp(work: &Path) {
    let Ok(rd) = fs::read_dir(work) else {
        return;
    };
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        let pid = ["verify-tmp-", "restore-tmp-"]
            .iter()
            .find_map(|p| name.strip_prefix(p))
            .and_then(|rest| rest.split_once('-'))
            .and_then(|(pid, _)| pid.parse::<libc::pid_t>().ok())
            .filter(|&pid| pid > 0);
        let dead = pid.is_some_and(|pid| {
            // SAFETY: signal 0 only checks that the process exists.
            let rc = unsafe { libc::kill(pid, 0) };
            rc != 0 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
        });
        if dead {
            match fs::remove_dir_all(e.path()) {
                Ok(()) => tracing::info!("backup: removed stale {name}"),
                Err(err) => tracing::warn!("backup: remove stale {name}: {err}"),
            }
        }
    }
}

/// Removed on drop, so every exit path cleans up partial snapshot and diff files.
pub(crate) struct RunTmp(pub(crate) PathBuf);

impl RunTmp {
    pub(crate) fn create(path: PathBuf) -> Result<Self> {
        // Left over from a crashed run; the lock guarantees no run uses it now.
        let _ = fs::remove_dir_all(&path);
        DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .with_context(|| format!("create {}", path.display()))?;
        Ok(Self(path))
    }
}

impl Drop for RunTmp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn run_locked(
    config: &HippoConfig,
    mode: &RunMode,
    now_ms: i64,
    once: bool,
    work: &Path,
    st: &mut BackupStatus,
    notes: &mut Vec<String>,
) -> Result<RunReport> {
    let dest = Path::new(&config.backup.destination);
    let outbox = work.join("outbox");
    // A checkpoint is written to the local outbox, so destination problems do not fail it.
    let cp = matches!(mode, RunMode::Checkpoint { .. });
    let mut manifest = tolerate(cp, notes, load_manifest(dest, &outbox))?.flatten();

    let waiting = match manifest.as_mut() {
        Some(m) => {
            // Only an unavailable destination holds back a new artifact: an item that
            // fails for another reason stays in the outbox and is reported.
            let (waiting, errors) = ship_outbox(&outbox, dest, m, now_ms)?;
            for e in errors {
                tracing::warn!("backup outbox: {e:#}");
                notes.push(format!("{e:#}"));
            }
            tolerate(cp, notes, refresh_uploaded(dest, m))?;
            st.pending_upload = pending_upload(m);
            waiting
        }
        None => pending_entries(&outbox)?.len(),
    };

    if let (true, RunMode::Checkpoint { label }) = (once, mode) {
        let has = |e: &ManifestEntry| e.verified && e.meta.label.as_deref() == Some(label);
        let in_manifest = manifest.as_ref().is_some_and(|m| m.entries.iter().any(has));
        // A sidecar that cannot be read counts as absent: a duplicate is cheaper than none.
        let in_outbox = pending_entries(&outbox)?.iter().any(|p| {
            fs::read(p)
                .ok()
                .and_then(|b| serde_json::from_slice::<ManifestEntry>(&b).ok())
                .is_some_and(|e| has(&e))
        });
        if in_manifest || in_outbox {
            return Ok(RunReport::Skipped {
                reason: format!("a verified checkpoint labeled {label} already exists"),
            });
        }
    }

    let label = match mode {
        RunMode::Scheduled => {
            // Without this, every hourly run during an outage would add an artifact.
            ensure!(
                waiting == 0,
                "destination {} unavailable; {waiting} artifact(s) wait in the outbox",
                dest.display()
            );
            if st
                .last_scheduled_ms
                .is_some_and(|t| now_ms.saturating_sub(t) < SKIP_WITHIN_MS)
            {
                return Ok(RunReport::Skipped {
                    reason: "a scheduled backup succeeded within the last 20 hours".into(),
                });
            }
            None
        }
        RunMode::Checkpoint { label } => Some(label.as_str()),
    };
    let recipient = age::x25519::Recipient::from_str(&config.backup.recipient)
        .map_err(|e| anyhow!("invalid [backup] recipient: {e}"))?;

    let tmp = RunTmp::create(work.join(RUN_TMP_DIR))?;
    let snap = tmp.0.join("hippo.db");
    let info = take_snapshot(&config.db_path(), &snap)?;
    let hashes_path = work.join(PAGEHASHES_FILE);
    let base = match (mode, &manifest) {
        (RunMode::Scheduled, Some(m)) => delta_base(&hashes_path, m, info.page_size, now_ms),
        _ => None,
    };
    let kind = match (mode, &base) {
        (RunMode::Checkpoint { .. }, _) => ArtifactKind::Checkpoint,
        (_, Some(_)) => ArtifactKind::Delta,
        (_, None) => ArtifactKind::Full,
    };
    // Same label in the same second would collide: step the id's timestamp until unused.
    let taken = |id: &str| {
        let file = format!("{id}{ARTIFACT_EXT}");
        manifest
            .as_ref()
            .is_some_and(|m| m.entries.iter().any(|e| e.meta.id == id))
            || outbox.join(&file).exists()
            || outbox.join(format!("{id}{ENTRY_EXT}")).exists()
            || dest.join(&file).exists()
    };
    let (id_ms, id) = (0..10)
        .map(|i| now_ms + i * 1000)
        .map(|ms| (ms, artifact_id(kind, ms, label)))
        .find(|(_, id)| !taken(id))
        .ok_or_else(|| anyhow!("no unused artifact id after 10 tries"))?;
    let new_hashes = match kind {
        ArtifactKind::Checkpoint => None,
        _ => Some(PageHashes::compute(&snap, &id)?),
    };
    let diff = tmp.0.join("hippo.db.pagediff");
    let db_entry = match (&base, &new_hashes) {
        (Some(base), Some(new)) => {
            write_diff(base, &snap, &diff)?;
            ensure!(
                hashes_after_diff(base, &diff)? == new.hashes,
                "delta verification failed: base plus diff does not reproduce the snapshot"
            );
            DbEntry::Delta(&diff)
        }
        _ => DbEntry::Full(&snap),
    };

    let meta = ArtifactMeta {
        id: id.clone(),
        kind,
        label: label.map(str::to_owned),
        created_ms: id_ms,
        base_id: base.as_ref().map(|b| b.base_id.clone()),
        hippo_version: env!("HIPPO_VERSION_FULL").to_owned(),
        schema_version: info.schema_version,
        db_sha256: hex(&info.sha256),
        row_counts: info.row_counts,
    };
    let inputs = ArtifactInputs {
        db_entry,
        config_dir: &config.storage.config_dir,
        data_dir: &config.storage.data_dir,
    };
    let file = format!("{id}{ARTIFACT_EXT}");
    let out = outbox.join(&file);
    let sum = write_artifact(&meta, &inputs, &recipient, &out)?;
    let entry = ManifestEntry {
        meta,
        file,
        ciphertext_sha256: hex(&sum),
        verified: true,
        shipped_ms: None,
        uploaded: false,
    };
    let sidecar = outbox.join(format!("{id}{ENTRY_EXT}"));
    if let Err(e) = write_sidecar(&sidecar, &entry) {
        let _ = fs::remove_file(&out);
        return Err(e);
    }
    drop(tmp);

    let shipped = match manifest
        .as_mut()
        .map(|m| ship_pending(&sidecar, &outbox, dest, m, now_ms))
    {
        // Outbox errors no longer hold back a scheduled run, so a destination that fails
        // every copy would otherwise gain one artifact per hourly run.
        Some(Err(e)) if !cp && out.exists() => {
            let _ = fs::remove_file(&sidecar);
            let _ = fs::remove_file(&out);
            return Err(e);
        }
        Some(r) => tolerate(cp, notes, r)?.unwrap_or(false),
        None => false,
    };
    let shipped_into = manifest.as_mut().filter(|_| shipped);
    let Some(manifest) = shipped_into else {
        let msg = format!(
            "{id} was not shipped to {} and waits in the outbox",
            dest.display()
        );
        ensure!(cp, "{msg}");
        notes.push(msg);
        return Ok(RunReport::Done {
            id,
            kind,
            shipped: false,
        });
    };
    if let Some(h) = new_hashes.filter(|_| kind == ArtifactKind::Full) {
        h.write(&hashes_path).context("write pagehashes.bin")?;
    }
    prune(dest, manifest, now_ms, notes);
    st.pending_upload = pending_upload(manifest);
    Ok(RunReport::Done {
        id,
        kind,
        shipped: true,
    })
}

/// Ok for a checkpoint: the error goes to `notes` (and `last_error`). Fatal for a scheduled run.
fn tolerate<T>(checkpoint: bool, notes: &mut Vec<String>, r: Result<T>) -> Result<Option<T>> {
    match r {
        Ok(v) => Ok(Some(v)),
        Err(e) if checkpoint => {
            tracing::warn!("backup checkpoint: {e:#}");
            notes.push(format!("{e:#}"));
            Ok(None)
        }
        Err(e) => Err(e),
    }
}

/// `file` is the only manifest value joined to a path, so it must be the plain
/// `<id>.tar.zst.age` name built by `artifact_id` (no `/` or `.` in the id).
pub(crate) fn valid_entry(e: &ManifestEntry) -> bool {
    let id = &e.meta.id;
    id.starts_with("hippo-")
        && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        && e.file == format!("{id}{ARTIFACT_EXT}")
}

/// The manifest, or `None` when the destination folder is unavailable. Refuses an
/// empty manifest over existing artifacts: saving it would erase their record (for
/// example, Drive lists the folder before `manifest.json` has synced). An artifact whose
/// sidecar is still in the outbox is ours, shipped just before a crash.
fn load_manifest(dest: &Path, outbox: &Path) -> Result<Option<Manifest>> {
    if !dest.is_dir() {
        return Ok(None);
    }
    let m = Manifest::load(dest)?;
    if m.entries.is_empty() && fs::symlink_metadata(dest.join("manifest.json")).is_err() {
        for e in fs::read_dir(dest).with_context(|| format!("read {}", dest.display()))? {
            let name = e?.file_name().to_string_lossy().into_owned();
            let ours = |id: &str| outbox.join(format!("{id}{ENTRY_EXT}")).is_file();
            ensure!(
                name.strip_suffix(ARTIFACT_EXT).is_none_or(ours),
                "destination {} holds artifacts (for example {name}) but no manifest.json; \
                 refusing to write a new manifest",
                dest.display()
            );
        }
    }
    for e in m.entries.iter().filter(|e| !valid_entry(e)) {
        tracing::warn!(id = %e.meta.id, file = %e.file, "backup: ignoring invalid manifest entry");
    }
    Ok(Some(m))
}

/// Removes expired entries from the manifest and saves it before deleting their files,
/// so the manifest never lists a deleted artifact. Deletion is best effort. Invalid
/// entries are never pruned.
fn prune(dest: &Path, manifest: &mut Manifest, now_ms: i64, notes: &mut Vec<String>) {
    let valid: Vec<ManifestEntry> = manifest
        .entries
        .iter()
        .filter(|e| valid_entry(e))
        .cloned()
        .collect();
    let doomed = plan_prune(&valid, now_ms);
    if doomed.is_empty() {
        return;
    }
    let (gone, kept): (Vec<_>, Vec<_>) = std::mem::take(&mut manifest.entries)
        .into_iter()
        .partition(|e| valid_entry(e) && doomed.contains(&e.meta.id));
    manifest.entries = kept;
    if let Err(e) = manifest.save(dest) {
        tracing::warn!("backup: prune manifest save: {e:#}");
        notes.push(format!("prune skipped: {e:#}"));
        return;
    }
    for e in gone {
        match fs::remove_file(dest.join(&e.file)) {
            Ok(()) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => tracing::warn!("backup: prune {}: {err}", e.file),
        }
    }
}

/// Page hashes to diff against, or `None` when this run must be a Full.
fn delta_base(path: &Path, manifest: &Manifest, page_size: u32, now_ms: i64) -> Option<PageHashes> {
    // Missing or unreadable hashes: start a new Full rather than trust them.
    let base = PageHashes::read(path).ok()?;
    let entry = manifest.entries.iter().find(|e| {
        e.verified
            && e.meta.kind == ArtifactKind::Full
            && e.meta.id == base.base_id
            && valid_entry(e)
    })?;
    let week = |ms| chrono::DateTime::from_timestamp_millis(ms).map(|t| t.iso_week());
    let same_week = week(entry.meta.created_ms)
        .zip(week(now_ms))
        .is_some_and(|(a, b)| a == b);
    (base.page_size == page_size && same_week).then_some(base)
}

/// Sidecar paths in the outbox, sorted. The outbox holds only sidecars and their artifacts.
fn pending_entries(outbox: &Path) -> Result<Vec<PathBuf>> {
    let mut v: Vec<PathBuf> = fs::read_dir(outbox)
        .with_context(|| format!("read {}", outbox.display()))?
        .map(|e| e.map(|e| e.path()))
        .filter(|p| {
            p.as_ref()
                .map_or(true, |p| p.to_string_lossy().ends_with(ENTRY_EXT))
        })
        .collect::<std::io::Result<_>>()
        .with_context(|| format!("read {}", outbox.display()))?;
    v.sort();
    Ok(v)
}

/// Ships every pending outbox artifact. Returns how many wait for an unavailable
/// destination and the per-file errors; a failed file stays in the outbox but is not
/// counted as waiting. Files with no sidecar
/// (temp files and artifacts from a crashed run) are removed.
fn ship_outbox(
    outbox: &Path,
    dest: &Path,
    manifest: &mut Manifest,
    now_ms: i64,
) -> Result<(usize, Vec<anyhow::Error>)> {
    let mut errors = Vec::new();
    for e in fs::read_dir(outbox).with_context(|| format!("read {}", outbox.display()))? {
        let name = e?.file_name().to_string_lossy().into_owned();
        let owned = name
            .strip_suffix(ARTIFACT_EXT)
            .is_some_and(|id| outbox.join(format!("{id}{ENTRY_EXT}")).exists());
        if !name.ends_with(ENTRY_EXT)
            && !owned
            && let Err(e) = fs::remove_file(outbox.join(&name))
        {
            errors.push(anyhow!(e).context(format!("remove stale outbox file {name}")));
        }
    }
    let mut waiting = 0;
    for sidecar in pending_entries(outbox)? {
        match ship_pending(&sidecar, outbox, dest, manifest, now_ms) {
            Ok(true) => {}
            Ok(false) => waiting += 1,
            Err(e) => errors.push(e),
        }
    }
    Ok((waiting, errors))
}

/// Ships one outbox artifact and records its manifest entry. Returns `false` when the
/// destination is unavailable and the artifact stays in the outbox.
fn ship_pending(
    sidecar: &Path,
    outbox: &Path,
    dest: &Path,
    manifest: &mut Manifest,
    now_ms: i64,
) -> Result<bool> {
    let mut entry: ManifestEntry = serde_json::from_slice(
        &fs::read(sidecar).with_context(|| format!("read {}", sidecar.display()))?,
    )
    .with_context(|| format!("parse {}", sidecar.display()))?;
    ensure!(
        valid_entry(&entry),
        "invalid outbox entry {}",
        sidecar.display()
    );
    let expected = unhex(&entry.ciphertext_sha256)?;
    let artifact = outbox.join(&entry.file);
    let dest_file = if artifact.exists() {
        match ship(&artifact, dest, &expected)? {
            ShipOutcome::Shipped { dest_file } => dest_file,
            ShipOutcome::DestinationUnavailable => return Ok(false),
        }
    } else if !dest.is_dir() {
        return Ok(false);
    } else {
        // The artifact left the outbox: a crash after `ship` but before the manifest save.
        let f = dest.join(&entry.file);
        if !(f.is_file() && sha256_of(&f)? == expected) {
            tracing::warn!(id = %entry.meta.id, "backup: dropping outbox entry whose artifact is gone");
            fs::remove_file(sidecar)?;
            return Ok(true);
        }
        f
    };
    entry.shipped_ms = Some(now_ms);
    entry.uploaded = uploaded(&dest_file, dest);
    if !manifest.entries.iter().any(|e| e.meta.id == entry.meta.id) {
        manifest.entries.push(entry);
        manifest.save(dest)?;
    }
    fs::remove_file(sidecar).with_context(|| format!("remove {}", sidecar.display()))?;
    Ok(true)
}

fn write_sidecar(path: &Path, entry: &ManifestEntry) -> Result<()> {
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    let res = (|| -> Result<()> {
        let f = File::create(&tmp).with_context(|| format!("create {}", tmp.display()))?;
        serde_json::to_writer(&f, entry)?;
        f.sync_all()?;
        super::ship::rename_noreplace(&tmp, path)
            .with_context(|| format!("rename to {} (never replaces)", path.display()))
    })();
    if res.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    res
}

/// Upload state of a shipped file. An error leaves it not uploaded, so the
/// stuck-upload alarm surfaces it instead of failing every run.
fn uploaded(file: &Path, dest: &Path) -> bool {
    is_uploaded(file, is_drive_fs(dest)).unwrap_or_else(|e| {
        tracing::warn!("backup: upload state of {}: {e:#}", file.display());
        false
    })
}

fn refresh_uploaded(dest: &Path, manifest: &mut Manifest) -> Result<()> {
    let mut changed = false;
    for e in manifest
        .entries
        .iter_mut()
        .filter(|e| !e.uploaded && valid_entry(e))
    {
        if uploaded(&dest.join(&e.file), dest) {
            e.uploaded = true;
            changed = true;
        }
    }
    if changed {
        manifest.save(dest)?;
    }
    Ok(())
}

fn pending_upload(manifest: &Manifest) -> Vec<PendingUpload> {
    manifest
        .entries
        .iter()
        .filter(|e| !e.uploaded)
        .map(|e| PendingUpload {
            id: e.meta.id.clone(),
            shipped_ms: e.shipped_ms.unwrap_or_default(),
        })
        .collect()
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write;
    bytes.iter().fold(String::new(), |mut s, b| {
        let _ = write!(s, "{b:02x}");
        s
    })
}

fn unhex(s: &str) -> Result<[u8; 32]> {
    ensure!(s.len() == 64 && s.is_ascii(), "bad SHA-256 hex: {s}");
    let mut out = [0u8; 32];
    for (i, b) in out.iter_mut().enumerate() {
        *b = u8::from_str_radix(&s[2 * i..2 * i + 2], 16)
            .with_context(|| format!("bad SHA-256 hex: {s}"))?;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_round_trips() {
        let b: [u8; 32] = std::array::from_fn(|i| u8::try_from(i * 7).unwrap());
        assert_eq!(unhex(&hex(&b)).unwrap(), b);
        assert!(unhex("zz").is_err());
        assert!(unhex(&"g".repeat(64)).is_err());
    }
}
