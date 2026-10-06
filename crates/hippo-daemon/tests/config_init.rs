use hippo_core::config::HippoConfig;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::Command;
use tempfile::tempdir;

fn hippo(root: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_hippo"));
    command
        .env("HOME", root)
        .env("XDG_CONFIG_HOME", root.join("config"))
        .env("XDG_DATA_HOME", root.join("data"))
        .env("HIPPO_OTEL_ENABLED", "false")
        .env("EDITOR", root.join("missing-editor"));
    command
}

#[test]
fn init_creates_private_default_config_at_home_or_xdg_path() {
    for use_xdg in [false, true] {
        let temp = tempdir().unwrap();
        let mut command = hippo(temp.path());
        let base = if use_xdg {
            temp.path().join("config")
        } else {
            command.env_remove("XDG_CONFIG_HOME");
            temp.path().join(".config")
        };
        let output = command.args(["config", "init"]).output().unwrap();
        assert!(output.status.success(), "{output:?}");
        let config_dir = base.join("hippo");
        let config_path = config_dir.join("config.toml");
        assert_eq!(
            std::fs::read(&config_path).unwrap(),
            include_bytes!("../../../config/config.default.toml")
        );
        HippoConfig::load(&config_path).unwrap();
        for (path, mode) in [(&config_dir, 0o700), (&config_path, 0o600)] {
            assert_eq!(
                std::fs::metadata(path).unwrap().permissions().mode() & 0o777,
                mode
            );
        }
        assert!(!temp.path().join("data/hippo/hippo.db").exists());
    }
}

#[test]
fn init_preserves_existing_config_on_repeated_runs() {
    let temp = tempdir().unwrap();
    let config_dir = temp.path().join("config/hippo");
    std::fs::create_dir_all(&config_dir).unwrap();
    let config_path = config_dir.join("config.toml");
    let contents = "# Keep my settings and comments\n[browser]\nenabled = false\n";
    std::fs::write(&config_path, contents).unwrap();
    for _ in 0..2 {
        let output = hippo(temp.path())
            .args(["config", "init"])
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        assert_eq!(std::fs::read_to_string(&config_path).unwrap(), contents);
    }
}

#[test]
fn edit_still_initializes_config_before_opening_editor() {
    let temp = tempdir().unwrap();
    let output = hippo(temp.path())
        .args(["config", "edit"])
        .env("EDITOR", "/usr/bin/true")
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        std::fs::read(temp.path().join("config/hippo/config.toml")).unwrap(),
        include_bytes!("../../../config/config.default.toml")
    );
}

#[test]
fn init_uses_configured_directory_and_reports_create_errors() {
    let temp = tempdir().unwrap();
    let config_dir = temp.path().join("config/hippo");
    std::fs::create_dir_all(&config_dir).unwrap();
    let custom_dir = temp.path().join("custom-config");
    let contents = format!("[storage]\nconfig_dir = '{}'\n", custom_dir.display());
    let config_path = config_dir.join("config.toml");
    std::fs::write(&config_path, &contents).unwrap();

    std::fs::write(&custom_dir, "not a directory").unwrap();
    let output = hippo(temp.path())
        .args(["config", "init"])
        .output()
        .unwrap();
    assert!(!output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("config directory"));
    assert_eq!(
        std::fs::read_to_string(&custom_dir).unwrap(),
        "not a directory"
    );

    std::fs::remove_file(&custom_dir).unwrap();
    let output = hippo(temp.path())
        .args(["config", "init"])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        std::fs::read(custom_dir.join("config.toml")).unwrap(),
        include_bytes!("../../../config/config.default.toml")
    );
    assert_eq!(std::fs::read_to_string(&config_path).unwrap(), contents);
}

#[test]
fn init_rejects_invalid_config_without_overwriting_it() {
    let temp = tempdir().unwrap();
    let config_dir = temp.path().join("config/hippo");
    std::fs::create_dir_all(&config_dir).unwrap();
    let config_path = config_dir.join("config.toml");
    std::fs::write(&config_path, "[broken").unwrap();
    let output = hippo(temp.path())
        .args(["config", "init"])
        .output()
        .unwrap();
    assert!(!output.status.success(), "{output:?}");
    assert_eq!(std::fs::read_to_string(&config_path).unwrap(), "[broken");
}

#[test]
fn config_help_lists_init_and_init_help_creates_no_config() {
    let temp = tempdir().unwrap();
    let output = hippo(temp.path())
        .args(["config", "--help"])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stdout).contains("init"));
    let output = hippo(temp.path())
        .args(["config", "init", "--help"])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    assert!(!temp.path().join("config").exists());
}
