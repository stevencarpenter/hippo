use anyhow::{Context, Result};
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
}

pub struct PlistVars {
    pub hippo_bin: PathBuf,
    pub uv_bin: PathBuf,
    pub brain_dir: PathBuf,
    pub home: PathBuf,
    pub path: String,
    pub data_dir: PathBuf,
}

/// Auto-detect system paths for plist variable substitution.
pub fn detect_vars(brain_dir: &Path) -> Result<PlistVars> {
    let hippo_bin = std::env::current_exe().context("cannot determine hippo binary path")?;
    let uv_bin = which("uv").unwrap_or_else(|| PathBuf::from("/usr/local/bin/uv"));
    let home = dirs::home_dir().context("cannot determine home directory")?;
    let path = std::env::var("PATH").unwrap_or_default();
    let data_dir = dirs::data_local_dir()
        .unwrap_or_else(|| home.join(".local/share"))
        .join("hippo");

    Ok(PlistVars {
        hippo_bin,
        uv_bin,
        brain_dir: brain_dir.to_path_buf(),
        home,
        path,
        data_dir,
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
    fn test_render_plist_replaces_all_placeholders() {
        let template = r#"<string>__HIPPO_BIN__</string>
<string>__UV_BIN__</string>
<string>__BRAIN_DIR__</string>
<string>__HOME__</string>
<string>__PATH__</string>
<string>__DATA_DIR__</string>"#;

        let vars = PlistVars {
            hippo_bin: PathBuf::from("/usr/local/bin/hippo"),
            uv_bin: PathBuf::from("/usr/local/bin/uv"),
            brain_dir: PathBuf::from("/Users/me/projects/hippo/brain"),
            home: PathBuf::from("/Users/me"),
            path: "/usr/local/bin:/usr/bin:/bin".to_string(),
            data_dir: PathBuf::from("/Users/me/.local/share/hippo"),
        };

        let result = render_plist(template, &vars);
        assert!(!result.contains("__HIPPO_BIN__"));
        assert!(!result.contains("__UV_BIN__"));
        assert!(!result.contains("__BRAIN_DIR__"));
        assert!(!result.contains("__HOME__"));
        assert!(!result.contains("__PATH__"));
        assert!(!result.contains("__DATA_DIR__"));
        assert!(result.contains("/usr/local/bin/hippo"));
        assert!(result.contains("/usr/local/bin/uv"));
    }
}
