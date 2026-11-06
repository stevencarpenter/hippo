//! `<work_dir>/status.json`: the outcome of recent backup runs, read by status and alarms.

use super::artifact::{ArtifactKind, ArtifactMeta};
use super::manifest::Manifest;
use anyhow::{Context, Result};
use hippo_core::config::HippoConfig;
use serde::{Deserialize, Serialize};
use std::fmt::Write as _;
use std::fs::{self, File};
use std::io::Write;
use std::path::Path;

const STATUS_FILE: &str = "status.json";
const STATUS_TMP: &str = ".status.json.tmp";

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct BackupStatus {
    /// `created_ms` of the last run that shipped its artifact, of any kind. Watchdog
    /// I-21 and doctor use it for freshness.
    pub last_success_ms: Option<i64>,
    /// `created_ms` of the last shipped Full or Delta. The 20-hour skip uses it, so
    /// checkpoints cannot starve the scheduled tier. Absent in older files: `None`.
    pub last_scheduled_ms: Option<i64>,
    pub last_kind: Option<ArtifactKind>,
    /// Error of the last run; cleared by the next successful run.
    pub last_error: Option<String>,
    pub last_verify_ms: Option<i64>,
    /// Shipped manifest entries that Google Drive has not uploaded yet.
    pub pending_upload: Vec<PendingUpload>,
    /// Artifacts waiting in the local outbox for the destination.
    pub outbox_count: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PendingUpload {
    pub id: String,
    pub shipped_ms: i64,
}

/// A missing or unreadable file is the default status.
pub fn load(work_dir: &Path) -> BackupStatus {
    fs::read(work_dir.join(STATUS_FILE))
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default()
}

/// Writes via a temp file, then renames over `status.json`.
pub fn save(work_dir: &Path, s: &BackupStatus) -> Result<()> {
    let tmp = work_dir.join(STATUS_TMP);
    let mut f = File::create(&tmp).with_context(|| format!("create {}", tmp.display()))?;
    f.write_all(&serde_json::to_vec_pretty(s)?)?;
    f.sync_all()?;
    drop(f);
    fs::rename(&tmp, work_dir.join(STATUS_FILE)).context("rename status.json")
}

fn when(ms: Option<i64>) -> String {
    ms.and_then(chrono::DateTime::from_timestamp_millis)
        .map_or_else(|| "never".to_owned(), |t| t.to_rfc3339())
}

/// Text for `hippo backup status`: the status fields and the newest manifest entry per kind.
pub fn report(config: &HippoConfig) -> String {
    let mut out = String::new();
    if !config.backup.is_configured() {
        return "backup: not configured ([backup] destination and recipient are empty)\n".into();
    }
    let s = load(&config.backup_work_dir());
    let dest = Path::new(&config.backup.destination);
    let _ = writeln!(out, "destination:     {}", dest.display());
    let _ = writeln!(out, "last success:    {}", when(s.last_success_ms));
    let _ = writeln!(out, "last kind:       {:?}", s.last_kind);
    let _ = writeln!(out, "last scheduled:  {}", when(s.last_scheduled_ms));
    let _ = writeln!(
        out,
        "last error:      {}",
        s.last_error.as_deref().unwrap_or("none")
    );
    let _ = writeln!(out, "last verify:     {}", when(s.last_verify_ms));
    let _ = writeln!(out, "outbox:          {}", s.outbox_count);
    let _ = writeln!(out, "pending upload:  {}", s.pending_upload.len());
    for p in &s.pending_upload {
        let _ = writeln!(out, "  {} (shipped {})", p.id, when(Some(p.shipped_ms)));
    }
    if !dest.is_dir() {
        let _ = writeln!(out, "manifest:        destination unavailable");
        return out;
    }
    match Manifest::load(dest) {
        Ok(m) => {
            for kind in [
                ArtifactKind::Full,
                ArtifactKind::Delta,
                ArtifactKind::Checkpoint,
            ] {
                let latest: Option<&ArtifactMeta> = m
                    .entries
                    .iter()
                    .map(|e| &e.meta)
                    .filter(|e| e.kind == kind)
                    .max_by_key(|e| e.created_ms);
                let _ = writeln!(
                    out,
                    "latest {:<11}{}",
                    format!("{kind:?}:"),
                    latest.map_or_else(
                        || "none".to_owned(),
                        |e| format!("{} ({})", e.id, when(Some(e.created_ms)))
                    )
                );
            }
        }
        Err(e) => {
            let _ = writeln!(out, "manifest:        {e:#}");
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_or_corrupt_is_default_and_save_round_trips() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(load(dir.path()), BackupStatus::default());
        fs::write(dir.path().join(STATUS_FILE), b"{not json").unwrap();
        assert_eq!(load(dir.path()), BackupStatus::default());
        let s = BackupStatus {
            last_success_ms: Some(5),
            last_kind: Some(ArtifactKind::Delta),
            pending_upload: vec![PendingUpload {
                id: "a".into(),
                shipped_ms: 7,
            }],
            outbox_count: 1,
            ..BackupStatus::default()
        };
        save(dir.path(), &s).unwrap();
        assert_eq!(load(dir.path()), s);
        assert!(!dir.path().join(STATUS_TMP).exists());
    }
}
