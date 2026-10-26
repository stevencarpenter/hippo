//! Doctor check: installed `hippo-mcp` package version vs the running brain.
//!
//!   cargo test -p hippo-daemon -- doctor::check_mcp_server_version

mod doctor {
    mod check_mcp_server_version {
        use std::fs;
        use std::os::unix::fs::PermissionsExt;
        use std::path::Path;

        use hippo_daemon::commands::check_mcp_server_version;
        use tempfile::tempdir;

        /// Build a fake installed brain: `hippo-mcp` entry point plus the
        /// given `src/hippo_brain/_version.py` stamp (when `stamp` is `Some`).
        fn fake_brain(dir: &Path, stamp: Option<&str>) {
            let bin = dir.join(".venv/bin");
            fs::create_dir_all(&bin).unwrap();
            let mcp = bin.join("hippo-mcp");
            fs::write(&mcp, "#!/bin/sh\n").unwrap();
            fs::set_permissions(&mcp, fs::Permissions::from_mode(0o755)).unwrap();
            if let Some(v) = stamp {
                let src = dir.join("src/hippo_brain");
                fs::create_dir_all(&src).unwrap();
                fs::write(src.join("_version.py"), format!("__version__ = \"{v}\"\n")).unwrap();
            }
        }

        fn health(version: &str) -> serde_json::Value {
            serde_json::json!({ "version": version })
        }

        #[test]
        fn matching_versions_pass() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path(), Some("0.40.2-dev.1+gabc1234"));
            let json = health("0.40.2-dev.1+gabc1234");
            assert_eq!(check_mcp_server_version(tmp.path(), Some(&json), false), 0);
        }

        #[test]
        fn version_mismatch_warns_without_failing() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path(), Some("0.40.2"));
            let json = health("0.40.1");
            assert_eq!(
                check_mcp_server_version(tmp.path(), Some(&json), true),
                0,
                "a version mismatch is [WW], not a failure"
            );
        }

        #[test]
        fn missing_entry_point_fails() {
            let tmp = tempdir().unwrap();
            let json = health("0.40.2");
            assert_eq!(check_mcp_server_version(tmp.path(), Some(&json), true), 1);
        }

        #[test]
        fn brain_unreachable_is_not_a_failure() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path(), Some("0.40.2"));
            assert_eq!(check_mcp_server_version(tmp.path(), None, false), 0);
        }

        #[test]
        fn unreadable_installed_version_does_not_fail() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path(), None);
            let json = health("0.40.2");
            assert_eq!(check_mcp_server_version(tmp.path(), Some(&json), false), 0);
        }

        #[test]
        fn dist_info_is_used_when_no_stamp_exists() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path(), None);
            let sp = tmp.path().join(".venv/lib/python3.14/site-packages");
            fs::create_dir_all(sp.join("hippo_brain-0.40.2.dist-info")).unwrap();
            let json = health("0.40.2");
            assert_eq!(check_mcp_server_version(tmp.path(), Some(&json), false), 0);
        }
    }
}
