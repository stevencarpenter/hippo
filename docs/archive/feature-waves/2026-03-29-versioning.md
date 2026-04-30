# Versioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Embed build-time version strings (semver + git metadata) into both the Rust daemon and Python brain so `hippo --version` and `hippo doctor` report exactly what code is running.

**Architecture:** A custom `build.rs` in hippo-daemon composes a version string from `CARGO_PKG_VERSION` + `git describe` output. A `mise run stamp:version` task writes the same string into a Python `_version.py` file. The brain's `/health` endpoint exposes its version, and `hippo doctor` compares both.

**Tech Stack:** Rust build.rs (std::process::Command for git), Python importlib.metadata, mise tasks, clap env! macro

**Spec:** `docs/superpowers/specs/2026-03-29-versioning-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `crates/hippo-daemon/build.rs` | Create | Run git describe, emit `HIPPO_VERSION_FULL` env var |
| `crates/hippo-daemon/src/cli.rs` | Modify | Wire `HIPPO_VERSION_FULL` into clap `#[command(version)]` |
| `brain/src/hippo_brain/version.py` | Create | `get_version()` reads `_version.py` with fallback |
| `brain/src/hippo_brain/_version.py` | Generated | Stamped at build time (gitignored) |
| `brain/src/hippo_brain/server.py` | Modify | Add `"version"` to `/health` JSON response |
| `brain/tests/test_server.py` | Modify | Assert `"version"` in health response |
| `crates/hippo-daemon/src/commands.rs` | Modify | Add version display + brain version comparison to doctor |
| `mise.toml` | Modify | Add `stamp:version` task, wire into install flow |
| `.gitignore` | Modify | Add `brain/src/hippo_brain/_version.py` |

---

### Task 1: Rust build.rs — version string composition

**Files:**
- Create: `crates/hippo-daemon/build.rs`

- [ ] **Step 1: Create `build.rs` with git describe logic**

```rust
// crates/hippo-daemon/build.rs
use std::process::Command;

fn main() {
    // Base version from Cargo.toml workspace (set by Cargo as env var for build scripts)
    let base = std::env::var("CARGO_PKG_VERSION").unwrap();

    let version = git_describe_version(&base);
    println!("cargo:rustc-env=HIPPO_VERSION_FULL={version}");

    // Rebuild when git state changes
    println!("cargo:rerun-if-changed=../../.git/HEAD");
    println!("cargo:rerun-if-changed=../../.git/refs/");
}

fn git_describe_version(base: &str) -> String {
    let describe = Command::new("git")
        .args(["describe", "--tags", "--always", "--dirty", "--match", "v*"])
        .output();

    let Ok(output) = describe else {
        return format!("{base}-dev+unknown");
    };

    if !output.status.success() {
        return format!("{base}-dev+unknown");
    }

    let raw = String::from_utf8_lossy(&output.stdout).trim().to_string();

    // Exactly at a tag: "v0.2.0" or "v0.2.0-dirty"
    if raw == format!("v{base}") {
        return base.to_string();
    }
    if raw == format!("v{base}-dirty") {
        return format!("{base}+dirty");
    }

    // After a tag: "v0.2.0-3-g63ea88d" or "v0.2.0-3-g63ea88d-dirty"
    // No tags: just a short hash "63ea88d" or "63ea88d-dirty"
    if raw.starts_with("v") {
        // Parse: v{tag}-{count}-g{sha}[-dirty]
        let dirty = raw.ends_with("-dirty");
        let clean = if dirty {
            raw.trim_end_matches("-dirty")
        } else {
            &raw
        };
        let parts: Vec<&str> = clean.splitn(4, '-').collect();
        if parts.len() >= 3 {
            let count = parts[1];
            let sha = parts[2]; // already has "g" prefix
            let dirty_suffix = if dirty { ".dirty" } else { "" };
            return format!("{base}-dev.{count}+{sha}{dirty_suffix}");
        }
    }

    // Fallback: no tags, raw is just a sha or sha-dirty
    let dirty = raw.ends_with("-dirty");
    let sha = if dirty {
        raw.trim_end_matches("-dirty")
    } else {
        &raw
    };
    let dirty_suffix = if dirty { ".dirty" } else { "" };
    format!("{base}-dev+g{sha}{dirty_suffix}")
}
```

- [ ] **Step 2: Build and verify the version string**

Run:
```bash
cd ~/projects/hippo && cargo build -p hippo-daemon 2>&1 | tail -5
```
Expected: Build succeeds with no errors.

Then verify:
```bash
./target/debug/hippo --version
```
Expected: `hippo 0.1.0-dev+g63ea88d` (or similar — a dev string with the current SHA, since no tags exist yet).

- [ ] **Step 3: Commit**

```bash
git add crates/hippo-daemon/build.rs
git commit -m "feat(version): add build.rs to embed git metadata in version string"
```

---

### Task 2: Wire version into clap CLI

**Files:**
- Modify: `crates/hippo-daemon/src/cli.rs:4`

- [ ] **Step 1: Update the clap command attribute**

In `crates/hippo-daemon/src/cli.rs`, change line 4 from:

```rust
#[command(name = "hippo", version, about = "Local knowledge capture daemon")]
```

to:

```rust
#[command(name = "hippo", version = env!("HIPPO_VERSION_FULL"), about = "Local knowledge capture daemon")]
```

- [ ] **Step 2: Build and verify**

Run:
```bash
cd ~/projects/hippo && cargo build -p hippo-daemon && ./target/debug/hippo --version
```
Expected: `hippo 0.1.0-dev+g<sha>` (the full version string with git metadata).

- [ ] **Step 3: Commit**

```bash
git add crates/hippo-daemon/src/cli.rs
git commit -m "feat(version): wire HIPPO_VERSION_FULL into clap --version output"
```

---

### Task 3: Python version module

**Files:**
- Create: `brain/src/hippo_brain/version.py`
- Modify: `.gitignore`

- [ ] **Step 1: Add `_version.py` to `.gitignore`**

Add this line to the top of `.gitignore` (after line 1 `/target`):

```
brain/src/hippo_brain/_version.py
```

- [ ] **Step 2: Create `version.py` with fallback logic**

```python
# brain/src/hippo_brain/version.py
"""Build-time version with fallback to package metadata."""

from importlib.metadata import version as _pkg_version


def get_version() -> str:
    """Return the full version string (e.g. '0.2.0-dev.3+g63ea88d').

    Reads from the build-stamped _version.py first. Falls back to the
    static version in pyproject.toml via importlib.metadata.
    """
    try:
        from hippo_brain._version import __version__

        return __version__
    except ImportError:
        return _pkg_version("hippo-brain")
```

- [ ] **Step 3: Verify the fallback path works**

Run:
```bash
cd ~/projects/hippo && uv run --project brain python -c "from hippo_brain.version import get_version; print(get_version())"
```
Expected: `0.1.0` (falls back to pyproject.toml since `_version.py` doesn't exist yet).

- [ ] **Step 4: Commit**

```bash
git add .gitignore brain/src/hippo_brain/version.py
git commit -m "feat(version): add Python version module with _version.py fallback"
```

---

### Task 4: mise `stamp:version` task

**Files:**
- Modify: `mise.toml`

- [ ] **Step 1: Add the `stamp:version` task to `mise.toml`**

Insert this new task block after the `[tasks."build:all"]` section (after line 24) and before the `# ── Test` comment (line 26):

```toml
[tasks."stamp:version"]
description = "Stamp brain _version.py with git metadata (matches Rust build.rs)"
run = """
#!/usr/bin/env bash
set -euo pipefail

BASE_VERSION=$(grep '^version' Cargo.toml | head -1 | cut -d'"' -f2)
RAW=$(git describe --tags --always --dirty --match 'v*' 2>/dev/null || echo "unknown")

if [ "$RAW" = "v${BASE_VERSION}" ]; then
    FULL_VERSION="$BASE_VERSION"
elif [ "$RAW" = "v${BASE_VERSION}-dirty" ]; then
    FULL_VERSION="${BASE_VERSION}+dirty"
elif [[ "$RAW" == v* ]]; then
    DIRTY=""
    CLEAN="$RAW"
    if [[ "$RAW" == *-dirty ]]; then
        DIRTY=".dirty"
        CLEAN="${RAW%-dirty}"
    fi
    # v0.1.0-3-g63ea88d -> count=3, sha=g63ea88d
    COUNT=$(echo "$CLEAN" | sed 's/v[^-]*-\\([0-9]*\\)-.*/\\1/')
    SHA=$(echo "$CLEAN" | sed 's/v[^-]*-[0-9]*-//')
    FULL_VERSION="${BASE_VERSION}-dev.${COUNT}+${SHA}${DIRTY}"
else
    DIRTY=""
    SHA="$RAW"
    if [[ "$RAW" == *-dirty ]]; then
        DIRTY=".dirty"
        SHA="${RAW%-dirty}"
    fi
    FULL_VERSION="${BASE_VERSION}-dev+g${SHA}${DIRTY}"
fi

mkdir -p brain/src/hippo_brain
echo "__version__ = \\"${FULL_VERSION}\\"" > brain/src/hippo_brain/_version.py
echo "Stamped brain version: ${FULL_VERSION}"
"""
```

- [ ] **Step 2: Run the stamp task and verify**

Run:
```bash
cd ~/projects/hippo && mise run stamp:version
```
Expected output: `Stamped brain version: 0.1.0-dev+g<sha>` (or similar).

Then verify the file:
```bash
cat brain/src/hippo_brain/_version.py
```
Expected: `__version__ = "0.1.0-dev+g<sha>"`

Then verify Python reads it:
```bash
uv run --project brain python -c "from hippo_brain.version import get_version; print(get_version())"
```
Expected: `0.1.0-dev+g<sha>` (now reads from `_version.py` instead of fallback).

- [ ] **Step 3: Wire stamp:version into build:brain and install**

Update the `build:brain` task to depend on `stamp:version`. Change:

```toml
[tasks."build:brain"]
description = "Sync Python brain dependencies"
run = "uv sync --project brain"
```

to:

```toml
[tasks."build:brain"]
description = "Sync Python brain dependencies"
depends = ["stamp:version"]
run = "uv sync --project brain"
```

Then in the `[tasks.install]` script, add a new step between step 3 (cargo build) and step 4 (uv sync). After the line `cargo build --release`, add:

```bash

# ── 3b. Stamp brain version ───────────────────────────────────────
echo "==> Stamping brain version..."
mise run stamp:version
```

- [ ] **Step 4: Commit**

```bash
git add mise.toml
git commit -m "feat(version): add mise stamp:version task, wire into build and install"
```

---

### Task 5: Brain `/health` version field

**Files:**
- Modify: `brain/src/hippo_brain/server.py:100-111`
- Modify: `brain/tests/test_server.py:74-97`

- [ ] **Step 1: Update the health test to expect a `version` field**

In `brain/tests/test_server.py`, add a new import and assertion. At the top of the file (after line 10, the existing imports), add:

```python
from hippo_brain.version import get_version
```

In `test_health_endpoint` (around line 82), after `assert data["status"] == "ok"`, add:

```python
    assert "version" in data
    assert data["version"] == get_version()
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
cd ~/projects/hippo && uv run --project brain pytest brain/tests/test_server.py::test_health_endpoint -v
```
Expected: FAIL with `KeyError: 'version'` or `AssertionError`.

- [ ] **Step 3: Add version to the health response**

In `brain/src/hippo_brain/server.py`, add an import at the top of the file (after line 1 or with the other imports):

```python
from hippo_brain.version import get_version
```

Then in the `health()` method, add `"version"` to the response dict. Change the return block (lines 100-112) from:

```python
        return JSONResponse(
            {
                "status": "ok" if db_reachable else "degraded",
                "lmstudio_reachable": reachable,
```

to:

```python
        return JSONResponse(
            {
                "status": "ok" if db_reachable else "degraded",
                "version": get_version(),
                "lmstudio_reachable": reachable,
```

The rest of the dict stays the same.

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
cd ~/projects/hippo && uv run --project brain pytest brain/tests/test_server.py::test_health_endpoint -v
```
Expected: PASS.

- [ ] **Step 5: Run the full Python test suite**

Run:
```bash
cd ~/projects/hippo && uv run --project brain pytest brain/tests -v
```
Expected: All tests pass. If any other test that hits `/health` fails because it doesn't expect the `version` field, note that the existing tests only check for the presence of specific keys (not the absence of others), so they should pass.

- [ ] **Step 6: Commit**

```bash
git add brain/src/hippo_brain/server.py brain/tests/test_server.py
git commit -m "feat(version): add version field to brain /health endpoint"
```

---

### Task 6: Doctor version display and comparison

**Files:**
- Modify: `crates/hippo-daemon/src/commands.rs:132-204` (`print_brain_health_details`)
- Modify: `crates/hippo-daemon/src/commands.rs:485-488` (`handle_doctor`)

- [ ] **Step 1: Add daemon version line to `handle_doctor`**

In `crates/hippo-daemon/src/commands.rs`, in `handle_doctor()` (line 485), add the version line right after the header. Change:

```rust
pub async fn handle_doctor(config: &HippoConfig) -> Result<()> {
    println!("Hippo Doctor");
    println!("============");
```

to:

```rust
pub async fn handle_doctor(config: &HippoConfig) -> Result<()> {
    println!("Hippo Doctor");
    println!("============");
    println!("[OK] Daemon version: {}", env!("HIPPO_VERSION_FULL"));
```

- [ ] **Step 2: Add brain version comparison to `print_brain_health_details`**

In `print_brain_health_details()`, after the existing brain health field parsing (after the `last_error` extraction around line 168), add the version extraction and comparison. After this block:

```rust
                    let last_error = json
                        .get("last_error")
                        .and_then(|v| v.as_str())
                        .filter(|s| !s.is_empty())
                        .map(|s| s.to_string());
```

Add:

```rust
                    let brain_version = json
                        .get("version")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown");
                    let daemon_version = env!("HIPPO_VERSION_FULL");
                    if brain_version == daemon_version {
                        println!("[OK] Brain version match");
                    } else {
                        println!(
                            "[!!] Brain version mismatch: brain={}, daemon={}",
                            brain_version, daemon_version
                        );
                    }
```

- [ ] **Step 3: Update the Rust doctor test**

In the same file, the test `test_doctor_reports_brain_health_details_from_json` (line 686) uses a hardcoded JSON body. Add `"version"` to the test's mock JSON. Change the `body` variable (line 695) from:

```rust
            let body = r#"{"status":"ok","lmstudio_reachable":true,"enrichment_running":true,"db_reachable":true,"queue_depth":3,"queue_failed":1,"last_success_at_ms":123456,"last_error":"model offline"}"#;
```

to:

```rust
            let body = format!(
                r#"{{"status":"ok","version":"{}","lmstudio_reachable":true,"enrichment_running":true,"db_reachable":true,"queue_depth":3,"queue_failed":1,"last_success_at_ms":123456,"last_error":"model offline"}}"#,
                env!("HIPPO_VERSION_FULL")
            );
```

This ensures the mock brain reports the same version as the daemon, so the test validates the "match" path.

- [ ] **Step 4: Build and run the Rust tests**

Run:
```bash
cd ~/projects/hippo && cargo test -p hippo-daemon -- doctor
```
Expected: `test_doctor_reports_brain_health_details_from_json` passes.

- [ ] **Step 5: Run the full Rust test suite**

Run:
```bash
cd ~/projects/hippo && cargo test
```
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-daemon/src/commands.rs
git commit -m "feat(version): add daemon version to doctor, compare with brain version"
```

---

### Task 7: Create initial git tag and end-to-end verification

**Files:** None (verification only)

- [ ] **Step 1: Run clippy and format checks**

Run:
```bash
cd ~/projects/hippo && cargo clippy --all-targets -- -D warnings && cargo fmt --check
```
Expected: No warnings or format issues.

- [ ] **Step 2: Run Python lint checks**

Run:
```bash
cd ~/projects/hippo && uv run --project brain ruff check brain/ && uv run --project brain ruff format --check brain/
```
Expected: No lint or format issues.

- [ ] **Step 3: Run all tests**

Run:
```bash
cd ~/projects/hippo && cargo test && uv run --project brain pytest brain/tests -v
```
Expected: All Rust and Python tests pass.

- [ ] **Step 4: Create the baseline v0.1.0 tag**

Run:
```bash
cd ~/projects/hippo && git tag v0.1.0
```

- [ ] **Step 5: Verify tagged version**

Run:
```bash
cd ~/projects/hippo && cargo build -p hippo-daemon && ./target/debug/hippo --version
```
Expected: `hippo 0.1.0` (exactly at the tag, no dev suffix).

- [ ] **Step 6: Verify dev version after a new commit**

Make a trivial commit to move past the tag, then rebuild:

```bash
cd ~/projects/hippo && cargo build -p hippo-daemon && ./target/debug/hippo --version
```
Expected: `hippo 0.1.0-dev.N+g<sha>` where N is the number of commits after v0.1.0.

- [ ] **Step 7: Verify mise stamp:version matches**

Run:
```bash
cd ~/projects/hippo && mise run stamp:version && cat brain/src/hippo_brain/_version.py
```
Expected: The version string in `_version.py` matches what `hippo --version` shows.

- [ ] **Step 8: Verify dirty detection**

Run:
```bash
cd ~/projects/hippo && echo "# temp" >> README.md && cargo build -p hippo-daemon && ./target/debug/hippo --version && git checkout README.md
```
Expected: Version includes `.dirty` suffix, e.g. `hippo 0.1.0-dev.N+g<sha>.dirty`. After checkout, working tree is clean again.
