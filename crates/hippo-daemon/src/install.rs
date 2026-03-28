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

#[cfg(test)]
mod tests {
    use super::*;

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
