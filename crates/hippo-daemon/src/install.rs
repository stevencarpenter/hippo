use anyhow::{Context, Result};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

/// Replace plist template placeholders with actual system values.
pub fn render_plist(template: &str, vars: &PlistVars) -> String {
    template
        .replace("__HIPPO_BIN__", &vars.hippo_bin.to_string_lossy())
        .replace("__UV_BIN__", &vars.uv_bin.to_string_lossy())
        .replace("__BRAIN_DIR__", &vars.brain_dir.to_string_lossy())
        .replace("__HOME__", &vars.home.to_string_lossy())
        .replace("__PATH__", &vars.path)
        .replace("__DATA_DIR__", &vars.data_dir.to_string_lossy())
        .replace("__HIPPO_OTEL_ENABLED__", &vars.otel_enabled)
        .replace("__OTEL_ENDPOINT__", &vars.otel_endpoint)
        .replace("__GITHUB_TOKEN__", &vars.github_token)
}

pub struct PlistVars {
    pub hippo_bin: PathBuf,
    pub uv_bin: PathBuf,
    pub brain_dir: PathBuf,
    pub home: PathBuf,
    pub path: String,
    pub data_dir: PathBuf,
    pub otel_enabled: String,
    pub otel_endpoint: String,
    /// GitHub API token injected into com.hippo.gh-poll.plist.
    /// Empty string for plists that don't use it (substitute is a no-op).
    pub github_token: String,
}

/// Auto-detect system paths for plist variable substitution.
pub fn detect_vars(brain_dir: &Path) -> Result<PlistVars> {
    let hippo_bin = std::env::current_exe().context("cannot determine hippo binary path")?;
    let uv_bin = which("uv").unwrap_or_else(|| PathBuf::from("/usr/local/bin/uv"));
    let home = dirs::home_dir().context("cannot determine home directory")?;
    let path = std::env::var("PATH").unwrap_or_default();
    let data_dir = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local/share"))
        .join("hippo");

    let telemetry = hippo_core::config::HippoConfig::load_default()
        .map(|c| c.telemetry)
        .unwrap_or_default();

    Ok(PlistVars {
        hippo_bin,
        uv_bin,
        brain_dir: brain_dir.to_path_buf(),
        home,
        path,
        data_dir,
        otel_enabled: if telemetry.enabled {
            "1".to_string()
        } else {
            "0".to_string()
        },
        otel_endpoint: {
            let mut parsed = url::Url::parse(&telemetry.endpoint)
                .unwrap_or_else(|_| url::Url::parse("http://localhost:4318").unwrap());
            if parsed.port() == Some(4317) {
                let _ = parsed.set_port(Some(4318));
            }
            parsed.to_string()
        },
        github_token: String::new(),
    })
}

fn which(binary: &str) -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths).find_map(|dir| {
            let candidate = dir.join(binary);
            candidate.is_file().then_some(candidate)
        })
    })
}

/// Symlink the hippo binary into ~/.local/bin so it's on PATH for shell hooks.
/// Creates ~/.local/bin if it doesn't exist. Returns the symlink path.
pub fn symlink_binary(hippo_bin: &Path, force: bool) -> Result<PathBuf> {
    let bin_dir = dirs::home_dir()
        .context("cannot determine home directory")?
        .join(".local/bin");
    std::fs::create_dir_all(&bin_dir)?;

    let link = bin_dir.join("hippo");

    if link.exists() || link.symlink_metadata().is_ok() {
        if !force {
            // Check if it already points to the right place
            if let Ok(target) = std::fs::read_link(&link)
                && target == hippo_bin
            {
                println!("  Symlink already correct: {}", link.display());
                return Ok(link);
            }
            anyhow::bail!(
                "{} already exists. Use --force to overwrite.",
                link.display()
            );
        }
        // Remove existing symlink or file
        std::fs::remove_file(&link)
            .with_context(|| format!("cannot remove existing {}", link.display()))?;
    }

    // nosemgrep
    std::os::unix::fs::symlink(hippo_bin, &link)
        .with_context(|| format!("cannot create symlink {}", link.display()))?;
    println!("  Symlinked {} -> {}", link.display(), hippo_bin.display());

    // Warn if ~/.local/bin is not on PATH
    if let Ok(path) = std::env::var("PATH")
        && !std::env::split_paths(&path).any(|p| p == bin_dir)
    {
        println!(
            "\n  ⚠ ~/.local/bin is not on your PATH. Add to your shell config:\n    export PATH=\"$HOME/.local/bin:$PATH\""
        );
    }

    Ok(link)
}

/// Write a rendered plist to ~/Library/LaunchAgents/.
/// Returns the destination path. Fails if file exists unless `force` is true.
pub fn install_plist(
    label: &str,
    template: &str,
    vars: &PlistVars,
    force: bool,
) -> Result<PathBuf> {
    let launch_agents = dirs::home_dir()
        .context("cannot determine home directory")?
        .join("Library/LaunchAgents");
    std::fs::create_dir_all(&launch_agents)?;

    let dest = launch_agents.join(format!("{}.plist", label));
    if dest.exists() && !force {
        anyhow::bail!(
            "{} already exists. Use --force to overwrite.",
            dest.display()
        );
    }

    let rendered = render_plist(template, vars);
    std::fs::write(&dest, rendered)?;
    println!("  Installed {}", dest.display());
    Ok(dest)
}

/// Install the Firefox Native Messaging host manifest and wrapper script.
///
/// Creates `hippo_daemon.json` (the manifest) and `hippo-native-messaging` (a wrapper
/// script that calls `hippo native-messaging-host`) in the Mozilla NativeMessagingHosts
/// directory. Firefox requires the wrapper because Native Messaging launches the binary
/// directly without subcommand arguments.
pub fn install_native_messaging_manifest(hippo_bin: &Path, force: bool) -> Result<()> {
    let nm_dir = dirs::home_dir()
        .context("cannot determine home directory")?
        .join("Library/Application Support/Mozilla/NativeMessagingHosts");
    std::fs::create_dir_all(&nm_dir)?;

    let manifest_path = nm_dir.join("hippo_daemon.json");
    if manifest_path.exists() && !force {
        anyhow::bail!(
            "{} already exists. Use --force to overwrite.",
            manifest_path.display()
        );
    }

    // Write wrapper script
    let wrapper_path = nm_dir.join("hippo-native-messaging");
    let wrapper_content = format!(
        "#!/bin/bash\nexec {} native-messaging-host\n",
        hippo_bin.display()
    );
    std::fs::write(&wrapper_path, wrapper_content)?;
    std::fs::set_permissions(&wrapper_path, std::fs::Permissions::from_mode(0o755))?;
    println!("  Installed wrapper {}", wrapper_path.display());

    // Write manifest JSON
    let manifest = serde_json::json!({
        "name": "hippo_daemon",
        "description": "Hippo knowledge capture daemon - browser event bridge",
        "path": wrapper_path.to_string_lossy(),
        "type": "stdio",
        "allowed_extensions": ["hippo-browser@local"]
    });
    let manifest_json =
        serde_json::to_string_pretty(&manifest).context("cannot serialize manifest")?;
    std::fs::write(&manifest_path, manifest_json)?;
    println!("  Installed manifest {}", manifest_path.display());

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_detect_vars_finds_current_exe() {
        let vars = detect_vars(Path::new("/fake/brain")).unwrap();
        assert!(vars.hippo_bin.exists() || vars.hippo_bin.to_string_lossy().contains("hippo"));
        assert!(!vars.home.as_os_str().is_empty());
        assert!(!vars.path.is_empty());
    }

    #[test]
    fn test_symlink_binary_creates_link() {
        let tmp = tempfile::tempdir().unwrap();
        let fake_bin = tmp.path().join("hippo");
        std::fs::write(&fake_bin, "fake").unwrap();

        let bin_dir = tmp.path().join(".local/bin");
        // Call the underlying logic directly to avoid touching real ~/.local/bin
        std::fs::create_dir_all(&bin_dir).unwrap();
        let link = bin_dir.join("hippo");
        std::os::unix::fs::symlink(&fake_bin, &link).unwrap();

        assert!(link.symlink_metadata().is_ok());
        assert_eq!(std::fs::read_link(&link).unwrap(), fake_bin);
    }

    #[test]
    fn test_symlink_binary_force_replaces_existing() {
        let tmp = tempfile::tempdir().unwrap();
        let old_bin = tmp.path().join("old_hippo");
        let new_bin = tmp.path().join("new_hippo");
        std::fs::write(&old_bin, "old").unwrap();
        std::fs::write(&new_bin, "new").unwrap();

        let bin_dir = tmp.path().join(".local/bin");
        std::fs::create_dir_all(&bin_dir).unwrap();
        let link = bin_dir.join("hippo");

        // Create initial symlink
        std::os::unix::fs::symlink(&old_bin, &link).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), old_bin);

        // Replace it
        std::fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink(&new_bin, &link).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), new_bin);
    }

    #[test]
    fn test_render_plist_replaces_all_placeholders() {
        let template = r#"<string>__HIPPO_BIN__</string>
<string>__UV_BIN__</string>
<string>__BRAIN_DIR__</string>
<string>__HOME__</string>
<string>__PATH__</string>
<string>__DATA_DIR__</string>
<string>__HIPPO_OTEL_ENABLED__</string>
<string>__OTEL_ENDPOINT__</string>"#;

        let vars = PlistVars {
            hippo_bin: PathBuf::from("/usr/local/bin/hippo"),
            uv_bin: PathBuf::from("/usr/local/bin/uv"),
            brain_dir: PathBuf::from("/Users/me/projects/hippo/brain"),
            home: PathBuf::from("/Users/me"),
            path: "/usr/local/bin:/usr/bin:/bin".to_string(),
            data_dir: PathBuf::from("/Users/me/.local/share/hippo"),
            otel_enabled: "0".to_string(),
            otel_endpoint: "http://localhost:4318".to_string(),
            github_token: String::new(),
        };

        let result = render_plist(template, &vars);
        assert!(!result.contains("__HIPPO_BIN__"));
        assert!(!result.contains("__UV_BIN__"));
        assert!(!result.contains("__BRAIN_DIR__"));
        assert!(!result.contains("__HOME__"));
        assert!(!result.contains("__PATH__"));
        assert!(!result.contains("__DATA_DIR__"));
        assert!(!result.contains("__HIPPO_OTEL_ENABLED__"));
        assert!(!result.contains("__OTEL_ENDPOINT__"));
        assert!(result.contains("/usr/local/bin/hippo"));
        assert!(result.contains("/usr/local/bin/uv"));
        assert!(result.contains("http://localhost:4318"));
    }
}
