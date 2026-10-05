use super::{detect_vars, render_plist};
use hippo_core::config::HippoConfig;
use std::path::PathBuf;
use std::process::Command;

#[test]
fn generated_services_preserve_runtime_paths() {
    if let Some(root) = std::env::var_os("HIPPO_TEST_SERVICE_ROOT") {
        let root = PathBuf::from(root);
        let custom = std::env::var("HIPPO_TEST_SERVICE_MODE").unwrap() == "custom";
        let config_root = root.join(if custom { "config" } else { ".config" });
        let data_root = root.join(if custom { "data" } else { ".local/share" });
        let loaded = HippoConfig::load_default().unwrap();
        assert_eq!(loaded.brain.port, 19876);
        assert_eq!(loaded.storage.config_dir, config_root.join("hippo"));
        assert_eq!(loaded.storage.data_dir, data_root.join("hippo"));
        if std::env::var_os("HIPPO_TEST_SERVICE_CONSUMER").is_some() {
            return;
        }
        let vars = detect_vars(&root.join("brain"), Some(root.join("bin/hippo"))).unwrap();
        assert_eq!(vars.data_dir, data_root.join("hippo"));
        for entry in
            std::fs::read_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/../../launchd")).unwrap()
        {
            let path = entry.unwrap().path();
            if path
                .extension()
                .is_none_or(|extension| extension != "plist")
            {
                continue;
            }
            let rendered = render_plist(&std::fs::read_to_string(&path).unwrap(), &vars);
            let output_path = root.join(path.file_name().unwrap());
            std::fs::write(&output_path, rendered).unwrap();
            let output = Command::new("python3")
                .args([
                    "-c",
                    r#"
import json, plistlib, sys
with open(sys.argv[1], 'rb') as f:
    env = plistlib.load(f)['EnvironmentVariables']
print(json.dumps(env))
"#,
                ])
                .arg(&output_path)
                .output()
                .unwrap();
            assert!(output.status.success(), "{path:?}: {output:?}");
            let env: std::collections::HashMap<String, String> =
                serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(env["XDG_CONFIG_HOME"], config_root.to_str().unwrap());
            assert_eq!(env["XDG_DATA_HOME"], data_root.to_str().unwrap());
            let output = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "install::service_paths::generated_services_preserve_runtime_paths",
                ])
                .env_remove("XDG_CONFIG_HOME")
                .env_remove("XDG_DATA_HOME")
                .envs(env)
                .env("HIPPO_TEST_SERVICE_CONSUMER", "1")
                .output()
                .unwrap();
            assert!(output.status.success(), "{path:?}: {output:?}");
        }
        return;
    }
    for mode in ["unset", "empty", "custom"] {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let config_root = root.join(if mode == "custom" {
            "config"
        } else {
            ".config"
        });
        std::fs::create_dir_all(config_root.join("hippo")).unwrap();
        std::fs::write(
            config_root.join("hippo/config.toml"),
            "[brain]\nport = 19876\n",
        )
        .unwrap();
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "install::service_paths::generated_services_preserve_runtime_paths",
            ])
            .env("HOME", root)
            .env("HIPPO_TEST_SERVICE_ROOT", root)
            .env("HIPPO_TEST_SERVICE_MODE", mode)
            .env_remove("HIPPO_TEST_SERVICE_CONSUMER");
        for (name, base) in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")] {
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
    }
}
