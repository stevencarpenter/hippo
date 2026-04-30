# Service Lifecycle Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 5 code issues identified in `docs/smoke-test-and-risk-assessment.md` Part 6 — LaunchAgent installer, toml semver warning, daemon stop verification, brain stop command, and daemon restart behavior.

**Architecture:** Three independent work streams that can run in parallel. Agent A handles the LaunchAgent installer (new template substitution + file copy logic). Agent B handles daemon stop/restart lifecycle (polling confirmation + restart-as-instructions). Agent C handles the toml dependency fix and adding a brain subcommand to the CLI.

**Tech Stack:** Rust (clap CLI, tokio async, std::fs), Python (uvicorn/starlette), macOS launchd

---

## File Map

| File | Agent | Action | Responsibility |
|------|-------|--------|----------------|
| `Cargo.toml` | C | Modify | Fix toml version semver metadata |
| `crates/hippo-daemon/src/cli.rs` | A+C | Modify | Add `Install --force`, add `Brain` subcommand |
| `crates/hippo-daemon/src/main.rs` | A+B+C | Modify | Wire up install logic, stop polling, restart behavior, brain stop |
| `crates/hippo-daemon/src/commands.rs` | B | Modify | Add `poll_socket_removal()` helper |
| `crates/hippo-daemon/src/install.rs` | A | Create | Template substitution + plist copy logic |

**Merge order:** Agent C (toml fix) has no conflicts — can merge first. Agent A (install.rs is new file, cli.rs changes are additive) and Agent B (touches main.rs stop/restart blocks and commands.rs) have minimal overlap. Agent A and B touch different sections of `main.rs` (Install vs Stop/Restart) so they can work in parallel.

---

## Agent A: LaunchAgent Installer (Issue 1)

### Task A1: Create install module with template substitution

**Files:**
- Create: `crates/hippo-daemon/src/install.rs`
- Modify: `crates/hippo-daemon/src/main.rs:1` (add `mod install;`)

- [ ] **Step A1.1: Write failing test for template substitution**

Create `crates/hippo-daemon/src/install.rs` with:

```rust
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
```

- [ ] **Step A1.2: Add `mod install;` to main.rs**

At the top of `crates/hippo-daemon/src/main.rs`, add `mod install;` alongside the other module declarations:

```rust
mod cli;
mod commands;
mod daemon;
mod framing;
mod install;
```

- [ ] **Step A1.3: Run test to verify it passes**

Run: `cargo test -p hippo-daemon install -- --nocapture`
Expected: PASS — `test_render_plist_replaces_all_placeholders`

- [ ] **Step A1.4: Commit**

```bash
git add crates/hippo-daemon/src/install.rs crates/hippo-daemon/src/main.rs
git commit -m "feat: add plist template rendering for LaunchAgent install"
```

### Task A2: Add path detection and plist installation logic

**Files:**
- Modify: `crates/hippo-daemon/src/install.rs`

- [ ] **Step A2.1: Write test for path detection**

Add to install.rs tests:

```rust
    #[test]
    fn test_detect_vars_finds_current_exe() {
        // detect_vars should at minimum resolve the current exe
        let vars = detect_vars(Path::new("/fake/brain")).unwrap();
        assert!(vars.hippo_bin.exists() || vars.hippo_bin.to_string_lossy().contains("hippo"));
        assert!(!vars.home.as_os_str().is_empty());
        assert!(!vars.path.is_empty());
    }
```

- [ ] **Step A2.2: Implement `detect_vars` and `install_plist`**

Add to install.rs above the tests module:

```rust
/// Auto-detect system paths for plist variable substitution.
pub fn detect_vars(brain_dir: &Path) -> Result<PlistVars> {
    let hippo_bin = std::env::current_exe().context("cannot determine hippo binary path")?;
    let uv_bin = which("uv").unwrap_or_else(|| PathBuf::from("/usr/local/bin/uv"));
    let home = dirs::home_dir().context("cannot determine home directory")?;
    let path = std::env::var("PATH").unwrap_or_default();
    let data_dir = dirs::data_local_dir()
        .unwrap_or_else(|| PathBuf::from("~/.local/share"))
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
```

- [ ] **Step A2.3: Run tests**

Run: `cargo test -p hippo-daemon install -- --nocapture`
Expected: PASS — both tests pass

- [ ] **Step A2.4: Commit**

```bash
git add crates/hippo-daemon/src/install.rs
git commit -m "feat: add path detection and plist file installation"
```

### Task A3: Wire install command into CLI

**Files:**
- Modify: `crates/hippo-daemon/src/cli.rs:92` (add `--force` flag to Install)
- Modify: `crates/hippo-daemon/src/main.rs:54-56` (replace print stub with real logic)

- [ ] **Step A3.1: Add `--force` flag to Install variant**

In `crates/hippo-daemon/src/cli.rs`, change the `Install` variant:

```rust
    /// Install LaunchAgents for daemon and brain
    Install {
        /// Overwrite existing plist files
        #[arg(long)]
        force: bool,
    },
```

- [ ] **Step A3.2: Update main.rs Install handler**

In `crates/hippo-daemon/src/main.rs`, replace the `DaemonAction::Install` match arm with:

```rust
            DaemonAction::Install { force } => {
                let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
                let project_root = manifest_dir
                    .parent()
                    .and_then(|p| p.parent())
                    .expect("cannot determine project root");
                let brain_dir = project_root.join("brain");

                let vars = install::detect_vars(&brain_dir)?;

                println!("Installing LaunchAgents...");
                println!("  hippo binary: {}", vars.hippo_bin.display());
                println!("  uv binary:    {}", vars.uv_bin.display());
                println!("  brain dir:    {}", vars.brain_dir.display());
                println!("  data dir:     {}", vars.data_dir.display());
                println!();

                let daemon_template =
                    include_str!("../../../launchd/com.hippo.daemon.plist");
                let brain_template =
                    include_str!("../../../launchd/com.hippo.brain.plist");

                install::install_plist(
                    "com.hippo.daemon",
                    daemon_template,
                    &vars,
                    force,
                )?;
                install::install_plist(
                    "com.hippo.brain",
                    brain_template,
                    &vars,
                    force,
                )?;

                println!();
                println!("Load with:");
                println!("  launchctl load ~/Library/LaunchAgents/com.hippo.daemon.plist");
                println!("  launchctl load ~/Library/LaunchAgents/com.hippo.brain.plist");
            }
```

Add `use std::path::PathBuf;` to the imports at the top of main.rs if not already present.

- [ ] **Step A3.3: Build and verify**

Run: `cargo build -p hippo-daemon 2>&1`
Expected: Compiles with no errors.

Run: `cargo run --bin hippo -- daemon install --help 2>&1`
Expected: Shows `--force` flag in help output.

- [ ] **Step A3.4: Run all daemon tests**

Run: `cargo test -p hippo-daemon`
Expected: All tests pass.

- [ ] **Step A3.5: Commit**

```bash
git add crates/hippo-daemon/src/cli.rs crates/hippo-daemon/src/main.rs
git commit -m "feat: wire LaunchAgent install with template substitution"
```

---

## Agent B: Daemon Stop & Restart Lifecycle (Issues 3 + 5)

### Task B1: Add socket polling to daemon stop

**Files:**
- Modify: `crates/hippo-daemon/src/main.rs:31-37` (Stop handler)

- [ ] **Step B1.1: Implement polling in the Stop handler**

In `crates/hippo-daemon/src/main.rs`, replace the `DaemonAction::Stop` match arm with:

```rust
            DaemonAction::Stop => {
                let socket = config.socket_path();
                match commands::send_request(
                    &socket,
                    &hippo_core::protocol::DaemonRequest::Shutdown,
                )
                .await
                {
                    Ok(_) => {
                        print!("Shutdown signal sent. Waiting for daemon to exit");
                        let deadline =
                            tokio::time::Instant::now() + std::time::Duration::from_secs(5);
                        loop {
                            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
                            if !socket.exists() {
                                println!(" done.");
                                break;
                            }
                            if tokio::time::Instant::now() >= deadline {
                                println!(
                                    " timed out.\nSocket still exists at {}. \
                                     The daemon may still be shutting down, or you may need: \
                                     pkill -9 -f 'hippo.*daemon'",
                                    socket.display()
                                );
                                break;
                            }
                            print!(".");
                            use std::io::Write;
                            std::io::stdout().flush().ok();
                        }
                    }
                    Err(_) => {
                        if socket.exists() {
                            println!(
                                "Could not connect to daemon, but socket exists at {}.\n\
                                 The daemon may have crashed. Cleaning up stale socket.",
                                socket.display()
                            );
                            std::fs::remove_file(&socket).ok();
                        } else {
                            println!("Daemon is not running (no socket found).");
                        }
                    }
                }
            }
```

- [ ] **Step B1.2: Build and verify**

Run: `cargo build -p hippo-daemon 2>&1`
Expected: Compiles with no errors.

- [ ] **Step B1.3: Manual test (no daemon running)**

Run: `cargo run --bin hippo -- daemon stop 2>&1`
Expected: Prints "Daemon is not running (no socket found)."

- [ ] **Step B1.4: Run all tests**

Run: `cargo test -p hippo-daemon`
Expected: All tests pass.

- [ ] **Step B1.5: Commit**

```bash
git add crates/hippo-daemon/src/main.rs
git commit -m "fix: daemon stop now polls for confirmation and handles stale sockets"
```

### Task B2: Fix daemon restart to not block

**Files:**
- Modify: `crates/hippo-daemon/src/main.rs:39-53` (Restart handler)

- [ ] **Step B2.1: Replace blocking restart with stop + instructions**

In `crates/hippo-daemon/src/main.rs`, replace the `DaemonAction::Restart` match arm with:

```rust
            DaemonAction::Restart => {
                let socket = config.socket_path();
                if socket.exists() {
                    let _ = commands::send_request(
                        &socket,
                        &hippo_core::protocol::DaemonRequest::Shutdown,
                    )
                    .await;

                    // Poll for shutdown (same logic as Stop, shorter timeout)
                    let deadline =
                        tokio::time::Instant::now() + std::time::Duration::from_secs(3);
                    loop {
                        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
                        if !socket.exists() {
                            break;
                        }
                        if tokio::time::Instant::now() >= deadline {
                            eprintln!(
                                "Warning: daemon did not stop within 3s. \
                                 You may need: pkill -9 -f 'hippo.*daemon'"
                            );
                            std::process::exit(1);
                        }
                    }
                }

                println!("Starting daemon...");
                tracing_subscriber::fmt()
                    .with_env_filter(
                        EnvFilter::try_from_default_env()
                            .unwrap_or_else(|_| EnvFilter::new("info")),
                    )
                    .init();
                daemon::run(config).await?;
            }
```

This keeps the restart-in-place behavior (which is correct for foreground use and launchd) but adds proper shutdown verification with a timeout instead of a blind 1-second sleep.

- [ ] **Step B2.2: Build and verify**

Run: `cargo build -p hippo-daemon 2>&1`
Expected: Compiles with no errors.

- [ ] **Step B2.3: Run all tests**

Run: `cargo test -p hippo-daemon`
Expected: All tests pass.

- [ ] **Step B2.4: Commit**

```bash
git add crates/hippo-daemon/src/main.rs
git commit -m "fix: daemon restart verifies shutdown before restarting"
```

---

## Agent C: Toml Fix + Brain Stop Command (Issues 2 + 4)

### Task C1: Fix toml semver metadata warning

**Files:**
- Modify: `Cargo.toml:21`

- [ ] **Step C1.1: Remove semver metadata from toml dependency**

In the workspace `Cargo.toml`, change line 21:

From: `toml = "1.1.0+spec-1.1.0"`
To: `toml = "1.1.0"`

- [ ] **Step C1.2: Build and verify warning is gone**

Run: `cargo build 2>&1 | grep -c "semver metadata"`
Expected: `0` (no semver warning)

Run: `cargo build 2>&1`
Expected: Compiles successfully with no warnings about toml.

- [ ] **Step C1.3: Commit**

```bash
git add Cargo.toml
git commit -m "fix: remove semver metadata from toml dependency version"
```

### Task C2: Add brain subcommand with stop action

**Files:**
- Modify: `crates/hippo-daemon/src/cli.rs` (add `Brain` subcommand)
- Modify: `crates/hippo-daemon/src/main.rs` (wire up brain stop)

- [ ] **Step C2.1: Add Brain subcommand to CLI**

In `crates/hippo-daemon/src/cli.rs`, add after the `DaemonAction` enum:

```rust
#[derive(Subcommand)]
pub enum BrainAction {
    /// Stop the brain server (sends SIGTERM)
    Stop,
}
```

And add to the `Commands` enum, after the `Daemon` variant:

```rust
    /// Brain server management
    Brain {
        #[command(subcommand)]
        action: BrainAction,
    },
```

- [ ] **Step C2.2: Wire up brain stop in main.rs**

In `crates/hippo-daemon/src/main.rs`, add the import of `BrainAction` to the use statement:

```rust
use cli::{BrainAction, Cli, Commands, ConfigAction, DaemonAction, RedactAction, SendEventSource};
```

Add a new match arm in the `match cli.command` block, after the `Daemon` block:

```rust
        Commands::Brain { action } => match action {
            BrainAction::Stop => {
                let output = std::process::Command::new("pkill")
                    .args(["-f", "hippo-brain serve"])
                    .output();
                match output {
                    Ok(o) if o.status.success() => {
                        println!("Sent SIGTERM to brain server.");
                    }
                    _ => {
                        println!("No brain server process found.");
                    }
                }
            }
        },
```

- [ ] **Step C2.3: Build and verify**

Run: `cargo build -p hippo-daemon 2>&1`
Expected: Compiles with no errors.

Run: `cargo run --bin hippo -- brain --help 2>&1`
Expected: Shows `stop` subcommand.

Run: `cargo run --bin hippo -- brain stop 2>&1`
Expected: "No brain server process found." (since brain isn't running)

- [ ] **Step C2.4: Run all tests**

Run: `cargo test -p hippo-daemon`
Expected: All tests pass.

- [ ] **Step C2.5: Commit**

```bash
git add crates/hippo-daemon/src/cli.rs crates/hippo-daemon/src/main.rs
git commit -m "feat: add 'hippo brain stop' command to send SIGTERM to brain server"
```

### Task C3: Update nuke task to include brain stop

**Files:**
- Modify: `mise.toml` (update nuke task)

- [ ] **Step C3.1: Add hippo brain stop to nuke**

In `mise.toml`, in the nuke task's run script, add a graceful stop attempt before the SIGKILL lines. Replace the `echo "Killing all hippo processes (SIGKILL)..."` section with:

```
echo "Attempting graceful brain shutdown..."
cargo run --bin hippo -- brain stop 2>/dev/null || true

echo "Killing all hippo processes (SIGKILL)..."
```

The rest of the nuke task remains unchanged (the SIGKILL lines are the fallback).

- [ ] **Step C3.2: Verify task runs**

Run: `mise run nuke 2>&1`
Expected: Shows graceful attempt, then SIGKILL fallback, all succeeds.

- [ ] **Step C3.3: Commit**

```bash
git add mise.toml
git commit -m "chore: nuke task attempts graceful brain stop before SIGKILL"
```

---

## Merge Sequence

All three agents work in parallel. Merge order:

1. **Agent C, Task C1** (toml fix) — standalone one-liner, merge first
2. **Agent A** (install.rs) — new file + additive CLI changes, no conflicts
3. **Agent B** (stop/restart) — touches main.rs Stop/Restart blocks
4. **Agent C, Tasks C2-C3** (brain stop) — additive CLI + main.rs changes

If Agent B and Agent C's main.rs changes overlap, the later merge just needs to resolve the import line (`use cli::{...}`) which both modify.

## Verification (after all merges)

```bash
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo build 2>&1 | grep -c "semver metadata"  # should be 0
cargo run --bin hippo -- daemon install --help  # should show --force
cargo run --bin hippo -- daemon stop            # should show "not running"
cargo run --bin hippo -- brain stop             # should show "no process found"
cargo run --bin hippo -- brain --help           # should show stop subcommand
```
