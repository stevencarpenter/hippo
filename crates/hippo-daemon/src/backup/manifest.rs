//! `manifest.json`: the record of every artifact in the destination folder.

use super::artifact::ArtifactMeta;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::fs::{self, File};
use std::io::Write;
use std::path::Path;

const MANIFEST_FILE: &str = "manifest.json";
const MANIFEST_TMP: &str = ".manifest.json.tmp";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ManifestEntry {
    pub meta: ArtifactMeta,
    pub file: String,
    pub ciphertext_sha256: String,
    pub verified: bool,
    pub shipped_ms: Option<i64>,
    pub uploaded: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Manifest {
    pub version: u32,
    pub entries: Vec<ManifestEntry>,
}

impl Default for Manifest {
    fn default() -> Self {
        Self {
            version: 1,
            entries: Vec::new(),
        }
    }
}

impl Manifest {
    /// A missing file is an empty manifest. An unreadable or unparseable file is an
    /// error: treating it as empty would make a later run see the destination as empty.
    pub fn load(dest: &Path) -> Result<Manifest> {
        let path = dest.join(MANIFEST_FILE);
        match fs::read(&path) {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .with_context(|| format!("corrupt manifest {}", path.display())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Manifest::default()),
            Err(e) => Err(e).with_context(|| format!("read {}", path.display())),
        }
    }

    pub fn save(&self, dest: &Path) -> Result<()> {
        let tmp = dest.join(MANIFEST_TMP);
        let final_path = dest.join(MANIFEST_FILE);
        let mut f = File::create(&tmp).with_context(|| format!("create {}", tmp.display()))?;
        f.write_all(&serde_json::to_vec_pretty(self)?)?;
        f.sync_all()?;
        drop(f);
        fs::rename(&tmp, &final_path).with_context(|| format!("rename to {}", final_path.display()))
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::backup::artifact::ArtifactKind;
    use std::collections::BTreeMap;

    pub(crate) fn entry(id: &str) -> ManifestEntry {
        ManifestEntry {
            meta: ArtifactMeta {
                id: id.into(),
                kind: ArtifactKind::Full,
                label: None,
                created_ms: 1,
                base_id: None,
                hippo_version: "0".into(),
                schema_version: 29,
                db_sha256: "x".into(),
                row_counts: BTreeMap::new(),
            },
            file: format!("{id}.tar.zst.age"),
            ciphertext_sha256: "y".into(),
            verified: true,
            shipped_ms: Some(2),
            uploaded: false,
        }
    }

    #[test]
    fn manifest_save_is_atomic() {
        let dir = tempfile::tempdir().unwrap();
        let m = Manifest {
            version: 1,
            entries: vec![entry("a"), entry("b")],
        };
        m.save(dir.path()).unwrap();
        assert!(!dir.path().join(".manifest.json.tmp").exists());
        assert_eq!(Manifest::load(dir.path()).unwrap(), m);
    }

    #[test]
    fn load_missing_is_empty() {
        let dir = tempfile::tempdir().unwrap();
        let m = Manifest::load(dir.path()).unwrap();
        assert_eq!(m.version, 1);
        assert!(m.entries.is_empty());
    }

    #[test]
    fn load_corrupt_is_error() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("manifest.json"), b"{not json").unwrap();
        assert!(Manifest::load(dir.path()).is_err());
    }
}
