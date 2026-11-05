//! Moves a finished artifact from the local outbox into the destination folder.

use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};
use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

/// Hypothesis: Google Drive for desktop sets this xattr once the item has an id on the server.
/// The operator verifies this on a real Drive mount; replace here and in `is_uploaded` only.
pub const DRIVE_UPLOADED_XATTR: &str = "com.google.drivefs.item-id#S";

#[derive(Debug, PartialEq, Eq)]
pub enum ShipOutcome {
    Shipped { dest_file: PathBuf },
    DestinationUnavailable,
}

pub fn is_drive_fs(dest: &Path) -> bool {
    dest.to_string_lossy()
        .contains("/Library/CloudStorage/GoogleDrive-")
}

/// Reports whether Google Drive has uploaded `file`. Outside a Drive mount there is no
/// upload step, so the answer is `true`. Any failure other than a missing attribute is an
/// error, because pruning trusts a `true` result.
pub fn is_uploaded(file: &Path, drive_fs: bool) -> Result<bool> {
    if !drive_fs {
        return Ok(true);
    }
    let path = CString::new(file.as_os_str().as_bytes()).context("path contains NUL")?;
    let name = CString::new(DRIVE_UPLOADED_XATTR).expect("constant has no NUL");
    // SAFETY: both pointers are valid NUL-terminated strings; a null value with size 0
    // asks only for the attribute length.
    #[cfg(target_os = "macos")]
    let rc = unsafe {
        libc::getxattr(
            path.as_ptr(),
            name.as_ptr(),
            std::ptr::null_mut(),
            0,
            0,
            libc::XATTR_NOFOLLOW,
        )
    };
    #[cfg(not(target_os = "macos"))]
    let rc = unsafe { libc::lgetxattr(path.as_ptr(), name.as_ptr(), std::ptr::null_mut(), 0) };
    if rc >= 0 {
        return Ok(true);
    }
    let err = io::Error::last_os_error();
    #[cfg(target_os = "macos")]
    let missing = libc::ENOATTR;
    #[cfg(not(target_os = "macos"))]
    let missing = libc::ENODATA;
    if err.raw_os_error() == Some(missing) {
        Ok(false)
    } else {
        Err(err).with_context(|| format!("getxattr {}", file.display()))
    }
}

fn sha256_of(path: &Path) -> io::Result<[u8; 32]> {
    let mut f = File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            return Ok(h.finalize().into());
        }
        h.update(&buf[..n]);
    }
}

/// Copies to `dest/.{name}.partial`, fsyncs, renames, verifies by read-back, then deletes
/// the outbox file. `dest` is never created. An existing `dest/{name}` is an error.
pub fn ship(outbox_file: &Path, dest: &Path, expected_sha256: &[u8; 32]) -> Result<ShipOutcome> {
    let name = outbox_file.file_name().context("outbox file has no name")?;
    match fs::metadata(dest) {
        Ok(m) if m.is_dir() => {}
        Ok(_) => bail!("destination {} is not a directory", dest.display()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            return Ok(ShipOutcome::DestinationUnavailable);
        }
        Err(e) => return Err(e).with_context(|| format!("stat {}", dest.display())),
    }
    let final_path = dest.join(name);
    if fs::symlink_metadata(&final_path).is_ok() {
        bail!(
            "{} already exists; refusing to overwrite",
            final_path.display()
        );
    }
    let mut partial_name = std::ffi::OsString::from(".");
    partial_name.push(name);
    partial_name.push(".partial");
    let partial = dest.join(partial_name);

    let copied = (|| -> io::Result<()> {
        let mut src = File::open(outbox_file)?;
        let mut out = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&partial)?;
        io::copy(&mut src, &mut out)?;
        out.sync_all()
    })();
    if let Err(e) = copied {
        let _ = fs::remove_file(&partial);
        // dest vanished mid-copy (drive unmounted): same as unavailable.
        if e.kind() == io::ErrorKind::NotFound && !dest.exists() {
            return Ok(ShipOutcome::DestinationUnavailable);
        }
        return Err(e).with_context(|| format!("copy to {}", partial.display()));
    }
    if let Err(e) = fs::rename(&partial, &final_path) {
        let _ = fs::remove_file(&partial);
        return Err(e).with_context(|| format!("rename to {}", final_path.display()));
    }
    let actual =
        sha256_of(&final_path).with_context(|| format!("read back {}", final_path.display()));
    if !matches!(&actual, Ok(h) if h == expected_sha256) {
        // We just created this file; remove it so a bad copy never looks shipped.
        let _ = fs::remove_file(&final_path);
        actual?;
        bail!("read-back hash mismatch for {}", final_path.display());
    }
    fs::remove_file(outbox_file)
        .with_context(|| format!("remove outbox file {}", outbox_file.display()))?;
    Ok(ShipOutcome::Shipped {
        dest_file: final_path,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn outbox_file(dir: &Path, bytes: &[u8]) -> (PathBuf, [u8; 32]) {
        let p = dir.join("a.tar.zst.age");
        fs::write(&p, bytes).unwrap();
        (p, Sha256::digest(bytes).into())
    }

    #[test]
    fn ship_moves_and_verifies() {
        let t = tempfile::tempdir().unwrap();
        let dest = t.path().join("dest");
        fs::create_dir(&dest).unwrap();
        let (src, hash) = outbox_file(t.path(), b"payload");
        let out = ship(&src, &dest, &hash).unwrap();
        let dest_file = dest.join("a.tar.zst.age");
        assert_eq!(
            out,
            ShipOutcome::Shipped {
                dest_file: dest_file.clone()
            }
        );
        assert_eq!(fs::read(dest_file).unwrap(), b"payload");
        assert!(!src.exists());
        assert_eq!(fs::read_dir(&dest).unwrap().count(), 1, "no .partial left");
    }

    #[test]
    fn ship_missing_dest_keeps_outbox() {
        let t = tempfile::tempdir().unwrap();
        let dest = t.path().join("absent");
        let (src, hash) = outbox_file(t.path(), b"payload");
        assert_eq!(
            ship(&src, &dest, &hash).unwrap(),
            ShipOutcome::DestinationUnavailable
        );
        assert!(src.exists());
        assert!(!dest.exists(), "dest must not be created");
    }

    #[test]
    fn ship_hash_mismatch_errors_and_removes_partial() {
        let t = tempfile::tempdir().unwrap();
        let dest = t.path().join("dest");
        fs::create_dir(&dest).unwrap();
        let (src, _) = outbox_file(t.path(), b"payload");
        assert!(ship(&src, &dest, &[0u8; 32]).is_err());
        assert!(src.exists());
        assert_eq!(fs::read_dir(&dest).unwrap().count(), 0);
    }

    #[test]
    fn ship_does_not_overwrite_existing_destination() {
        let t = tempfile::tempdir().unwrap();
        let dest = t.path().join("dest");
        fs::create_dir(&dest).unwrap();
        fs::write(dest.join("a.tar.zst.age"), b"old").unwrap();
        let (src, hash) = outbox_file(t.path(), b"payload");
        assert!(ship(&src, &dest, &hash).is_err());
        assert_eq!(fs::read(dest.join("a.tar.zst.age")).unwrap(), b"old");
        assert!(src.exists());
        assert_eq!(fs::read_dir(&dest).unwrap().count(), 1);
    }

    #[test]
    fn drive_fs_detected_by_path() {
        assert!(is_drive_fs(Path::new(
            "/Users/x/Library/CloudStorage/GoogleDrive-a@b.c/My Drive/hippo"
        )));
        assert!(!is_drive_fs(Path::new("/Users/x/backups")));
    }

    #[test]
    fn uploaded_detects_xattr() {
        let t = tempfile::tempdir().unwrap();
        let f = t.path().join("f");
        fs::write(&f, b"x").unwrap();
        assert!(!is_uploaded(&f, true).unwrap());
        let path = CString::new(f.as_os_str().as_bytes()).unwrap();
        let name = CString::new(DRIVE_UPLOADED_XATTR).unwrap();
        let rc = unsafe { set_xattr_for_test(&path, &name) };
        assert_eq!(rc, 0, "{}", io::Error::last_os_error());
        assert!(is_uploaded(&f, true).unwrap());
        assert!(is_uploaded(&f, false).unwrap());
        assert!(is_uploaded(&t.path().join("none"), false).unwrap());
    }

    #[test]
    fn uploaded_errors_on_missing_file_when_drive() {
        let t = tempfile::tempdir().unwrap();
        assert!(is_uploaded(&t.path().join("none"), true).is_err());
    }

    unsafe fn set_xattr_for_test(path: &CString, name: &CString) -> i32 {
        let v = b"id";
        // SAFETY: valid NUL-terminated strings and a valid buffer of the stated length.
        unsafe {
            libc::setxattr(
                path.as_ptr(),
                name.as_ptr(),
                v.as_ptr().cast(),
                v.len(),
                0,
                0,
            )
        }
    }
}
