//! Doctor check: installed `hippo-mcp` package version vs the running brain.
//!
//!   cargo test -p hippo-daemon -- doctor::check_mcp_server_version

mod doctor {
    mod check_mcp_server_version {
        use std::fs;
        use std::os::unix::fs::PermissionsExt;
        use std::path::Path;

        use hippo_daemon::commands::{
            McpVersionStatus, check_mcp_server_version, mcp_version_status,
        };
        use tempfile::tempdir;

        const SP: &str = ".venv/lib/python3.14/site-packages";

        /// Fake installed brain with an executable `hippo-mcp` entry point.
        fn fake_brain(dir: &Path) {
            let bin = dir.join(".venv/bin");
            fs::create_dir_all(&bin).unwrap();
            let mcp = bin.join("hippo-mcp");
            fs::write(&mcp, "#!/bin/sh\n").unwrap();
            fs::set_permissions(&mcp, fs::Permissions::from_mode(0o755)).unwrap();
        }

        fn stamp(dir: &Path, rel: &str, version: &str) {
            let pkg = dir.join(rel).join("hippo_brain");
            fs::create_dir_all(&pkg).unwrap();
            fs::write(
                pkg.join("_version.py"),
                format!("__version__ = \"{version}\"\n"),
            )
            .unwrap();
        }

        fn dist_info(dir: &Path, version: &str) {
            fs::create_dir_all(
                dir.join(SP)
                    .join(format!("hippo_brain-{version}.dist-info")),
            )
            .unwrap();
        }

        fn health(version: &str) -> serde_json::Value {
            serde_json::json!({ "version": version })
        }

        fn installed(v: &str) -> String {
            v.to_string()
        }

        #[test]
        fn matching_versions_report_match() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path());
            stamp(tmp.path(), "src", "0.40.2-dev.1+gabc1234");
            let json = health("0.40.2-dev.1+gabc1234");
            assert_eq!(
                mcp_version_status(tmp.path(), Some(&json)),
                McpVersionStatus::Match {
                    installed: installed("0.40.2-dev.1+gabc1234")
                }
            );
            assert_eq!(check_mcp_server_version(tmp.path(), Some(&json), false), 0);
        }

        #[test]
        fn version_mismatch_reports_both_versions_and_does_not_fail() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path());
            stamp(tmp.path(), "src", "0.40.2");
            let json = health("0.40.1");
            assert_eq!(
                mcp_version_status(tmp.path(), Some(&json)),
                McpVersionStatus::Mismatch {
                    installed: installed("0.40.2"),
                    running: "0.40.1".to_string(),
                }
            );
            assert_eq!(check_mcp_server_version(tmp.path(), Some(&json), true), 0);
        }

        #[test]
        fn missing_entry_point_fails() {
            let tmp = tempdir().unwrap();
            let json = health("0.40.2");
            assert_eq!(
                mcp_version_status(tmp.path(), Some(&json)),
                McpVersionStatus::EntryPointMissing
            );
            assert_eq!(check_mcp_server_version(tmp.path(), Some(&json), true), 1);
        }

        #[test]
        fn brain_unreachable_reports_installed_version() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path());
            stamp(tmp.path(), "src", "0.40.2");
            assert_eq!(
                mcp_version_status(tmp.path(), None),
                McpVersionStatus::BrainUnreachable {
                    installed: installed("0.40.2")
                }
            );
            // /health without a `version` field is treated the same way.
            let json = serde_json::json!({ "status": "ok" });
            assert!(matches!(
                mcp_version_status(tmp.path(), Some(&json)),
                McpVersionStatus::BrainUnreachable { .. }
            ));
            assert_eq!(check_mcp_server_version(tmp.path(), None, false), 0);
        }

        #[test]
        fn no_version_source_is_unreadable() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path());
            let json = health("0.40.2");
            assert_eq!(
                mcp_version_status(tmp.path(), Some(&json)),
                McpVersionStatus::VersionUnreadable
            );
            assert_eq!(check_mcp_server_version(tmp.path(), Some(&json), false), 0);
        }

        #[test]
        fn dist_info_is_used_when_no_stamp_exists() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path());
            dist_info(tmp.path(), "0.40.2");
            let json = health("0.40.2");
            assert_eq!(
                mcp_version_status(tmp.path(), Some(&json)),
                McpVersionStatus::Match {
                    installed: installed("0.40.2")
                }
            );
        }

        #[test]
        fn editable_install_reads_stamp_from_src() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path());
            // Editable layout: site-packages has only dist-info (older version),
            // the stamp lives in src/.
            dist_info(tmp.path(), "0.40.1");
            stamp(tmp.path(), "src", "0.40.1-dev.4+gca902f8");
            let json = health("0.40.1-dev.4+gca902f8");
            assert_eq!(
                mcp_version_status(tmp.path(), Some(&json)),
                McpVersionStatus::Match {
                    installed: installed("0.40.1-dev.4+gca902f8")
                }
            );
        }

        #[test]
        fn site_packages_stamp_wins_over_src_and_dist_info() {
            let tmp = tempdir().unwrap();
            fake_brain(tmp.path());
            dist_info(tmp.path(), "0.1.0");
            stamp(tmp.path(), "src", "0.2.0");
            stamp(tmp.path(), SP, "0.3.0");
            let json = health("0.3.0");
            assert_eq!(
                mcp_version_status(tmp.path(), Some(&json)),
                McpVersionStatus::Match {
                    installed: installed("0.3.0")
                }
            );
        }
    }
}
