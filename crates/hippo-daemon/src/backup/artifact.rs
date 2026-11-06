//! Encrypted backup artifact: tar, then zstd (level 9, long-distance), then age X25519.
//!
//! Recoverable with stock tools: `age -d -i key f.tar.zst.age | zstd -d | tar -x`.
//! Entry order: `meta.json`, `hippo.db` or `hippo.db.pagediff`, `config/config.toml`,
//! `config/redact.toml`, `enrichment-state.json`, `fallback/*.jsonl`.

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Component, Path};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ArtifactKind {
    Full,
    Delta,
    Checkpoint,
}

impl ArtifactKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Full => "full",
            Self::Delta => "delta",
            Self::Checkpoint => "checkpoint",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactMeta {
    pub id: String,
    pub kind: ArtifactKind,
    pub label: Option<String>,
    pub created_ms: i64,
    pub base_id: Option<String>,
    pub hippo_version: String,
    pub schema_version: i64,
    /// Hex SHA-256 of the reconstructed database.
    pub db_sha256: String,
    pub row_counts: BTreeMap<String, i64>,
}

pub enum DbEntry<'a> {
    /// Stored as `hippo.db`.
    Full(&'a Path),
    /// Stored as `hippo.db.pagediff`.
    Delta(&'a Path),
}

pub struct ArtifactInputs<'a> {
    pub db_entry: DbEntry<'a>,
    pub config_dir: &'a Path,
    pub data_dir: &'a Path,
}

/// `hippo-{kind}-{YYYYMMDDTHHMMSSZ}` plus `-{slug}` when labeled.
pub fn artifact_id(kind: ArtifactKind, created_ms: i64, label: Option<&str>) -> String {
    let ts = chrono::DateTime::from_timestamp_millis(created_ms)
        .unwrap_or_default()
        .format("%Y%m%dT%H%M%SZ");
    let mut id = format!("hippo-{}-{ts}", kind.as_str());
    if let Some(label) = label {
        let mut slug = String::new();
        for c in label.chars().map(|c| c.to_ascii_lowercase()) {
            let c = if c.is_ascii_alphanumeric() { c } else { '-' };
            if c == '-' && (slug.is_empty() || slug.ends_with('-')) {
                continue;
            }
            slug.push(c);
        }
        slug.truncate(40);
        let slug = slug.trim_end_matches('-');
        if !slug.is_empty() {
            id.push('-');
            id.push_str(slug);
        }
    }
    id
}

/// Writes through to `inner` while hashing the bytes.
struct HashWriter<W> {
    inner: W,
    hasher: Sha256,
}

impl<W: Write> Write for HashWriter<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.hasher.update(&buf[..n]);
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

fn append_bytes<W: Write>(tar: &mut tar::Builder<W>, name: &str, data: &[u8]) -> Result<()> {
    let mut h = tar::Header::new_gnu();
    h.set_size(data.len() as u64);
    h.set_mode(0o600);
    h.set_mtime(chrono::Utc::now().timestamp().max(0) as u64);
    tar.append_data(&mut h, name, data)?;
    Ok(())
}

/// Yields exactly `remaining` bytes from `inner`; EOF before that is an error, bytes past it are ignored.
struct ExactReader<R> {
    inner: R,
    remaining: u64,
}

impl<R: Read> Read for ExactReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.remaining == 0 || buf.is_empty() {
            return Ok(0);
        }
        let max = buf
            .len()
            .min(usize::try_from(self.remaining).unwrap_or(usize::MAX));
        let n = self.inner.read(&mut buf[..max])?;
        if n == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "source shrank below the size recorded in the tar header",
            ));
        }
        self.remaining -= n as u64;
        Ok(n)
    }
}

/// Appends `len` bytes from `reader`; a short source is an error, never a misaligned tar.
fn append_exact<W: Write>(
    tar: &mut tar::Builder<W>,
    name: &str,
    len: u64,
    mtime: u64,
    reader: impl Read,
) -> Result<()> {
    let mut h = tar::Header::new_gnu();
    h.set_size(len);
    h.set_mode(0o600);
    h.set_mtime(mtime);
    let reader = ExactReader {
        inner: reader,
        remaining: len,
    };
    tar.append_data(&mut h, name, reader)
        .with_context(|| format!("archive {name}"))
}

/// Appends the file at `src` (symlinks followed) as `name`, truncated to its length
/// at open time so a concurrently appended file cannot misalign the archive.
fn append_file<W: Write>(tar: &mut tar::Builder<W>, name: &str, src: &Path) -> Result<()> {
    let file = File::open(src).with_context(|| format!("open {}", src.display()))?;
    let md = file.metadata()?;
    let mtime = md
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map_or(0, |d| d.as_secs());
    append_exact(tar, name, md.len(), mtime, file)
}

fn build<W: Write>(
    tar: &mut tar::Builder<W>,
    meta: &ArtifactMeta,
    inputs: &ArtifactInputs<'_>,
) -> Result<()> {
    append_bytes(tar, "meta.json", &serde_json::to_vec_pretty(meta)?)?;
    match inputs.db_entry {
        DbEntry::Full(p) => append_file(tar, "hippo.db", p)?,
        DbEntry::Delta(p) => append_file(tar, "hippo.db.pagediff", p)?,
    }
    append_file(
        tar,
        "config/config.toml",
        &inputs.config_dir.join("config.toml"),
    )?;
    let redact = inputs.config_dir.join("redact.toml");
    if redact.is_file() {
        append_file(tar, "config/redact.toml", &redact)?;
    }
    let state = inputs.data_dir.join("enrichment-state.json");
    if state.is_file() {
        append_file(tar, "enrichment-state.json", &state)?;
    }
    // The selector recovery uses: `.recovering` files hold events not yet in the DB.
    let files = hippo_core::storage::list_fallback_files(&inputs.data_dir.join("fallback"))?;
    for p in files {
        if fs::symlink_metadata(&p).is_ok_and(|m| m.is_file()) {
            let name = format!("fallback/{}", p.file_name().unwrap().to_string_lossy());
            append_file(tar, &name, &p)?;
        }
    }
    Ok(())
}

fn write_to(
    tmp: &Path,
    meta: &ArtifactMeta,
    inputs: &ArtifactInputs<'_>,
    recipient: &age::x25519::Recipient,
) -> Result<[u8; 32]> {
    let file = File::create(tmp).with_context(|| format!("create {}", tmp.display()))?;
    let hw = HashWriter {
        inner: file,
        hasher: Sha256::new(),
    };
    let enc = age::Encryptor::with_recipients(std::iter::once(recipient as &dyn age::Recipient))?;
    let age_w = enc.wrap_output(hw)?;
    let mut zw = zstd::Encoder::new(age_w, 9)?;
    zw.long_distance_matching(true)?;
    let mut tar = tar::Builder::new(zw);
    build(&mut tar, meta, inputs)?;
    let zw = tar.into_inner()?;
    let age_w = zw.finish()?;
    let hw = age_w.finish()?;
    hw.inner.sync_all()?;
    Ok(hw.hasher.finalize().into())
}

/// Writes the artifact to `out` (temp name, then rename that fails if `out` exists) and returns the SHA-256
/// of the ciphertext as written.
pub fn write_artifact(
    meta: &ArtifactMeta,
    inputs: &ArtifactInputs<'_>,
    recipient: &age::x25519::Recipient,
    out: &Path,
) -> Result<[u8; 32]> {
    let mut tmp_name = out.file_name().context("out has no file name")?.to_owned();
    tmp_name.push(".tmp");
    let tmp = out.with_file_name(tmp_name);
    let res = write_to(&tmp, meta, inputs, recipient).and_then(|sum| {
        super::ship::rename_noreplace(&tmp, out)
            .with_context(|| format!("rename to {} (never replaces)", out.display()))?;
        Ok(sum)
    });
    if res.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    res
}

/// Decrypts and unpacks `path` into `dest_dir` (files mode 0600) and returns its meta.
/// Rejects entries with absolute or `..` paths.
pub fn extract_artifact(
    path: &Path,
    identity: &age::x25519::Identity,
    dest_dir: &Path,
) -> Result<ArtifactMeta> {
    use std::os::unix::fs::OpenOptionsExt;
    let dec = age::Decryptor::new(File::open(path).with_context(|| path.display().to_string())?)?;
    let reader = dec.decrypt(std::iter::once(identity as &dyn age::Identity))?;
    let mut archive = tar::Archive::new(zstd::Decoder::new(reader)?);
    fs::create_dir_all(dest_dir)?;
    for entry in archive.entries()? {
        let mut entry = entry?;
        let rel = entry.path()?.into_owned();
        if !rel.components().all(|c| matches!(c, Component::Normal(_))) {
            bail!("unsafe path in artifact: {}", rel.display());
        }
        if !entry.header().entry_type().is_file() {
            continue;
        }
        let dest = dest_dir.join(&rel);
        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut f = fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&dest)?;
        io::copy(&mut entry, &mut f)?;
    }
    // Drain to EOF so the zstd frame end and the age final-chunk MAC are verified.
    io::copy(&mut archive.into_inner(), &mut io::sink())
        .context("artifact stream is truncated or corrupt")?;
    let meta = fs::read(dest_dir.join("meta.json")).context("artifact has no meta.json")?;
    Ok(serde_json::from_slice(&meta)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use age::secrecy::ExposeSecret;

    fn meta() -> ArtifactMeta {
        ArtifactMeta {
            id: "hippo-full-20261105T091320Z".into(),
            kind: ArtifactKind::Full,
            label: None,
            created_ms: 1793870000000,
            base_id: None,
            hippo_version: "0.0.0".into(),
            schema_version: 29,
            db_sha256: "ab".repeat(32),
            row_counts: BTreeMap::from([("events".to_string(), 3)]),
        }
    }

    struct Fx {
        _t: tempfile::TempDir,
        root: std::path::PathBuf,
        db: std::path::PathBuf,
        cfg_link: std::path::PathBuf,
        data: std::path::PathBuf,
    }

    fn fixture() -> Fx {
        let t = tempfile::tempdir().unwrap();
        let root = t.path().to_path_buf();
        let real_cfg = root.join("real-config");
        let cfg_link = root.join("config-link");
        let data = root.join("data");
        fs::create_dir_all(&real_cfg).unwrap();
        fs::create_dir_all(data.join("fallback")).unwrap();
        std::os::unix::fs::symlink(&real_cfg, &cfg_link).unwrap();
        fs::write(real_cfg.join("config.toml"), "SECRET_MARKER_CFG=1").unwrap();
        fs::write(real_cfg.join("redact.toml"), "r=1").unwrap();
        fs::write(data.join("enrichment-state.json"), "{\"p\":true}").unwrap();
        fs::write(data.join("fallback/a.jsonl"), "{\"a\":1}\n").unwrap();
        fs::write(data.join("fallback/a.jsonl.done"), "x").unwrap();
        fs::write(data.join("fallback/b.jsonl.partial"), "x").unwrap();
        fs::write(data.join("fallback/c.jsonl.recovering"), "{\"c\":1}\n").unwrap();
        let db = root.join("snap.db");
        fs::write(&db, b"SQLite format 3\0SECRET_MARKER_DB").unwrap();
        Fx {
            _t: t,
            root,
            db,
            cfg_link,
            data,
        }
    }

    fn keys() -> (age::x25519::Identity, age::x25519::Recipient) {
        let id = age::x25519::Identity::generate();
        let r = id.to_public();
        (id, r)
    }

    fn write(fx: &Fx, r: &age::x25519::Recipient, out: &Path) -> Result<[u8; 32]> {
        let inputs = ArtifactInputs {
            db_entry: DbEntry::Full(&fx.db),
            config_dir: &fx.cfg_link,
            data_dir: &fx.data,
        };
        write_artifact(&meta(), &inputs, r, out)
    }

    #[test]
    fn artifact_roundtrip() {
        let fx = fixture();
        let (id, r) = keys();
        let out = fx.root.join("a.tar.zst.age");
        let sum = write(&fx, &r, &out).unwrap();
        assert_eq!(
            sum.as_slice(),
            Sha256::digest(fs::read(&out).unwrap()).as_slice()
        );
        assert!(!fx.root.join("a.tar.zst.age.tmp").exists());
        let dest = fx.root.join("x");
        let got = extract_artifact(&out, &id, &dest).unwrap();
        assert_eq!(got, meta());
        assert_eq!(
            fs::read(dest.join("hippo.db")).unwrap(),
            fs::read(&fx.db).unwrap()
        );
        assert_eq!(
            fs::read(dest.join("config/config.toml")).unwrap(),
            b"SECRET_MARKER_CFG=1"
        );
        assert_eq!(fs::read(dest.join("config/redact.toml")).unwrap(), b"r=1");
        assert_eq!(
            fs::read(dest.join("enrichment-state.json")).unwrap(),
            b"{\"p\":true}"
        );
        assert_eq!(
            fs::read(dest.join("fallback/a.jsonl")).unwrap(),
            b"{\"a\":1}\n"
        );
        assert!(!dest.join("fallback/a.jsonl.done").exists());
        assert!(!dest.join("fallback/b.jsonl.partial").exists());
        assert_eq!(
            fs::read(dest.join("fallback/c.jsonl.recovering")).unwrap(),
            b"{\"c\":1}\n"
        );
    }

    #[test]
    fn artifact_is_encrypted() {
        let fx = fixture();
        let (_, r) = keys();
        let out = fx.root.join("a.tar.zst.age");
        write(&fx, &r, &out).unwrap();
        let bytes = fs::read(&out).unwrap();
        for marker in ["SECRET_MARKER_DB", "SECRET_MARKER_CFG"] {
            assert!(!bytes.windows(marker.len()).any(|w| w == marker.as_bytes()));
        }
    }

    #[test]
    fn missing_config_is_error_and_leaves_no_files() {
        let fx = fixture();
        let (_, r) = keys();
        fs::remove_file(fx.root.join("real-config/config.toml")).unwrap();
        let out = fx.root.join("a.tar.zst.age");
        assert!(write(&fx, &r, &out).is_err());
        assert!(!out.exists());
        assert!(!fx.root.join("a.tar.zst.age.tmp").exists());
    }

    #[test]
    fn extract_rejects_path_traversal() {
        let fx = fixture();
        let (id, r) = keys();
        let out = fx.root.join("evil.tar.zst.age");
        let enc =
            age::Encryptor::with_recipients(std::iter::once(&r as &dyn age::Recipient)).unwrap();
        let mut zw =
            zstd::Encoder::new(enc.wrap_output(File::create(&out).unwrap()).unwrap(), 3).unwrap();
        // Header::set_path rejects `..`, so write the raw name bytes.
        let mut h = tar::Header::new_gnu();
        h.as_old_mut().name[..9].copy_from_slice(b"../escape");
        h.set_size(1);
        h.set_mode(0o600);
        h.set_cksum();
        zw.write_all(h.as_bytes()).unwrap();
        let mut block = [0u8; 512];
        block[0] = b'x';
        zw.write_all(&block).unwrap();
        zw.write_all(&[0u8; 1024]).unwrap();
        zw.finish().unwrap().finish().unwrap();
        let dest = fx.root.join("dest");
        let err = extract_artifact(&out, &id, &dest).unwrap_err();
        assert!(err.to_string().contains("unsafe path"), "{err:#}");
        assert!(!fx.root.join("escape").exists());
    }

    #[test]
    fn short_source_is_error_and_growth_is_truncated() {
        let mut tar = tar::Builder::new(Vec::new());
        let err = append_exact(&mut tar, "a", 10, 0, &b"short"[..]).unwrap_err();
        assert!(format!("{err:#}").contains("shrank"), "{err:#}");

        let mut tar = tar::Builder::new(Vec::new());
        append_exact(&mut tar, "a", 4, 0, &b"abcdEXTRA"[..]).unwrap();
        append_exact(&mut tar, "b", 2, 0, &b"zz"[..]).unwrap();
        let bytes = tar.into_inner().unwrap();
        let mut ar = tar::Archive::new(&bytes[..]);
        let got: Vec<(String, Vec<u8>)> = ar
            .entries()
            .unwrap()
            .map(|e| {
                let mut e = e.unwrap();
                let mut v = Vec::new();
                e.read_to_end(&mut v).unwrap();
                (e.path().unwrap().to_string_lossy().into_owned(), v)
            })
            .collect();
        assert_eq!(
            got,
            vec![("a".into(), b"abcd".to_vec()), ("b".into(), b"zz".to_vec())]
        );
    }

    #[test]
    fn truncated_ciphertext_is_rejected() {
        let fx = fixture();
        let (id, r) = keys();
        let out = fx.root.join("a.tar.zst.age");
        write(&fx, &r, &out).unwrap();
        let bytes = fs::read(&out).unwrap();
        for cut in [1, 20, 200] {
            fs::write(&out, &bytes[..bytes.len() - cut]).unwrap();
            assert!(
                extract_artifact(&out, &id, &fx.root.join(format!("d{cut}"))).is_err(),
                "cut {cut}"
            );
        }
    }

    #[test]
    fn artifact_id_format() {
        assert_eq!(
            artifact_id(
                ArtifactKind::Checkpoint,
                1793870000000,
                Some("Pre Re-Embed!")
            ),
            "hippo-checkpoint-20261105T091320Z-pre-re-embed"
        );
        assert_eq!(
            artifact_id(ArtifactKind::Delta, 1793870000000, None),
            "hippo-delta-20261105T091320Z"
        );
    }

    #[test]
    #[ignore = "needs age, zstd, tar CLIs; run manually"]
    fn standard_tools_decrypt_full() {
        let fx = fixture();
        let (id, r) = keys();
        let out = fx.root.join("a.tar.zst.age");
        write(&fx, &r, &out).unwrap();
        let key = fx.root.join("key.txt");
        fs::write(&key, id.to_string().expose_secret()).unwrap();
        let cmd = format!(
            "age -d -i {} {} | zstd -d | tar -t",
            key.display(),
            out.display()
        );
        let o = std::process::Command::new("sh")
            .args(["-c", &cmd])
            .output()
            .unwrap();
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
        assert!(
            String::from_utf8_lossy(&o.stdout)
                .lines()
                .any(|l| l == "hippo.db")
        );
    }
}
