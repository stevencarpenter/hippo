use hippo_core::config::HippoConfig;
use std::path::PathBuf;
use std::process::Command;

#[test]
fn config_paths_follow_xdg_with_empty_values_treated_as_unset() {
    if let Some(root) = std::env::var_os("HIPPO_TEST_XDG_ROOT") {
        let root = PathBuf::from(root);
        let custom = std::env::var("HIPPO_TEST_XDG_MODE").unwrap() == "custom";
        let config_dir = root.join(if custom {
            "xdg-config/hippo"
        } else {
            ".config/hippo"
        });
        let data_dir = root.join(if custom {
            "xdg-data/hippo"
        } else {
            ".local/share/hippo"
        });
        let defaults = HippoConfig::default();
        assert_eq!(defaults.storage.config_dir, config_dir);
        assert_eq!(defaults.storage.data_dir, data_dir);

        let loaded = HippoConfig::load_default().unwrap();
        assert_eq!(loaded.storage.config_dir, config_dir);
        assert_eq!(loaded.brain.port, if custom { 9882 } else { 9881 });
        let expected_data = std::env::var_os("HIPPO_TEST_EXPLICIT_DATA_DIR")
            .map(PathBuf::from)
            .unwrap_or(data_dir);
        assert_eq!(loaded.db_path(), expected_data.join("hippo.db"));
        return;
    }

    for mode in ["unset", "empty", "custom"] {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        for (base, port) in [(".config", 9881), ("xdg-config", 9882)] {
            let dir = root.join(base).join("hippo");
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("config.toml"), format!("[brain]\nport = {port}\n")).unwrap();
        }
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "config_paths_follow_xdg_with_empty_values_treated_as_unset",
            ])
            .current_dir(root)
            .env("HOME", root)
            .env("HIPPO_TEST_XDG_ROOT", root)
            .env("HIPPO_TEST_XDG_MODE", mode)
            .env_remove("HIPPO_TEST_EXPLICIT_DATA_DIR");
        for (name, base) in [
            ("XDG_CONFIG_HOME", "xdg-config"),
            ("XDG_DATA_HOME", "xdg-data"),
        ] {
            match mode {
                "unset" => {
                    command.env_remove(name);
                }
                "empty" => {
                    command.env(name, "");
                }
                _ => {
                    command.env(name, root.join(base));
                }
            }
        }
        let output = command.output().unwrap();
        assert!(output.status.success(), "{mode}: {output:?}");

        let config_base = if mode == "custom" {
            "xdg-config"
        } else {
            ".config"
        };
        let port = if mode == "custom" { 9882 } else { 9881 };
        let explicit_data = root.join("explicit-data");
        std::fs::write(
            root.join(config_base).join("hippo/config.toml"),
            format!(
                "[brain]\nport = {port}\n[storage]\ndata_dir = '{}'\n",
                explicit_data.display()
            ),
        )
        .unwrap();
        let output = command
            .env("HIPPO_TEST_EXPLICIT_DATA_DIR", explicit_data)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{mode} with explicit data: {output:?}"
        );
        assert!(!root.join("hippo").exists());
    }
}
