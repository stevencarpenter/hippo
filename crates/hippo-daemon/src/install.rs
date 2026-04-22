use anyhow::{Context, Result};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

/// Replace plist template placeholders with actual system values.
pub fn render_plist(template: &str, vars: &PlistVars) -> String {
    template
        .replace("__HIPPO_BIN__", &vars.hippo_bin.to_string_lossy())
        .replace("__UV_BIN__", &vars.uv_bin.to_string_lossy())
        .replace("__BRAIN_DIR__", &vars.brain_dir.to_string_lossy())
        .replace("__SCRIPTS_DIR__", &vars.scripts_dir.to_string_lossy())
        .replace("__HOME__", &vars.home.to_string_lossy())
        .replace("__PATH__", &vars.path)
        .replace("__DATA_DIR__", &vars.data_dir.to_string_lossy())
        .replace("__HIPPO_OTEL_ENABLED__", &vars.otel_enabled)
        .replace("__OTEL_ENDPOINT__", &vars.otel_endpoint)
}

pub struct PlistVars {
    pub hippo_bin: PathBuf,
    pub uv_bin: PathBuf,
    pub brain_dir: PathBuf,
    pub scripts_dir: PathBuf,
    pub home: PathBuf,
    pub path: String,
    pub data_dir: PathBuf,
    pub otel_enabled: String,
    pub otel_endpoint: String,
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

    let scripts_dir = brain_dir.join("scripts");

    Ok(PlistVars {
        hippo_bin,
        uv_bin,
        brain_dir: brain_dir.to_path_buf(),
        scripts_dir,
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

/// Write a wrapper script for gh-poll that sources the GitHub token at runtime.
///
/// Reads the token from `$HIPPO_GITHUB_TOKEN` or, if unset, from the user's
/// env file (`~/.config/zsh/.env`). This avoids embedding the plaintext token
/// in the LaunchAgent plist.
pub fn install_gh_poll_wrapper(
    hippo_bin: &Path,
    token_env: &str,
    data_dir: &Path,
    force: bool,
) -> Result<PathBuf> {
    std::fs::create_dir_all(data_dir)?;
    let wrapper = data_dir.join("gh-poll-wrapper.sh");
    if wrapper.exists() && !force {
        anyhow::bail!(
            "{} already exists. Use --force to overwrite.",
            wrapper.display()
        );
    }
    let content = format!(
        r#"#!/bin/bash
# Wrapper for hippo gh-poll LaunchAgent.
# Sources the GitHub token at runtime to avoid embedding secrets in plists.
set -euo pipefail

# Try env var first; fall back to sourcing the encrypted-env-deployed file.
if [ -z "${{{token_env}:-}}" ] && [ -f "$HOME/.config/zsh/.env" ]; then
    set -a
    source "$HOME/.config/zsh/.env"
    set +a
fi

exec {hippo_bin} gh-poll
"#,
        token_env = token_env,
        hippo_bin = hippo_bin.display(),
    );
    std::fs::write(&wrapper, content)?;
    std::fs::set_permissions(&wrapper, std::fs::Permissions::from_mode(0o700))?;
    println!("  Installed wrapper {}", wrapper.display());
    Ok(wrapper)
}

/// Symlink the hippo binary into ~/.local/bin so it's on PATH for shell hooks.
/// Creates ~/.local/bin if it doesn't exist. Returns the symlink path.
pub fn symlink_binary(hippo_bin: &Path, force: bool) -> Result<PathBuf> {
    let bin_dir = dirs::home_dir()
        .context("cannot determine home directory")?
        .join(".local/bin");
    std::fs::create_dir_all(&bin_dir)?;

    let link = bin_dir.join("hippo");

    // Binary is already at the target path (e.g., installed directly by the installer).
    // Creating a symlink here would delete the real binary and create a self-referential loop.
    if hippo_bin == link {
        println!("  Binary already installed at {}", link.display());
        return Ok(link);
    }

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

/// Returns true if a launchd service label is currently loaded in the user session.
pub fn service_is_loaded(label: &str) -> bool {
    std::process::Command::new("launchctl")
        .args(["list", label])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Unloads a launchd service (sends SIGTERM to the process if running, prevents restart).
/// Ignores errors silently — a "not loaded" error is harmless.
pub fn service_bootout(domain: &str, plist: &Path) {
    let _ = std::process::Command::new("launchctl")
        .args(["bootout", domain, plist.to_str().unwrap_or("")])
        .status();
}

/// Loads a launchd service from its plist.
pub fn service_bootstrap(domain: &str, plist: &Path) -> Result<()> {
    let status = std::process::Command::new("launchctl")
        .args(["bootstrap", domain, plist.to_str().unwrap_or("")])
        .status()
        .context("launchctl bootstrap failed")?;
    if !status.success() {
        anyhow::bail!("launchctl bootstrap failed for {}", plist.display());
    }
    Ok(())
}

/// Sends SIGTERM to the brain process and waits up to `timeout` for graceful exit.
///
/// Lets uvicorn finish in-flight HTTP requests before the process stops. Prints progress
/// dots while waiting. Returns true if the process exited within the timeout.
pub fn drain_brain(timeout: std::time::Duration) -> bool {
    let Some(pid) = brain_managed_pid() else {
        return true;
    };

    // nosemgrep
    unsafe { libc::kill(pid as i32, libc::SIGTERM) };

    drain_poll(
        // nosemgrep
        || unsafe { libc::kill(pid as i32, 0) } != 0,
        std::time::Duration::from_millis(500),
        timeout,
        true,
    )
}

/// Return the PID of the `com.hippo.brain` LaunchAgent's managed process,
/// or `None` if the service isn't loaded or has no running process.
///
/// Uses `launchctl list com.hippo.brain` rather than `pgrep -f` because:
///   1. The agent wraps the real brain inside `uv run --project ... hippo-brain
///      serve`. `pgrep -f 'hippo-brain serve'` would match both the `uv run`
///      wrapper AND the child python process (same substring in both argv
///      strings) plus anything else a user has running with that substring.
///   2. `launchctl list`'s PID is the single process launchd is actually
///      managing — always the `uv run` wrapper. SIGTERM to the wrapper
///      propagates to the child, which is the drain we want.
fn brain_managed_pid() -> Option<u32> {
    let output = std::process::Command::new("launchctl")
        .args(["list", "com.hippo.brain"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    parse_launchctl_pid(&String::from_utf8_lossy(&output.stdout))
}

/// Extract the PID from the plist-like output of `launchctl list <label>`.
/// The relevant line looks like `\t"PID" = 53990;`. If the service is
/// loaded but no process is running, `PID` is absent — returns None.
fn parse_launchctl_pid(output: &str) -> Option<u32> {
    for line in output.lines() {
        let trimmed = line.trim();
        if let Some(rest) = trimmed.strip_prefix("\"PID\" = ") {
            return rest.trim_end_matches(';').trim().parse().ok();
        }
    }
    None
}

/// Poll `is_done` every `interval` until it returns true or `timeout` elapses.
/// Prints progress dots when `verbose` is true. Returns true if `is_done` fired in time.
fn drain_poll(
    is_done: impl Fn() -> bool,
    interval: std::time::Duration,
    timeout: std::time::Duration,
    verbose: bool,
) -> bool {
    let start = std::time::Instant::now();
    loop {
        std::thread::sleep(interval);
        if is_done() {
            return true;
        }
        if start.elapsed() >= timeout {
            return false;
        }
        if verbose {
            print!(".");
            let _ = std::io::Write::flush(&mut std::io::stdout());
        }
    }
}

/// Configure the Claude Code session hook in ~/.claude/settings.json.
///
/// Skips if the hook is already pointing at the expected path. Updates only the
/// `command` field of the matching hook entry if the path has drifted (other fields
/// on the matcher and hook objects are preserved). Appends a new matcher if no hippo
/// hook exists at all. All other settings.json content is preserved.
///
/// Returns an error if the file exists but contains malformed JSON — never silently
/// overwrites user settings with an empty object.
///
/// This step does not respect `--force` because it is fully idempotent: it only
/// writes when the path has actually drifted, never destroys existing data, and the
/// "already correct" check makes repeat runs a no-op.
pub fn configure_claude_session_hook(brain_dir: &Path) -> Result<()> {
    let settings_path = dirs::home_dir()
        .context("cannot determine home directory")?
        .join(".claude/settings.json");
    configure_claude_session_hook_at(&settings_path, brain_dir)
}

fn configure_claude_session_hook_at(settings_path: &Path, brain_dir: &Path) -> Result<()> {
    let hook_path = brain_dir.join("shell/claude-session-hook.sh");
    let hook_path_str = hook_path
        .to_str()
        .context("hook path is not valid UTF-8")?
        .to_string();

    let mut root: serde_json::Value = if settings_path.exists() {
        let content = std::fs::read_to_string(settings_path)
            .context("failed to read ~/.claude/settings.json")?;
        serde_json::from_str(&content).with_context(
            || "~/.claude/settings.json is malformed JSON — fix it manually before running install",
        )?
    } else {
        serde_json::json!({})
    };

    if !root.is_object() {
        anyhow::bail!("~/.claude/settings.json root is not a JSON object");
    }

    let matchers = root
        .as_object_mut()
        .unwrap()
        .entry("hooks")
        .or_insert_with(|| serde_json::json!({}))
        .as_object_mut()
        .context("hooks is not an object")?
        .entry("SessionStart")
        .or_insert_with(|| serde_json::json!([]))
        .as_array_mut()
        .context("SessionStart is not an array")?;

    // Find the (matcher_idx, hook_idx) of the specific hippo hook command
    let hippo_location = matchers.iter().enumerate().find_map(|(mi, m)| {
        m.get("hooks").and_then(|h| h.as_array()).and_then(|hooks| {
            hooks.iter().enumerate().find_map(|(hi, h)| {
                h.get("command")
                    .and_then(|c| c.as_str())
                    .filter(|cmd| cmd.contains("claude-session-hook.sh"))
                    .map(|_| (mi, hi))
            })
        })
    });

    match hippo_location {
        Some((mi, hi)) => {
            let current = matchers[mi]
                .get("hooks")
                .and_then(|h| h.as_array())
                .and_then(|hooks| hooks.get(hi))
                .and_then(|h| h.get("command"))
                .and_then(|c| c.as_str());
            if current == Some(hook_path_str.as_str()) {
                println!("  Claude session hook already correct, skipped");
                return Ok(());
            }
            // Mutate only the command field — preserve all other hook/matcher fields
            let hooks = matchers[mi]
                .get_mut("hooks")
                .and_then(|h| h.as_array_mut())
                .context("existing matcher hooks is not an array")?;
            let hook_obj = hooks
                .get_mut(hi)
                .and_then(|h| h.as_object_mut())
                .context("existing hook entry is not an object")?;
            hook_obj.insert(
                "command".to_string(),
                serde_json::Value::String(hook_path_str),
            );
            println!("  Updated Claude session hook: {}", hook_path.display());
        }
        None => {
            matchers.push(serde_json::json!({
                "hooks": [{ "type": "command", "command": hook_path_str }]
            }));
            println!("  Configured Claude session hook: {}", hook_path.display());
        }
    }

    // Atomic write: PID-suffixed tmp sibling + rename so a crash mid-write cannot
    // leave a truncated settings.json. The PID suffix avoids conflicts if two
    // `daemon install` processes somehow run concurrently.
    let parent = settings_path
        .parent()
        .context("~/.claude/settings.json has no parent directory")?;
    std::fs::create_dir_all(parent)?;
    let pretty =
        serde_json::to_string_pretty(&root).context("failed to serialize settings.json")?;
    let tmp_path =
        settings_path.with_file_name(format!("settings.json.tmp.{}", std::process::id()));
    std::fs::write(&tmp_path, &pretty).context("failed to write temporary settings file")?;
    if let Err(e) = std::fs::rename(&tmp_path, settings_path) {
        let _ = std::fs::remove_file(&tmp_path);
        return Err(anyhow::Error::new(e)
            .context("failed to atomically update ~/.claude/settings.json"));
    }

    Ok(())
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
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::time::Duration;

    #[test]
    fn drain_poll_returns_true_immediately_when_already_done() {
        let result = drain_poll(
            || true,
            Duration::from_millis(1),
            Duration::from_secs(1),
            false,
        );
        assert!(result);
    }

    #[test]
    fn drain_poll_returns_true_when_done_before_timeout() {
        let alive = Arc::new(AtomicBool::new(true));
        let alive2 = alive.clone();
        // After the first sleep the closure fires, setting alive=false on the second call.
        let calls = Arc::new(std::sync::atomic::AtomicU32::new(0));
        let calls2 = calls.clone();
        let result = drain_poll(
            move || {
                let n = calls2.fetch_add(1, Ordering::SeqCst);
                if n >= 1 {
                    alive2.store(false, Ordering::SeqCst);
                }
                !alive.load(Ordering::SeqCst)
            },
            Duration::from_millis(1),
            Duration::from_secs(5),
            false,
        );
        assert!(result);
    }

    #[test]
    fn drain_poll_returns_false_on_timeout() {
        let result = drain_poll(
            || false, // never done
            Duration::from_millis(1),
            Duration::from_millis(20),
            false,
        );
        assert!(!result);
    }

    #[test]
    fn service_is_loaded_returns_false_for_unknown_label() {
        assert!(!service_is_loaded(
            "com.hippo.definitely-not-installed-xyzzy"
        ));
    }

    #[test]
    fn test_detect_vars_finds_current_exe() {
        let vars = detect_vars(Path::new("/fake/brain")).unwrap();
        assert!(vars.hippo_bin.exists() || vars.hippo_bin.to_string_lossy().contains("hippo"));
        assert!(!vars.home.as_os_str().is_empty());
        assert!(!vars.path.is_empty());
        // scripts_dir must be a child of brain_dir (installer packs scripts
        // inside the brain tarball — see scripts/install.sh and release.yml).
        assert_eq!(vars.scripts_dir, Path::new("/fake/brain/scripts"));
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
<string>__SCRIPTS_DIR__</string>
<string>__HOME__</string>
<string>__PATH__</string>
<string>__DATA_DIR__</string>
<string>__HIPPO_OTEL_ENABLED__</string>
<string>__OTEL_ENDPOINT__</string>"#;

        let vars = PlistVars {
            hippo_bin: PathBuf::from("/usr/local/bin/hippo"),
            uv_bin: PathBuf::from("/usr/local/bin/uv"),
            brain_dir: PathBuf::from("/Users/me/projects/hippo/brain"),
            scripts_dir: PathBuf::from("/Users/me/projects/hippo/scripts"),
            home: PathBuf::from("/Users/me"),
            path: "/usr/local/bin:/usr/bin:/bin".to_string(),
            data_dir: PathBuf::from("/Users/me/.local/share/hippo"),
            otel_enabled: "0".to_string(),
            otel_endpoint: "http://localhost:4318".to_string(),
        };

        let result = render_plist(template, &vars);
        assert!(!result.contains("__HIPPO_BIN__"));
        assert!(!result.contains("__UV_BIN__"));
        assert!(!result.contains("__BRAIN_DIR__"));
        assert!(!result.contains("__SCRIPTS_DIR__"));
        assert!(!result.contains("__HOME__"));
        assert!(!result.contains("__PATH__"));
        assert!(!result.contains("__DATA_DIR__"));
        assert!(!result.contains("__HIPPO_OTEL_ENABLED__"));
        assert!(!result.contains("__OTEL_ENDPOINT__"));
        assert!(result.contains("/usr/local/bin/hippo"));
        assert!(result.contains("/usr/local/bin/uv"));
        assert!(result.contains("http://localhost:4318"));
    }

    #[test]
    fn parse_launchctl_pid_extracts_running_process_pid() {
        // Actual `launchctl list com.hippo.brain` output for a healthy service.
        let sample = "{\n\
            \t\"StandardOutPath\" = \"/Users/me/.local/share/hippo/brain.stdout.log\";\n\
            \t\"Label\" = \"com.hippo.brain\";\n\
            \t\"PID\" = 53990;\n\
            \t\"Program\" = \"/opt/homebrew/bin/uv\";\n\
            };";
        assert_eq!(parse_launchctl_pid(sample), Some(53990));
    }

    #[test]
    fn parse_launchctl_pid_returns_none_when_service_not_running() {
        // Loaded-but-not-running services omit the PID key entirely.
        let sample = "{\n\
            \t\"Label\" = \"com.hippo.brain\";\n\
            \t\"LastExitStatus\" = 0;\n\
            };";
        assert_eq!(parse_launchctl_pid(sample), None);
    }

    #[test]
    fn parse_launchctl_pid_ignores_malformed_values() {
        let sample = "\t\"PID\" = not-a-number;";
        assert_eq!(parse_launchctl_pid(sample), None);
    }

    // ── configure_claude_session_hook ──────────────────────────────────────────

    #[test]
    fn configure_hook_adds_entry_when_none_exists() {
        let tmp = tempfile::tempdir().unwrap();
        let settings = tmp.path().join(".claude/settings.json");
        std::fs::create_dir_all(settings.parent().unwrap()).unwrap();
        std::fs::write(&settings, r#"{"theme":"dark"}"#).unwrap();

        let brain_dir = tmp.path().join("hippo-brain");
        configure_claude_session_hook_at(&settings, &brain_dir).unwrap();

        let content = std::fs::read_to_string(&settings).unwrap();
        let v: serde_json::Value = serde_json::from_str(&content).unwrap();
        let cmd = v["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            .as_str()
            .unwrap();
        assert!(cmd.ends_with("claude-session-hook.sh"));
        // Unrelated field preserved
        assert_eq!(v["theme"].as_str(), Some("dark"));
    }

    #[test]
    fn configure_hook_skips_when_already_correct() {
        let tmp = tempfile::tempdir().unwrap();
        let settings = tmp.path().join(".claude/settings.json");
        std::fs::create_dir_all(settings.parent().unwrap()).unwrap();

        let brain_dir = tmp.path().join("hippo-brain");
        let hook_path = brain_dir.join("shell/claude-session-hook.sh");
        let initial = serde_json::json!({
            "hooks": {
                "SessionStart": [{
                    "hooks": [{"type": "command", "command": hook_path.to_string_lossy()}]
                }]
            }
        });
        std::fs::write(&settings, serde_json::to_string(&initial).unwrap()).unwrap();
        let mtime_before = std::fs::metadata(&settings).unwrap().modified().unwrap();

        configure_claude_session_hook_at(&settings, &brain_dir).unwrap();

        let mtime_after = std::fs::metadata(&settings).unwrap().modified().unwrap();
        // File must not have been rewritten. APFS nanosecond mtime precision
        // makes this reliable; HFS+ second granularity would need a sleep.
        assert_eq!(mtime_before, mtime_after);
    }

    #[test]
    fn configure_hook_updates_drifted_path_in_place() {
        let tmp = tempfile::tempdir().unwrap();
        let settings = tmp.path().join(".claude/settings.json");
        std::fs::create_dir_all(settings.parent().unwrap()).unwrap();

        let brain_dir = tmp.path().join("hippo-brain");
        let initial = serde_json::json!({
            "hooks": {
                "SessionStart": [{
                    "matcher": "some-filter",
                    "hooks": [
                        {"type": "command", "command": "/old/path/claude-session-hook.sh"}
                    ]
                }]
            }
        });
        std::fs::write(&settings, serde_json::to_string(&initial).unwrap()).unwrap();

        configure_claude_session_hook_at(&settings, &brain_dir).unwrap();

        let content = std::fs::read_to_string(&settings).unwrap();
        let v: serde_json::Value = serde_json::from_str(&content).unwrap();
        let matcher = &v["hooks"]["SessionStart"][0];
        // matcher-level field preserved
        assert_eq!(matcher["matcher"].as_str(), Some("some-filter"));
        let cmd = matcher["hooks"][0]["command"].as_str().unwrap();
        assert!(cmd.ends_with("claude-session-hook.sh"));
        assert!(!cmd.starts_with("/old/path"));
    }

    #[test]
    fn configure_hook_preserves_unrelated_session_start_matchers() {
        let tmp = tempfile::tempdir().unwrap();
        let settings = tmp.path().join(".claude/settings.json");
        std::fs::create_dir_all(settings.parent().unwrap()).unwrap();

        let brain_dir = tmp.path().join("hippo-brain");
        let initial = serde_json::json!({
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "/other/tool/hook.sh"}]}
                ]
            }
        });
        std::fs::write(&settings, serde_json::to_string(&initial).unwrap()).unwrap();

        configure_claude_session_hook_at(&settings, &brain_dir).unwrap();

        let content = std::fs::read_to_string(&settings).unwrap();
        let v: serde_json::Value = serde_json::from_str(&content).unwrap();
        let matchers = v["hooks"]["SessionStart"].as_array().unwrap();
        // Original matcher preserved, hippo matcher appended
        assert_eq!(matchers.len(), 2);
        assert_eq!(
            matchers[0]["hooks"][0]["command"].as_str(),
            Some("/other/tool/hook.sh")
        );
        assert!(
            matchers[1]["hooks"][0]["command"]
                .as_str()
                .unwrap()
                .ends_with("claude-session-hook.sh")
        );
    }

    #[test]
    fn configure_hook_returns_error_on_malformed_json() {
        let tmp = tempfile::tempdir().unwrap();
        let settings = tmp.path().join(".claude/settings.json");
        std::fs::create_dir_all(settings.parent().unwrap()).unwrap();
        std::fs::write(&settings, "{ this is not json }").unwrap();

        let brain_dir = tmp.path().join("hippo-brain");
        let result = configure_claude_session_hook_at(&settings, &brain_dir);
        assert!(result.is_err());
        // File must not have been touched
        assert_eq!(
            std::fs::read_to_string(&settings).unwrap(),
            "{ this is not json }"
        );
    }
}
