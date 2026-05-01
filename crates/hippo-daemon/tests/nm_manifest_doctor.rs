//! Regression guard for capture-reliability F-6.
//!
//! Failure mode: the Native Messaging host manifest at
//! `~/Library/Application Support/Mozilla/NativeMessagingHosts/hippo_daemon.json`
//! drifts out of sync with reality. Concretely:
//!
//! 1. The `path` field points to a binary that no longer exists
//!    (user moved / rebuilt in a different target dir).
//! 2. The wrapper script at `path` exists but is not executable.
//! 3. The `allowed_extensions` list omits `hippo-browser@local` so Firefox
//!    refuses to launch the host.
//!
//! `hippo doctor` does not currently read this manifest at all — when the
//! browser source goes silent the user has no automated signal. These tests
//! are `#[ignore]` until the doctor adds the check; the skeleton reserves
//! the test file so enabling them is a one-line change.
//!
//! Tracking: docs/capture/test-matrix.md row F-6.

use std::fs;
use std::os::unix::fs::PermissionsExt;

use tempfile::tempdir;

/// Shared setup: write a valid manifest + wrapper pair into a tempdir and
/// return the manifest path. The helper is intentionally simple so the
/// real doctor check can accept either (manifest_path) or (nm_dir) as
/// its entry point without reshaping the test.
fn write_valid_manifest(nm_dir: &std::path::Path) -> std::path::PathBuf {
    fs::create_dir_all(nm_dir).unwrap();
    let wrapper = nm_dir.join("hippo-native-messaging");
    fs::write(
        &wrapper,
        "#!/bin/bash\nexec /path/to/hippo native-messaging-host\n",
    )
    .unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();

    let manifest = nm_dir.join("hippo_daemon.json");
    let json = serde_json::json!({
        "name": "hippo_daemon",
        "description": "Hippo knowledge capture daemon - browser event bridge",
        "path": wrapper.to_string_lossy(),
        "type": "stdio",
        "allowed_extensions": ["hippo-browser@local"],
    });
    fs::write(&manifest, serde_json::to_string_pretty(&json).unwrap()).unwrap();
    manifest
}

#[test]
#[ignore = "blocked on F-6 doctor source change — hippo doctor does not yet read the NM manifest"]
fn doctor_ok_on_valid_nm_manifest() {
    let temp = tempdir().unwrap();
    let _manifest = write_valid_manifest(temp.path());
    // TODO when F-6 lands: call the doctor helper that inspects this path
    // and assert its return is [OK]. Shape like:
    //   assert_eq!(doctor::check_nm_manifest(temp.path()), NmManifestHealth::Ok);
    unimplemented!("remove #[ignore] once hippo doctor inspects NM manifest");
}

#[test]
#[ignore = "blocked on F-6 doctor source change — no manifest-inspection helper exists"]
fn doctor_flags_missing_manifest_file() {
    let temp = tempdir().unwrap();
    // Intentionally write no manifest.
    // TODO when F-6 lands:
    //   assert_eq!(doctor::check_nm_manifest(temp.path()),
    //              NmManifestHealth::Missing);
    let _ = temp;
    unimplemented!("remove #[ignore] once doctor flags missing NM manifest");
}

#[test]
#[ignore = "blocked on F-6 doctor source change"]
fn doctor_flags_nonexecutable_wrapper_path() {
    let temp = tempdir().unwrap();
    let manifest = write_valid_manifest(temp.path());
    // Remove execute bit on the wrapper.
    let wrapper = temp.path().join("hippo-native-messaging");
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o644)).unwrap();
    // TODO: assert doctor reports [!!] wrapper not executable.
    let _ = manifest;
    unimplemented!();
}

#[test]
#[ignore = "blocked on F-6 doctor source change"]
fn doctor_flags_manifest_pointing_to_deleted_binary() {
    let temp = tempdir().unwrap();
    let manifest = write_valid_manifest(temp.path());
    // Delete the wrapper that `path` references.
    fs::remove_file(temp.path().join("hippo-native-messaging")).unwrap();
    let _ = manifest;
    // TODO: assert doctor reports [!!] wrapper path does not exist.
    unimplemented!();
}

#[test]
#[ignore = "blocked on F-6 doctor source change"]
fn doctor_flags_missing_allowed_extension() {
    let temp = tempdir().unwrap();
    let manifest = temp.path().join("hippo_daemon.json");
    let wrapper = temp.path().join("hippo-native-messaging");
    fs::write(&wrapper, "#!/bin/bash\n").unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();

    // Manifest present but allowed_extensions is empty — Firefox will
    // refuse to launch the host with this shape.
    let json = serde_json::json!({
        "name": "hippo_daemon",
        "path": wrapper.to_string_lossy(),
        "type": "stdio",
        "allowed_extensions": [],
    });
    fs::write(&manifest, serde_json::to_string_pretty(&json).unwrap()).unwrap();

    // TODO: assert doctor reports [!!] allowed_extensions missing
    // 'hippo-browser@local'.
    unimplemented!();
}
