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
const CHECKPOINT_LOCK_WAIT: Duration = Duration::from_secs(600);
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
    /// `shipped` is always true: an artifact left in the outbox makes the run an error.
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

/// On error the temp directory is removed, `pagehashes.bin` and the manifest keep their
/// last good contents, and `status.json` records `last_error`.
pub fn run_backup(config: &HippoConfig, mode: RunMode, now_ms: i64) -> Result<RunReport> {
    if !config.backup.is_configured() {
        return Ok(RunReport::NotConfigured);
    }
    let work = config.backup_work_dir();
    let outbox = work.join("outbox");
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&outbox)
        .with_context(|| format!("create {}", outbox.display()))?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .mode(0o600)
        .open(work.join("lock"))
        .context("open backup lock")?;
    let wait = match mode {
        RunMode::Scheduled => None,
        RunMode::Checkpoint { .. } => Some(CHECKPOINT_LOCK_WAIT),
    };
    if !acquire(&lock, wait)? {
        return Ok(RunReport::Skipped {
            reason: "another backup run holds the lock".into(),
        });
    }

    let mut st = status::load(&work);
    let result = run_locked(config, &mode, now_ms, &work, &mut st);
    st.outbox_count = pending_entries(&outbox).map_or(st.outbox_count, |v| {
        u32::try_from(v.len()).unwrap_or(u32::MAX)
    });
    match &result {
        Ok(RunReport::Done { kind, .. }) => {
            st.last_success_ms = Some(now_ms);
            st.last_kind = Some(*kind);
            st.last_error = None;
        }
        Ok(_) => {}
        Err(e) => st.last_error = Some(format!("{e:#}")),
    }
    let saved = status::save(&work, &st);
    let report = result?;
    saved?;
    Ok(report)
}

/// Takes the lock; `None` tries once, `Some(d)` polls with backoff for up to `d`.
/// Returns `false` only when a single try finds the lock held.
fn acquire(lock: &File, wait: Option<Duration>) -> Result<bool> {
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

/// Removed on drop, so every exit path cleans up partial snapshot and diff files.
struct RunTmp(PathBuf);

impl RunTmp {
    fn create(path: PathBuf) -> Result<Self> {
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
    work: &Path,
    st: &mut BackupStatus,
) -> Result<RunReport> {
    let dest = Path::new(&config.backup.destination);
    let outbox = work.join("outbox");
    let mut manifest = Manifest::load(dest)?;

    let waiting = ship_outbox(&outbox, dest, &mut manifest, now_ms)?;
    refresh_uploaded(dest, &mut manifest)?;
    st.pending_upload = pending_upload(&manifest);

    let label = match mode {
        RunMode::Scheduled => {
            // Without this, every hourly run during an outage would add an artifact.
            ensure!(
                waiting == 0,
                "destination {} unavailable; {waiting} artifact(s) wait in the outbox",
                dest.display()
            );
            if st
                .last_success_ms
                .is_some_and(|t| now_ms.saturating_sub(t) < SKIP_WITHIN_MS)
            {
                return Ok(RunReport::Skipped {
                    reason: "a backup succeeded within the last 20 hours".into(),
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
    let base = match mode {
        RunMode::Scheduled => delta_base(&hashes_path, &manifest, info.page_size, now_ms),
        RunMode::Checkpoint { .. } => None,
    };
    let kind = match (mode, &base) {
        (RunMode::Checkpoint { .. }, _) => ArtifactKind::Checkpoint,
        (_, Some(_)) => ArtifactKind::Delta,
        (_, None) => ArtifactKind::Full,
    };
    let id = artifact_id(kind, now_ms, label);
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
        created_ms: now_ms,
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

    ensure!(
        ship_pending(&sidecar, &outbox, dest, &mut manifest, now_ms)?,
        "destination {} unavailable; {id} waits in the outbox",
        dest.display()
    );
    if let Some(h) = new_hashes.filter(|_| kind == ArtifactKind::Full) {
        h.write(&hashes_path).context("write pagehashes.bin")?;
    }

    let doomed = plan_prune(&manifest.entries, now_ms);
    if !doomed.is_empty() {
        for e in manifest
            .entries
            .iter()
            .filter(|e| doomed.contains(&e.meta.id))
        {
            match fs::remove_file(dest.join(&e.file)) {
                Ok(()) => {}
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
                Err(err) => return Err(err).with_context(|| format!("prune {}", e.file)),
            }
        }
        manifest.entries.retain(|e| !doomed.contains(&e.meta.id));
        manifest.save(dest)?;
    }
    st.pending_upload = pending_upload(&manifest);
    Ok(RunReport::Done {
        id,
        kind,
        shipped: true,
    })
}

/// Page hashes to diff against, or `None` when this run must be a Full.
fn delta_base(path: &Path, manifest: &Manifest, page_size: u32, now_ms: i64) -> Option<PageHashes> {
    // Missing or unreadable hashes: start a new Full rather than trust them.
    let base = PageHashes::read(path).ok()?;
    let entry = manifest
        .entries
        .iter()
        .find(|e| e.verified && e.meta.kind == ArtifactKind::Full && e.meta.id == base.base_id)?;
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
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.to_string_lossy().ends_with(ENTRY_EXT))
        .collect();
    v.sort();
    Ok(v)
}

/// Ships every pending outbox artifact and returns how many still wait for the destination.
/// Files with no sidecar (temp files and artifacts from a crashed run) are removed.
fn ship_outbox(outbox: &Path, dest: &Path, manifest: &mut Manifest, now_ms: i64) -> Result<usize> {
    for e in fs::read_dir(outbox).with_context(|| format!("read {}", outbox.display()))? {
        let name = e?.file_name().to_string_lossy().into_owned();
        let owned = name
            .strip_suffix(ARTIFACT_EXT)
            .is_some_and(|id| outbox.join(format!("{id}{ENTRY_EXT}")).exists());
        if !name.ends_with(ENTRY_EXT) && !owned {
            fs::remove_file(outbox.join(&name))
                .with_context(|| format!("remove stale outbox file {name}"))?;
        }
    }
    let mut waiting = 0;
    for sidecar in pending_entries(outbox)? {
        if !ship_pending(&sidecar, outbox, dest, manifest, now_ms)? {
            waiting += 1;
        }
    }
    Ok(waiting)
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
        fs::rename(&tmp, path).with_context(|| format!("rename to {}", path.display()))
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
    for e in manifest.entries.iter_mut().filter(|e| !e.uploaded) {
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

fn hex(bytes: &[u8]) -> String {
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
