//! Page-hash file and page-level delta format for backups.
//!
//! Page-hash file (little-endian): magic `HIPPOPH1`; `page_size: u32`;
//! `base_id_len: u32` + UTF-8 bytes; `page_count: u64`; then `page_count`
//! SHA-256 digests (32 bytes each), page 0 first.
//!
//! Diff file (little-endian): magic `HIPPOPD1`; `page_size: u32`;
//! `base_page_count: u64`; `new_page_count: u64`; `base_id_len: u32` + UTF-8
//! bytes; then records of `page_no: u64` (0-based) + `page_size` bytes in
//! ascending order, ending at EOF. Pages not listed are copied from the base.

use anyhow::{Context, Result, bail, ensure};
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::Path;

const HASH_MAGIC: &[u8; 8] = b"HIPPOPH1";
const DIFF_MAGIC: &[u8; 8] = b"HIPPOPD1";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageHashes {
    pub base_id: String,
    pub page_size: u32,
    pub hashes: Vec<[u8; 32]>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiffSummary {
    pub changed_pages: u64,
    pub new_page_count: u64,
}

fn read_u32(r: &mut impl Read) -> Result<u32> {
    let mut b = [0u8; 4];
    r.read_exact(&mut b)?;
    Ok(u32::from_le_bytes(b))
}

fn read_u64(r: &mut impl Read) -> Result<u64> {
    let mut b = [0u8; 8];
    r.read_exact(&mut b)?;
    Ok(u64::from_le_bytes(b))
}

fn read_magic(r: &mut impl Read, want: &[u8; 8]) -> Result<()> {
    let mut m = [0u8; 8];
    r.read_exact(&mut m)?;
    ensure!(&m == want, "bad magic");
    Ok(())
}

fn read_string(r: &mut impl Read) -> Result<String> {
    let len = usize::try_from(read_u32(r)?)?;
    ensure!(len <= 4096, "base_id too long");
    let mut b = vec![0u8; len];
    r.read_exact(&mut b)?;
    Ok(String::from_utf8(b)?)
}

fn write_string(w: &mut impl Write, s: &str) -> Result<()> {
    w.write_all(&u32::try_from(s.len())?.to_le_bytes())?;
    w.write_all(s.as_bytes())?;
    Ok(())
}

/// Returns (page_size, page_count) from the SQLite header and file length.
fn db_geometry(db: &Path) -> Result<(u32, u64)> {
    let mut f = File::open(db).with_context(|| format!("open {}", db.display()))?;
    let mut hdr = [0u8; 18];
    f.read_exact(&mut hdr).context("read sqlite header")?;
    ensure!(&hdr[..16] == b"SQLite format 3\0", "not a SQLite database");
    let raw = u16::from_be_bytes([hdr[16], hdr[17]]);
    let page_size = if raw == 1 { 65536 } else { u32::from(raw) };
    let len = f.metadata()?.len();
    ensure!(
        page_size > 0 && len % u64::from(page_size) == 0,
        "database length {len} is not a multiple of page_size {page_size}"
    );
    Ok((page_size, len / u64::from(page_size)))
}

fn hash_page(page: &[u8]) -> [u8; 32] {
    Sha256::digest(page).into()
}

/// Reads `buf` fully from `r`, treating a clean EOF at the first byte as `Ok(false)`.
fn read_page_or_eof(r: &mut impl Read, buf: &mut [u8]) -> Result<bool> {
    let mut n = 0;
    while n < buf.len() {
        let k = r.read(&mut buf[n..])?;
        if k == 0 {
            ensure!(n == 0, "truncated page");
            return Ok(false);
        }
        n += k;
    }
    Ok(true)
}

impl PageHashes {
    pub fn compute(db: &Path, base_id: &str) -> Result<PageHashes> {
        let (page_size, count) = db_geometry(db)?;
        let mut r = BufReader::new(File::open(db)?);
        let mut buf = vec![0u8; usize::try_from(page_size)?];
        let mut hashes = Vec::with_capacity(usize::try_from(count)?);
        while read_page_or_eof(&mut r, &mut buf)? {
            hashes.push(hash_page(&buf));
        }
        Ok(PageHashes {
            base_id: base_id.to_owned(),
            page_size,
            hashes,
        })
    }

    /// Writes via a sibling temp file, then renames over `path`.
    pub fn write(&self, path: &Path) -> Result<()> {
        let tmp = path.with_extension("tmp");
        {
            let mut w = BufWriter::new(File::create(&tmp)?);
            w.write_all(HASH_MAGIC)?;
            w.write_all(&self.page_size.to_le_bytes())?;
            write_string(&mut w, &self.base_id)?;
            w.write_all(&u64::try_from(self.hashes.len())?.to_le_bytes())?;
            for h in &self.hashes {
                w.write_all(h)?;
            }
            w.into_inner()?.sync_all()?;
        }
        std::fs::rename(&tmp, path)?;
        Ok(())
    }

    pub fn read(path: &Path) -> Result<PageHashes> {
        let mut r = BufReader::new(File::open(path)?);
        read_magic(&mut r, HASH_MAGIC)?;
        let page_size = read_u32(&mut r)?;
        let base_id = read_string(&mut r)?;
        let count = read_u64(&mut r)?;
        let mut hashes = Vec::new();
        for _ in 0..count {
            let mut h = [0u8; 32];
            r.read_exact(&mut h).context("truncated hash file")?;
            hashes.push(h);
        }
        let mut extra = [0u8; 1];
        ensure!(r.read(&mut extra)? == 0, "trailing bytes in hash file");
        Ok(PageHashes {
            base_id,
            page_size,
            hashes,
        })
    }
}

/// Writes the pages of `new_db` that differ from `base` (or lie past its end) to `out`.
pub fn write_diff(base: &PageHashes, new_db: &Path, out: &Path) -> Result<DiffSummary> {
    let (page_size, new_count) = db_geometry(new_db)?;
    if page_size != base.page_size {
        bail!(
            "page_size changed ({} -> {page_size}); a full backup is required",
            base.page_size
        );
    }
    let mut r = BufReader::new(File::open(new_db)?);
    let mut w = BufWriter::new(File::create(out)?);
    w.write_all(DIFF_MAGIC)?;
    w.write_all(&page_size.to_le_bytes())?;
    w.write_all(&u64::try_from(base.hashes.len())?.to_le_bytes())?;
    w.write_all(&new_count.to_le_bytes())?;
    write_string(&mut w, &base.base_id)?;
    let mut buf = vec![0u8; usize::try_from(page_size)?];
    let mut changed = 0u64;
    let mut i = 0usize;
    while read_page_or_eof(&mut r, &mut buf)? {
        if base.hashes.get(i) != Some(&hash_page(&buf)) {
            w.write_all(&u64::try_from(i)?.to_le_bytes())?;
            w.write_all(&buf)?;
            changed += 1;
        }
        i += 1;
    }
    w.into_inner()?.sync_all()?;
    Ok(DiffSummary {
        changed_pages: changed,
        new_page_count: new_count,
    })
}

struct DiffReader {
    r: BufReader<File>,
    page_size: u32,
    base_page_count: u64,
    new_page_count: u64,
}

impl DiffReader {
    fn open(path: &Path) -> Result<Self> {
        let mut r = BufReader::new(File::open(path)?);
        read_magic(&mut r, DIFF_MAGIC)?;
        let page_size = read_u32(&mut r)?;
        let base_page_count = read_u64(&mut r)?;
        let new_page_count = read_u64(&mut r)?;
        read_string(&mut r)?;
        ensure!(page_size > 0, "zero page_size in diff");
        Ok(Self {
            r,
            page_size,
            base_page_count,
            new_page_count,
        })
    }

    /// Next `(page_no, bytes)` record, or `None` at EOF.
    fn next_record(&mut self, buf: &mut [u8]) -> Result<Option<u64>> {
        let mut no = [0u8; 8];
        if !read_page_or_eof(&mut self.r, &mut no)? {
            return Ok(None);
        }
        self.r.read_exact(buf).context("truncated diff record")?;
        Ok(Some(u64::from_le_bytes(no)))
    }
}

/// Rebuilds the new database from `base_db` plus `diff`, streaming page by page.
pub fn apply_diff(base_db: &Path, diff: &Path, out_db: &Path) -> Result<()> {
    let mut d = DiffReader::open(diff)?;
    let (base_ps, base_count) = db_geometry(base_db)?;
    ensure!(base_ps == d.page_size, "page_size mismatch with base");
    ensure!(base_count == d.base_page_count, "base page count mismatch");
    let mut base = BufReader::new(File::open(base_db)?);
    let mut w = BufWriter::new(File::create(out_db)?);
    let mut rec = vec![0u8; usize::try_from(d.page_size)?];
    let mut page = vec![0u8; rec.len()];
    let mut next = d.next_record(&mut rec)?;
    for i in 0..d.new_page_count {
        let in_base = i < base_count;
        if in_base {
            base.read_exact(&mut page)?; // keep the base cursor aligned even when replaced
        }
        if next == Some(i) {
            w.write_all(&rec)?;
            next = d.next_record(&mut rec)?;
        } else {
            ensure!(in_base, "diff lacks page {i} beyond the base");
            w.write_all(&page)?;
        }
    }
    ensure!(next.is_none(), "diff has out-of-order or excess records");
    w.into_inner()?.sync_all()?;
    Ok(())
}

/// Page hashes of the database that `apply_diff(base, diff)` would produce.
pub fn hashes_after_diff(base: &PageHashes, diff: &Path) -> Result<Vec<[u8; 32]>> {
    let mut d = DiffReader::open(diff)?;
    ensure!(
        d.page_size == base.page_size,
        "page_size mismatch with base"
    );
    ensure!(
        d.base_page_count == u64::try_from(base.hashes.len())?,
        "base page count mismatch"
    );
    let n = usize::try_from(d.new_page_count)?;
    let mut out: Vec<Option<[u8; 32]>> = (0..n).map(|i| base.hashes.get(i).copied()).collect();
    let mut buf = vec![0u8; usize::try_from(d.page_size)?];
    while let Some(no) = d.next_record(&mut buf)? {
        let slot = out
            .get_mut(usize::try_from(no)?)
            .context("diff record beyond new_page_count")?;
        *slot = Some(hash_page(&buf));
    }
    out.into_iter()
        .enumerate()
        .map(|(i, h)| h.with_context(|| format!("diff lacks page {i} beyond the base")))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use rusqlite::Connection;

    fn make_db(path: &Path, page_size: u32, rows: usize) {
        let c = Connection::open(path).unwrap();
        c.pragma_update(None, "page_size", page_size).unwrap();
        c.execute_batch("CREATE TABLE t(id INTEGER PRIMARY KEY, b BLOB);")
            .unwrap();
        grow(&c, rows);
    }

    fn grow(c: &Connection, rows: usize) {
        for i in 0..rows {
            c.execute("INSERT INTO t(b) VALUES (?1)", [vec![i as u8; 600]])
                .unwrap();
        }
    }

    fn roundtrip(base: &Path, new: &Path, dir: &Path) -> DiffSummary {
        let hashes = PageHashes::compute(base, "base1").unwrap();
        let diff = dir.join("d.pagediff");
        let summary = write_diff(&hashes, new, &diff).unwrap();
        let out = dir.join("out.db");
        apply_diff(base, &diff, &out).unwrap();
        assert_eq!(std::fs::read(&out).unwrap(), std::fs::read(new).unwrap());
        assert_eq!(
            hashes_after_diff(&hashes, &diff).unwrap(),
            PageHashes::compute(new, "x").unwrap().hashes
        );
        summary
    }

    #[test]
    fn diff_roundtrip_unchanged() {
        let dir = tempfile::tempdir().unwrap();
        let base = dir.path().join("base.db");
        make_db(&base, 4096, 50);
        let new = dir.path().join("new.db");
        std::fs::copy(&base, &new).unwrap();
        let s = roundtrip(&base, &new, dir.path());
        assert_eq!(s.changed_pages, 0);
    }

    #[test]
    fn diff_rejects_page_size_change() {
        let dir = tempfile::tempdir().unwrap();
        let (a, b) = (dir.path().join("a.db"), dir.path().join("b.db"));
        make_db(&a, 4096, 10);
        make_db(&b, 8192, 10);
        let h = PageHashes::compute(&a, "a").unwrap();
        let err = write_diff(&h, &b, &dir.path().join("d")).unwrap_err();
        assert!(err.to_string().contains("page_size"), "{err}");
    }

    #[test]
    fn hashes_file_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a.db");
        make_db(&a, 4096, 20);
        let h = PageHashes::compute(&a, "base-xyz").unwrap();
        let p = dir.path().join("a.hashes");
        h.write(&p).unwrap();
        assert_eq!(PageHashes::read(&p).unwrap(), h);
        assert!(!p.with_extension("tmp").exists());
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(24))]
        #[test]
        fn diff_roundtrip_growth_and_shrink(
            ops in proptest::collection::vec((any::<bool>(), 1usize..80), 1..6)
        ) {
            let dir = tempfile::tempdir().unwrap();
            let base = dir.path().join("base.db");
            make_db(&base, 4096, 100);
            let new = dir.path().join("new.db");
            std::fs::copy(&base, &new).unwrap();
            let c = Connection::open(&new).unwrap();
            for (insert, n) in ops {
                if insert {
                    grow(&c, n);
                } else {
                    c.execute("DELETE FROM t WHERE id % 3 = ?1 AND id <= ?2", [n as i64 % 3, n as i64 * 2]).unwrap();
                }
            }
            c.execute_batch("VACUUM").unwrap();
            drop(c);
            roundtrip(&base, &new, dir.path());
        }
    }
}
